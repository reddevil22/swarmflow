# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
