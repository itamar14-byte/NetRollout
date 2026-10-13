# Changelog

All notable changes to NetRollout are recorded in this file. It is for anyone deciding whether to update, and for anyone tracking what changed between releases.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and NetRollout uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a `1.0.x` release only fixes bugs and is safe to update to, a `1.x.0` release adds features without breaking anything, and a new major version may need you to act. Version numbers are written the Python way ([PEP 440](https://peps.python.org/pep-0440/)), so a release candidate is `1.0.0rc1`, not `1.0.0-rc.1`. Each major version has a release name; 1.x is **Anycast**.

## [Unreleased]

### Fixed

- A device's Edit window opened with "Enter a valid IPv4 or IPv6 address." under a valid address. Saving worked; the field is now checked when the window fills it in.
- The Add Device window called the security profile "optional". It's needed to roll out to the device (you can save without one and link it later), and the label now says so.

## [1.0.0rc3] - 2026-10-13

### How to update from 1.0.0rc2

- **Windows:** NetRollout Manager → **Update**. It finds this release candidate by itself, downloads Setup, checks it against the release's checksums and updates the install in place, with a backup first. Or download `NetRollout-Setup-1.0.0rc3.exe` from the [Releases page](https://github.com/itamar14-byte/NetRollout/releases) and run it.
- **Linux:** run `sudo ./bin/netrollout.sh update` in the install folder.

After the update every device keeps the security profile it has today, as its owner's link. Anyone else who rolls out to a global device links one of their own profiles to it first: the device shows **ACTION NEEDED** until they do.

### Security

- **Personal credentials by design.** NetRollout now logs in to a device only with the security profile of the person who launches the rollout, never anyone else's. A global device is shared, but each user links one of their own profiles to it (**Your security profile** on the device, or Security Profiles); before, it used its creator's credentials for everyone. A change on a device is attributable to the person in the device's own AAA logs, and each team works within its own rights. The database itself refuses a link to another user's profile. Updating keeps every device on the profile it has today, as its owner's link.
- A rollback logs in with the credentials of the person starting it, no longer the job owner's. For devices without a profile of theirs, the rollback window asks them to pick one of their own.

### Added

- **ACTION NEEDED** on any device without a security profile of yours, local or global, and a line on Inventory saying how many devices need one, with **Show them**. A rollout to such devices is refused before it starts, naming them.
- Deleting an LDAP server lists the users who sign in through it and offers **Also delete these users**; otherwise they're kept but disabled. Refused when it would leave no administrator who can sign in, or when you sign in through that server yourself. Audited with the users deleted or disabled.

### Changed

- Deleting a user keeps the global devices they created, for everyone; only what is theirs alone goes (private devices, profiles, mappings, properties, results). Before, their global devices were deleted with them.
- A security profile can be deleted while devices use it: those devices then show ACTION NEEDED until another profile of yours is linked.
- The Audit Log shows each entry's detail in one line under its action (a failed sign-in's reason, a change's from and to, a lock), and a click on an entry (or Enter) opens its full detail.
- Saving an LDAP server is audited with what changed, from and to (a new server with its settings); the bind password only as "changed", never its value. These settings decide where directory users' passwords go at sign-in.
- Trusted CA certificates refuse a server's own certificate issued by a CA, a common mistake that can't work, and name the CA to upload instead.
- After 5 failed sign-ins in a row with the same username, the sign-in page now says "Too many failed sign-ins. Try again in N minutes." and stops that username for 15 minutes, instead of answering "Invalid credentials" throughout. It works the same for every username, an account or not (a made-up name gets the same message at the same moment), so it still doesn't reveal which usernames exist. Directory accounts get the same stop on the sign-in page. A local account's own lock and the disable after 10 wrong passwords are unchanged; an admin's **Enable**, a right password and `netrollout reset-admin` clear the stop too. User Management's **Locked** badge says how many minutes of the lock are left.
- When GitHub refuses an update check because its limit for requests without a sign-in is used up (60 an hour per internet address, shared by everything on that network), NetRollout Manager and the Linux `update` say so and when it resets, instead of only "answered 403".

### Fixed

- The Audit Log's **View Detail** showed no detail for any entry: the details (a failed sign-in's reason, a role change's from and to, ...) were stored but didn't reach the page whole.
- A restore took the backup's hostname, so a backup from another server (or one with no hostname set) changed this installation's address, and Setup's **Open NetRollout in the browser** then opened `https://` with no name. A restore now keeps this installation's hostname, as it already kept its HTTPS port, and puts the backup's certificate back only when it serves that hostname. Setup falls back to this computer's name when no hostname is set.

## [1.0.0rc2] - 2026-10-12

### How to update from 1.0.0rc1

The 1.0.0rc1 updater only looks for final releases, so it doesn't see this release candidate.

- **Windows:** download `NetRollout-Setup-1.0.0rc2.exe` from the [Releases page](https://github.com/itamar14-byte/NetRollout/releases) and run it. It updates the install in place, with a backup first.
- **Linux:** run `sudo ./bin/netrollout.sh update --version 1.0.0rc2` in the install folder.

From this release on, an installed release candidate also finds newer release candidates by itself.

### Added

- The dashboard's running-rollout card has **Watch live**: it opens Active Jobs with that rollout's live log already open.
- `netrollout port <N>` (Windows) and `netrollout.sh port <N>` (Linux) move NetRollout to another HTTPS port when its current port is taken by another program, so System Settings can't be reached. The port is checked before you are asked, and the change is audited. NetRollout Manager offers the same as **Change port...** while the port is taken.
- The HTTPS port is checked at every start: when another program or container holds it, `start`, `status` and NetRollout Manager name the holder (also inside a published port range) and say what to do.
- `manage.ps1 port-check` reports whether nginx holds its HTTPS port, and who does if not.
- An update that didn't finish is remembered (`.update-pending`) and finished, never skipped: by running the same Setup again (**Finish update**), NetRollout Manager's **Finish update**, or `start`.
- Setup recovers a damaged install (folder partly deleted by hand): it asks to clear what's left, optionally keeping the data, and installs again.
- Setup finds NetRollout's data left in Docker when its folder is unknown, and asks whether you still have that folder, instead of assuming it is gone.
- **Start fresh instead** on Setup's folder page, and `install.sh --start-fresh` on Linux, delete kept data before a fresh install. Backups are kept.
- Linux records where an uninstall kept the data (`/etc/netrollout/kept-data`), as Windows does.
- Account lockout: 5 wrong passwords in a row lock a local account for 15 minutes; once the lock has run out, 5 more disable it until an admin enables it. The factory `admin` is only ever locked, never disabled. The sign-in page answers "Invalid credentials" throughout (only `admin`'s lock is announced), so it doesn't reveal which usernames exist. User Management marks a locked account, and **Enable** lifts the lock. Audited as `auth.account_lock_timed` and `auth.account_locked`.
- LDAP group rules can give the **Admin** role: choose **Operator** or **Admin** per rule under User Management -> Group Rules. Where a server has a service account and an active rule, a directory user's role is decided at every sign-in from the rules they match (admin if any gives admin), the import applies the same rule, and Promote / Demote are refused for them. A demotion that would leave no administrator is not made. Role changes are audited as `user.role_from_directory` and `admin.ldap_group_role`.
- The factory `admin` can turn two-factor sign-in on for itself on **My Account**, and off again with its password and a code. It stays off by default.
- Admins can disable the factory `admin` in User Management. Disable, Demote and Delete are refused when no administrator who can sign in would be left.
- `netrollout reset-admin` (Windows) and `netrollout.sh reset-admin` (Linux) make the factory `admin` usable again from the server when no administrator can sign in: enabled, 2FA off, failed sign-ins and lock cleared, signed out everywhere, and optionally a temporary password.
- Server Management → TLS Certificate → **Trusted CA certificates**: add or remove the CA certificates NetRollout trusts for LDAPS, on top of the operating system's. Backups include them.
- Setup shows the current step live under the progress bar, and during an update's preparation.
- A page that doesn't exist answers like a router console (`% Invalid input detected at '^' marker.`) with a link back, instead of a bare "Not Found".
- Empty Inventory and Results pages say so and offer the way in.
- The page footer shows the release name (Anycast).

### Changed

- The Linux release zip's `README.md` is now a short guide to the install folder (install, the main commands, links to the documentation on GitHub) instead of the repository's README, whose links didn't work inside an install.
- **Verify after push** starts ticked on New Rollout. You can still untick it per rollout. The CLI keeps `-vf` opt-in.
- The CLI refuses a commands file that uses variables (`$$TOKEN$$`) before it checks or connects to any device, with exit code 2 and a message saying why. Before, it skipped every device and counted each as failed.
- NetRollout Manager and the Linux `update` follow the installed version's channel: a release candidate looks at pre-releases too, a final release only at final releases. A mirror feed (`UpdateFeed`, `--feed`) may now be a list of releases.
- Kept data decides the install folder: Setup's folder page is locked to the folder holding kept data, and Linux refuses to install in another folder while data is kept, so a new install can't land on an old database with new passwords.
- The uninstall questions are both "Delete ... too?", with Yes and No spelled out and No the default.
- `start` and `stop` say when there is nothing to do. NetRollout Manager greys out Start and Stop when they don't apply and says why on hover.
- A failed update says how to finish it later: run Setup again, or **Finish update** in NetRollout Manager.
- Setup's finished page uses its own check boxes, which follow Windows' light and dark mode.
- Password fields show eye and eye-slash icons instead of emoji.
- System Settings labels an HTTPS port set outside the page as "set on the server".
- Development: the development stack publishes ports 8443 and 8081, so it can run beside an installed NetRollout.

### Fixed

- Setup reported Docker as missing when it was started with a `PATH` that lacked Windows PowerShell's folder. It now runs PowerShell by its full path.
- `netrollout status` on Windows failed with "expected one argument" when no container existed.
- After a failed `netrollout port <N>`, the message said NetRollout was back on its previous port even when that port was still taken.
- NetRollout Manager kept showing a taken port after Docker stopped answering.
- The password eye on the sign-in page could toggle the wrong element when a browser extension added one next to the field.
- NetRollout Manager's **Check for updates** and the Linux `update` failed with a 404 while only pre-releases were published.
- Docker volumes created outside compose (with NetRollout's project name but no label) were missed by the kept-data checks.
- Setup refused a damaged install's kept data of a newer version as "data kept by an uninstall". It now says the folder holds data of that newer version, and which Setup to run.
- The Linux `update --check` and the update's confirmation didn't show the release notes. They now show the first 30 lines and a link to the full notes on the release's page.

### Security

- LDAPS connections now verify the directory server's certificate and host name, always. Before, any certificate was accepted. A directory whose certificate comes from your organisation's own CA needs that CA under **Trusted CA certificates** first; the LDAP **Test** button says why a certificate is refused.
- Wrong passwords are counted per account in the database, on top of the per-address rate limit (see Added: account lockout).
- A directory account whose LDAP server is deactivated, removed or unreachable now gets "Invalid credentials", like a name with no account. Before, "LDAP auth service unavailable" told anyone that the username existed. The audit log keeps the real reason.

## [1.0.0rc1] - 2026-10-10

Anycast. The first public release.

### Added

**Rollouts**

- Push configuration commands to many devices in parallel over SSH (Netmiko), with limits for concurrent jobs and devices per job.
- 12 platforms: Cisco IOS, IOS XE, NX-OS and IOS XR; Arista EOS; Aruba AOS-CX; HPE ProCurve and Comware; Juniper Junos; Palo Alto PAN-OS; Check Point Gaia; Fortinet FortiOS. Each saves or commits the change its own way, and a failed commit is discarded where the platform needs it.
- Verify: after the push, each device's configuration is read and every command checked, including removals (`no`, `undo`, `delete`, `unset`), on all 12 platforms.
- Variable mappings: a `$$TOKEN$$` in the commands is filled in per device from a device property.
- Live rollout log in the browser (Server-Sent Events), Active Jobs, and cancelling queued or running jobs.
- Results: each device's status, log, configuration snapshot and Verify Diff; `ACTION NEEDED` for cases only a person can resolve; rollback of a finished job; log download.
- Secrets in job logs (`password`, `secret`, `community` and similar) are replaced with `<redacted>`, so a log can be shared in a bug report.

**Inventory and accounts**

- Device inventory with labels, system and personal attributes, and CSV import (IPv4 and IPv6).
- Security profiles: device credentials stored encrypted, reused across devices.
- Operator and admin roles; local accounts with access requests and admin approval; LDAP sign-in with group mapping; TOTP two-factor authentication.
- A password rule, forced password change for the factory admin and after a reset, idle and absolute session limits, and signing out everywhere on a reset.
- Audit log of sign-ins, rollouts, settings and admin actions.
- Analytics for each user's rollouts, and across all users for admins.

**Administration**

- System Settings: retention, concurrency, the reachability cache, sessions, backups, the hostname and the HTTPS port, applied without a restart where possible.
- TLS: a self-signed certificate by default, or an organisation's certificate uploaded and checked against the hostname.
- Backups: scheduled (daily by default) and on demand, with download; restore from the command line onto the same or a new install.
- Moving the database to your own PostgreSQL server (with a copy of the data) and switching Redis, from Server Management.
- Monitoring (optional): Prometheus, Loki, Alloy and Grafana dashboards, for signed-in admins only.
- Stop and restart drain running rollouts first; a maintenance mode for the database move.
- A nightly clean-up of old results, snapshots and audit entries.

**Install and update**

- Windows: NetRollout Setup (installs Docker Desktop when it is missing) and NetRollout Manager (status, start, stop, update, backup, restore, from the tray).
- Linux: `install.sh` and `netrollout.sh`, with an interactive menu and unattended flags.
- Updates with a backup first and the download checked against `SHA256SUMS`; mirror feeds and offline updates (`--from`) on Linux.
- An HTTPS port saved in System Settings is applied by a port helper, tried on the new port and rolled back if no browser confirms it.

**CLI**

- `netrollout-cli` for one-off rollouts from a devices CSV and a commands file, with optional Verify. A standalone Windows `.exe`; no database or Docker needed.

[Unreleased]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc3...HEAD
[1.0.0rc3]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc2...v1.0.0rc3
[1.0.0rc2]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc1...v1.0.0rc2
[1.0.0rc1]: https://github.com/itamar14-byte/NetRollout/releases/tag/v1.0.0rc1
