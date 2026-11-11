# Runbook

Every alert in `monitoring/alert-rules.yaml` links to a section here. First step for every alert:

```bash
kubectl -n demo get pods -o wide
python -m remediate.cli -n demo --from-cluster      # what the detector sees and proposes
```

## High error rate
1. Check whether a deploy just happened: `kubectl -n demo rollout history deployment/web`.
2. If yes, roll back: `kubectl -n demo rollout undo deployment/web`.
3. If no, look at logs: `kubectl -n demo logs deploy/web --since=10m`.

## High latency
1. Check CPU throttling and pod count: `kubectl -n demo top pods` (needs metrics-server).
2. Check the HPA: `kubectl -n demo get hpa web`.

## OOMKilled
1. Did the new revision start the problem while the old one is fine? Roll back; do not raise memory (that hides a leak).
2. Did memory grow with real load on a stable version? Raise the limit (the detector proposes +50%, capped at 1 Gi) and open a ticket to profile memory.

## CrashLoopBackOff
1. `kubectl -n demo logs <pod> --previous` to see why the last container exited.
2. After a deploy: roll back. Otherwise fix the cause (config, secret, dependency).

## ImagePullBackOff
1. `kubectl -n demo describe pod <pod>`: wrong tag, missing registry credentials, or registry down.
2. Roll back to the last image that pulled correctly.

## Rollout stuck
1. `kubectl -n demo rollout status deployment/web` and `kubectl -n demo describe pod <new pod>`.
2. With `maxUnavailable: 0` the old pods keep serving, so there is time. Roll back, then fix the readiness problem.
