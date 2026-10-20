# Security policy

## Report a problem

Do not open a public issue for a security problem.

Use GitHub private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

The report is visible only to the maintainers. If the form is not available,
open a public issue that asks the owner for a private channel. Do not include
the details in that issue.

## In scope

- A signed command that a machine accepts without the right key, or that
  can be replayed.
- A Redis login or ACL rule that gives more access than the command needs.
- A secret in a log, an error message, a command argument, or a commit.
- A path that lets a loop read another worker's signing key.
- A privilege escalation through the loop user's sudo rules.

## Out of scope

- A problem that needs the owner's own machine or an unlocked session.
- A missing rate limit on a local-only dashboard.
- A finding in a third-party package that already has an advisory.

## What to expect

The owner reads the report and answers in the advisory. Do not publish the
problem until the owner says it is safe.
