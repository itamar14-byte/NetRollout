# NetRollout CLI

The NetRollout CLI runs one rollout from the command line. You give it a devices CSV and a commands file. It pushes the commands to every device in parallel, can verify them, and prints a summary. It uses no database, no web app and no Docker. This page is for network engineers and for anyone scripting rollouts in CI or a scheduler.

The CLI uses the same rollout engine as the web app, so a device is configured the same way from either. For the web app, see the [user guide](user-guide.md).

## Contents

- [When to use the CLI](#when-to-use-the-cli)
- [Getting it](#getting-it)
- [Usage](#usage)
- [Options](#options)
- [Interactive runs and prompts](#interactive-runs-and-prompts)
- [The devices CSV](#the-devices-csv)
- [The commands file](#the-commands-file)
- [What happens during a run](#what-happens-during-a-run)
- [Exit codes](#exit-codes)
- [Logs](#logs)
- [Examples](#examples)
- [Supported platforms](#supported-platforms)

## When to use the CLI

Use the CLI when:

- You need a one-off change and don't have NetRollout's web app installed.
- You want to run a rollout from a script, a CI pipeline or a scheduled task. The exit code tells you how it went.
- You work from a laptop or jump host that can reach the devices but can't run Docker.

Use the web app if you need an inventory, stored and encrypted credentials, variable substitution (`$$TOKEN$$`), a history of results, rollback, or several people working together.

## Getting it

There are two ways to run the CLI. Both take the same options.

| Form | What you need | How to run it |
|---|---|---|
| `netrollout-cli-<version>.exe` | Windows, nothing else. It's a single file of about 15 MB and starts in about 2 seconds. | `netrollout-cli-<version>.exe -d devices.csv -c commands.txt` |
| `python -m src.cli` | Python 3.12 and the packages in `requirements.txt`, run from the root of a NetRollout checkout | `python -m src.cli -d devices.csv -c commands.txt` |

The `.exe` is published with each release on GitHub. You can rename it, for example to `netrollout-cli.exe`. The examples below use `netrollout-cli.exe`.

> [!NOTE]
> The `.exe` isn't code-signed. Windows SmartScreen or Microsoft Defender may warn you the first time you run it. To run it anyway, choose **More info**, then **Run anyway**. You can check the file against the `SHA256SUMS` file published with the release:
>
> ```powershell
> Get-FileHash .\netrollout-cli-1.0.0.exe -Algorithm SHA256
> ```

## Usage

```text
netrollout-cli.exe [-d DEVICES] [-c COMMANDS] [-vf] [-v] [--version] [-h]
python -m src.cli  [-d DEVICES] [-c COMMANDS] [-vf] [-v] [--version] [-h]
```

## Options

| Option | Description |
|---|---|
| `-d`, `--devices` *path* | The devices CSV. If you leave it out, the CLI asks for it. |
| `-c`, `--commands` *path* | The commands file (`.txt`). If you leave it out, the CLI asks for it. |
| `-vf`, `--verify` | Verify after the push: read each device's configuration back and check every command. Off unless you give it, or answer yes when asked. |
| `-v`, `--verbose` | Print the log to the console as well. The log file is always written either way. |
| `--version` | Print the version (`NetRollout CLI <version>`) and exit. |
| `-h`, `--help` | Print the help and exit. |

## Interactive runs and prompts

A run with both `-d` and `-c` is a scripted run. It never asks anything:

- Verify is on only if you pass `-vf`.
- There is no confirmation before the push.

If either path is missing, the CLI assumes a person is at the keyboard and switches to an interactive run:

1. It asks for each missing path: `Enter device file path:` and `Enter commands file path:`. If the file doesn't exist, it says so and asks again. You can drag a file into a Windows terminal: the quotes Windows adds are removed.
2. Without `-vf`, it asks `Verify rollout? (y/n):`.
3. After reading the files, it asks for confirmation, for example: `About to push 3 commands to 12 devices. Continue? (y/n):`. Any answer other than `y` stops the run with nothing pushed.

Every run prints whether Verify is on (`Verify: on`, or `Verify: off (add -vf to check the config after the push)`).

When the run ends, the CLI waits with `Press Enter to exit...`, so a window opened by double-clicking the `.exe` stays readable. Without a terminal (a pipe, CI or a scheduler), it doesn't wait.

## The devices CSV

One row per device, with a header row. The file is read as UTF-8; a byte-order mark, such as the one Notepad writes, is fine. Column names are case-sensitive.

| Column | Required | Description |
|---|---|---|
| `ip` | Yes | The management address, IPv4 or IPv6, such as `10.0.0.1` or `2001:db8::1`. |
| `device_type` | Yes | The Netmiko device type. It must be one of the [supported platforms](#supported-platforms). |
| `port` | Yes | The SSH port, 1 to 65535. Usually `22`. |
| `username` | Yes (CLI) | The SSH username. |
| `password` | Yes (CLI) | The SSH password. |
| `secret` | No | The enable secret, if the platform needs one. Leave it empty if not. |
| `label` | No | A name for the device in the log. Defaults to the IP address. |

Any other column is ignored by the CLI.

> [!IMPORTANT]
> The CLI has no variable mappings. If a command contains a `$$TOKEN$$`, the CLI can't fill it in on any device. Each device is then skipped (`<ip:port> skipped — $$TOKEN$$: no mapping on this device`) and counted as failed. Extra attribute columns, such as `hostname`, are not substituted into commands. Use the web app for per-device values.

This is the same CSV format the web app's **Import CSV** takes. The web app doesn't require credentials, and it turns extra columns into device attributes. See [Import CSV](user-guide.md#importing-devices-from-a-csv).

Example:

```csv
ip,device_type,port,username,password,secret,label
10.0.10.1,cisco_ios,22,netops,S3cret!pass,S3cretEnable,branch-01
10.0.10.2,cisco_ios,22,netops,S3cret!pass,S3cretEnable,branch-02
2001:db8:10::1,arista_eos,22,netops,S3cret!pass,,core-01
192.0.2.10,juniper_junos,2222,netops,S3cret!pass,,edge-01
```

Each row is checked on its own. A bad row is reported and skipped, and the other rows still run. A row is rejected if:

- `ip` or `port` is missing: `Row <n>: ip and port are required`
- the IP, port or device type isn't valid: `Row <n> (<ip>): invalid ip, port or device type`
- `username` or `password` is empty: `Row <n> (<ip>): username and password are required`
- the device isn't reachable: `<ip:port> is not reachable` (see below)

> [!WARNING]
> The CSV holds passwords in plain text. Keep it somewhere only you can read, and delete it when you're done. The log never contains them (see [Logs](#logs)).

## The commands file

A UTF-8 text file whose name ends in `.txt`, with one command per line, in order. Leading and trailing spaces are removed and blank lines are skipped. Write the commands as you would type them in configuration mode. NetRollout enters configuration mode and saves or commits for you, the way each platform needs (see [Supported platforms](#supported-platforms)). Don't add `configure terminal`, `write memory` or `commit` yourself.

Example for Cisco IOS:

```text
interface GigabitEthernet0/1
description uplink to core
no shutdown
exit
ntp server 192.0.2.123
```

The same file is pushed to every device in the CSV. If your devices run different platforms, run the CLI once per platform with its own commands file. The web app can take one command set per platform in a single rollout.

## What happens during a run

1. **Read and check.** The CLI reads the CSV and checks every row (see above). It also probes each device's SSH port with a TCP connection: up to 3 attempts, 5 seconds each, a second apart. Unreachable devices are dropped before anything is pushed and reported as `<ip:port> is not reachable`. If no devices or no commands are left, the run stops with `Aborting — no devices or commands to process.` and exit code 2.
2. **Push.** Up to 10 devices are configured at the same time. On each one, NetRollout enters configuration mode, sends every command, reports any command the device refuses (and still sends the rest), then saves or commits.
3. **Verify** (with `-vf`). NetRollout reads the configuration of each device the push applied to. It looks for every command in the section where it was typed. A command must be present, and a removal (`no`, `undo`, `delete`, `unset`) must be gone. Navigation and operational commands, such as `exit`, are counted as not verifiable. Verify can mark a command as not configured when it is in fact there, for example a typed abbreviation (`int gi1`), a hashed secret, or a value the device normalises. It can also mark a command as verified because it was already there before the rollout.
4. **Summary.** Each device gets a status:

   | Status | Meaning |
   |---|---|
   | `success` | Every command was accepted. With Verify, every checkable command was also confirmed in the config. |
   | `partial` | Some commands were refused or not confirmed. Also used when the device stopped answering partway through. |
   | `failed` | Nothing took effect: no connection, a failed commit, or every configuring command refused. With Verify, none were confirmed. |
   | `cancelled` | The device was never started because the run was cancelled with Ctrl+C. |

   The last lines are, for example, `Configuration rollout complete: 10 success, 1 partial, 1 failed (of 12 devices)`, followed by the path of the log file.

**ACTION NEEDED.** Some problems need a person on the device, for example a change that is live but not saved, a commit that timed out, or a shared candidate configuration someone else left uncommitted. These are logged as `ACTION NEEDED — <ip:port>: <what to do>` and counted in the summary (`ACTION NEEDED on 2 devices (...) — see the lines marked ACTION NEEDED`). Search the log for `ACTION NEEDED`.

**Ctrl+C.** The first Ctrl+C during the push cancels the rollout. Devices not yet reached are skipped, and devices being configured are allowed to finish. The summary shows what was done, and the exit code is 130. A second Ctrl+C, or one during Verify, exits at once without waiting for the devices in flight. A Ctrl+C at a question exits with nothing pushed.

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| `0` | OK | Every device succeeded. |
| `1` | Mixed | At least one device succeeded or was partial, and at least one wasn't a success. |
| `2` | Nothing applied | No device succeeded or was partial. Also used when the run stopped before the push: a missing file, no valid devices or commands left, a "no" at the confirmation, or the input ending at a question. |
| `130` | Interrupted | Ctrl+C. |

In a script, treat anything other than `0` as needing a look at the log.

## Logs

Every run writes a log file, whether or not you pass `-v`:

- `netrollout-cli.exe`: in a `logs` folder next to the `.exe`.
- `python -m src.cli`: in the `logs` folder at the root of the checkout. If `NETROLLOUT_HOME` is set, it's in `$NETROLLOUT_HOME/logs` instead.

The file is named `cli_rollout_<YYYYMMDD>_<HHMMSS>.log`. Each run also deletes `.log` files in that folder that haven't changed for 60 days.

Logs are safe to share. Any value that follows a word such as `password`, `secret`, `community`, `key-string` or `psksecret` is written as `<redacted>`, in the file and on the console.

## Examples

Push a change and verify it, from a script:

```powershell
.\netrollout-cli.exe -d .\branch-devices.csv -c .\ntp.txt -vf
if ($LASTEXITCODE -ne 0) { Write-Warning "Rollout not fully successful - see the log in .\logs" }
```

The same from a checkout on Linux, with the log on the console:

```bash
python -m src.cli --devices branch-devices.csv --commands ntp.txt --verify --verbose
echo "exit code: $?"
```

Interactive: start with no options and answer the questions.

```text
> netrollout-cli.exe
Enter device file path: C:\change\devices.csv
Enter commands file path: C:\change\commands.txt
Verify rollout? (y/n): y
...
Verify: on
About to push 5 commands to 12 devices. Continue? (y/n): y
```

> [!TIP]
> Try a new command set on one lab device first: a CSV with a single row. Then run it on the rest.

## Supported platforms

The `device_type` column takes one of these 12 Netmiko device types. How NetRollout finishes the push is the same in the CLI and the web app.

| `device_type` | Platform | How the change is finished |
|---|---|---|
| `cisco_ios` | Cisco IOS | Save the configuration |
| `cisco_xe` | Cisco IOS XE | Save the configuration |
| `cisco_nxos` | Cisco NX-OS | Save the configuration |
| `cisco_xr` | Cisco IOS-XR | Commit |
| `arista_eos` | Arista EOS | Save the configuration |
| `aruba_aoscx` | Aruba AOS-CX | Save the configuration |
| `hp_procurve` | HP ProCurve | Save the configuration |
| `hp_comware` | HP Comware | Save the configuration |
| `juniper_junos` | Juniper JunOS | Commit, in `configure private` |
| `paloalto_panos` | Palo Alto PAN-OS | Commit |
| `checkpoint_gaia` | Check Point Gaia | `save config` (in clish) |
| `fortinet` | Fortinet FortiOS | Nothing to save: the change is live as entered. NetRollout closes any `config` block left open, because FortiOS discards an open block. |

Not every platform has been tested on real devices yet. The [README](../README.md) shows which have been confirmed and how to report a platform that works for you.
