#!/usr/bin/env bash
# Alias: blank TUI with in-app pick (same as launch_tui.sh with no -a).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launch_tui.sh" "$@"
