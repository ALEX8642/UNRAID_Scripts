#!/bin/bash
# Rebuilds the orphan-scan image (cheap/cached after the first run) and runs it.
# Defaults to report-only. Pass --apply to actually delete (file-level only, never a
# directory) — everything not passed --apply just prints a report and touches nothing.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env — copy .env.example to .env and fill in your qBittorrent credentials first."
  exit 1
fi

docker build -q -t orphan-scan . >/dev/null
mkdir -p logs
docker run --rm -it --network host \
  -v "/mnt/docker/plex/Library/Application Support:/plexdata:ro" \
  -v "/mnt/user/Media:/mnt/user/Media" \
  -v "$(pwd)/logs:/logs" \
  --env-file .env \
  orphan-scan "$@"
