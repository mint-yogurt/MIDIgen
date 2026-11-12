"""Load config/midigen.toml. Relative paths resolve against the MIDIgen folder."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "midigen.toml"


@dataclass
class Config:
    data: dict
    root: Path

    def path(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def soundfont_dirs(self) -> list[Path]:
        return [self.path(d) for d in self.data["paths"]["soundfont_dirs"]]

    @property
    def catalog_dir(self) -> Path:
        return self.path(self.data["paths"]["catalog"])

    @property
    def notes_file(self) -> Path:
        return self.path(self.data["paths"].get("notes", "soundfonts/notes.toml"))

    @property
    def render(self) -> dict:
        return self.data.get("render", {})

    @property
    def fluidsynth(self) -> str:
        return self.data.get("tools", {}).get("fluidsynth", "fluidsynth")

    def soundfont_options(self, sf_id: str) -> dict:
        return self.data.get("soundfonts", {}).get(sf_id, {})

    def reverb_presets(self) -> dict[str, dict]:
        return self.data.get("reverb", {})


def load(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("MIDIGEN_CONFIG") or DEFAULT_CONFIG)
    with open(path, "rb") as f:
        return Config(tomllib.load(f), ROOT)
