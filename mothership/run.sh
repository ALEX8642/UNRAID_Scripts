#!/bin/bash
# Single entry point for all three library-maintenance tools. Rebuilds the image
# (cheap/cached after the first run) and launches the interactive menu.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env — copy .env.example to .env and fill in your credentials first."
  exit 1
fi

docker build -q -t plex-mothership . >/dev/null
mkdir -p logs

# Only request a TTY when one actually exists — a real interactive shell has one (menu or a
# subcommand you're typing yourself), but cron/scripted invocations (e.g. a scheduled
# "./run.sh dedupe --apply") have none, and `-it` fails outright without one.
DOCKER_STDIN_FLAGS="-i"
if [ -t 0 ]; then
  DOCKER_STDIN_FLAGS="-it"
fi

docker run --rm $DOCKER_STDIN_FLAGS --network host \
  -v "/mnt/docker/plex/Library/Application Support:/plexdata:ro" \
  -v "/mnt/user/Media:/mnt/user/Media" \
  -v "$(pwd)/logs:/logs" \
  --env-file .env \
  plex-mothership "$@"
