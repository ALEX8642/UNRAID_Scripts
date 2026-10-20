#!/bin/bash
# Give one movie (or one specific EDITION of a movie) a custom poster that Kometa will keep and draw its overlays on.
# Kometa looks for assets/<movie folder name>/poster.jpg, and each edition lives in its own folder, so editions can have different posters.
# Usage (run ON the server):
#   set-poster.sh "Aliens" 226933                                  # dry run: lists every Aliens item and its asset folder
#   set-poster.sh "Aliens" 226933 --edition "Special Edition" --apply
#   set-poster.sh "Aliens" https://example.com/poster.jpg --edition "Theatrical Cut" --apply
# The second argument is a ThePosterDB asset id (number) or any direct image URL.
# After --apply, run Kometa for the Movies library; it sees the changed poster and redraws the badges on it.
set -e
TITLE="$1"; SRC="$2"; shift 2 || true
ED=""; APPLY=0
while [ $# -gt 0 ]; do case "$1" in --apply) APPLY=1;; --edition) ED="$2"; shift;; esac; shift; done
[ -n "$TITLE" ] && [ -n "$SRC" ] || { sed -n 2,11p "$0"; exit 1; }
case "$SRC" in http*) URL="$SRC";; *) URL="https://theposterdb.com/api/assets/$SRC";; esac
set -a; . /mnt/user/system/scripts/mothership/.env; set +a
ASSETS="/mnt/docker/kometa/assets"
curl -s -G -H "X-Plex-Token: $PLEX_TOKEN" -H "Accept: application/json" --data-urlencode "title=$TITLE" "$PLEX_URL/library/sections/1/all?type=1" \
 | jq -r '.MediaContainer.Metadata[]?|[.ratingKey,.title,.year,(.editionTitle//""),.Media[0].Part[0].file]|join("|")' | while IFS='|' read -r key title year ed file; do
  [ -z "$ED" ] || [ "$ed" = "$ED" ] || continue
  rel="${file#/movies/}"; case "$rel" in */*) folder="${rel%%/*}";; *) folder="";; esac
  if [ -z "$folder" ]; then echo "SKIP $title ($year) [$ed]: file is loose in the library root, no folder to hold a poster"; continue; fi
  echo "$([ $APPLY = 1 ] && echo SET || echo DRY) $title ($year) [$ed] -> assets/$folder/poster.jpg"
  if [ $APPLY = 1 ]; then
    mkdir -p "$ASSETS/$folder"
    curl -sfL -o "$ASSETS/$folder/poster.jpg" "$URL" || { echo "  download failed: $URL"; exit 1; }
    chown -R nobody:users "$ASSETS/$folder"
  fi
done
