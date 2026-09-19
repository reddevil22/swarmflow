# swarmflow architecture

## Roles

| Role | Backed by | Responsibility |
|---|---|---|
| Planner / Verifier / Acceptator | deepseek-v4-flash via `cmd -p` headless (`FrontierClient`) | PRD decomposition, contract design, failure triage, verdicts, acceptance |
| Worker | local Pi session -> vllm-79/qwen36 | implement one pinned task (module + tests + report) |
| Control plane | `swarmflow` Python package | deterministic state machine: queue, dispatch, admission control, retries, artifact audits |
| Human | pull request review | the only human checkpoint for now: merge the PR or send it back |

## Task state machine

```
queued -> running -> delivered -> verified -> integrated -> accepted
             |            |            |
             v            v            v
          failed       needs_fix    escalated        (awaiting_pr_review between verified and integrated
                                                      when a review gate is configured)
```

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
| scope creep | ownership map diff audit (roadmap) | reject delivery |
| engine saturation | `/metrics`: waiting > 0 or KV > threshold | hold dispatches (admission control) |

Exit codes are never trusted as success signals (observed rc=0 with an empty deliverable).

## Verification principles (why the verifier is a separate role)

The local model is a strong local executor and a weak global verifier; in experiments its
own tests missed requirement gaps and edge cases roughly half the time. Verification therefore:
1. is authored against the spec, not against the implementation
2. uses independent recomputation / smoke probing by a different method than the author's
3. applies revert/discrimination checks where a change has a pre-change behavior

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
swarmflow/            control plane package (config, ledger, frontier, workers, plan, cli)
config/               swarmflow.yaml (models, concurrency, timeouts, paths)
prompts/              planner, task brief, verifier, acceptance templates
AGENTS.worker.md      canonical worker rules (copied into scaffolded projects)
tests/                unit tests for ledger, frontier parsing, trace analysis
```
