#!/bin/bash
# Empties quarantine day-folders older than 7 days (everything the mothership tools "removed" waits there first). Daily.
cd /mnt/user/system/scripts/mothership || exit 1
OUT=$(docker run --rm -v /mnt:/mnt --entrypoint python3 plex-mothership /app/quarantine.py purge --days 7 --apply 2>&1)
echo "$OUT"; echo "$(date '+%F %T') $OUT" >> logs/quarantine.log
