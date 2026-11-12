"""Piece -> Standard MIDI File (type 1) with mido.

Track 0 is the conductor (title, tempo map, meter, key, section markers). Every other track
starts with its setup: bank select (CC0, GS style, skipped on the drum channel), program,
volume, pan, reverb and chorus sends and the pitch-bend range (RPN 0), then the music.
Each track's name and an instrument-name meta say which SoundFont preset it was written for,
since the file itself can't select a SoundFont.
"""

from __future__ import annotations

from pathlib import Path

import mido

from .spec import Piece, Track

TPB = 480


def _tick(beat: float) -> int:
    return int(round(beat * TPB))


def _conductor(piece: Piece, end_tick: int) -> mido.MidiTrack:
    events = [(0, 0, mido.MetaMessage("track_name", name=piece.title[:120])),
              (0, 0, mido.MetaMessage("time_signature", numerator=piece.numerator,
                                      denominator=piece.denominator, clocks_per_click=24,
                                      notated_32nd_notes_per_beat=8))]
    if piece.key:
        events.append((0, 0, mido.MetaMessage("key_signature", key=piece.key)))
    for beat, bpm in piece.tempo_map:
        events.append((_tick(beat), 1, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm))))
    for s in piece.sections:
        events.append((_tick((s["bar"] - 1) * piece.bar_beats), 2, mido.MetaMessage("marker", text=s["name"][:60])))
    return _to_track(events, end_tick)


def _track(t: Track, end_tick: int) -> mido.MidiTrack:
    ch = t.channel
    cc = lambda n, v: mido.Message("control_change", channel=ch, control=n, value=v)
    setup = [mido.MetaMessage("track_name", name=t.name[:120]),
             mido.MetaMessage("instrument_name", name=f"{t.soundfont} {t.bank}:{t.program} {t.preset_name}"[:120])]
    if not t.drums:
        setup += [cc(0, t.bank), cc(32, 0)]
    setup += [mido.Message("program_change", channel=ch, program=t.program),
              cc(7, t.volume), cc(10, t.pan), cc(91, t.reverb), cc(93, t.chorus),
              cc(101, 0), cc(100, 0), cc(6, t.bend_range), cc(38, 0), cc(101, 127), cc(100, 127)]
    # (tick, order, message): at equal ticks, setup < program/controls < note-offs < note-ons.
    events = [(0, 0, m) for m in setup]
    for beat, bank, program in t.programs:
        if not t.drums:
            events.append((_tick(beat), 1, cc(0, bank)))
        events.append((_tick(beat), 1, mido.Message("program_change", channel=ch, program=program)))
    for beat, n, v in t.controls:
        events.append((_tick(beat), 1, cc(n, v)))
    for beat, wheel in t.bends:
        events.append((_tick(beat), 1, mido.Message("pitchwheel", channel=ch, pitch=wheel)))
    for n in t.notes:
        on, off = _tick(n.start), max(_tick(n.start) + 1, _tick(n.start + n.length))
        events.append((on, 3, mido.Message("note_on", channel=ch, note=n.pitch, velocity=n.velocity)))
        events.append((off, 2, mido.Message("note_off", channel=ch, note=n.pitch, velocity=0)))
    return _to_track(events, end_tick)


def _to_track(events: list[tuple], end_tick: int) -> mido.MidiTrack:
    events.sort(key=lambda e: (e[0], e[1]))
    tr = mido.MidiTrack()
    now = 0
    for tick, _, msg in events:
        tr.append(msg.copy(time=tick - now))
        now = tick
    tr.append(mido.MetaMessage("end_of_track", time=max(0, end_tick - now)))
    return tr


def build(piece: Piece, tracks: list[Track] | None = None) -> mido.MidiFile:
    """The whole piece, or only some of its tracks (a render stem) with the same conductor."""
    end_tick = _tick(piece.beats)
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    mid.tracks.append(_conductor(piece, end_tick))
    for t in tracks if tracks is not None else piece.tracks:
        mid.tracks.append(_track(t, end_tick))
    return mid


def save(piece: Piece, path: Path, tracks: list[Track] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build(piece, tracks).save(path)
    return path


def inspect(path: Path) -> dict:
    """Re-read a written file: a sanity check that it parses and what it contains."""
    mid = mido.MidiFile(path)
    notes = 0
    channels: set[int] = set()
    open_notes: dict[tuple[int, int], int] = {}
    unmatched = 0
    for tr in mid.tracks:
        for msg in tr:
            if msg.type == "note_on" and msg.velocity > 0:
                notes += 1
                channels.add(msg.channel + 1)
                open_notes[(msg.channel, msg.note)] = open_notes.get((msg.channel, msg.note), 0) + 1
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                k = (msg.channel, msg.note)
                if open_notes.get(k):
                    open_notes[k] -= 1
                else:
                    unmatched += 1
    return {"type": mid.type, "tracks": len(mid.tracks), "ticks_per_beat": mid.ticks_per_beat,
            "seconds": round(mid.length, 3), "notes": notes, "channels": sorted(channels),
            "hanging_notes": sum(open_notes.values()), "unmatched_note_offs": unmatched}
