# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(**Security → Report a vulnerability** on this repository), or email the
maintainer. Include:

- what the issue is and where it lives (file/route),
- how to reproduce it,
- the impact you believe it has.

You can expect an acknowledgement within a few days. Please give a reasonable
window to fix and release before any public disclosure.

## Scope

In scope: the application code in `src/`, the container image and its
configuration, and the CI/CD workflows in `.github/`.

Out of scope: findings that require a compromised host or credentials you were
already given; reports from automated scanners with no demonstrated impact.

## Automated checks

This repository runs, on every pull request and on `main`:

- **CodeQL** — static analysis for Python (`.github/workflows/codeql.yml`).
- **gitleaks** — secret scanning over the full history
  (`.github/workflows/secret-scan.yml`).
- **Dependency review** — blocks PRs that introduce high-severity vulnerable
  dependencies (`.github/workflows/dependency-review.yml`).
- **Dependabot** — weekly dependency and base-image update PRs
  (`.github/dependabot.yml`).

GitHub's native secret scanning with push protection and private vulnerability
reporting should also be enabled in the repository settings.
