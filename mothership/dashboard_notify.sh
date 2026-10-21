#!/bin/bash
# Hands notifications the Plex Dashboard queued to Unraid's own notify (so e-mail/browser settings apply). Every 5 minutes.
O=/mnt/cache/appdata/plex-dashboard/outbox
for f in "$O"/*.json; do
  [ -e "$f" ] || exit 0
  subj=$(jq -r .subject "$f"); msg=$(jq -r .message "$f"); imp=$(jq -r .importance "$f")
  /usr/local/emhttp/webGui/scripts/notify -e "Plex Dashboard" -s "$subj" -d "$msg" -i "$imp" && rm -f "$f"
done
