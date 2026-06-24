#!/bin/bash

echo "🔍 SMART Drive Health Report - $(date)"
echo "================================================================================="
echo "⚠️  Note: SAS drives may not return full SMART data without additional parameters (e.g., -d sat or -d scsi)."
echo "   This script currently skips SAS drives unless manually adapted."

# Declare known firmware issues with severity and reference
# Format: [model_prefix] = $'FWVER|SEVERITY|Description|Link\n...'
declare -A firmware_warnings
firmware_warnings["ST18000NM"]=$'SN04|warn|EPC bug in SN04 – avoid long SMART tests|https://github.com/Seagate/openSeaChest/issues/126'
firmware_warnings["ST12000NM0008"]=$'SN02|crit|Premature failure risk SN02/SN03 (DataHoarder)|https://www.reddit.com/r/DataHoarder/comments/1fv5tpp\nSN03|crit|Same failure risk as SN02|https://www.reddit.com/r/DataHoarder/comments/1fv5tpp'
firmware_warnings["ST20000NM007D"]=$'SPG0|crit|Reported issue (source needed)|'
firmware_warnings["WD181KFGX"]=$'83.00A83|warn|Retired firmware (WD notice)|'
firmware_warnings["WDC WD80EFZX"]=$'82.00A82|warn|Excessive head parking in older firmware|https://support-en.wd.com/app/answers/detail/a_id/23407'
firmware_warnings["WDC WD40EFRX"]=$'80.00A80|warn|Retired firmware, higher failure risk|https://www.reddit.com/r/DataHoarder/comments/nupry6/wd_red_failure_firmware/'
firmware_warnings["HUS7260"]=$'A5GNT7J0|warn|Occasional issues with vibration sensitivity in older Ultrastar firmware|https://serverfault.com/questions/880460/hgst-ultrastar-disks-dropping-in-servers'
firmware_warnings["HUH7210"]=$'JKT1|crit|Premature failure risk on HC510 units shipped with JKT1|https://forums.servethehome.com/index.php?threads/ultrastar-hc510-jkt1-head-crash.25074/'

# Track unknown models and critical issues
unknown_models=()
critical_issues=0
fw_warning_summary=()

# Collect report for bulk output
full_report=""

for disk in /dev/sd*; do
  # Skip non-block devices or partitions
  [[ -b "$disk" && "$disk" =~ ^/dev/sd[a-z]+$ ]] || continue

  echo "🧪 Checking $disk..." >&2

  # Get SMART info and capture stderr
  id_output=$(smartctl -i "$disk" 2>&1)
  SMART_DTYPE=""

  # Skip unsupported USB bridge devices
  if echo "$id_output" | grep -q "Unknown USB bridge"; then
    echo "🔌 Skipping USB device $disk (unsupported USB bridge)" >&2
    continue
  # Retry SATA-behind-HBA with explicit SAT passthrough
  elif echo "$id_output" | grep -q "Transport protocol: SAS"; then
    id_retry=$(smartctl -i -d sat "$disk" 2>&1)
    if echo "$id_retry" | grep -q "Device Model"; then
      id_output="$id_retry"
      SMART_DTYPE="-d sat"
    else
      echo "🚫 True SAS drive at $disk — skipping (needs -d scsi handling)" >&2
      continue
    fi
  fi

  # Parse model and firmware from output if possible
  if echo "$id_output" | grep -q "Device Model"; then
    model=$(echo "$id_output" | awk -F: '/Device Model/ {gsub(/^ +| +$/, "", $2); print $2}')
    fw=$(echo "$id_output" | awk -F: '/Firmware Version/ {gsub(/^ +| +$/, "", $2); print $2}')
  else
    echo "❌ smartctl failed for $disk:" >&2
    echo "$id_output" >&2
    continue
  fi

  [[ -z "$model" ]] && continue
  attrs=$(smartctl -A $SMART_DTYPE "$disk")
  poh=$(echo "$attrs" | awk '/Power_On_Hours/ {print $10}')
  lu=$(echo "$attrs" | awk '/Load_Cycle_Count/ {print $10}')
  reall=$(echo "$attrs" | awk '/Reallocated_Sector_Ct/ {print $10}')
  pend=$(echo "$attrs" | awk '/Current_Pending_Sector/ {print $10}')
  temp=$(echo "$attrs" | awk '/Temperature_Celsius|Temperature_Internal/ {print $10}' | head -n 1)

  poh=${poh:-0}; reall=${reall:-0}; pend=${pend:-0}; temp=${temp:-0}

  class="Unknown"; max_poh=30000; max_lu=300000
  if [[ "$model" == *"Exos"* || "$model" == ST*NM* ]]; then
    class="Enterprise (Exos)"; max_poh=60000; max_lu=600000
  elif [[ "$model" == *"WD"* || "$model" == *"Red"* ]]; then
    class="NAS (WD Red)"; max_poh=60000; max_lu=300000
  elif [[ "$model" =~ ST[0-9]+V[XNHT] ]]; then
    class="Surveillance (SkyHawk)"; max_poh=60000; max_lu=300000
  elif [[ "$model" == ST*VE* ]]; then
    class="Surveillance (SkyHawk AI)"; max_poh=60000; max_lu=300000
  elif [[ "$model" == ST20*NE* ]]; then
    class="NAS (IronWolf Pro)"; max_poh=55000; max_lu=300000
  elif [[ "$model" == ST22*NT* ]]; then
    class="Enterprise NAS (IronWolf Pro)"; max_poh=55000; max_lu=300000
  elif [[ "$model" == HUH72* || "$model" == HUS72* || "$model" == *Ultrastar* ]]; then
    class="Enterprise (HGST Ultrastar)"; max_poh=60000; max_lu=600000
  elif [[ "$model" == TOSHIBA*MG* ]]; then
    class="Enterprise (Toshiba MG Series)"; max_poh=60000; max_lu=600000
  elif [[ "$model" == TOSHIBA*HDWN* ]]; then
    class="NAS (Toshiba N300)"; max_poh=50000; max_lu=300000
  else
    unknown_models+=("$model")
  fi

  pct_poh=$(awk "BEGIN { printf \"%.1f\", (100 * $poh) / $max_poh }")
  if [[ -n "$lu" ]]; then
    pct_lu=$(awk "BEGIN { printf \"%.1f\", (100 * $lu) / $max_lu }")
    lu_pct=$(awk "BEGIN { printf \"%.1f\", $lu / $max_lu }")
    max_pct=$(awk "BEGIN { print ($pct_poh > $pct_lu) ? $pct_poh : $pct_lu }")
  else
    pct_lu="N/A"
    max_pct=$pct_poh
  fi
  erul_days=$(awk "BEGIN { printf \"%d\", (100 - $max_pct) * $max_poh / 100 / 24 }")
  erul_years=$(awk "BEGIN { printf \"%.1f\", $erul_days / 365 }")

  health_flags=""
  (( reall > 0 || pend > 0 )) && health_flags+="❗ Bad sectors, "
  (( temp >= 50 )) && health_flags+="❗ Hot, "
  (( temp >= 45 && temp < 50 )) && health_flags+="🟡 Warm, "
  if [[ -n "$lu" ]]; then
    if awk "BEGIN {exit !($lu_pct > 0.9)}"; then
      health_flags+="❗ Head park near limit, "
    elif awk "BEGIN {exit !($lu_pct > 0.7)}"; then
      health_flags+="🟡 Head park elevated, "
    fi
  fi
  if awk "BEGIN {exit !($max_pct >= 90)}"; then
    health_flags+="❗ Near EOL, "
  elif awk "BEGIN {exit !($max_pct >= 70)}"; then
    health_flags+="🟡 Elevated wear, "
  fi

  health_flags=${health_flags%, }
  [[ -z "$health_flags" ]] && health_flags="✅ Normal"
  [[ "$health_flags" == *"❗"* ]] && ((critical_issues++))

  for prefix in "${!firmware_warnings[@]}"; do
    if [[ "$model" == $prefix* ]]; then
      IFS=$'\n' read -rd '' -a fw_entries <<< "${firmware_warnings[$prefix]}"
      for entry in "${fw_entries[@]}"; do
        IFS='|' read -r fw_id severity reason link <<< "$entry"
        if [[ "$fw" == "$fw_id" ]]; then
          icon="⚠️"; [[ "$severity" == "crit" ]] && icon="❗"
          note="$icon ${disk##/dev/} ($model / $fw): $reason"
          [[ -n "$link" ]] && note+=" ($link)"
          fw_warning_summary+=("$note")
        fi
      done
      break
    fi
  done

  full_report+=$'\n'
  full_report+=$'📦 Disk: '"${disk##/dev/}"$'\n'
  full_report+=$' ├─ Model:   '"$model"$'\n'
  full_report+=$' ├─ Class:   '"$class"$'\n'
  full_report+=$' ├─ POH:     '"$poh"' hrs ('"$pct_poh"'% used)'$'\n'
  if [[ -n "$lu" ]]; then
    full_report+=$' ├─ L/U:     '"$lu"' cycles ('"$pct_lu"'% used)'$'\n'
  else
    full_report+=$' ├─ L/U:     N/A (Load_Cycle_Count not reported)'$'\n'
  fi
  full_report+=$' ├─ Temp:    '"$temp"'°C'$'\n'
  full_report+=$' ├─ Firmware: '"$fw"$'\n'
  full_report+=$' ├─ Realloc/Pending: '"$reall"' / '"$pend"$'\n'
  full_report+=$' ├─ Est. Remaining Life (rough approx, not MTBF): '"$erul_years"' years'$'\n'
  full_report+=$' └─ Health Flags: '"$health_flags"$'\n'

done

echo "$full_report"

if (( critical_issues > 0 )); then
  /usr/local/emhttp/webGui/scripts/notify -e "SMART Health Report" -s "⚠️ Disk issues detected!" -d "$critical_issues disk(s) flagged with potential failure indicators."
else
  /usr/local/emhttp/webGui/scripts/notify -e "SMART Health Report" -s "✅ All drives healthy" -d "No critical issues detected in SMART scan."
fi

if (( ${#fw_warning_summary[@]} > 0 )); then
  echo ""
  echo "📌 Firmware Warnings:"
  for line in "${fw_warning_summary[@]}"; do
    echo " - $line"
  done
fi

if (( ${#unknown_models[@]} > 0 )); then
  echo ""
  echo "🔧 The following drive models were classified as 'Unknown':"
  printf ' - %s\n' "${unknown_models[@]}" | sort -u
  echo "🔀 Consider adding them to the model classification logic."
fi
echo "================================================================================="
