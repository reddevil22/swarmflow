# Examples

## smoke-plan.yaml - minimal two-task run

Prerequisites: a working worker setup (`swarmflow smoke-worker`) and a frontier CLI
(`swarmflow smoke-frontier`). The example uses Python modules with pytest because it
needs nothing else installed.

```bash
cd <repo root>
swarmflow plan-load --plan examples/smoke-plan.yaml
swarmflow freeze --project example-target
swarmflow wave-run --wave 1 --concurrency 2
swarmflow audit --project example-target --json
swarmflow status --json
```

What you should see: two workers deliver `greeter.py` and `fizzbuzz.py` with their
tests under `example-target/`, the ledger turns green, and the post-wave scope audit
prints `ok`. `example-target/` is gitignored - delete it freely between runs.

Notes:
- `project` in the plan is resolved relative to the current directory; run from the
  repository root, or pass `--project <path>`.
- The example mirrors the shape used in the TaskDock case study
  (`docs/CASE_STUDY.md`): disjoint `owner_files`, one exact `test_command` per task,
  and acceptance checks.
