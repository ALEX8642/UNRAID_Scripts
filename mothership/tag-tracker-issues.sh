#!/bin/bash
# tag-tracker-issues.sh
#
# Scans every torrent's tracker status and adds a qBittorrent tag describing any real
# tracker-reported problem, so they can be sorted/filtered in the WebUI. Never touches files,
# never removes a torrent, never changes seeding behavior — purely additive tags.
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

apply_tag() {
  local file="$1" tag="$2"
  local count
  count=$(wc -l < "$file")
  [ "$count" -eq 0 ] && return
  local hashes
  hashes=$(paste -sd '|' "$file")
  if [ "$DRY_RUN" -eq 1 ]; then
    log "WOULD TAG '$tag': $count torrent(s)"
  else
    curl -s -b "$COOKIE" -X POST "$QBIT_URL/api/v2/torrents/addTags" \
      --data-urlencode "hashes=$hashes" --data-urlencode "tags=$tag" > /dev/null
    log "TAGGED '$tag': $count torrent(s)"
  fi
}

apply_tag "$WORKDIR/trumped.txt" "trumped"
apply_tag "$WORKDIR/tracker-deleted.txt" "tracker-deleted"
apply_tag "$WORKDIR/tracker-missing.txt" "tracker-missing"
apply_tag "$WORKDIR/tracker-auth.txt" "tracker-auth"
apply_tag "$WORKDIR/tracker-issue.txt" "tracker-issue"

rm -f "$COOKIE"
log "=== tag-tracker-issues run finished ==="
if [ "$DRY_RUN" -eq 1 ]; then
  log "This was a dry run. Review the counts above, then re-run with --apply to actually add tags."
fi
