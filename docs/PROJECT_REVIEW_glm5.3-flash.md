# Project Review — swarmflow

> **Status banner (2026-09-20):** this review targets the revision at `78da8bd`. Several
> findings have since been fixed and are kept here for the record:
> - repo-checkout coupling — shipped assets moved into the package and resolved via
>   `importlib.resources`; state and config default to per-user paths
> - prompt templates had no callers — `plan`, `verify` and `accept` are wired, with
>   parsers and tests; the review's "no verify/accept commands" notes are superseded
> - `run_inline` ledger pollution — fixed, with a regression test
> - gate state was worker-writable — the control-plane state store lives outside the
>   project, and pre-existing untracked files are exempted by a preflight snapshot
> - `cli.py` god module and `WorkerRunner` god class — extracted into `pipeline.py`,
>   `prompt.py` and `trace.py`
> Remaining findings (frozen-file laundering variants, parser hardening, worker-authored
> test poisoning) are still open where the notes below describe them.

- **Date:** 2026-09-19
- **Review model:** glm5.3-flash (multi-agent review, five parallel axes)
- **Scope:** Full repository — `swarmflow/` (14 modules, ~3,600 LOC), `tests/` (16 files, 147 tests), `prompts/`, `config/`, `docs/`, `README.md`, `AGENTS.md`, `CONTRIBUTING.md`
- **Verified state:** `python -m pytest tests -q` → **130 passed, 1 skipped** (~3.5 min; runtime dominated by real git-worktree subprocess tests). Working tree clean at `78da8bd`.

## Executive summary

An unusually disciplined alpha. The deterministic control plane matches its documentation almost line-for-line, house rules from `AGENTS.md` are genuinely followed (not aspirational), and the test suite is substantive rather than smoke. The debt clusters in three places:

1. **The frontier-driven half is scaffolding** — planner/verifier/acceptance prompts have no caller or parser; 6 of 10 ledger statuses are never set; the upper state machine is dead surface.
2. **The anti-gaming gates can themselves be gamed** because their state (`recon.json`, `frozen.json`, `run.json`) lives inside the worker-writable project tree — the highest-severity finding of this review.
3. **Orchestration is trapped in `cli.py`** and low-level primitives (`_git`, run.json readers) are duplicated across modules.

---

## Axis 1 — Architecture & Design

### Strengths

- **High docs↔code fidelity on the deterministic core.** State-machine statuses (`swarmflow/ledger.py:8-19`), wave pipeline order (sweep → regression gate → discrimination → audit, `swarmflow/cli.py:295-372`, matching `docs/WORKFLOW.md` brownfield §4), config defaults (`swarmflow/config.py:41-47`), and the failure taxonomy all map to real, tested code.
- **"Artifacts, never exit codes" consistently implemented.** `workers._finalize` classifies from trace contents + filesystem (`swarmflow/workers.py:307-332`), `delivery_changed` catches no-op deliveries (`swarmflow/workers.py:159-166`), and `regression.compare` compares failure identity plus executed-test inventory, failing closed on indeterminate results (`swarmflow/regression.py:343-413`).
- **Deep, single-purpose leaf modules with stated invariants.** `procs.kill_tree` never signals its own process group (`swarmflow/procs.py:82-97`); `sweep` reports pre-existing listeners but never kills them (`swarmflow/sweep.py:74-98`); `audit.freeze` persists the effective ignore list inside the baseline (`swarmflow/audit.py:66-97`).
- **Fail-safe defaults and evidence-always-written.** Strict fail-closed regression, per-check `try/except` barriers so a check bug can't break a wave (`swarmflow/cli.py:380-387, 424-427`), and every wave gate writes its JSON evidence file regardless of outcome.
- **Discrimination check is a genuinely novel, well-engineered seam** — throwaway worktree at `base_sha`, junction/symlinked `node_modules`, verdict taxonomy with `preexisting_at_parent` escape hatch (`swarmflow/discrimination.py:95-242`).
- **Ledger discipline:** append-only events, whitelisted updatable fields, JSON-typed list columns (`swarmflow/ledger.py:51, 104-120`); tests mirror module structure 1:1.
- **Frontier normalization:** one `complete()` contract, deterministic `auto` resolution, injectable transport in `OpenAICompatBackend` (`swarmflow/frontier.py:72-97, 329-380`).

### Concerns

1. **Frontier decision-gate roles are documented but not wired in.** The only `.complete()` call in the package is the smoke test (`swarmflow/cli.py:48`). No plan/verify/accept commands; six of ten ledger statuses — `verified`, `needs_fix`, `escalated`, `awaiting_pr_review`, `integrated`, `accepted` — are never set (only `running`/`delivered`/`failed` at `swarmflow/workers.py:322, 413, 438`). The upper half of the documented state machine is dead code.
2. **The wave pipeline has no module — it lives in the CLI.** `cmd_wave_run` + `_run_discrimination`/`_run_sweep`/`_run_audit` (`swarmflow/cli.py:244-372, 375-500`) embed ~200 lines of orchestration policy inside argparse handlers. Any other front-end must re-implement the pipeline.
3. **Sequential finalization weakens supervision under concurrency.** `run_wave` finalizes chunk handles in order (`swarmflow/workers.py:445-449`); worker *k* is unpoliced (no turn-cap kill, no timeout) while workers 1..k-1 finalize; worst-case wall time ≈ k × `worker_timeout_s`. A poll loop over all handles would make the cap a true per-spawn invariant.
4. **Admission control fails open and runs once per wave.** `_wait_ready` gives up after ~10 minutes and dispatches anyway (`swarmflow/workers.py:338-347`); `read_server_load` returns `{}` on any error, treated as "proceed" (`swarmflow/workers.py:81-87, 342-343`). Not re-checked between chunks.
5. **Headline guardrails are hardcoded, not configured.** `FORBIDDEN_ACTIONS`, `SERVER_LAUNCH_RE`, `STACK_RULES` are module constants (`swarmflow/workers.py:106-176, 211-213`) while nearly everything else is in `config.py`. The forbidden scan is naive substring matching (`swarmflow/workers.py:226-235`) — fragile against quoted/aliased forms and prone to false positives (`echo go get`).
6. **Pi trace-format knowledge is duplicated three ways.** `workers.scan_trace` (`swarmflow/workers.py:25-67`), `frontier._pi_final_text` (`swarmflow/frontier.py:258-282`), and `evidence.py`'s reliance on `scan_trace` (`swarmflow/evidence.py:14, 87`) all encode the Pi JSONL grammar. A `trace.py` module would remove the odd `evidence → workers` dependency.
7. **Private helpers cross module boundaries.** `workers.py:15` imports `audit._hash_file`; `discrimination.py:15` imports `audit._git, _hash_file`; three near-identical `_git` wrappers exist (`swarmflow/audit.py:134`, `swarmflow/evidence.py:23`, `swarmflow/recon.py:41`).
8. **"SQLite ledger is the single source of truth" oversells it.** Critical run state is split across the ledger, `.swarmflow/run.json`, `.swarmflow/frozen.json`, and `recon.json`. Wave-level gate results are recorded as ledger events attributed to `tasks[0]["id"]` (`swarmflow/cli.py:338, 393, 439, 498`) — wave state has no first-class row. `set_status` accepts any transition (`swarmflow/ledger.py:104-120`), so the "deterministic state machine" is unenforced.
9. **Frontier extensibility has ceilings.** `build_backend` is a closed if-chain (`swarmflow/frontier.py:329-380`); new backends can't participate in `auto` resolution. `CommandCodeBackend` hardcodes Windows `cmd.exe /c` (`swarmflow/frontier.py:146-151`) — POSIX-broken as written. `system_prompt`/`extra_body` silently ignored by CLI/Pi backends; error semantics inconsistent (transport failures raise `FrontierError`, HTTP failures return `ok=False`).
10. **Repo-checkout coupling.** `REPO_ROOT` is derived from the package file location (`swarmflow/config.py:9-11`) and used to reach `prompts/task_brief.md`, `AGENTS.worker.md`, and the default config (`swarmflow/workers.py:352, 376`; `swarmflow/cli.py:34`) — a wheel install breaks prompt building. ARCHITECTURE.md's layout omits `recon.py`; WORKFLOW.md Stage 1's "dependency DAG" has no schema support (`swarmflow/plan.py:16-52` validates only `wave` integers).

---

## Axis 2 — Code Quality & Standards

### Adherence to the repo's own rules — verified compliant

- **CLI non-interactive/scriptable:** `status`, `recon`, `wave-run`, `freeze`, `audit`, `smoke-frontier`, `smoke-worker` all offer `--json` (`swarmflow/cli.py:553-605`); `trace` is always JSON; no command prompts or reads stdin.
- **Guardrails intact, none weakened:** turn cap (`swarmflow/workers.py:294-297`), forbidden-action scan (`swarmflow/workers.py:169-176, 226-235`), frozen-file audit (`swarmflow/audit.py:100-131`), admission control (`swarmflow/workers.py:338-347`). Escape hatches are explicit and loud (`--skip-regression` prints "at your own risk", `swarmflow/cli.py:346-347`).
- **Config portability:** no personal paths in tracked files; `${ENV_VAR}` expansion implemented (`swarmflow/config.py:84-92`); `resolve_executable` used for `pi`/`commandcode`; `build_cli_command` used at `swarmflow/workers.py:255` and `swarmflow/frontier.py:297`.
- **Prompt-template sync:** all 11 placeholders in `prompts/task_brief.md` are supplied by `build_prompt` (`swarmflow/workers.py:392-404`).
- **Clean hygiene:** no TODO/FIXME debt anywhere, no bare `except:`, no dead code, consistent naming.

### Findings against the rules

| Sev | Location | Finding |
|---|---|---|
| medium | `swarmflow/frontier.py:147` | `CommandCodeBackend` bypasses `build_cli_command` — hardcodes `COMSPEC /c` instead of using the shared helper; duplicates .cmd/.js dispatch logic and is unrunnable on POSIX. |
| medium | `swarmflow/audit.py:111` | Unguarded `json.loads` on the frozen baseline — a corrupt/truncated `frozen.json` makes `swarmflow audit` die with a raw traceback instead of a structured exit, violating CONTRIBUTING.md's "exit codes that mean things". Every other JSON read in the codebase is guarded. |
| medium | `swarmflow/workers.py:15`, `swarmflow/discrimination.py:15` | Cross-module private imports (`audit._hash_file`, `audit._git`) plus three divergent `_git` wrappers (`audit.py:134`, `evidence.py:23`, `recon.py:41`) — the divergence is already a latent behavioral inconsistency. |
| medium | `swarmflow/cli.py:108` | `cmd_trace` emits truncated, invalid JSON — `print(json.dumps(scan, indent=2)[:4000])` cuts mid-token; any JSON consumer piping the output gets a parse error. |
| low | `swarmflow/cli.py:608-610, 570-577` | `evidence` and `plan-load` lack `--json` despite being status-like (AGENTS.md:16-17). |

### General code health

- **[medium] Overly long orchestration functions in `cli.py`** — `cmd_wave_run` (~130 lines, `swarmflow/cli.py:244-372`), `_brownfield_preflight` (~60 lines); the 620-line file also houses regression-baseline persistence that belongs in `regression.py`.
- **[low] Duplicated spawn sequence** (`swarmflow/workers.py:407-417` vs `433-449`), **duplicated run.json reader** (`swarmflow/cli.py:201-208` vs `swarmflow/evidence.py:33-40`), **duplicated report→result copy block** (`swarmflow/regression.py:298-310` vs `320-328`).
- **[low] File handles opened without `with`** — `swarmflow/frontier.py:261`, `swarmflow/workers.py:41, 184`.
- **[low] `_spawn` failure leaks a handle and strands the task** — `Popen` OSError at `swarmflow/workers.py:262-265` leaves the task `running` forever; no try/except wrapper in `run_task`/`run_wave`.
- **[low] `run_inline` pollutes the ledger** — `swarmflow/workers.py:471-477` uses task id `"inline"` with no matching row; `_finalize` records `status:delivered` events for a nonexistent task.
- **[low] Error paths skip `ledger.close()`** — `swarmflow/cli.py:67-73, 298-303` catch only specific exceptions; a context-manager `Ledger` would remove the class of bugs.
- **[low] Inconsistent type hints** — frontier backends' `complete()` and `_spawn`/`_finalize` untyped; no ruff/mypy config in `pyproject.toml`.
- **[low] `swarmflow/audit.py:159-161`** — git C-style quoted paths decoded via `latin-1`→`unicode_escape`, producing mojibake for non-ASCII filenames (git emits octal UTF-8 escapes).
- **[low] `swarmflow/discrimination.py:228-229`** — unsupported-family check runs after the worktree is created and suites executed; the skip could be decided before `worktree add`.
- **[low] `swarmflow/plan.py:12-13`** — raw traceback on missing `--plan` file, unlike every other CLI error; `write_specs` uses unsanitized `task["id"]` as a filename.
- **[low] Magic numbers scattered** — 15s poll sleeps, 40-iteration backpressure loop, various truncation limits (500/2000/4000/20000 chars) inline or module-local.

**Overall:** well-above-average for an alpha. The debt clusters in (1) duplicated low-level primitives — the highest-leverage fix is a shared `gitutil`/`procutil` module; (2) a handful of unguarded paths breaking the "exit codes mean things" contract; (3) `cli.py` accretion.

---

## Axis 3 — Tests & Verification

### Summary

16 test files, 147 test functions, no conftest.py, external deps `pytest` + `yaml` only. The network-free mandate (AGENTS.md:14-15) is **fully respected**: no HTTP client imports in tests, subprocess use is exclusively `git` (skipif-guarded) and `sys.executable -c` scripts; production network code is quarantined via injected transport. Loopback-only socket tests are the borderline case and are acceptable.

### Well covered

- **Regression gate — excellent** (`tests/test_regression.py`, 32 tests): summary parsers for six runners, fingerprint normalization, output-truncation ordering, timeout kill-tree including grandchildren, the full `compare()` matrix, fail-closed strict mode.
- **Frozen-file audit — excellent** (`tests/test_audit.py` + `test_audit_brownfield.py`): modified/deleted frozen detection, added-unowned, git-based brownfield semantics, per-wave owner re-scoping.
- **Discrimination — strong** (`tests/test_discrimination.py`, 18 tests): all 6 verdicts, worktree cleanup verification, byte-compared source restoration, enforce-mode wiring through `cli.main`.
- **Scanners** have positive/negative command tables (`tests/test_workers.py:88-120`); `procs`/`sweep` tests cover kill-tree safety, attribution, and pid-reuse via monkeypatching.

### Coverage gaps

| Priority | Gap |
|---|---|
| **Critical** | **CLI `--json` output — zero tests.** Every command's JSON rendering (`swarmflow/cli.py:553-605`) is never asserted, directly conflicting with the "machine consumers matter" mandate. |
| **Critical** | **Worker enforcement layer untested.** `_spawn`/`_finalize`/`_wait_ready`/`run_task`/`run_wave` (`swarmflow/workers.py:249-348, 419-469`) — where the turn-cap kill, timeout kill, `scope_violation` outcome, and `no_changes` downgrade actually fire — have no coverage. The `FakeRunner` in `tests/test_wave_run.py:27-40` bypasses `run_wave` in every integration test. Testable offline with a `sys.executable` fake CLI (the pattern `test_frontier.py` already uses). |
| **Critical** | **Audit gate not wired into any wave-run test** — no test makes a wave violate the freeze and asserts rc 1; the `failures += 1` accounting (`swarmflow/cli.py:310-311`) is untested. |
| moderate | Untested CLI commands: `init`, `smoke-frontier`, `smoke-worker`, `recon`, `trace`, `status`, `freeze`, `audit`. |
| moderate | Ledger breadth: only `queued → delivered` traversed; 10 states defined; `set_status` on a nonexistent task id silently records an orphan event (untested edge). |
| moderate | `read_server_load` vLLM-gauge parsing untested (urlopen inline). |
| moderate | `tests/test_evidence.py` is one ~130-line monolith with a single assertion loop — first failure masks the rest. |

### Hygiene

- `_git_available()` + `_init_repo()` duplicated across 5 test files — an obvious `conftest.py` `git_repo` fixture.
- 8 of 16 files are git-gated and silently skip on gitless CI — consider reporting skip counts.
- Low timing-based flakiness risk (poll loops, post-kill sleeps); the kill-tree survivor scan matches a cmdline substring across the global process table, which could theoretically collide.

---

## Axis 4 — Security & Robustness

`SECURITY.md` correctly states the guardrails "are not a security boundary" — but the frozen-file audit, regression gate, and sweep *are* the product's core value. The most serious findings are cases where a worker (or injected content in a brownfield target repo) can silently defeat those gates, which undercuts the tool's own threat model.

### F1 [medium] — `cmd /c` shim path re-parses unescaped arguments
`config.build_cli_command` returns `[COMSPEC, "/c", executable]` and callers append arguments afterward (`swarmflow/workers.py` `--model/--thinking`; `swarmflow/frontier.py` `--model/--effort`). `list2cmdline` only quotes whitespace; when `cmd.exe` re-parses the line, any argument value containing `&`, `|`, `^`, `%VAR%` is interpreted — e.g. a plan task with `thinking: "off&calc"` reaches `--thinking off&calc` unquoted → arbitrary command execution. This is the *default* path for npm shims on Windows.
**Fix:** don't route arg-carrying invocations through `cmd /c` (launch the underlying `.js` with node directly), or escape cmd metacharacters, or validate `model`/`thinking` against `^[A-Za-z0-9._/-]+$`.

### F2 [high] — `run_regression` executes a worker-writable command string with `shell=True`
`regression.run_regression` does `subprocess.Popen(command, shell=True, ...)`. The command is resolved at wave time via `cli._recon_regression_command` → `recon.load_recon` → `<project>/.swarmflow/recon.json` (used whenever `regression.command` is unset — the real config's case). That file lives **inside the target project** where workers run arbitrary shell commands: a prompt-injected worker can rewrite it, and the control plane executes the payload at the next wave boundary with the operator's full privileges — outside turn caps, trace scanning, and sweep attribution.
**Fix:** persist the resolved regression command outside the project (e.g. `REPO_ROOT/state` keyed by project path) or into `run.json` at preflight **before** any worker dispatches; never re-read `recon.json` after workers run; prefer `shell=False` argv vectors.

### F3 [low] — Plan string fields reach `git` and `mklink` argv unvalidated
`plan.branch_ensure` interpolates `plan.project_name` into a branch name (argv-list git, prefix prevents `-`-leading names — OK); `discrimination._link_dir` passes project paths through `cmd /c mklink /J` — paths containing cmd metacharacters would be re-parsed. Validate ids/names/paths in `validate_plan`; prefer junction creation without `cmd /c`.

### F4 [medium] — No charset/path validation of task ids and `owner_files`
`plan.validate_plan` never checks id format or containment. An id like `..\..\Users\public\evil` writes specs/logs outside the project (`plan.write_specs`, `workers._spawn`); `scaffold` mkdirs every `owner_files` parent; `owner_files` entries are echoed into the worker prompt as "FILES YOU OWN."
**Fix:** require `id` to match `^[A-Za-z0-9._-]+$` (reject `..`) and require `owner_files`/`files_to_read` entries to be relative and resolve strictly inside the project root.

### F5 [high] — Frozen-file audit can be silently defeated
The baseline lives at `<project>/.swarmflow/frozen.json` and `.swarmflow` is excluded from the audit itself. A worker can rewrite `frozen.json` (adding owners/digests → `modified_frozen`/`added_unowned` never fire) or edit `run.json`'s `audit_ignores` (adding `*` neutralizes the brownfield untracked-file check). Design-level bypass, not a local bug.
**Fix:** store gate state outside the writable project (e.g. `REPO_ROOT/state/<hash>/`) or HMAC-sign it; treat `.swarmflow/*` mismatches as violations.

### F6 [low] — Windows case-sensitivity in `_iter_files` ignores
`audit._iter_files` filters directories case-sensitively while `fnmatch` is case-insensitive on Windows — `Node_modules/` is walked and hashed, causing slowdown and baseline churn. Normalize with `os.path.normcase`.

### F7 [low] — No guard against `project_root` being the swarmflow repo itself
Workers could operate inside the repo containing `state/ledger.db` and `config/swarmflow.yaml` (potential `api_key`). Add a preflight check that the resolved project root is not `REPO_ROOT` or an ancestor.

### F8 [low] — Worker-controlled text persists in trust artifacts
Forbidden worker commands, full command lines of attributed processes (including unrelated operator processes), and full trace sessions persist under the target project. Consider redacting obvious `key=...`/`token=...` patterns before persistence.

### F9 [low] — `_expand` applies `os.path.expandvars` to every config string
A literal `$`/`%` in any value is silently mangled. Restrict expansion to designated fields (`api_key`, `base_url`).

### F10 [info] — Transport
Default TLS verification untouched; real config gitignored. Worth documenting that `metrics_url` carries no secrets and that `http://` base_urls would send the API key in cleartext.

### F11 [medium] — Regression fingerprints parsed from worker-controllable stdout
Worker-authored test files can print fake suite summaries to inflate `tests_ran` (defeating the suite-shrink check) or fake failure lines. Prefer structured reporter output written to a control-plane-chosen file over parsing shared stdout.

### F12 [medium] — Discrimination check executes worker-authored test files at parent state
Worker code executes even for gated waves and can poison the real `node_modules` (gitignored, invisible to the audit). Document explicitly; consider enforce mode + refusing indeterminate when the wave owns test files, and pinning a pristine `node_modules` copy.

### F13 [medium] — Untrusted narratives flow into the evidence bundle → future verifier
`evidence.bundle` embeds worker `last_text` verbatim. Harmless today, but a direct prompt-injection channel the moment a verifier backend reads the bundle. Keep "deterministic gates decide, LLM only advises" and delimit untrusted blocks.

### F14 — Prompt-injection → gate bypass chain (synthesis)
A brownfield target repo is untrusted content: a malicious `AGENTS.md`/spec can steer a worker toward F2/F5. Specs frozen at plan time, deterministic outcome classification, and worktree isolation hold up well — the holes are exactly the worker-writable control files (F2, F5); closing those breaks the chain.

### F15 [medium] — Sweep attribution is loose substring matching, and kill mode trusts it
`sweep._attributable` matches the project root as a cmdline *substring* — `...\swarmflow` matches `...\swarmflow-tests`, and an IDE opened on the project is attributed. With `sweep.mode: kill` those processes get terminated.
**Fix:** require the root followed by a path separator or end (or prefer cwd match); consider requiring `kill_requires_port` AND a `server_names` match.

### F16 [medium] — PID-reuse race between snapshot and kill
`run_sweep` kills pids from the `after` snapshot without re-verifying `create_time` immediately before `kill_tree`; on Windows pid reuse is aggressive and `taskkill /PID <pid> /T /F` can kill the wrong tree. Re-verify identity before killing.

### F17 [low] — `kill_tree` fallback gaps
TOCTOU lets freshly spawned grandchildren escape; an exited direct child (common) returns False and leaks the subtree; unbounded `proc.wait()` if all kill mechanisms fail. Consider bounded wait + group-first kill + survivor re-scan.

### F18 [medium-low] — Tasks stranded in `running` on spawn failure
`set_status("running")` then `_spawn`/`_send_prompt` with no try/finally; a missing executable or `BrokenPipeError` leaves the task `running` forever. Wrap in try/except that records a terminal status.

### F19 [medium] — Corrupt `frozen.json` crashes the post-wave audit instead of failing closed
`audit.py` uncaught `json.loads` + non-atomic `write_text` baseline; a crashed freeze or worker truncation makes `cmd_wave_run` raise after the wave, skipping remaining gates and leaking the ledger connection. Wrap like `_run_sweep`/`_run_discrimination` already do; write baselines atomically (temp + `os.replace`).

### F20 [low] — SQLite robustness
No `busy_timeout`/WAL — concurrent `wave-run` and `status` yields `database is locked` mid-wave. Add `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000`. (SQL itself is fully parameterized; dynamic columns whitelisted — no injection.)

### Done well

No `shell=True` in hot paths; secrets hygiene (env expansion → Authorization header only, never logged, real config gitignored); parameterized SQL with column whitelists; freeze ignore-semantics persisted inside the baseline; sweep discipline (pre-existing processes never killed, self/ancestor exclusion, opt-in `--kill-stale`); Windows pid-reuse awareness in `workers._finalize`; bounded deterministic recon; `yaml.safe_load` everywhere; evidence bundle labels worker narratives untrusted.

---

## Axis 5 — Docs, Config & Prompts Consistency

### Prompt templates vs code (declared part of the behavior surface)

- **`prompts/verifier.md` has no parser and no caller** — `needs_fix` exists only in `STATUSES`; WORKFLOW Stage 4 steps 4–5 unimplemented; CASE_STUDY admits the verifier pass wasn't run.
- **`prompts/acceptance.md` has no parser and no caller** — `accepted` never set; WORKFLOW Stage 7 mapping has no code.
- **`prompts/planner.md` — no caller, plus schema drift:** emits `deps: []` (dead key, never read, undocumented in PLAN_SCHEMA.md); output schema has **no `project`/`mode` key** but `plan-load` requires `project` and only `mode: brownfield` triggers the git preflight — a planner-produced brownfield plan would load as greenfield.
- **`prompts/task_brief.md` — fully consistent** (11/11 placeholders, covered by tests).

### Docs vs `swarmflow/cli.py`

- **WORKFLOW.md Stage 6 (PR gate) is stale:** claims `gh` PR automation, `awaiting_pr_review`, and `wave run --continue` — zero PR code exists, the command is `wave-run`, and there is no `--continue` flag; README contradicts it ("PR automation intentionally out of scope").
- Stage 7 (Package) and Stage 4.3 ("traceability matrix parsed from worker reports") have no code.
- **WORKFLOW's evidence-bundle tree is stale** (`docs/WORKFLOW.md:93-103`): lists `requirements.md`, `reports/`, `traces/`, etc. — the code writes a single `.swarmflow/evidence/bundle.md` plus per-wave JSON evidence files.
- ARCHITECTURE.md names `FrontierClient` — no such class (`CommandCodeBackend` is real); layout omits `recon.py`; verdict naming drift between CASE_STUDY (`pre-existing`) and code (`preexisting_at_parent`), and `not_observed` is omitted.
- **Undocumented CLI surface:** `status`, `trace`, `wave-run --concurrency/--strict`, `recon --regression-command`, `freeze --mode`.

### Config consistency — strongest area

- `config/swarmflow.example.yaml` ↔ `config.DEFAULTS` ↔ consumers is one-to-one; no read-but-undocumented keys.
- Dead key: `worker.thinking` (`swarmflow.example.yaml:49`) — never read by code; effective thinking comes from per-task plan fields and `worker.retry_thinking`.
- `frontier.thinking` is pi-backend-only and `frontier.effort` is commandcode-only — not marked as such in the example.
- Undocumented env var: `SWARMFLOW_CONFIG` (`swarmflow/config.py:105-113`).
- README dependency claim inaccurate: `psutil>=5.9` is a hard dependency (`pyproject.toml:26`), not just "stdlib + pyyaml".
- AGENTS.md gotcha about scaffold installs using `--include=dev` — no install code exists in the tool (that behavior lived in the pilot).

### What checks out

README/llms.txt quickstart commands and flags all exist; the example smoke plan validates; PLAN_SCHEMA.md's validation rules match `plan.py` exactly; the brownfield pipeline docs match `cli.py`/`audit.py`/`recon.py` step-for-step.

---

## Top fixes by leverage

1. **Move gate state out of the worker-writable project** (recon-resolved regression command, `frozen.json`, `run.json`) or sign it — closes the F2/F5 privilege-escalation and audit-bypass chain. *(Security)*
2. **Wire the frontier gates in or mark planner/verifier/acceptance as operator-run** — the upper state machine and three of four prompts are currently dead surface. *(Architecture/docs)*
3. **Fix `cmd /c` arg handling** (use `build_cli_command` + validate `model`/`thinking`) **and verify PID identity before sweep kills**. *(Security F1/F16)*
4. **Extract a `pipeline` module from `cli.py`** and a shared git/proc util module — kills the `_git` ×3 and run.json-reader duplication, and unblocks sequential-finalization/admission-control fixes. *(Architecture/quality)*
5. **Add tests for `--json` output and the real worker enforcement path** (the `sys.executable` fake-CLI pattern from `test_frontier.py` applies), plus the audit-gate wave test. *(Tests)*

---

*Review produced by five parallel review agents (architecture, code quality, tests, security, docs/config) against commit `78da8bd`, with the test suite executed to verify claims.*
