# TUI color themes

Drop a JSON file here to add a theme. No code changes required (restart TUI or re-open theme picker after `reload_themes()`).

## File format

`my-theme.json`:

```json
{
  "id": "my-theme",
  "name": "My Theme",
  "colors": {
    "BG": "#1e1e2e",
    "FG": "#cdd6f4",
    "GRAY": "#6c7086",
    "RED": "#f38ba8",
    "GREEN": "#a6e3a1",
    "YELLOW": "#f9e2af",
    "BLUE": "#89b4fa",
    "PURPLE": "#cba6f7",
    "AQUA": "#94e2d5",
    "ORANGE": "#fab387",
    "BR_RED": "#f38ba8",
    "BR_GREEN": "#a6e3a1",
    "BR_YELLOW": "#f9e2af",
    "BR_BLUE": "#89b4fa",
    "BR_PURPLE": "#cba6f7",
    "BR_AQUA": "#94e2d5",
    "BR_ORANGE": "#fab387",
    "DARK1": "#313244",
    "DARK2": "#45475a",
    "DARK3": "#585b70",
    "DARK4": "#6c7086"
  }
}
```

- **id**: key used by Ctrl+T selector and `~/.config/abidetui/theme.json`
- **name**: shown in the picker
- **colors**: all keys above are required (missing keys fall back to Gruvbox Dark)

Built-ins: `gruvbox-dark`, `gruvbox-light`, `solarized-dark`.
