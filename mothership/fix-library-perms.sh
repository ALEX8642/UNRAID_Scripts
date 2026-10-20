#!/bin/bash
# Make dirs/files under Media/{Movies,tv} owned by nobody:users with Unraid-standard perms (dirs 777, files 666) so Radarr/Sonarr (PUID 99) can delete/upgrade. Raw disks only. usage: fixperms.sh [--apply]
for r in Movies tv; do
  for d in /mnt/disk[0-9]*/Media/$r; do
    [ -d "$d" ] || continue
    nd=$(find "$d" -type d \( ! -user nobody -o ! -group users -o ! -perm 0777 \) 2>/dev/null | wc -l)
    nf=$(find "$d" -type f \( ! -user nobody -o ! -group users -o ! -perm -0006 \) 2>/dev/null | wc -l)
    echo "$d: dirs-to-fix=$nd files-to-fix=$nf"
    if [ "$1" = "--apply" ]; then
      find "$d" -type d \( ! -user nobody -o ! -group users -o ! -perm 0777 \) -exec chown nobody:users {} + -exec chmod 777 {} +
      find "$d" -type f \( ! -user nobody -o ! -group users -o ! -perm -0006 \) -exec chown nobody:users {} + -exec chmod a+rw {} +
    fi
  done
done
