"""Remediation plans: deterministic playbooks plus a verification gate for any plan.

A plan is a small, structured action. It is never a free-form shell command, so a plan
proposed by an AI model can be checked field by field before anything runs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .classify import CRASH_LOOP, IMAGE_PULL, NOT_READY, OOM_KILLED, RESTARTING, Incident

ROLLOUT_UNDO = "rollout_undo"
ROLLOUT_RESTART = "rollout_restart"
SET_MEMORY_LIMIT = "set_memory_limit"
ALLOWED_ACTIONS = {ROLLOUT_UNDO, ROLLOUT_RESTART, SET_MEMORY_LIMIT}

DEFAULT_MEMORY_CAP_MI = 1024  # never raise a limit above 1Gi automatically
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")  # DNS-1123 label


@dataclass
class Plan:
    action: str
    namespace: str
    deployment: str
    container: str | None = None
    value: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- memory quantities ----------
_UNITS = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1000 / 1024 ** 2,
          "M": 1000 ** 2 / 1024 ** 2, "G": 1000 ** 3 / 1024 ** 2}


def to_mebibytes(quantity: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|K|M|G)?", (quantity or "").strip())
    if not m:
        raise ValueError(f"unsupported memory quantity: {quantity!r}")
    number, unit = float(m.group(1)), m.group(2)
    if unit is None:
        return number / 1024 ** 2  # plain bytes
    return number * _UNITS[unit]


# ---------- deterministic playbooks ----------
def playbook(incident: Incident, memory_cap_mi: int = DEFAULT_MEMORY_CAP_MI) -> Plan | None:
    """The default, rule-based response to each incident kind."""
    ns, dep = incident.namespace, incident.deployment
    if incident.kind == OOM_KILLED:
        if incident.healthy_other_revision:
            # The new revision runs out of memory while the previous one is fine: that points to a
            # leak or regression in the change. Raising the limit would only hide it, so roll back.
            return Plan(ROLLOUT_UNDO, ns, dep,
                        reason="new revision is OOMKilled while the previous revision is healthy; roll back")
        if incident.memory_limit:
            current = to_mebibytes(incident.memory_limit)
            target = min(int(current * 1.5), memory_cap_mi)
            if target > current:
                return Plan(SET_MEMORY_LIMIT, ns, dep, incident.container, f"{target}Mi",
                            f"OOMKilled at {incident.memory_limit}; raise limit by 50% (cap {memory_cap_mi}Mi)")
        return Plan(ROLLOUT_UNDO, ns, dep, reason="OOMKilled at the memory cap; roll back the last change")
    if incident.kind in (CRASH_LOOP, IMAGE_PULL, NOT_READY):
        return Plan(ROLLOUT_UNDO, ns, dep, reason=f"{incident.kind} after a change; roll back to the last good revision")
    if incident.kind == RESTARTING:
        return Plan(ROLLOUT_RESTART, ns, dep, reason="repeated restarts without a clear cause; restart pods")
    return None


# ---------- verification gate ----------
def verify(plan: dict | Plan, incidents: list[Incident], allowed_namespaces: set[str],
           memory_cap_mi: int = DEFAULT_MEMORY_CAP_MI) -> tuple[bool, list[str]]:
    """Check a plan (from a playbook or an AI model) before it can run. Returns (ok, reasons)."""
    p = plan.to_dict() if isinstance(plan, Plan) else dict(plan or {})
    problems: list[str] = []

    action = p.get("action")
    if action not in ALLOWED_ACTIONS:
        problems.append(f"action {action!r} is not on the allowlist {sorted(ALLOWED_ACTIONS)}")

    ns, dep = p.get("namespace"), p.get("deployment")
    if not isinstance(ns, str) or not NAME_RE.match(ns):
        problems.append(f"namespace {ns!r} is not a valid name")
    elif ns not in allowed_namespaces:
        problems.append(f"namespace {ns!r} is outside the allowed scope {sorted(allowed_namespaces)}")
    if not isinstance(dep, str) or not NAME_RE.match(dep):
        problems.append(f"deployment {dep!r} is not a valid name")

    # A plan may only touch a deployment that actually has an open incident.
    matching = [i for i in incidents if i.namespace == ns and i.deployment == dep]
    if not matching:
        problems.append(f"no open incident for {ns}/{dep}; refusing to act on it")

    if action == SET_MEMORY_LIMIT:
        oom = [i for i in matching if i.kind == OOM_KILLED]
        if not oom:
            problems.append("set_memory_limit is only allowed for an OOMKilled incident")
        else:
            container = p.get("container")
            if container != oom[0].container:
                problems.append(f"container {container!r} does not match the OOMKilled container {oom[0].container!r}")
            try:
                new = to_mebibytes(str(p.get("value")))
                current = to_mebibytes(oom[0].memory_limit) if oom[0].memory_limit else None
                if new > memory_cap_mi:
                    problems.append(f"new limit {new:.0f}Mi is above the cap {memory_cap_mi}Mi")
                if current is not None and new <= current:
                    problems.append("new limit must be higher than the current limit")
                if current is not None and new > current * 2:
                    problems.append("new limit may be at most double the current limit")
            except ValueError as exc:
                problems.append(str(exc))
    return (not problems, problems)


def to_kubectl(plan: Plan | dict) -> list[str]:
    """Render a verified plan as an argv list (no shell, so nothing can be injected)."""
    p = plan.to_dict() if isinstance(plan, Plan) else plan
    ns, dep = p["namespace"], p["deployment"]
    if p["action"] == ROLLOUT_UNDO:
        return ["kubectl", "-n", ns, "rollout", "undo", f"deployment/{dep}"]
    if p["action"] == ROLLOUT_RESTART:
        return ["kubectl", "-n", ns, "rollout", "restart", f"deployment/{dep}"]
    if p["action"] == SET_MEMORY_LIMIT:
        return ["kubectl", "-n", ns, "set", "resources", f"deployment/{dep}",
                "-c", p["container"], f"--limits=memory={p['value']}"]
    raise ValueError(f"unknown action {p['action']!r}")
