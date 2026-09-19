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
