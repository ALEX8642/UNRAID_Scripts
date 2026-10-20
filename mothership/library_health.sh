#!/bin/bash
# library_health.sh - read-only health check for the Plex/Radarr/Sonarr/qBittorrent stack. Run ON the Unraid host (bash, curl, jq).
# Checks: files Radarr/Sonarr track but are missing on disk; torrents in missingFiles/error; blocked or stuck import queue items;
#         video files in Movies that Radarr does not know; array disks nearly full.
# Notifies through Unraid's notification system (importance 'alert', which your Unraid setup sends by browser AND email; plus a Discord webhook if DISCORD_WEBHOOK is set) only when something is wrong,
# plus a short "all clear" on Sundays. Usage:  library_health.sh [--quiet] [--no-notify]
# Needs credentials from /mnt/user/system/scripts/mothership/.env (QBIT_URL/USER/PASS); the arr API keys are read from each app's config.xml.
set -u
QUIET=0; NOTIFY=1; for a in "$@"; do case $a in --quiet) QUIET=1;; --no-notify) NOTIFY=0;; esac; done
set -a; . /mnt/user/system/scripts/mothership/.env 2>/dev/null; set +a
ISSUES=(); say(){ [ $QUIET = 1 ] || echo "$@"; }
arr(){ # arr APP path  -> JSON
  local app=$1 path=$2 key port ip
  key=$(grep -oP '(?<=<ApiKey>)[^<]+' /mnt/docker/$app/config.xml); port=$(grep -oP '(?<=<Port>)[^<]+' /mnt/docker/$app/config.xml)
  ip=$(docker inspect $app --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
  curl -s -m 120 -H "X-Api-Key: $key" "http://$ip:$port/api/v3/$path"; }
exists(){ ls /mnt/disk*/"$1" >/dev/null 2>&1 || ls /mnt/cache/"$1" >/dev/null 2>&1; }

# 1. Radarr files missing on disk
miss=0; list=""
while IFS= read -r f; do r="Media/${f#/media/}"; exists "$r" || { miss=$((miss+1)); [ $miss -le 5 ] && list="$list\n   $f"; }; done < <(arr radarr movie | jq -r '.[]|select(.hasFile)|.movieFile.path')
say "Radarr tracked files missing on disk: $miss"; [ $miss -gt 0 ] && ISSUES+=("Radarr: $miss tracked file(s) missing on disk$(printf "$list")")
# 2. Sonarr files missing on disk
missT=0; listT=""
for id in $(arr sonarr series | jq -r '.[]|select(.statistics.episodeFileCount>0)|.id'); do
  while IFS= read -r f; do r="Media/${f#/media/}"; exists "$r" || { missT=$((missT+1)); [ $missT -le 5 ] && listT="$listT\n   $f"; }; done < <(arr sonarr "episodefile?seriesId=$id" | jq -r '.[].path')
done
say "Sonarr tracked files missing on disk: $missT"; [ $missT -gt 0 ] && ISSUES+=("Sonarr: $missT tracked episode file(s) missing on disk$(printf "$listT")")
# 3. qBittorrent torrents in trouble
CK=$(mktemp); curl -s -c $CK -d "username=$QBIT_USER&password=$QBIT_PASS" "$QBIT_URL/api/v2/auth/login" >/dev/null
bad=$(curl -s -b $CK "$QBIT_URL/api/v2/torrents/info" | jq -r '[.[]|select(.state=="missingFiles" or .state=="error")]|length'); rm -f $CK
say "qBittorrent torrents missing files / in error: $bad"; [ "${bad:-0}" -gt 0 ] && ISSUES+=("qBittorrent: $bad torrent(s) in missingFiles/error state")
# 4. import queue stuck or blocked
for app in radarr sonarr; do
  q=$(arr $app "queue?pageSize=200&includeUnknownMovieItems=true" | jq -r '[.records[]?|select(.trackedDownloadState=="importBlocked" or .trackedDownloadState=="importFailed" or .trackedDownloadStatus=="warning")]|length' 2>/dev/null)
  say "$app queue items blocked/warning: ${q:-0}"; [ "${q:-0}" -gt 0 ] && ISSUES+=("$app: ${q} queue item(s) blocked or with warnings (check before anything unblocks them)")
done
# 5. untracked video files in Movies (Radarr has no record of them)
arr radarr movie | jq -r '.[]|select(.hasFile)|(.path|sub("^/media/";"Media/"))+"/"+.movieFile.relativePath' | sort -u > /tmp/health_tracked.txt
unt=0; ul=""
while IFS= read -r p; do r=${p#/mnt/disk*/}; grep -qxF -- "$r" /tmp/health_tracked.txt || { unt=$((unt+1)); [ $unt -le 5 ] && ul="$ul\n   $(basename "$p")"; }; done < <(for d in /mnt/disk*/Media/Movies; do find "$d" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m2ts' -o -iname '*.avi' \) -size +300M; done)
say "Movie files Radarr does not track (large videos): $unt"; [ $unt -gt 60 ] && ISSUES+=("Movies: $unt large untracked video files (was ~45 after the Oct 2026 cleanup, mostly intentional editions)$(printf "$ul")")
# 6. disks nearly full
full=$(df -h /mnt/disk[0-9]* /mnt/cache 2>/dev/null | awk 'NR>1 && $5+0>=97 {print $6" "$5}' | tr '\n' ';')
say "Disks at 97%+: ${full:-none}"; [ -n "$full" ] && ISSUES+=("Disks nearly full: $full")

if [ ${#ISSUES[@]} -eq 0 ]; then MSG="All clear: no missing files, no stuck imports, qBittorrent healthy."; LEVEL=normal; else MSG=$(printf '%s\n' "${ISSUES[@]}"); LEVEL=alert; fi
say ""; say "$MSG"
if [ $NOTIFY = 1 ] && { [ $LEVEL = alert ] || [ "$(date +%u)" = 7 ]; }; then
  /usr/local/emhttp/webGui/scripts/notify -e "Library health" -s "Library health: $([ $LEVEL = alert ] && echo "${#ISSUES[@]} issue(s)" || echo "all clear")" -d "$MSG" -i $LEVEL >/dev/null 2>&1
  [ -n "${DISCORD_WEBHOOK:-}" ] && curl -s -H "Content-Type: application/json" -d "$(jq -n --arg c "Library health:\n$MSG" '{content:$c}')" "$DISCORD_WEBHOOK" >/dev/null
fi
exit $([ $LEVEL = alert ] && echo 1 || echo 0)
