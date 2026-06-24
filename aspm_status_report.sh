#!/bin/bash

# counter variables for summary
total=0
enabled_count=0
fixable_count=0
blocked_count=0
unsupported_count=0

# Output paths
CSV_OUT="/boot/logs/aspm_report.csv"
CSV_MODE=false

if [[ "$1" == "--csv" ]]; then
    CSV_MODE=true
    mkdir -p /boot/logs
    echo "PCIe Address,Device Name,ASPMOptComp,ASPM Capable,ASPM Enabled,Blocker,Recommendation" > "$CSV_OUT"
fi

# Known ASPM-blocking devices by keyword match
declare -A known_blockers
known_blockers["LSI SAS3008"]="lsi"
known_blockers["Broadcom / LSI SAS3008"]="lsi"
known_blockers["SAS3008"]="lsi"
known_blockers["ASM1061"]="asmedia"
known_blockers["ASM1062"]="asmedia"
known_blockers["PEX 8724"]="plx"
known_blockers["PLX"]="plx"

echo "🔍 PCIe ASPM Diagnostics ($(date))"
echo "=========================================================================="

# Loop over all PCIe devices
while read -r addr; do
    name=$(lspci -s "$addr" | cut -d' ' -f 2-)
    info=$(lspci -s "$addr" -vv)

    lnkcap=$(echo "$info" | grep -m1 'LnkCap:')
    lnkctl=$(echo "$info" | grep -m1 'LnkCtl:')

    # Improved detection
    optcomp=$(echo "$lnkcap" | grep -q 'ASPMOptComp+' && echo "Yes" || echo "No")
    capable=$(echo "$lnkcap" | grep -qE 'ASPM L[0-9]' && echo "Yes" || echo "No")

    # ASPM enabled
    if [[ "$lnkctl" =~ "ASPM L" ]]; then
        enabled="Yes"
    elif [[ "$lnkctl" =~ "ASPM Disabled" ]]; then
        enabled="No"
    else
        enabled="Unknown"
    fi

    # Check against known blocker keywords
    blocker="None"
    for key in "${!known_blockers[@]}"; do
        if [[ "$name" == *"$key"* ]]; then
            blocker="${known_blockers[$key]}"
            break
        fi
    done

    # Generate recommendation
    if [[ "$enabled" == "No" && "$capable" == "Yes" && "$optcomp" == "Yes" && "$blocker" == "None" ]]; then
        note="🔧 Try pcie_aspm=force"
    elif [[ "$blocker" != "None" ]]; then
        case $blocker in
            lsi)   note="❌ LSI blocks ASPM" ;;
            asmedia) note="⚠️ ASM1061 no ASPM" ;;
            plx)   note="⚠️ PLX may inherit block" ;;
            *)     note="⚠️ Known blocker: $blocker" ;;
        esac
    elif [[ "$capable" == "No" ]]; then
        note="Unsupported"
    elif [[ "$enabled" == "Yes" ]]; then
        note="✅ OK"
    else
        note="⚠️ Review manually"
    fi

    ((total++))

    case "$note" in
        "✅ OK") ((enabled_count++)) ;;
        "🔧 Try pcie_aspm=force") ((fixable_count++)) ;;
        "❌ LSI blocks ASPM"|"⚠️ ASM1061 no ASPM"|"⚠️ PLX may inherit block") ((blocked_count++)) ;;
        "Unsupported") ((unsupported_count++)) ;;
    esac

    # Print table row
    echo "🔹 $addr - $name"
    printf "    OptComp     : %s\n" "$optcomp"
    printf "    Capable     : %s\n" "$capable"
    printf "    Enabled     : %s\n" "$enabled"
    printf "    Recommendation: %s\n\n" "$note"
    echo "--------------------------------------------------------------------------"

    # Write to CSV
    if $CSV_MODE; then
        echo "$addr,\"$name\",$optcomp,$capable,$enabled,$blocker,\"$note\"" >> "$CSV_OUT"
    fi

done <<< "$(lspci | awk '{print $1}')"

echo
echo "📌 Legend:"
echo " - ✅ OK: ASPM enabled and working"
echo " - 🔧 Try: ASPM supported + OptComp+, disabled (try pcie_aspm=force)"
echo " - ❌ LSI blocks ASPM: hardware limitation"
echo " - ⚠️ ASM1061: no ASPM support"
echo " - ⚠️ PLX: may inherit block from child"
echo " - Unsupported: No ASPM capability"
echo " - ⚠️ Review: Inconclusive, needs deeper inspection"
[[ "$CSV_MODE" == true ]] && echo "📄 CSV report saved to: $CSV_OUT"

echo
echo "📊 Summary:"
printf "  Devices scanned: %d\n" "$total"
printf "  ASPM Enabled: %d\n" "$enabled_count"
printf "  ASPM Disabled but fixable (Try): %d\n" "$fixable_count"
printf "  Blocked (LSI/ASM/PLX): %d\n" "$blocked_count"
printf "  Unsupported: %d\n" "$unsupported_count"

# UNRAID Notifier/logger - Smart severity logic

# Default
severity="normal"

# Escalate if anything is fixable
if (( fixable_count > 0 )); then
    severity="warning"
fi

# Do NOT escalate for just hardware blockers (LSI/PLX/ASM)
if (( fixable_count == 0 && blocked_count > 0 )); then
    severity="normal"
fi

# Force 'normal' even with both fixable + blocked, unless you *explicitly* want 'alert'
# Uncomment below if you want alerts
# if (( fixable_count > 0 && blocked_count > 0 )); then
#     severity="alert"
# fi

# Compose message with icons
summary_msg="✅ $enabled_count enabled | ⚠️ $fixable_count fixable | ⛔ $blocked_count blocked | 💤 $unsupported_count unsupported (total $total)"

# Log and notify
logger "ASPM Diagnostics Summary: $summary_msg"
/usr/local/emhttp/webGui/scripts/notify -i "$severity" -s "PCIe ASPM Diagnostics Complete" -d "$summary_msg"
