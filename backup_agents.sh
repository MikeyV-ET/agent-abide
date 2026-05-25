#!/usr/bin/env bash
# backup_agents.sh — Automated backup of agent state and infrastructure
# ======================================================================
# Creates daily backups of critical agent data using rsync (incremental)
# and periodic tar.xz snapshots for disaster recovery.
#
# Usage:
#   backup_agents.sh                    # run backup now
#   backup_agents.sh --install-cron     # install daily cron job (3 AM)
#   backup_agents.sh --list             # list existing backups
#   backup_agents.sh --prune            # remove backups older than 7 days
#
# What gets backed up:
#   1. ~/agents/              - agent state (notebooks, notes, doorbells, config)
#   2. ~/projects/mikeyv-infra/ - infrastructure code
#   3. ~/projects/erics-notes/  - Eric's notes (if exists)
#   4. ~/projects/socratic-arena/ - Socratic arena (if exists)
#   5. ~/.grok/sessions/      - session data (chat history, signals)
#   6. Git bundles             - full commit history for all repos
#
# Backup locations:
#   ~/backups/daily/YYYY-MM-DD/     - rsync mirror (incremental)
#   ~/backups/snapshots/            - tar.xz snapshots (weekly)
#   /mnt/d/MikeyV/                  - USB drive (if mounted, off-machine)
#
# Features:
#   - Post-backup verification (critical files, size check, bundle check)
#   - Failure notification via localmail to Sr
#   - USB drive sync with NTFS-compatible flags (optional)
#
# Retention: 7 daily mirrors, 4 weekly snapshots
# ======================================================================

set -euo pipefail

BACKUP_ROOT="${HOME}/backups"
DAILY_DIR="${BACKUP_ROOT}/daily"
SNAPSHOT_DIR="${BACKUP_ROOT}/snapshots"
LOG_FILE="${BACKUP_ROOT}/backup.log"
RETENTION_DAYS=7
SNAPSHOT_RETENTION_DAYS=28

# USB drive (optional, skipped if not mounted)
USB_DIR="/mnt/d/MikeyV"

# Sources to back up
AGENTS_DIR="${HOME}/agents"
INFRA_DIR="${HOME}/projects/mikeyv-infra"
ERICS_NOTES_DIR="${HOME}/projects/erics-notes"
SOCRATIC_DIR="${HOME}/projects/socratic-arena"
SESSIONS_DIR="${HOME}/.grok/sessions"

# Git repos to bundle (commit history preservation)
GIT_REPOS=(
    "${HOME}/agents"
    "${HOME}/projects/mikeyv-infra"
    "${HOME}/projects/erics-notes"
    "${HOME}/projects/socratic-arena"
)

# Track errors for notification
ERRORS=()

err_track() {
    ERRORS+=("$1")
    log "ERROR: $1"
}

# ---- Functions ----

log() {
    local ts
    ts=$(date +"%Y-%m-%d %H:%M:%S %Z")
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

do_backup() {
    local today
    today=$(date +"%Y-%m-%d")
    local target="${DAILY_DIR}/${today}"

    mkdir -p "$target/agents" "$target/infra" "$target/sessions" \
             "$target/erics-notes" "$target/socratic-arena" \
             "$target/git-bundles" "$SNAPSHOT_DIR"
    touch "$LOG_FILE"

    log "=== Backup starting ==="

    # 1. Agents directory (exclude tar.xz archives and __pycache__)
    log "Backing up agents..."
    rsync -a --delete \
        --exclude='*.tar.xz' \
        --exclude='__pycache__' \
        --exclude='.git' \
        --exclude='asdaaas/adapters/*/outbox/' \
        "${AGENTS_DIR}/" "${target}/agents/" \
        || err_track "rsync agents failed"
    log "  agents: $(du -sh "${target}/agents/" | cut -f1)"

    # 2. Infrastructure code
    log "Backing up infra..."
    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='.git' \
        --exclude='*.bak' \
        --exclude='*.bak2' \
        "${INFRA_DIR}/" "${target}/infra/" \
        || err_track "rsync infra failed"
    log "  infra: $(du -sh "${target}/infra/" | cut -f1)"

    # 3. Eric's notes repo
    if [[ -d "$ERICS_NOTES_DIR" ]]; then
        log "Backing up erics-notes..."
        rsync -a --delete \
            --exclude='__pycache__' \
            --exclude='.git' \
            "${ERICS_NOTES_DIR}/" "${target}/erics-notes/" \
            || err_track "rsync erics-notes failed"
        log "  erics-notes: $(du -sh "${target}/erics-notes/" | cut -f1)"
    fi

    # 4. Socratic arena repo
    if [[ -d "$SOCRATIC_DIR" ]]; then
        log "Backing up socratic-arena..."
        rsync -a --delete \
            --exclude='__pycache__' \
            --exclude='.git' \
            "${SOCRATIC_DIR}/" "${target}/socratic-arena/" \
            || err_track "rsync socratic-arena failed"
        log "  socratic-arena: $(du -sh "${target}/socratic-arena/" | cut -f1)"
    fi

    # 5. Session data (only signals.json, summary.json, and chat_history.jsonl per session)
    #    Skip updates.jsonl (large, reconstructable) and tool outputs
    log "Backing up sessions..."
    rsync -a --delete \
        --include='*/' \
        --include='signals.json' \
        --include='summary.json' \
        --include='chat_history.jsonl' \
        --exclude='*' \
        "${SESSIONS_DIR}/" "${target}/sessions/" \
        || err_track "rsync sessions failed"
    log "  sessions: $(du -sh "${target}/sessions/" | cut -f1)"

    # 6. Git bundles (preserves full commit history)
    log "Creating git bundles..."
    for repo in "${GIT_REPOS[@]}"; do
        if [[ -d "${repo}/.git" ]]; then
            local repo_name
            repo_name=$(basename "$repo")
            git -C "$repo" bundle create "${target}/git-bundles/${repo_name}.bundle" --all 2>/dev/null \
                || err_track "git bundle ${repo_name} failed"
            log "  bundle ${repo_name}: $(du -sh "${target}/git-bundles/${repo_name}.bundle" 2>/dev/null | cut -f1)"
        fi
    done

    log "Daily backup complete: ${target}"
    log "  total: $(du -sh "${target}" | cut -f1)"

    # 7. USB drive sync (if mounted)
    do_usb_sync "$target"

    # 8. Weekly snapshot (on Sundays or if none exist)
    local day_of_week
    day_of_week=$(date +%u)  # 7 = Sunday
    local latest_snapshot
    latest_snapshot=$(ls -1 "${SNAPSHOT_DIR}"/agents_*.tar.xz 2>/dev/null | tail -1 || true)

    if [[ "$day_of_week" == "7" ]] || [[ -z "$latest_snapshot" ]]; then
        log "Creating weekly snapshot..."
        local snapshot_file="${SNAPSHOT_DIR}/agents_${today}.tar.xz"
        tar -cJf "$snapshot_file" \
            -C "$HOME" \
            --exclude='*.tar.xz' \
            --exclude='__pycache__' \
            --exclude='.git' \
            agents/ 2>/dev/null || true
        log "  snapshot: $(du -sh "$snapshot_file" | cut -f1)"
    fi

    # 9. Post-backup verification
    do_verify "$target"

    # 10. Failure notification
    do_notify

    log "=== Backup complete ==="
}

do_usb_sync() {
    local source_dir="$1"
    if [[ ! -d "$USB_DIR" ]]; then
        log "USB drive not mounted at ${USB_DIR}, skipping off-machine backup"
        return
    fi

    log "Syncing to USB drive..."

    # NTFS doesn't support Linux permissions/timestamps. rsync returns exit 23
    # for "failed to set times" even though files copy fine. Tolerate that.
    usb_rsync() {
        local rc=0
        rsync -a --no-perms --no-owner --no-group --no-links "$@" 2>/dev/null || rc=$?
        # exit 23 = partial transfer (NTFS timestamp warnings). Files are fine.
        if [[ $rc -ne 0 && $rc -ne 23 ]]; then
            return $rc
        fi
        return 0
    }

    # Mirror the daily backup to USB
    usb_rsync --delete "${source_dir}/agents/" "${USB_DIR}/daily/agents/" \
        || err_track "USB rsync agents failed (exit $?)"
    usb_rsync --delete "${source_dir}/infra/" "${USB_DIR}/daily/infra/" \
        || err_track "USB rsync infra failed (exit $?)"
    usb_rsync --delete "${source_dir}/sessions/" "${USB_DIR}/daily/sessions/" \
        || err_track "USB rsync sessions failed (exit $?)"

    # Sync git bundles to USB
    if [[ -d "${source_dir}/git-bundles" ]]; then
        mkdir -p "${USB_DIR}/git-bundles" 2>/dev/null || true
        usb_rsync "${source_dir}/git-bundles/" "${USB_DIR}/git-bundles/" \
            || err_track "USB rsync git-bundles failed (exit $?)"
    fi

    # Sync extra repos if they were backed up
    for repo_name in erics-notes socratic-arena; do
        if [[ -d "${source_dir}/${repo_name}" ]]; then
            usb_rsync --delete "${source_dir}/${repo_name}/" "${USB_DIR}/daily/${repo_name}/" \
                || err_track "USB rsync ${repo_name} failed (exit $?)"
        fi
    done

    log "  USB total: $(du -sh "${USB_DIR}" 2>/dev/null | cut -f1)"
    log "  USB free: $(df -h "${USB_DIR}" 2>/dev/null | tail -1 | awk '{print $4}')"
}

do_verify() {
    local target="$1"
    log "Verifying backup..."

    # Check critical files exist in backup
    local critical_files=(
        "agents/Sr/lab_notebook_sr.md"
        "agents/Sr/AGENTS.md"
        "agents/Jr/lab_notebook_jr.md"
        "infra/live/comms/asdaaas.py"
    )

    for f in "${critical_files[@]}"; do
        if [[ ! -f "${target}/${f}" ]]; then
            err_track "Missing critical file: ${f}"
        fi
    done

    # Check backup isn't suspiciously small (< 1 MB agents = something wrong)
    local agents_kb
    agents_kb=$(du -sk "${target}/agents/" 2>/dev/null | cut -f1)
    if [[ "${agents_kb:-0}" -lt 1024 ]]; then
        err_track "Agents backup suspiciously small: ${agents_kb}K"
    fi

    # Check git bundles were created
    local bundle_count
    bundle_count=$(ls -1 "${target}/git-bundles/"*.bundle 2>/dev/null | wc -l)
    if [[ "$bundle_count" -lt 1 ]]; then
        err_track "No git bundles created"
    else
        log "  ${bundle_count} git bundle(s) verified"
    fi

    log "Verification complete"
}

do_notify() {
    if [[ ${#ERRORS[@]} -eq 0 ]]; then
        return
    fi

    local error_text="Backup errors:\n"
    for e in "${ERRORS[@]}"; do
        error_text+="  - ${e}\n"
    done

    # Send localmail to Sr
    local mail_dir="${HOME}/agents/Sr/asdaaas/adapters/localmail/inbox"
    if [[ -d "$mail_dir" ]]; then
        local ts
        ts=$(date +%s%N | head -c16)
        local rand
        rand=$(head -c4 /dev/urandom | xxd -p)
        cat > "${mail_dir}/mail_${ts}_${rand}.json" <<EOF
{
  "from": "backup_agents",
  "to": "Sr",
  "text": "$(echo -e "$error_text")",
  "timestamp": "$(date -Iseconds)"
}
EOF
        log "Failure notification sent to Sr via localmail"
    fi

    log "WARNING: ${#ERRORS[@]} error(s) during backup"
}

do_prune() {
    log "Pruning old backups..."

    # Prune daily backups older than RETENTION_DAYS
    local count=0
    if [[ -d "$DAILY_DIR" ]]; then
        while IFS= read -r dir; do
            rm -rf "$dir"
            count=$((count + 1))
            log "  removed daily: $(basename "$dir")"
        done < <(find "$DAILY_DIR" -maxdepth 1 -mindepth 1 -type d -mtime +${RETENTION_DAYS} 2>/dev/null || true)
    fi

    # Prune snapshots older than SNAPSHOT_RETENTION_DAYS
    if [[ -d "$SNAPSHOT_DIR" ]]; then
        while IFS= read -r file; do
            rm -f "$file"
            count=$((count + 1))
            log "  removed snapshot: $(basename "$file")"
        done < <(find "$SNAPSHOT_DIR" -name "agents_*.tar.xz" -mtime +${SNAPSHOT_RETENTION_DAYS} 2>/dev/null || true)
    fi

    log "Pruned $count old backup(s)"
}

do_list() {
    echo "=== Daily Backups ==="
    if [[ -d "$DAILY_DIR" ]]; then
        for dir in "$DAILY_DIR"/*/; do
            [[ -d "$dir" ]] || continue
            local size
            size=$(du -sh "$dir" | cut -f1)
            echo "  $(basename "$dir")  ${size}"
        done
    else
        echo "  (none)"
    fi

    echo ""
    echo "=== Snapshots ==="
    if [[ -d "$SNAPSHOT_DIR" ]]; then
        for file in "$SNAPSHOT_DIR"/agents_*.tar.xz; do
            [[ -f "$file" ]] || continue
            local size
            size=$(du -sh "$file" | cut -f1)
            echo "  $(basename "$file")  ${size}"
        done
    else
        echo "  (none)"
    fi
}

install_cron() {
    # Install cron job to run at 3 AM daily
    local script_path
    script_path=$(readlink -f "$0")
    # Note: log() function handles logging via tee. Only redirect stderr here
    # to avoid double-logging stdout.
    local cron_line="0 3 * * * ${script_path} 2>> ${LOG_FILE}"

    # Check if already installed
    if crontab -l 2>/dev/null | grep -qF "backup_agents.sh"; then
        echo "Cron job already installed. Current entry:"
        crontab -l | grep "backup_agents"
        return
    fi

    # Add to crontab
    (crontab -l 2>/dev/null || true; echo "$cron_line") | crontab -
    echo "Cron job installed: daily at 3 AM"
    echo "  ${cron_line}"
    echo ""
    echo "Verify with: crontab -l"
}

# ---- Main ----

case "${1:-}" in
    --install-cron)
        install_cron
        ;;
    --list)
        do_list
        ;;
    --prune)
        do_prune
        ;;
    --help|-h)
        head -20 "$0" | tail -18
        ;;
    *)
        do_backup
        do_prune
        ;;
esac