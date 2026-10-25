# Changelog

All notable changes to NetRollout are recorded in this file. It is for anyone deciding whether to upgrade, and for anyone tracking what changed between releases.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and NetRollout uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a `1.0.x` release only fixes bugs and is safe to upgrade to, a `1.x.0` release adds features without breaking anything, and a new major version may need you to act. Version numbers are written the Python way ([PEP 440](https://peps.python.org/pep-0440/)), so a release candidate is `1.0.0rc1`, not `1.0.0-rc.1`. Each major version has a release name; 1.x is **Anycast**.

## [Unreleased]

### Fixed

- Signing in after a session ended on a page left open could land on `/account/session` (a page's background session check) instead of the page you were on: once signed out, that check - and any request from a page's script - now gets a 401, never the sign-in page, so it can't replace the page to return to. The page returned to also drops an automatic reload's `?_bg=1`.

## [1.0.0] - 2026-10-25

The first release. Since 1.0.0rc9, seven platforms were run through NetRollout's own engine against virtual devices in an EVE-NG lab - IOS, NX-OS, IOS-XR, Junos, PAN-OS, Check Point Gaia and FortiOS - and what that round found is fixed here. An installed release candidate upgrades from NetRollout Manager (**Upgrade**) or Linux's `netrollout.sh upgrade`, keeping everything; a before-upgrade backup is made first.

### Added

- **Test sign-in** in the device window (Add, Edit, and a global device's read-only window): with a security profile chosen, an SSH sign-in with that profile at the address, port and platform the window holds now, saved or not - "Connected successfully", "Authentication failed", or what to fill in first.
- **View Log** in Results: a job's rollout log in a window on the page, with a filter box (a device's address, a command, a word), next to Download Log - the same rule: its owner, or any administrator.
- **Progress in the log** (shown with Verbose): during a push, every 5 seconds "pushing - 340 of 2500 sent"; a commit says "committing - the device applies the change now", then "still committing (25 s)" every 15 seconds - a PAN-OS commit often takes a minute or more, even for one line.
- A rollout that waited for a free slot starts its log with how long and why ("Waited 53 s for a free slot - rollout jobs run a few at a time (System Settings: Concurrent rollout jobs)").
- IOS-XR: a failed commit's log line ends with what `show configuration failed` shows - the reason, which IOS-XR's commit reply doesn't give.
- Results: filter by job ID (`/results?id=<job>`).
- The log says when a device's commands were pushed ("pushed N commands"), before the save, commit and verdicts.

### Changed

- **Commit platforms** (Junos, IOS-XR, PAN-OS): a candidate with nothing in it (every command refused, or none changed anything) isn't committed - the log says so. PAN-OS commits by sending `commit` and reading the candidate after it: a failed commit is known within seconds and reverted, where it waited about 7 minutes for a progress bar that never came.
- **The CLI's exit code** is 0 only when every device in the file succeeded: a device dropped before the push (unreachable, or an invalid row) makes the run mixed (1). It exited 0 when the others succeeded.
- The Verification window says "Unchanged: the push added and removed nothing" when a verified push changed nothing, instead of leaving the before → after section out; a changed line that isn't configuration (FortiOS's `#conf_file_ver`, a block's closing `next`) is marked "not configuration".
- Messages built around a Netmiko error keep only its reason - never its paragraph of advice for developers ("Things you might try…"): a refused `configure private`, a configuration that couldn't be read, the defaults view, the save check, a failed commit (the device's own words, without the echo and prompt).
- A connection the device closes before SSH starts says why ("couldn't connect: Error reading SSH protocol banner…"); "timed out" only when it timed out.
- New Rollout's **Load from file…** puts a file's commands in the box, to read and edit before launching.
- Active Jobs: a just-launched rollout's log opens on arrival; a log opened after the job ended shows its log file; a running job's row has no Rollback; the cancel question says what cancelling does ("Keep it running"); the local time clock is 24-hour in every locale.
- The Grafana link opens the dashboards list; Inventory's Test Connection is in a global device's read-only window too; the rollback's keep-my-profile toggle reads as one sentence and names its devices.
- The README marks seven platforms as tested in a lab; the user guide says which were verified and how long commit platforms take.
- The sign-in page's highlights: 12 platforms, Verified on the device, One-click rollback, Full audit trail - on one line (two on a phone), with the app's own icons.

### Fixed

- **Active Jobs' Cancel did nothing** (since 1.0.0rc4): the button's handler couldn't be reached from the page. A test now checks that every inline handler on every page can be.
- Junos: a refused command no longer stops the push (Junos echoes it in pieces); the commands after it are sent and judged.
- Device messages (Auto Image Upgrade, syslog, a broadcast) redrawing the prompt no longer cut a configuration read short or move a reply to the next command; an empty configuration read means "not verified", never a configuration with nothing in it; a connection that times out while connecting is tried once more.
- Check Point Gaia: `delete host name X ipv4` is judged **removed** - the stored line says `ipv4-address`, which hid it - not "already absent".
- A value a command replaced ("snmp-server location a" → "… b") is no longer given as another command's evidence (a typo next to it was "rejected" with the old value's removal as its evidence).
- A failed enable says so, on one line; errors are logged as their first line.
- A progress bar a device redraws while it works (NX-OS's `copy running-config startup-config`) is no longer logged frame by frame, nor taken for a reply: the log line keeps what the device said ("Copy complete.").
- Grafana's panels count what their titles say, by Results' rules: job status, Devices Today, the breakdown by job, the average job duration.
- The CLI no longer prints Paramiko's traceback when a connection fails.
- Empty pages: the button under the big icon is a normal button again (it was stretched into a square), the icons are visible, and New Rollout's empty device list offers **Add devices** as a button.
- System Settings: a message shown off screen scrolls into view. Verify and Verbose's icons are cyan only while switched on.
- Pillow 13.0.0 (from 12.3.0).

## [1.0.0rc9] - 2026-10-22

### Changed

- **Verify judges each device by its own configuration, before and after the push.** NetRollout reads the configuration before the commands and again after the save or commit, in the same session, and compares the device with itself. A command is **took effect** (its words are in a line the push added, however the device abbreviates or numbers it), **already in place**, or **removed** (also by the configuration shown with its defaults, read only for what's left unproven, and by a removed negation) - **rejected** when the configuration doesn't show it and the device replied (to it, or when its block closed), or, when nothing proves it either way, **review**, decided by a person with the evidence. That the device replied counts, never what it said: no list of error messages decides anything, and a quiet device proves nothing. Exec commands (`do show …`, FortiOS `get …`) are shown, not judged. Every platform declares its exec words, its defaults view (or none) and the shape of its log lines. Examples found in the EVE lab, now handled: NX-OS stores `ip address … 255.255.255.255` as `/32` (review with that line as evidence, no longer a false `partial`); IOS refusing `ntp server` and FortiOS answering a typo with `Unknown action 0` (review with the device's reply, no longer a success without Verify); FortiOS `edit 0` becoming `edit 1`; NX-OS ACL entries gaining sequence numbers; a rerun (already in place, no longer "verified" by accident). FortiOS re-encrypts every stored password on each read: that churn is learned on the spot and left out.
- Results shows what a device that wasn't judged replied: a dashed **replied N** note opens it, command by command (stored redacted, `device_results.replies`; the export's `replied` column counts them).
- Devices of a job awaiting the same decision (the same commands, verdicts and replies) can be decided together from the Verification window - each keeps its own record and audit row.
- A device with a rejected command is **partial** when another was proven, else **failed**; a person may confirm it (a command whose effect isn't kept in the configuration, such as `crypto key generate`, is rejected too). Results and the dashboard count these with the reviews as devices to decide. A rollback includes partial devices.
- New device statuses: **review** (a person decides) and **not verified** (the commands were sent; Verify was off, or the configuration couldn't be read). Without Verify a device is `not verified`, no longer `success`.
- **Results → Verification** (was Verify Diff) shows, per device, each command as sent with its verdict, the line it rests on and the device's reply, the changes no command explains, and the configuration before and after. A device in review is confirmed (→ `success`, included in a rollback) or rejected (→ `failed`) there, with an optional comment, kept with the result and in the audit log. A job shows **N to review**; the dashboard and the finished-rollout card say how many devices wait.
- Every reply a device gives is kept in the log, with its command. A command in review is printed on the CLI's console even without `-v`, and the summary names the devices to review and says why any are not verified.
- On platforms that save, NetRollout checks that the lines the push added are in the saved configuration; Check Point's save state is read (`show config-state`). After a failed commit the discard commands are sent, then the device is asked what is still in the candidate (Junos `show | compare`, PAN-OS `show config diff`): nothing left needs nobody, anything left is an ACTION NEEDED showing it.
- A commit that doesn't answer within 5 minutes is decided by the configuration (with Verify): read again from new sessions for up to 2 minutes, changed - judged as usual, unchanged - failed with an ACTION NEEDED. It was always reported failed before.
- A command that gets no prompt back (a question such as `[confirm]`) keeps what the device printed: in the log and in the ACTION NEEDED.
- Without Verify, what a device replied to a command is shown on the CLI's console even without `-v`, and the summary names the devices that replied, so a refused command isn't overlooked. The reply is never judged: the device stays `not verified`.
- Verification's before → after diff tags each changed line with the command it rests on, or "not by these commands"; the device's own changes (FortiOS's re-encrypted passwords) are behind **Show the device's own changes**.
- The CLI's exit code: `review` counts as mixed (1); `not verified` is OK (0) only without `-vf`.
- Rollback starts with **Verify after push** on, as New Rollout does, so an undo is proven like any change; its options are the same switches as New Rollout's. The Verify help on both says what Verify does now.
- Signed in, the sign-in page and Request access send you on to the Dashboard (or the page `?next=` names).
- Results' CSV export adds the review decision (`review`, `reviewed_by`, `reviewed_at`, `review_comment`); Analytics and Grafana's Job details know the two new statuses.

- Inventory and New Rollout: a device's vendor logo is in its brand colors while the device is reachable, and grey while it's being checked or when it isn't, so the devices you can roll out to stand out at a glance. New Rollout showed every logo in white before.

### Fixed

- Each platform is one class holding everything about it - its grammar and how a session is brought to speak it (shell, privilege, modes, output format, the finish). PAN-OS reads its configuration as set lines in configure mode, the documented way (set format first); IOS-XR switches off the time stamp above every show; HP Comware stays in user view until configuration (Netmiko's "enable" for it is system-view). Each platform's own navigation and removal words reach the judge.
- Check Point Gaia: NetRollout no longer asks Netmiko for enable mode, which on Gaia is the expert shell (bash, where every `set` is bash's own) - in the push's session and any new one.
- HP Comware: its bracketed prompts (`[HP-GigabitEthernet1/0/1]`, `<HP>`) and the echo after them are no longer taken for the device's reply.
- Junos and PAN-OS: a command after `top` or `up` on the same line (`top set system host-name x`) is judged instead of passing as navigation; after a plain `up` the path counts as unknown.
- IOS-XR's time stamp above every show's output, and classic IOS's drifting `ntp clock-period`, no longer count as configuration changes.
- More log-line shapes are told apart from replies: IOS's `.`-stamped and sequence-numbered lines, Comware's trap and boot stamps, IOS-XR with a year, `Message from syslogd@…` and `Broadcast message from …`.
- Each platform's exec commands completed from its documentation (ProCurve `ping` / `copy` / `reload`, PAN-OS `check` / `validate`, Junos `status` / `save`, FortiOS `tree` / `sudo`, …); `write` dropped for IOS-XR, which has no such command. IOS-XR's `end-group` closes a block. Gaia's save state is read from the last line of `show config-state`. FortiOS `config global` has no defaults path.
- New Rollout shows the Arista, Aruba and Check Point logos; it showed a generic icon for them.
- Windows: NetRollout comes back after a restart even when Docker Desktop's own "Start when you sign in" setting is off. At sign-in, NetRollout's port helper starts Docker Desktop if it isn't running half a minute later; the containers follow Docker. A Docker Desktop you quit later stays quit.

## [1.0.0rc8] - 2026-10-19

### Changed

- Inventory: the tooltip shows a name saved without a domain the way NetRollout reaches it, with System Settings' **Device DNS suffix**: `core1` reads `core1.corp.local (10.0.0.7)`.
- Inventory's filters stay set after a save or a delete (the page reloads); **Clear filters** clears them.
- Inventory's platform filter takes several platforms: tick any number to show the devices of all of them.
- **Test Connection** to `localhost`, `127.x` or `::1` explains that this is NetRollout's own server (in Docker, its container), not your computer, instead of testing it.
- The windows that assign devices (a security profile's devices, a mapping's) have a search box above both columns. It matches name, address or platform as you type and stays set while you move devices across.

### Fixed

- Devices whose SSH offers only an `ssh-rsa` host key, such as NX-OS 7 and IOS 12 or 15, can be reached again. NetRollout's SSH library is pinned to Paramiko 4.x, which still accepts it (and the SHA-1 key exchanges) after every modern algorithm; Paramiko 5 removed them. The low-severity advisory this brings (CVE-2026-44405) and why it's accepted are in the security notes' Known limitations.

## [1.0.0rc7] - 2026-10-19

### Changed

- NetRollout Manager's **Upgrade** window shows the release notes formatted, with headings, bullets and bold, instead of their markdown (`##`, `**`). The download links at their end, meant for the release page, are left out.
- A window closes the way it opens: it slides up while it fades out, instead of vanishing at once.
- Inventory: hovering a device saved by hostname shows its address too, `core1.corp.local (10.0.0.7)`. A long name wraps instead of being cut off.
- The reachability check looks hostnames up, at most once a minute per device (its cache), and keeps each one's address. That address stays current for the tooltip and for the "already used by" warning, the way FortiGate keeps FQDN address objects. A rollout uses the same check to refuse a name and its IP together, so it no longer does a lookup of its own.
- A device whose name doesn't resolve shows **Not reachable (can't resolve)** on its red dot, in its tooltip and in New Rollout.

## [1.0.0rc6] - 2026-10-18

### Changed

- Upgrades no longer read rc4's names: from rc5 on, the backup before an upgrade is always `before-upgrade` and `.env` says `# Upgraded to NetRollout X`. Older `before-update` backups still show in the list and still restore.
- **Enter** in a window does its main step, as Escape closes it: Create New Profile and the quick-create mapping create, Add Device tests the connection and then confirms, Edit property saves, and the same in the other windows. A red, destructive button still takes a click, and a risky question still starts on Cancel.
- Server Management: an open card (PostgreSQL, Redis, an LDAP server) closes on Escape, on a click outside it, or on its own button again. During a database move, PostgreSQL's stays open.

### Fixed

- Add / Edit Device: the result of **Test Connection** was cut off next to the buttons ("TCP port 22 unreachable on 19"), and **Save Anyway** broke over two lines. The result now has its own line above the buttons.

## [1.0.0rc5] - 2026-10-18

### Added

- A device's address can be a **hostname** as well as an IP address: in the Inventory forms, the CSV import and the CLI's devices CSV (the column is still `ip`). A name alone, such as `core1`, gets the new **Device DNS suffix** in System Settings (`core1.corp.local`); the CLI uses the computer's own search domains. Names are looked up in the DNS the NetRollout server uses. Saving a name looks it up: one that doesn't resolve is saved with a warning, and the "already used by" warning also catches a name and an IP (or two names) of the same device. Every rollout looks its devices' names up again: a name and its IP are refused together in one rollout, and the address kept for each name follows DNS changes. A rollout or Test connection to a name that doesn't resolve says "can't resolve" instead of a timeout.

### Changed

- No em dashes anywhere: in the pages, messages, log lines, scripts, NetRollout Manager, Setup and the documentation, a spaced hyphen instead.
- Moving to a newer version is now called an **upgrade** everywhere, the way network appliances call a new firmware or OS version: NetRollout Manager's **Upgrade** and **Check for upgrades**, Setup's pages, `netrollout.sh upgrade` and `manage.ps1 upgrade` (the old `update` names are gone), `logs\upgrade.log`, the registry value `UpgradeFeed`, and the backup made first, now `…-before-upgrade.zip`. Backups made before this release keep their `before-update` names and restore as before.
- The LDAP server form's **Bind DN** says it's the service account, and that Active Directory also takes `svc@corp.local` or `CORP\svc` (never the bare name).

### Fixed

- A window opened over another one was dimmed and took no click, only Escape: **Create New Profile** (and the quick-create mapping) from Edit device froze the page. After a question had been asked over another window, the next question on that page froze the same way. Every window opened over another now goes above it.
- On a phone, the page's title had no room next to the Operations | Administration switch, the administration pages hid it, and the NO DIRECTORY badge broke over two lines. The top bar now gives the title the room (the logo stays in the menu, and the user menu shows a person icon), and badges never break.
- NetRollout Manager's **Logs** (and other script output it shows) garbled every character that isn't plain English, such as a dash in the app's log lines or a device label in another language: `manage.ps1` now writes UTF-8 and the Manager reads it so.

## [1.0.0rc4] - 2026-10-18

### Added

- **Reconnect** in User Management: accounts kept when their LDAP server was deleted (now marked NO DIRECTORY) can be linked to another server. Every name must exist there, checked with its service account, or nothing changes; they take the directory's email and name, keep their data, and stay disabled until an administrator enables them. The LDAP explorer's import can do the same ("Reconnect existing accounts with these names"). Until then, Enable, Approve and Promote are refused on them: before, they could be enabled but never sign in.
- Inventory filters: by name and IP address as you type, and by platform from a list of the platforms you have. Sections with no match hide, and the page says how many devices it shows.
- Unticking Global on a global device warns that the other users lose it, with their security profile, attribute values and variable mappings on it, and Save becomes a red Confirm.
- Results filters: by note and by device name or IP as you type, by status, by the days a job started on, and in an administrator's All Users view by user. They cover every job, not only the page shown.
- **Export CSV** on Results and the Audit Log: every job (one row per device) or entry the filters let through. Results exports your own jobs, or everyone's in an administrator's All Users view; it leaves out the commands and config snapshots. Cells a spreadsheet would run as a formula are quoted, and Excel reads the file as UTF-8. Each export is recorded in the audit log.
- The Audit Log filters by day (From / To).
- Every password field can show what's typed (the eye): also the LDAP bind and test-user passwords, the database move's and Redis' passwords, a quick-created security profile's, and turning 2FA off.
- The Audit Log's entry for making a global device private says what the other users lost on it: how many users, and their security profile links, attribute values and variable mappings.

### Changed

- The sign-in page's stop after 5 failed sign-ins shows the time left as mm:ss and counts it down ("Try again in 14:59."), then says "You can try again now." It was in whole minutes, rounded up.
- User Management: the Locked badge's tooltip counts the lock down in mm:ss, and the badge goes when the lock ends. The button that lifts a lock reads **Enable / Unlock**: just **Unlock** when every user ticked is only locked, just **Enable** when every one is disabled.
- Administrators move between the day-to-day pages and the administration pages with one switch at the top right of every page, **Operations | Administration**. Each side returns to the last page open there, and the administration pages have a violet line under the top bar. It replaces the Admin Panel gear in the sidebar, the Admin Panel item in the user menu and the admin pages' **← Home**. **← Home** stays only on My Account and the sign-in pages, and the gear now means only System Settings.
- Every question and message is in NetRollout's own window, never the browser's: restarting, cancelling a job or rollout, deleting a backup, a group rule or a property, ending sessions, a self-signed certificate, no longer trusting a CA, moving the database, switching Redis back, and making an LDAP group rule Admin. A risky one is red and starts on Cancel. A browser can switch its own pop-ups off for a page, after which they answer No without asking, so those actions silently did nothing. New Rollout says missing commands in the platform's own box instead of a pop-up.
- A user signed out by an administrator (Terminate Session, a password reset, being disabled) leaves the page within 5 seconds instead of 30, and at once when they switch back to its tab.
- The services' console logs (`netrollout logs`) are capped: each rolls over at 10 MB and keeps 3 files, about 30 MB a service. Before, Docker kept them forever, and nginx's grew with every request. NetRollout's own logs (`logs`) are unchanged.

### Fixed

- On a phone, pages showed a shrunken desktop page (they declared no viewport). Now they fit the screen: the sidebar slides in from a menu button in the top bar, page headers wrap, Inventory's device cards and Results' jobs fit on two lines, Active Jobs' figures go two by two, and wide tables scroll inside their card.
- A device's Edit window opened with "Enter a valid IPv4 or IPv6 address." under a valid address. Saving worked; the field is now checked when the window fills it in.
- The Add Device window called the security profile "optional". It's needed to roll out to the device (you can save without one and link it later), and the label now says so.
- Deleting a security profile: the Delete window said the devices using it had to be reassigned first. Deleting has always gone ahead and unlinked them; the window now says how many devices it's linked to and that they'll need another profile.
- "A security profile of yours" now reads "your security profile" everywhere (Inventory, New Rollout, the rollback window, Security Profiles and the messages).

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

[Unreleased]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc9...v1.0.0
[1.0.0rc9]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc8...v1.0.0rc9
[1.0.0rc8]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc7...v1.0.0rc8
[1.0.0rc7]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc6...v1.0.0rc7
[1.0.0rc6]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc5...v1.0.0rc6
[1.0.0rc5]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc4...v1.0.0rc5
[1.0.0rc4]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc3...v1.0.0rc4
[1.0.0rc3]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc2...v1.0.0rc3
[1.0.0rc2]: https://github.com/itamar14-byte/NetRollout/compare/v1.0.0rc1...v1.0.0rc2
[1.0.0rc1]: https://github.com/itamar14-byte/NetRollout/releases/tag/v1.0.0rc1
