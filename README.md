# UNRAID SCRIPTS

This is my repo for my private Unraid media server setup. It includes custom tools and scripts I've developed to enhance performance, monitoring, and automation — beyond what Unraid provides out of the box.

### 📜 Custom User Scripts Included

1. **🔍 HDD SMART Health Monitoring**  
   Deep-dive SMART analyzer with firmware warning checks and Unraid notification integration.

2. **⚡ PCIe ASPM Diagnostics**  
   Detects ASPM capability, status, and known blockers (e.g., LSI, ASM, PLX) with actionable summaries and notification support.

3. **🧹 Library Dedupe (Radarr/Sonarr + qBittorrent)**  
   Finds titles that exist twice — once as a loose, qBittorrent-seeded file outside Radarr/Sonarr's recognized structure, and once as the properly sorted library copy — and removes the sorted duplicate, keeping the seeding copy intact. Matches by exact file size at individual-file granularity (safe for TV season packs, where a season folder holds many episodes). Defaults to a dry run; nothing is deleted without `--apply`.

4. **🎬 Duplicate Version Review (interactive)**  
   For titles that exist as more than one *genuinely different* file — different resolution, source, or encode, not exact duplicates — this walks you through each one in a rich terminal UI: a side-by-side comparison table (resolution, bitrate, codec, HDR/DV, audio, size, release group) pulled from Plex's own probed metadata plus Radarr/Sonarr's actual customFormatScore for whichever copy each app tracks. Flags files still actively seeding in qBittorrent, and refuses to suggest a "best" pick when runtimes don't match closely enough to be confident it's really the same content (protects against Plex having mis-grouped unrelated files under one title). You choose what to keep per title; nothing is deleted without an explicit choice.

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
### 🧹 Library Dedupe Script (Radarr/Sonarr + qBittorrent)

If you hardlink-seed with qBittorrent alongside Radarr/Sonarr, it's easy to end up with a
title existing twice on disk: once as the raw, scene-named file qBittorrent is actively
seeding (living outside Radarr/Sonarr's recognized folder structure), and once as the
properly renamed/sorted copy Radarr/Sonarr and Plex actually use. If those two copies ever
stop being a true hardlink (e.g. from a cross-disk move, or downloads/library briefly using
cache), you're silently paying for the same file twice. This script finds those pairs and
removes the sorted duplicate, keeping the loose copy so qBittorrent keeps seeding
uninterrupted.

**Key Features:**

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

> 📂 Script path: `user.scripts/Library_Dedupe`  
> 🕒 Recommended: Run occasionally (manually, or scheduled via User Scripts) as new content
> accumulates  
> ⚙️ Requires: `curl`, `jq`; a Radarr and/or Sonarr instance with API access, and
> qBittorrent's WebUI API enabled. Edit the CONFIG block at the top of the script with your
> own API keys/credentials and library paths before running — ship it with placeholders, not
> real secrets.

---
**Example Script Description (for Unraid UI):**

```text
Finds titles duplicated between a loose qBittorrent-seeded file and Radarr/Sonarr's sorted
library copy, and deletes the sorted duplicate while preserving the seeding file. Matches by
exact file size, file-level granularity (safe for TV season packs), skips ambiguous matches,
and protects recent/actively-seeding content from deletion (hit-and-run safe). Dry-run by
default — pass --apply to actually delete.
```

---
### 🎬 Duplicate Version Review Script (interactive, rich terminal UI)

The exact-size dedupe script above only handles *true* duplicates — the same bytes sitting in
two places. It deliberately leaves alone titles that exist as more than one file where the
files are genuinely different (a 1080p Remux next to a 4K WEB-DL, an SDR encode next to a
Dolby Vision one, etc.) — that's a real quality/preference decision, not something safe to
automate. This script makes reviewing that backlog fast without taking the decision away from
you: for every title Plex already knows has more than one version, it prints a full
side-by-side comparison and asks what to keep.

**Key Features:**

- **Reads Plex's own probed metadata** (resolution, bitrate, codec, HDR/DV, audio) instead of
  re-parsing filenames — Plex already ran ffprobe on every file, and already grouped
  same-title files together as "versions."
- **Pulls the real Radarr/Sonarr customFormatScore** for whichever file each app currently
  tracks, straight from their API — reflects your actual configured quality-profile
  preferences rather than a guess this script invents.
- **Runtime-mismatch guard**: if the "versions" of a title have runtimes that don't match
  closely, it refuses to suggest a "best" pick and prints a loud warning instead. Plex
  occasionally mis-groups unrelated files (e.g. a multi-track music demo disc) under one
  title — this catches that before you trust a suggestion that would delete the wrong thing.
- **qBittorrent-aware**: flags any candidate-for-deletion file that is still an actively
  seeding torrent, and requires a second explicit confirmation before deleting it.
- **Never deletes automatically.** Every group requires a typed choice — keep one, keep
  several, or keep all (skip). `--report-only` previews every group with no prompts and no
  deletions, useful for a first look or for re-testing after a config change.
- Progress bars while it scans Plex + cross-references Radarr/Sonarr/qBittorrent; resumable
  (remembers which titles you've already decided on across runs, `--reset` to start over).

> 📂 Script path: `dupe-review/` (Dockerfile + `dupe_review.py` + `run.sh`)  
> 🕒 Recommended: Run after the exact-size dedupe script, as a periodic cleanup pass  
> ⚙️ Requires: Docker (builds a small `python:3.12-slim` image with `rich`+`requests`, run with
> `--network host` so it can reach Radarr/Sonarr/qBittorrent on `localhost`); a read-only bind
> mount of Plex's `Plug-in Support/Databases` folder. Edit the CONFIG block at the top of
> `dupe_review.py` with your own API keys/credentials before running — ship it with
> placeholders, not real secrets. Run via `./run.sh` (add `--report-only` to preview without
> prompts).

---
**Example Script Description (for Unraid UI):**

```text
Interactive rich-terminal review of Plex titles that exist as more than one genuinely
different file (quality/source/encode, not exact duplicates). Shows a side-by-side comparison
(resolution, bitrate, codec, HDR/DV, audio, size, release group, Radarr/Sonarr quality score)
per title and asks what to keep. Warns when runtimes don't match closely enough to trust a
suggestion (protects against Plex mis-grouping unrelated files), and flags files still
actively seeding in qBittorrent. Nothing is deleted without an explicit per-title choice.
```
