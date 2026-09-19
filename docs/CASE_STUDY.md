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
| Verifier flagged non-discriminating tests and missing e2e assertions | model-produced tests can be green without pinning behavior | recorded as open issues; verification prompts require discrimination evidence |

## Open items at the time of writing
- No automated gate against a worker editing files it does not own (the audit catches
  it after the fact and fails the wave; prevention would need tool-level enforcement).
- Some generated controller tests are non-discriminating; the verifier role is the
  current mitigation.
