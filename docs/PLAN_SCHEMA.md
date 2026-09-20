# Plan schema (`plan.yaml`)

A plan is the machine-readable contract between the planner (frontier model) and the
swarm. `swarmflow plan-load --plan plan.yaml` validates it, scaffolds the project,
writes per-task specs, and enqueues tasks in the ledger. `swarmflow plan --prd <file>
--project <path>` produces the file with the frontier planner (the `project`/`mode` keys
are injected from the CLI args, so the result loads as-is).

## Top level

| field | required | meaning |
|---|---|---|
| `project_name` | no | short slug used in scaffolded SPEC.md and the run branch name |
| `project` | yes* | absolute or repo-relative path to the target project root |
| `mode` | no | `greenfield` (default) or `brownfield`; brownfield requires git, writes artifacts under `.swarmflow/`, and enables the regression gate |
| `tasks` | yes | non-empty list of task objects (see below) |

\* `--project` on the CLI overrides the plan value; one of the two must be present.

## Task object

| field | required | meaning |
|---|---|---|
| `id` | yes | unique short id (used in ledger, traces, `specs/<id>.md`) |
| `module` | yes | human label for the area this task owns (directory or file) |
| `owner_files` | yes | exact list of files the worker may create/edit - no other task may list them |
| `spec` | yes | enumerated requirements + edge cases; written to `specs/<id>.md` and embedded in the worker brief |
| `acceptance` | no | machine-checkable checks (listed in the brief and used by verifiers) |
| `test_command` | recommended | one exact runnable command the worker runs to verify; injected verbatim into the brief so workers never discover test setups |
| `files_to_read` | no | existing files the worker should study first (defaults to `owner_files`) |
| `thinking` | no | one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (default `high`); use `medium` for parser/formatting-heavy tasks to avoid reasoning spirals |
| `wave` | no | integer wave (default 1); later waves may depend on earlier files existing |

## Validation rules (enforced by `swarmflow.plan.validate_plan`)
1. `tasks` is non-empty; unknown top-level fields are ignored, unknown task keys are
   tolerated only if they do not break the model above.
2. Every task has `id`, `module`, `owner_files`; `owner_files` must be non-empty.
3. Ownership is **disjoint**: no file may appear in two tasks. Shared files
   (package manifests, tool configs, package `__init__`, CLI entry points) belong to
   integration, never to a worker - keep them out of `owner_files` and record them in
   the scaffold instead.
4. Ids are unique; `test_command` (when present) must be a string.
5. Wave assignment should reflect dependency order: wave 1 tasks must be buildable
   from the scaffold alone.
6. `id` and `project_name` must match `^[A-Za-z0-9._-]{1,64}$` and contain no `..`
   (ids become spec/log filenames); `owner_files` and `files_to_read` entries must be
   non-empty, project-relative, and free of `..` segments and drive prefixes.
7. `thinking` must be one of the levels above; `acceptance` must be a list of strings;
   `module` must be a string.

## Example
See `examples/smoke-plan.yaml` for a minimal two-task plan and `docs/CASE_STUDY.md`
for a five-task production example (NestJS + hexagonal architecture).

## Brownfield notes
- `owner_files` lists files to CREATE or MODIFY; ownership stays disjoint across all
  tasks of the run. Shared surfaces (manifests, tool configs, package entry points,
  registries/DI wiring) belong to a dedicated integration task, and per-wave freezes
  make them mutable only in that task's wave.
- All swarmflow artifacts live under `.swarmflow/` (SPEC.md, specs/, run.json,
  recon.json, frozen.json, evidence/), which is appended to the repo's `.gitignore`.
- The worker brief injects the canonical working rules inline (the repo's own
  AGENTS.md is never overwritten) and carries the regression command as
  MUST KEEP WORKING.
