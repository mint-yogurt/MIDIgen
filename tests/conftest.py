"""Tests run against a throwaway config and catalogue. They need *some* SoundFont to render with;
by default the system's TimGM6mb (a test fixture only, never offered to compositions), or
$MIDIGEN_TEST_SF2."""

import os
from pathlib import Path

import pytest

from midigen import catalog, config

TEST_SF2 = Path(os.environ.get("MIDIGEN_TEST_SF2", "/usr/share/sounds/sf2/TimGM6mb.sf2"))


@pytest.fixture(scope="session")
def cfg(tmp_path_factory):
    if not TEST_SF2.exists():
        pytest.skip(f"no test SoundFont at {TEST_SF2}")
    base = tmp_path_factory.mktemp("midigen")
    (base / "sf").mkdir()
    (base / "sf" / "testfont.sf2").symlink_to(TEST_SF2)
    c = config.load()
    c.data = {**c.data, "paths": {**c.data["paths"], "soundfont_dirs": [str(base / "sf")],
                                  "catalog": str(base / "catalog"), "notes": str(base / "notes.toml")}}
    return c


@pytest.fixture(scope="session")
def cat(cfg):
    return catalog.build(cfg)


@pytest.fixture
def kit(cat):
    return next(p for p in cat["soundfonts"][0]["presets"] if p["kind"] == "drum kit")
