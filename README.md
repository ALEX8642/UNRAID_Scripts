# UNRAID SCRIPTS

This is my repo for my private Unraid media server setup. It includes custom tools and scripts I've developed to enhance performance, monitoring, and automation — beyond what Unraid provides out of the box.

### 📜 Custom User Scripts Included

1. **🔍 HDD SMART Health Monitoring**  
   Deep-dive SMART analyzer with firmware warning checks and Unraid notification integration.

2. **⚡ PCIe ASPM Diagnostics**  
   Detects ASPM capability, status, and known blockers (e.g., LSI, ASM, PLX) with actionable summaries and notification support.

3. **🚀 Plex Library Maintenance Toolkit (`mothership/`)**  
   One Docker image, four tools for keeping a Radarr/Sonarr + qBittorrent + Plex library healthy: cross-disk duplicate cleanup, an interactive quality-based duplicate review, an orphan file scan, and a tracker-problem tagger. Everything is dry-run by default and destructive actions go through one tested safety layer (`safe_ops.py`). Details below.

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

One Docker image (`./run.sh`, interactive menu, or `./run.sh <tool> [flags]` for scripted use) with four tools.
**Every tool is dry-run / report-only by default.** Destructive steps share one safety layer, `safe_ops.py`
(unit-tested: `python3 -m unittest discover -s tests`).

#### Safety model (what the tools will and won't do)
- **Dry run first, execute second.** `dupe-review` records your choices as a plan file; nothing changes. `--apply`
  runs each removal after a y/N prompt; `--execute PLAN.json [--apply]` re-validates a saved plan against the
  *current* state and skips anything that changed.
- **Never write through `/mnt/user`** (Unraid's FUSE layer). Library-only files are unlinked on the physical
  `/mnt/diskN` copy; a path that exists on several disks is refused (use cross-disk-dedupe first).
- **Seeds are protected.** qBittorrent tracks data by path, so a torrent-owned file is only ever removed through
  qBittorrent's API. A torrent seeded less than `HNR_MIN_SEED_DAYS` (default 30; tracker hit-and-run rules vary)
  is never deleted: if the library name is just a hardlink of a seed kept elsewhere (the `arr/` download tree),
  only that name is removed and the torrent is tagged `delete-later`; otherwise the torrent is only tagged.
  Multi-file pack torrents are refused (a partial delete would break the seed). Loose-file ("rootless")
  torrents are matched file by file, never by prefix.
- **Hardlinks are understood.** Versions that are the same data under two names are flagged in the review table
  (deleting one name frees no space).
- Radarr/Sonarr are repointed at the kept file and rescanned with `--reconcile` (never a search or grab).

#### Tools
- **`dupe-review`** — interactive review of titles that exist as more than one *different* file (resolution /
  source / encode). Side-by-side table from Plex's probe data plus Radarr/Sonarr's real custom-format score and
  qBittorrent seeding state; suggestion is advisory. `mothership.py dupe-review [--report-only] [--apply]
  [--reconcile] [--reset] [--execute PLAN.json]`
- **`cross-disk-dedupe`** — the same file physically on two array disks at one logical path (invisible through
  `/mnt/user`). SHA-256 + ctime identify the copy `/mnt/user` exposes; only the other is removed. `[--apply]`
- **`orphan-scan`** — video files neither qBittorrent (checked file by file) nor Plex knows. Report-only unless
  `--apply`, which unlinks on the physical disk, skips anything hardlinked elsewhere or recently modified, and
  prunes only the empty parent folders it just emptied. `[--apply] [--exclude PATH ...]`
- **`tag-tracker-issues`** — tags torrents with real tracker-reported problems (trumped, deleted, unregistered,
  auth). Only ever adds/removes its own tags; safe unattended (it does not touch `delete-later`). `[--apply]`

#### Setup
> 📂 `mothership/` (Dockerfile, `mothership.py`, `common.py`, `safe_ops.py`, the four tools, `run.sh`, `.env.example`)  
> ⚙️ Needs Docker; the image runs with `--network host`, a read-only mount of Plex's databases folder and the
> whole `/mnt` (the physical disks are needed for raw-disk operations). Copy `.env.example` to `.env` and fill in
> Radarr/Sonarr/qBittorrent credentials (and `PLEX_TOKEN` for the automatic refresh). `.env` is gitignored.  
> 🗄️ Retired and kept for history in `archive/` (see `archive/README.md`).

---
**Example Script Description (for Unraid UI):**

```text
Library maintenance toolkit (one Docker image): interactive quality-based duplicate review, cross-disk duplicate
cleanup, orphan file scan, and a tracker-issue tagger. Dry-run by default; removals use raw-disk unlinks or
qBittorrent's API, never /mnt/user, and seeds inside a hit-and-run window are only tagged, never deleted.
```
