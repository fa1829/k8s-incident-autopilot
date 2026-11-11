# Game days

Each game day ships a bad change on purpose, then measures detection and recovery.
Run them with the **gameday** workflow (Actions tab) or by hand on kind, and fill in the results from the evidence files.

> Fill in only what you actually observed. Times come from `evidence/*-at.txt`.

## 1. Memory leak in a new release (`memory_leak`)

| | |
|---|---|
| Bad change | `kubectl -n demo set env deployment/web FAULT_MODE=memory_leak` |
| Expected | New pod OOMKilled within seconds; old pods keep serving because `maxUnavailable: 0` |
| Detected after | _fill in_ |
| Plan chosen | _fill in_ (expected: `rollout_undo`, because the old revision is healthy) |
| Recovered after | _fill in_ |
| What I learned | _fill in_ |

## 2. Crash on start (`crash`)

| | |
|---|---|
| Bad change | `kubectl -n demo set env deployment/web FAULT_MODE=crash` |
| Expected | CrashLoopBackOff on the new pod; rollout never completes |
| Detected after | _fill in_ |
| Plan chosen | _fill in_ |
| Recovered after | _fill in_ |
| What I learned | _fill in_ |

## 3. Readiness never passes (`not_ready`)

| | |
|---|---|
| Bad change | `kubectl -n demo set env deployment/web FAULT_MODE=not_ready` |
| Expected | New pod Running but not Ready; detector waits out the 60 s grace period, then reports NotReady |
| Detected after | _fill in_ |
| Plan chosen | _fill in_ |
| Recovered after | _fill in_ |
| What I learned | _fill in_ |

## 4. AI agent proposes something unsafe

| | |
|---|---|
| Setup | `python -m remediate.cli -n demo --from-cluster --use-llm` with a local Ollama model |
| What to record | The model's proposal, whether the gate accepted it, and why (see the `audit` field with `--json`) |
| Result | _fill in_ |
