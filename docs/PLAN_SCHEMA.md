# Plan schema (`plan.yaml`)

A plan is the machine-readable contract between the planner (frontier model) and the
swarm. `swarmflow plan-load --plan plan.yaml` validates it, scaffolds the project,
writes per-task specs, and enqueues tasks in the ledger.

## Top level

| field | required | meaning |
|---|---|---|
| `project_name` | no | short slug used in scaffolded SPEC.md |
| `project` | yes* | absolute or repo-relative path to the target project root |
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
| `thinking` | no | `medium` or `high` (default `high`); use `medium` for parser/formatting-heavy tasks to avoid reasoning spirals |
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

## Example
See `examples/smoke-plan.yaml` for a minimal two-task plan and `docs/CASE_STUDY.md`
for a five-task production example (NestJS + hexagonal architecture).
