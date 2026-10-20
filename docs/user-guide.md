# NetRollout user guide

This guide is for the network engineers who use NetRollout's web app day to day. It covers signing in, building your inventory, storing credentials, per-device variables, running a rollout, and reading the results. Administrators have their own [admin guide](admin-guide.md). For headless one-off rollouts without the web app, see the [CLI guide](cli.md).

## Contents

- [How it fits together](#how-it-fits-together)
- [Signing in](#signing-in)
- [Finding your way around](#finding-your-way-around)
- [The dashboard](#the-dashboard)
- [Inventory](#inventory)
- [Importing devices from a CSV](#importing-devices-from-a-csv)
- [Security profiles](#security-profiles)
- [Properties and variable mappings](#properties-and-variable-mappings)
- [Running a rollout](#running-a-rollout)
- [Active Jobs and the live log](#active-jobs-and-the-live-log)
- [Results](#results)
- [Rolling back](#rolling-back)
- [Analytics](#analytics)
- [Your account](#your-account)
- [Supported platforms](#supported-platforms)

## How it fits together

A rollout is built from a few blocks. Each one is a page in the sidebar, and each is explained in depth below.

```mermaid
flowchart LR
    profile["Security profile<br/>(username, password,<br/>enable secret)"] -->|"assigned to"| device["Device<br/>(Inventory: address, port,<br/>platform, label)"]
    property["Property<br/>(system or custom)"] -->|"has a value on each device:<br/>an attribute"| device
    mapping["Variable mapping<br/>token → property"] -->|"reads"| property
    mapping -->|"assigned to"| device
    commands["Commands<br/>with tokens"] --> rollout
    device -->|"ticked in"| rollout["Rollout<br/>(one push per device)"]
    rollout --> results["Results, logs,<br/>Verify, rollback"]
```

1. **A device** (Inventory) is a target: an address (an IP or a hostname), SSH port, platform and a label.
2. **A security profile** holds *your* credentials for logging in to devices. One profile serves many devices. NetRollout always logs in with the profile of the person who rolls out, never someone else's, so you can't roll out to a device until it has your profile.
3. **A property** is a kind of per-device value, such as `hostname` or `loopback_ip`. Its value on one device is an **attribute** of that device.
4. **A variable mapping** binds a token, such as `$$LOOPBACK$$`, to a property, and is assigned to the devices it applies to.
5. **A rollout** sends your commands to the devices you tick. On each device, every `$$TOKEN$$` is replaced by that device's value, through your mapping. If a token can't be filled in on a device, the rollout won't start until you fix it.
6. **Results** keep each device's outcome, the log, each command's verdict with its evidence, and a way to roll back.

Commands without tokens need only devices and security profiles. Properties and mappings matter only when the same commands must carry different values per device.

### Who sees and edits what

| Item | Whose is it | Who sees it | Who edits it |
|---|---|---|---|
| Your own device | Yours | You | You |
| Global device | Shared by an administrator | Everyone | Administrators |
| Security profile | Yours | You | You |
| Which of your profiles a device uses | Yours | You | You, on any device you see, global devices included |
| System property (`hostname`, `vrfs`, ...) | Built in | Everyone | Nobody (built in) |
| A system property's value on a device | The device's | Everyone who sees the device | Whoever may edit the device |
| Custom property, and its values | Yours | You | You, on any device you see, global devices included |
| Variable mapping | Yours | You | You |
| Rollout and its results | Yours | You and administrators | Administrators can also cancel it or roll it back |

### Personal credentials

Every user logs in to a device with **their own** security profile. A global device is shared, but its credentials never are: each person who uses it links one of their own profiles to it, and NetRollout logs in with that person's credentials when they roll out. The device's own login records (TACACS, RADIUS) and NetRollout's audit log both name who made a change, and each team works within its own rights on the device.

A device without your security profile shows **ACTION NEEDED** in Inventory. Link one of your profiles to it (open the device, **Your security profile**, or **Security Profiles**) before you roll out to it.

## Signing in

Open NetRollout's address in your browser. Your administrator gives it to you, for example `https://netrollout.corp.local`.

### Getting an account

An account comes one of three ways:

- **Your administrator adds you.** You get a username and a temporary password. At the first sign-in you choose your own password.
- **You ask for one.** On the sign-in page, choose **Request access** and fill in **Username**, **Email**, **Full Name**, an optional **Position** and a password. Then select **Request Access**. An administrator must approve your request before you can sign in. Until then, signing in fails with a message that your account is pending approval.
- **Your organisation's directory (LDAP / Active Directory).** If your administrator has set this up, sign in with your directory username and password. An account is created for you the first time. Your role (operator or admin) can come from your directory groups; it is then checked again every time you sign in.

### The password rule

A local account's password must:

- be at least 8 characters long
- use at least 2 of: letters, digits, special characters
- use ASCII characters only
- not contain your username

The page tells you what's wrong as soon as you leave the field. A directory (LDAP) account's password is managed by the directory, not by NetRollout.

### Wrong passwords

A wrong password gets "Invalid credentials". After 5 failed sign-ins in a row with the same username, the sign-in page says **"Too many failed sign-ins. Try again in N minutes."** and stops that username for 15 minutes: nothing is checked until then, not even the right password. Wait for the time shown, or ask an administrator to **Enable** your account, which lifts it at once.

If the next 5 passwords after that are wrong too (10 in a row), a local account is disabled. Signing in with the right password afterwards says "User disabled, please check with administrator": ask an administrator to enable your account again.

The 15-minute stop works the same for every username, whether an account exists or not, so the page never reveals which usernames exist. A right password, before the 5th or after the stop, starts the count again. Wrong 2FA codes don't count here.

The factory `admin` account is never disabled this way: after every 5 wrong passwords it is only stopped for 15 minutes.

Directory (LDAP) accounts get the same 15-minute stop on the sign-in page; beyond that, the directory's own lockout rules apply.

### Two-factor sign-in

Local accounts sign in with a password and a 6-digit code from an authenticator app, such as Google Authenticator or Authy. Directory (LDAP) accounts sign in with the password only.

The first time you sign in, NetRollout shows **Authenticator Enrollment**:

1. Scan the QR code with your authenticator app.
2. Enter the 6-digit code the app shows.
3. Select **Confirm Enrollment**.

From then on, after your password you'll see **Two-Factor Auth**. Enter the current code and select **Verify**. After 5 wrong codes in a row, you start again from the password.

If you lose your phone, ask an administrator to reset your two-factor sign-in. At your next sign-in you enrol again with a new QR code.

The factory `admin` account signs in with its password alone unless it turns two-factor sign-in on for itself: **My Account → Set up two-factor sign-in**. See [The factory admin account](admin-guide.md#the-factory-admin-account).

### Changing your password

Open the user menu (your username, top right), choose **My Account**, then **Change password**. Enter your current password and the new one twice.

When you sign in with a temporary password (a new account, or after an administrator reset), NetRollout asks you to set your own password before you can do anything else.

Changing your password signs you out everywhere else. The current browser stays signed in.

### Session timeout

You're signed out after a period without activity: 15 minutes unless your administrator changed it. A minute before, a **Still there?** box counts down. Select **Stay signed in** to continue, or **Sign out**. Activity in another tab of NetRollout counts too. Automatic refreshes and an open live log don't count as activity.

However active you are, you sign in again after 12 hours. When NetRollout restarts or is upgraded, everyone is signed out.

## Finding your way around

The sidebar on the left holds every page:

| Section | Pages |
|---|---|
| **Operation** | **Launch Rollout**, **Active Jobs** |
| **Management** | **Inventory**, **Security Profiles**, **Variable Mappings**, **Properties** |
| **Observability** | **Dashboard**, **Results**, **Analytics** |

The user menu (your username, top right) has **My Account** and **Logout**. Administrators also have a switch next to it, **Operations | Administration**, to move between the day-to-day pages and the administration pages; each side returns to the last page open there.

## The dashboard

The dashboard is the first page after you sign in.

![The NetRollout dashboard](images/dashboard.png)

It shows:

- **Active rollout**: your rollout that is running or queued, with **Cancel**. If none is running, **Launch Rollout** starts one.
- **Recent activity**: your last 5 rollouts and their status. A job with a problem that needs a person is marked **Action needed - open the job**. **View all** opens Results.
- **Summary**: how many devices, security profiles and variable mappings you have, your total rollouts and the last status.
- **Your 30-day numbers**: success rate, device pushes, commands pushed, and the device with the most failures.
- **Quick actions**: **Manage Inventory**, **View All Results**.

## Inventory

**Inventory** lists the devices you can roll out to.

![The device inventory](images/inventory.png)

### Your devices and global devices

There are two kinds of device:

- **Your own devices.** Only you see them, and only you can edit or remove them.
- **Global devices**, marked **GLOBAL**. An administrator creates them and shares them with everyone. Only an administrator can edit them.

You can use a global device in your own rollouts with **your own** security profile: open it and choose **Your security profile**. You can also bind your own variable mappings to it and set your own values for your custom properties. Until you link a profile, it shows **ACTION NEEDED**. If the person who created a global device leaves NetRollout, the device stays for everyone.

When some of your devices need a profile, Inventory says how many ("3 devices need your security profile") with **Show them**, which lists only those.

Above the devices, three filters narrow the list as you type: **Name** and **Address** match any part (`edge`, `10.1.`, `corp.local`), and the platform list offers the platforms in your inventory. They work together; **Clear filters** shows everything again, and Escape empties the box you're in.

### Adding a device

1. Select **Add Device**.
2. Fill in:
   - **LABEL** (required): a name, such as `Core-SW-01`.
   - **ADDRESS**: an IPv4 or IPv6 address, such as `192.168.1.1` or `2001:db8::1`, or a hostname, such as `core1.corp.local`. A name alone, such as `core1`, gets the **Device DNS suffix** your administrator set in System Settings. NetRollout looks names up in the DNS its server uses. A name that doesn't resolve is saved anyway, with a warning: a rollout to it fails until it does. If the address, or the address a name resolves to, is already used by another device you see on the same port, NetRollout says so.
   - **Port**: the SSH port, 1 to 65535.
   - **DEVICE TYPE**: the platform. See [Supported platforms](#supported-platforms).
   - **YOUR SECURITY PROFILE**: one of your profiles, which NetRollout logs in with when you roll out to the device. You can save the device without one and link it later. You can pick one or select **Create New Profile**. Without one, the device shows **ACTION NEEDED** and you can't roll out to it.
3. Optionally, select **Test Connection** to check that NetRollout can log in.
4. Save.

Administrators also see a **Global device** switch, which shares the device with all users. Switching it off on a global device makes it yours and hides it from everyone else. Their security profile, attribute values and variable mappings on it are deleted, and switching it on again doesn't bring them back. The window warns you, and Save becomes a red **Confirm**.

To change a device, select its card. The **Edit Device** window also has the device's **VARIABLE MAPPINGS** and **VARIABLE ATTRIBUTES** (see [Properties and variable mappings](#properties-and-variable-mappings)). To delete it, use **Remove device** and confirm.

Two devices can share an address if their ports differ, for example behind NAT. NetRollout identifies a device by `address:port` (written `ip:port` in messages and the CSV). A device saved by a name and one saved by its IP are the same target: NetRollout warns when you save the second, and won't put both in one rollout.

### Reachability

Each card shows whether NetRollout's server can reach the device's `ip:port` over TCP. This is checked from the server, not from your computer, because that's where the push comes from. Results are reused for a short time (60 seconds unless your administrator changed it). Select **Recheck** to probe every device again now.

The device's logo follows it: in its vendor's colors while the device is reachable, grey while it's being checked or when it isn't. New Rollout's device list shows it the same way.

## Importing devices from a CSV

Select **Import CSV** to add many devices at once. The file is the same devices CSV the [CLI](cli.md#the-devices-csv) uses.

| Column | Required | Description |
|---|---|---|
| `ip` | Yes | An IPv4 or IPv6 address, or a hostname (a name alone gets the Device DNS suffix). Names that don't resolve are imported and listed in a warning. |
| `device_type` | Yes | A supported Netmiko device type, such as `cisco_ios` |
| `port` | Yes | SSH port, 1 to 65535 |
| `label` | No | The device's name. Defaults to the address. |
| `username`, `password`, `secret` | No | Credentials. See **Create security profiles** below. |
| *a property's name or label* | No | An attribute value, for example `hostname`, `site` or `vrfs`. List values are comma-separated inside one quoted cell. |

Example:

```csv
ip,device_type,port,label,username,password,secret,hostname,site,vrfs
10.1.0.1,cisco_ios,22,BR1-RTR,netops,S3cret!pass,S3cretEnable,br1-rtr-01,Branch 1,"MGMT,VOICE"
10.1.0.2,cisco_ios,22,BR2-RTR,netops,S3cret!pass,S3cretEnable,br2-rtr-01,Branch 2,MGMT
2001:db8::10,arista_eos,22,DC-LEAF-01,netops,S3cret!pass,,dc-leaf-01,DC,
```

The import form has:

- **CSV FILE**: the file to import.
- **LABEL** (optional): a label for every imported device. It takes precedence over the file's `label` column.
- **Create security profiles from credential columns** (on by default). Rows with the same credentials share one new profile. One of your existing profiles with exactly the same credentials is reused. Existing profiles are never changed. A row needs both `username` and `password` for a profile; a row with only one of them is imported without a profile, and NetRollout names it. When this option is off, the credential columns are ignored and the devices are imported without a profile.

After the import, NetRollout shows what was added and lists each row it couldn't import and why. Columns that aren't a property are listed as **Ignored columns**, so a misspelt property name doesn't go unnoticed. To import such a column, create the property under **Properties** first, then import again. A system property's value is stored on the device. A custom property's value is stored as yours (see below). The import doesn't check reachability; the Inventory page shows it once the devices are in.

> [!WARNING]
> A CSV with credentials holds passwords in plain text. Delete it after the import. Once imported, NetRollout stores them encrypted.

## Security profiles

A security profile is a set of login credentials: **USERNAME**, **PASSWORD** and an optional **ENABLE SECRET**. Assign one profile to many devices, and change the password in one place.

On **Security Profiles**:

- **Add Profile** creates one. The **LABEL** is optional, for example `Core Switches`.
- **Edit profile** changes it. Leave the password blank to keep the current one. **Clear enable secret** removes the secret.
- **Test connection** picks one of your devices and tries a full SSH login with the profile.
- **View attached devices** shows which devices use the profile when you roll out: your own devices and global ones. Drag devices between **Assigned** and **Available**, or click one to move it. Nothing changes until you select **Save**. Linking a global device here changes only your own link; other users keep theirs.
- **Delete profile** deletes it even while devices use it: those devices then show **ACTION NEEDED** until you link another of your profiles, and the Delete window says how many first.

Profiles are yours. Other users don't see them, and nobody else can log in with them. Passwords and secrets are encrypted in the database and are never shown again after you save them.

## Properties and variable mappings

Variables let one command set carry per-device values. You write a token such as `$$LOOPBACK$$` in your commands, and each device gets its own value at push time.

Three pieces work together:

1. A **property** is a kind of value, such as a hostname or a loopback address.
2. An **attribute** is a property's value on one device.
3. A **variable mapping** binds a token to a property, and is assigned to devices.

### Properties

**Properties** lists them. NetRollout ships these system properties, which can't be changed:

| Name | Label | Kind |
|---|---|---|
| `hostname` | Hostname | single value |
| `loopback_ip` | Loopback IP | single value |
| `asn` | ASN | single value |
| `mgmt_vrf` | Management VRF | single value |
| `mgmt_interface` | Management Interface | single value |
| `site` | Site | single value |
| `domain` | Domain | single value |
| `timezone` | Timezone | single value |
| `vrfs` | VRFs | list |

Select **Add Property** to define a custom property. Give it a **LABEL**, such as `BGP Peer IP`. The **NAME** is generated from the label (`bgp_peer_ip`) and can't be changed after you create the property. Choose an **ICON** if you like. Tick **List property** for a comma-separated list of values that commands can index, like `vrfs`.

Custom properties are yours: other users don't see them or their values.

### Attributes

You set a device's values in its **Edit Device** window, under **VARIABLE ATTRIBUTES**, and they can come from the CSV import too.

- A **system property's** value belongs to the device. Everyone who sees the device sees it, and whoever may edit the device can change it.
- A **custom property's** value belongs to you. You can set it on any device you see, global devices included, under **My attributes** (**Save Mine**). Only your mappings use it.

### Variable mappings

On **Variable Mappings**, select **Add Mapping**:

- **TOKEN**: letters, digits and underscores, up to 64 characters, without the `$$`. For example `LOOPBACK`, used in commands as `$$LOOPBACK$$`.
- **ATTRIBUTE**: the property it reads. You can create a new one from here.
- **LIST INDEX**: for a list property, the 0-based position. For example, `vrfs` with index `1` is the second VRF.
- **LABEL** (optional): a description.

Then assign the mapping to devices: select **View assigned devices** and move devices between **Assigned** and **Eligible**. Only devices that have a value for the property are eligible. You can also bind mappings to a device from its **Edit Device** window.

Mappings are yours. Two users can use the same token name for different things.

### How tokens are filled in

At push time, each `$$TOKEN$$` in a device's commands is replaced by that device's value, as you see it. For example:

```text
interface Loopback0
 ip address $$LOOPBACK$$ 255.255.255.255
```

A token can't be filled in on a device if no mapping for it is assigned to that device, or if the device has no value (or the list is too short for the index). NetRollout checks this before it starts:

- On **Launch Rollout**, each device with a problem is marked, and **Launch Rollout** stays disabled until you fix the commands or the device, or untick it.
- If a value disappears while a rollout is queued, that device is skipped at push time and counted as failed. The other devices still run.

## Running a rollout

Open **Launch Rollout**.

![The New Rollout page](images/new-rollout.png)

1. **Select Devices.** Tick the devices, or use **Select all**. **Filter by label or IP** narrows the list. A device without your security profile can't be ticked; link one in Inventory. **Recheck** probes reachability again.
2. **Commands.** Give the commands for the devices you ticked, one per line, as you would type them in configuration mode. Choose **Paste** to type them, or **File** to load a `.txt` file. Don't add `configure terminal`, `write memory` or `commit`: NetRollout enters configuration mode and saves or commits the way each platform needs. If you tick devices of more than one platform, you see **Multiple platforms selected**, and you give one command set per platform.
3. **Rollout Note** (optional): up to 255 characters, such as a change or ticket number. It's shown in Results.
4. **Options:**
   - **Verify after push** (on by default): NetRollout reads each device's configuration before and after the push and judges every command by what changed. See [Verify](#verify).
   - **Verbose logging**: more detail in the job's log.
5. Select **Launch Rollout**.

**Launch Rollout** stays disabled while no device is ticked, the same `ip:port` is ticked twice, or a token can't be filled in. When you launch, a device ticked by its name and again by its IP (the name looked up then) is refused the same way. That lookup also updates the address NetRollout keeps for each name, so the "already used by" warning follows DNS changes. When you launch, NetRollout also checks that every ticked device is reachable from the server. If any isn't, the rollout is refused and the message names the devices; select **Recheck** once they're back.

### What happens on each device

1. NetRollout connects over SSH. With Verify on, it reads the device's configuration first.
2. It enters configuration mode and sends the commands in order. Whatever the device answers, the remaining commands are still sent, and each answer is kept with its command.
3. It finishes the change the way the platform needs: it saves the configuration, commits, or does nothing on platforms where changes are live. See [Supported platforms](#supported-platforms).
4. With Verify on, it reads the configuration again, in the same session, and judges each command by what changed.

A rollout configures several devices in parallel: 10 at a time unless your administrator changed it.

### The queue

NetRollout runs a limited number of rollouts at once: 4 unless your administrator changed it. A rollout launched while all slots are busy waits in a queue and shows **Queued - waiting for a free slot** until one frees up.

### Verify

With **Verify after push**, NetRollout judges each device by its own configuration, not by what the device printed. It reads the configuration before the push and again after it (saved or committed), compares the two, and gives each command a verdict:

| Verdict | When |
|---|---|
| **took effect** | The command's words are in a line the push added, in order. The device may add words of its own (a sequence number, quotes) and may abbreviate differently: `int lo98` matches `interface Loopback98`. |
| **already in place** | The configuration already had exactly that line, unchanged. Running the same rollout twice gives this. |
| **removed** | A removal (`no`, `undo`, `delete`, `unset`) whose words are in a line the push removed. A removal of something that was never there is **already in place** ("already absent"). |
| **review** | The configuration doesn't prove it either way: the device stored the value in its own form (NX-OS writes `ip address 10.0.0.1 255.255.255.255` as `ip address 10.0.0.1/32`, secrets are stored hashed), nothing changed (a refused command, a typo, a value back to its default, a command that leaves nothing in the configuration such as `crypto key generate`), or one changed line could belong to two of your commands. A person decides: see [Reviewing a device](#reviewing-a-device). |

Commands that only move between sections (`exit`, `end`, `next`) aren't judged. A command that opens a section the device names its own way (FortiOS `edit 0` becomes `edit 1`) is matched by the section it opened.

What the device replied to each command is kept and shown next to its verdict, as the reason, but it never decides the verdict: an error message can't make a command fail, and a quiet device can't make it succeed. Changes no command explains (another person, or the device itself) are listed apart, as **changed, not by these commands**. Some devices change their own configuration on every read (FortiOS encrypts every stored password anew each time): when changes like that appear, NetRollout reads the configuration once more and leaves out what changed by itself.

Each device is judged against its own commands, with its tokens filled in. On platforms that save, NetRollout also checks that the lines the push added are in the saved configuration; if not, the device gets an **ACTION NEEDED**: the change is live but not saved.

How a device's result shows:

- Every command proven (took effect, already in place, or removed): `success`.
- At least one command in review: `review`.
- The device's **Verified** count in Results is the number of commands the configuration proved.

Without Verify, or when the configuration can't be read, the device is `not verified`: the commands were sent, and nothing judged them. The device's replies are still in the log.

### Cancelling

Select **Cancel** on the dashboard or on Active Jobs:

- A queued rollout ends at once and is recorded as cancelled ("Cancelled before it started").
- A running rollout stops starting new devices. Devices being configured finish, and devices not reached yet are skipped and recorded as `cancelled`.

## Active Jobs and the live log

When you select **Launch Rollout**, NetRollout takes you to **Active Jobs**, with your new job highlighted.

**Active Jobs** lists the rollouts running or queued, with **Running**, **Queued** and **Devices in flight** counts. The page refreshes itself every 15 seconds while jobs are active (not while a log is open). Administrators also see other users' jobs, under **Other Users**.

For each job:

- **Log** opens the live log under the job's row. It streams the job's log lines as they happen: each device connecting, what each device replied to a command, the save or commit, each command's verdict, any **ACTION NEEDED** line and the final summary. Opening it later shows the lines so far first. A queued job's log starts with "Queued - waiting for a free slot" and waits for it to start.
- **Cancel** cancels it (see [Cancelling](#cancelling)).
- **Rollback** is greyed while the rollout runs.

When the rollout ends with its log open, a card shows the outcome: the job's status, the count per status, any **Action needed** devices with what to do, **Review N devices** when some are in review, **Rollback** (when at least one device succeeded) and **View in Results**.

The **dashboard** also shows your running or queued rollout, with its elapsed time, **Cancel** and **▶ Watch live**, which opens Active Jobs with that rollout's live log already open.

The live log is a convenience. If it drops ("Stream ended."), the job carries on, and the full log is kept: see [Logs](#logs).

### Logs

Every rollout writes a log file on the server, whether or not you watch it live.

- **Download Log**, on the job in Results, downloads it. It's the same text as the live log: every device's connection, what each device replied, the save or commit, each command's verdict with the line it rests on, the **ACTION NEEDED** and **REVIEW** lines and the summary. **Verbose logging** adds more detail.
- Secrets are redacted. The value after words such as `password`, `secret`, `community`, `key-string` or `psksecret` is written as `<redacted>`, in the file and in the live log, so a log is safe to attach to a ticket.
- You can download the logs of your own jobs; administrators can download any job's.
- Log files are kept 60 days by default. Administrators find them in the server's `logs` folder; see [Logs](admin-guide.md#logs) in the admin guide.

## Results

**Results** lists your finished rollouts, newest first.

![The Results page](images/results.png)

Each row shows the job, its note, when it started, how long it took, the number of devices and commands, and the status. Choose how many jobs to show per page. Administrators can switch to **All Users**.

The filters above the list cover every job, not only the page shown. **Note** and **Device name or IP** match any part as you type, and keep the whole job (all its devices). Pick a status, and a **From** / **To** day the job started on (both included). In **All Users**, administrators can also pick one user. **Clear filters** shows everything again, and Escape empties the box you're in.

**Export CSV** downloads every job the filters let through, one row per device: the job, its note, owner and status, when the device started and finished, the device's name, IP, port and platform, its status, the commands sent and verified, any ACTION NEEDED, and the review decision (outcome, by whom, when, the comment). It holds your own jobs, or everyone's in an administrator's **All Users** view. The commands typed and config snapshots aren't in it. Each export is recorded in the audit log.

Open a job to see each device: IP and port, platform, commands **Sent**, commands **Verified** and the device's **Status**. A job with devices still to decide shows **N to review** next to its status, and the dashboard says how many of your devices wait for a review.

### Statuses

Each device gets one of these:

| Status | Meaning |
|---|---|
| `success` | The configuration proved every command (Verify on), or a person confirmed a device in review. |
| `review` | The configuration didn't prove every command: a person decides. See [Reviewing a device](#reviewing-a-device). |
| `partial` | The device stopped answering partway through: some commands were sent, the rest weren't. |
| `failed` | Nothing took effect: no connection, login or configuration mode refused, a failed commit - or a person rejected a device in review. |
| `not verified` | The commands were sent, but nothing judged them: Verify was off, or the configuration couldn't be read (the log says why). |
| `cancelled` | The rollout was cancelled before this device was started. |

A job's status comes from its devices:

- `cancelled` if any device was cancelled
- otherwise `failed` if every device failed
- otherwise `partial` if any device failed or was partial
- otherwise `review` if any device is in review
- otherwise `not verified` if any device is
- otherwise `success`

### Reviewing a device

A device in `review` waits for a person: NetRollout couldn't prove every command, and won't guess. The job's owner or any administrator decides it:

1. Open the job in Results and select the device's **review** status, or **Verification**.
2. The **Verification** window shows each command with its verdict, the line it rests on (or "no change in its section"), and what the device replied. For example: `ip address 10.0.0.1 255.255.255.255` in review, with `+ ip address 10.0.0.1/32` - the device wrote your mask as a prefix. Or `ntp server 192.0.2.123` in review, no change, the device replied `% Initialization of NTP failed`.
3. Select **Confirm - it took effect** or **Reject - it didn't**, with a comment if you like.

Confirmed makes the device a `success` (so a rollback includes it), rejected makes it `failed`. The decision is kept with the result - who, when and the comment - shown in Results and in the export, and recorded in the audit log. A device is decided once.

### ACTION NEEDED

Some problems can only be fixed by a person on the device. NetRollout marks these **Action needed** in Results and on the dashboard, and writes them in the log as `ACTION NEEDED - <ip:port>: <what to do>`. The job lists each affected device with the action, for example:

- The change is live but **not saved**: save it on the device, or it's lost at the next reboot.
- A commit didn't finish in time: check on the device whether it went through.
- A commit failed: NetRollout sent the platform's discard command and shows what the device answered. Check that the **candidate configuration** is empty.
- Configuration mode was refused, usually because another session has uncommitted changes in the shared configuration: commit or discard them, then run the rollout again.
- The device stopped answering partway through: the log says which commands it received.
- The account logs in to the wrong shell (for example Check Point's expert shell) and nothing was sent.

### Job actions

- **See Commands**: the commands as you typed them, before token substitution.
- **Download Log**: the job's full log as a file. Secrets are redacted: the value after words such as `password`, `secret`, `community` or `psksecret` is written as `<redacted>`, so the log is safe to share. The commands you typed and configuration snapshots are not redacted.
- **Verification**: per verified device, each command as sent (tokens filled in) with its verdict, the line it rests on and the device's reply; the changes no command explains; and the configuration before and after. Devices in review are decided here (see [Reviewing a device](#reviewing-a-device)). The configuration snapshots are kept for 7 days unless your administrator changed it; after that the button shows **Verification expired**.
- **Rollback**: see [Rolling back](#rolling-back).
- **Compare**: tick two jobs, then select **Diff** to see their commands side by side.

Job records are kept for 30 days unless your administrator changed it.

## Rolling back

NetRollout doesn't guess how to undo a change, and it doesn't restore a saved configuration. A rollback is a new rollout of **compensatory commands that you write**, sent to the devices the original job configured **successfully**.

1. Select **Rollback** on the job in Results, or on the completion card in Active Jobs. It's offered only when at least one device ended `success` (a confirmed review counts).
2. Enter the compensatory commands, one per line. For a job that added `ntp server 192.0.2.123`, that's `no ntp server 192.0.2.123`. You can use tokens.
3. Choose **Verify after push** and **Verbose**, as for any rollout.
4. Select **Execute Rollback**.

What it does exactly:

- **Which devices**: those that ended `success` in the original job (including reviews a person confirmed), matched by `ip:port` among the devices the job's owner can see today (their own entry preferred over a global one with the same address). Devices that ended `review`, `partial`, `failed`, `not verified` or `cancelled` are left out: check and fix them by hand, or with a new rollout. A device deleted since the job is left out.
- **Which commands**: the same commands to every one of those devices, whatever their platform.
- **Whose credentials**: yours, never the job owner's. NetRollout logs in with your own profile on each device. If you have no profile on some devices, the rollback window lists them and asks you to pick one of your profiles. **Link it to the device** (ticked by default) keeps it as your profile for those devices, for next time. An administrator rolling back someone else's job may hit that person's private devices: the profile picked is used for this rollback only, since you can't link to a device that isn't yours.
- **Whose values**: the job owner's. Their mappings and values fill in the tokens, so the rollback targets what the job configured. Values are read when you start the rollback, not taken from the original job, so check they still reflect the network.
- **Checks first**: as for any rollout, every device must be reachable and resolve every token, or the rollback is refused with the reason.
- **Whose job**: the rollback is a new job under your name, shown in Active Jobs and Results. The audit log records it against the original job.

You can roll back your own jobs. Administrators can roll back anyone's.

## Analytics

**Analytics** shows your last 30 days: success rate, device pushes, commands pushed, the device with the most failures, and your top platforms.

The **Device Results Explorer** queries your rollout history. Build filters on **Started At**, **Platform**, **Status**, **Commands Sent**, **Device IP** and **Device Port**, then select **Run Query**. **Export CSV** downloads the results.

Administrators can choose **VIEW AS** to see another user's numbers.

## Your account

**My Account** (in the user menu) shows your details (full name, email, position, member since), whether two-factor sign-in is enrolled, and your all-time numbers: total rollouts, devices configured, commands pushed, success rate and top platform. **Change password** is here too. A directory (LDAP) account's password is managed by the directory.

## Supported platforms

| Device type | Platform | How NetRollout finishes the change |
|---|---|---|
| `cisco_ios` | Cisco IOS | Saves the configuration |
| `cisco_xe` | Cisco IOS XE | Saves the configuration |
| `cisco_nxos` | Cisco NX-OS | Saves the configuration |
| `cisco_xr` | Cisco IOS-XR | Commits. A failed commit leaves the running configuration unchanged. |
| `arista_eos` | Arista EOS | Saves the configuration |
| `aruba_aoscx` | Aruba AOS-CX | Saves the configuration |
| `hp_procurve` | HP ProCurve | Saves the configuration |
| `hp_comware` | HP Comware | Saves the configuration |
| `juniper_junos` | Juniper JunOS | Commits, in `configure private` |
| `paloalto_panos` | Palo Alto PAN-OS | Commits |
| `checkpoint_gaia` | Check Point Gaia | Runs `save config` (in clish) |
| `fortinet` | Fortinet FortiOS | Nothing to save: changes are live. NetRollout closes any `config` block left open. |

Not every platform has been tested on real devices yet. The [README](../README.md) shows which have been confirmed. Try a new command set on a lab device first.
