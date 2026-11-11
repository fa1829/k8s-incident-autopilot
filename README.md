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
| `.github/workflows/ci.yml` | Lint, unit tests, manifest schema validation (kubeconform), alert-rule tests, image build, Trivy scan, and an end-to-end deploy on a `kind` cluster. |
| `.github/workflows/gameday.yml` | One-click game day: ship a bad change, wait for detection, apply the verified fix, prove recovery, keep the evidence. |

## Design decisions worth discussing

- **Plans are data, not shell commands.** A plan is `{action, namespace, deployment, container, value}`, rendered to an argv list. There is no string a model could smuggle `; rm -rf /` into.
- **The gate does not trust anyone.** Allowed actions only (`rollout_undo`, `rollout_restart`, `set_memory_limit`); only the namespace you pass; only a deployment that has an open incident; memory may only go up, at most double, never above 1 Gi; server-side dry run before applying.
- **Do not hide a leak with more memory.** If a new revision is OOMKilled while the previous revision is still healthy, the playbook rolls back instead of raising the limit.
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

## Results

See [docs/GAMEDAYS.md](docs/GAMEDAYS.md) for each scenario: what broke, how long detection and recovery took, and what I learned.
