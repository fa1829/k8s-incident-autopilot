# Game days

Each game day ships a bad change on purpose, then measures detection and recovery.
Run them with the **gameday** workflow (Actions tab) or by hand on kind. Times below are from my own runs on a local kind cluster (WSL2, November 11, 2026).

## 1. Memory leak in a new release (`memory_leak`), first run: the platform let it through

| | |
|---|---|
| Bad change | `kubectl -n demo set env deployment/web FAULT_MODE=memory_leak` at 01:17:44 |
| What happened | New pods passed readiness after ~5 s. Kubernetes counted them as available, finished the rollout and removed **both** healthy pods within ~10 s. The new pods were OOMKilled ~14 s after start and then crash-looped (9 restarts each over ~22 minutes). |
| Detection | Detector reported `OOMKilled` correctly. |
| Plan chosen | `set_memory_limit` 128Mi → 192Mi. Wrong fix for a leak: with no healthy revision left, the detector could not tell this was a bad release. At 192Mi the pod still OOMKilled, about 20 s after start, and the detector then proposed 288Mi. |
| A second bug | Right after the memory change, the newest revision was briefly healthy while an old leaking pod was still crashing, so the detector proposed `rollout_undo`, which would have restored the leaking revision. |
| Recovery | Re-applied the known-good manifests from git (`kubectl apply -k`), healthy at ~01:42. |

## 2. Memory leak in a new release (`memory_leak`), after adding `minReadySeconds: 30`

| | |
|---|---|
| Bad change | Same change at 01:42:57 |
| What happened | The new pod passed readiness at 5 s and was OOMKilled at 14 s, before reaching 30 s of readiness. The rollout stopped there: **both old pods stayed 1/1 Running the whole time**, so serving capacity was never lost. |
| Detection | Within ~1 minute (first check after a 60 s watch). |
| Plan chosen | `rollout_undo`, because the new revision was OOMKilled while the previous revision was healthy. Verification: PASS. Server-side dry run, then applied. |
| Recovery | Rollout complete and `FAULT_MODE=none` confirmed at 01:50:40. (The gap is the time I took before running `make fix`; the fix itself took seconds.) |
| Found while recovering | The final check still reported the old leaking pod while it was terminating. `kubectl rollout undo` also warned that it does not update the `last-applied-configuration`, so the cluster no longer matched git. |

## 3. Crash on start (`crash`)

| | |
|---|---|
| Bad change | `kubectl -n demo set env deployment/web FAULT_MODE=crash` |
| Detected after | _fill in_ |
| Plan chosen | _fill in_ |
| Recovered after | _fill in_ |

## 4. Readiness never passes (`not_ready`)

| | |
|---|---|
| Bad change | `kubectl -n demo set env deployment/web FAULT_MODE=not_ready` |
| Detected after | _fill in_ (expected: after the 60 s grace period) |
| Plan chosen | _fill in_ |
| Recovered after | _fill in_ |

## 5. AI agent proposes something unsafe

| | |
|---|---|
| Setup | `python -m remediate.cli -n demo --from-cluster --use-llm --json` with a local Ollama model |
| Result | _fill in: the model's proposal, whether the gate accepted it, and why_ |
