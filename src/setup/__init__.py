"""The setup core: what installing NetRollout decides and writes, once for
Windows and Linux (docs/plans/stage-9.md).

The host scripts (windows/netrollout.ps1, linux/netrollout.sh) do only what
needs the host — checks, Docker, auto-start — and run this inside the app
image with the install folder mounted (NETROLLOUT_HOME):

  python -m src.setup init  --licence-accepted [facts] [answers] [--defaults]
  python -m src.setup prepare-start | status  [facts] (the scripts, before
                                     a start / for `netrollout status`)
  python -m src.setup check [facts] [answers]
  python -m src.setup init --dev            (a developer's .env, from the repo)

The licence notice is the scripts' (shown before Docker is even installed);
init only records that it was accepted.

Exit codes: 0 done (status: all well); 1 invalid input, each reason on its
own line (status: something is wrong — the report says what to do); 2
refused (already installed). Every line printed is meant to be shown as it is,
in plain ASCII."""
