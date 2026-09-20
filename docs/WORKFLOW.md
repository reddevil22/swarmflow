# swarmflow workflow: PRD -> MVP 1

## Stage 0 - Intake (frontier)
Input: PRD/spec text. Output: `requirements.md` (normalized, numbered), MVP-1 scope cut,
non-goals, acceptance criteria. Scope is frozen at the end of this stage.

## Stage 1 - Plan (frontier)
`swarmflow plan --prd <file> --project <path> [--mode brownfield] [--load]` invokes
`prompts/planner.md`, validates the result (one retry with the validation errors as
feedback), injects `project`/`mode` from the CLI, and writes
`<project>/.swarmflow/plan.yaml` (raw model output in `evidence/plan.json`).
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
   the wave, `warn` records evidence). Python projects get their worktree source roots
   prepended to PYTHONPATH (`discrimination.python_paths`) and an import probe; an
   import that escapes the worktree (editable installs that bypass sys.path) makes the
   check indeterminate rather than a silent false pass.
3. requirement->test traceability matrix parsed from worker reports (roadmap - not
   implemented yet)
4. verifier pass (frontier): `swarmflow verify --task <id>` per task, or the
   `wave-run --verify` stage over a wave's delivered tasks (`verify.enabled` in config,
   capped by `verify.max_tasks`). The material is the task row, the frozen spec, the
   pinned PRD (`<project>/.swarmflow/PRD.md`, hash-checked against the run state if
   `swarmflow plan` wrote it), the worker report, the owned files' contents and the
   latest gate results. A `pass` moves the task to `verified`; `fail`/`needs_fix`
   moves it to `needs_fix` and the findings are printed + stored in `evidence/verify_<id>.json`.
   Recovery is a **fresh plan producing new task ids** - `wave-run` only dispatches `queued`.
5. new tasks (fixes/retries) are appended to the current or next wave

## Stage 5 - Integrate
- integration session(s) own shared files (`__init__`, CLI wiring, package config)
- end-to-end demo command runs green; its transcript is kept as evidence

## Stage 6 - Human review (HUMAN CHECKPOINT)
- the control plane leaves the work on the run branch (`swarmflow/<name>`) with an
  evidence bundle (`swarmflow evidence --project P`)
- **no PR automation exists**: there is no `gh` integration, no `awaiting_pr_review`
  transition and no `--continue` flag. The human reviews the branch and merges it by
  hand; requested changes become new fix tasks (roadmap: PR gate + pipeline resume).

## Stage 7 - Package (deployable artifact) - roadmap, not implemented
- planned: build the artifact (wheel/binary/container per project type), run the
  packaged artifact's smoke test, attach build outputs + checksums to the evidence bundle
- acceptance is wired: `swarmflow accept --project <path>` maps the frozen acceptance
  criteria to evidence via `prompts/acceptance.md` + `evidence/bundle.md`; an `accepted`
  verdict moves every `verified` task to `accepted`, `rejected` prints the gaps. The
  packaged-artifact part remains roadmap.

## Brownfield runs (existing repositories)

Same pipeline, stricter envelope. Used when `mode: brownfield` is set in the plan.
1. **Recon**: `swarmflow recon --project P` - stacks, commands (evidence-backed), git
   state, existing test inventory, bounded file inventory; writes
   `.swarmflow/recon.json` and the digest the planner consumes.
2. **Plan**: planner receives the PRD plus the recon digest. Minimal diffs; shared
   surfaces go to an integration task; `test_command` uses the repo's own runner.
3. **Preflight** (`plan-load`): git required; tracked-file dirtiness refuses the run
   (`--allow-dirty` overrides); `.swarmflow/` and `logs/` are appended to `.gitignore`;
   the run branch `swarmflow/<name>` is created or reused; the gate command is resolved
   once (config override or recon) and **frozen in the control-plane state store**
   (`<state_dir>/runs/<hash-of-project>/run.json`, default `<repo>/state/`) together with
   mode/branch/base_sha. The project-side `.swarmflow/run.json` is a marked mirror that
   nothing reads; a pre-upgrade run is adopted once (structural fields only). Pre-existing
   untracked files are snapshotted (file-level) into the run and exempted from the scope
   audit - only files created after the preflight can violate it.
4. **Waves**: before each wave, freeze only that wave's owner map (per-wave ownership).
   The freeze **carries** the previous baseline's hashes for files the wave does not own,
   so an edit made between waves stays visible; only an owning wave re-reads a file, and
   its post-wave content is sealed right after the audit. A file the wave *created* is
   added to the baseline by that seal (capped per wave) and leaves the owner map, so the
   next wave protects it by hash instead of flagging it as a new file. A deliberate
   re-baseline is `swarmflow freeze --project P` (mode inferred from the run; it prints
   the counts and keeps previously sealed entries).
   After each wave: the process sweep diffs process snapshots taken before/after the
   wave, attributes new processes to the project (command line or working directory),
   attaches listening ports and reports them (`evidence/wave<N>.sweep.json`;
   `sweep.mode: kill` terminates this wave's new processes, pre-existing listeners are
   only reported). The sweep runs **before** the regression suite so a leaked server
   cannot serve stale code to the gate. Then the project regression suite runs and is
   compared against the baseline in the control-plane state store by failure identity -
   fingerprinted failing tests plus an executed-test inventory, so a different test
   breaking at equal counts and a suite shrinking while staying green both fail the
   wave. A config `regression.command` change always wins over the frozen value and is
   printed. Uncomparable
   results fail closed (`regression.strict`), the comparison is written to
   `evidence/wave<N>.compare.json`, and `--rebaseline` re-records the baseline when the
   current state is known-good. Then the discrimination check re-runs the wave's owned
   test files at `base_sha` in a throwaway git worktree (verdicts to
   `evidence/wave<N>.discrimination.json`; warn by default, `enforce` fails the wave).
   Then the git-based scope audit runs. Untouched deliveries are rejected (`no_changes`).
   Preflight additionally warns about project-attributed listeners that were already
   running before the run (`plan-load --kill-stale` removes them).
5. **Verify/accept**: `swarmflow evidence --project P` assembles the bundle (recon
   digest, ledger, worker reports from traces, audit, regression, bounded diff of
   tests + manifests) for the verifier/acceptance passes.
6. **Human review** on the run branch. No PR automation.

## Evidence (per run, actually written today)
```
<project>/.swarmflow/
  SPEC.md                    frozen scope + acceptance criteria
  specs/<task>.md            the spec text handed to each worker
  recon.json                 reconnaissance digest (stacks, commands, tests, git state)
  evidence/
    bundle.md                assembled bundle (recon, ledger, reports, audit, regression,
                             discrimination, processes, bounded diff) for the verifier
    baseline.txt             regression baseline run output
    wave<N>.txt              regression run output for wave N
    wave<N>.compare.json     fingerprint comparison (new/fixed failures, suite delta)
    wave<N>.discrimination.json  per-file parent-state verdicts
    wave<N>.discrimination.parent<K>.txt  raw output of each parent-state run
    wave<N>.sweep.json       post-wave process sweep (orphans, pre-existing, killed)
  logs/<task>_a<N>.jsonl     raw session traces
<state_dir>/runs/<hash>/     control-plane state store: run.json + frozen.json
```
The verifier/acceptance prompts read `evidence/bundle.md`; they are operator-run today
(there is no `swarmflow verify`/`accept` command yet).

## Rules that are non-negotiable (from experiments)
- artifact-based progress; exit codes are advisory only
- workers never touch files outside their ownership; shared files are integration-only
- every worker task includes at least one test that fails against the pre-change state
  (fix tasks) or verifier-authored acceptance tests (greenfield)
- admission control: if the model server queues (waiting > 0) or KV > 55%, hold launches
