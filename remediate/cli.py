"""Command line: read pod JSON, report incidents, propose verified fixes, optionally apply them.

Examples:
  kubectl get pods -n demo -o json | python -m remediate.cli --namespace demo
  python -m remediate.cli --namespace demo --from-cluster --apply
  python -m remediate.cli --namespace demo --from-cluster --use-llm
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from .agent import ollama_llm, propose
from .classify import classify
from .plan import ROLLOUT_RESTART, to_kubectl, verify


def _kubectl_json(namespace: str, kind: str) -> dict:
    out = subprocess.run(["kubectl", "-n", namespace, "get", kind, "-o", "json"],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_state(args: argparse.Namespace) -> tuple[dict, dict | None]:
    """Return (pods, replicasets). ReplicaSets are optional but make rollback decisions safer."""
    if args.from_cluster:
        return _kubectl_json(args.namespace, "pods"), _kubectl_json(args.namespace, "replicasets")
    pods = _read_json(args.file) if args.file else json.load(sys.stdin)
    return pods, (_read_json(args.rs_file) if args.rs_file else None)


def run(cmd: list[str], dry_run: bool) -> int:
    if dry_run:
        cmd = cmd + ["--dry-run=server"]
    print("  $", " ".join(cmd))
    return subprocess.run(cmd).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", "-n", required=True, help="namespace to inspect (also the only one allowed to change)")
    ap.add_argument("--file", help="read pod list JSON from a file instead of stdin")
    ap.add_argument("--rs-file", help="ReplicaSet list JSON (kubectl get rs -o json) to make the detector revision-aware")
    ap.add_argument("--from-cluster", action="store_true", help="run kubectl get pods yourself")
    ap.add_argument("--use-llm", action="store_true", help="ask a local Ollama model for a plan (still verified)")
    ap.add_argument("--apply", action="store_true", help="apply verified plans (server-side dry run first)")
    ap.add_argument("--json", action="store_true", help="print machine-readable output")
    args = ap.parse_args(argv)

    pods, replicasets = load_state(args)
    incidents = classify(pods, rs_list=replicasets)
    allowed = {args.namespace}
    llm = ollama_llm() if args.use_llm else None
    report = []

    if not incidents:
        print(json.dumps({"incidents": []}) if args.json else "No incidents found.")
        return 0

    for inc in incidents:
        plan, audit = propose(inc, incidents, allowed, llm)
        ok, problems = verify(plan, incidents, allowed) if plan else (False, ["no playbook"])
        entry = {"incident": inc.__dict__, "plan": plan.to_dict() if plan else None,
                 "verified": ok, "problems": problems, "audit": audit}
        report.append(entry)
        if not args.json:
            rev = f" revision={inc.revision}" if inc.revision is not None else ""
            print(f"[{inc.kind}] {inc.namespace}/{inc.deployment}{rev} pods={','.join(inc.pods)} restarts={inc.restarts}")
            print(f"  detail : {inc.detail}")
            if plan:
                print(f"  plan   : {plan.action} ({plan.reason}) source={audit['source']}")
                print(f"  verify : {'PASS' if ok else 'FAIL ' + '; '.join(problems)}")
                print(f"  command: {' '.join(to_kubectl(plan))}")
        if args.apply and plan and ok:
            if plan.action != ROLLOUT_RESTART and run(to_kubectl(plan), dry_run=True) != 0:
                print("  dry run failed; not applying")
                continue
            run(to_kubectl(plan), dry_run=False)

    if args.json:
        print(json.dumps({"incidents": report}, indent=2, default=str))
    return 1  # non-zero: incidents were found (useful in CI and game days)


if __name__ == "__main__":
    sys.exit(main())
