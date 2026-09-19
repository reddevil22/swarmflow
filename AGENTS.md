# Agent guide for swarmflow

This file is for coding agents (and humans) working ON this repository. It is not
the worker prompt template - that is `AGENTS.worker.md`, which gets copied into
scaffolded target projects.

## What this is
swarmflow turns a project spec/PRD into an MVP using a swarm of local agent sessions
(Pi against a local OpenAI-compatible model) supervised by a deterministic control
plane, with a frontier model at decision gates. Read `docs/ARCHITECTURE.md` and
`docs/WORKFLOW.md` before changing behavior.

## Ground rules for changes
- Tests: `python -m pytest tests -q` must pass. Tests must never require network,
  a model server, or an installed agent CLI (mock or unit-test instead).
- Keep the CLI non-interactive and scriptable. Machine consumers matter: prefer
  `--json` output for new status-like commands.
- The guardrails exist because of observed failures. Do not weaken them (turn cap,
  forbidden-action scan, frozen-file audit, admission control) without replacing
  them with something stronger and documenting it in `docs/ARCHITECTURE.md`.
- Configuration must stay portable: no personal paths in tracked files; resolve
  executables via `swarmflow.config.resolve_executable` and allow `${ENV_VAR}`
  expansion. Local overrides go in `config/swarmflow.yaml` (untracked).
- Frontier backends live in `swarmflow/frontier.py`. To add one, implement
  `complete(prompt, **kwargs)` returning the normalized dict and register it in
  `build_backend()`. Keep tests network-free (inject a transport, or run a local
  process such as `sys.executable`).
- Prompt templates in `prompts/` are part of the behavior surface. If you change a
  template, update the matching parser/validator (e.g. plan schema) in the same change.

## Layout
- `swarmflow/` - control plane package (config, ledger, frontier, workers, audit,
  recon, regression, evidence, plan, cli)
- `prompts/` - planner / task brief / verifier / acceptance templates
- `docs/` - architecture, workflow, plan schema, case study
- `tests/` - unit tests (fast, no network)
- `examples/` - runnable example plan
- `config/swarmflow.example.yaml` - copy to `config/swarmflow.yaml` (or run
  `swarmflow init`)

## Common commands
```
pip install -e ".[dev]"
python -m pytest tests -q
swarmflow status --json
swarmflow audit --project <path> --json
swarmflow recon --project <path>            # brownfield survey
swarmflow evidence --project <path>          # verifier bundle
swarmflow smoke-frontier        # needs the configured frontier backend
swarmflow smoke-worker          # needs Pi + a local model endpoint
```

## Gotchas
- Windows: `.cmd` shims must be invoked through `cmd /c`; `.js` entry points through
  `node`. Use `swarmflow.config.build_cli_command`.
- `NODE_ENV=production` in this environment makes npm omit devDependencies: when someone
  installs dependencies for a target repo, use `npm ci --include=dev`. The control plane
  never installs anything itself.
- The ledger (`state/ledger.db`) is local state, never committed.
