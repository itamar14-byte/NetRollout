#!/bin/sh
# Run by the official entrypoint before nginx starts: put the first site in
# place (waiting for the certificate if needed), then keep watching for
# changes in the background.
set -e
netrollout-watcher.sh boot
netrollout-watcher.sh watch &
