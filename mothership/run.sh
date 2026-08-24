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
docker run --rm -it --network host \
  -v "/mnt/docker/plex/Library/Application Support:/plexdata:ro" \
  -v "/mnt/user/Media:/mnt/user/Media" \
  -v "$(pwd)/logs:/logs" \
  --env-file .env \
  plex-mothership "$@"
