# PILOT - first real run of swarmflow

Goal: take one small real PRD (greenfield, ~5-8 modules) through the full pipeline with
one human PR checkpoint, and measure. Success = deployable artifact + acceptance matrix,
not just code that exists.

## Pilot shape
1. Pick a PRD that is genuinely wanted (not a toy). Recommended envelope:
   - 5-8 modules / work packages, stdlib-only or one dependency
   - each package independently testable; shared surface small (one CLI entry point)
2. Run `swarmflow plan load` with a hand-written `plan.yaml` first. Do NOT automate
   planning yet: one manual frontier planning call (planner.md prompt) produces the plan;
   hand-check the ownership map and contracts before scaffolding. This validates the
   planner prompt against reality cheaply.
3. Scaffold + wave 1 (concurrency 8). Watch for:
   - spiral failures (expect ~1 in 10; verify the auto-retry picks thinking medium)
   - report compliance (all 5 sections) and traceability matrix quality
4. Verify + integrate, then open the PR. The human review at this point IS the experiment:
   note what a reviewer catches that the pipeline did not.
5. Package + acceptance. Record wall time, token totals, retry count, frontier spend,
   and every place the pipeline needed manual intervention.

## Manual-first checklist (what is NOT automated yet)
- [ ] frontier planning call (run planner.md by hand, save JSON)
- [ ] verifier pass (run verifier.md by hand over the wave)
- [ ] PR creation (gh pr create) - commands documented, not wired
- [ ] packaging step (project-specific)

## Measured baseline to compare against (from the 10-session stress test)
- 10 workers built 10 pinned modules + 263 tests in ~16 min wall, 0 conflicts,
  1 spiral failure (recovered), 1 uncovered edge case found by independent probing
- per-worker: 13-28 turns, 6-27K output tokens, wall ~15-16 min under full load

## Pilot run 1 - TaskDock (2026-09-19) - OUTCOMES

Result: MVP-1 delivered and independently verified; PR-ready branch `feat/taskdock-mvp1`
in `C:\Users\redde\taskdock-pilot` with `PR_BODY.md` and `evidence/`.

Worked: frontier planner produced a clean disjoint 5-task plan with correct waves and
thinking tiers; scaffold/ledger/dispatch mechanics; artifact-based auditing; final
acceptance = tsc clean + 48 unit + 11 e2e + external HTTP probe 13/13 + verifier pass
(deepseek found 1 major + 3 minor findings on top of our checks).

Incidents (all found by this run; fixes applied):
1. Process-level `NODE_ENV=production` made npm omit devDependencies, so jest/ts-jest were
   missing and workers thrashed trying to "fix" the environment.
   Fix: install with `--include=dev`; project `.npmrc` with `include=dev`.
2. Worker thrash: 90-100 turn loops probing node_modules, pnpm confusion, 48-56 jest runs.
   Fixes: hardened worker brief (npm-only, one jest run per cycle, never read node_modules,
   BLOCKED protocol after 3 attempts, <=40 tool calls); supervisor turn cap (45).
3. A worker deleted node_modules and modified frozen files under thrash (orchestrator
   restored; npm ci --include=dev rebuilt).
   Fixes: forbidden-action scanner (npm install/pnpm/rm -rf node_modules -> reject),
   node_modules preflight before dispatch. Frozen-file integrity gate still MISSING.
4. Frontier client crashed on non-ASCII prompts (cp1252 encode). Fixed: explicit UTF-8.

Metrics: wave 1 = 2m19s (post-fix), wave 2 = 1m42s; T1-T4 attempts=4 each (3 lost to the
environment/thrash era), T5 attempts=1; verifier call 25.8K in / 7.6K out tokens.

Roadmap from this run:
- frozen-file integrity gate (baseline hashes verified after every wave)
- file-level scope audit (diff actual changes vs owner map)
- inject the exact test command per task (no discovery by workers)
- wire PR creation (gh) once a remote is configured
