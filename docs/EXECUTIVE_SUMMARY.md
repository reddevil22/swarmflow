# Swarmflow - executive summary

Swarmflow is a command-line tool that takes a written requirement and drives AI models
through the delivery loop: split the work into tasks, hand each task to a coding agent,
run the project's own tests, have a second model review the result, and keep a record of
every step. An operator stays in the loop: they approve the plan before work starts and
review the branch that comes out.

## Local inference: one server, many sessions

The runs this summary counts on - the `sdlc-cli` change, the `prompt-scaler` run, and a
scratch greenfield project - sent every AI call to a single local vLLM server
(`192.168.1.79:8000`, model `qwen3.6-35b-a3b`): the planning call that turns requirements
into tasks, the coding sessions that write the code, and the review calls that judge the
result. (An earlier validation run, `TaskDock`, predates that setup: its plan came from a
cloud model; only its four coding sessions ran locally.)

Coding agents run as long-lived sessions, several at once, and each session executes the
project's real test suite while it works. The harness keeps this within what the machine
can serve:

- Sessions are dispatched with a short stagger: four at once in the `TaskDock` validation
  run, two in the later runs.
- Dispatch is paced by the server's own live load - request queue length and memory
  pressure - so sessions wait rather than overwhelm the endpoint.
- Sessions last minutes, not seconds: `TaskDock`'s four-worker wave finished in about
  2m19s and its single-worker follow-up in about 1m42s; a small change to an existing
  repository took 8 turns and roughly 2.4k output tokens; a full fix-and-verify cycle
  took about two minutes.
- The same server answers the planning and review calls; one planning attempt exceeded a
  five-minute timeout while the server reported no other active requests, and succeeded
  on retry inside a ten-minute budget.

The work served was real, not benchmark traffic: a NestJS API with 48 unit and 11
end-to-end tests, an existing Python project with 139 tests, a TypeScript project from an
earlier run, and a small greenfield project used to exercise the pipeline.

## Day-to-day work this supports

- **Changes to existing repositories.** On `sdlc-cli` (Python, 139 tests) the harness
  added an optional check to an existing class plus five tests in 8 turns, changing only
  the two files the task was allowed to change, with the suite green (139 -> 144); the
  change sits on a branch awaiting review.
- **Small greenfield builds.** A three-task project was planned, built in batches,
  reviewed task by task, and accepted with no gaps.
- **Checks that run after every batch.** The project suite is re-run and compared with a
  snapshot taken before the run; on existing-repository runs the new tests are re-run
  against the old code to see whether they actually catch anything; and a file-ownership
  check confirms nothing outside a task's files was touched.
- **Review cycles that close.** When review finds a problem, the findings are fed into
  the next attempt. This was proven directly: a re-run without the findings answered "no
  changes needed"; the same task with the findings attached fixed the weak test
  assertions and passed.

## How results are checked

Tests must pass, output is limited to the files a task may change, and changes are
compared against the pre-run snapshot so a passing suite cannot be faked by weakening
tests. A separate model then reviews each delivery against the task description, and a
final acceptance pass maps the stored evidence to the original criteria. Every verdict
and piece of evidence is kept under the project's `.swarmflow/evidence/`.

## Known limits

- The test check copies the repository at its starting commit and runs the new tests
  there to prove they fail against the old code. Python projects installed in editable
  mode can defeat that copy; those runs are now flagged inconclusive rather than passed
  silently.
- Planning calls can take minutes; one attempt exceeded a five-minute timeout on an idle
  server (the retry succeeded).
- Pull-request automation and packaged delivery are not built.

## Basis

Compiled September 2026 from end-to-end runs on `sdlc-cli`, `prompt-scaler`, a scratch
greenfield project, and the earlier `TaskDock` validation, plus the harness's own suite
(246 tests).
