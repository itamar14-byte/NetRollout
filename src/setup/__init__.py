"""The setup core: what installing NetRollout decides and writes, once for
Windows and Linux (docs/plans/stage-9.md).

The host scripts (windows/netrollout.ps1, linux/netrollout.sh) do only what
needs the host — checks, Docker, auto-start — and run this inside the app
image with the install folder mounted (NETROLLOUT_HOME):

  python -m src.setup init  [facts] [answers] [--yes] [--defaults]
  python -m src.setup check [facts] [answers]
  python -m src.setup init --dev            (a developer's .env, from the repo)

Exit codes: 0 done; 1 invalid input (each reason on its own line); 2 refused
(already installed). Every line printed is meant to be shown as it is."""
