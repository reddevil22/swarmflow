# swarmflow

Turns a project spec or PRD into an MVP-1 deliverable using a swarm of local agent
sessions (Pi + the vLLM-served local model) managed by a deterministic control plane,
with a frontier model (deepseek-v4-flash via Command Code headless) used at decision gates.

## How it works (short version)

```
PRD --(frontier: plan)--> work packages + contracts + acceptance criteria
    --(scaffold)--> git repo + AGENTS.md rules + interfaces frozen
    --(swarm: N local Pi sessions)--> module implementations + tests + reports
    --(verify: scripts + frontier)--> requirement traceability, regression gates
    --(integrate)--> shared-file wiring + end-to-end demo
    --(PR gate: HUMAN)--> pull request reviewed by a person, then merged
    --(package)--> deployable artifact + evidence bundle
```

Hard-won operating rules baked in (see ARCHITECTURE.md):
- workers get disjoint file ownership; shared files belong to integration only
- success is judged by artifacts, never exit codes
- every task carries the validated worker rules (AGENTS.worker.md)
- reasoning-spiral failures (token cap) are detected from traces and retried at lower thinking
- admission control keeps the swarm at the measured sweet spot (~8-10 concurrent sessions)

## Quick start

```
pip install pyyaml        # only external dep
python -m swarmflow.cli smoke-frontier     # deepseek-flash reachable?
python -m swarmflow.cli smoke-worker       # local Pi worker reachable?
python -m swarmflow.cli plan load --plan plans/example.yaml
python -m swarmflow.cli wave run --wave 1
python -m swarmflow.cli status
```

## Status

Core control plane implemented: ledger, frontier client, worker runner, wave dispatch,
trace analysis, CLI. Roadmap (see PILOT.md): planner prompt hardening, verifier stage,
PR gate automation, packaging stage, pilot run on a real PRD.
