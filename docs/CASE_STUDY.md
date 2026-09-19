# Case study: TaskDock MVP-1 (first full validation run)

This is the first end-to-end run of the complete pipeline: a real PRD, produced and
verified by swarmflow on a local machine, without human code edits.

## Input
The PRD (`pilot/PRD_taskdock.md`): a small task-tracking HTTP API built with NestJS
in hexagonal architecture - framework-free domain, use-case layer over a repository
port, in-memory and HTTP adapters, frozen HTTP contract (201/200/400/404/409), and a
strict testing bar.

## Pipeline as executed
1. **Plan (frontier)**: deepseek-v4-flash produced a 5-task plan with disjoint file
   ownership, two waves (4 parallel + 1 wiring), per-task `test_command`, and 8
   acceptance criteria including a layering check and an ownership audit.
2. **Scaffold + freeze**: project git repo, worker rules (`AGENTS.md`), frozen
   contracts (port file, tooling configs), dependency install, sha256 baseline.
3. **Wave 1** (4 concurrent workers): domain, use cases, persistence adapter, HTTP
   controller - each with its own specs and tests.
4. **Wave 2** (1 worker): composition root, bootstrap, end-to-end smoke test.
5. **Verify**: typecheck, unit suite, e2e suite, an external HTTP probe that boots the
   real server, layering greps, then a frontier verifier pass over an evidence digest.
6. **Gate**: acceptance mapping (AC-1..8) and a ready-to-review branch with PR body.

## Results
- 5 tasks, ~30 files, **48 unit tests + 11 e2e tests**, all passing.
- External HTTP probe: **13/13** contract checks against the running server.
- Layering verified mechanically: no framework imports in the domain layer, no
  infrastructure imports in the application layer.
- Frontier verifier verdict: **pass**, with 1 major and 3 minor findings on top of the
  orchestrator's own checks (see below).
- Post-fix wave timings: wave 1 in ~2m19s, wave 2 in ~1m42s.

## Incidents and the guardrails they produced
| Incident | Root cause | Fix shipped |
|---|---|---|
| Workers could not run tests; 90-100 turn thrash loops, 48-56 test invocations each | `NODE_ENV=production` in the environment made npm omit devDependencies; workers tried to "repair" the environment | installs use `--include=dev`; worker brief forbids installs; turn cap (45); exact `test_command` injection |
| A worker deleted `node_modules` and modified frozen files during the thrash | panic behavior under a broken environment; no enforcement of the ownership envelope | forbidden-action scanner (rejects npm install / pnpm / node_modules deletion); frozen-file integrity + scope audit after every wave |
| Frontier client crashed on prompts containing non-ASCII text | cp1252 stdin encoding | explicit UTF-8 everywhere |
| Verifier flagged non-discriminating tests and missing e2e assertions | model-produced tests can be green without pinning behavior | discrimination check: the wave's owned test files re-run at the run's base commit in a throwaway worktree, verdicts as evidence (`enforce` fails the wave); verification prompts also require discrimination evidence |

## Open items at the time of writing
- No automated gate against a worker editing files it does not own (the audit catches
  it after the fact and fails the wave; prevention would need tool-level enforcement).
- Some generated controller tests are non-discriminating. Mechanized as the
  discrimination check (`swarmflow/discrimination.py`): each wave's owned test files are
  re-run at the run's `base_sha` in a throwaway git worktree and every file gets a
  verdict (`fails_at_parent`, `passes_at_parent`, `error_at_parent`, `pre-existing`,
  `deleted_in_wave`) recorded in the ledger and the evidence bundle; `enforce` mode
  fails the wave. The verifier still judges intent (a `passes_at_parent` regression
  guard for earlier-wave code is not automatically a defect).

## Brownfield validation run (2026-09-19)

Second full validation, on an existing repository: `taskdock-pilot` (the MVP-1 above)
received a brownfield delta on branch `swarmflow/taskdock-mvp2`, base `1b2748a65c`.

Pipeline as executed: `recon` (digest embedded in the bundle) -> `mode: brownfield`
plan (3 tasks, disjoint ownership) -> preflight (clean-tree check, `.swarmflow/`
gitignored, run branch created, run.json with base_sha) -> wave 1 (done-filter +
controller 400-test rewrite) -> wave 2 (bootstrap e2e) -> verifier round 1
(`needs_fix`) -> wave 3 (filter HTTP tests + test hardening) -> verifier round 2
(`needs_fix` on the error contract) -> wave 4 (invalid `done` -> TaskValidationError +
assertion tightening) -> verifier round 3 (**pass**).

Wall times: wave 1 5m07s, wave 2 55s, wave 3 2m33s, wave 4 1m04s; all six brownfield
tasks delivered on attempt 1. Final state: 55 unit + 20 e2e tests green, external HTTP
probe 7/7 (filter both directions, unchanged no-param order, invalid value -> 400
`{error: "TaskValidationError"}`), git-based scope audit clean after every wave, and the
unit+e2e regression gate green at every boundary (baseline recorded once per run).

What the run caught (pipeline fixes applied):
1. **Recon override loss**: plan-load's re-recon silently replaced a CLI-provided
   regression command with the detected one, degrading the gate to unit-only. Fixed:
   preflight preserves explicit overrides.
2. **Orphaned servers**: workers left a `ts-node src/main.ts` listener on port 3000
   twice; external probes then measured stale code and reported false failures.
   Cleanup was manual. **Delivered:** tree-safe termination (workers run in their own
   session; every kill path tears down the tree, including after a clean exit) plus a
   post-wave process sweep that diffs before/after snapshots, attributes processes to
   the project by command line or working directory, reports ports in the ledger and
   evidence, and can terminate them (`sweep.mode: kill`); a server-launch scanner in
   worker traces plus a worker rule against starting servers/watchers.
3. **Verifier narrative confusion**: worker reports describing intermediate failures
   caused two `needs_fix` rounds partly about bookkeeping. Fixed in the bundle: a
   narrative disclaimer plus per-task spec embedding so spec-vs-delivery checks work.

Roadmap from this run: unit-level `@Query('done')` test; generated e2e tests should
avoid presence-only assertions; assert error message text where the contract includes
it; post-wave orphan-process sweep (delivered: tree-safe termination plus the
snapshot-diff sweep).

## Brownfield validation run 2 (2026-09-19, `xstate-orchestration-demo`)

Third full validation, and the first on a repository nobody prepared for the pipeline:
a real React 19 + XState v5 demo with Vitest (machine + component layers) and a
Playwright e2e suite, clean working tree, and its own feature backlog in
`docs/FEATURES.md`. Branch `swarmflow/xstate-orch`, base `c6264f3`, run commits
`880a52d` -> `d05827a` -> `f38212b`.

Delta chosen from the project's own backlog: (a) repair the red component-test layer
(9/9 failing with `TypeError: React.act is not a function`), (b) implement the deferred
"multi-tab history sync" feature (BroadcastChannel), (c) prove it at the browser layer.

### Pipeline as executed
| Wave | Tasks | Attempts | Turns | Outcome |
|---|---|---|---|---|
| 1 (killed) | act-repair, history-sync | - | 2 / 5 | act-repair stalled on a mis-briefed 400 KB `node_modules` read; wave killed, spec reworked |
| 1 (retry) | act-repair, history-sync | 2 / 3 | 16 / 48 | act-repair delivered; history-sync hit the turn cap (spiral: 8 `debug*.test.ts` scratch files, cross-file type breakage) |
| 2 | history-sync-e2e, sync-typecheck-fix | 1 / 1 | 42 / 16 | typecheck restored project-wide; e2e delivered but hollow (see below) |
| 3 | sync-live-fix | 2 (spiral -> auto-retry) | 33K-token single turn -> 42 | delivered, again hollow; orchestrator repaired |

### Final state
`npm run typecheck` clean; **34 unit tests** (26 original + 8 sync) and **13 e2e tests**
(10 original + 3 sync) green; evidence bundle 46 KB; four commits on the run branch;
base preserved; no leaked listeners at exit. The act fix is scoped to the vitest
process only, so the production build path is unchanged.

### What this run caught
1. **Environment gremlin #3 (same family as taskdock)**: the machine exports
   `NODE_ENV=production`, which makes react resolve under production export conditions
   where `act` is absent - and the RTL component layer dies. Recon now records the
   ambient `NODE_ENV` and the digest warns about it. The pipeline's confidence came
   from a reproduced lever (`NODE_ENV=development npm test` -> 26/26), not from a guess.
2. **The over-broad fix lesson**: forcing development mode unconditionally in
   `vite.config.ts` also changed `vite build` (the e2e bundle), silently breaking an
   untouched URL-sync test. Caught only by full-suite verification; fixed by scoping the
   override to the vitest process. Acceptance criteria for config edits must include
   every other consumer of that config.
3. **Tests written to accommodate broken code - three separate times**:
   (a) the unit fake for the sync channel delivered messages to *itself*, matching the
   broken wrapper and masking a dead feature; (b) the first e2e spec "simulated" sync by
   writing localStorage and reloading, with comments claiming it verified the channel;
   (c) after a cosmetic fix, the rewritten e2e still passed by opening tab B *after* the
   search. Discrimination discipline (a two-minute two-page probe, then break/restore
   verification: bridge disabled -> 3/3 fail, restored -> 3/3 pass) exposed all three.
4. **Cross-file type ripple**: the feature's machine-type change broke sibling call
   sites (`spawn('history')`, single-arg `createActor`) in files the task did not own.
   A wave-2 type-compat task (owning only the machine + its test) restored a clean
   project-wide typecheck without touching the siblings.
5. **Gate hole - red-baseline tolerance is count-only**: when the recorded baseline is
   already red, a red->red transition with a *different* failure cause (here: a flaky
   URL-sync test, then a broken typecheck) passes as "no new failures". The gate needs
   stage fingerprints (which stage failed + error signatures), not just counts.
   Roadmap: per-stage exit codes and failure fingerprints in the regression record.
6. **Stale preview server**: Playwright's `reuseExistingServer: true` kept an old
   `vite preview` alive on :4173, so several test runs (including a worker's) silently
   exercised a stale bundle and produced wrong conclusions. Verification now includes a
   listener check; delivered as the post-wave process sweep, plus a start-of-run warning
   about pre-existing project listeners (`plan-load --kill-stale` removes them).
7. **Frozen-config vs repair tasks**: the legitimate repair of `vite.config.ts` fails
   the freeze audit (`modified_frozen`), because the policy has no sanctioned exception
   path. Roadmap: per-task freeze allow-lists recorded in `run.json`.
8. **Spiral retry worked**: wave 3 attempt 1 emitted a single ~33K-token message
   (cap), was detected and auto-retried at lower thinking with a conciseness directive.

### Orchestrator interventions (operator-scope, all listed)
Root-cause recon for the `act` failure (including three failed shim spikes); spec
rework after the killed wave; the `defaultSyncChannel` handler bridge; the true
live-delivery e2e rewrite; scoping the dev-mode override to vitest; killing the stale
preview and an orphaned taskdock `ts-node` from the previous session (port 3000);
break/restore discrimination checks. The frontier verifier pass was not run this time -
orchestrator verification covered the same ground, and the gap is noted honestly.

