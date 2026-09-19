# Contributing to swarmflow

Thanks for your interest. swarmflow orchestrates swarms of local coding agents; the
quality bar for this repository is that every guardrail is justified by an observed
failure and covered by a test.

## Development setup
```bash
git clone <your-fork>
cd swarmflow
pip install -e ".[dev]"
python -m pytest tests -q          # must pass; tests never use the network
```

## Project principles (read these first)
- `docs/ARCHITECTURE.md` documents roles, the task state machine, the failure
  taxonomy, and why each guardrail exists. Behavior changes must update it.
- `docs/WORKFLOW.md` is the contract for the PRD-to-MVP pipeline stages.
- Prompt templates under `prompts/` are part of the behavior surface: when you change
  a template, update the code that consumes its output (e.g. plan validation) and the
  matching docs in the same change.
- Keep the CLI non-interactive and scriptable; new status-like commands should offer
  `--json` output. Success is judged by artifacts and exit codes that mean things -
  never by trusting an agent's self-report.

## Tests
- Unit tests only: no network, no model server, no real agent CLIs. Mock or construct
  inputs (see `tests/test_audit.py` for the style).
- Every fix or feature needs a test that would fail on the previous behavior.

## Pull requests
1. One logical change per PR; describe what failure mode or use case it addresses.
2. Run `python -m pytest tests -q` before opening; include the result in the PR body.
3. Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.
4. CI is not wired up yet - maintainers run the suite manually.

## Reporting issues
Include: what you ran, what you expected, what happened, and the relevant ledger
events (`swarmflow status --json`) or trace excerpts when the issue involves a run.
