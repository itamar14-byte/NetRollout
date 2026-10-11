# NetRollout

**Push config. Verify. Done.**

[![CI](https://github.com/itamar14-byte/NetRollout/actions/workflows/ci.yml/badge.svg)](https://github.com/itamar14-byte/NetRollout/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/itamar14-byte/NetRollout?include_prereleases&sort=semver)](https://github.com/itamar14-byte/NetRollout/releases)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Docker Hub](https://img.shields.io/docker/v/itamarweinstein/netrollout?sort=semver&label=docker%20hub)](https://hub.docker.com/r/itamarweinstein/netrollout)

NetRollout pushes configuration snippets to many network devices at once over SSH, reads each device's configuration back to verify the change, and records every result. You run it as a web app on your own server (Windows or Linux, with Docker), or as a single command-line program for a one-off rollout from a CSV file.

It is for network engineers who want a bulk change done and checked without writing playbooks.

![The NetRollout dashboard](docs/images/dashboard.png)

> [!IMPORTANT]
> NetRollout is at release candidate stage. Try it on a lab device before you point it at production.

## Contents

- [Features](#features)
- [Supported platforms](#supported-platforms)
- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Documentation](#documentation)
- [Support and contributing](#support-and-contributing)
- [License](#license)

## Features

- **Bulk push.** Send the same commands to many devices in parallel, with per-job and per-device concurrency limits.
- **12 platforms.** Cisco, Juniper, Arista, Aruba, HP, Palo Alto, Check Point and Fortinet, through [Netmiko](https://github.com/ktbyers/netmiko). Each platform saves or commits the change in its own way.
- **Verify.** After the push, NetRollout reads each device's configuration and checks every command, including `no` / `undo` / `delete` / `unset` removals. Verify is on by default in the web app.
- **Live log.** Watch a rollout as it runs, device by device. Cancel a queued or running job.
- **Results and rollback.** Every device's outcome is stored, with a status, the log, a configuration snapshot and a Verify Diff. Cases that need a person are flagged as `ACTION NEEDED`. You can roll back a finished job from its results.
- **Inventory.** Keep devices with labels and attributes, import them from CSV, and store credentials as encrypted security profiles.
- **Variable mappings.** A `$$TOKEN$$` in your commands is filled in per device from a device property, so one snippet fits many devices.
- **Accounts.** Operator and admin roles, local accounts with approval, LDAP sign-in, and two-factor authentication (TOTP).
- **Audit log.** Sign-ins, rollouts, setting changes and admin actions are recorded.
- **Analytics.** Success rate, device pushes, commands pushed, the devices that fail most and the top platforms over 30 days, for your own rollouts; admins can view any user's, and an organisation overview.
- **Safe-to-share logs.** Passwords, secrets and SNMP communities in job logs are replaced with `<redacted>`.
- **Backups.** Scheduled and on-demand backups of the database, certificates, logs and dashboards; restore from the command line.
- **Monitoring.** Optional Prometheus, Loki and Grafana, with ready-made dashboards for admins.
- **Updates.** NetRollout Manager (Windows) and `netrollout.sh update` (Linux) download, check and apply a new release, with a backup first.
- **CLI.** `netrollout-cli` runs a one-off rollout from a devices CSV and a commands file. It needs no database and no Docker.

## Supported platforms

NetRollout uses Netmiko's `device_type` names. The finish column says how a pushed change is made permanent.

Most platforms were implemented from vendor documentation and have not yet been confirmed on real equipment. If you run NetRollout against one of them, please tell us whether it worked with the [platform confirmation form](https://github.com/itamar14-byte/NetRollout/issues/new?template=platform_confirmation.yml). Confirmed platforms are marked here as reports come in.

| Vendor / OS | `device_type` | How a push is finished | Status |
| --- | --- | --- | --- |
| Cisco IOS | `cisco_ios` | Save the configuration | Tested in a lab (Cisco IOL on EVE-NG) |
| Cisco IOS-XE | `cisco_xe` | Save the configuration | Not yet confirmed |
| Cisco NX-OS | `cisco_nxos` | Save the configuration | Not yet confirmed |
| Cisco IOS-XR | `cisco_xr` | Commit (a failed commit reverts by itself) | Not yet confirmed |
| Arista EOS | `arista_eos` | Save the configuration | Not yet confirmed |
| Aruba AOS-CX | `aruba_aoscx` | Save the configuration | Not yet confirmed |
| HP ProCurve | `hp_procurve` | Save the configuration | Not yet confirmed |
| HP Comware | `hp_comware` | Save the configuration | Not yet confirmed |
| Juniper JunOS | `juniper_junos` | Commit, in `configure private` (a failed commit is discarded with `rollback 0`) | Not yet confirmed |
| Palo Alto PAN-OS | `paloalto_panos` | Commit (a failed commit is discarded with `revert config`) | Not yet confirmed |
| Check Point Gaia | `checkpoint_gaia` | `save config` (switches from expert mode to clish first) | Not yet confirmed |
| Fortinet FortiOS | `fortinet` | None: the change is live as typed | Not yet confirmed |

Verify covers all 12 platforms.

## Quick start

> [!TIP]
> Point your first rollout at a lab device, and leave Verify on.

### Windows

1. Download `NetRollout-Setup-<version>.exe` from the [Releases page](https://github.com/itamar14-byte/NetRollout/releases).
2. Run it. Setup checks the computer, installs Docker Desktop if it is missing, and asks for a hostname, an HTTPS port and a timezone.
3. When Setup finishes, open NetRollout in the browser and sign in as `admin` / `admin`. You set a new password at once.

NetRollout Manager (Start Menu, desktop, tray) starts, stops, updates and backs up NetRollout.

### Linux

You need Docker Engine with the compose plugin. Download `netrollout-<version>-linux.zip` from the [Releases page](https://github.com/itamar14-byte/NetRollout/releases), then:

```bash
sudo unzip netrollout-<version>-linux.zip -d /opt
cd /opt/netrollout
sudo ./bin/install.sh
```

The installer asks a few questions, starts NetRollout and prints its address. Sign in as `admin` / `admin` and set a new password. Day to day, run `sudo ./bin/netrollout.sh` for a menu.

### CLI only

Download `netrollout-cli-<version>.exe` from the [Releases page](https://github.com/itamar14-byte/NetRollout/releases) (Windows, no Python needed), or run `python -m src.cli` from a checkout.

```text
netrollout-cli-<version>.exe -d devices.csv -c commands.txt -vf
```

`devices.csv` needs the columns `ip`, `device_type`, `port`, `username`, `password` and `secret`. `commands.txt` holds one command per line. `-vf` verifies the change on every device. See the [CLI guide](docs/cli.md).

## Requirements

- **Windows:** Windows 10 or 11, 64-bit (not Windows Server), with virtualization turned on. Docker Desktop is installed by Setup if it is missing.
- **Linux:** a 64-bit (x86-64) server with Docker Engine and the Docker Compose plugin.
- **Network:** HTTPS to the NetRollout server (port 443 by default; you can choose another), and SSH from the server to your devices.
- **CLI:** Windows for the `.exe`; any OS with Python 3.12 for `python -m src.cli`.

The full list, including virtual machines and firewalls, is in the [installation guide](docs/install.md#requirements).

## Documentation

| Document | For | What it covers |
| --- | --- | --- |
| [Installation](docs/install.md) | Admins | Requirements, Windows Setup, Linux install, first sign-in, certificates, uninstall, troubleshooting |
| [Admin guide](docs/admin-guide.md) | Admins | NetRollout Manager, the `netrollout` commands, updates, backups, users and sign-in, System Settings, monitoring |
| [User guide](docs/user-guide.md) | Operators | Inventory, security profiles, variable mappings, rollouts, results, analytics |
| [CLI](docs/cli.md) | Engineers | `netrollout-cli` flags, input files, exit codes, logs |
| [Security](docs/security.md) | Reviewers | How NetRollout protects credentials, sessions and data |
| [Architecture](docs/architecture.md) | Developers | Tech stack, containers, flows, packages, object and data model |
| [Roadmap](docs/roadmap.md) | Everyone | What comes after 1.0 |
| [Contributing](CONTRIBUTING.md) | Contributors | Development setup, tests, code style, pull requests, releases |
| [Security policy](SECURITY.md) | Everyone | Reporting a vulnerability, supported versions |
| [Changelog](CHANGELOG.md) | Everyone | What changed in each release |

## Support and contributing

NetRollout is built and maintained by one person, Itamar Weinstein, on a best-effort basis. It is tested, but not every path or environment is covered. Your reports are how that gap closes.

- **Found a bug?** Open a [bug report](https://github.com/itamar14-byte/NetRollout/issues/new/choose). Include the device type and OS version, the commands, the job log (secrets are already redacted) and the NetRollout version from the page footer.
- **Ran it on a platform?** Fill in the [platform confirmation form](https://github.com/itamar14-byte/NetRollout/issues/new?template=platform_confirmation.yml), whether it worked or not.
- **Want something?** Open a feature request from the same [issue page](https://github.com/itamar14-byte/NetRollout/issues/new/choose).
- **Found a vulnerability?** Report it privately, as described in [SECURITY.md](SECURITY.md). Don't open a public issue.
- **Want to fix something?** Read [CONTRIBUTING.md](CONTRIBUTING.md). Fixes are even more welcome than reports.

## License

NetRollout is free software under the [GNU Affero General Public License v3.0](LICENSE). In short:

- You may use, study, change and share it.
- If you give a changed version to others, or let others use it over a network, you must offer them its source code under the same license.
- It comes **as is, without any warranty**, to the extent permitted by law (sections 15 to 17 of the license). You use it at your own risk, so test on a lab device first.

This summary is not the license. The [LICENSE](LICENSE) file is.
