# UNRAID_Scripts — Change Specification

Target repo: `ALEX8642/UNRAID_Scripts`
Files: `aspm_status_report.sh`, `check_drive_health.sh`

Changes are grouped by priority. **P1 = correctness bug** (produces wrong output today), **P2 = robustness**, **P3 = enhancement**. Each item gives the current code, the problem, and the required change.

Do **not** add `set -euo pipefail` to either script. Both rely on `grep -q` / `awk` exit codes as normal control flow; strict mode would cause premature exit. Leave error handling as-is unless an item below says otherwise.

---

## `check_drive_health.sh`

### P1-A — Head-park / wear flags append multiple states (precedence bug)

Current:
```bash
awk "BEGIN {exit !($lu_pct > 0.9)}" && health_flags+="❗ Head park near limit, " ||
awk "BEGIN {exit !($lu_pct > 0.7)}" && health_flags+="🟡 Head park elevated, "

awk "BEGIN {exit !($max_pct >= 90)}" && health_flags+="❗ Near EOL, " ||
awk "BEGIN {exit !($max_pct >= 70)}" && health_flags+="🟡 Elevated wear, "
```

Problem: Bash evaluates `A && B || C && D` as `(((A && B) || C) && D)`. When the first condition is true and its append succeeds, the `|| C` short-circuits to true, then `&& D` still runs — so the second (lower-tier) flag is appended **in addition** to the first. A drive at 95% head-park life gets tagged both "near limit" and "elevated."

Required: Replace each two-line `&&/||` chain with explicit `if/elif` using numeric comparison:
```bash
if awk "BEGIN {exit !($lu_pct > 0.9)}"; then
    health_flags+="❗ Head park near limit, "
elif awk "BEGIN {exit !($lu_pct > 0.7)}"; then
    health_flags+="🟡 Head park elevated, "
fi

if awk "BEGIN {exit !($max_pct >= 90)}"; then
    health_flags+="❗ Near EOL, "
elif awk "BEGIN {exit !($max_pct >= 70)}"; then
    health_flags+="🟡 Elevated wear, "
fi
```

### P1-B — `Load_Cycle_Count` falls back to unrelated attribute

Current:
```bash
lu=$(smartctl -A "$disk" | awk '
/Load_Cycle_Count/ {lcc=$10}
/Start_Stop_Count/ && !lcc {lcc=$10}
END {print lcc + 0}'
)
```

Problem: `Start_Stop_Count` is spinup/spindown events, not head parks. Substituting it hides the exact SkyHawk parking problem this script exists to catch — a drive with 200,000 parks but 50 spindowns reports 50.

Required: Remove the `Start_Stop_Count` fallback. If `Load_Cycle_Count` is absent, report the value as `N/A` and skip the head-park percentage/flag logic for that drive rather than computing against a substituted value.

### P2-A — `smartctl -A` invoked 5× per drive

Current: separate `smartctl -A "$disk"` calls for `poh`, `lu`, `reall`, `pend`, `temp`.

Problem: Five subprocess spawns and five attribute-table reads per drive; on a spun-up array this is wasteful and the five reads are not a single consistent snapshot.

Required: Capture once, parse from the variable:
```bash
attrs=$(smartctl -A "$disk")
poh=$(echo "$attrs"  | awk '/Power_On_Hours/ {print $10}')
# ...derive lu, reall, pend, temp from "$attrs" the same way
```
Apply the same single-capture pattern to the `smartctl -i` output already stored in `id_output` (already done — keep).

### P2-B — SAS drives skipped instead of retried with SAT

Current:
```bash
elif echo "$id_output" | grep -q "Transport protocol: SAS"; then
    echo "🚫 SAS drive detected..."
    continue
```

Problem: SATA drives behind some HBAs are reported by smartctl as SAS transport and silently dropped from the report. Post-HBA-swap (9400-series, working SAT passthrough) this skip becomes unnecessary for SATA drives.

Required: Before `continue`, retry once with explicit SAT translation:
```bash
id_retry=$(smartctl -i -d sat "$disk" 2>&1)
if echo "$id_retry" | grep -q "Device Model"; then
    id_output="$id_retry"
    SMART_DTYPE="-d sat"   # reuse for subsequent -A calls on this disk
else
    echo "🚫 True SAS drive at $disk — skipping (needs -d scsi handling)" >&2
    continue
fi
```
Thread `${SMART_DTYPE:-}` into the `smartctl -A` capture from P2-A so the retry path reads attributes correctly. Leave native-SAS drives skipped (out of scope).

### P2-C — SkyHawk model match is incomplete

Current:
```bash
elif [[ "$model" == ST*VX* ]]; then
    class="Surveillance (SkyHawk)"
```

Problem: `ST*VX*` misses SkyHawk SKUs using `VN`/`VT` suffixes.

Required:
```bash
elif [[ "$model" =~ ST[0-9]+V[XNHT] ]]; then
    class="Surveillance (SkyHawk)"
```
Keep the existing `ST*VE*` SkyHawk AI branch, or fold it into the regex as `V[XNHTE]` and remove the separate branch — implementer's choice, but do not double-classify.

### P2-D — Remaining-life estimate presented as fact

Current: `erul_years` printed as "Est. Remaining Life: X years" using community `max_poh` denominators.

Problem: `max_poh` values are rough estimates, not rated MTBF; the output reads as a deterministic prediction.

Required: Either (a) relabel the output line to "Est. Remaining Life (rough approximation, not MTBF): X years", or (b) keep the value but add a one-line disclaimer in the report footer. No formula change required.

### P3-A — `notify` missing importance flag

Current health-report notify calls use `-e`/`-s`/`-d` but no `-i` importance.

Required: Add `-i warning` to the critical-issue notification and `-i normal` to the healthy one, matching the convention used in `aspm_status_report.sh`.

### P3-B — NVMe drives not covered

Current loop is `/dev/sd*` only.

Required (optional): Add a parallel pass over `/dev/nvme?n1` using `smartctl -A` NVMe attributes (`Percentage Used`, `Power On Hours`, `Media and Data Integrity Errors`, temperature). Separate classification table; do not force NVMe through the HDD wear logic. Mark clearly as a new section if implemented.

---

## `aspm_status_report.sh`

### P1-C — `capable` detection returns false positives

Current:
```bash
capable=$(echo "$lnkcap" | grep -q 'ASPM' && echo "Yes" || echo "No")
```

Problem: Every `LnkCap` line contains the substring "ASPM", including `ASPM not supported`. Devices that explicitly declare no ASPM are reported as `Capable: Yes`.

Required:
```bash
capable=$(echo "$lnkcap" | grep -qE 'ASPM L[0-9]' && echo "Yes" || echo "No")
```
This matches only when an actual link state (`L0s`, `L1`, `L0s L1`) is advertised.

### P2-E — `LnkCap` parsed as a multi-line range

Current:
```bash
lnkcap=$(echo "$info" | awk '/LnkCap:/,/LnkCtl:/')
```

Problem: On multi-function devices / bridges, `lspci -vv` emits multiple `LnkCap`/`LnkCtl` blocks; the range match merges them and can corrupt `optcomp`/`capable` derivation.

Required: Use the first occurrence only, consistent with how `lnkctl` is already handled:
```bash
lnkcap=$(echo "$info" | grep -m1 'LnkCap:')
```
Re-derive `optcomp` and `capable` from this single line (P1-C change already assumes this).

### P2-F — Add 9400/9600 controllers to blocker awareness

Current `known_blockers` includes `SAS3008` (9300) as a permanent blocker but nothing for newer silicon.

Required:
- Keep `SAS3008` as a permanent blocker (correct — 9300 does not negotiate ASPM L1 without forcing).
- Do **not** add `SAS3416` / `SAS3508` (9400) or `SAS4xxx` (9600) to `known_blockers`. These negotiate ASPM L1. Instead let them fall through to the normal capable/enabled logic so a working 9400 reports `✅ OK` and a 9400 with ASPM not yet enabled reports the `🔧 Try` path.

### P2-G — Qualify the `pcie_aspm=force` recommendation for CPU-attached root ports

Current: any device with `Capable=Yes, OptComp=Yes, Enabled=No, blocker=None` gets `🔧 Try pcie_aspm=force`.

Problem: Forcing ASPM on CPU-attached root ports (typically PCI addresses `00:01.x`, `00:06.x` on 12th/13th-gen Intel) is the slot path that commonly causes instability; the chipset-slot relocation is the correct fix there, not the kernel flag.

Required: When the device address matches `^00:0[16]\.` add a qualifier to the recommendation, e.g. `🔧 ASPM disabled — CPU-attached root port; prefer relocating endpoint to a chipset slot over pcie_aspm=force`. All other addresses keep the existing recommendation.

### P3-C — Report L1 substates

Current: ASPM reported as binary enabled/disabled.

Required (optional): When `lspci -vv` shows `L1SubCap`/`L1SubCtl`, add an `L1ss` field indicating whether `L1.1`/`L1.2` are enabled. Diagnostic value for the forced-L1 path on the new HBA; no effect on summary counters.

---

## Suggested commit grouping

1. `fix(health): correct flag precedence and Load_Cycle_Count fallback` (P1-A, P1-B)
2. `fix(aspm): accurate ASPM capability detection` (P1-C, P2-E)
3. `perf(health): single smartctl snapshot per drive` (P2-A)
4. `feat(health): SAT retry for SATA-behind-HBA, SkyHawk match, life disclaimer` (P2-B, P2-C, P2-D)
5. `feat(aspm): 9400/9600 awareness and CPU-root-port guidance` (P2-F, P2-G)
6. Enhancements as separate commits if pursued (P3-A/B/C)
