# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
