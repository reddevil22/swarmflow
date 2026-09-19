# Worker task brief

{context_files}

TASK ID: {task_id}
PROJECT ROOT: {project_root}

FILES YOU OWN (create/edit only these):
{owner_files}

STUDY FIRST (existing files relevant to this task; read before editing):
{files_to_read}

SPECIFICATION:
{spec}

ACCEPTANCE CHECKS (must all hold when done):
{acceptance}

MUST KEEP WORKING (the control plane runs this after your wave; do not break it):
{must_keep_working}

## VERIFICATION
- Run EXACTLY your verification command, once per fix cycle, never in a loop:
  `{test_command}`
- Do not run other workers' tests and do not explore the repository for test setups;
  the control plane runs the full project suite after the wave.

{worker_rules}
{stack_rules}

When done, run your verification command from the project root and report using the
required sections.
