# Roadmap

This page lists what may come after NetRollout 1.0.0, for users deciding whether to adopt it and for contributors looking for something to work on.

> [!NOTE]
> These are intentions, not promises. NetRollout has one maintainer; the order and the contents can change, and items can be dropped. Every 1.x release is a minor or patch release of 1.x (*Anycast*): no breaking changes. See [CONTRIBUTING.md](../CONTRIBUTING.md#versioning) for how versions are numbered.

## 1.1: Deep Diff

Today, Verify reads each device's configuration after the push and checks every command against it, section by section. That is fast (one connection per device) and catches most problems, but it can't tell two things apart:

- A line that was already in the configuration before the rollout still counts as verified.
- Typed abbreviations (`int gi0/1`) and values the device rewrites (hashed secrets, reformatting, hidden defaults) can show as not configured.

Deep Diff compares the configuration **before and after** the push instead:

- A **Deep Diff** option on New Rollout, next to Verify (available only when Verify is on). It fetches the configuration before the push and again after it, so it takes longer.
- Results store both configurations and show a full before/after diff. Without Deep Diff, the current commands-versus-configuration view stays.
- The comparison is hierarchy-aware, using [hier_config](https://github.com/netdevops/hier_config), with [netutils](https://github.com/networktocode/netutils) normalising names such as interface abbreviations. Junos can use its own commit diff (`show | compare rollback 1`).
- The pass/fail result stays: a command that appears in the diff came from this push.

## Later in 1.x

In no particular order.

**Rollouts and results**

- **"Explain this failure"** (optional): a small local AI model on the server that, on request, suggests what went wrong on a failed device. Advisory only: it never decides pass or fail, and nothing leaves the server.
- **Email notifications**: one mail server setting (with a test button) for every notice: an access request waiting, a rollout finished or failed, ACTION NEEDED on a device, a failed scheduled backup or nightly clean-up. Never a password.

**Monitoring**

- **Grafana for operators**: today Grafana is for admins only, because the free edition lets anyone signed in query every data source. The plan is a separate Grafana organisation for operators that sees only aggregates (counts, rates), never commands, logs or the audit log.
- **Your own Grafana**: connecting an existing Grafana instead of the bundled one.

**Installing and running**

- **Signed Windows files**: code signing for NetRollout Setup, NetRollout Manager and the CLI, so Windows stops warning about an unknown publisher.
- **Linux packages**: `.deb` and `.rpm` packages and a package repository, so NetRollout installs with `apt` / `dnf` and updates with the system. Today's zip and `install.sh` stay complete on their own.
- **macOS hosts**: Docker Desktop for Mac runs the same stack; this needs multi-architecture images (amd64 and arm64, also useful for ARM Linux servers) and a portable Linux script.
- **Isolated networks**: the web pages' third-party assets (Bootstrap, fonts, icons) served from the image instead of a CDN, and moving the images in offline.
- **Phones, read-only** (if people ask for it): sign-in, the dashboard, Active Jobs, the live log and Results usable on a phone, for watching a rollout on call. Editing stays desktop-only.

**Security**

- **Device host keys**: verify each device's SSH host key (trust on first use, with a page to see, accept and reset them), so a device that changes its key is noticed. Today NetRollout connects without checking it, like most automation built on Netmiko.
- **Content-Security-Policy and asset integrity**: a Content-Security-Policy header and integrity hashes for every script and stylesheet, which pairs with serving the third-party assets from the image (see *Isolated networks*).

**Code**

- **Frontend assets in files**: the pages' inline CSS and JavaScript moved to `static/css/` and `static/js/`.
- **A connector abstraction**: NetRollout talks to devices through Netmiko only. An interface for choosing between backends will be added when a second backend is actually being written, not before.

## Asking for a feature

Open a [feature request](https://github.com/itamar14-byte/NetRollout/issues/new/choose). Describe the task you're trying to do, not only the solution you picture. For a new device platform, give the Netmiko device type and the OS version.

A request for something listed here is still useful: it shows what people need first. A pull request is even better; see [CONTRIBUTING.md](../CONTRIBUTING.md).
