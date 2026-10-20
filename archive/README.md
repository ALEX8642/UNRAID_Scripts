# Archive

Files kept for history only. Nothing here is used by the live setup; do not run them.

| File | Why it was retired |
|---|---|
| `dedupe_library.sh` | Deleted the *sorted* copy of a loose/sorted pair by file size. Deleting one name of a hardlinked pair made qBittorrent (path-based completeness) re-download 19+ files (2026-08-25). It was built for the old, disorganised library (loose files vs sorted copies). Replaced by the `mothership/` tools and, for the migration itself, by the dry-run-first scripts in the private `plex-library-cleanup` repo. The script now exits immediately. |
| `docker-compose.windows-dev.yml` | A January 2025 compose file for a Windows/WSL desktop (`/mnt/j/DOCKER/...`), not the Unraid server, which runs these containers from Unraid templates. Unused. |
| `CROSS_DISK_DEDUPE_PLAN.md` | The design note for `mothership/cross-disk-dedupe.sh`, which is implemented. Kept as the rationale. |
