# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
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
