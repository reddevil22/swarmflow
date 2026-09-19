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
2. requirement->test traceability matrix parsed from worker reports
3. verifier pass (frontier) over specs vs. delivered artifacts; findings become fix tasks
4. new tasks (fixes/retries) are appended to the current or next wave

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
