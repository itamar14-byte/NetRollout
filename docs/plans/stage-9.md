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
9. Backups include `.env` (with a clear warning; the `backups\` folder
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
install (date, Windows/Linux account, version, licence terms accepted). Keys:
`NETROLLOUT_VERSION`; the 7 secrets (`POSTGRES_PASSWORD`,
`NETROLLOUT_DB_PASSWORD`, `GRAFANA_DB_PASSWORD`, `REDIS_PASSWORD`,
`GRAFANA_ADMIN_PASSWORD`, `SECRET_KEY`, `NETROLLOUT_ENCRYPTION_KEY`);
`HTTPS_PORT`; `TZ`; `NETROLLOUT_SERVER_IPS`; the compose switches
(`COMPOSE_PROFILES`, `COMPOSE_PATH_SEPARATOR`, `COMPOSE_FILE`); and, only
after a bring-your-own move, the external connection (below).

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
- **E** — the server's IPs (`NETROLLOUT_SERVER_IPS`, for "Generate
  self-signed", refreshed by `netrollout status`) and the install record go in
  `.env`, not in new files.
- **F** — Grafana's setup script and dashboards are baked into the app image.
- **The merge** — the port request (`desired.env`) and its confirmation
  (`apply-confirm`) move into `config/nginx/site.env`: one file the app writes
  for both nginx (reads only its two keys) and the port helper. The status
  files stay one per writer (nginx's `status.json`, the helper's
  `apply-status.json`).

Installer questions: hostname [this computer's name], HTTPS port [443],
monitoring [Y], organisation certificate [none → self-signed], timezone
[this computer's].

## The commands

| Command | What it does |
|---|---|
| `install` | Refuses if already installed (→ `status` / `update`). Checks: not Windows Server; virtualization; Docker installed and running (waits for Docker Desktop); ports free (443 or the chosen one; 80 → port-80 redirect only if free). Licence notice → type `yes`. The 5 questions (Enter = default). Setup core writes `.env`, `site.env`, the folders (restricted where they hold secrets). Certificate (the organisation's, checked; else self-signed for the hostname + the server's IPs). `docker compose pull` → `up -d --wait` (on failure: what's unhealthy + the app's last log lines). Health checked from this computer. The port helper's auto-start (task / unit). Desktop shortcut. Prints the address (name and IP) and "sign in as admin / admin — you'll set a new password"; opens the browser. Auto-logon guidance (Windows: Docker Desktop starts at sign-in). |
| `start` | `up -d --wait`, then the address; opens the browser. |
| `stop` | Running rollouts are reported; waits for the drain (they finish and are recorded), then stops. |
| `status` | Address, health from this computer, each container, running/queued rollouts, nginx's last verdict, the certificate's expiry, a pending port change. Plain words, with the next step when something is wrong. |
| `open` | Opens the browser at the address. |
| `logs` | The app's recent log (or a service's: `logs nginx`). |
| `backup` | `pg_dump -Fc` as the superuser inside the Postgres container, Grafana's volume, `config\`, `certs\`, `.env` → one zip in `backups\` (warning: it holds every secret). |
| `restore <zip>` | Onto this install: refuses a backup from a newer version; backs up the current state first; stops, restores, starts; checks the data. |
| `update` | Backup → downloads the release (GitHub; checksum checked) → replaces the scripts and compose files → `pull` → `up -d --wait`. Reports running rollouts and waits for them. |
| `apply` | Runs the port helper's step once (a pending HTTPS port change). |
| `move-database <url>` / `move-redis <url>` | Bring your own (below); `bundled` moves back. |
| `uninstall` | Stops and removes the containers and the helper's auto-start; asks before deleting the data volumes; asks before deleting the folder. |

Every command can be run twice safely, prints steps in plain words (Docker's
output only on failure), and exits 0 on success. `--yes --defaults` (+ answers
as flags) for unattended runs (CI).

## Bring your own database / Redis

A move, not a switch: switching live to an empty server (today's Server
Management switch) would leave the data behind. The move is a host command,
because it stops the app, copies the data and changes what Grafana uses too.

- **`move-database <url>`** (the target's admin connection, used once, never
  stored): checks the target (reachable, Postgres version, empty or a
  NetRollout database) → stops the app (drain) → backup → creates the
  `netrollout` database and roles (`netrollout`, `grafana_reader`) on the target
  → `pg_dump` / `pg_restore` → checks the row counts → writes the app's
  connection (`.env`) and Grafana's datasource (host, port, database) → starts
  → the bundled Postgres stops (its volume kept until `uninstall`, so `move-database
  bundled` can go back).
- **`move-redis <url>`**: nothing to copy (sessions, job state, live logs) —
  waits until no rollout runs, writes the connection, restarts; everyone signs
  in again.
- **pg_cron**: many managed databases don't offer it, and today retention then
  never runs. The app gets a fallback: when pg_cron isn't available, it runs
  the same retention statements itself, daily at 03:00 (like the log pruning).
- **Server Management** keeps the connection test (useful before a move) and
  shows where the database and Redis are; in Docker installs the live switch is
  replaced by "move it with `netrollout move-database`". `config/runtime.env`
  stays for development and non-Docker runs.

## Subtasks

| # | Subtask |
|---|---|
| 9.1 | Thinner settings: A–F + the merge (compose, `site.env` seed, the image gets Grafana's setup + dashboards; port-helper contract updated) |
| 9.2 | The setup core (`src/setup/`): `init` / `check` (questions, validation, secrets, `.env`, `site.env`, folders), the status report, the contract with the scripts (arguments, exit codes, messages) |
| 9.3 | Windows: `netrollout.ps1` + the two launchers — install, start, stop, status, open, logs |
| 9.4 | Linux: `netrollout.sh` + `install.sh` — the same |
| 9.5 | `backup` / `restore` |
| 9.6 | `update` + `uninstall` |
| 9.7 | The port helper (Windows Task Scheduler, Linux systemd `.path`) + `apply` |
| 9.8 | Bring your own: `move-database` / `move-redis`, the retention fallback, Server Management |
| 9.9 | The release zip build (a script stage 10's release job calls) + verification: a full install on the developer's PC into a scratch folder on other ports (the dev stack holds 443/80); Linux in CI (stage 10) |
