"""SoundFont discovery and the instrument catalogue the composing LLM chooses from.

Three kinds of information per preset, kept apart so nobody mistakes one for another:
  - facts from the file: name, bank, program, mapped keys, instruments (sf2.py)
  - measured: render one note dry and measure attack, sustain, release, brightness, level
  - listened: notes a person wrote after hearing it (config/notes.toml)
plus "name_words": words picked out of the preset name, which say nothing reliable about sound.

The catalogue is cached in catalog/catalog.json and rebuilt for any file that changed.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import mido
import numpy as np

from . import gm, render, sf2
from .config import Config

ANALYSIS_VERSION = 1
CATALOG_VERSION = 2             # bump when an entry gains fields, so cached entries are rebuilt
ANALYSIS_RATE = 22050
SLOT_S, HOLD_S = 4.5, 2.0
BATCH = 100                     # presets per measuring render (~0.15 GB of float audio)

NAME_WORDS = {
    "pad", "warm", "bright", "soft", "dark", "sweep", "halo", "choir", "voice", "vox", "aah", "ooh", "bell", "bells",
    "crystal", "glass", "glock", "chime", "celesta", "vibe", "vibes", "marimba", "music", "box", "saw", "square",
    "sine", "pulse", "analog", "analogue", "digital", "fm", "dx", "dx7", "poly", "polysynth", "synth", "brass",
    "horn", "horns", "trumpet", "sax", "strings", "string", "orchestra", "slow", "fast", "stab", "hit", "bass",
    "slap", "fretless", "finger", "pick", "acid", "rubber", "piano", "epiano", "rhodes", "wurly", "clav", "organ",
    "lead", "atmosphere", "space", "fantasia", "new", "age", "metallic", "bowed", "rain", "echo", "echoes", "sci",
    "fi", "goblin", "air", "breath", "flute", "pan", "whistle", "guitar", "clean", "dist", "distortion", "muted",
    "jazz", "steel", "nylon", "harp", "kalimba", "timpani", "taiko", "stereo", "detuned", "chorus", "wide", "mellow",
    "hard", "funky", "power", "ambient", "noise", "vintage", "80s", "90s", "solo", "ensemble", "section",
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "soundfont"


def discover(cfg: Config) -> list[tuple[str, Path]]:
    """(id, path) for every .sf2 in the configured folders, each real file once."""
    seen: dict[Path, str] = {}
    ids: set[str] = set()
    out = []
    for d in cfg.soundfont_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() != ".sf2" or not p.is_file():
                continue
            real = p.resolve()
            if real in seen:
                continue
            sid, n = slug(p.stem), 2
            while sid in ids:
                sid, n = f"{slug(p.stem)}_{n}", n + 1
            seen[real] = sid
            ids.add(sid)
            out.append((sid, p))
    return out


def _stamp(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def name_words(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return [w for w in words if w in NAME_WORDS]


# ---------------------------------------------------------------- measuring

def _analysis_note(keys: set[int]) -> int | None:
    if not keys:
        return None
    return min(keys, key=lambda k: (abs(k - 60), k))


def analyze(cfg: Config, sf_id: str, path: Path, presets: list[sf2.Preset]) -> dict[str, dict]:
    """Render one held note per melodic preset (dry, velocity 100) and measure it."""
    melodic = [p for p in presets if p.bank < 128 and _analysis_note(p.keys) is not None]
    out = {}
    for i in range(0, len(melodic), BATCH):
        out.update(_analyze_batch(cfg, sf_id, path, melodic[i:i + BATCH]))
    return out


def _analyze_batch(cfg: Config, sf_id: str, path: Path, melodic: list[sf2.Preset]) -> dict[str, dict]:
    """One FluidSynth run: every preset gets a 4.5 s slot on channel 1."""
    tpb, tempo = 480, 500000                                       # 120 bpm: 1 beat = 0.5 s
    beats = lambda s: int(round(s / 0.5 * tpb))
    mid = mido.MidiFile(ticks_per_beat=tpb)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage("set_tempo", tempo=tempo))
    now = 0
    for i, p in enumerate(melodic):
        start, note = beats(i * SLOT_S), _analysis_note(p.keys)
        tr.append(mido.Message("control_change", channel=0, control=0, value=p.bank, time=start - now))
        tr.append(mido.Message("program_change", channel=0, program=p.program))
        tr.append(mido.Message("note_on", channel=0, note=note, velocity=100))
        tr.append(mido.Message("note_off", channel=0, note=note, velocity=0, time=beats(HOLD_S)))
        tr.append(mido.Message("control_change", channel=0, control=120, value=0, time=beats(SLOT_S - HOLD_S) - 1))
        now = start + beats(SLOT_S) - 1
    tr.append(mido.MetaMessage("end_of_track", time=1))
    cfg.catalog_dir.mkdir(parents=True, exist_ok=True)
    midi_path = cfg.catalog_dir / f".analysis_{sf_id}_{os.getpid()}.mid"
    mid.save(midi_path)
    settings = {"synth.gain": 1.0, "synth.reverb.active": 0, "synth.chorus.active": 0,
                "synth.midi-bank-select": "gs", "synth.polyphony": 256}
    try:
        audio = render.fluid_render(cfg, path, midi_path, settings, ANALYSIS_RATE)
    finally:
        midi_path.unlink(missing_ok=True)
    out = {}
    slot = int(SLOT_S * ANALYSIS_RATE)
    for i, p in enumerate(melodic):
        seg = audio[i * slot:(i + 1) * slot].mean(axis=1)
        out[f"{p.bank}:{p.program}"] = _measure(seg, _analysis_note(p.keys))
    return out


def _measure(x: np.ndarray, note: int) -> dict:
    sr, win = ANALYSIS_RATE, int(0.01 * ANALYSIS_RATE)
    n = len(x) // win
    if n == 0:
        return {"silent": True}
    env = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(env.max())
    if peak < 1e-5:
        return {"note": note, "silent": True}
    db = lambda v: float(20 * np.log10(max(v, 1e-9) / peak))
    attack = int(np.argmax(env >= 0.9 * peak)) * 10
    hold_end = int(HOLD_S * 100)
    sustain_db = db(env[hold_end - 5: hold_end].mean())
    after = env[hold_end:]
    below = np.nonzero(after < peak * 10 ** (-40 / 20))[0]
    release = int(below[0]) * 10 if len(below) else int((SLOT_S - HOLD_S) * 1000)
    a, b = int(0.05 * sr), int(0.6 * sr)
    seg = x[a:b] * np.hanning(b - a)
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    centroid = float((spec * freqs).sum() / max(spec.sum(), 1e-12))
    f0 = 440 * 2 ** ((note - 69) / 12)
    ratio = centroid / f0
    return {
        "note": note,
        "level_db": round(float(20 * np.log10(peak)), 1),
        "attack_ms": attack,
        "sustain_db": round(sustain_db, 1),
        "release_ms": release,
        "centroid_hz": round(centroid),
        "brightness": round(ratio, 1),
    }


def describe(m: dict) -> dict:
    """Plain-word labels from the measurements. Thresholds are rough and documented in README."""
    if not m or m.get("silent"):
        return {"summary": "silent at the test note"}
    attack = "instant" if m["attack_ms"] < 20 else "fast" if m["attack_ms"] < 90 else \
        "soft" if m["attack_ms"] < 350 else "slow swell"
    sustain = "holds" if m["sustain_db"] > -6 else "fades while held" if m["sustain_db"] > -20 else \
        "decays" if m["sustain_db"] > -40 else "one-shot"
    tone = "dark" if m["brightness"] < 2.5 else "mellow" if m["brightness"] < 5 else \
        "bright" if m["brightness"] < 10 else "very bright"
    release = "long release" if m["release_ms"] >= 1000 else ""
    return {"attack": attack, "sustain": sustain, "tone": tone,
            "summary": ", ".join(x for x in (f"{attack} attack", sustain, tone, release) if x)}


# ---------------------------------------------------------------- building

def _notes(cfg: Config) -> dict:
    if not cfg.notes_file.exists():
        return {}
    with open(cfg.notes_file, "rb") as f:
        return tomllib.load(f)


def gm_layout(presets: list[sf2.Preset]) -> bool:
    """Is bank 0 laid out as General MIDI (program 0 = piano ... 127 = gunshot)?

    A font of patches numbered in whatever order (like a synth's patch list) isn't, and then its
    program numbers say nothing about the instrument."""
    return len([p for p in presets if p.bank == 0]) >= 120


def _entry(cfg: Config, sf_id: str, path: Path, measure: bool, cached: dict | None) -> dict:
    font = sf2.read(path)
    is_gm = gm_layout(font.presets)
    stamp = _stamp(path)
    analysis = {}
    if cached and cached.get("stamp") == stamp and cached.get("analysis_version") == ANALYSIS_VERSION:
        analysis = cached.get("analysis", {})
    if measure and not analysis:
        analysis = analyze(cfg, sf_id, path, font.presets)
    presets = []
    for p in font.presets:
        key = f"{p.bank}:{p.program}"
        e = {"bank": p.bank, "program": p.program, "name": p.name, "instruments": p.instruments,
             "keys": sf2.key_ranges(p.keys), "zones": p.zones}
        if p.bank == 128:
            e["kind"] = "drum kit"
            e["drums"] = {str(k): gm.DRUMS[k] for k in sorted(p.keys) if k in gm.DRUMS}
            e["gm_drum_notes_missing"] = [k for k in range(35, 82) if k not in p.keys]
        else:
            e["kind"] = "melodic"
            if is_gm:
                e["gm_program"] = gm.PROGRAMS[p.program]
                e["gm_family"] = gm.family(p.program)
                if p.bank:
                    e["gs_variation_of"] = gm.PROGRAMS[p.program]
            e["name_words"] = name_words(p.name)
            e["samples_loop"] = p.looped_zones == p.zones and p.zones > 0
            if p.one_shot_seconds and not p.looped_zones:
                e["one_shot_seconds"] = round(p.one_shot_seconds, 2)
            if 0 < len(p.root_keys) <= 3:
                e["root_keys"] = sorted(p.root_keys)
            if key in analysis:
                e["measured"] = analysis[key]
                e["measured_words"] = describe(analysis[key])
        presets.append(e)
    opts = cfg.soundfont_options(sf_id)
    return {
        "id": sf_id, "file": path.name, "path": str(path), "stamp": stamp, "catalog_version": CATALOG_VERSION,
        "layout": "General MIDI" if is_gm else "custom (program numbers are just patch slots)",
        "info": font.info, "description": opts.get("description", ""),
        "sample_mb": round(font.sample_bytes / 1048576, 1),
        "banks": sorted({p.bank for p in font.presets}),
        "preset_count": len(font.presets),
        "analysis_version": ANALYSIS_VERSION if analysis else None,
        "analysis": analysis,
        "presets": presets,
    }


def build(cfg: Config, measure: bool = True, force: bool = False) -> dict:
    """Scan the folders, (re)read changed files, write catalog.json and catalog.md."""
    old = {} if force else {f["id"]: f for f in _load_raw(cfg).get("soundfonts", [])}
    fonts, errors = [], []
    for sf_id, path in discover(cfg):
        cached = old.get(sf_id)
        try:
            if cached and cached.get("path") == str(path) and cached.get("stamp") == _stamp(path) \
                    and cached.get("catalog_version") == CATALOG_VERSION \
                    and (cached.get("analysis") or not measure):
                cached["description"] = cfg.soundfont_options(sf_id).get("description", "")
                fonts.append(cached)
            else:
                fonts.append(_entry(cfg, sf_id, path, measure, cached))
        except Exception as e:                       # one odd file must not take the catalogue down
            errors.append({"id": sf_id, "file": str(path), "error": f"{type(e).__name__}: {e}"})
    cat = {"version": 1, "soundfonts": fonts, "errors": errors}
    _apply_notes(cfg, cat)
    cfg.catalog_dir.mkdir(parents=True, exist_ok=True)
    _write(cfg.catalog_dir / "catalog.json", json.dumps(cat, indent=1) + "\n")
    _write(cfg.catalog_dir / "catalog.md", to_markdown(cat))
    return cat


def _write(path: Path, text: str) -> None:
    """Write then rename, so a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _load_raw(cfg: Config) -> dict:
    p = cfg.catalog_dir / "catalog.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _apply_notes(cfg: Config, cat: dict) -> None:
    notes = _notes(cfg)
    for f in cat["soundfonts"]:
        for p in f["presets"]:
            text = notes.get(f["id"], {}).get(f"{p['bank']}:{p['program']}")
            p.pop("listened", None)
            if text:
                p["listened"] = text


def load(cfg: Config) -> dict:
    """The catalogue, rebuilt (without measuring new files) if fonts were added, changed or removed."""
    cat = _load_raw(cfg)
    have = {f["id"]: (f.get("stamp"), f.get("catalog_version")) for f in cat.get("soundfonts", [])}
    now = {sf_id: (_stamp(p), CATALOG_VERSION) for sf_id, p in discover(cfg)}
    if not cat or have != now:
        cat = build(cfg, measure=False)
    else:
        _apply_notes(cfg, cat)
    return cat


def find_preset(cat: dict, sf_id: str, bank: int, program: int) -> dict | None:
    for f in cat.get("soundfonts", []):
        if f["id"] == sf_id:
            return next((p for p in f["presets"] if p["bank"] == bank and p["program"] == program), None)
    return None


def font(cat: dict, sf_id: str) -> dict | None:
    return next((f for f in cat.get("soundfonts", []) if f["id"] == sf_id), None)


# ---------------------------------------------------------------- LLM-readable view

def _keys_text(ranges: list[list[int]]) -> str:
    if ranges == [[0, 127]]:
        return "all"
    return " ".join(f"{gm.note_name(a)}..{gm.note_name(b)}" if a != b else gm.note_name(a) for a, b in ranges)


def _samples_text(p: dict) -> str:
    parts = []
    if "one_shot_seconds" in p:
        parts.append(f"one-shot {p['one_shot_seconds']:g} s")
    elif p.get("samples_loop"):
        parts.append("loop")
    else:
        parts.append("partly looped")
    if p.get("root_keys"):
        parts.append("root " + "/".join(gm.note_name(k) for k in p["root_keys"]))
    return ", ".join(parts)


def to_markdown(cat: dict, soundfonts: list[str] | None = None, families: list[str] | None = None) -> str:
    """Compact catalogue for a prompt. Optionally only some fonts or GM families."""
    out = ["# Instrument catalogue", "",
           "Columns: `bank:program` to use in a track, the preset's own name, the General MIDI slot it sits in "
           "(only for fonts laid out as General MIDI), the keys that have samples (notes outside are silent), "
           "and how one held C4-ish note measured when rendered dry (attack / sustain / tone; level = peak dBFS "
           "at full gain, louder presets need less volume). `samples` says whether a held note can sustain: "
           "`loop` sustains as long as the key is held; `one-shot N s` plays its recording once and is silent "
           "after N seconds however long the note is, so give those notes short lengths or re-strike them. "
           "`root` is the pitch a single sample was recorded at: far from it the sound is pitch-shifted "
           "(faster and thinner above, slower and darker below), so stay within about an octave of it. "
           "`name` words come from the preset name only. `heard` is a person's listening note and is the most "
           "trustworthy description.", ""]
    for f in cat.get("soundfonts", []):
        if soundfonts and f["id"] not in soundfonts:
            continue
        title = f["info"].get("name") or f["file"]
        out.append(f"## SoundFont `{f['id']}`: {title}")
        out.append(f"File {f['file']}, {f['preset_count']} presets, {f['sample_mb']} MB of samples, banks {f['banks']}.")
        if f.get("description"):
            out.append(f["description"])
        extra = "; ".join(f"{k}: {v}" for k, v in f["info"].items() if k in ("product", "comment", "engineer"))
        if extra:
            out.append(extra)
        if f.get("layout", "General MIDI") != "General MIDI":
            out.append("Not a General MIDI layout: program numbers are just patch slots. Choose by name, "
                       "measurements and listening notes.")
        out.append("")
        mel = [p for p in f["presets"] if p["kind"] == "melodic"
               and (not families or p.get("gm_family") in families or "gm_family" not in p)]
        if mel:
            out += ["| bank:prog | preset | GM slot | keys | samples | measured | level | name | heard |",
                    "|---|---|---|---|---|---|---|---|---|"]
            for p in mel:
                m = p.get("measured_words", {}).get("summary", "not measured")
                lvl = p.get("measured", {}).get("level_db", "")
                slot = f"{p['gm_family']}: {p['gm_program']}" if "gm_family" in p else "-"
                out.append(f"| {p['bank']}:{p['program']} | {p['name']} | {slot} | {_keys_text(p['keys'])} | "
                           f"{_samples_text(p)} | {m} | {lvl} | {' '.join(p['name_words'])} | "
                           f"{p.get('listened', '')} |")
            out.append("")
        kits = [p for p in f["presets"] if p["kind"] == "drum kit"]
        if kits and (not families or "drums" in families):
            out.append("Drum kits (channel 10 only; bank 128):")
            for p in kits:
                missing = p["gm_drum_notes_missing"]
                cover = "all GM drum notes 35-81" if not missing else \
                    f"GM drum notes 35-81 except {', '.join(str(k) for k in missing)}"
                heard = f" Heard: {p['listened']}" if p.get("listened") else ""
                out.append(f"- `128:{p['program']}` {p['name']}: has {cover}.{heard}")
            out.append("")
    if cat.get("errors"):
        out.append("Unreadable files (not usable): " + ", ".join(e["file"] for e in cat["errors"]))
    return "\n".join(out) + "\n"
