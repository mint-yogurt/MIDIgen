"""Read what a SoundFont 2 file contains, straight from its RIFF chunks (no dependencies).

What comes out per preset is only what the file states: name, bank, program, the
instruments it layers, which keys actually have samples, and whether those samples loop
(sustain while held) or play once. Nothing here guesses at how anything sounds.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

GEN_INSTRUMENT = 41
GEN_KEY_RANGE = 43
GEN_SAMPLE_ID = 53
GEN_SAMPLE_MODES = 54
GEN_ROOT_KEY = 58

INFO_FIELDS = {
    "ifil": "version", "isng": "sound_engine", "INAM": "name", "irom": "rom", "iver": "rom_version",
    "ICRD": "created", "IENG": "engineer", "IPRD": "product", "ICOP": "copyright",
    "ICMT": "comment", "ISFT": "software",
}


class SF2Error(ValueError):
    pass


@dataclass
class Preset:
    name: str
    bank: int
    program: int
    instruments: list[str] = field(default_factory=list)
    keys: set[int] = field(default_factory=set)       # MIDI notes that have at least one sample
    zones: int = 0
    looped_zones: int = 0                              # zones whose sample loops while the key is held
    one_shot_seconds: float = 0.0                      # longest unlooped sample (a note can't outlast it)
    root_keys: set[int] = field(default_factory=set)   # the pitches the samples were recorded at


@dataclass
class SoundFont:
    path: Path
    info: dict
    presets: list[Preset]
    sample_bytes: int


def _chunks(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        cid, size = struct.unpack_from("<4sI", data, pos)
        yield cid.decode("latin-1"), pos + 8, size
        pos += 8 + size + (size & 1)


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def _records(data: bytes, off: int, size: int, fmt: str) -> list[tuple]:
    rec = struct.calcsize(fmt)
    return [struct.unpack_from(fmt, data, off + i * rec) for i in range(size // rec)]


def _zones(bags: list[tuple], gens: list[tuple], first: int, last: int) -> list[dict[int, int]]:
    """Generator dicts for the bag range [first, last). Amounts stay raw unsigned 16-bit."""
    out = []
    for b in range(first, last):
        g0, g1 = bags[b][0], bags[b + 1][0]
        out.append({gens[g][0]: gens[g][1] for g in range(g0, g1)})
    return out


def _key_range(zone: dict[int, int], default: tuple[int, int] = (0, 127)) -> tuple[int, int]:
    if GEN_KEY_RANGE not in zone:
        return default
    amount = zone[GEN_KEY_RANGE]
    return amount & 0xFF, (amount >> 8) & 0xFF


def _lists(path: Path) -> tuple[dict[str, bytes], int]:
    """Top-level LIST chunks by type, read into memory, except the sample data, which is
    only measured (a big SoundFont is mostly samples)."""
    lists: dict[str, bytes] = {}
    sample_bytes = 0
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"sfbk":
            raise SF2Error(f"{path} is not a SoundFont 2 file")
        pos = 12
        while pos + 8 <= size:
            f.seek(pos)
            cid, csize = struct.unpack("<4sI", f.read(8))
            if cid == b"LIST":
                kind = f.read(4).decode("latin-1")
                if kind == "sdta":
                    sub, end = pos + 12, pos + 8 + csize
                    while sub + 8 <= end:
                        f.seek(sub)
                        scid, ssize = struct.unpack("<4sI", f.read(8))
                        if scid in (b"smpl", b"sm24"):
                            sample_bytes += ssize
                        sub += 8 + ssize + (ssize & 1)
                else:
                    lists[kind] = f.read(csize - 4)
            pos += 8 + csize + (csize & 1)
    return lists, sample_bytes


def read(path: str | Path) -> SoundFont:
    path = Path(path)
    lists, sample_bytes = _lists(path)

    info: dict = {}
    if "INFO" in lists:
        data = lists["INFO"]
        for cid, off, size in _chunks(data, 0, len(data)):
            if cid in ("ifil", "iver") and size >= 4:
                major, minor = struct.unpack_from("<HH", data, off)
                info[INFO_FIELDS[cid]] = f"{major}.{minor:02d}"
            elif cid in INFO_FIELDS:
                info[INFO_FIELDS[cid]] = _text(data[off:off + size])

    if "pdta" not in lists:
        raise SF2Error(f"{path} has no preset data (pdta)")
    data = lists["pdta"]
    pdta = {cid: (off, size) for cid, off, size in _chunks(data, 0, len(data))}
    try:
        phdr = _records(data, *pdta["phdr"], "<20sHHHIII")
        pbag = _records(data, *pdta["pbag"], "<HH")
        pgen = _records(data, *pdta["pgen"], "<HH")
        inst = _records(data, *pdta["inst"], "<20sH")
        ibag = _records(data, *pdta["ibag"], "<HH")
        igen = _records(data, *pdta["igen"], "<HH")
        shdr = _records(data, *pdta["shdr"], "<20sIIIIIBbHH") if "shdr" in pdta else []
    except KeyError as e:
        raise SF2Error(f"{path} is missing the {e.args[0]} chunk") from None

    def instrument_zones(i: int) -> tuple[str, list[dict[int, int]], dict[int, int]]:
        zones = _zones(ibag, igen, inst[i][1], inst[i + 1][1])
        glob = {}
        if zones and GEN_SAMPLE_ID not in zones[0]:
            glob, zones = zones[0], zones[1:]
        return _text(inst[i][0]), [z for z in zones if GEN_SAMPLE_ID in z], glob

    presets = []
    for p in range(len(phdr) - 1):                      # the last record is the "EOP" terminator
        name, program, bank, bag0 = phdr[p][:4]
        zones = _zones(pbag, pgen, bag0, phdr[p + 1][3])
        pglob = {}
        if zones and GEN_INSTRUMENT not in zones[0]:
            pglob, zones = zones[0], zones[1:]
        preset = Preset(name=_text(name), bank=bank, program=program)
        for pz in zones:
            i = pz.get(GEN_INSTRUMENT)
            if i is None or i >= len(inst) - 1:
                continue
            plo, phi = _key_range(pz, _key_range(pglob))
            iname, izones, iglob = instrument_zones(i)
            if iname not in preset.instruments:
                preset.instruments.append(iname)
            for iz in izones:
                lo, hi = _key_range(iz, _key_range(iglob))
                lo, hi = max(lo, plo), min(hi, phi)
                if lo > hi:
                    continue
                preset.keys.update(range(lo, hi + 1))
                preset.zones += 1
                looped = iz.get(GEN_SAMPLE_MODES, iglob.get(GEN_SAMPLE_MODES, 0)) & 1
                preset.looped_zones += bool(looped)
                sid = iz[GEN_SAMPLE_ID]
                if sid < len(shdr):
                    _, start, end, _, _, rate, pitch = shdr[sid][:7]
                    root = iz.get(GEN_ROOT_KEY, iglob.get(GEN_ROOT_KEY, 0xFFFF))
                    preset.root_keys.add(pitch if root > 127 else root)
                    if not looped and rate:
                        preset.one_shot_seconds = max(preset.one_shot_seconds, (end - start) / rate)
        presets.append(preset)
    presets.sort(key=lambda pr: (pr.bank, pr.program))
    return SoundFont(path=path, info=info, presets=presets, sample_bytes=sample_bytes)


def key_ranges(keys: set[int]) -> list[list[int]]:
    """{36, 37, 38, 40} -> [[36, 38], [40, 40]]"""
    out: list[list[int]] = []
    for k in sorted(keys):
        if out and k == out[-1][1] + 1:
            out[-1][1] = k
        else:
            out.append([k, k])
    return out
