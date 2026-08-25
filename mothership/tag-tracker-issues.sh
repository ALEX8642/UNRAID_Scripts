#!/bin/bash
# tag-tracker-issues.sh
#
# Scans every torrent's tracker status and syncs a qBittorrent tag describing any real
# tracker-reported problem, so they can be sorted/filtered in the WebUI. Never touches files,
# never removes a torrent, never changes seeding behavior — only ever adds/removes these tags.
#
# Fully synced each run, safe for a recurring cron: a torrent gets the tag if it currently has
# that problem, and loses the tag if it no longer does (e.g. Sonarr/Radarr upgraded a "trumped"
# release, or a tracker-auth issue got fixed). Without this, tags would only ever accumulate
# and a stale tag would be indistinguishable from a current one.
#
# Categories (checked in this order, first match wins per torrent):
#   trumped        - tracker says a better release now supersedes this one (BHD "Trumped: ...")
#   tracker-deleted   - tracker says the torrent itself has been deleted
#   tracker-missing   - tracker doesn't recognize this torrent at all (unregistered / not registered)
#   tracker-auth      - an authentication problem (passkey rejected) — likely an ACCOUNT-level
#                        issue on that tracker, not specific to this one torrent; investigate the
#                        tracker account directly rather than assuming these torrents are dead
#   tracker-issue     - any other tracker status-4 message not matching the above, so nothing
#                        with a real problem is silently skipped
#
# A torrent whose tracker is merely unreachable (status 1/2/3, or a client-side message like
# "skipping tracker announce" / "timed out") is NOT tagged — those aren't tracker-reported
# problems, just connectivity state, and tagging them would just be noise.
#
# Defaults to DRY RUN (reports what would be tagged, tags nothing). Pass --apply to actually
# add the tags.

set -uo pipefail

# ==== CONFIG — credentials come from the environment (see .env.example); never hardcode
# real values here, this file is committed to a public repo ====
QBIT_URL="${QBIT_URL:-http://localhost:8080}"
QBIT_USER="${QBIT_USER:-admin}"
QBIT_PASS="${QBIT_PASS:?QBIT_PASS not set — copy .env.example to .env and fill it in}"

WORKDIR="/tmp/tag-tracker-issues"
LOGFILE="/logs/tag-tracker-issues.log"
# ==== END CONFIG ====

DRY_RUN=1
for arg in "$@"; do
  case "$arg" in
    --apply) DRY_RUN=0 ;;
    --help)
      echo "Usage: $0 [--apply]"
      echo "  (no args)  Dry run — report which torrents would be tagged, tag nothing."
      echo "  --apply    Actually add the tags."
      exit 0
      ;;
  esac
done

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR" "$(dirname "$LOGFILE")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

COOKIE=$(mktemp)
login_http=$(curl -s -o /dev/null -w '%{http_code}' -c "$COOKIE" -d "username=$QBIT_USER&password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login")
if [ "$login_http" != "200" ]; then
  log "FATAL: qBittorrent login failed (HTTP $login_http)."
  rm -f "$COOKIE"
  exit 1
fi

log "=== tag-tracker-issues run started $( [ "$DRY_RUN" -eq 1 ] && echo '(DRY RUN — nothing will be tagged)' || echo '(APPLY MODE)' ) ==="

HASHES=$(curl -s -b "$COOKIE" "$QBIT_URL/api/v2/torrents/info" | jq -r '.[].hash')
total=$(echo "$HASHES" | wc -l)
log "Checking $total torrents..."

: > "$WORKDIR/trumped.txt"
: > "$WORKDIR/tracker-deleted.txt"
: > "$WORKDIR/tracker-missing.txt"
: > "$WORKDIR/tracker-auth.txt"
: > "$WORKDIR/tracker-issue.txt"

checked=0
for h in $HASHES; do
  checked=$((checked + 1))
  (( checked % 500 == 0 )) && log "... checked $checked/$total so far"

  # One torrent can have multiple trackers; take the first real status-4 message found.
  msg=$(curl -s -b "$COOKIE" "$QBIT_URL/api/v2/torrents/trackers?hash=$h" \
    | jq -r '.[] | select(.url | test("^http")) | select(.status==4) | .msg' \
    | grep -v '^$' | head -1)

  [ -z "$msg" ] && continue

  lower=$(echo "$msg" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    *trumped*)                            echo "$h" >> "$WORKDIR/trumped.txt" ;;
    *deleted*)                            echo "$h" >> "$WORKDIR/tracker-deleted.txt" ;;
    *unregistered*|*not\ registered*)     echo "$h" >> "$WORKDIR/tracker-missing.txt" ;;
    *passkey*|*authenticat*)              echo "$h" >> "$WORKDIR/tracker-auth.txt" ;;
    *)                                    echo "$h" >> "$WORKDIR/tracker-issue.txt" ;;
  esac
done

# Syncs one tag to exactly the torrents in $file: adds it to any that don't have it yet,
# removes it from any that currently have it but no longer belong (problem resolved). This is
# what makes the tool safe for a recurring cron — a tag always reflects the CURRENT scan, never
# a stale leftover from a past run.
sync_tag() {
  local file="$1" tag="$2"
  local current_file="$WORKDIR/current_${tag}.txt"

  sort -o "$file" "$file"
  curl -s -b "$COOKIE" "$QBIT_URL/api/v2/torrents/info" --data-urlencode "tag=$tag" \
    | jq -r '.[].hash' | sort > "$current_file"

  local to_add to_remove add_count remove_count
  to_add=$(comm -13 "$current_file" "$file")
  to_remove=$(comm -23 "$current_file" "$file")
  add_count=$(grep -c . <<< "$to_add" || true)
  remove_count=$(grep -c . <<< "$to_remove" || true)
  [ -z "$to_add" ] && add_count=0
  [ -z "$to_remove" ] && remove_count=0

  if [ "$DRY_RUN" -eq 1 ]; then
    [ "$add_count" -gt 0 ] && log "WOULD ADD '$tag': $add_count torrent(s)"
    [ "$remove_count" -gt 0 ] && log "WOULD REMOVE '$tag': $remove_count torrent(s) (no longer applies)"
    [ "$add_count" -eq 0 ] && [ "$remove_count" -eq 0 ] && log "'$tag': no change needed ($(wc -l < "$file") torrent(s))"
    return
  fi

  if [ "$add_count" -gt 0 ]; then
    curl -s -b "$COOKIE" -X POST "$QBIT_URL/api/v2/torrents/addTags" \
      --data-urlencode "hashes=$(tr '\n' '|' <<< "$to_add")" --data-urlencode "tags=$tag" > /dev/null
    log "ADDED '$tag': $add_count torrent(s)"
  fi
  if [ "$remove_count" -gt 0 ]; then
    curl -s -b "$COOKIE" -X POST "$QBIT_URL/api/v2/torrents/removeTags" \
      --data-urlencode "hashes=$(tr '\n' '|' <<< "$to_remove")" --data-urlencode "tags=$tag" > /dev/null
    log "REMOVED '$tag': $remove_count torrent(s) (no longer applies)"
  fi
  [ "$add_count" -eq 0 ] && [ "$remove_count" -eq 0 ] && log "'$tag': no change ($(wc -l < "$file") torrent(s), unchanged)"
}

sync_tag "$WORKDIR/trumped.txt" "trumped"
sync_tag "$WORKDIR/tracker-deleted.txt" "tracker-deleted"
sync_tag "$WORKDIR/tracker-missing.txt" "tracker-missing"
sync_tag "$WORKDIR/tracker-auth.txt" "tracker-auth"
sync_tag "$WORKDIR/tracker-issue.txt" "tracker-issue"

rm -f "$COOKIE"
log "=== tag-tracker-issues run finished ==="
if [ "$DRY_RUN" -eq 1 ]; then
  log "This was a dry run. Review the counts above, then re-run with --apply to sync the tags."
fi
