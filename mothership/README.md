
## Added 2026-10-20

- `hnr_guard.py` — unmonitors library files that are still a young seed (torrent lives in the library, not in `arr/`) so Radarr/Sonarr never delete them on upgrade inside a hit-and-run window; re-monitors them when the window passes. Dry-run by default (`--apply` to act). State: `logs/hnr_guard_state.json`. Run: `docker run --rm --network host --env-file .env -v $PWD/hnr_guard.py:/app/hnr_guard.py -v $PWD/logs:/logs --entrypoint python3 plex-mothership /app/hnr_guard.py [--apply]`.
- `fix-library-perms.sh` — Radarr/Sonarr run as `nobody` (PUID 99); any library directory created as root blocks their deletes/upgrades. This sets `nobody:users`, dirs 777, files rw on the raw disks (dry-run default, `--apply`). Any script that creates library directories must do the same.
