# Cross-disk duplicate cleanup: a new physical-disk-side tool

## Context

Tonight's redownload cascade (Big Bang Theory, Line of Duty, Star City, The Bridge) left real,
byte-for-byte duplicate files on disk. Investigating that surfaced something bigger: Unraid's
`/mnt/user` (FUSE union of the array disks) only ever shows **one** file per logical path, even
when the underlying data actually exists on two different physical disks. A scan of every
`.mkv` under `Media/tv` and `Media/Movies`, done directly against `/mnt/disk1`...`/mnt/disk12`
(bypassing `/mnt/user` entirely), found **43 files that exist twice — same relative path, two
different physical disks** — totaling **499.42 GB** of wasted space, spanning far more titles
than tonight's incident (Stranger Things, Ted Lasso, Star Trek: Strange New Worlds, Walking
Dead: Dead City, Reacher, Silo, Lanterns, DTF St Louis, Lioness, and Bridge).

**This is invisible to every existing tool in this toolkit** (`dedupe-library.sh`,
`orphan_scan.py`, `dupe_review.py`) by construction — they all operate through `/mnt/user`,
and the FUSE layer hides the second copy entirely. You cannot detect this class of duplicate
without reading each physical disk directly. That's the gap this plan closes.

An earlier draft of a broader migration plan proposed pinning the whole Media share to one
physical disk to guarantee hardlinks always work. **That's wrong and should not be pursued**:
the 43 splits found here aren't confined to one disk pair — they're disk8↔disk12, but a
related, different-class problem (below) spans disk11↔disk6 and disk11↔disk12. Pinning one
disk doesn't fit that spread, and doing it now would mean physically relocating terabytes of
already-scattered data — exactly the high-blast-radius batch operation this whole night has
been about avoiding.

## Two different problems — only one is in scope here

**Class A — same logical path, two physical disks (in scope).** A file gets rewritten or
re-imported and Unraid's allocator happens to place the new write on a different disk than the
original. `/mnt/user` keeps showing one of them; the other becomes permanent silent waste. This
is what the 43 files above are, and what this plan's tool fixes.

**Class B — a loose/sorted pair that was never hardlinkable to begin with (out of scope, defer).**
Confirmed tonight: Big Bang Theory's loose copy is on `disk11`, its sorted copy on `disk6`.
Bridge's loose copy is on `disk11`, sorted on `disk12`. These were never able to share an inode
— hardlinks can't cross physical disks — so they're structurally forced to be two independent
copies unless one is physically moved to co-locate with the other. That's a real decision with
real cost (a genuine multi-GB copy operation per title) and doesn't belong in the same tool as
Class A cleanup, which never needs to move any bytes at all. Note it, don't act on it here.

**Known exception, already broken, handle by hand**: The Bridge S01E01 is in a third,
not-fully-understood state from an `ln` attempt earlier tonight that failed partway (see
session notes) — a mystery-inode file now sits on disk12 where the original sorted copy used
to be, and the original loose copy is untouched on disk11. **Exclude this specific file from
any bulk run.** Resolve it by hand once its actual current state is fully understood.

## The verified-safe method (already tested successfully on Ted Lasso S04E03)

1. Compare full checksum (not size — a size-only match once produced a false match against a
   subtitle file earlier tonight) between the two physical copies. If they don't match, skip
   and report — not a simple duplicate, needs a human look.
2. Determine which copy `/mnt/user` currently exposes, **per file, every file** — don't assume
   one disk "wins" globally just because it did for one earlier test. Match `ctime` between
   `/mnt/user`'s view and each physical copy.
   - **If ctime doesn't uniquely identify one side** (both disks show the same ctime), skip
     and report for manual handling rather than guessing — picking wrong deletes the copy
     everything actually depends on.
3. Cross-check qBittorrent's own tracked `content_path` for that filename resolves to the same
   disk `/mnt/user` exposes, when a matching torrent exists. If qBittorrent's tracked path
   would resolve to the *non-exposed* copy, skip and report — deleting the other side would
   reintroduce tonight's original disaster (breaking qBittorrent's path tracking).
4. Delete the non-exposed physical copy directly via its `/mnt/diskN` path — never through
   `/mnt/user`, whose `ln`/`rm` behavior for cross-disk cases produced an unexplained,
   unrecoverable-without-investigation result earlier tonight.
5. Verify afterward: `/mnt/user` resolves to the same inode as before (untouched), qBittorrent
   (if applicable) still shows the same seeding state, and the non-exposed copy is confirmed
   gone.

This exact sequence was run end-to-end on Ted Lasso S04E03 tonight and verified successful
against all three goals: qBittorrent still seeding (unaffected), `/mnt/user`/Plex/Sonarr
unaffected (same inode before and after), wasted copy removed.

## The new tool

A new, standalone script — not a modification of `dedupe-library.sh` or a port of its logic.
Working name: `mothership/cross-disk-dedupe.sh`.

- Scans `/mnt/disk1` through `/mnt/diskN` (enumerate dynamically, don't hardcode 12 — disks
  get added) directly, for `Media/tv`, `Media/Movies`, `Media/arr/shows`, `Media/arr/movies`.
- Groups by relative path; reports (dry-run default, same convention as the rest of this
  toolkit) any path found on more than one disk.
- For `--apply`, runs the verified-safe method above per file, with the ctime-ambiguity guard
  and the qBittorrent cross-check as hard stops, not warnings-that-proceed-anyway.
- Also scans `.srt`/`.nfo` alongside `.mkv` for the same split pattern — not for the space
  (kilobytes), but to catch orphaned subtitle fragments left behind if a video's non-exposed
  copy gets removed while a same-disk subtitle doesn't. Report-only for these; not worth
  blocking the main cleanup on.
- Excludes The Bridge S01E01 explicitly (or more generally, anything already known to be in an
  inconsistent state) until manually resolved.

## What doesn't change

- `dedupe-library.sh` stays retired (already done tonight) — this new tool doesn't revive or
  replace it, it solves a problem that script was never able to see in the first place.
- `orphan_scan.py` and `dupe_review.py` stay exactly as they are, operating on `/mnt/user`.
  Their actual questions ("is this claimed by Plex?", "is this claimed by qBittorrent?", "which
  quality version should a human keep?") are correctly mount-side questions — Plex and
  qBittorrent both see the world through `/mnt/user` too, so that's the right layer to ask on.
  Adding disk-awareness to them would break what already works for no benefit.
- Class B (the loose/sorted-pair-never-hardlinkable problem) is not addressed by this plan.
  It needs its own decision about whether the cost of physically relocating files to co-locate
  them is worth it, separate from this cleanup.

## Verification

- Before `--apply`: dry-run output reviewed by a human, same as every other tool in this kit.
- Per-file, immediately after each deletion: confirm `/mnt/user` inode unchanged, confirm
  qBittorrent state unchanged (if a matching torrent exists), confirm the deleted disk path is
  actually gone.
- After a full run: re-run the disk-side scan (report-only) and confirm the previously-found
  43 (minus Bridge S01E01, minus anything skipped for ctime ambiguity or checksum mismatch) no
  longer appear.
