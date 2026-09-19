# swarmflow

> Deterministic control plane for orchestrating swarms of local coding agents on
> greenfield projects: PRD in, verified MVP out.

swarmflow coordinates many local agent sessions (e.g. [Pi](https://pi.dev) against a
local OpenAI-compatible model server) to build a project in parallel waves, while a
frontier model handles planning, verification and acceptance. Frontier access is
backend-agnostic: any OpenAI-compatible API (stdlib client, no SDK), the Command Code
CLI, a generic adapter for any terminal agent CLI, or Pi itself. A SQLite ledger is the
single source of truth; every claim is verified by artifacts, never by exit codes.

## Documentation
- [Architecture](docs/ARCHITECTURE.md) - roles, state machine, failure taxonomy, capacity numbers
- [Workflow](docs/WORKFLOW.md) - PRD to MVP stages, evidence bundle
- [Plan schema](docs/PLAN_SCHEMA.md) - the machine-readable plan format
- [Case study](docs/CASE_STUDY.md) - first full validation run (TaskDock MVP-1)
- [Agent guide](AGENTS.md) - for agents working on this repo

## Quickstart
```bash
pip install -e ".[dev]"          # stdlib + pyyaml; pytest for development
swarmflow init                   # writes config/swarmflow.yaml from the example
$EDITOR config/swarmflow.yaml    # set your worker model id and CLI paths
swarmflow smoke-frontier         # configured frontier backend reachability
swarmflow smoke-worker           # local worker reachability (Pi + model server)
swarmflow plan-load --plan examples/smoke-plan.yaml --project <path>
swarmflow freeze --project <path>
swarmflow wave-run --wave 1
swarmflow audit --project <path> --json
```

## How it works (short version)
```
PRD --(frontier: plan)--> work packages + frozen contracts + acceptance criteria
    --(scaffold + freeze)--> repo, AGENTS.md worker rules, sha256 baseline
    --(swarm waves)--> modules + tests + reports, disjoint file ownership
    --(audit + verify)--> regression suite, scope audit, verifier findings
    --(integrate)--> shared-file wiring + end-to-end probe
    --(accept)--> evidence bundle mapped to acceptance criteria
```
Guardrails exist because of observed failures in real runs: turn caps, forbidden-action
scanning (`npm install`, `pnpm`, `rm -rf node_modules`), frozen-file integrity auditing,
admission control against the model server, and reasoning-spiral retries at lower
thinking. See the case study for what each one caught.

## Status
Alpha. The core pipeline is implemented and was validated end-to-end on a 5-task
NestJS project (48 unit + 11 e2e tests, external HTTP probe 13/13, frontier verifier
verdict `pass`). PR automation is intentionally out of scope for now.

## License
MIT - see [LICENSE](LICENSE).
