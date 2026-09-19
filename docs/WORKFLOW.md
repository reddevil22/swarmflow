# swarmflow workflow: PRD -> MVP 1

## Stage 0 - Intake (frontier)
Input: PRD/spec text. Output: `requirements.md` (normalized, numbered), MVP-1 scope cut,
non-goals, acceptance criteria. Scope is frozen at the end of this stage.

## Stage 1 - Plan (frontier)
Output: `plan.yaml` containing
- work packages with **disjoint file ownership** (one module or feature per package)
- frozen interface contracts (signatures, schemas, CLI surface)
- dependency DAG and wave assignment (wave 1 = leaves with no dependencies)
- per-task spec text (enumerated requirements + edge cases)
- acceptance criteria per task (machine-checkable where possible)
- risk tier -> worker thinking level mapping

## Stage 2 - Scaffold (script)
- create project repo (git init), directory layout, test harness
- copy `AGENTS.worker.md` -> project `AGENTS.md`, write `SPEC.md` + contracts
- record ownership map in the ledger
- freeze the baseline: `swarmflow freeze --project <root>` records sha256 for every
  non-owned file; owned paths stay mutable, everything else must remain byte-identical

## Stage 3 - Build waves (swarm)
- dispatch queued tasks of a wave, concurrency <= config (default 8-10)
- per-task JSON trace stored under `logs/`; ledger updated on completion
- artifact audit on every completion: expected files exist, tests run, report present
- spiral/timeout failures retried once at thinking `medium` (see ARCHITECTURE.md)
- after each wave the supervisor runs the scope audit (frozen-file integrity plus
  stray/added-file detection); violations mark the wave failed and are ledgered

## Stage 4 - Verify (scripts + frontier)
Per wave boundary:
1. full regression suite + MUST-KEEP-WORKING contract checks
2. discrimination check: the wave's owned test files re-run at the run's base commit in
   a throwaway worktree (`evidence/wave<N>.discrimination.json`; `enforce` mode fails
   the wave, `warn` records evidence)
3. requirement->test traceability matrix parsed from worker reports
4. verifier pass (frontier) over specs vs. delivered artifacts; findings become fix tasks
5. new tasks (fixes/retries) are appended to the current or next wave

## Stage 5 - Integrate
- integration session(s) own shared files (`__init__`, CLI wiring, package config)
- end-to-end demo command runs green; its transcript is kept as evidence

## Stage 6 - PR gate (HUMAN CHECKPOINT)
- control plane creates a branch, commits the wave, and opens a pull request
  (gh CLI); PR body includes: summary, evidence bundle links, open issues, demo output
- task statuses -> `awaiting_pr_review`; pipeline pauses
- human merges (or requests changes -> new fix tasks)
- `swarmflow wave run --continue` resumes after merge

## Stage 7 - Package (deployable artifact)
- build the artifact (wheel/binary/container per project type), run the packaged
  artifact's smoke test, attach build outputs + checksums to the evidence bundle
- acceptance (frontier) maps every frozen acceptance criterion to evidence;
  gaps become the MVP-2 backlog

## Brownfield runs (existing repositories)

Same pipeline, stricter envelope. Used when `mode: brownfield` is set in the plan.
1. **Recon**: `swarmflow recon --project P` - stacks, commands (evidence-backed), git
   state, existing test inventory, bounded file inventory; writes
   `.swarmflow/recon.json` and the digest the planner consumes.
2. **Plan**: planner receives the PRD plus the recon digest. Minimal diffs; shared
   surfaces go to an integration task; `test_command` uses the repo's own runner.
3. **Preflight** (`plan-load`): git required; tracked-file dirtiness refuses the run
   (`--allow-dirty` overrides); `.swarmflow/` and `logs/` are appended to `.gitignore`;
   the run branch `swarmflow/<name>` is created or reused; `run.json` records
   mode/branch/base_sha (first write wins).
4. **Waves**: before each wave, freeze only that wave's owner map (per-wave ownership).
   After each wave: the project regression suite runs and is compared against the
   baseline in `run.json` by failure identity - fingerprinted failing tests plus an
   executed-test inventory, so a different test breaking at equal counts and a suite
   shrinking while staying green both fail the wave. Uncomparable results fail closed
   (`regression.strict`), the comparison is written to `evidence/wave<N>.compare.json`,
   and `--rebaseline` re-records the baseline when the current state is known-good.
   Then the discrimination check re-runs the wave's owned test files at `base_sha` in a
   throwaway git worktree (verdicts to `evidence/wave<N>.discrimination.json`; warn by
   default, `enforce` fails the wave). Then the git-based scope audit runs. Untouched
   deliveries are rejected (`no_changes`).
5. **Verify/accept**: `swarmflow evidence --project P` assembles the bundle (recon
   digest, ledger, worker reports from traces, audit, regression, bounded diff of
   tests + manifests) for the verifier/acceptance passes.
6. **Human review** on the run branch. No PR automation.

## Evidence bundle (per run)
```
evidence/
  requirements.md            frozen scope + acceptance criteria
  plan.yaml                  tasks, ownership, acceptance per task
  reports/<task>.md          worker reports (5 required sections)
  traces/<task>*.jsonl       raw session traces (incl. failed attempts)
  verification.md            regression results, traceability matrix, verifier findings
  demo.txt                   end-to-end demo transcript
  acceptance.md              criterion -> evidence mapping + verdict
```

## Rules that are non-negotiable (from experiments)
- artifact-based progress; exit codes are advisory only
- workers never touch files outside their ownership; shared files are integration-only
- every worker task includes at least one test that fails against the pre-change state
  (fix tasks) or verifier-authored acceptance tests (greenfield)
- admission control: if the model server queues (waiting > 0) or KV > 55%, hold launches
