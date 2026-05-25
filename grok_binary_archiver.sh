#!/bin/bash
# grok_binary_archiver.sh — Archives every grok binary version
# Watches ~/.grok/bin/ for new grok-* files and copies them to archive/
# Run in background: nohup ./grok_binary_archiver.sh
#
# Rollback: ln -sf grok-<version> ~/.grok/bin/grok

BINDIR="$HOME/.grok/bin"
ARCHIVE="$BINDIR/archive"
INTERVAL=60

mkdir -p "$ARCHIVE"

echo "[archiver] Watching $BINDIR for new grok binaries (every ${INTERVAL}s)"
echo "[archiver] Archive: $ARCHIVE"

while true; do
    for f in "$BINDIR"/grok-*; do
        [ -f "$f" ] || continue
        bn=$(basename "$f")
        [[ "$bn" == *.tmp.* ]] && continue
        [[ "$bn" == *.link.* ]] && continue
        [[ "$bn" == *.bak ]] && continue
        if [ ! -f "$ARCHIVE/$bn" ]; then
            cp "$f" "$ARCHIVE/$bn"
            echo "[archiver] $(date +%Y-%m-%dT%H:%M:%S) Archived: $bn ($(du -h "$ARCHIVE/$bn" | cut -f1))"
        fi
    done
    sleep "$INTERVAL"
done
