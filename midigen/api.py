"""What callers use: validate a composition, render it, audition presets.

    from midigen import api
    report = api.validate(comp)                       # {"ok", "errors", "warnings", "summary"}
    result = api.render(comp, "out/dir", name="jingle")   # writes jingle.mid/.wav/.json/.render.json

Everything here is also on the command line (midigen --help), with JSON output for scripts.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, gm, midi, spec
from . import render as synth
from .config import Config, load


class CompositionError(ValueError):
    def __init__(self, errors: list[str], warnings: list[str]):
        super().__init__("composition is invalid:\n- " + "\n- ".join(errors))
        self.errors, self.warnings = errors, warnings


def _cfg(cfg: Config | None) -> Config:
    return cfg or load()


def _summary(piece: spec.Piece) -> dict:
    tracks = []
    for t in piece.tracks:
        first = min((n.start for n in t.notes), default=None)
        last = max((n.start + n.length for n in t.notes), default=None)
        tracks.append({
            "name": t.name, "soundfont": t.soundfont, "bank": t.bank, "program": t.program,
            "preset": t.preset_name, "channel": t.channel + 1, "notes": len(t.notes),
            "first_note_s": round(piece.seconds_at(first), 3) if first is not None else None,
            "last_note_end_s": round(piece.seconds_at(last), 3) if last is not None else None,
            "first_note_bar": int(first // piece.bar_beats) + 1 if first is not None else None,
        })
    return {"title": piece.title, "bars": piece.bars, "time_signature": f"{piece.numerator}/{piece.denominator}",
            "seconds": round(piece.seconds, 3), "tracks": tracks}


def validate(comp: dict, cfg: Config | None = None) -> dict:
    cfg = _cfg(cfg)
    res = spec.check(comp, catalog.load(cfg))
    out = {"ok": res.ok, "errors": res.errors, "warnings": res.warnings}
    if res.piece:
        out["summary"] = _summary(res.piece)
    return out


def safe_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "composition"


def render(comp: dict, out_dir: str | Path, name: str | None = None, cfg: Config | None = None,
           keep_stems: bool | None = None) -> dict:
    """Validate, write <name>.mid, render <name>.wav, save <name>.json and <name>.render.json.

    Raises CompositionError (with .errors/.warnings) if the composition is invalid."""
    cfg = _cfg(cfg)
    cat = catalog.load(cfg)
    res = spec.check(comp, cat)
    if not res.ok:
        raise CompositionError(res.errors, res.warnings)
    piece = res.piece
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or safe_name(piece.title)
    rcfg = cfg.render
    sr = int(rcfg.get("sample_rate", 44100))
    keep_stems = rcfg.get("keep_stems", False) if keep_stems is None else keep_stems
    rooms = cfg.reverb_presets()
    if piece.reverb not in rooms:
        raise CompositionError([f"mix.reverb '{piece.reverb}' isn't one of: {', '.join(rooms)}"], res.warnings)
    tail = piece.tail_seconds or float(rcfg.get("tail_seconds", 3.0))

    started = time.monotonic()
    mid_path = midi.save(piece, out_dir / f"{name}.mid")
    groups: dict[tuple[str, int], list[spec.Track]] = {}
    for t in piece.tracks:
        groups.setdefault((t.soundfont, t.channel), []).append(t)
    stems_info, jobs = [], []
    with tempfile.TemporaryDirectory(prefix="midigen-stems-") as tmp:
        for (sf_id, ch), tracks in groups.items():
            font = catalog.font(cat, sf_id)
            settings = {**rcfg.get("fluidsynth", {}), **rooms[piece.reverb],
                        **cfg.soundfont_options(sf_id).get("fluidsynth", {})}
            stem_mid = midi.save(piece, Path(tmp) / f"{sf_id}_ch{ch + 1}.mid", tracks)
            jobs.append((Path(font["path"]), stem_mid, settings))
            stems_info.append({"soundfont": sf_id, "channel": ch + 1, "tracks": [t.name for t in tracks],
                               "settings": settings})
        stems = synth.render_many(cfg, jobs, sr)
    mixed = synth.mix(stems)
    final, gain_db = synth.finish(mixed, sr, piece.seconds + tail, float(rcfg.get("peak_dbfs", -1.0)))
    wav_path = out_dir / f"{name}.wav"
    synth.write_wav(wav_path, final, sr, int(rcfg.get("bits", 16)))
    gain = 10 ** (gain_db / 20)
    for info, audio in zip(stems_info, stems):
        info["levels_in_mix"] = synth.levels(audio * gain, sr)
        if keep_stems:
            stem_wav = out_dir / f"{name}.stems" / f"{safe_name('_'.join(info['tracks']))}.wav"
            synth.write_wav(stem_wav, audio[: len(final)] * gain, sr, int(rcfg.get("bits", 16)))
            info["wav"] = str(stem_wav)

    json_path = out_dir / f"{name}.json"
    json_path.write_text(json.dumps(comp, indent=1) + "\n")
    manifest = {
        "name": name,
        "files": {"midi": str(mid_path), "wav": str(wav_path), "composition": str(json_path)},
        "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "render_seconds": round(time.monotonic() - started, 2),
        "fluidsynth": synth.fluidsynth_version(cfg),
        "sample_rate": sr, "bits": int(rcfg.get("bits", 16)), "reverb": piece.reverb,
        "wav_seconds": round(len(final) / sr, 3), "gain_db": round(gain_db, 1),
        "levels": synth.levels(final, sr),
        "midi": midi.inspect(mid_path),
        "soundfonts": {sf: {"file": catalog.font(cat, sf)["file"], "stamp": catalog.font(cat, sf)["stamp"]}
                       for sf in sorted({t.soundfont for t in piece.tracks})},
        "summary": _summary(piece),
        "stems": stems_info,
        "warnings": res.warnings,
    }
    render_path = out_dir / f"{name}.render.json"
    manifest["files"]["manifest"] = str(render_path)
    render_path.write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


# ---------------------------------------------------------------- auditioning

PHRASES = ("auto", "melody", "chords", "bass", "pad", "drums")


def _phrase_for(preset: dict) -> str:
    if preset["kind"] == "drum kit":
        return "drums"
    fam = preset.get("gm_family")
    if fam is None:
        words = set(preset.get("name_words", []))
        fam = "bass" if "bass" in words else "synth pad" if words & {"pad", "strings", "string", "choir", "vox",
                                                                     "voice", "organ"} else \
            "piano" if words & {"piano", "epiano", "clav", "guitar", "marimba"} else "synth lead"
    if fam == "bass":
        return "bass"
    if fam in ("synth pad", "ensemble", "organ") or fam == "strings":
        return "pad"
    if fam in ("piano", "guitar", "chromatic percussion"):
        return "chords"
    return "melody"


def audition_composition(sf_id: str, preset: dict, phrase: str = "auto") -> dict:
    """A short, fixed test phrase for one preset, as a normal composition."""
    phrase = _phrase_for(preset) if phrase == "auto" else phrase
    drums = preset["kind"] == "drum kit"
    base = {"format": spec.FORMAT, "title": f"Audition {sf_id} {preset['bank']}:{preset['program']} "
            f"{preset['name']} ({phrase})", "tempo": 100, "time_signature": "4/4", "bars": 4,
            "mix": {"reverb": "room"}}
    track = {"name": preset["name"], "soundfont": sf_id, "bank": preset["bank"], "program": preset["program"],
             "channel": 10 if drums else 1, "volume": 110, "reverb": 30}
    if phrase == "drums":
        track["notes"] = []
        for bar in range(1, 5):
            for e in range(8):
                track["notes"].append([bar, 1 + e / 2, "closed_hat" if e % 4 else "open_hat" if bar == 4 and e == 7
                                       else "closed_hat", 0.25, 70 + (20 if e % 2 == 0 else 0)])
            track["notes"] += [[bar, 1, "kick", 0.5, 110], [bar, 3, "kick", 0.5, 100], [bar, 3.5, "kick", 0.5, 80],
                               [bar, 2, "snare", 0.5, 105], [bar, 4, "snare", 0.5, 110]]
        track["notes"] += [[1, 1, "crash", 1, 100], [4, 3.5, "tom_high", 0.25, 90], [4, 3.75, "tom_mid", 0.25, 95],
                           [4, 4, "tom_low", 0.25, 100], [4, 4.5, "tom_floor", 0.5, 105]]
    else:
        keys = set()
        for a, b in preset["keys"]:
            keys.update(range(a, b + 1))
        root = 36 if phrase == "bass" else 60
        while root not in keys and root < 108:
            root += 12
        notes = {
            "melody": [[1, 1, 0, 1, 90], [1, 2, 4, 1, 85], [1, 3, 7, 1, 90], [1, 4, 12, 1, 95], [2, 1, 11, 2, 95],
                       [2, 3, 7, 2, 80], [3, 1, 9, 1.5, 90], [3, 2.5, 7, 0.5, 80], [3, 3, 5, 1, 85], [3, 4, 4, 1, 80],
                       [4, 1, 0, 3, 90]],
            "chords": [[1, 1, [0, 4, 7], 2, 90], [1, 3, [0, 4, 7], 2, 70], [2, 1, [-3, 0, 4], 4, 90],
                       [3, 1, [-7, -3, 0, 5], 4, 90], [4, 1, [-5, -1, 2, 7], 3.5, 95]],
            "bass": [[b, beat, iv, 0.9, 100 if beat == 1 else 85] for b, iv0 in ((1, 0), (2, -3), (3, 5), (4, 7))
                     for beat, iv in ((1, iv0), (2, iv0), (2.5, iv0 + 12), (3, iv0), (4, iv0 + 7))],
            "pad": [[1, 1, [-12, 0, 4, 7], 8, 85], [3, 1, [-15, -3, 0, 4], 7.5, 85]],
        }[phrase]

        def shift(p):
            return [root + x for x in p] if isinstance(p, list) else root + p
        track["notes"] = [[b, beat, shift(p), length, vel] for b, beat, p, length, vel in notes]
    base["tracks"] = [track]
    return base


def audition(sf_id: str, bank: int, program: int, phrase: str = "auto", out_dir: str | Path | None = None,
             cfg: Config | None = None) -> dict:
    """Render the test phrase for one preset. Cached under catalog/auditions/ unless out_dir is given."""
    cfg = _cfg(cfg)
    cat = catalog.load(cfg)
    preset = catalog.find_preset(cat, sf_id, bank, program)
    if preset is None:
        raise CompositionError([f"no preset {bank}:{program} in SoundFont '{sf_id}'"], [])
    comp = audition_composition(sf_id, preset, phrase)
    phrase = comp["title"].rsplit("(", 1)[1].rstrip(")")
    name = f"{sf_id}__{bank:03d}-{program:03d}__{safe_name(preset['name'])}__{phrase}"
    target = Path(out_dir) if out_dir else cfg.catalog_dir / "auditions" / sf_id
    wav = target / f"{name}.wav"
    font = catalog.font(cat, sf_id)
    stamp_file = target / f"{name}.render.json"
    if not out_dir and wav.exists() and stamp_file.exists():
        cached = json.loads(stamp_file.read_text())
        if cached.get("soundfonts", {}).get(sf_id, {}).get("stamp") == font["stamp"]:
            return cached
    return render(comp, target, name=name, cfg=cfg)


def compare(program: int, bank: int | None = None, phrase: str = "auto", out_dir: str | Path | None = None,
            soundfonts: list[str] | None = None, cfg: Config | None = None) -> list[dict]:
    """The same phrase on every SoundFont's preset at this program (and bank, if given): A/B listening."""
    cfg = _cfg(cfg)
    cat = catalog.load(cfg)
    results = []
    for f in cat["soundfonts"]:
        if soundfonts and f["id"] not in soundfonts:
            continue
        for p in f["presets"]:
            if p["program"] == program and (p["bank"] == bank if bank is not None else p["bank"] < 128):
                results.append(audition(f["id"], p["bank"], p["program"], phrase, out_dir, cfg))
    return results


def describe_gm(program: int) -> str:
    return f"{gm.family(program)}: {gm.PROGRAMS[program]}"
