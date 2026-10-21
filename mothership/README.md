# mothership: Plex library maintenance tools

Everything here is **dry-run by default** and never writes through `/mnt/user` (Unraid's FUSE layer); destructive actions resolve to the physical `/mnt/diskN` path. Full background is in the repo root `README.md`. Credentials live in `.env` (copy `.env.example`; never commit it).

| Tool | What it does | How to run |
|---|---|---|
| `run.sh` / `mothership.py` | Menu that launches the tools below inside the `plex-mothership` image | `./run.sh` |
| `cross-disk-dedupe.sh` | Finds the same file stored on two array disks at one library path | `./cross-disk-dedupe.sh` (report), `--apply` to remove the extra copy |
| `dupe_review.py` | Review titles that exist as more than one real file (quality/source); writes a plan to `logs/`, nothing is deleted until you confirm | `mothership.py dupe-review [--report-only] [--apply]`, `--execute PLAN [--apply]`, `--reconcile` |
| `orphan_scan.py` | Lists video files that neither Plex nor qBittorrent knows about (report only unless `--apply`; never touches hardlinked files) | `mothership.py orphan-scan [--apply]` |
| `tag-tracker-issues.sh` | Tags torrents with tracker problems (trumped, deleted, unregistered, auth) so you can filter them in qBittorrent; only adds/removes tags | nightly User Script `mothership_tag_tracker_issues`; `--apply` to write |
| `hnr_guard.py` | Unmonitors a Radarr movie / Sonarr episode while its file is a library-owned seed that has not met the tracker rule (BHD: 120 h or 1:1), so an upgrade cannot delete it; re-monitors when the rule is met | User Script `mothership_hnr_guard` (set a daily schedule); manual: `docker run --rm --network host --env-file .env -v $PWD/hnr_guard.py:/app/hnr_guard.py -v $PWD/logs:/logs --entrypoint python3 plex-mothership /app/hnr_guard.py [--apply]`. Extra torrents to protect: one name fragment per line in `hnr_extra.txt` |
| `fix-library-perms.sh` | Sets library directories to `nobody:users` 777 and files rw on the raw disks (Radarr/Sonarr run as `nobody`; root-owned folders block their deletes and upgrades) | `./fix-library-perms.sh` (dry run), `--apply`. Any script that creates library folders must do the same |
| `library_health.sh` | Read-only health check: tracked files missing on disk (Radarr and Sonarr), torrents in missingFiles/error, blocked import-queue items, untracked videos in Movies, nearly full disks. Notifies through Unraid's notification system only when something is wrong (plus an all-clear on Sundays); optional Discord via `DISCORD_WEBHOOK` in `.env` | User Script `mothership_library_health` (daily); manual: `./library_health.sh [--no-notify]` |
| `seed_cleanup.py` | Removes qBittorrent torrents whose data Radarr/Sonarr deleted on purpose in an upgrade (state `missingFiles`). Every check must pass: content absent on all raw disks, an "Upgrade" deletion in Radarr/Sonarr history for that file, tracker rule met (120 h or 1:1), seen missing on two runs 20+ h apart, and no more than 5 at once (circuit breaker). Removes the torrent only (`deleteFiles=false`). Dry-run by default | User Script `mothership_seed_cleanup` (daily, applies); manual: `docker run --rm --network host --env-file .env -v /mnt:/mnt:ro -v $PWD/seed_cleanup.py:/app/seed_cleanup.py -v $PWD/logs:/logs --entrypoint python3 plex-mothership /app/seed_cleanup.py [--apply]`. Tests: `python3 -m unittest tests.test_seed_cleanup` |
| `safe_ops.py` | Shared safety layer used by the tools above (raw-disk unlinks, seeds handled through qBittorrent) | library, not run directly; tests: `python3 -m unittest discover -s tests` |

Kometa poster helpers are documented in `../kometa/README.md`.

## Scheduling gotcha (User Scripts plugin)

The plugin reads `/tmp/user.scripts/schedule.json`, a copy made at boot from `/boot/config/plugins/user.scripts/schedule.json`. Editing the `/boot` file by hand does **not** reach cron until the copy is refreshed (`cp` the boot file over the tmp one, or save the schedule once in the plugin UI). Scripts added by hand and scheduled this way silently never run. After scheduling, confirm with `ls /tmp/user.scripts/finished` the next morning.
