import json

import pytest

from remediate.agent import propose
from remediate.classify import CRASH_LOOP, IMAGE_PULL, NOT_READY, OOM_KILLED, RESTARTING, classify
from remediate.cli import main
from remediate.plan import ROLLOUT_RESTART, ROLLOUT_UNDO, SET_MEMORY_LIMIT, Plan, playbook, to_kubectl, to_mebibytes, verify

ALLOWED = {"demo"}


def pods(*items):
    return {"items": list(items)}


# ---------- classification ----------
def test_healthy_pods_have_no_incidents(pod):
    assert classify(pods(pod(), pod(name="web-7d9f8c6b5-fghij"))) == []


def test_oomkilled_is_detected_even_while_crashlooping(pod):
    p = pod(restarts=4, ready=False,
            state={"waiting": {"reason": "CrashLoopBackOff"}},
            last_state={"terminated": {"reason": "OOMKilled"}})
    [inc] = classify(pods(p))
    assert inc.kind == OOM_KILLED
    assert inc.deployment == "web" and inc.memory_limit == "128Mi"


@pytest.mark.parametrize("state,ready,restarts,kind", [
    ({"waiting": {"reason": "CrashLoopBackOff"}}, False, 5, CRASH_LOOP),
    ({"waiting": {"reason": "ErrImagePull"}}, False, 0, IMAGE_PULL),
    ({"running": {}}, False, 0, NOT_READY),
    ({"running": {}}, True, 6, RESTARTING),
])
def test_each_incident_kind(pod, state, ready, restarts, kind):
    [inc] = classify(pods(pod(state=state, ready=ready, restarts=restarts)))
    assert inc.kind == kind


def test_pod_warming_up_is_not_an_incident(pod):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    young = pod(ready=False, state={"running": {"startedAt": (now - timedelta(seconds=10)).isoformat()}})
    old = pod(ready=False, state={"running": {"startedAt": (now - timedelta(seconds=120)).isoformat()}})
    assert classify(pods(young), now=now) == []
    assert classify(pods(old), now=now)[0].kind == NOT_READY


def test_incidents_are_merged_per_deployment(fixture_file):
    incidents = classify(fixture_file("pods_oom.json"))
    assert len(incidents) == 1
    assert incidents[0].kind == OOM_KILLED
    assert len(incidents[0].pods) == 2 and incidents[0].restarts == 4


def test_pods_without_a_deployment_are_ignored(pod):
    p = pod()
    p["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "backup"}]
    assert classify(pods(p)) == []


def test_untrusted_messages_are_truncated(pod):
    p = pod(ready=False, state={"waiting": {"reason": "CrashLoopBackOff", "message": "x\n" * 500}})
    [inc] = classify(pods(p))
    assert len(inc.detail) <= 200 and "\n" not in inc.detail


# ---------- playbooks ----------
def test_oom_playbook_raises_limit_by_half(pod):
    [inc] = classify(pods(pod(last_state={"terminated": {"reason": "OOMKilled"}}, restarts=1, ready=False)))
    plan = playbook(inc)
    assert plan.action == SET_MEMORY_LIMIT and plan.value == "192Mi"


def test_oom_at_cap_rolls_back_instead(pod):
    [inc] = classify(pods(pod(memory="1Gi", last_state={"terminated": {"reason": "OOMKilled"}}, ready=False)))
    assert playbook(inc).action == ROLLOUT_UNDO


def test_oom_in_new_revision_rolls_back_instead_of_raising_memory(pod):
    old = pod(name="web-1111aaaa-ok001", pod_hash="1111aaaa")
    new = pod(name="web-2222bbbb-bad01", pod_hash="2222bbbb", ready=False, restarts=2,
              last_state={"terminated": {"reason": "OOMKilled"}})
    [inc] = classify(pods(old, new))
    assert inc.healthy_other_revision
    assert playbook(inc).action == ROLLOUT_UNDO


def test_crashloop_rolls_back(pod):
    [inc] = classify(pods(pod(state={"waiting": {"reason": "CrashLoopBackOff"}}, ready=False, restarts=5)))
    assert playbook(inc).action == ROLLOUT_UNDO


def test_memory_units():
    assert to_mebibytes("1Gi") == 1024
    assert to_mebibytes("512Mi") == 512
    assert round(to_mebibytes("1G")) == 954
    with pytest.raises(ValueError):
        to_mebibytes("lots")


# ---------- verification gate ----------
def oom_incidents(pod):
    return classify(pods(pod(last_state={"terminated": {"reason": "OOMKilled"}}, ready=False, restarts=2)))


def test_playbook_plan_passes_verification(pod):
    incs = oom_incidents(pod)
    ok, problems = verify(playbook(incs[0]), incs, ALLOWED)
    assert ok, problems


@pytest.mark.parametrize("bad,expected", [
    ({"action": "delete_namespace", "namespace": "demo", "deployment": "web"}, "allowlist"),
    ({"action": ROLLOUT_UNDO, "namespace": "kube-system", "deployment": "web"}, "outside the allowed scope"),
    ({"action": ROLLOUT_UNDO, "namespace": "demo", "deployment": "payments"}, "no open incident"),
    ({"action": ROLLOUT_UNDO, "namespace": "demo", "deployment": "web; rm -rf /"}, "not a valid name"),
    ({"action": SET_MEMORY_LIMIT, "namespace": "demo", "deployment": "web", "container": "app", "value": "64Gi"}, "above the cap"),
    ({"action": SET_MEMORY_LIMIT, "namespace": "demo", "deployment": "web", "container": "app", "value": "64Mi"}, "higher than the current"),
    ({"action": SET_MEMORY_LIMIT, "namespace": "demo", "deployment": "web", "container": "app", "value": "512Mi"}, "at most double"),
    ({"action": SET_MEMORY_LIMIT, "namespace": "demo", "deployment": "web", "container": "sidecar", "value": "192Mi"}, "does not match"),
])
def test_gate_rejects_unsafe_plans(pod, bad, expected):
    ok, problems = verify(bad, oom_incidents(pod), ALLOWED)
    assert not ok
    assert any(expected in p for p in problems), problems


def test_kubectl_is_rendered_as_argv_not_shell():
    cmd = to_kubectl(Plan(SET_MEMORY_LIMIT, "demo", "web", "app", "192Mi"))
    assert cmd == ["kubectl", "-n", "demo", "set", "resources", "deployment/web", "-c", "app", "--limits=memory=192Mi"]
    assert to_kubectl(Plan(ROLLOUT_RESTART, "demo", "web"))[-2:] == ["restart", "deployment/web"]


# ---------- AI proposals go through the same gate ----------
def test_verified_llm_plan_is_used(pod):
    incs = oom_incidents(pod)
    llm = lambda _prompt: json.dumps({"action": SET_MEMORY_LIMIT, "namespace": "demo", "deployment": "web",
                                      "container": "app", "value": "256Mi", "reason": "leak suspected"})
    plan, audit = propose(incs[0], incs, ALLOWED, llm)
    assert plan.value == "256Mi" and audit["source"] == "llm (verified)"


def test_dangerous_llm_plan_falls_back_to_playbook(pod):
    incs = oom_incidents(pod)
    llm = lambda _prompt: json.dumps({"action": "delete_namespace", "namespace": "kube-system", "deployment": "web"})
    plan, audit = propose(incs[0], incs, ALLOWED, llm)
    assert plan.action == SET_MEMORY_LIMIT and plan.value == "192Mi"
    assert audit["source"] == "playbook" and audit["llm_rejected"]


def test_broken_llm_output_falls_back(pod):
    incs = oom_incidents(pod)
    plan, audit = propose(incs[0], incs, ALLOWED, lambda _p: "sure! here is my plan...")
    assert plan.action == SET_MEMORY_LIMIT and "llm_error" in audit


# ---------- CLI ----------
def test_cli_reports_and_exits_nonzero(fixture_file, tmp_path, capsys):
    f = tmp_path / "pods.json"
    f.write_text(json.dumps(fixture_file("pods_oom.json")))
    assert main(["--namespace", "demo", "--file", str(f)]) == 1
    out = capsys.readouterr().out
    assert "[OOMKilled] demo/web" in out and "verify : PASS" in out


def test_cli_clean_cluster_exits_zero(pod, tmp_path):
    f = tmp_path / "pods.json"
    f.write_text(json.dumps(pods(pod())))
    assert main(["--namespace", "demo", "--file", str(f)]) == 0


# ---------- lessons from the first game day ----------
def rs(name, revision, namespace="demo"):
    return {"metadata": {"name": name, "namespace": namespace,
                         "annotations": {"deployment.kubernetes.io/revision": str(revision)}}}


def test_terminating_pods_are_ignored(pod):
    p = pod(last_state={"terminated": {"reason": "OOMKilled"}}, ready=False, restarts=6)
    p["metadata"]["deletionTimestamp"] = "2026-11-11T06:50:40Z"
    assert classify(pods(p)) == []


def test_failures_in_an_older_revision_are_ignored(pod):
    # Game day 1: after the memory bump, the newest revision was briefly healthy while a pod of the
    # older leaking revision was still crashing. Rolling back from there would restore the leak.
    old_failing = pod(name="web-5fd444b58-aaaaa", pod_hash="5fd444b58", ready=False, restarts=9,
                      last_state={"terminated": {"reason": "OOMKilled"}})
    new_healthy = pod(name="web-d78f8f59c-bbbbb", pod_hash="d78f8f59c")
    replicasets = {"items": [rs("web-5fd444b58", 2), rs("web-d78f8f59c", 3)]}
    assert classify(pods(old_failing, new_healthy), rs_list=replicasets) == []


def test_rollback_only_when_newest_revision_fails(pod):
    good = pod(name="web-7d9cb49cf7-ccccc", pod_hash="7d9cb49cf7")
    bad = pod(name="web-5fd444b58-ddddd", pod_hash="5fd444b58", ready=False, restarts=2,
              last_state={"terminated": {"reason": "OOMKilled"}})
    replicasets = {"items": [rs("web-7d9cb49cf7", 4), rs("web-5fd444b58", 5)]}
    [inc] = classify(pods(good, bad), rs_list=replicasets)
    assert inc.revision == 5 and inc.healthy_other_revision
    assert playbook(inc).action == ROLLOUT_UNDO
