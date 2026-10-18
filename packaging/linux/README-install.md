# NetRollout

This folder holds NetRollout for Linux: its scripts (`bin/`), the Docker Compose files and the configuration they mount (`deploy/`). NetRollout itself runs in Docker. Once installed, this folder also holds its settings, certificates, logs and backups.

## Install

```bash
sudo ./bin/install.sh
```

It checks Docker Engine and its Compose plugin, shows the licence, asks for the hostname, HTTPS port, monitoring, certificate and time zone, then starts NetRollout and prints its address. Sign in as `admin` / `admin`; you set a new password at once. For an unattended install, add `--yes` and the answers as flags (see the install guide below).

## Manage

```bash
sudo ./bin/netrollout.sh                  # a numbered menu of the common commands
sudo ./bin/netrollout.sh status           # is everything well? what to do if not
sudo ./bin/netrollout.sh start | stop | open | logs
sudo ./bin/netrollout.sh backup | restore <file>
sudo ./bin/netrollout.sh upgrade --check  # is a newer release out? (upgrade: install it)
sudo ./bin/netrollout.sh help             # every command and option
```

## Documentation

- NetRollout: https://github.com/itamar14-byte/NetRollout
- Installing: https://github.com/itamar14-byte/NetRollout/blob/master/docs/install.md
- Running it (commands, backups, upgrades): https://github.com/itamar14-byte/NetRollout/blob/master/docs/admin-guide.md
- Releases: https://github.com/itamar14-byte/NetRollout/releases

## License

GNU Affero General Public License v3.0: see `LICENSE` in this folder. The source code: https://github.com/itamar14-byte/NetRollout
