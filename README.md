# UNRAID SCRIPTS

This is my repo for my private Unraid media server setup. It includes custom tools and scripts I've developed to enhance performance, monitoring, and automation — beyond what Unraid provides out of the box.

### 📜 Custom User Scripts Included

1. **🔍 HDD SMART Health Monitoring**  
   Deep-dive SMART analyzer with firmware warning checks and Unraid notification integration.

2. **⚡ PCIe ASPM Diagnostics**  
   Detects ASPM capability, status, and known blockers (e.g., LSI, ASM, PLX) with actionable summaries and notification support.

3. **🚀 Plex Library Maintenance Toolkit (`mothership/`)**  
   One Docker image, one entry point, three modes for keeping a Radarr/Sonarr + qBittorrent + Plex library lean:
   - **Exact-duplicate cleanup** — a title existing twice on disk (once as qBittorrent's loose seeding copy, once as Radarr/Sonarr's sorted copy) gets the sorted duplicate removed, byte-identical matches only. Dry run by default.
   - **Quality-based duplicate review** — an interactive rich-terminal walkthrough for titles that exist as more than one *genuinely different* file (different resolution/source/encode). Shows a side-by-side comparison — Plex's own probed metadata plus Radarr/Sonarr's real quality score — and asks what to keep, per title. Nothing is deleted without an explicit choice.
   - **Orphan file scan** — finds video files on disk that neither qBittorrent nor Plex has any record of (failed imports, disc-rip remnants, files orphaned by a renamed torrent). Report by default.

   Run any one interactively via a menu, run all three in the sequence that makes sense (cheap byte-identical wins → judgment calls → final sweep), or invoke a specific mode non-interactively for scripted/cron use (`mothership.py dedupe --apply`). See below for the full breakdown of each mode.

---

### 🔍 HDD SMART Health Monitoring Script

This script performs a comprehensive audit of SMART data from all connected `/dev/sd?` drives in Unraid, offering **deeper and more contextual analysis than Unraid's built-in SMART monitoring**. While Unraid notifies users about raw SMART failures, it does **not** evaluate drive longevity trends, firmware-level risks, or usage pattern mismatches—critical for aging or repurposed drives in media servers.

For example, the drives in my Unraid array had abnormally high head park (load/unload) counts due to running 24/7 in a Windows desktop before being migrated to this server. These kinds of wear indicators are not flagged by Unraid, yet they can point to reliability issues long before a SMART failure is triggered.

**Key Features:**

- **Classifies** drives based on usage class (Enterprise, NAS, Surveillance)
- **Evaluates SMART attributes**, including:
  - Power-On Hours (POH)
  - Load/Unload cycle count (head parking)
  - Reallocated and pending sector counts
  - Operating temperature
- **Flags potential risks**, such as:
  - Excessive wear / nearing lifespan limits
  - High thermal exposure
  - Emerging SMART errors
- **Outputs warnings for known firmware bugs**, including severity and links to vendor/community advisories
- **Integrates with Unraid notifications** for health issues (excludes firmware warnings to avoid false alerts)
- **Identifies unclassified drive models** to refine logic for future updates

> 📂 Script path: `user.scripts/HDD_SMART_Status_Snapshot` (name it however you like)  
> 🕒 Recommended: Run daily or weekly using Unraid’s User Scripts plugin

---

**Example Script Description (for Unraid UI):**

```text
Scans all drives for SMART metrics, flags health issues like bad sectors, high temperatures, and near-EOL wear. Classifies drive types (e.g. Exos, Red, SkyHawk) and estimates remaining life. Also checks firmware against a database of known bugs (e.g. SN04 EPC issue). Sends Unraid alerts for health issues only. Firmware warnings shown in console output only.
```
### ⚡ PCIe ASPM Diagnostics Script

This script audits **PCIe Active State Power Management (ASPM)** support and status across all PCIe devices in your Unraid system. Unlike typical ASPM tools, it analyzes not just link state, but also:

- **Capability detection** (`ASPM`, `ASPMOptComp+`)
- **Enablement status**
- **Known hardware blockers** (e.g. LSI, PLX, ASMedia)
- **Fixable misconfigurations** (e.g. Intel root ports that support ASPM but aren't using it)

It also integrates with:
- ✅ **Unraid notifications** (severity-based)
- ✅ **Syslog logging**
- ✅ **Optional CSV export** to `/boot/logs/aspm_report.csv`
- ✅ **Summary counters** and detailed recommendations
- ✅ **Formatted output** compatible with User Scripts UI or terminal

> 📂 Script path: `user.scripts/PCIe_ASPM_Diagnostics`  
> 🕒 Recommended: Run at boot, after BIOS updates, or after kernel or HBA updates  
> ⚙️ Optional Pre-Req: [User Scripts plugin](https://forums.unraid.net/topic/48286-plugin-ca-user-scripts/) for scheduling and UI execution
---
**Example Script Description (for Unraid UI):**

```text
Scans all PCIe devices and reports ASPM status (Active State Power Management). Highlights devices that block ASPM or support it but are disabled. Useful for reducing idle power and debugging high wattage issues in always-on servers. Suggests boot parameters and hardware-level fixes where applicable.
```
**Example Output (User Scripts terminal):**
```
🔹 00:1c.1 - Intel PCIe Root Port #2
    OptComp      : Yes
    Capable      : Yes
    Enabled      : No
    Recommendation: 🔧 Try pcie_aspm=force
--------------------------------------------------------------------------

🔹 0c:00.0 - LSI SAS3008 PCIe HBA
    OptComp      : Yes
    Capable      : Yes
    Enabled      : No
    Recommendation: ❌ LSI blocks ASPM
--------------------------------------------------------------------------

📊 Summary:
  Devices scanned: 36
  ASPM Enabled: 14
  ASPM Disabled but fixable (Try): 1
  Blocked (LSI/ASM/PLX): 7
  Unsupported: 14
```

---
### 🚀 Plex Library Maintenance Toolkit (`mothership/`)

If you hardlink-seed with qBittorrent alongside Radarr/Sonarr, a library accumulates a few
predictable kinds of waste: titles duplicated between qBittorrent's loose seeding copy and
Radarr/Sonarr's sorted copy (byte-identical, or genuinely different quality/source), and
files neither system has any record of any more. This is one Docker image with three modes
for cleaning that up, plus a menu so you don't have to remember which mode to run, in what
order, or which flags each one takes.

Every mode defaults to a dry run / report and requires an explicit choice or `--apply` before
touching anything. Run interactively (`./run.sh`) for a menu, or invoke a mode directly and
non-interactively for scripted/cron use — `./run.sh dedupe --apply`, `./run.sh orphan-scan`,
etc. — with no menu prompts in the way.

#### Mode 1 — Exact-duplicate cleanup

Finds titles existing twice on disk — once as the raw, scene-named file qBittorrent is
actively seeding (living outside Radarr/Sonarr's recognized folder structure), and once as
the properly renamed/sorted copy Radarr/Sonarr and Plex actually use — and removes the sorted
duplicate, keeping the loose copy so qBittorrent keeps seeding uninterrupted. If those two
copies ever stop being a true hardlink (a cross-disk move, downloads/library briefly using
cache), you're silently paying for the same file twice; this is what catches that.

- **Matches by exact file size** within each app's own library tree — only acts on a match
  that's unique (exactly one candidate); ambiguous matches are skipped and logged, never
  guessed at.
- **File-level, not folder-level, deletion.** A TV "loose" match is often a season-pack
  folder whose files share a real `Season XX/` folder with unrelated episodes — only the one
  matched file is ever removed, never a whole directory that might hold siblings.
- **Hit-and-run protection**: skips any match where the loose file is both younger than
  `PROTECT_AGE_DAYS` (default 30) and still associated with an active torrent in
  qBittorrent — recent content gets left alone. On top of that, as an unconditional check
  with no age exception, it refuses to delete anything that is itself an active qBittorrent
  torrent path, regardless of how the match was derived.
- **Dry run by default.** Reports exactly what it would delete and why anything was skipped;
  nothing is touched until you pass `--apply`.
- Logs every run (matches, skips, and reasons) to a logfile for review.

**CLI:** `mothership.py dedupe [--apply]`

#### Mode 2 — Quality-based duplicate review (interactive, rich terminal UI)

Exact-duplicate cleanup only handles *true* duplicates — the same bytes sitting in two
places. It deliberately leaves alone titles that exist as more than one file where the files
are genuinely different (a 1080p Remux next to a 4K WEB-DL, an SDR encode next to a Dolby
Vision one, etc.) — that's a real quality/preference decision, not something safe to
automate. This mode makes reviewing that backlog fast without taking the decision away from
you: for every title Plex already knows has more than one version, it prints a full
side-by-side comparison and asks what to keep.

- **Reads Plex's own probed metadata** (resolution, bitrate, codec, HDR/DV, audio) instead of
  re-parsing filenames — Plex already ran ffprobe on every file, and already grouped
  same-title files together as "versions."
- **Pulls the real Radarr/Sonarr customFormatScore** for whichever file each app currently
  tracks, straight from their API — reflects your actual configured quality-profile
  preferences rather than a guess this tool invents.
- **Runtime-mismatch guard**: if the "versions" of a title have runtimes that don't match
  closely, it refuses to suggest a "best" pick and prints a loud warning instead. Plex
  occasionally mis-groups unrelated files (e.g. a multi-track music demo disc) under one
  title — this catches that before you trust a suggestion that would delete the wrong thing.
- **qBittorrent-aware**: flags any candidate-for-deletion file that is still an actively
  seeding torrent, and requires a second explicit confirmation before deleting it.
- **Never deletes automatically.** Every group requires a typed choice — keep one, keep
  several, or keep all (skip). `--report-only` previews every group with no prompts and no
  deletions, useful for a first look or for re-testing after a config change.
- **Season batching**: when every episode in a season has one side sharing the same
  qBittorrent torrent (a season-pack grab vs. the individually Sonarr-managed copies), review
  the whole season as one decision instead of once per episode.
- **Green-highlights** a file with meaningfully better audio (lossless over lossy) or a more
  broadly device-compatible Dolby Vision profile — suppressed whenever a 4K Remux is in the
  group, so device-compatibility nuance never visually outweighs a top-tier remux.
- **Multi-part guard**: filenames indicating different discs/parts of one release (Disc 1 vs
  Disc 2, CD1/CD2) are flagged as NOT the same content and never get an auto-suggestion.
- **Optional `--trash`**: moves deletions into `logs/trash/<run>/` instead of removing them
  outright, for an undo window. Not auto-cleaned — that's a separate, deliberate step.
- **Optional `--reconcile`** (off by default): if you keep the loose file over the one
  Radarr/Sonarr was tracking, asks that app to import the kept file so it's tracked again
  instead of staying stale. **Why this defaults off**, for anyone whose setup looks
  "improperly" configured on paper: a stale record — Radarr/Sonarr still believing it has the
  file you just deleted — is not automatically a problem. If that record's remembered quality
  already meets your quality profile's cutoff, automatic search skips the title entirely: no
  missing-search, no upgrade-search. That's genuinely useful when you've deliberately kept a
  release you judge better than anything currently available, and don't want it silently
  replaced — the alternative (rescanning to correct the bookkeeping) either re-grabs something
  you don't want, or unmonitors the title into a permanent gap you have to remember to fix
  yourself. Only pass `--reconcile` if you actually want this tool correcting that bookkeeping
  for you.
- **Optional automatic Plex refresh + Empty Trash** at the end of a session that deleted
  anything, if `PLEX_TOKEN` is set — otherwise just reminds you to do it manually.
- Progress counter (`[Title 42/335]`) and progress bars while scanning; resumable — a title's
  "reviewed" state is tied to its specific file set (path+size), so if a new duplicate shows up
  for a title you already decided on, it resurfaces instead of staying silently skipped.

**CLI:** `mothership.py dupe-review [--report-only] [--trash] [--reconcile] [--reset]`

#### Mode 3 — Orphan file scan

Finds video files sitting on disk under Movies/tv that neither qBittorrent nor Plex has any
record of — failed imports, leftover artifacts from a botched cross-disk move, raw Blu-ray
disc-rip remnants left behind after a proper remux was made, files orphaned when a torrent
got renamed or removed. This is the class of dead weight that's easy to accumulate and easy
to miss, since it doesn't show up as a "duplicate" anywhere — it's just inert bytes nobody
references any more.

- **Cross-references every torrent's full file list**, not just its category folder — a
  season pack or disc structure has many files under one torrent, and each one needs to be
  individually recognized as "claimed," not just the parent folder.
- **Cross-references Plex's complete media library**, not a filtered subset — so a file only
  counts as orphaned if genuinely neither system has any record of it.
- **Recently-modified files are flagged separately**, not called orphaned outright — a file
  that landed in the last 24h may simply not be indexed by Plex yet, or may be a torrent added
  after the scan started.
- **File-level, not folder-level, deletion.** `--apply` removes only the exact files found;
  now-empty directories are swept up as a separate pass afterward, same convention as
  Mode 1.
- Defaults to a report; nothing is touched without `--apply`.

**CLI:** `mothership.py orphan-scan [--apply] [--exclude PATH ...]` (`--exclude` is repeatable)

#### Setup

> 📂 Script path: `mothership/` (Dockerfile + `mothership.py` + `common.py` + the underlying
> tool for each mode + `run.sh` + `.env.example`)  
> 🕒 Recommended: Run occasionally as new content accumulates — Mode 1 first, then Mode 2, then
> Mode 3, or just pick "run all three" from the menu  
> ⚙️ Requires: Docker (builds a `python:3.12-slim` image with `bash`/`curl`/`jq` for Mode 1 and
> `rich`/`requests` for Modes 2-3, run with `--network host` so it can reach
> Radarr/Sonarr/qBittorrent on `localhost`); a read-only bind mount of Plex's
> `Plug-in Support/Databases` folder, and a read-write mount of your media root (needed to
> actually delete/trash files). Copy `.env.example` to `.env` in the same directory and fill
> in your Radarr/Sonarr/qBittorrent credentials (and optionally `PLEX_TOKEN` for Mode 2's
> auto-refresh) — `.env` is gitignored, never commit real values. Run via `./run.sh` for the
> interactive menu, or `./run.sh <mode> [flags]` to skip straight to one mode non-interactively.

---
**Example Script Description (for Unraid UI):**

```text
One Docker image, three modes for keeping a Radarr/Sonarr + qBittorrent + Plex library lean:
exact-duplicate cleanup (byte-identical loose-vs-sorted matches, hit-and-run safe), an
interactive rich-terminal review for titles that exist as more than one genuinely different
file (quality/source/encode), and an orphan file scan (files neither qBittorrent nor Plex has
any record of). Every mode defaults to a dry run/report; nothing is deleted without an
explicit choice or --apply. Run interactively via a menu, or invoke a mode directly and
non-interactively for scripted/cron use.
```
