# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Provider layer**: `providers.<name>` defines a frontier block and a worker model once;
  `role_providers.frontier|worker` selects which one serves each role, overriding the role
  defaults. Swapping models (hosted <-> local, frontier and workers independently) is one
  config line, keys expand from the environment, and unknown/incomplete selections are
  rejected at load time. Documented in the README and the example config.
- **CI** (`.github/workflows/ci.yml`): the suite runs on Linux (3.11, 3.13) and Windows
  (3.13), and a packaging job builds the wheel, installs it into a clean environment and
  smokes `swarmflow init` + `status --json` outside the source tree.
- **`docs/EXECUTIVE_SUMMARY.md`** (linked from the README, with the independent review).
- **Plan-shape rule: same-wave tasks must be independent.** `validate_plan` now rejects two
  tasks in one wave that share a `test_command` (the recurring "implement X" + "write X's
  tests" split, which races: the second task's tests fail until the first lands). The
  planner prompt states the rule and asks for unique, scoped commands per wave; the retry
  feedback carries the message, and the acceptance-criteria guidance now requires one
  checkable entry per exact PRD requirement (message wording, ordering, precedence).

### Changed
- **The package is installable**: shipped assets (planner/verifier/acceptance prompts,
  `AGENTS.worker.md`, the example config) moved into `swarmflow/assets/` and are resolved
  through `swarmflow.resources.asset` (`importlib.resources`); control state, the ledger
  and the config default to per-user paths (`SWARMFLOW_STATE_DIR`, `%LOCALAPPDATA%`,
  `$XDG_STATE_HOME`, `~/Library/Application Support`). `pyproject` gains package-data, a
  dynamic version, an SPDX license and repository URLs.
- **Structure**: `cli.py` (1010 lines) is now `cli.py` (argparse + exit codes) plus
  `pipeline.py` (preflight, freeze, dispatch, gates, verify, report); `workers.py` is now
  `prompt.py` (brief rendering + fix context), `trace.py` (trace analysis) and
  `workers.py` (supervision only); one `gitutil.git` replaces three drifted copies, and
  `runstate.persist_run` / `evidence.latest_artifact` / `procs.format_ports` collapse the
  remaining duplication.
- **Docs truth-up**: architecture layout and config key names, workflow claims (one
  regression gate, no demo transcript), plan-schema artifact locations, and the shipped
  acceptance prompt now describe what the code does; the independent review carries a
  status banner listing what has since been fixed.
- Tracked files are free of personal paths and the LAN address, and `.gitignore` covers
  the tool's own artifacts (`.swarmflow/`, `*.db`, `.env*`, editor dirs).

### Fixed
- **HTTP requests identify the client**: urllib's default `User-Agent` is blocked by WAFs
  in front of some OpenAI-compatible gateways (the Command Code provider edge answers
  `403 code 1010`), so the transport now sends `swarmflow/<version>`.
- **Dead ledger surface**: the never-used `report_path` column and the phantom `inline`
  task row that `run_inline` recorded are gone (the smoke path writes no ledger state,
  pinned by a test).
- **Fix cycles can close**: `swarmflow retry --task <id>` re-queues a `needs_fix`/`failed`
  task (refused after 3 attempts) and the next dispatch injects the verifier's findings
  into the brief as a fenced `FIX CONTEXT` block (attempt >= 2 only; the findings quote
  worker-authored evidence, so they enter as data). Without findings the previous outcome
  is stated instead (`no_changes` now tells the worker its delivery is hashed). `verify`
  additionally accepts a `failed` task whose outcome is `no_changes`, so a correct
  implementation with weak assertions can be judged instead of re-run. Found by the
  greenfield stability run, where a `needs_fix` task was re-dispatched with the same spec
  and correctly answered "no changes needed" - which the harness could only record as
  `failed`.
- **Verifier test-count false positives**: `prompts/verifier.md` now states that
  `tests_ran`/`failures` are scoped to the whole command in
  `regression.baseline.command` and that parametrized tests expand into cases, and the
  gate summary carries the same scope as `_counts_scope` data (a suite-wide 36 vs a
  file's 14 collected cases was reported as a critical discrepancy).
- **Acceptance sees the evidence, not a truncated report**: the bundle now leads with the
  objective sections (`## Per-task verification` - verdicts + finding summaries - then
  audit, regression, discrimination, sweep, diff) and renders worker reports last, bounded
  head+tail with an explicit omission marker (`clip`), so a cut narrative can no longer
  hide a task's results; the acceptance bundle section gets a 30000-char budget instead of
  6000, and its prompt no longer promises a demo transcript. `scan_trace` keeps 12000
  chars of the final report (was 4000, which already deleted every report head).
- **Pre-existing untracked files no longer fail the audit**: the brownfield preflight
  snapshots the run's untracked files (file-level, `git status -uall`, capped) into the
  run state and the audit exempts them by exact path - only files created after the
  preflight violate. The file-level listing also fixes untracked directories being
  attributed as a single `dir/` entry (which mis-attributed owned files inside them).
  Found by the sdlc-cli live run, where an operator's untracked note failed the wave and
  the verifier relayed it as critical.
- **The discrimination check isolates Python source trees**: the worktree's source roots
  (config `discrimination.python_paths`, default `["src"]`) are prepended to `PYTHONPATH`
  for parent-state runs, and a `find_spec` probe verifies every project package imports
  from inside the worktree. An editable install that bypasses sys.path (front-inserted
  `meta_path` finder) now yields `indeterminate` - printed as inconclusive under `warn`,
  fail-closed under `enforce` - instead of a false `passes_at_parent`. `run_regression`
  gained an `env` override for this; the probe outcome and the exempted untracked count
  are recorded in the discrimination JSON and the evidence bundle.
- **node:test (`tsx --test` / `node --test`) is a first-class runner**: TAP and spec
  reporter summaries (`# tests/# pass/# fail`, `ℹ tests/ℹ pass/ℹ fail`) give the
  regression gate an inventory and failure count, `not ok N - name` / `✖ name` lines
  become fingerprints (directives stripped, nested subtests included), and cancelled
  tests count as skipped. Found by the prompt-scaler end-to-end run, which had a fully
  blind regression gate for this stack.
- **Discrimination verdicts cannot report a false `passes_at_parent` anymore**: file
  observability is computed per run (a red run only exonerates a file when *its*
  failures name files), path-less failures are attributed to a single copied test file
  by unique quoted-literal name match, and anything unattributable is `not_observed` with
  a result-level `unattributable` flag - which fails the wave closed under
  `mode: enforce` and is printed as inconclusive under `warn`. This was the live run's
  worst finding: a genuinely discriminating test was labeled non-discriminating.
- The verifier now receives the owned files' **contents** (bounded, untrusted) and the
  **pinned PRD**: `swarmflow plan` persists the exact PRD text to
  `<project>/.swarmflow/PRD.md` and records its sha256, so verification can judge
  spec-vs-delivery *and* delivery-vs-request (a mismatched or operator-supplied PRD is
  labelled accordingly). Previously the verifier only saw a file list and the worker's
  narrative, and planner-level spec defects were invisible by construction.

### Added
- **Wave-created files are sealed into the frozen baseline** (`audit.seal`): an owned,
  untracked, non-ignored file a wave creates (and that the preflight did not already
  exempt) is added to the baseline with its post-wave hash, capped at
  `MAX_SEAL_ADDITIONS` (200) per wave with the overflow reported as `capped` and printed.
  Ownership is wave-scoped, so sealed paths leave the baseline's owner map immediately -
  the next wave neither flags the file as `added_unowned` nor silently trusts edits to it
  (`modified_frozen` / `deleted_frozen`). The manual `swarmflow freeze` re-baseline keeps
  previously sealed entries whose files still exist without absorbing strays, and never
  unions a greenfield baseline. A `seal` ledger event records the counts.
- **Frontier roles wired** (`swarmflow/roles.py` + three commands):
  - `swarmflow plan --prd F --project P [--mode brownfield] [--out] [--load]` runs the
    planner, validates the JSON (one retry with the validation errors as feedback),
    injects `project`/`mode`, and writes `<project>/.swarmflow/plan.yaml`.
  - `swarmflow verify --task T` (and the opt-in `wave-run --verify` stage, config
    `verify.enabled` / `verify.max_tasks`, off by default) runs the verifier against the
    spec, the worker report and the latest gate artifacts; `pass` -> `verified`,
    `fail|needs_fix` -> `needs_fix` (the worker's `verdict` column is never touched).
  - `swarmflow accept --project P` runs acceptance over the frozen criteria and the
    evidence bundle; `accepted` moves that project's `verified` tasks to `accepted`.
  - every call writes its raw output + parsed result to
    `.swarmflow/evidence/{plan,verify_<id>,accept}.json`; shared exit codes `0`/`1`/`2`
    (positive / model-negative-or-unusable / infrastructure); worker- and tool-produced
    blocks are fenced as `<untrusted>` with the anti-injection note outside the fence.
- **Sticky frozen baseline** (`audit.freeze(carry_over=True)` + `audit.seal`): the
  per-wave freeze no longer re-hashes files the wave does not own - it carries the
  previous baseline's hashes (including entries for deleted files), so a `modified_frozen`
  or `deleted_frozen` finding stays red until an explicit re-baseline. The wave that owns
  a file seals its post-wave content right after the audit, which is the only moment an
  owned change becomes the new baseline. `swarmflow freeze` (mode inferred from the run,
  counts printed, rebalance note) is the explicit re-baseline; a missing baseline after a
  run froze one refuses the wave instead of silently re-baking. Wave reports and the
  ledger carry the carried/refreshed counts.
- **Control-plane state store** (`swarmflow/runstate.py`): gate state (run record,
  frozen baseline, the resolved regression command) lives outside the worker-writable
  project at `<state_dir>/runs/<hash-of-project>/` (default `<repo>/state/`). The
  regression command is frozen at plan-load and never re-derived from
  `.swarmflow/recon.json`; a config `regression.command` change still wins and is
  printed. Pre-upgrade runs are adopted once (structural fields only - never
  `audit_ignores`/`regression`); the project-side `run.json` is a marked, unread mirror.
- **Supervision invariant**: one supervisor loop polls every live worker handle with its
  own deadline (a slow worker no longer leaves its siblings unpoliced), kills are
  bounded (`kill_grace_s`, then `proc.kill()`, then an honest `kill_failed` outcome
  instead of a hang), interrupted waves tear down live sessions, and spawn/send
  failures either abort the wave with a `spawn-failed` event (configuration errors) or
  fail just that task (`spawn_failed`) while the wave continues.
- **Input validation**: plan ids (`^[A-Za-z0-9._-]{1,64}$`, no `..`), project names,
  `owner_files`/`files_to_read` (relative, contained), `thinking` (Pi's real levels),
  `acceptance` shapes; config values that reach the CLI are charset-checked and
  `.cmd`/`.bat` executables routed through `cmd.exe` may not contain spaces or shell
  metacharacters; junction creation refuses paths `cmd.exe` would re-parse.
- `Ledger` is a context manager; `audit` failures (corrupt baseline, missing baseline on
  a brownfield run, unexpected exceptions) fail the wave structurally instead of
  escaping it, and `swarmflow evidence` honours `audit.ignore_extra` like the wave gate.
- `--json` output added to `plan-load` and `evidence`; `smoke-frontier`/`smoke-worker`
  emit a JSON error object on failure; `trace` output is always valid JSON (no more
  mid-token truncation). CLI tests now assert every `--json` document parses.
- Docs/config/prompt truth-up: WORKFLOW's Stage 6/7 no longer claim PR/package
  automation (`gh`, `awaiting_pr_review`, `wave run --continue` never existed); the
  evidence section describes what is actually written; planner/verifier/acceptance are
  marked operator-run; the planner schema carries `project`/`mode` and drops the dead
  `deps` key; the dead `worker.thinking` config key is gone and `frontier.thinking` /
  `frontier.effort` are labelled per backend; README lists the runtime dependencies
  (pyyaml + psutil) and the wave flags.
- **Brownfield support** (existing repositories, regression-safe):
  - `swarmflow recon` surveys a repo deterministically (stacks, evidence-backed test/
    build/lint commands, git state with tracked-only dirtiness, test inventory).
  - `mode: brownfield` plans: minimal diffs, integration task for shared surfaces,
    repo's own test runner; `files_to_read` per task.
  - Git preflight: clean-tree refusal (`--allow-dirty`), run branch create/reuse,
    `.swarmflow/` artifact namespace with `.gitignore` management, first-write-wins
    `run.json` (mode/branch/base_sha).
  - Regression gate: project suite baselined once per run and re-run after every wave
    (timeout-bounded, per-stack failure parsing, baseline-red aware); new failures
    fail the wave before the audit runs.
  - Git-based scope audit for brownfield (tracked hashes unless owned,
    untracked-unowned detection) with per-wave ownership scoping.
  - Truthful deliveries: `no_changes` when a task's owned files are untouched.
  - Stack-aware worker briefs (node/python/go/rust/generic) with inline working rules
    for brownfield; broader forbidden-action scan (pip/poetry/uv/go get/cargo add).
  - `swarmflow evidence` bundle (recon, ledger, reports from traces, audit,
    regression, bounded diff of tests + manifests) for verifier/acceptance passes.
- **Process sweep + tree-safe termination**:
  - workers are spawned in their own session (POSIX) and every kill path terminates the
    whole tree (`killpg` / `taskkill /T`); a worker that exits cleanly still gets its
    group cleaned, a Ctrl-C tears down live sessions, and a hung regression suite is
    tree-killed instead of leaving the runner alive.
  - after each wave (and before the regression gate) process snapshots taken before/after
    are diffed: new processes attributable to the project by command line or working
    directory are reported with their listening ports in the ledger, a per-wave JSON and
    a new evidence section. `sweep.mode: kill` terminates them (port-holding or
    allowlisted server processes only); pre-existing listeners are reported, never
    killed. Plan-load warns about stale project listeners and `--kill-stale` removes
    them.
  - worker traces are scanned for server/watcher launches (evidence in the task verdict,
    not a failure) and the worker rules forbid starting them.
- **Discrimination check** (brownfield, per wave): the wave's owned test files are
  re-run at the run's `base_sha` in a throwaway git worktree - parent sources plus the
  new tests, dependencies linked in. Each file gets a verdict (`fails_at_parent`,
  `passes_at_parent`, `error_at_parent` for new modules the parent cannot import,
  `preexisting_at_parent` against a red baseline, `deleted_in_wave`, `not_observed`),
  recorded in the ledger, `evidence/wave<N>.discrimination.json` (raw run output
  alongside) and a new `## Discrimination` evidence section. Default mode `warn`
  (evidence only); `discrimination.mode: enforce` fails the wave on non-discriminating
  or deleted tests. The live tree is never modified: linked directories are removed
  before the worktree is deleted.
- Parser: `parse_report` now separates run-level errors (pytest collection `ERROR`,
  jest `Test suite failed to run`, compiler errors) from failure fingerprints.
- Brownfield audit honors explicit ignore patterns, and the preflight records the
  `.gitignore` it creates so a first run in a repo without one no longer fails the
  scope audit on `added_unowned: .gitignore`.

### Changed
- Regression gate compares failure **identity**, not counts: failing-test fingerprints
  (pytest/jest/vitest/go/cargo summary lines plus embedded TS/mypy/eslint errors) and an
  executed-test inventory. A red -> red run where a different test broke now fails the
  gate; a suite that shrinks while staying green (`it.skip`, `#[ignore]`, deleted
  specs) fails as `suite shrank`; uncomparable results fail closed by default
  (`regression.strict`). Per-wave comparisons land in `evidence/wave<N>.compare.json`,
  the `run.json` baseline no longer stores the raw output blob, and `wave-run` gains
  `--rebaseline` and `--strict`; `evidence` reports known-failing tests and the latest
  comparison.
- Frontier access is backend-agnostic: `frontier.backend` selects `openai` (any
  OpenAI-compatible `/chat/completions` API via a stdlib-only client), `commandcode`,
  a generic `cli` adapter for any terminal agent CLI (argv template + text/JSON
  extraction), or `pi`; `auto` resolves from what is configured. The Command Code CLI
  is no longer required. All backends return one normalized result shape, and
  `smoke-frontier` reports which backend is active.

## [0.1.0] - 2026-09-19

### Added
- Deterministic control plane for local agent swarms: SQLite ledger as the single
  source of truth, task state machine, queue and wave dispatch with admission control.
- Frontier client (deepseek-v4-flash via Command Code headless mode) with NDJSON
  result parsing, JSON-mode helper and UTF-8 safe transport.
- Worker runner for Pi sessions: artifact-based auditing, reasoning-spiral detection
  with auto-retry at lower thinking, turn cap, forbidden-action scanner
  (npm install / pnpm / node_modules deletion) and node_modules preflight.
- Frozen-file integrity and scope audit: `freeze` snapshots sha256 of all non-owned
  files; `audit` detects modified_frozen / deleted_frozen / added_unowned violations;
  `wave-run` audits automatically after every wave.
- Plan pipeline: planner/task-brief/verifier/acceptance prompt templates, plan
  validation (disjoint ownership, exact `test_command` per task), scaffolding with
  worker rules and per-task specs.
- CLI: `init`, `smoke-frontier`, `smoke-worker`, `trace`, `plan-load`, `freeze`,
  `audit`, `wave-run`, `status` - all non-interactive, with `--json` where relevant.
- Test suite (unit, no network): ledger, trace analysis, forbidden-action scanning,
  frontier parsing, plan validation, audit gate.
