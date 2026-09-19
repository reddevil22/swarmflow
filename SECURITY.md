# Security Policy

## Reporting a vulnerability
Please report suspected vulnerabilities through GitHub's private security advisory
("Report a vulnerability" on the repository's Security tab). Do not open a public
issue for security problems. You will get a response as maintainers are available;
please include reproduction steps and affected versions.

## Scope notes
swarmflow executes local agent CLIs and model endpoints that you configure. It does
not sandbox them: workers run with the permissions of the user that launched
swarmflow, can edit files inside the target project, and the target project's own
`AGENTS.md` rules are advisory to the agents.

Documented guardrails (turn caps, forbidden-action scanning, frozen-file auditing)
reduce known failure modes but are not a security boundary. If you run untrusted
plans, prompts, or target repositories, run swarmflow in a container or dedicated
user account, consistent with the containerization guidance for the worker harnesses
you use.

## Supported versions
Pre-1.0: only the latest release on `main` receives fixes.
