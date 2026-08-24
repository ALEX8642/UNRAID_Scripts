#!/bin/bash
# Rebuilds the dupe-review image (cheap/cached after the first run) and runs it interactively.
# Pass --report-only to preview every duplicate group with no prompts and nothing deleted.
# Pass --trash to move deletions to logs/trash/<run> instead of removing them outright.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env — copy .env.example to .env and fill in your Radarr/Sonarr/qBittorrent credentials first."
  exit 1
fi

docker build -q -t plex-dupe-review . >/dev/null
mkdir -p logs
docker run --rm -it --network host \
  -v "/mnt/docker/plex/Library/Application Support:/plexdata:ro" \
  -v "/mnt/user/Media:/mnt/user/Media" \
  -v "$(pwd)/logs:/logs" \
  --env-file .env \
  plex-dupe-review "$@"
