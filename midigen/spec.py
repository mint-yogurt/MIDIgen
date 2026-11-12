"""The composition format ("midigen/1"): its JSON schema, and turning it into timed events.

Timing is musical, never ticks or seconds:
  - a beat is always a quarter note; tempo is quarter notes per minute
  - bars and beats count from 1, like a musician counts ("bar 3, beat 2.5")
  - a note is [bar, beat, pitch, length_in_beats, velocity]; pitch is a MIDI number, a note
    name ("C4" = 60 = middle C), a drum name on drum tracks ("kick"), or a list (a chord)
  - patterns are reusable clips; a track's "play" list places them at bars, with repeats

The JSON schema only checks shape (it stays inside the subset structured-output APIs accept);
check() does everything else and reports every problem it finds, with where it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import catalog, gm

FORMAT = "midigen/1"
DRUM_CHANNEL = 10                          # 1-based, as written in compositions
CONTROLLERS = {"modulation": 1, "volume": 7, "pan": 10, "expression": 11, "sustain": 64,
               "reverb": 91, "chorus": 93}
EPS = 1e-6
MAX_ERRORS = 60

_num = {"type": "number"}
_int = {"type": "integer"}
_str = {"type": "string"}
_pitch = {"anyOf": [_int, _str]}
_note = {"type": "array", "minItems": 5, "maxItems": 5,
         "items": {"anyOf": [_num, _str, {"type": "array", "items": _pitch}]}}
_at = {"bar": _int, "beat": _num}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SCHEMA = _obj({
    "format": {"type": "string", "enum": [FORMAT]},
    "title": _str,
    "description": _str,
    "arrangement_notes": _str,
    "tempo": _num,
    "time_signature": _str,
    "key": _str,
    "bars": _int,
    "target_seconds": _num,
    "tempo_changes": {"type": "array", "items": _obj({**_at, "bpm": _num}, ["bar", "beat", "bpm"])},
    "sections": {"type": "array", "items": _obj(
        {"name": _str, "bar": _int, "bars": _int, "description": _str}, ["name", "bar", "bars"])},
    "mix": _obj({"reverb": _str, "tail_seconds": _num}, []),
    "patterns": {"type": "object", "additionalProperties": _obj(
        {"bars": _int, "notes": {"type": "array", "items": _note}}, ["bars", "notes"])},
    "tracks": {"type": "array", "minItems": 1, "items": _obj({
        "name": _str,
        "role": _str,
        "soundfont": _str,
        "bank": _int,
        "program": _int,
        "channel": _int,
        "volume": _int,
        "pan": _int,
        "reverb": _int,
        "chorus": _int,
        "bend_range": _int,
        "play": {"type": "array", "items": _obj({
            "pattern": _str, "bar": _int, "repeat": _int, "transpose": _int, "velocity": _num,
        }, ["pattern", "bar"])},
        "notes": {"type": "array", "items": _note},
        "controls": {"type": "array", "items": _obj({
            **_at, "cc": {"anyOf": [_int, _str]}, "value": _int,
            "to_bar": _int, "to_beat": _num, "to_value": _int,
        }, ["bar", "beat", "cc", "value"])},
        "bends": {"type": "array", "items": _obj({
            **_at, "semitones": _num, "to_bar": _int, "to_beat": _num, "to_semitones": _num,
        }, ["bar", "beat", "semitones"])},
        "program_changes": {"type": "array", "items": _obj({**_at, "bank": _int, "program": _int},
                                                            ["bar", "beat", "program"])},
    }, ["name", "soundfont", "bank", "program", "channel"])},
}, ["format", "title", "tempo", "time_signature", "bars", "tracks"])


@dataclass
class Note:
    start: float            # beats from the start of the piece
    length: float
    pitch: int
    velocity: int
    where: str


@dataclass
class Track:
    name: str
    soundfont: str
    bank: int
    program: int
    channel: int            # 0-based MIDI channel
    volume: int
    pan: int
    reverb: int
    chorus: int
    bend_range: int
    drums: bool
    notes: list[Note] = field(default_factory=list)
    controls: list[tuple[float, int, int]] = field(default_factory=list)       # (beat, cc, value)
    bends: list[tuple[float, int]] = field(default_factory=list)               # (beat, -8192..8191)
    programs: list[tuple[float, int, int]] = field(default_factory=list)       # (beat, bank, program)
    preset_name: str = ""


@dataclass
class Piece:
    title: str
    bar_beats: float
    numerator: int
    denominator: int
    bars: int
    tempo_map: list[tuple[float, float]]      # (beat, bpm), sorted, first at 0
    key: str | None
    tracks: list[Track]
    sections: list[dict]
    reverb: str
    tail_seconds: float

    @property
    def beats(self) -> float:
        return self.bars * self.bar_beats

    def seconds_at(self, beat: float) -> float:
        t, prev_beat, bpm = 0.0, 0.0, self.tempo_map[0][1]
        for b, new_bpm in self.tempo_map[1:]:
            if b >= beat:
                break
            t += (b - prev_beat) * 60 / bpm
            prev_beat, bpm = b, new_bpm
        return t + (beat - prev_beat) * 60 / bpm

    @property
    def seconds(self) -> float:
        return self.seconds_at(self.beats)


@dataclass
class Result:
    errors: list[str]
    warnings: list[str]
    piece: Piece | None

    @property
    def ok(self) -> bool:
        return not self.errors and self.piece is not None


KEY_RE = re.compile(r"^\s*([A-Ga-g])([#b]?)\s*(major|minor|maj|min|m)?\s*$", re.I)


def _key(text: str) -> str | None:
    """'F major' -> 'F', 'A minor' -> 'Am' (mido's key_signature names)."""
    m = KEY_RE.match(text or "")
    if not m:
        return None
    root = m.group(1).upper() + m.group(2)
    return root + ("m" if (m.group(3) or "").lower() in ("minor", "min", "m") else "")


def check(comp: dict, cat: dict) -> Result:
    """Validate a composition against the catalogue and expand it into a Piece."""
    errors: list[str] = []
    warnings: list[str] = []

    def err(msg: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(msg)

    import jsonschema
    shape = sorted(jsonschema.Draft202012Validator(SCHEMA).iter_errors(comp), key=lambda e: list(e.path))
    for e in shape[:MAX_ERRORS]:
        where = "/".join(str(p) for p in e.path) or "(top level)"
        err(f"{where}: {e.message}")
    if errors:
        return Result(errors, warnings, None)

    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", comp["time_signature"])
    if not m or int(m.group(2)) not in (1, 2, 4, 8, 16) or int(m.group(1)) < 1:
        err(f"time_signature {comp['time_signature']!r}: write it like \"4/4\", \"3/4\" or \"6/8\"")
        return Result(errors, warnings, None)
    num, den = int(m.group(1)), int(m.group(2))
    bar_beats = num * 4 / den
    bars = comp["bars"]
    if bars < 1:
        err("bars must be at least 1")
    if not 20 <= comp["tempo"] <= 300:
        err(f"tempo {comp['tempo']} is outside 20-300 quarter notes per minute")
    if errors:
        return Result(errors, warnings, None)
    total = bars * bar_beats

    def pos(bar, beat, where: str, limit_bars: int = bars) -> float | None:
        if not 1 <= bar <= limit_bars:
            err(f"{where}: bar {bar} is outside 1-{limit_bars}")
            return None
        if not 1 - EPS <= beat < bar_beats + 1 - EPS:
            err(f"{where}: beat {beat} is outside 1 to <{bar_beats + 1:g} (a {num}/{den} bar has {bar_beats:g} "
                f"quarter-note beats)")
            return None
        return (bar - 1) * bar_beats + (beat - 1)

    tempo_map = [(0.0, float(comp["tempo"]))]
    for i, tc in enumerate(comp.get("tempo_changes", [])):
        p = pos(tc["bar"], tc["beat"], f"tempo_changes[{i}]")
        if p is not None:
            if not 20 <= tc["bpm"] <= 300:
                err(f"tempo_changes[{i}]: bpm {tc['bpm']} is outside 20-300")
            tempo_map.append((p, float(tc["bpm"])))
    tempo_map.sort()

    def seconds(beat: float) -> float:
        t, prev, bpm = 0.0, 0.0, tempo_map[0][1]
        for b, new in tempo_map[1:]:
            if b >= beat:
                break
            t, prev, bpm = t + (b - prev) * 60 / bpm, b, new
        return t + (beat - prev) * 60 / bpm

    mix = comp.get("mix", {})
    reverb = mix.get("reverb", "room")
    tail = float(mix.get("tail_seconds", 0) or 0)
    if tail < 0 or tail > 10:
        err(f"mix.tail_seconds {tail} is outside 0-10")

    sections = comp.get("sections", [])
    for i, s in enumerate(sections):
        if s["bar"] < 1 or s["bars"] < 1 or s["bar"] + s["bars"] - 1 > bars:
            err(f"sections[{i}] '{s['name']}': bars {s['bar']}-{s['bar'] + s['bars'] - 1} don't fit in {bars} bars")

    # Patterns: expand each to (offset_beats, pitches, length, velocity, where) once.
    patterns: dict[str, tuple[int, list]] = {}
    for name, pat in comp.get("patterns", {}).items():
        if pat["bars"] < 1:
            err(f"patterns.{name}: bars must be at least 1")
            continue
        rows = []
        for j, row in enumerate(pat["notes"]):
            where = f"patterns.{name}.notes[{j}]"
            parsed = _note_row(row, where, err)
            if parsed is None:
                continue
            bar, beat, pitch, length, vel = parsed
            p = pos(bar, beat, where, pat["bars"])
            if p is None:
                continue
            # A note may ring past the end of its pattern (over the bar line, into whatever comes
            # next); only the end of the piece is a hard limit, checked once patterns are placed.
            rows.append((p, pitch, length, vel, where))
        patterns[name] = (pat["bars"], rows)

    used: set[str] = set()
    tracks: list[Track] = []
    channel_owner: dict[int, tuple] = {}
    for ti, t in enumerate(comp["tracks"]):
        tw = f"tracks[{ti}] '{t['name']}'"
        ch = t["channel"]
        if not 1 <= ch <= 16:
            err(f"{tw}: channel {ch} is outside 1-16 (channels count from 1; drums use 10)")
            continue
        drums = ch == DRUM_CHANNEL
        font = catalog.font(cat, t["soundfont"])
        preset = None
        if font is None:
            have = ", ".join(f["id"] for f in cat.get("soundfonts", [])) or "none are installed"
            err(f"{tw}: SoundFont '{t['soundfont']}' isn't available ({have})")
        else:
            preset = catalog.find_preset(cat, t["soundfont"], t["bank"], t["program"])
            if preset is None:
                err(f"{tw}: SoundFont '{t['soundfont']}' has no preset at bank {t['bank']}, program {t['program']}")
            elif drums and preset["kind"] != "drum kit":
                err(f"{tw}: channel 10 is the drum channel; pick a drum kit (bank 128), not '{preset['name']}'")
            elif not drums and preset["kind"] == "drum kit":
                err(f"{tw}: '{preset['name']}' is a drum kit; drum kits only play on channel 10")
        sound = (t["soundfont"], t["bank"], t["program"])
        if ch in channel_owner and channel_owner[ch][0] != sound:
            err(f"{tw}: channel {ch} is already used by '{channel_owner[ch][1]}' with a different sound; "
                f"give each sound its own channel")
        channel_owner.setdefault(ch, (sound, t["name"]))
        for k, lo, hi in (("volume", 0, 127), ("pan", 0, 127), ("reverb", 0, 127), ("chorus", 0, 127),
                          ("bend_range", 1, 24)):
            if k in t and not lo <= t[k] <= hi:
                err(f"{tw}: {k} {t[k]} is outside {lo}-{hi}")
        track = Track(name=t["name"], soundfont=t["soundfont"], bank=t["bank"], program=t["program"],
                      channel=ch - 1, volume=t.get("volume", 100), pan=t.get("pan", 64),
                      reverb=t.get("reverb", 40), chorus=t.get("chorus", 0), bend_range=t.get("bend_range", 2),
                      drums=drums, preset_name=preset["name"] if preset else "")

        def add(start: float, pitch, length: float, vel: int, where: str, transpose: int = 0) -> None:
            for one in (pitch if isinstance(pitch, list) else [pitch]):
                n = _pitch_number(one, drums, where, err)
                if n is None:
                    continue
                n += 0 if drums else transpose
                if not 0 <= n <= 127:
                    err(f"{where}: pitch {one} transposed by {transpose} leaves MIDI range 0-127")
                    continue
                track.notes.append(Note(start, length, n, vel, where))

        for j, row in enumerate(t.get("notes", [])):
            where = f"{tw} notes[{j}]"
            parsed = _note_row(row, where, err)
            if parsed is None:
                continue
            bar, beat, pitch, length, vel = parsed
            p = pos(bar, beat, where)
            if p is not None:
                add(p, pitch, length, vel, where)

        for j, pl in enumerate(t.get("play", [])):
            where = f"{tw} play[{j}]"
            if pl["pattern"] not in patterns:
                err(f"{where}: no pattern called '{pl['pattern']}'"
                    + (f" (patterns: {', '.join(patterns)})" if patterns else ""))
                continue
            used.add(pl["pattern"])
            pbars, rows = patterns[pl["pattern"]]
            repeat, transpose, vscale = pl.get("repeat", 1), pl.get("transpose", 0), pl.get("velocity", 1.0)
            if repeat < 1:
                err(f"{where}: repeat must be at least 1")
                continue
            if drums and transpose:
                warnings.append(f"{where}: transpose is ignored on the drum channel")
            if not 0 < vscale <= 2:
                err(f"{where}: velocity scale {vscale} is outside 0-2 (1.0 = as written)")
                continue
            base = pos(pl["bar"], 1, where)
            if base is None:
                continue
            for r in range(repeat):
                offset = base + r * pbars * bar_beats
                for p, pitch, length, vel, nwhere in rows:
                    v = max(1, min(127, round(vel * vscale)))
                    add(offset + p, pitch, length, v, f"{where} (repeat {r + 1}) -> {nwhere}", transpose)

        for j, c in enumerate(t.get("controls", [])):
            where = f"{tw} controls[{j}]"
            cc = CONTROLLERS.get(c["cc"]) if isinstance(c["cc"], str) else c["cc"]
            if cc is None or not 0 <= cc <= 127:
                err(f"{where}: cc {c['cc']!r} isn't a controller number 0-127 or one of {', '.join(CONTROLLERS)}")
                continue
            if cc in (0, 32):
                err(f"{where}: use program_changes for bank selection, not cc {cc}")
                continue
            vals = [c["value"]] + ([c["to_value"]] if "to_value" in c else [])
            if any(not 0 <= v <= 127 for v in vals):
                err(f"{where}: values must be 0-127")
                continue
            p = pos(c["bar"], c["beat"], where)
            if p is None:
                continue
            if "to_bar" in c or "to_value" in c:
                if not all(k in c for k in ("to_bar", "to_beat", "to_value")):
                    err(f"{where}: a ramp needs to_bar, to_beat and to_value")
                    continue
                q = pos(c["to_bar"], c["to_beat"], where)
                if q is None:
                    continue
                if q <= p:
                    err(f"{where}: the ramp ends before it starts")
                    continue
                track.controls += _ramp(p, q, c["value"], c["to_value"], cc)
            else:
                track.controls.append((p, cc, c["value"]))

        for j, b in enumerate(t.get("bends", [])):
            where = f"{tw} bends[{j}]"
            rng = track.bend_range
            semis = [b["semitones"]] + ([b["to_semitones"]] if "to_semitones" in b else [])
            if any(abs(s) > rng + EPS for s in semis):
                err(f"{where}: bends must stay within ±{rng} semitones (the track's bend_range)")
                continue
            p = pos(b["bar"], b["beat"], where)
            if p is None:
                continue
            wheel = lambda s: max(-8192, min(8191, round(s / rng * 8192)))
            if "to_bar" in b or "to_semitones" in b:
                if not all(k in b for k in ("to_bar", "to_beat", "to_semitones")):
                    err(f"{where}: a bend glide needs to_bar, to_beat and to_semitones")
                    continue
                q = pos(b["to_bar"], b["to_beat"], where)
                if q is None or q <= p:
                    err(f"{where}: the glide ends before it starts")
                    continue
                steps = max(2, int((q - p) * 16))
                for k in range(steps + 1):
                    s = b["semitones"] + (b["to_semitones"] - b["semitones"]) * k / steps
                    track.bends.append((p + (q - p) * k / steps, wheel(s)))
            else:
                track.bends.append((p, wheel(b["semitones"])))

        for j, pc in enumerate(t.get("program_changes", [])):
            where = f"{tw} program_changes[{j}]"
            bank = pc.get("bank", t["bank"])
            if font is not None and catalog.find_preset(cat, t["soundfont"], bank, pc["program"]) is None:
                err(f"{where}: SoundFont '{t['soundfont']}' has no preset at bank {bank}, program {pc['program']}")
                continue
            p = pos(pc["bar"], pc["beat"], where)
            if p is not None:
                track.programs.append((p, bank, pc["program"]))

        _check_notes(track, preset, total, err, warnings, seconds)
        if not track.notes:
            warnings.append(f"{tw}: has no notes")
        tracks.append(track)

    for name in patterns:
        if name not in used:
            warnings.append(f"patterns.{name}: never played by any track")

    if comp.get("key") and _key(comp["key"]) is None:
        warnings.append(f"key {comp['key']!r} isn't like 'F major' or 'A minor'; no key signature written")

    if errors:
        return Result(errors, warnings, None)

    piece = Piece(title=comp["title"], bar_beats=bar_beats, numerator=num, denominator=den, bars=bars,
                  tempo_map=tempo_map, key=_key(comp.get("key", "")), tracks=tracks, sections=sections,
                  reverb=reverb, tail_seconds=tail)

    target = comp.get("target_seconds")
    if target:
        off = piece.seconds - target
        # Only a warning: real music lands where its phrases end, and post-production trims and
        # stitches anyway.
        if abs(off) > max(1.0, 0.2 * target):
            warnings.append(f"the piece lasts {piece.seconds:.1f} s ({bars} bars of {num}/{den}) but target_seconds "
                            f"is {target}; one bar is {bar_beats * 60 / comp['tempo']:.2f} s at {comp['tempo']} bpm")

    peak = _max_polyphony(tracks)
    if peak > 64:
        warnings.append(f"up to {peak} notes sound at once; above ~64 the synth may steal voices")
    return Result(errors, warnings, piece if not errors else None)


def _note_row(row: list, where: str, err) -> tuple | None:
    bar, beat, pitch, length, vel = row
    bad = []
    if not isinstance(bar, int) or isinstance(bar, bool):
        bad.append(f"bar {bar!r} must be a whole number")
    if not isinstance(beat, (int, float)) or isinstance(beat, bool):
        bad.append(f"beat {beat!r} must be a number")
    if not isinstance(length, (int, float)) or isinstance(length, bool) or length <= 0:
        bad.append(f"length {length!r} must be a positive number of beats")
    if not isinstance(vel, (int, float)) or isinstance(vel, bool) or not 1 <= vel <= 127:
        bad.append(f"velocity {vel!r} must be 1-127")
    if isinstance(pitch, list) and not pitch:
        bad.append("an empty chord")
    if bad:
        err(f"{where} {row}: " + "; ".join(bad) + " (a note is [bar, beat, pitch, length, velocity])")
        return None
    return bar, float(beat), pitch, float(length), int(round(vel))


def _pitch_number(p, drums: bool, where: str, err) -> int | None:
    if isinstance(p, bool):
        err(f"{where}: pitch {p!r} isn't a note")
        return None
    if isinstance(p, (int, float)):
        if p != int(p) or not 0 <= p <= 127:
            err(f"{where}: pitch {p} must be a whole MIDI note 0-127")
            return None
        return int(p)
    if isinstance(p, str):
        if drums and p.lower() in gm.DRUM_ALIASES:
            return gm.DRUM_ALIASES[p.lower()]
        try:
            return gm.note_number(p)
        except ValueError as e:
            hint = f"; drum names: {', '.join(sorted(gm.DRUM_ALIASES))}" if drums else ""
            err(f"{where}: {e}{hint}")
            return None
    err(f"{where}: pitch {p!r} isn't a note number, note name or drum name")
    return None


def _ramp(p: float, q: float, v0: int, v1: int, cc: int) -> list[tuple[float, int, int]]:
    steps = max(1, min(abs(v1 - v0), int((q - p) * 16)))
    out, last = [], None
    for k in range(steps + 1):
        v = round(v0 + (v1 - v0) * k / steps)
        if v != last:
            out.append((p + (q - p) * k / steps, cc, v))
            last = v
    return out


def _check_notes(track: Track, preset: dict | None, total: float, err, warnings: list[str], seconds) -> None:
    keys = set()
    if preset:
        for a, b in preset["keys"]:
            keys.update(range(a, b + 1))
    family_range = gm.FAMILY_RANGE.get(preset["gm_family"]) if preset and preset.get("gm_family") else None
    silent, odd = [], []
    for n in track.notes:
        if n.start + n.length > total + EPS:
            err(f"{n.where}: note ends at beat {n.start + n.length + 1:g} of the piece, after the last bar "
                f"(the piece is {total:g} beats long)")
        if keys and n.pitch not in keys:
            silent.append(n)
        elif family_range and not family_range[0] <= n.pitch <= family_range[1]:
            odd.append(n)
    if preset and preset.get("one_shot_seconds"):
        limit = preset["one_shot_seconds"]
        long_notes = [n for n in track.notes if seconds(n.start + n.length) - seconds(n.start) > limit * 1.5]
        if long_notes:
            warnings.append(f"{track.name}: '{preset['name']}' is a one-shot sample of {limit:g} s; "
                            f"{len(long_notes)} notes are written longer than it can sound")
    for n in silent[:5]:
        name = gm.DRUMS.get(n.pitch, str(n.pitch)) if track.drums else gm.note_name(n.pitch)
        err(f"{n.where}: '{track.preset_name}' has no sample for {name} ({n.pitch}); it would be silent")
    if len(silent) > 5:
        err(f"{track.name}: {len(silent) - 5} more silent notes like that")
    if odd:
        lo, hi = family_range
        warnings.append(f"{track.name}: {len(odd)} notes outside the usual range for a {preset['gm_family']} "
                        f"({gm.note_name(lo)}-{gm.note_name(hi)}), e.g. {gm.note_name(odd[0].pitch)}")

    # Same key on the same channel overlapping: the first note-off would cut the second note.
    track.notes.sort(key=lambda n: (n.start, n.pitch))
    last: dict[int, Note] = {}
    keep, fixed, dupes = [], 0, 0
    for n in track.notes:
        prev = last.get(n.pitch)
        if prev and abs(prev.start - n.start) < EPS:
            dupes += 1
            prev.length = max(prev.length, n.length)
            prev.velocity = max(prev.velocity, n.velocity)
            continue
        if prev and prev.start + prev.length > n.start + EPS:
            prev.length = n.start - prev.start
            fixed += 1
        last[n.pitch] = n
        keep.append(n)
    track.notes = keep
    if fixed:
        warnings.append(f"{track.name}: shortened {fixed} notes that overlapped the next note on the same key")
    if dupes:
        warnings.append(f"{track.name}: merged {dupes} notes written twice at the same time and key")


def _max_polyphony(tracks: list[Track]) -> int:
    edges = []
    for t in tracks:
        for n in t.notes:
            edges += [(n.start, 1), (n.start + n.length, -1)]
    edges.sort(key=lambda e: (e[0], e[1]))
    now = peak = 0
    for _, d in edges:
        now += d
        peak = max(peak, now)
    return peak


def bar_seconds(tempo: float, time_signature: str = "4/4") -> float:
    num, den = (int(x) for x in time_signature.split("/"))
    return num * 4 / den * 60 / tempo


def bars_for(seconds: float, tempo: float, time_signature: str = "4/4") -> int:
    return max(1, round(seconds / bar_seconds(tempo, time_signature)))
