# Kometa helpers

Kometa lives at `/mnt/docker/kometa` on the server (config: `config.yml`, custom collections: `Movie Collections.yml`, `Movie Awards.yml`, overlays: `overlays/`). These scripts run **on the server** as root and are dry-run by default (add `--apply` to change anything). Copies sit in `/mnt/cache/system/scripts/mothership/`.

## How Kometa handles posters (why the scripts exist)
- It saves each movie's original poster to `overlays/Movies Original Posters/<Plex id>.jpg` and redraws the badges on that file every run. If you pick a different poster in Plex, the saved original is used again and your pick is overwritten.
- A poster in `assets/<movie folder>/poster.jpg` wins over everything and is re-badged whenever the file changes. Each edition of a movie lives in its own folder, so each edition can have its own poster.

## `set-poster.sh` — custom poster for a movie or one edition
```
set-poster.sh "Aliens" 226933                                   # dry run: lists every match and the asset folder it would use
set-poster.sh "Aliens" 226933 --edition "Special Edition" --apply
set-poster.sh "Aliens" https://host/img.jpg --edition "Theatrical Cut" --apply
```
Second argument: a ThePosterDB asset id (the number in `theposterdb.com/api/assets/<id>`) or a direct image URL. Run Kometa for Movies afterwards.

## `repick-poster.sh` — re-base overlays on a poster you chose in Plex
```
repick-poster.sh "Rambo: Last Blood"          # dry run
repick-poster.sh "Rambo: Last Blood" --apply
```
Choose the new poster in Plex first. The script deletes Kometa's saved original and removes the item's "Overlay" label, so the next run treats the current poster as the new original. Do not run it before changing the poster (Kometa would treat the already-badged poster as the original).

## Running Kometa by hand
```
docker exec -d -u abc kometa sh -c "cd /config && /lsiopy/bin/python /app/kometa/kometa.py --run --collections-only --run-libraries Movies --config /config/config.yml"
```
Drop `--collections-only` for a full run (operations, collections, overlays). Logs: `/mnt/docker/kometa/logs/meta.log`. The container runs a nightly schedule on its own (04:15).
