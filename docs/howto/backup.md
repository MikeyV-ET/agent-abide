# Backup Reference

## Daily Automated Backup (cron, 3 AM)

Script: `scripts/backup_agents.sh`

Backs up agents state, infrastructure code, project repos, session metadata, and git bundles. ~1GB. Runs via cron daily at 3 AM.

```bash
# Run manually
bash scripts/backup_agents.sh

# Install cron
bash scripts/backup_agents.sh --install-cron

# List backups
bash scripts/backup_agents.sh --list
```

**What's included:** `~/agents/`, `~/projects/mikeyv-infra/`, `~/projects/agent-abide/`, `~/projects/erics-notes/`, `~/projects/socratic-arena/`, `~/.grok/sessions/` (metadata only: signals.json, summary.json, chat_history.jsonl), git bundles for all repos.

**Retention:** 7 daily mirrors, 4 weekly snapshots.

## Full Home Backup (manual, to USB)

Full `/home/eric` backup to USB drive D. ~5-6GB compressed. Run when needed for disaster recovery.

### 1. Mount USB drive

From WSL:
```bash
sudo mount -t drvfs D: /mnt/d
```

Verify: `ls /mnt/d/` and `touch /mnt/d/test && rm /mnt/d/test`

### 2. Run backup

**Important:** `--exclude` flags must come BEFORE the directory argument. GNU tar silently ignores excludes placed after the directory.

```bash
tar czf - -C / \
  --exclude="home/eric/.cache" \
  --exclude="home/eric/.npm" \
  --exclude="home/eric/.local" \
  --exclude="home/eric/backups" \
  --exclude="home/eric/.nvm" \
  --exclude="home/eric/.vscode-server" \
  --exclude="home/eric/projects/dev-arena-tutor" \
  --exclude="home/eric/.grok/bin" \
  --exclude="home/eric/.grok/worktrees" \
  --exclude="home/eric/.grok/activity" \
  --exclude="home/eric/.grok/marketplace-cache" \
  --exclude="home/eric/.grok/vendor" \
  --exclude="home/eric/.grok/logs" \
  --exclude="home/eric/.grok/irc_logs" \
  --exclude="home/eric/.grok/bundled" \
  --exclude="home/eric/.grok/docs" \
  --exclude="home/eric/.grok/skills" \
  home/eric \
  | split -b 3G - /mnt/d/home_eric_backup_lean_YYYYMMDD.tar.gz.
```

Split into 3GB chunks for NTFS compatibility. Output: `.tar.gz.aa`, `.tar.gz.ab`, etc.

**Before re-running:** delete previous chunks first. `split` overwrites existing files but doesn't remove extras — a smaller backup leaves stale chunks from a larger prior run.

```bash
rm -f /mnt/d/home_eric_backup_lean_YYYYMMDD.tar.gz.a*
```

### 3. Verify

```bash
cat /mnt/d/home_eric_backup_lean_YYYYMMDD.tar.gz.a* | gzip -t && echo "VALID"
```

### 4. Restore

```bash
cat /mnt/d/home_eric_backup_lean_YYYYMMDD.tar.gz.a* | tar xzf - -C /
```

### What's excluded and why

| Excluded | Size | Reason |
|---|---|---|
| `.cache` | 4.6G | Regenerable |
| `.npm` | 5.3G | Regenerable |
| `.local` | 6.9G | Regenerable (pip packages, etc.) |
| `backups/` | 5.5G | Already backed up separately |
| `.nvm` | 369M | Regenerable (node versions) |
| `.vscode-server` | 224M | Regenerable |
| `projects/dev-arena-tutor` | 6.4G | Archived project |
| `.grok/bin` | 769M | Downloadable binaries |
| `.grok/worktrees` | 191M | Temporary |
| `.grok/{activity,logs,vendor,...}` | ~100M | Regenerable caches/logs |

### What's included (critical)

| Included | Size | Why |
|---|---|---|
| `.grok/sessions/` | 6.0G | Agent history, updates.jsonl, compaction segments |
| `agents/` | 1.0G | Lab notebooks, notes, config, doorbells |
| `projects/agent-abide/` | 5.8M | Infrastructure code |
| `projects/mikeyv-infra/` | 19M | Legacy infrastructure |
| `projects/socratic-arena/` | 345M | Arena project |
| `projects/erics-notes/` | 592K | Eric's notes |
