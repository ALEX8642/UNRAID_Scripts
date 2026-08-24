#!/bin/bash
# dedupe-library.sh
#
# Finds titles that exist BOTH as a loose/unsorted file (tracked by qBittorrent, tucked
# outside Radarr/Sonarr's recognized structure) AND as a Radarr/Sonarr-sorted copy in the
# library, and deletes the SORTED copy, keeping the loose one so qBittorrent keeps seeding.
#
# Matching is by exact file size within each app's own root folder tree — the same technique
# validated by hand against this library. A match only counts if it's unique (exactly one
# sorted-tree file of that size); ambiguous matches are skipped and logged, never guessed at.
#
# Safety guard: a match is skipped (protected) if the loose file is BOTH less than
# PROTECT_AGE_DAYS old AND currently associated with an active torrent in qBittorrent — i.e.
# something that just landed and might still be settling, rather than stable old backlog.
#
# Deletion is at individual FILE granularity, never a directory. A TV "unmapped" match is
# often a season-pack folder whose files share a real Season XX/ folder with other episodes
# that must not be touched — only the one matched file is removed. Now-empty directories are
# swept up afterward as a separate, safe pass (nothing but confirmed-empty dirs are removed).
#
# Defaults to DRY RUN (report only, deletes nothing). Pass --apply to actually delete.

set -uo pipefail

# ==== CONFIG — credentials come from the environment (see .env.example); never hardcode
# real values here, this file is committed to a public repo ====
RADARR_URL="${RADARR_URL:-http://localhost:7878}"
RADARR_KEY="${RADARR_KEY:?RADARR_KEY not set — copy .env.example to .env and fill it in}"
SONARR_URL="${SONARR_URL:-http://localhost:8989}"
SONARR_KEY="${SONARR_KEY:?SONARR_KEY not set — copy .env.example to .env and fill it in}"
QBIT_URL="${QBIT_URL:-http://localhost:8080}"
QBIT_USER="${QBIT_USER:-admin}"
QBIT_PASS="${QBIT_PASS:?QBIT_PASS not set — copy .env.example to .env and fill it in}"

# ==== CONFIG — non-secret settings, edit directly for your setup ====
MOVIES_HOST_ROOT="/mnt/user/Media/Movies"
TV_HOST_ROOT="/mnt/user/Media/tv"

MIN_SIZE_BYTES=100000000    # 100MB floor — excludes nfo/sample/subtitle false-positive matches
PROTECT_AGE_DAYS=30         # skip matches newer than this AND still active in qBittorrent

WORKDIR="/tmp/dedupe-library"
LOGFILE="/logs/dedupe-library.log"
# ==== END CONFIG ====

DRY_RUN=1
for arg in "$@"; do
  case "$arg" in
    --apply) DRY_RUN=0 ;;
    --help)
      echo "Usage: $0 [--apply]"
      echo "  (no args)  Dry run — report what would be deleted, delete nothing."
      echo "  --apply    Actually delete the sorted duplicates found."
      exit 0
      ;;
  esac
done

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR" "$(dirname "$LOGFILE")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

# ---- qBittorrent: build an index of every active torrent's content path ----
# Critical safety dependency: is_in_qbit() (the H&R guard) is only as good as this index.
# A silent failure here (bad login, qBittorrent down) must never be mistaken for "nothing is
# in qBittorrent" — that would disable the H&R protection entirely. Hard-abort the whole run
# if this can't be built correctly.
build_qbit_index() {
  local cookie login_http
  cookie=$(mktemp)
  login_http=$(curl -s -o /dev/null -w '%{http_code}' -c "$cookie" -d "username=$QBIT_USER&password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login")
  if [ "$login_http" != "200" ]; then
    rm -f "$cookie"
    log "FATAL: qBittorrent login failed (HTTP $login_http). Refusing to run without a working H&R safety check. Is qBittorrent up and are the credentials in this script still correct?"
    exit 1
  fi
  local torrents_json torrents_http
  torrents_json=$(curl -s -w '\n%{http_code}' -b "$cookie" "$QBIT_URL/api/v2/torrents/info")
  torrents_http=$(printf '%s' "$torrents_json" | tail -1)
  torrents_json=$(printf '%s' "$torrents_json" | sed '$d')
  rm -f "$cookie"
  if [ "$torrents_http" != "200" ]; then
    log "FATAL: qBittorrent torrents/info call failed (HTTP $torrents_http). Refusing to run without a working H&R safety check."
    exit 1
  fi
  # Bare-path rules (no trailing slash) come first: qBittorrent reports a multi-file torrent's
  # content_path as exactly "/movies" or "/tv" (no trailing slash) when "Keep top level folder"
  # is off, and matching only the slash-terminated form silently drops that torrent from the
  # H&R index entirely — confirmed against 8 real torrents (Vikings S01-S05, The Score,
  # Marauders). Same failure mode as to_host_path() in common.py; fixed the same way here.
  printf '%s' "$torrents_json" | jq -r '.[] | .content_path' \
    | sed -e "s|^/movies\$|$MOVIES_HOST_ROOT|" \
          -e "s|^/movies/|$MOVIES_HOST_ROOT/|" \
          -e "s|^/tv\$|$TV_HOST_ROOT|" \
          -e "s|^/tv/|$TV_HOST_ROOT/|" \
          -e "s|^/arr/movies\$|/mnt/user/Media/arr/movies|" \
          -e "s|^/arr/movies/|/mnt/user/Media/arr/movies/|" \
          -e "s|^/arr/shows\$|/mnt/user/Media/arr/shows|" \
          -e "s|^/arr/shows/|/mnt/user/Media/arr/shows/|" \
    > "$WORKDIR/qbit_paths.txt"
  if [ ! -s "$WORKDIR/qbit_paths.txt" ]; then
    log "FATAL: qBittorrent reports zero active torrents — that's almost certainly wrong for this library and would disable the H&R safety check for everything. Refusing to run."
    exit 1
  fi
}

is_in_qbit() {
  local f="$1"
  awk -v want="$f" '$0==want || index(want, $0"/")==1 {found=1} END{exit !found}' "$WORKDIR/qbit_paths.txt"
}

# ---- process one library (movies or tv) ----
process_library() {
  local kind="$1" api_url="$2" api_key="$3" host_root="$4" container_prefix="$5"
  local deleted=0 skipped_ambiguous=0 skipped_protected=0 freed_bytes=0

  log "--- $kind: fetching unmapped (unrecognized) items from ${api_url##*/} ---"
  local rootfolder_json
  rootfolder_json=$(curl -s -w '\n%{http_code}' -H "X-Api-Key: $api_key" "$api_url/api/v3/rootfolder")
  local http_code
  http_code=$(printf '%s' "$rootfolder_json" | tail -1)
  rootfolder_json=$(printf '%s' "$rootfolder_json" | sed '$d')
  if [ "$http_code" != "200" ]; then
    log "ABORT: $kind API call to ${api_url##*/} failed (HTTP $http_code) — is it running? Skipping $kind entirely rather than risk treating this as 'nothing to do'."
    return
  fi
  printf '%s' "$rootfolder_json" | jq -r "[.[0].unmappedFolders[]] | .[].path" \
    | sed "s|^$container_prefix/|$host_root/|" > "$WORKDIR/${kind}_unmapped.txt"
  if [ ! -s "$WORKDIR/${kind}_unmapped.txt" ]; then
    log "ABORT: $kind unmapped-folder list came back empty — treating as a fetch problem, not 'nothing to clean up'. Skipping $kind."
    return
  fi

  log "--- $kind: indexing full library tree (this can take a bit) ---"
  find "$host_root" -type f -printf "%s\t%p\n" > "$WORKDIR/${kind}_all.txt"

  # Split into: loose files (>= size floor) vs sorted files (the rest). A file is loose if
  # either (a) it's under one of Radarr/Sonarr's unmappedFolders, or (b) it's a direct child
  # of the root itself — the unmappedFolders API only ever reports unrecognized *directories*,
  # so a bare scene-named file sitting loose at the library root (no wrapping folder) never
  # appears in that list and must be caught separately.
  awk -F'\t' -v floor="$MIN_SIZE_BYTES" -v root="$host_root" -v loosef="$WORKDIR/${kind}_loose.txt" -v sortedf="$WORKDIR/${kind}_sorted.txt" '
    NR==FNR { unmapped[$0]=1; next }
    {
      size=$1; path=$2; under=0;
      for (d in unmapped) { if (index(path, d "/") == 1) { under=1; break } }
      if (!under && index(path, root "/") == 1) {
        rest = substr(path, length(root) + 2);
        if (index(rest, "/") == 0) under = 1;
      }
      if (under) { if (size+0 >= floor) print $0 >> loosef; }
      else { print $0 >> sortedf; }
    }
  ' "$WORKDIR/${kind}_unmapped.txt" "$WORKDIR/${kind}_all.txt"

  [ -f "$WORKDIR/${kind}_loose.txt" ] || { log "--- $kind: nothing loose found, skipping ---"; return; }

  while IFS=$'\t' read -r size loosefile; do
    matches=$(awk -F'\t' -v sz="$size" '$1==sz {print $2}' "$WORKDIR/${kind}_sorted.txt" 2>/dev/null)
    match_count=$(printf '%s\n' "$matches" | grep -c . || true)

    if [ "$match_count" -ne 1 ]; then
      log "SKIP (ambiguous, $match_count matches) $loosefile"
      skipped_ambiguous=$((skipped_ambiguous + 1))
      continue
    fi
    sorted_file="$matches"

    mtime=$(stat -c '%Y' "$loosefile" 2>/dev/null || echo 0)
    age_days=$(( ( $(date +%s) - mtime ) / 86400 ))

    if [ "$age_days" -lt "$PROTECT_AGE_DAYS" ] && is_in_qbit "$loosefile"; then
      log "SKIP (protected: ${age_days}d old, active in qBittorrent) $loosefile"
      skipped_protected=$((skipped_protected + 1))
      continue
    fi

    # Hard safety check, no age exception: never delete a file qBittorrent is actively
    # seeding from, regardless of how the match was derived. Guards against an H&R even if
    # the loose/sorted distinction above is ever wrong.
    if is_in_qbit "$sorted_file"; then
      log "ABORT-SKIP (sorted_file is itself an active qBittorrent path — refusing to delete): $sorted_file"
      skipped_protected=$((skipped_protected + 1))
      continue
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
      log "WOULD DELETE: $sorted_file  (keep loose: $loosefile, ${age_days}d old)"
    else
      rm -f -- "$sorted_file"
      log "DELETED: $sorted_file  (kept loose: $loosefile, ${age_days}d old)"
    fi
    deleted=$((deleted + 1))
    freed_bytes=$((freed_bytes + size))
  done < "$WORKDIR/${kind}_loose.txt"

  if [ "$DRY_RUN" -eq 0 ]; then
    find "$host_root" -mindepth 1 -type d -empty -delete 2>/dev/null || true
  fi

  log "--- $kind summary: $deleted deleted, $skipped_ambiguous skipped (ambiguous), $skipped_protected skipped (protected), $(( freed_bytes / 1024 / 1024 / 1024 )) GB $( [ "$DRY_RUN" -eq 1 ] && echo would-be-freed || echo freed ) ---"
}

main() {
  log "=== dedupe-library run started $( [ "$DRY_RUN" -eq 1 ] && echo '(DRY RUN — nothing will be deleted)' || echo '(APPLY MODE)' ) ==="
  build_qbit_index
  process_library "movies" "$RADARR_URL" "$RADARR_KEY" "$MOVIES_HOST_ROOT" "/movies"
  process_library "tv" "$SONARR_URL" "$SONARR_KEY" "$TV_HOST_ROOT" "/tv"
  log "=== dedupe-library run finished ==="
  if [ "$DRY_RUN" -eq 1 ]; then
    log "This was a dry run. Review the log above, then re-run with --apply to actually delete."
  else
    log "Remember: Plex needs a library scan + Empty Trash to stop showing these as duplicates."
  fi
}

main
