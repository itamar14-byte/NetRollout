# Phase 4 stage 9 — installer, management script, port helper, bring-your-own

_Specification, agreed 2026-10-05 (replaces stage 9's paragraph in
`phase-4.md`, which points here). Built subtask by subtask: each is explained,
approved, done, reviewed._

## The user

A network admin installing on a Windows 10/11 PC or VM (Linux as the
alternative). Knows networking, not necessarily Docker — and never has to type
`docker`: the script pulls the images and runs compose itself.

## Decisions (2026-10-05)

1. **The install folder is the folder the zip was extracted to.** Copying or
   deleting that folder plus the Docker volumes is the whole installation.
2. **One zip for both OSes**, the scripts in `windows\` and `linux\`, everything
   shared at the root.
3. **One management script per OS** (`netrollout.ps1`, `netrollout.sh`) with
   every command — `install` and `update` included (PowerShell reads a whole
   script before running it; the bash script wraps its body in one block, so
   `update` can replace it safely). Windows adds two launchers: `install.bat`
   (double-click) and `netrollout.bat` (command line, ExecutionPolicy Bypass).
4. **Thin host scripts, one setup core.** The scripts do only what needs the
   host (checks, `docker compose`, auto-start, the port helper, downloads); the
   logic — questions, validation, secrets, `.env`, backup packing, restore
   checks, the status report — is Python in the app image
   (`python -m src.setup …`, run with the install folder mounted), written
   and tested once for both OSes.
5. **`install` and `start` open the browser** at NetRollout's address when run
   on a desktop (as the dev start does; `--no-browser`, and never in
   unattended / headless runs).
6. **`uninstall`**: stops and removes the containers; asks separately before
   deleting the data volumes and, last, the folder.
7. **Thinner settings** (A–F, below): `.env` is the one file a person ever
   edits; the app hands nginx and the port helper one file.
8. **Bring your own database / Redis** is supported as a *move*, with the data
   — not as a live switch (below).
9. **The version lives in one file** (2026-10-05): `VERSION` at the repo root
   is the source — `src/runtime.py` reads it (copied into the image, bundled
   into the `.exe`, the repo root in dev), so the footer, the health endpoint
   and the source link follow it; the Dockerfile's `sed` stamping goes; the
   release job checks the tag equals `VERSION` and refuses a mismatch. The same
   file ships in the zip. Releasing: set `VERSION` to `1.0.0`, commit, tag
   `v1.0.0`; then `1.0.1.dev0`.
10. **No `.env.example`**, shipped or in the repo: the `.env` the setup core
   writes explains every line itself, and its template is the only
   description of the file. Developers get theirs the same way:
   `python -m src.setup init --dev` (adds the dev compose files).
11. Backups include `.env` (with a clear warning; the `backups\` folder
   restricted to admins); restore accepts the same or an older version; Postgres
   password rotation is documented (a command post-v1); Linux: the script checks
   for Docker Engine and explains, never installs it; Ubuntu is the tested
   distribution.

## The zip

```
netrollout-1.0.0\
  windows\
    install.bat          double-click: first-time install
    netrollout.bat       command line: netrollout status | backup | …
    netrollout.ps1       every command
    netrollout.ico       the shortcuts' icon (the logo's N; made once, committed)
  linux\
    install.sh           ./linux/install.sh — the same as netrollout.sh install
    netrollout.sh        every command
  compose.yaml           the stack
  compose.http.yaml      port 80 → HTTPS (only when port 80 is free)
  deploy\
    prometheus\prometheus.yml
    loki\loki-config.yml
    alloy\config.alloy
    grafana\provisioning\datasources\netrollout.yml
  README.md   LICENSE   VERSION
```

Not in it: the images (pulled), `netrollout-cli.exe` (its own release asset —
a workstation tool), `.env.example` (the generated `.env` explains every line),
`compose.build.yaml` / `compose.dev.yaml` (development), `deploy/nginx` and
`deploy/postgres` (in their images), Grafana's setup script and dashboards
(moved into the app image — F), `SHA256SUMS` (next to the zip in the release;
`update` checks the download against it).

After `install` the folder also holds `.env`, `config\`, `certs\`, `logs\`,
`backups\`; the databases live in Docker volumes (`netrollout_*`).

## Settings after install (thinner: A–F + the merge)

**`.env`** — written by the setup core, never by the app. A header records the
install (date, Windows/Linux account, version, licence terms accepted). Keys
(13): the 7 secrets (`POSTGRES_PASSWORD`,
`NETROLLOUT_DB_PASSWORD`, `GRAFANA_DB_PASSWORD`, `REDIS_PASSWORD`,
`GRAFANA_ADMIN_PASSWORD`, `SECRET_KEY`, `NETROLLOUT_ENCRYPTION_KEY`);
`HTTPS_PORT`; `TZ`; `NETROLLOUT_SERVER_IPS`; the compose switches
(`COMPOSE_PROFILES`, `COMPOSE_PATH_SEPARATOR`, `COMPOSE_FILE`). (An external
database / Redis after a move lives in `config/runtime.env`, not here.)

- **The version** — `NETROLLOUT_VERSION` leaves `.env`: the scripts run
  compose themselves and pass it from the `VERSION` file on every call (one
  copy in the install folder; `update` only replaces files). A hand-run
  `docker compose` stops with compose's own message pointing to the script.
- **A** — the hostname leaves `.env` and nginx's environment: the setup core
  writes it into `site.env` (with the app's own `write_site`), the app seeds
  System Settings from `site.env` at the first start, nginx reads only
  `site.env`.
- **B** — nginx's `NETROLLOUT_HTTPS_PORT` from compose goes (its image defaults
  to 443 until the app writes `site.env` at start); the app keeps the published
  port.
- **C** — concurrent rollout jobs: no installer question, no `.env` line
  (default 4; System Settings).
- **D** — `NETROLLOUT_THREADS` (default in code, still settable — README
  "Advanced") and `HTTP_PORT` (always 80 in `compose.http.yaml`) leave `.env`.
  Port 80 is an on/off switch (`compose.http.yaml` in `COMPOSE_FILE`): on when
  host port 80 is free at install. It only matters on the host — inside the
  Docker network nginx's own port 80 never clashes. **Taken later** (IIS,
  another web server) would stop the whole stack from starting, so `start`,
  `update` and `status` check it while the switch is on: taken → say so, turn
  the switch off, start without the http→https redirect; `start` turns it back
  on once port 80 is free. Whoever holds a taken port is named (Windows: the owning process;
  IIS and other HTTP.sys users show as PID 4 "System" → "Windows' HTTP service
  (IIS, or another program using it)"), with what it means ("typing
  http://<name> reaches that program") and README → "Port 80 is taken": leave
  it; an HTTP Redirect rule in IIS to `https://<name>`; or free port 80 (then
  `netrollout start` turns the redirect on). A taken HTTPS port at install →
  asked again, a free one suggested (e.g. 8443). Path: port 80 → nginx answers
  with the redirect itself; the HTTPS port → nginx → the app (Waitress, 8080,
  inside the Docker network only — never published, so never asked).
- **E** — the server's IPs (`NETROLLOUT_SERVER_IPS`, for "Generate
  self-signed", refreshed by `netrollout status`) and the install record go in
  `.env`, not in new files.
- **F** — Grafana's setup script and dashboards are baked into the app image.
- **The merge** — the port request (`desired.env`) and its confirmation
  (`apply-confirm`) move into `config/nginx/site.env`: one file the app writes
  for both nginx (reads only its two keys) and the port helper — keys
  `NETROLLOUT_PORT_REQUEST`, `_REQUEST_ID`, `_REQUESTED_AT`, `_CONFIRMED`,
  written only by `src/site_env.py` (no web dependencies: the setup core uses
  it too). The status
  files stay one per writer (nginx's `status.json`, the helper's
  `apply-status.json`).

Installer questions: hostname [this computer's name], HTTPS port [443],
monitoring [Y], organisation certificate [none → self-signed], timezone
[this computer's].

## The commands

| Command         | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `install`       | Refuses if already installed (→ `status` / `update`). Checks: not Windows Server; virtualization; Docker installed and running (waits for Docker Desktop); ports free (443 or the chosen one; 80 → port-80 redirect only if free). Licence notice → type `yes`. The 5 questions (Enter = default). Setup core writes `.env`, `site.env`, the folders (restricted where they hold secrets). Certificate (the organisation's, checked; else self-signed for the hostname + the server's IPs). `docker compose pull` → `up -d --wait` (on failure: what's unhealthy + the app's last log lines). Health checked from this computer. The port helper's auto-start (task / unit). Desktop shortcut. Prints the address (name and IP) and "sign in as admin / admin — you'll set a new password"; opens the browser. Auto-logon guidance (Windows: Docker Desktop starts at sign-in). |
| `start`         | `up -d --wait`, then the address; opens the browser.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `stop`          | Running rollouts are reported; waits for the drain (they finish and are recorded), then stops.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `status`        | Address, health from this computer, each container, running/queued rollouts, nginx's last verdict, the certificate's expiry, a pending port change. Plain words, with the next step when something is wrong.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `open`          | Opens the browser at the address.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `logs`          | The app's recent log (or a service's: `logs nginx`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `backup`        | `pg_dump -Fc` as the superuser inside the Postgres container (after a move to an external database: that database, through the app's connection — the client's version must be at least the server's, so a newer server is reported, not dumped badly), Grafana's volume, `config\`, `certs\`, `.env` → one zip in `backups\` (warning: it holds every secret).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `restore <zip>` | Onto this install: refuses a backup from a newer version; backs up the current state first; stops, restores, starts; checks the data.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `update`        | Backup → downloads the release (GitHub; checksum checked) → replaces the scripts and compose files → `pull` → `up -d --wait`. Reports running rollouts and waits for them.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `apply`         | Runs the port helper's step once (a pending HTTPS port change).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `uninstall`     | Stops and removes the containers and the helper's auto-start; asks before deleting the data volumes; asks before deleting the folder.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

Every command can be run twice safely, prints steps in plain words (Docker's
output only on failure), and exits 0 on success. `--yes --defaults` (+ answers
as flags) for unattended runs (CI).

## `install`'s order, Docker, the licence, the menu (decided 2026-10-05)

1. Already installed → refuse (→ `status` / `update`).
2. Windows Server → refuse; virtualization off → how to turn it on (skipped
   when Docker already runs).
3. **The licence notice → type `yes`** — from the script, before anything is
   installed (Docker Desktop's terms matter at the moment it's installed; the
   setup core can't show it — it needs Docker): NetRollout AGPL-3.0, Docker
   Desktop's terms + Windows licensing in a VM (Windows) / Docker Engine
   (Linux). The setup core only records it (`--licence-accepted`).
4. **Docker**: not installed → "Install it now? [Y]" → `winget install
   Docker.DockerDesktop` (Docker's official installer; winget shows Docker's
   terms), else Docker's installer downloaded from docker.com and run, else
   Docker's download page opened; then wait (≈15 min, Ctrl+C stops) until it
   is installed and running, starting Docker Desktop when needed. A restart
   Docker asks for → "restart Windows, then run install again" (nothing of
   NetRollout is written yet). Installed but not running → started, waited
   for (≈3 min).
5. Busy ports, named (PID 4 → Windows' HTTP service; Docker's own
   listeners → another container).
6. The app image (pulled only when missing).
7. `src.setup init`: the 5 questions, the files.
8. `up -d --wait`, health from this computer, `.env` and `backups\`
   restricted (Administrators, SYSTEM, the installing account), the
   shortcuts, the address + "sign in as admin / admin", the browser,
   auto-logon advice.

**Two desktop shortcuts**: "NetRollout" (the web app) and "NetRollout
Manager" — `netrollout` with no command: a numbered menu (Open, Status,
Start, Stop, Logs, then Backup / Restore / Update / Uninstall as they land;
0 Exit), "Press Enter to return to the menu" after each.

**Setup-core commands the scripts call** (so the Linux script reuses them and
`.env` is only written by Python): `prepare-start` (the port-80 switch from
the busy ports — our own published ports excluded — and the server IPs
refreshed), `status` (the report: from the files, the containers' states and
the host's reachability check passed in, and the app's health asked over the
compose network). Single `.env` lines are edited in place (its permissions
kept), only the keys the scripts own.

## 9.3b — a Windows app, not a script (decided 2026-10-05)

- **`NetRollout-Setup-<version>.exe`** (Inno Setup — free; compiled in CI on
  GitHub's Windows runner): the wizard — Welcome → Licence (NetRollout
  AGPL-3.0 + Docker Desktop's terms; I accept / I don't) → Docker (found /
  "Install Docker Desktop" → `netrollout ensure-docker`, waits) → Settings
  (the 5 questions as a form, pre-filled; a busy port flagged on the page) →
  install location (default `C:\NetRollout`) → progress ("Setting up…
  Starting…": `netrollout install -Yes` with the answers as parameters, its
  output in a log the wizard shows on failure) → Finish ("Open NetRollout
  now"). Settings → Apps entry (icon, version, publisher) with an
  uninstaller (`netrollout uninstall`: stops and removes the containers;
  asks whether to delete the data); Start Menu + desktop shortcuts; optional
  "Start NetRollout Manager at sign-in" (tray). The zip stays for Linux and
  for admins who prefer scripts; `netrollout.bat` stays the power-user
  command line.
- **`NetRollout Manager.exe`** (C# WinForms on .NET Framework 4.8, built into
  Windows 10/11; compiled with its `csc.exe`, no extra installs — a
  PowerShell window would flash a console and show as "Windows PowerShell"):
  the logo, a status light (Running / Stopped / Needs attention + why), the
  address, buttons (Open, Start, Stop, Status, Logs; Backup / Restore /
  Update as they land) running `netrollout.ps1` hidden and showing its
  output; a tray icon (green / red, right-click: the same actions); one
  instance. Replaces the console menu for people; the menu stays for the
  command line.
- The script takes the answers as parameters (`-Hostname -HttpsPort
  -Monitoring -OrgCertificate -TimeZone`) and gains `ensure-docker` and
  `uninstall`.
- Unsigned in v1.0 (SmartScreen: More info → Run anyway, explained in the
  README); SignPath Foundation (free for open source; Windows then shows
  "SignPath Foundation" as publisher) applied for after v1.0 is public with
  its CI pipeline — workplan post-v1.0.

## Bring your own database / Redis (from the UI)

A move, not a switch: switching live to an empty server would leave the data
behind. It runs from **Server Management**, in the background, in the app
(changed 2026-10-05 from a host command — the app can do every step itself).

- **Database → Move to another server**: the admin enters the target and its
  admin credentials (used for the move only, never stored) → **Test**
  (reachable, Postgres version, empty or a NetRollout database, can create
  roles) → **Move**, in the background:
  1. new rollouts refused, running ones drain (the existing drain);
  2. **maintenance mode**: a banner on every page, only admins in, nothing
     writes while the data is copied;
  3. on the target: the `netrollout` database and roles (`netrollout`,
     `grafana_reader` with its 3-table grant), the schema through the Alembic
     migrations;
  4. the rows copied table by table from Python — no `pg_dump`, so no client
     newer-than-server problem; live progress on the page (background polls);
  5. row counts compared; then the switch: the connection is written to
     `config/runtime.env` (app-owned, loaded over the environment — this is what
     it is for) and the app reconnects live; maintenance ends.
  Anything failing before step 5 leaves everything as it was, with the reason
  shown; a restart mid-copy is safe for the same reason (the switch is last).
  The bundled database is left untouched, so **Move back** is the same flow in
  reverse (to the bundled Postgres, emptied first). Audited.
- **Grafana follows the database**: its Postgres datasource is managed by the
  Grafana setup service (through Grafana's API, re-applied every 5 minutes, at
  once after a move) from the app's connection (`config/runtime.env`'s host /
  port / database, read-only mount) and the `grafana_reader` password — no
  longer a provisioning file with `postgres:5432`.
- **Redis**: nothing worth copying (sessions, job state, live log history):
  the existing Server Management switch stays, refused while rollouts run,
  with "everyone signs in again".
- **pg_cron**: many managed databases don't offer it, and today retention then
  never runs. The app gets a fallback: when pg_cron isn't available, it runs
  the same retention statements itself, daily at 03:00 (like the log pruning).
- **Grafana BYO stays post-v1** (workplan 4.0b: an external Grafana + an
  operators' organization) — no dependency either way (checked 2026-10-05), but
  two hooks are built now so it slots in without rework: Grafana's Postgres
  datasource is managed by the Grafana setup from the app's connection (needed
  by the move anyway), `grafana_reader` with its 3-table grant is created on any
  target database, and Server Management shows those connection details; and
  the Grafana setup (folders + dashboards through Grafana's API, in the app
  image after F) takes the Grafana URL and credentials as settings — pointing
  it at an external Grafana is the heart of Grafana BYO. Its open question for
  then: an external Grafana can't reach the bundled Postgres (only nginx
  publishes ports) — a moved database, or publishing Postgres on purpose.

## Subtasks

| #   | Subtask                                                                                                                                                                                                                                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 9.1 | Thinner settings: A–F + the merge (compose, `site.env` seed, the image gets Grafana's setup + dashboards; port-helper contract updated); the `VERSION` file as the one version source                                                                   |
| 9.2 | The setup core (`src/setup/`): `init` / `check` (questions, validation, secrets, `.env`, `site.env`, folders; `init --dev` for developers — `.env.example` removed), the status report, the contract with the scripts (arguments, exit codes, messages) |
| 9.3 | Windows: `netrollout.ps1` + the two launchers — install, start, stop, status, open, logs                                                                                                                                                                |
| 9.4 | Linux: `netrollout.sh` + `install.sh` — the same                                                                                                                                                                                                        |
| 9.5 | `backup` / `restore`                                                                                                                                                                                                                                    |
| 9.6 | `update` + `uninstall`                                                                                                                                                                                                                                  |
| 9.7 | The port helper (Windows Task Scheduler, Linux systemd `.path`) + `apply`                                                                                                                                                                               |
| 9.8 | Bring your own from the UI: the background move (maintenance mode, copy, switch, move back), Grafana's datasource from the setup service, the Redis switch kept, the retention fallback                                                                 |
| 9.9 | The release zip build (a script stage 10's release job calls) + verification: a full install on the developer's PC into a scratch folder on other ports (the dev stack holds 443/80); Linux in CI (stage 10)                                            |
