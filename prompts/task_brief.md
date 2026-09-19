# Worker task brief template

Read AGENTS.md first; it defines the mandatory working rules for this swarm.
Other workers are editing other files concurrently - stay strictly inside your ownership.

TASK ID: {task_id}
PROJECT ROOT: {project_root}

FILES YOU OWN (create/edit only these):
{owner_files}

SPECIFICATION:
{spec}

ACCEPTANCE CHECKS (must all hold when done):
{acceptance}

MUST KEEP WORKING (do not break these; run them if provided):
{must_keep_working}

## ENVIRONMENT RULES (violations cause rejection)

- This project uses ONLY npm and npx. Never use pnpm, yarn, or bun.
- Run EXACTLY this verification command, from the project root:
  `{test_command}`
  Run it once per fix cycle, never in a loop, and never run other workers' tests.
  Do not explore the repository for test setups or alternative commands.
- NEVER read, grep, list, or explore node_modules. It is not your business.
- NEVER modify package.json, package-lock.json, tsconfig.json, jest.config.js,
  jest-e2e.json, or any file you do not own. Do not run npm/pnpm/yarn install;
  dependencies are already installed.
- Do not create scratch/temp files. If you created one by accident, delete it.
- If the SAME failure persists after 3 fix attempts, stop immediately and report
  a `BLOCKED` section with: the exact command, the exact output, and what you
  tried. Do not keep probing.
- Stay under ~40 tool calls. Reading your own code beats shell experimentation.

When done, run your tests from the project root and report using the required sections.
