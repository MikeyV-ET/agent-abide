#!/usr/bin/env bash
# Deprecated alias: pick is now built into launch_tui.sh (-a optional on TTY).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launch_tui.sh" "$@"
