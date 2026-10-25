# Security Policy

This repository is part of SmartHeat (future product name: Setwell), a heating control service for Home Assistant and
for the SmartHeat gateway. It contains the Home Assistant add-on and the gateway software. The project is run by a
single person; the targets below are best-effort commitments, not a service level.

## Supported versions

Only the current release of the add-on and of the gateway software (the latest release tag on `main`) receives
security fixes. Please update before you report, or tell us which version you tested.

## Reporting a vulnerability

Please do **not** open a public issue, pull request or discussion for a vulnerability.

Report it privately by one of these ways:

1. GitHub: open the **Security** tab of this repository and choose **Report a vulnerability** (private vulnerability
   reporting).
2. E-mail: `security@hartfussha.org` (plain text; we do not offer PGP at the moment).

Useful content: affected component and version, what an attacker gains, steps to reproduce or a proof of concept, and
whether you think the issue is already being exploited (if so, say so in the first line). Please do not send
credentials or personal data of third parties.

## What you can expect

- **Acknowledgement:** within 3 working days.
- **First assessment** (accepted, not reproducible, out of scope) within 10 working days.
- **Status updates** at least every 14 days until the issue is resolved.
- **Fix and disclosure:** we aim to ship a fix and publish a security advisory within 90 days of the report, sooner
  for severe issues. If you want to publish earlier, please talk to us first; we will agree on a date together.
  Vulnerabilities that are being actively exploited are handled with priority and reported to the authorities as the
  law requires.
- **Credit:** we name you in the advisory if you wish; we do not run a bug bounty.

Response times can be longer during holidays or illness of the maintainer.

## Scope

In scope: the code in this repository, the release artifacts built from it, and the SmartHeat services that the add-on
and the gateway talk to.

Out of scope: denial-of-service by volume, social engineering of people, physical attacks, and issues in third-party
software (Home Assistant, your router, the manufacturer's cloud); please report those upstream.

## Good-faith research

We will not take legal action against research that follows this policy: do not access or change data of other
people, do not test against installations that are not yours, do not degrade the service, and give us time to fix
the issue before you publish it.
