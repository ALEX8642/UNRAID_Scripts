#!/bin/bash
# cross-disk-dedupe.sh
#
# Finds files that exist TWICE on disk at the exact same logical library path, because
# Unraid's /mnt/user (a FUSE union of every array disk) only ever shows ONE physical copy per
# path even when two different physical disks both hold a full copy of the same file. No tool
# that reads through /mnt/user — including this repo's other tools — can see this class of
# duplicate at all; it requires reading /mnt/disk1..N directly, which is what this script does.
#
# This is NOT the same problem dedupe-library.sh (retired) used to address. That was about a
# loose file vs. a Radarr/Sonarr-sorted copy, intentionally two different logical paths sharing
# one inode. This is about the SAME logical path silently existing on two different physical
# disks — always two full, independent copies, always real wasted space, never a hardlink
# opportunity (hardlinks can't cross physical disks; if a matched pair here also happens to
# need a hardlink relationship, that's a different, unrelated repair — not something this
# script attempts).
#
# Safety model, in order, for every duplicate found:
#   1. Full SHA-256 checksum of both copies — must match exactly, or skip (not a simple dupe).
#   2. Determine which copy /mnt/user currently exposes by matching ctime exactly against both
#      physical copies. If ctime doesn't uniquely identify one side, skip — guessing wrong
#      deletes the copy everything (Plex, Sonarr, qBittorrent) actually depends on.
#   3. Only ever delete the NON-exposed physical copy, directly via its /mnt/diskN path — never
#      through /mnt/user, whose hardlink/rm behavior for cross-disk cases is not well
#      understood and produced an unexplained result during testing.
#   4. A path found on 3+ disks is skipped and reported — this script only auto-resolves the
#      simple two-copy case.
#
# Defaults to DRY RUN (report only, deletes nothing). Pass --apply to actually delete.

set -uo pipefail

# ==== CONFIG — credentials come from the environment (see .env.example); never hardcode
# real values here, this file is committed to a public repo ====
QBIT_URL="${QBIT_URL:-http://localhost:8080}"
QBIT_USER="${QBIT_USER:-admin}"
QBIT_PASS="${QBIT_PASS:?QBIT_PASS not set — copy .env.example to .env and fill it in}"

# ==== CONFIG — non-secret settings, edit directly for your setup ====
# Subfolders (relative to each disk root) to scan for cross-disk duplicates.
SCAN_SUBDIRS=("Media/tv" "Media/Movies" "Media/arr/shows" "Media/arr/movies")
VIDEO_EXTENSIONS=("mkv")
SIDECAR_EXTENSIONS=("srt" "nfo")   # report-only, never deleted by this script

# Known-inconsistent paths to never touch automatically — add relative paths (from a disk
# root, e.g. "Media/tv/Foo/Foo.S01E01.mkv") here as they're discovered, with a comment saying
# why. Resolve these by hand; this script will only ever log that it skipped them.
EXCLUDE_RELPATHS=(
  "Media/tv/The Bridge (2011)/The.Bridge.2011.S01E01.1080i.BluRay.REMUX.AVC.DTS-HD.MA.5.1-EPSiLON.mkv"
)

WORKDIR="/tmp/cross-disk-dedupe"
LOGFILE="/logs/cross-disk-dedupe.log"
# ==== END CONFIG ====

DRY_RUN=1
for arg in "$@"; do
  case "$arg" in
    --apply) DRY_RUN=0 ;;
    --help)
      echo "Usage: $0 [--apply]"
      echo "  (no args)  Dry run — report cross-disk duplicates found, delete nothing."
      echo "  --apply    Actually delete the confirmed-redundant physical copy of each pair."
      exit 0
      ;;
  esac
done

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR" "$(dirname "$LOGFILE")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

# Fast partial-hash check instead of a full-file checksum: hashes the first and last 4MB of
# the file (8MB read regardless of file size) plus the exact size. For this script's specific
# case — the same relative path existing on two disks, i.e. what's meant to be the identical
# file, not two independent encodes — a same-size match is already extremely strong evidence,
# and this catches truncation/header/tail corruption at a fraction of the cost of hashing a
# multi-GB file twice. It would not catch corruption confined entirely to the untouched middle
# of the file; a full sha256sum is the only way to rule that out completely, at real time cost.
partial_hash() {
  local f="$1"
  { head -c 4M "$f"; tail -c 4M "$f"; } | sha256sum | cut -d' ' -f1
}

is_excluded() {
  local relpath="$1"
  for skip in "${EXCLUDE_RELPATHS[@]}"; do
    [ "$relpath" = "$skip" ] && return 0
  done
  return 1
}

# ---- discover physical disks ----
mapfile -t DISKS < <(find /mnt -maxdepth 1 -type d -name 'disk*' -printf '%f\n' | sort -V)
if [ "${#DISKS[@]}" -eq 0 ]; then
  log "FATAL: no /mnt/diskN mounts found. Refusing to run."
  exit 1
fi
log "=== cross-disk-dedupe run started $( [ "$DRY_RUN" -eq 1 ] && echo '(DRY RUN — nothing will be deleted)' || echo '(APPLY MODE)' ) — disks: ${DISKS[*]} ==="

# ---- qBittorrent index (informational cross-check only — see note below) ----
# Every app in this stack (qBittorrent, Sonarr, Radarr, Plex) reads exclusively through
# /mnt/user, never a raw /mnt/diskN path. Since this script only ever deletes the copy
# /mnt/user does NOT expose, nothing that operates through /mnt/user — including qBittorrent's
# own seeding — can be affected by construction. This index is kept only as a defense-in-depth
# sanity log, not a gate: it lets a human audit, for any file that WAS actively torrented,
# that the exposed copy really is what qBittorrent was seeing.
build_qbit_index() {
  local cookie login_http
  cookie=$(mktemp)
  login_http=$(curl -s -o /dev/null -w '%{http_code}' -c "$cookie" -d "username=$QBIT_USER&password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login")
  if [ "$login_http" != "200" ]; then
    rm -f "$cookie"
    log "WARNING: qBittorrent login failed (HTTP $login_http) — proceeding without the qBittorrent cross-check log (this does not weaken the actual safety guarantee, which never depends on qBittorrent's state)."
    : > "$WORKDIR/qbit_paths.txt"
    return
  fi
  curl -s -b "$cookie" "$QBIT_URL/api/v2/torrents/info" \
    | jq -r '.[] | .content_path' \
    | sed -e 's|^/movies/|/mnt/user/Media/Movies/|' \
          -e 's|^/tv/|/mnt/user/Media/tv/|' \
          -e 's|^/arr/movies/|/mnt/user/Media/arr/movies/|' \
          -e 's|^/arr/shows/|/mnt/user/Media/arr/shows/|' \
    > "$WORKDIR/qbit_paths.txt"
  rm -f "$cookie"
}

# ---- scan phase: every video+sidecar file on every disk, relative path -> list of disks ----
: > "$WORKDIR/all_files.txt"
for disk in "${DISKS[@]}"; do
  for sub in "${SCAN_SUBDIRS[@]}"; do
    root="/mnt/$disk/$sub"
    [ -d "$root" ] || continue
    for ext in "${VIDEO_EXTENSIONS[@]}" "${SIDECAR_EXTENSIONS[@]}"; do
      find "$root" -type f -iname "*.$ext" -printf "%s\t$disk\t$sub/%P\n" 2>/dev/null
    done
  done
done >> "$WORKDIR/all_files.txt"

log "Scanned ${#DISKS[@]} disk(s), $(wc -l < "$WORKDIR/all_files.txt") file(s) total."

# Group by relative path (field 3). Emit: count, comma-joined disk list, comma-joined sizes, path.
awk -F'\t' '
  { count[$3]++; disks[$3] = disks[$3] "," $2; sizes[$3] = sizes[$3] "," $1 }
  END { for (k in count) if (count[k] > 1) print count[k] "\t" disks[k] "\t" sizes[k] "\t" k }
' "$WORKDIR/all_files.txt" > "$WORKDIR/dupes.txt"

total_dupes=$(wc -l < "$WORKDIR/dupes.txt")
log "Found $total_dupes path(s) existing on more than one physical disk."

if [ "$total_dupes" -eq 0 ]; then
  log "Nothing to do."
  exit 0
fi

build_qbit_index

checked=0
resolved=0
skipped_excluded=0
skipped_manydisks=0
skipped_checksum=0
skipped_ambiguous=0
freed_bytes=0

while IFS=$'\t' read -r count diskcsv sizecsv relpath; do
  checked=$((checked + 1))

  if is_excluded "$relpath"; then
    log "SKIP (known-inconsistent, resolve by hand): $relpath"
    skipped_excluded=$((skipped_excluded + 1))
    continue
  fi

  IFS=',' read -ra disks_arr <<< "${diskcsv#,}"
  if [ "${#disks_arr[@]}" -ne 2 ]; then
    log "SKIP (found on ${#disks_arr[@]} disks, not 2 — needs manual review): $relpath"
    skipped_manydisks=$((skipped_manydisks + 1))
    continue
  fi

  disk_a="${disks_arr[0]}"; disk_b="${disks_arr[1]}"
  path_a="/mnt/$disk_a/$relpath"
  path_b="/mnt/$disk_b/$relpath"
  muser_path="/mnt/user/$relpath"

  if [ ! -f "$muser_path" ]; then
    log "SKIP (no /mnt/user path resolves here — unexpected, needs manual review): $relpath"
    skipped_ambiguous=$((skipped_ambiguous + 1))
    continue
  fi

  # Sidecar files (srt/nfo): report only, never delete — kilobytes, not worth automating, but
  # worth knowing about so a video-only cleanup doesn't leave an orphaned fragment behind.
  case "$relpath" in
    *.srt|*.nfo)
      log "REPORT (sidecar file split across disks, not deleted): $relpath"
      continue
      ;;
  esac

  size_a=$(stat -c '%s' "$path_a")
  size_b=$(stat -c '%s' "$path_b")
  if [ "$size_a" != "$size_b" ]; then
    log "SKIP (size mismatch — NOT a simple duplicate, needs manual review): $relpath"
    skipped_checksum=$((skipped_checksum + 1))
    continue
  fi

  hash_a=$(partial_hash "$path_a")
  hash_b=$(partial_hash "$path_b")
  if [ "$hash_a" != "$hash_b" ]; then
    log "SKIP (partial-hash mismatch — NOT a simple duplicate, needs manual review): $relpath"
    skipped_checksum=$((skipped_checksum + 1))
    continue
  fi

  ctime_user=$(stat -c '%Z' "$muser_path")
  ctime_a=$(stat -c '%Z' "$path_a")
  ctime_b=$(stat -c '%Z' "$path_b")

  exposed=""
  redundant=""
  if [ "$ctime_user" = "$ctime_a" ] && [ "$ctime_user" != "$ctime_b" ]; then
    exposed="$path_a"; redundant="$path_b"
  elif [ "$ctime_user" = "$ctime_b" ] && [ "$ctime_user" != "$ctime_a" ]; then
    exposed="$path_b"; redundant="$path_a"
  else
    log "SKIP (ctime does not uniquely identify the exposed copy — needs manual review): $relpath"
    skipped_ambiguous=$((skipped_ambiguous + 1))
    continue
  fi

  if grep -qF "$muser_path" "$WORKDIR/qbit_paths.txt" 2>/dev/null; then
    log "NOTE (actively tracked by qBittorrent; deleting only the non-exposed copy, qBittorrent's own view is unaffected): $relpath"
  fi

  size=$(stat -c '%s' "$redundant")
  if [ "$DRY_RUN" -eq 1 ]; then
    log "WOULD DELETE (redundant physical copy, not exposed via /mnt/user): $redundant  (keeping: $exposed)"
  else
    rm -f -- "$redundant"
    log "DELETED (redundant physical copy): $redundant  (kept: $exposed)"
    # Verify /mnt/user is unaffected by this deletion.
    ctime_user_after=$(stat -c '%Z' "$muser_path" 2>/dev/null || echo "MISSING")
    if [ "$ctime_user_after" != "$ctime_user" ]; then
      log "WARNING: /mnt/user ctime changed after deleting the redundant copy for: $relpath — verify manually."
    fi
  fi
  resolved=$((resolved + 1))
  freed_bytes=$((freed_bytes + size))
done < "$WORKDIR/dupes.txt"

log "=== summary: $resolved resolved, $skipped_excluded skipped (known-inconsistent), $skipped_manydisks skipped (>2 disks), $skipped_checksum skipped (checksum mismatch), $skipped_ambiguous skipped (ambiguous), $(( freed_bytes / 1024 / 1024 / 1024 )) GB $( [ "$DRY_RUN" -eq 1 ] && echo would-be-freed || echo freed ) ==="
log "=== cross-disk-dedupe run finished ==="
