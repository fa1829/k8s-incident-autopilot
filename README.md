# k8s-incident-autopilot

Break a Kubernetes service on purpose, detect it, and fix it automatically. Any AI-proposed fix has to pass the same safety checks as a hand-written one.

This is a personal learning project about one question: **how do you turn a recurring incident into automation that removes the whole class of incident, without letting automation (or an AI agent) do something dangerous?**

```
 bad deploy ──► pods fail ──► detector reads pod state ──► plan (playbook or LLM)
                                                              │
                                     verification gate ◄──────┘
                                     (allowlist, scope, limits, dry run)
                                              │
                                   kubectl rollout undo / set resources ──► service recovers
```

## What is in it

| Part | What it does |
|---|---|
| `app/` | Small FastAPI service with liveness and readiness probes, Prometheus metrics and **fault modes** (`memory_leak`, `crash`, `not_ready`, `slow`, `errors`) to reproduce incidents. |
| `Dockerfile` | Multi-stage build; non-root user; no build tools in the runtime image. |
| `k8s/` | Kustomize manifests: Deployment with probes, resource limits, zero-downtime rollout (`maxUnavailable: 0`), hardened security context, PodDisruptionBudget, HPA; a `kind` overlay. |
| `monitoring/` | Prometheus alert rules (error rate, p95 latency, OOMKilled, crash loops, stuck rollouts) **with unit tests** run by `promtool`. |
| `remediate/` | Incident detector and remediation: classifies `kubectl get pods -o json` (OOMKilled, CrashLoopBackOff, ImagePullBackOff, NotReady, restarts), picks a playbook, and **verifies every plan** before it runs. |
| `remediate/agent.py` | Optional: asks a local LLM (Ollama) for a plan. The model is treated as untrusted; if its plan fails verification, the deterministic playbook is used and the rejection is logged. |
| `.github/workflows/ci.yml` | Lint, 38 unit tests, manifest schema validation (kubeconform), alert-rule tests, image build, Trivy scan, and an end-to-end deploy on a `kind` cluster. |
| `.github/workflows/gameday.yml` | One-click game day: ship a bad change, wait for detection, apply the verified fix, prove recovery, keep the evidence. |

## Design decisions worth discussing

- **Plans are data, not shell commands.** A plan is `{action, namespace, deployment, container, value}`, rendered to an argv list. There is no string a model could smuggle `; rm -rf /` into.
- **The gate does not trust anyone.** Allowed actions only (`rollout_undo`, `rollout_restart`, `set_memory_limit`); only the namespace you pass; only a deployment that has an open incident; memory may only go up, at most double, never above 1 Gi; server-side dry run before applying.
- **Do not hide a leak with more memory.** If a new revision is OOMKilled while the previous revision is still healthy, the playbook rolls back instead of raising the limit.
- **A release must stay healthy before it replaces anything.** `minReadySeconds: 30` keeps old pods serving until a new pod has been Ready for 30 seconds (added after game day 1, see below).
- **Only judge the newest revision.** With ReplicaSet data, the detector ignores failures in older revisions that a rollout is already replacing, and ignores pods that are terminating.
- **No false alarms during normal rollouts.** A pod that is still warming up is not "NotReady" until it has failed readiness for longer than a grace period.
- **Pod data is untrusted input.** Messages from the cluster are truncated and flattened before they reach a model prompt, and the prompt tells the model not to follow instructions inside them.

## Run it

```bash
# 1. Unit tests and lint
pip install -r requirements-dev.txt
ruff check . && pytest -q

# 2. Service + Prometheus with Docker Compose (try FAULT_MODE=errors, then open localhost:9090/alerts)
FAULT_MODE=errors docker compose up --build

# 3. Kubernetes on kind
kind create cluster --name lab --config k8s/kind-config.yaml
docker build -t incident-autopilot:dev . && kind load docker-image incident-autopilot:dev --name lab
kubectl apply -k k8s/overlays/kind && kubectl -n demo rollout status deployment/web

# 4. Break it, detect it, fix it
kubectl -n demo set env deployment/web FAULT_MODE=memory_leak     # a bad deploy
python -m remediate.cli -n demo --from-cluster                    # detect and show the plan
python -m remediate.cli -n demo --from-cluster --apply            # apply the verified fix
python -m remediate.cli -n demo --from-cluster --use-llm          # same, with a local LLM proposing the plan
```

Or run the **gameday** workflow from the GitHub Actions tab and download the evidence artifact.

## What I learned: the first game day broke my own design

**What I built.** A rollout with `maxUnavailable: 0` and readiness probes, so I expected a bad release to stall while the old pods kept serving. A detector would then see "new revision failing, old revision healthy" and roll back.

**How it broke.** I shipped a release that leaks memory. Its pods passed readiness in about 5 seconds and were OOMKilled about 14 seconds after start. Kubernetes saw 5 healthy seconds, finished the rollout, and removed both good pods within about 10 seconds. With no healthy revision left, my detector could not tell this was a bad release. It fell back to raising the memory limit from 128Mi to 192Mi, which only delayed the crash (OOMKilled again after about 20 seconds), and then it proposed 288Mi. The service crash-looped until I re-applied the known-good manifests from git.

**How I fixed it.**
1. **Platform:** `minReadySeconds: 30`. A new pod now has to stay Ready for 30 seconds before Kubernetes removes an old one. On the second run the leaking pod died at 14 seconds, the rollout stopped, both old pods kept serving, and the detector correctly chose `rollout_undo`. Detection took about a minute, and no serving capacity was lost.
2. **Detector:** it now reads ReplicaSet revisions and only acts on failures in the **newest** revision. During the first run it had briefly proposed rolling back to the leaking revision, because an old crashing pod made the newer one look like "the other, healthy revision". It also ignores pods that are already terminating, which caused a false alarm after recovery.

**What it taught me.**
- A readiness probe proves a pod can start, not that it will stay up. Rollouts need a soak period before old capacity is removed.
- More memory is the right fix for growth on a stable release and the wrong fix for a leak in a new one. The automation has to know which revision is failing before it chooses.
- The verification gate checks that an action is **safe** (allowlist, scope, limits, dry run). It cannot check that the action is **right**. Correctness comes from giving the detector better context, and from game days that test it.
- `kubectl rollout undo` fixes the cluster but leaves it different from git; Kubernetes warned about exactly that. Next step: have the bot open a pull request that reverts the bad change, so git stays the source of truth.

Full timings for each run are in [docs/GAMEDAYS.md](docs/GAMEDAYS.md).

## Known limitations
- If a release leaks memory and no healthy revision exists, the detector still proposes one memory increase (capped). It does not yet escalate to a human after a repeat OOM.
- `rollout undo` creates drift from git (see above).
- Tested on a single-node kind cluster, not on a managed cloud cluster.
