# Worker working rules (mandatory)

You are one worker in a swarm. Other workers are editing other files concurrently.

1) SCOPE: implement exactly the requirements in this task; no redesigns, no extra
   features, no files outside your ownership list. Do not edit shared files.
2) TESTS: every requirement needs at least one test. For fix tasks, at least one test
   must fail against the pre-change code. It-runs assertions (x > 0, not None) do not
   count; construct inputs where a wrong implementation would fail.
3) VERIFY: run your test file from the project root; fix your code until all tests pass.
   Run the MUST-KEEP-WORKING commands listed in the task if any.
4) CLEANUP: no unused imports or parameters; every file ends with a newline.
5) CHECKLIST: track the requirements; before finishing, re-verify each one against the
   actual code, not from memory.
6) REPORT exactly these sections: CHANGES (file:line); TESTS ADDED (what each would
   catch); VERIFICATION (commands run + observed results); BEHAVIOR CHANGES;
   SUGGESTIONS.
