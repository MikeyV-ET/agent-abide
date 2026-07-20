#!/usr/bin/env bash
# weekly_backup_usb.sh — Weekly backup regime to USB drive D:
# =============================================================
# Full backup on Monday, incremental Tue-Sun using tar --listed-incremental.
# Requires USB drive mounted at /mnt/d. Exits cleanly if not mounted.
#
# Usage:
#   weekly_backup_usb.sh                # auto-detect full vs incremental
#   weekly_backup_usb.sh --full         # force full backup
#   weekly_backup_usb.sh --install-cron # install daily 3 AM cron job
#   weekly_backup_usb.sh --list         # list backups on USB
#
# Cron runs daily at 3 AM. Monday = full, Tue-Sun = incremental.
# =============================================================

set -euo pipefail

USB_DIR="/mnt/e"
SNAR_FILE="${USB_DIR}/home_eric.snar"
LOG_FILE="${USB_DIR}/backup.log"
DATE=$(date +%Y%m%d)
DAY_OF_WEEK=$(date +%u)  # 1=Monday

EXCLUDES=(
    "--exclude=home/eric/.cache"
    "--exclude=home/eric/.npm"
    "--exclude=home/eric/.local"
    "--exclude=home/eric/backups"
    "--exclude=home/eric/.nvm"
    "--exclude=home/eric/.vscode-server"
    "--exclude=home/eric/.config"
    "--exclude=home/eric/actionbench_workspaces"
    "--exclude=home/eric/.wdm"
    "--exclude=home/eric/asdaaas.bak"
    "--exclude=home/eric/.grok/bin"
    "--exclude=home/eric/.grok/worktrees"
    "--exclude=home/eric/.grok/activity"
    "--exclude=home/eric/.grok/marketplace-cache"
    "--exclude=home/eric/.grok/vendor"
    "--exclude=home/eric/.grok/logs"
    "--exclude=home/eric/.grok/irc_logs"
    "--exclude=home/eric/.grok/bundled"
    "--exclude=home/eric/.grok/docs"
    "--exclude=home/eric/.grok/skills"
)

log() {
    local ts
    ts=$(date +"%Y-%m-%d %H:%M:%S %Z")
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

check_usb() {
    if ! mountpoint -q "$USB_DIR" 2>/dev/null; then
        echo "USB drive not mounted at ${USB_DIR}. Skipping backup."
        exit 0
    fi
}

do_full() {
    log "=== FULL backup starting (${DATE}) ==="

    # Delete old snapshot to force full
    rm -f "$SNAR_FILE"

    # Clean previous full chunks for today
    rm -f "${USB_DIR}/home_eric_full_${DATE}.tar.gz.a"*

    log "Running tar..."
    tar czf - -C / \
        --listed-incremental="$SNAR_FILE" \
        "${EXCLUDES[@]}" \
        home/eric \
        | split -b 3G - "${USB_DIR}/home_eric_full_${DATE}.tar.gz."

    # Verify
    if cat "${USB_DIR}/home_eric_full_${DATE}.tar.gz.a"* | gzip -t 2>/dev/null; then
        log "Verification: VALID"
    else
        log "ERROR: Verification FAILED"
        exit 1
    fi

    local size
    size=$(du -ch "${USB_DIR}/home_eric_full_${DATE}.tar.gz.a"* 2>/dev/null | tail -1 | cut -f1)
    log "Full backup complete: ${size}"

    # Clean previous week's files (keep current full + any incrementals from this week)
    cleanup_old_backups

    log "=== FULL backup done ==="
}

do_incremental() {
    if [[ ! -f "$SNAR_FILE" ]]; then
        log "No snapshot file found — running full backup instead"
        do_full
        return
    fi

    log "=== INCREMENTAL backup starting (${DATE}) ==="

    tar czf "${USB_DIR}/home_eric_inc_${DATE}.tar.gz" -C / \
        --listed-incremental="$SNAR_FILE" \
        "${EXCLUDES[@]}" \
        home/eric

    # Verify
    if gzip -t "${USB_DIR}/home_eric_inc_${DATE}.tar.gz" 2>/dev/null; then
        log "Verification: VALID"
    else
        log "ERROR: Verification FAILED"
        exit 1
    fi

    local size
    size=$(du -h "${USB_DIR}/home_eric_inc_${DATE}.tar.gz" | cut -f1)
    log "Incremental backup complete: ${size}"
    log "=== INCREMENTAL backup done ==="
}

cleanup_old_backups() {
    # Keep only the most recent full backup + its incrementals
    # Find all full backups, sort, keep latest, delete rest + their incrementals
    local latest_full
    latest_full=$(ls -1 "${USB_DIR}"/home_eric_full_*.tar.gz.aa 2>/dev/null | sort | tail -1 || true)

    if [[ -z "$latest_full" ]]; then
        return
    fi

    # Extract date from latest full (home_eric_full_YYYYMMDD.tar.gz.aa)
    local latest_date
    latest_date=$(basename "$latest_full" | sed 's/home_eric_full_\([0-9]*\).*/\1/')

    # Delete older fulls
    for f in "${USB_DIR}"/home_eric_full_*.tar.gz.a*; do
        [[ -f "$f" ]] || continue
        local fdate
        fdate=$(basename "$f" | sed 's/home_eric_full_\([0-9]*\).*/\1/')
        if [[ "$fdate" < "$latest_date" ]]; then
            rm -f "$f"
            log "Cleaned old full chunk: $(basename "$f")"
        fi
    done

    # Delete incrementals older than latest full
    for f in "${USB_DIR}"/home_eric_inc_*.tar.gz; do
        [[ -f "$f" ]] || continue
        local fdate
        fdate=$(basename "$f" | sed 's/home_eric_inc_\([0-9]*\).*/\1/')
        if [[ "$fdate" < "$latest_date" ]]; then
            rm -f "$f"
            log "Cleaned old incremental: $(basename "$f")"
        fi
    done
}

do_list() {
    check_usb
    echo "=== Full Backups ==="
    for f in "${USB_DIR}"/home_eric_full_*.tar.gz.aa; do
        [[ -f "$f" ]] || { echo "  (none)"; break; }
        local date_str
        date_str=$(basename "$f" | sed 's/home_eric_full_\([0-9]*\).*/\1/')
        local size
        size=$(du -ch "${USB_DIR}/home_eric_full_${date_str}.tar.gz.a"* | tail -1 | cut -f1)
        echo "  ${date_str}  ${size}"
    done

    echo ""
    echo "=== Incremental Backups ==="
    local found=0
    for f in "${USB_DIR}"/home_eric_inc_*.tar.gz; do
        [[ -f "$f" ]] || continue
        found=1
        local size
        size=$(du -h "$f" | cut -f1)
        echo "  $(basename "$f" .tar.gz | sed 's/home_eric_inc_//')  ${size}"
    done
    [[ $found -eq 0 ]] && echo "  (none)"

    echo ""
    echo "=== Snapshot File ==="
    if [[ -f "$SNAR_FILE" ]]; then
        echo "  $(ls -lh "$SNAR_FILE" | awk '{print $5, $6, $7, $8}')"
    else
        echo "  (none)"
    fi
}

install_cron() {
    local script_path
    script_path=$(readlink -f "$0")
    local cron_line="0 3 * * * ${script_path} >> /tmp/weekly_backup.log 2>&1"

    if crontab -l 2>/dev/null | grep -qF "weekly_backup_usb.sh"; then
        echo "Cron job already installed:"
        crontab -l | grep "weekly_backup_usb"
        return
    fi

    (crontab -l 2>/dev/null || true; echo "$cron_line") | crontab -
    echo "Cron job installed: daily at 3 AM"
    echo "  ${cron_line}"
}

# ---- Main ----

case "${1:-}" in
    --full)
        check_usb
        do_full
        ;;
    --install-cron)
        install_cron
        ;;
    --list)
        do_list
        ;;
    --help|-h)
        head -14 "$0" | tail -12
        ;;
    *)
        check_usb
        # Monday = full, otherwise incremental
        if [[ "$DAY_OF_WEEK" == "1" ]]; then
            do_full
        else
            do_incremental
        fi
        ;;
esac