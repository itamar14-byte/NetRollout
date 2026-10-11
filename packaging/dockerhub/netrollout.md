# NetRollout

The application image of [NetRollout](https://github.com/itamar14-byte/NetRollout): push configuration snippets to many network devices at once over SSH, verify each device's configuration afterwards, and record every result. A web app for network engineers, supporting 12 platforms (Cisco, Juniper, Arista, Aruba, HP, Palo Alto, Check Point, Fortinet).

## How to use this image

This image isn't meant to be run on its own. It is one service of a Docker Compose project, with PostgreSQL, Redis, the [NetRollout nginx image](https://hub.docker.com/r/itamarweinstein/netrollout-nginx) in front of it, and optional Prometheus, Loki and Grafana.

Install NetRollout with its installer, which writes the compose settings, generates the passwords and keys, and makes a certificate:

- **Windows:** NetRollout Setup
- **Linux:** `sudo ./bin/install.sh` from the release zip

Both are on the [Releases page](https://github.com/itamar14-byte/NetRollout/releases). See the [installation guide](https://github.com/itamar14-byte/NetRollout/blob/master/docs/install.md).

## Tags

| Tag | Meaning |
| --- | --- |
| `<version>`, for example `1.0.0rc1` | One release. The installer pins the version it belongs to |
| `latest` | The newest final release. Not published for release candidates, so it first appears with 1.0.0 |

The same image also runs Grafana's setup service (`grafana-setup`) in the compose project.

## What it reads

The installer's compose file sets these; you don't normally set them by hand.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | The PostgreSQL connection (or `PG_HOST`, `PG_PORT`, `PG_NAME`, `PG_USER`, `PG_PASSWORD`, `PG_SCHEMA`) |
| `REDIS_URL` | The Redis connection (or `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD`) |
| `SECRET_KEY` | Signs sessions. Required |
| `NETROLLOUT_ENCRYPTION_KEY` | Encrypts stored device credentials. Required; the app refuses to start with a wrong or missing key while encrypted data exists |
| `NETROLLOUT_HTTPS_PORT` | The published HTTPS port, for addresses and redirects |
| `NETROLLOUT_SERVER_IPS` | The server's addresses, added to a generated self-signed certificate |
| `TZ` | The timezone for logs and scheduled tasks |
| `NETROLLOUT_THREADS` | Web server worker threads (default 32, 4 to 256) |
| `NETROLLOUT_DRAIN_SECONDS` | How long running rollouts may finish on stop (default 600) |

Set by the image: `NETROLLOUT_DEPLOYMENT=docker`, `NETROLLOUT_HOME=/data`, `PORT=8080`.

- Runs as the non-root user `netrollout` (uid 10001).
- Listens on port 8080 (plain HTTP, for nginx only).
- Keeps its files in `/data/config`, `/data/certs`, `/data/logs` and `/data/backups`, mounted from the install folder.
- Health check: `/_netrollout/health` (200 when the database and Redis are reachable).
- Runs the database migrations itself at every start.

## Source and license

Source code: <https://github.com/itamar14-byte/NetRollout>. Each image is labelled with its version, and the app's page footer links the source of that version.

NetRollout is free software under the [GNU Affero General Public License v3.0](https://github.com/itamar14-byte/NetRollout/blob/master/LICENSE). It comes as is, without any warranty, to the extent permitted by law. Report bugs through the [issue forms](https://github.com/itamar14-byte/NetRollout/issues/new/choose).
