"""Turn `kubectl get pods -o json` output into a list of incidents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Incident kinds, most severe first.
OOM_KILLED = "OOMKilled"
CRASH_LOOP = "CrashLoopBackOff"
IMAGE_PULL = "ImagePullBackOff"
NOT_READY = "NotReady"
RESTARTING = "Restarting"

IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}


@dataclass
class Incident:
    kind: str
    namespace: str
    deployment: str
    pod: str
    container: str
    restarts: int = 0
    memory_limit: str | None = None
    detail: str = ""
    pod_hash: str = ""
    revision: int | None = None  # Deployment revision of the failing pods (from their ReplicaSet), if known
    healthy_other_revision: bool = False  # another revision of this Deployment is still serving healthy pods
    pods: list[str] = field(default_factory=list)


def _deployment_of(pod: dict) -> str | None:
    """Work out the owning Deployment from the ReplicaSet name and pod-template-hash label."""
    meta = pod.get("metadata", {})
    for owner in meta.get("ownerReferences", []) or []:
        if owner.get("kind") == "ReplicaSet":
            rs = owner.get("name", "")
            pod_hash = meta.get("labels", {}).get("pod-template-hash")
            if pod_hash and rs.endswith("-" + pod_hash):
                return rs[: -(len(pod_hash) + 1)]
            return rs.rsplit("-", 1)[0] if "-" in rs else rs
    return None


def _memory_limit(pod: dict, container_name: str) -> str | None:
    for c in pod.get("spec", {}).get("containers", []) or []:
        if c.get("name") == container_name:
            return c.get("resources", {}).get("limits", {}).get("memory")
    return None


def _short(text: str | None, limit: int = 200) -> str:
    """Pod messages are untrusted input: keep them short and single-line."""
    return " ".join((text or "").split())[:limit]


def _running_for(state: dict, now: datetime) -> float:
    """Seconds since the container started; unknown start times count as long-running."""
    started = (state.get("running") or {}).get("startedAt")
    if not started:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return float("inf")


def classify_pod(pod: dict, restart_threshold: int = 3, not_ready_grace_s: float = 60,
                 now: datetime | None = None) -> list[Incident]:
    """Classify one pod. A pod is only NotReady after failing readiness for longer than the grace
    period, so a normal rollout (new pod still warming up) is not reported as an incident."""
    now = now or datetime.now(timezone.utc)
    meta = pod.get("metadata", {})
    namespace = meta.get("namespace", "default")
    name = meta.get("name", "?")
    deployment = _deployment_of(pod)
    if deployment is None:
        return []  # only pods managed by a Deployment are in scope

    incidents: list[Incident] = []
    for cs in pod.get("status", {}).get("containerStatuses", []) or []:
        cname = cs.get("name", "?")
        restarts = int(cs.get("restartCount", 0))
        state = cs.get("state", {}) or {}
        last = (cs.get("lastState", {}) or {}).get("terminated", {}) or {}
        waiting = state.get("waiting", {}) or {}
        base = dict(namespace=namespace, deployment=deployment, pod=name, container=cname,
                    restarts=restarts, pod_hash=meta.get("labels", {}).get("pod-template-hash", ""))

        if last.get("reason") == "OOMKilled" or (state.get("terminated", {}) or {}).get("reason") == "OOMKilled":
            incidents.append(Incident(kind=OOM_KILLED, memory_limit=_memory_limit(pod, cname),
                                      detail="container exceeded its memory limit", **base))
        elif waiting.get("reason") == "CrashLoopBackOff":
            incidents.append(Incident(kind=CRASH_LOOP, detail=_short(waiting.get("message")), **base))
        elif waiting.get("reason") in IMAGE_PULL_REASONS:
            incidents.append(Incident(kind=IMAGE_PULL, detail=_short(waiting.get("message")), **base))
        elif "running" in state and not cs.get("ready", False) and _running_for(state, now) > not_ready_grace_s:
            incidents.append(Incident(kind=NOT_READY, detail="running but failing readiness probe", **base))
        elif restarts >= restart_threshold:
            incidents.append(Incident(kind=RESTARTING, detail=f"{restarts} restarts", **base))
    return incidents


def _is_terminating(pod: dict) -> bool:
    """Pods that are already being deleted (for example after a rollback) are not incidents."""
    return bool(pod.get("metadata", {}).get("deletionTimestamp"))


def _rs_revisions(rs_list: dict | None) -> dict[tuple[str, str], int]:
    """Map (namespace, ReplicaSet name) to the Deployment revision recorded on the ReplicaSet."""
    revisions: dict[tuple[str, str], int] = {}
    for rs in (rs_list or {}).get("items", []) or []:
        meta = rs.get("metadata", {})
        rev = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        if rev and str(rev).isdigit():
            revisions[(meta.get("namespace", "default"), meta.get("name", ""))] = int(rev)
    return revisions


def _rs_name(pod: dict) -> str | None:
    for owner in pod.get("metadata", {}).get("ownerReferences", []) or []:
        if owner.get("kind") == "ReplicaSet":
            return owner.get("name")
    return None


def _is_healthy(pod: dict) -> bool:
    statuses = pod.get("status", {}).get("containerStatuses", []) or []
    return bool(statuses) and all(cs.get("ready") and "running" in (cs.get("state") or {}) for cs in statuses)


def classify(pod_list: dict, restart_threshold: int = 3, not_ready_grace_s: float = 60,
             now: datetime | None = None, rs_list: dict | None = None) -> list[Incident]:
    """Classify every pod, then merge incidents of the same kind on the same Deployment.

    With `rs_list` (`kubectl get rs -o json`), the detector knows which revision each pod belongs
    to: it only reports failures in the newest revision (older ones are already being replaced)
    and only suggests a rollback when the failing pods are the newest revision.
    """
    merged: dict[tuple[str, str, str], Incident] = {}
    healthy_hashes: dict[tuple[str, str], set[str]] = {}
    revisions = _rs_revisions(rs_list)
    newest: dict[tuple[str, str], int] = {}
    pods = [p for p in (pod_list.get("items", []) or []) if not _is_terminating(p)]
    for pod in pods:
        dep, rs = _deployment_of(pod), _rs_name(pod)
        ns = pod.get("metadata", {}).get("namespace", "default")
        if dep and (ns, rs) in revisions:
            newest[(ns, dep)] = max(newest.get((ns, dep), 0), revisions[(ns, rs)])
    for pod in pods:
        dep = _deployment_of(pod)
        if dep and _is_healthy(pod):
            key = (pod.get("metadata", {}).get("namespace", "default"), dep)
            healthy_hashes.setdefault(key, set()).add(pod["metadata"].get("labels", {}).get("pod-template-hash", ""))
    for pod in pods:
        rev = revisions.get((pod.get("metadata", {}).get("namespace", "default"), _rs_name(pod)))
        for inc in classify_pod(pod, restart_threshold, not_ready_grace_s, now):
            inc.revision = rev
            latest = newest.get((inc.namespace, inc.deployment))
            if rev is not None and latest is not None and rev < latest:
                continue  # an older revision that the current rollout is replacing
            others = healthy_hashes.get((inc.namespace, inc.deployment), set()) - {inc.pod_hash}
            inc.healthy_other_revision = bool(others)
            key = (inc.namespace, inc.deployment, inc.kind)
            if key in merged:
                merged[key].pods.append(inc.pod)
                merged[key].restarts = max(merged[key].restarts, inc.restarts)
            else:
                inc.pods = [inc.pod]
                merged[key] = inc
    order = [OOM_KILLED, CRASH_LOOP, IMAGE_PULL, NOT_READY, RESTARTING]
    return sorted(merged.values(), key=lambda i: (order.index(i.kind), i.namespace, i.deployment))
