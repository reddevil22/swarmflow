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
- [Executive summary](docs/EXECUTIVE_SUMMARY.md) - what it does, how local inference is used
- [Independent review](docs/PROJECT_REVIEW_glm5.3-flash.md) - adversarial audit of an earlier revision
- [Agent guide](AGENTS.md) - for agents working on this repo

## Quickstart
```bash
pip install -e ".[dev]"          # runtime deps: pyyaml + psutil; pytest for development
swarmflow init                   # writes the per-user config (printed on screen)
$EDITOR <that path>              # set your worker model id and CLI paths
swarmflow plan --prd PRD.md --project <path> --load   # frontier planner -> plan.yaml
swarmflow smoke-frontier         # configured frontier backend reachability
swarmflow smoke-worker           # local worker reachability (Pi + model server)
swarmflow plan-load --plan examples/smoke-plan.yaml --project <path>
swarmflow freeze --project <path>
swarmflow wave-run --wave 1
swarmflow audit --project <path> --json
swarmflow verify --task <id>     # frontier verification of one delivered task
swarmflow accept --project <path>  # frontier acceptance over the evidence bundle
swarmflow status --json                # ledger state
swarmflow trace logs/<task>_a1.jsonl   # session-trace analysis (always JSON)
```

Frontier-role exit codes (shared by `plan`/`verify`/`accept`): `0` positive verdict,
`1` the model responded but the result is unusable or negative (invalid plan,
`needs_fix`, `rejected`), `2` infrastructure (no backend configured, transport error,
nothing to verify/accept). Each call writes its raw output to
`<project>/.swarmflow/evidence/{plan,verify_<id>,accept}.json`; `swarmflow plan` also
persists the PRD it used to `<project>/.swarmflow/PRD.md` (hash-pinned) so verifiers can
compare the delivery against the original request.

Brownfield (existing repositories):
```bash
swarmflow recon --project <repo>            # stacks, commands, tests, git state
# write plan.yaml with `mode: brownfield` (the planner gets the recon digest)
swarmflow plan-load --plan plan.yaml        # clean-tree check, run branch, .swarmflow/
swarmflow wave-run --wave 1                 # sweep + regression gate + discrimination + audit
swarmflow evidence --project <repo>         # bundle for the verifier/acceptance pass
```

Useful wave flags: `--rebaseline` (re-record the regression baseline when the current
state is known-good), `--strict` (force fail-closed comparison), `--skip-regression`,
`--concurrency N`. `swarmflow recon --regression-command "<cmd>"` overrides detection
for a single survey; `swarmflow freeze --mode brownfield` snapshots the git baseline.

## Swapping models (providers)

Model choice is configuration, not code. Name the endpoints you use once, select one per
role, and prompts, gates and evidence stay identical:

```yaml
providers:
  commandcode:
    frontier:
      backend: openai
      base_url: "https://api.commandcode.ai/provider/v1"
      api_key: "${COMMANDCODE_API_KEY}"
      model: deepseek/deepseek-v4-flash
    worker: "commandcode/deepseek/deepseek-v4-flash"
  local-qwen:
    frontier: {backend: openai, base_url: "http://127.0.0.1:8000/v1",
               model: qwen3.8-flash-next}
    worker: "local/qwen3.8-flash-next"

role_providers: {frontier: commandcode, worker: commandcode}
```

- `frontier` serves planning, verification and acceptance. Any OpenAI-compatible
  `/chat/completions` endpoint (`openai`), the Command Code CLI (`commandcode`), an
  arbitrary agent CLI (`cli`) or Pi (`pi`); a provider's `frontier` block overrides the
  `frontier:` defaults, so switching endpoints or models is one line.
- `worker` selects the Pi `provider/model` that wave sessions run on - swap it to move
  the code-writing swarm between a local endpoint and a hosted one.
- Keys expand from the environment (`${COMMANDCODE_API_KEY}`), so secrets stay out of the
  file, and `swarmflow smoke-frontier` / `smoke-worker` verify a selection before a run.
- Prefer `backend: openai` for the frontier roles: they are text-in/text-out, so the
  agent harness buys nothing. Measured on the same acceptance material, the CLI path sent
  31.5k input tokens where the HTTP path sent 4.5k and returned valid JSON in 34s - and
  an agentic harness can decide to go explore the repository instead of answering.

## How it works (short version)
```
PRD --(frontier: plan, operator-run)--> work packages + frozen contracts + acceptance
    --(scaffold + freeze)--> repo, AGENTS.md worker rules, sha256 baseline
    --(swarm waves)--> modules + tests + reports, disjoint file ownership
    --(gate pipeline)--> process sweep, regression fingerprints, discrimination check,
                         scope audit (all automatic per wave)
    --(integrate)--> shared-file wiring + end-to-end probe
    --(accept, operator-run)--> evidence bundle mapped to acceptance criteria
```
Guardrails exist because of observed failures in real runs: turn caps, forbidden-action
scanning (`npm install`, `pnpm`, `rm -rf node_modules`), frozen-file integrity auditing,
admission control against the model server, and reasoning-spiral retries at lower
thinking. See the case study for what each one caught.

## Status
Alpha. The deterministic pipeline is implemented and was validated end-to-end on a 5-task
NestJS project (48 unit + 11 e2e tests, external HTTP probe 13/13). Frontier roles are
wired: `swarmflow plan` (PRD -> validated plan), `swarmflow verify` / `wave-run --verify`
(delivered -> verified/needs_fix) and `swarmflow accept` (verified -> accepted). PR
automation and packaged-artifact delivery are intentionally out of scope for now.

## License
MIT - see [LICENSE](LICENSE).
