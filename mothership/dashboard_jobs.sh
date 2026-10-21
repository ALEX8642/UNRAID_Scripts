#!/bin/bash
# Host-side runner for jobs queued by the Plex Dashboard (file removal, poster reset). Runs every minute from User Scripts;
# exits at once when nothing is pending. All the safety logic lives in job_runner.py / safe_ops.py.
J=/mnt/cache/appdata/plex-dashboard/jobs
ls "$J"/pending/*.json >/dev/null 2>&1 || exit 0
exec 9>/tmp/dashboard_jobs.lock; flock -n 9 || exit 0
cd /mnt/user/system/scripts/mothership || exit 1
docker run --rm --network host --env-file .env -v /mnt:/mnt -v "$J":/jobs -v "$PWD/logs":/logs -v /mnt/docker/kometa:/kometa \
  --entrypoint python3 plex-mothership /app/job_runner.py
