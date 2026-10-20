#!/bin/bash
# Make Kometa treat the poster you just chose in Plex as the new original, so overlays are re-drawn on it next run.
# Kometa keeps a backup of the original at overlays/<Library> Original Posters/<ratingKey>.jpg and re-uses it while the item has the "Overlay" label.
# Usage (run ON the server):  repick-poster.sh "Rambo: Last Blood" [--apply]     (dry run unless --apply)
# Pick the new poster in Plex FIRST, then run this, then let Kometa run (or run it for that library).
set -e
TITLE="$1"; APPLY=0; [ "$2" = "--apply" ] && APPLY=1
[ -n "$TITLE" ] || { echo "usage: $0 \"Movie Title\" [--apply]"; exit 1; }
set -a; . /mnt/user/system/scripts/mothership/.env; set +a
BK="/mnt/docker/kometa/overlays/Movies Original Posters"
H="X-Plex-Token: $PLEX_TOKEN"
curl -s -G -H "$H" -H "Accept: application/json" --data-urlencode "title=$TITLE" "$PLEX_URL/library/sections/1/all?type=1" \
 | jq -r '.MediaContainer.Metadata[]?|[.ratingKey,.title,.year,(.editionTitle//"")]|@tsv' | while IFS=$'\t' read -r key title year ed; do
  echo "$([ $APPLY = 1 ] && echo APPLY || echo DRY) $title ($year) $ed  ratingKey=$key  backup: $(ls "$BK/$key".* 2>/dev/null | tr '\n' ' ')"
  if [ $APPLY = 1 ]; then
    rm -f "$BK/$key".jpg "$BK/$key".png "$BK/$key".webp
    curl -s -o /dev/null -X PUT -G -H "$H" --data-urlencode "type=1" --data-urlencode "id=$key" --data-urlencode "label[].tag.tag-=Overlay" "$PLEX_URL/library/sections/1/all"
  fi
done
