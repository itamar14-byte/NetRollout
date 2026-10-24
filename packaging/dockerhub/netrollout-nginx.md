# NetRollout nginx

**[Website](https://itamar14-byte.github.io/NetRollout/)** · **[Documentation](https://itamar14-byte.github.io/NetRollout/docs/)** · **[Source on GitHub](https://github.com/itamar14-byte/NetRollout)**

The entry point of [NetRollout](https://github.com/itamar14-byte/NetRollout), a web app that pushes configuration to many network devices at once and verifies it. This image is nginx in front of the [NetRollout app image](https://hub.docker.com/r/itamarweinstein/netrollout): it serves HTTPS, redirects HTTP, and applies a new hostname or certificate without a restart.

## How to use this image

This image isn't meant to be run on its own. It is one service of NetRollout's Docker Compose project, which its installer sets up:

- **Windows:** NetRollout Setup
- **Linux:** `sudo ./bin/install.sh` from the release zip

Both are on the [Releases page](https://github.com/itamar14-byte/NetRollout/releases). See the [installation guide](https://github.com/itamar14-byte/NetRollout/blob/master/docs/install.md).

## Tags

| Tag | Meaning |
| --- | --- |
| `<version>`, for example `1.0.0` | One release, always used with the app image of the same version |
| `latest` | The newest final release (never a release candidate) |

## What it does

- Based on `nginx:1.30-alpine`.
- Listens on 443 (HTTPS) and 80 (redirects to `https://<hostname>:<port>`). Compose publishes 443 on the port chosen at install, and 80 only when it was free.
- Requests for another name than the configured hostname are redirected to it; IP addresses and `localhost` are served as they are.
- `/rollout/stream/` (the live rollout log) is unbuffered; `/metrics` answers 404.
- Grafana, when monitoring is on, is served at `/grafana/` to signed-in NetRollout admins only.
- A watcher checks for a changed hostname, port or certificate every 3 seconds. A change is rendered and tested with `nginx -t` before it replaces the running site; a rejected change leaves the last good site serving.

## What it reads

| Variable | Default | Purpose |
| --- | --- | --- |
| `NETROLLOUT_HOSTNAME` | (empty) | The canonical hostname |
| `NETROLLOUT_HTTPS_PORT` | `443` | The port people type, for redirects |
| `APP_UPSTREAM` | `app:8080` | The NetRollout app |
| `NETROLLOUT_WATCH_INTERVAL` | `3` | Seconds between the watcher's checks |

In a NetRollout install the hostname and port come from `site.env`, which the app writes, and override the first two.

| Mount | Contents |
| --- | --- |
| `/etc/nginx/certs` | `fullchain.pem` and `privkey.pem` |
| `/etc/nginx/netrollout/shared` | `site.env` in, the watcher's `status.json` out |

## Source and license

Source code: <https://github.com/itamar14-byte/NetRollout> (the image is built from `deploy/nginx/`).

NetRollout is free software under the [GNU Affero General Public License v3.0](https://github.com/itamar14-byte/NetRollout/blob/master/LICENSE). It comes as is, without any warranty, to the extent permitted by law. The base image, nginx, has its own license.
