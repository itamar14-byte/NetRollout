# Security policy

This file is for anyone who has found a security problem in NetRollout. It says which versions get fixes and how to report a vulnerability privately. For how NetRollout protects what it holds, see [docs/security.md](docs/security.md).

## Supported versions

Security fixes go into the latest release only. Upgrade to it to receive them.

| Version | Supported |
|---|---|
| The latest release (1.1.x) | Yes |
| Anything older | No |

## Reporting a vulnerability

> [!IMPORTANT]
> Please don't report a vulnerability in a public issue or a pull request.

Report it privately through GitHub:

1. Go to the repository's [Security tab](https://github.com/itamar14-byte/NetRollout/security).
2. Choose **Report a vulnerability** ([direct link](https://github.com/itamar14-byte/NetRollout/security/advisories/new)).

This opens a private security advisory that only you and the maintainer can see.

### What to include

- The NetRollout version (the footer of every page, `netrollout status`, or `netrollout-cli --version`) and how it was installed (Windows, Linux, the CLI).
- What an attacker can do, and what they need first (network access, an operator account, an admin account).
- Steps to reproduce, or a proof of concept.
- Any idea of a fix, if you have one.

Never include real device credentials, passwords or production configurations.

## What to expect

NetRollout has one maintainer, so everything below is best effort:

- An acknowledgement of your report as soon as the maintainer can.
- An assessment, and a fix in a new release when the report is confirmed.
- Coordinated disclosure: the advisory is published once a fixed release is available. Please keep the details private until then.
- Credit in the advisory and the changelog, unless you prefer not to be named.

## Scope

NetRollout stores network device credentials (encrypted at rest) and pushes configuration to network devices, so it is worth taking seriously. Relevant reports include, for example:

- Reading or using stored device credentials without the right to.
- Bypassing sign-in, two-factor authentication, roles (operator / admin) or ownership of devices, jobs and results.
- Injection into pages, commands or the database.
- Secrets leaking into logs, backups or responses where they shouldn't be.

The security model and its known limits are described in [docs/security.md](docs/security.md). NetRollout is provided "as is", without warranty, as set out in its [license](LICENSE).
