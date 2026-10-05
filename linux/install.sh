#!/usr/bin/env bash
# NetRollout: install it in the folder this was extracted to.
#   sudo ./linux/install.sh            (unattended: add --yes, and the answers)
exec "$(dirname "$(readlink -f "$0")")/netrollout.sh" install "$@"
