#!/usr/bin/env bash
# screenshot.sh — let an agent SEE the screen it is being told about.
#
# We run under WSL2 with Windows interop, so a multimodal agent can capture the
# host screen through powershell.exe and then read the PNG with its own vision.
# No X/Wayland screenshot binary is installed and none is needed.
#
#   scripts/screenshot.sh                      # whole virtual screen
#   scripts/screenshot.sh --window "Trip-G"    # only windows whose title matches
#   scripts/screenshot.sh --out /tmp/x.png     # choose the output path
#
# Prints the WSL path of the PNG. Feed that to your Read/vision tool.
#
# PRIVACY: a full-screen grab includes whatever else is open — mail, Slack,
# anything. Prefer --window. Captures land in /tmp; delete them when done.
set -euo pipefail

PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
OUT="/tmp/agent_screen_$(date +%H%M%S).png"
WINDOW=""
MAX_PX=1600   # downscale target; full 4K wastes vision tokens for no gain

while [ $# -gt 0 ]; do
    case "$1" in
        --window) WINDOW="${2:-}"; shift 2 ;;
        --out)    OUT="${2:-}";    shift 2 ;;
        --full-size) MAX_PX=0;     shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ -x "$PS" ] || { echo "no powershell.exe — not a WSL host with interop" >&2; exit 1; }

# PowerShell writes to the Windows temp dir; we copy it back into WSL after.
if [ -n "$WINDOW" ]; then
    # Window-targeted: find the first visible window whose title matches, and
    # capture only its rectangle. Needs the Win32 rect APIs.
    read -r -d '' SCRIPT <<'PSEOF' || true
param([string]$Match)
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
}
"@
$p = Get-Process | Where-Object { $_.MainWindowTitle -like "*$Match*" } | Select-Object -First 1
if (-not $p) { Write-Error "no window matching '$Match'"; exit 1 }
$r = New-Object W+RECT
[void][W]::GetWindowRect($p.MainWindowHandle, [ref]$r)
$w = $r.R - $r.L; $h = $r.B - $r.T
if ($w -le 0 -or $h -le 0) { Write-Error "window has no usable rect"; exit 1 }
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen((New-Object System.Drawing.Point $r.L, $r.T), [System.Drawing.Point]::Empty, (New-Object System.Drawing.Size $w, $h))
$out = Join-Path $env:TEMP 'agent_capture.png'
$bmp.Save($out)
Write-Output $out
PSEOF
    WINPATH="$("$PS" -NoProfile -Command "& { $SCRIPT }" -Match "$WINDOW" 2>/dev/null | tr -d '\r' | tail -1)"
else
    WINPATH="$("$PS" -NoProfile -Command "
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
\$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
\$bmp = New-Object System.Drawing.Bitmap \$b.Width, \$b.Height
\$g = [System.Drawing.Graphics]::FromImage(\$bmp)
\$g.CopyFromScreen(\$b.Location, [System.Drawing.Point]::Empty, \$b.Size)
\$out = Join-Path \$env:TEMP 'agent_capture.png'
\$bmp.Save(\$out)
Write-Output \$out" 2>/dev/null | tr -d '\r' | tail -1)"
fi

case "$WINPATH" in
    *.png) ;;
    *) echo "capture failed: ${WINPATH:-<no output>}" >&2; exit 1 ;;
esac

# C:\Users\x\... -> /mnt/c/Users/x/...
WSLPATH="$(printf '%s' "$WINPATH" | sed -e 's|\\|/|g' -e 's|^\([A-Za-z]\):|/mnt/\L\1|')"
cp "$WSLPATH" "$OUT"

if [ "$MAX_PX" -gt 0 ] && command -v python3 >/dev/null; then
    python3 - "$OUT" "$MAX_PX" <<'PY' 2>/dev/null || true
import sys
from PIL import Image
path, cap = sys.argv[1], int(sys.argv[2])
im = Image.open(path)
if max(im.size) > cap:
    im.thumbnail((cap, cap))
    im.save(path)
PY
fi

echo "$OUT"
