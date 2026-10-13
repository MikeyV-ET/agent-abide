"""Agent family from roster home path (LeviSmith/Holt, Sixel/Astro, flat MikeyV)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


FLAT_FAMILY = "MikeyV"


def family_from_home(home: str | Path, *, agents_dir: str | Path) -> str:
    """Return family name for an agent home.

    Nested under agents_dir (``LeviSmith/Holt``) → parent dir name.
    Flat (``~/agents/Sr``) → MikeyV.
    """
    home_p = Path(home).expanduser().resolve()
    root = Path(agents_dir).expanduser().resolve()
    try:
        rel = home_p.relative_to(root)
    except ValueError:
        parts = home_p.parts
        if len(parts) >= 2:
            return parts[-2]
        return FLAT_FAMILY
    parts = rel.parts
    if len(parts) >= 2:
        return parts[0]
    return FLAT_FAMILY


def catalog_label(family: str, name: str) -> str:
    if not family or family == FLAT_FAMILY:
        return name
    return f"{family} / {name}"


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    family: str
    home: Path
    has_asdaaas: bool

    @property
    def label(self) -> str:
        return catalog_label(self.family, self.name)


def catalog_entries(
    agents: dict,
    *,
    agents_dir: str | Path,
    require_asdaaas: bool = True,
) -> list[CatalogEntry]:
    """Sorted catalog from agents.json map."""
    root = Path(agents_dir).expanduser()
    out: list[CatalogEntry] = []
    for name, acfg in (agents or {}).items():
        if not isinstance(acfg, dict):
            continue
        home = Path(acfg.get("home") or (root / name)).expanduser()
        family = str(acfg.get("family") or "").strip() or family_from_home(home, agents_dir=root)
        has = (home / "asdaaas").is_dir()
        if require_asdaaas and not has:
            continue
        out.append(CatalogEntry(name=name, family=family, home=home, has_asdaaas=has))
    out.sort(key=lambda e: (e.family.lower(), e.name.lower()))
    return out
