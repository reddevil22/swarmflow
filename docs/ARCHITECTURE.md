# swarmflow architecture

## Roles

| Role | Backed by | Responsibility |
|---|---|---|
| Planner / Verifier / Acceptator | a configured frontier backend (`frontier.py`: `openai` / `commandcode` / `cli` / `pi`), driven by `swarmflow/roles.py` | PRD decomposition, contract design, failure triage, verdicts, acceptance - invoked by `swarmflow plan` / `verify` / `accept` |
| Worker | local Pi session -> vllm-79/qwen36 | implement one pinned task (module + tests + report) |
| Control plane | `swarmflow` Python package | deterministic state machine: queue, dispatch, admission control, retries, artifact audits |
| Human | run-branch review | the only human checkpoint for now: review the branch (no PR automation) |

## Task state machine

```
queued -> running -> delivered -> verified -> accepted
             |            |            |
             v            v            v
          failed       needs_fix     escalated
```

`delivered -> verified | needs_fix` is written by `swarmflow verify --task <id>` (or the
`wave-run --verify` stage) from the frontier verdict; `verified -> accepted` is written
by `swarmflow accept --project <path>`. `integrated` and `awaiting_pr_review` have no
producer yet (PR automation is out of scope); `needs_fix` and `verified` tasks are not
dispatched by `wave-run` (it only picks `queued`) - they are recovered by a **fresh plan
that produces new task ids** (or an explicit operator decision).

Statuses live in a SQLite ledger (`swarmflow/ledger.py`): `tasks` table + append-only
`events` table. Every transition is an event; the ledger is the single source of truth.

## Worker contract

Each task ships a prompt with: enumerated requirements, edge cases, forbidden scope,
pinned-test rules, and the report format. The canonical rules text is `AGENTS.worker.md`;
scaffolded projects also receive it as `AGENTS.md` so workers load it as context.

Measured behavior this design relies on (stress test, 2026-09-19):
- 10 concurrent workers: server never queued (waiting=0), KV peak 45%
- per-stream ~3.5x slower than solo, aggregate ~2.5-2.9x solo -> sweet spot ~8-10 workers
- rules-via-context yielded 10/10 compliant reports and zero scope drift
- observed failure classes and their counters (below)

## Failure taxonomy -> automated response

| Failure | Detection | Response |
|---|---|---|
| reasoning spiral into output cap | trace: output tokens >= 0.9*cap and empty final text, or no `agent_end` | retry same spec with thinking `medium` (proven recovery) |
| timeout / stall | process timeout (config `worker_timeout_s`) | kill, mark failed, requeue once |
| tests pass but requirements unmet | verifier stage (roadmap) + acceptance scripts | targeted fix task referencing the finding |
| silent cross-cutting regression | MUST-KEEP-WORKING contract suite run per wave | reject delivery, fix task |
| regression introduced at a wave boundary | regression gate re-runs the project suite after every wave and diffs failing-test fingerprints (baseline in the control-plane state store) | wave marked failed, ledger event, compare file, fix task |
| worker rewrites gate state (`recon.json`, `run.json`, project `frozen.json`) | gate inputs live in `REPO_ROOT/state/runs/<hash-of-project>/`; the regression command is frozen at plan-load and never re-derived from the project | audit outcome unchanged; the project-side `run.json` is only a marked mirror |
| frozen file modified between waves (laundering) | the per-wave freeze carries unowned hashes instead of re-hashing them; the wave that owns a file seals its post-wave content (`audit.seal`) | the finding stays red until an explicit `swarmflow freeze` re-baseline |
| tests pass but do not discriminate (accommodating tests) | discrimination check: the wave's owned test files re-run at the run's base commit in a throwaway git worktree | verdicts in the ledger + evidence bundle; `discrimination.mode: enforce` fails the wave |
| suite weakened or deleted while staying green | executed-test inventory (skips/ignores excluded) compared against the baseline | wave marked failed (`suite shrank 13 -> 5`) |
| uncomparable suite results (unknown runner, legacy baseline) | both runs red with no fingerprints on either side | fail closed under `regression.strict` (default); warning otherwise |
| leaked dev server / watcher poisons later probes | tree-safe termination at worker exit + a post-wave process sweep (snapshot diff, cmdline/cwd attribution, listening ports) run before the regression gate | orphans reported in the ledger + evidence; `sweep.mode: kill` terminates this wave's new processes (pre-existing listeners are reported, never killed) |
| modify task delivered without touching its owned files | dispatch-time sha256 snapshots of owned files | outcome `no_changes`, wave fails |
| scope creep | ownership map diff audit (roadmap) | reject delivery |
| engine saturation | `/metrics`: waiting > 0 or KV > threshold | hold dispatches (admission control) |

Audit modes: greenfield uses a walk-based snapshot with persisted ignore lists;
brownfield uses git itself (tracked hashes must match unless owned; untracked-unowned
files are violations; gitignored files are invisible). Brownfield freezes happen per
wave, scoping an integration task's ownership of shared files to its own wave - and the
freeze **carries** the previous baseline's hashes for files the wave does not own, so an
edit made between waves stays visible until an explicit `swarmflow freeze` re-baseline.
The wave that owns a file seals its post-wave content (`audit.seal`), which is the only
moment an owned change becomes the new baseline.

Exit codes are never trusted as success signals (observed rc=0 with an empty deliverable).

The regression gate compares failure *identity*, not counts: failing tests are
fingerprinted (runner summary lines plus embedded typechecker/linter errors) and the
executed-test inventory is compared alongside. A red -> red run where a different test
broke therefore fails the gate where a count comparison cannot see it, and a suite that
quietly shrinks while staying green is rejected as a regression.

## Verification principles (why the verifier is a separate role)

The local model is a strong local executor and a weak global verifier; in experiments its
own tests missed requirement gaps and edge cases roughly half the time. Verification therefore:
1. is authored against the spec, not against the implementation
2. uses independent recomputation / smoke probing by a different method than the author's
3. applies revert/discrimination checks where a change has a pre-change behavior
   (mechanized per wave by `swarmflow/discrimination.py`: parent-state worktree run)

## Concurrency and capacity (measured)

- worker session ~15-16 min under 10-way load for 13-28 turns (~6-27K output tokens)
- plan wall time ~= modules/concurrency * per-module minutes + verification waves
- budget 15-20% retry overhead; frontier calls cost ~18K input tokens baseline each

## Frontier integration

Frontier roles (plan / verify / accept) are text-in / text-out completions served by a
**selectable backend** (`swarmflow/frontier.py`), so no specific vendor CLI is required:

| backend | what it runs | notes |
|---|---|---|
| `openai` | any OpenAI-compatible `/chat/completions` endpoint (OpenRouter, DeepSeek API, Groq, Ollama, vLLM, ...) | stdlib-only client, no SDK dependency; `extra_body` passes vendor knobs |
| `commandcode` | Command Code CLI headless (`cmd -p --output-format json`) | NDJSON `{"type":"result"}` parsed for text + usage |
| `cli` | any terminal agent CLI via an argv template | tokens support `{prompt_file}` / `{prompt}`; `output: text\|json` with optional dotted `result_path` |
| `pi` | Pi itself with a provider model configured in Pi | final answer extracted from the session trace |

`backend: auto` (default) resolves deterministically: explicit `frontier.command` wins,
then `frontier.base_url`, then a configured or PATH-discovered Command Code CLI. All
backends return the same normalized dict (`ok, backend, subtype, final_text, usage,
duration_ms, session_id, exit_code`); `max_turns`/`effort` are best-effort and ignored
by single-shot HTTP backends. `swarmflow smoke-frontier` verifies whichever backend is
configured.

## Repository layout

```
swarmflow/            control plane package (config, ledger, runstate, frontier, workers,
                      recon, plan, audit, regression, discrimination, procs, sweep,
                      evidence, cli)
config/               swarmflow.yaml (models, concurrency, timeouts, paths)
prompts/              planner, task brief, verifier, acceptance templates
AGENTS.worker.md      canonical worker rules (copied into scaffolded projects)
tests/                unit tests for ledger, frontier parsing, trace analysis
```
