#!/bin/bash
# Rebuilds the dupe-review image (cheap/cached after the first run) and runs it interactively.
# Pass --report-only to preview every duplicate group with no prompts and nothing deleted.
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t plex-dupe-review . >/dev/null
mkdir -p logs
docker run --rm -it --network host \
  -v "/mnt/docker/plex/Library/Application Support:/plexdata:ro" \
  -v "/mnt/user/Media:/mnt/user/Media" \
  -v "$(pwd)/logs:/logs" \
  plex-dupe-review "$@"
