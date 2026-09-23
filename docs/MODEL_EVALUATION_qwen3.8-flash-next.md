# Model evaluation — qwen3.8-flash-next (local vLLM endpoint)

- **Date:** 2026-09-21
- **Model:** `qwen3.8-flash-next`, served by a local vLLM server (OpenAI-compatible `/v1`)
- **Roles exercised:** frontier planner, frontier verifier, acceptance pass, and coding workers
  driven through Pi agent sessions
- **Endpoint:** host details intentionally omitted; see "Reproduction" for the config shape

This document records what the model actually produced and where it degraded. Every claim below
comes from a saved workspace, a worker trace, or a server counter — not from impressions.

## Attribution: proving which model answered

The single most important step in this evaluation, learned the hard way. The first long-horizon
run was scored against this model but was in fact served by the cloud fallback: the config's
`role_providers.worker` had reverted to the default provider, and the Pi provider entry for the
local endpoint had been removed during an earlier experiment. The run was discarded, the provider
entry restored, and the battery re-run with the role pinned to the local provider.

Every result below therefore carries two independent attributions:

- the worker trace names the model that answered (e.g. 52 model hits for one session), and
- the server's counters move by the session's own token counts.

Conclusion for the config layer: referencing a provider that does not exist in Pi's provider
list falls back silently to the default. The provider layer already warns about literal keys;
it should also warn when a referenced provider is missing.

## Results

| Run | Role / task | Thinking | Session cost | Independent oracle | Own tests | Outcome |
|---|---|---|---|---|---|---|
| sdlc-cli brownfield | worker, add optional check + 5 tests | default | 7 turns, 2.5k out tokens | suite 139 → 144, discrimination `fails_at_parent=1` | 5 added | delivered, verified, accepted |
| Multi-file build | worker, larger component with 8 behaviours | high | 17 turns, 22.4k out tokens, 10.5 min | 8/8 checks | 25 | pass |
| Behaviour-preserving refactor | worker, collapse duplication | high | delivered | 4/4 checks | existing file untouched | pass |
| Trap-spec sweep, thinking off | worker, small spec with edge cases | off | 46 turns, 19.0k out tokens, 469s | 5/5 checks | 20 | **turn cap, no report** |
| Trap-spec sweep, thinking high | same prompt | high | 13 turns, 5.6k out tokens, 143s | 5/5 checks | 20 | pass, with report |

For comparison, the same multi-file prompt on the cloud model: 4 turns, 2.0k output tokens, 52s,
also 8/8 — correct, but with 16 tests instead of 25.

Frontier roles on the sdlc-cli run were equally clean: the planner returned a usable plan on the
first attempt (no retry), with a correct contract for the new parameter; the verifier mapped all
seven spec requirements to named tests with no findings; the acceptance pass returned 2 of 2
criteria with no gaps and flagged one accurate residual risk that the worker's own report had
already identified.

## Output quality

- **Correctness.** Every deliverable that could be checked independently passed. Where it looked
  like a failure, the fault was in the evaluation harness (a poorly designed oracle check), not
  the model — see "Method notes".
- **Scope discipline.** On the brownfield change only the two files the task owned were modified,
  and the delivered report proposed the follow-on work rather than doing it. On the refactor, the
  test file was deliberately left untouched and the file's documented-but-untested behaviour
  (exact spacing, wider-number passthrough) survived the rewrite.
- **Spec discipline.** Invariants the spec stated were honoured in the small print, not just the
  happy path: no input mutation, plain tuples in the result, sorted output, empty intervals
  dropped before merging.
- **Test quality above contract.** Left to its own devices it writes more tests than asked (25 for
  8 behaviours) and reaches for strong techniques unprompted: an independent brute-force reference
  implementation, invariant assertions over the whole result, and deliberate mutation of the
  reference to confirm the tests actually bite.
- **Report quality.** When it converges, the report is precise and checkable — file and line
  references, the exact commands run, and honest statements about what was not verified.

The one genuine design divergence seen: given malformed input (`start > end`) that the spec does
not define, one run passed it through verbatim and documented why, the other silently dropped it.
Both are defensible; it is exactly the kind of ambiguity a plan contract should pin down before
dispatch.

## Failure modes

- **Non-convergence under thinking off.** The trap-spec run produced correct code and 20 tests,
  then spent roughly three quarters of its budget mutation-testing its own implementation and
  never reported. The turn cap is what ended it. The harness classified this correctly as a
  turn-cap failure even though the artifact was good.
- **Under-thinking is not cheap.** On the same task, thinking off cost 3.4× the tokens and 3.3×
  the wall-clock of thinking high, and still failed the session. For worker-style tasks on this
  model, thinking high is both better and cheaper — fewer turns means less context re-prefill.
- **Heavy reasoning by default.** A 512-token cap on a frontier call can be consumed entirely by
  reasoning, returning empty content with `finish_reason=length`. Frontier calls must disable
  thinking explicitly.
- **Verbosity relative to cloud.** Roughly 10× the tokens and 12× the wall-clock on the
  long-horizon task, in exchange for a larger test suite. Correct either way; the trade is cost.

## Configuration notes

- Frontier: `enable_thinking: false` via `extra_body.chat_template_kwargs`. Planner, verifier and
  acceptance all remained usable at this setting.
- Worker: `thinking: high` (or `medium`), mapped correctly through Pi's `qwen-chat-template`
  thinking format. Reserve `off` for trivial, tightly specified tasks.
- Concurrency: earlier runs on this box measured a KV-cache ceiling of roughly four concurrent
  sessions (~4.13 by the server's own counter). Four at once matched eight at once for throughput;
  eight only added latency. Session throughput measured 37–51 tokens/s, holding near that range
  under load.

## Method notes and harness observations

- **Vet the oracle before blaming the model.** One evaluation check expected touching intervals
  *not* to merge, contradicting the spec it was testing; it marked a correct answer as a failure.
  Re-running the fixed oracle against the saved workspace re-graded the run at 5/5, without
  re-spending a session. Saved workspaces make this cheap; use them.
- **`run_inline` always reports `no_changes`.** `delivery_changed` (`swarmflow/prompt.py:69`)
  diffs only the files present in the pre-run owned-file snapshot, and the inline convenience path
  passes none. The pipeline path takes that snapshot, so real runs classify correctly — this is a
  limitation of the harness path, not a delivery misclassification.
- **Preflight untracked-file ordering.** The brownfield preflight reads the untracked-file list
  before it adds its own entries to `.gitignore`, so a run's scope-audit exemption list can
  include the tool's own `.swarmflow/` artifacts. Harmless here (no audit failure followed), but
  the snapshot should be taken after the ignore-file update.
- **Possible follow-up (not implemented):** when a session ends `turn_cap` but the regression gate
  is green and an artifact exists, letting verification judge the artifact would convert a
  correct-but-unreported delivery into a decision instead of a retry. This is a product choice,
  not a defect fix.

## Reproduction

A role can be pointed at a local endpoint in two places; both must agree.

1. Pi must hold a provider entry for the endpoint (`baseUrl`, `openai-completions` API, the model
   id, and the `qwen-chat-template` thinking format).
2. The swarmflow config selects that provider for the role and optionally overlays the frontier
   block:

```yaml
role_providers:
  worker: <pi-provider>/qwen3.8-flash-next
frontier:
  backend: openai
  base_url: <local vllm endpoint>/v1
  model: qwen3.8-flash-next
  extra_body:
    chat_template_kwargs:
      enable_thinking: false
```

Sessions were run against the local server only; the box's counters moved from 20 to 141
successful requests and from 11.7k to 87.4k generated tokens across the window covered here
(4.7M prefill tokens in total, reflecting full-context re-prefill each turn).

## Basis

Compiled 2026-09-21 from: one end-to-end brownfield run on `sdlc-cli` (plan → load → wave with all
gates → accept), a split-test battery of long-horizon build, behaviour-preserving refactor and
trap-spec tasks scored by independent oracles, worker traces, saved workspaces, and the local
server's own metrics. Configuration used is local and not tracked.
