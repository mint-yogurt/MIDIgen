"""Composition requests for an LLM: (system, prompt, schema), the shape AI-TV's llm.complete_json takes.

    req = build_request("upbeat 80s jingle for a floor cleaner", target_seconds=15)
    comp = backend.complete_json(req["system"], req["prompt"], req["schema"])
    report = api.validate(comp)
    if not report["ok"]:
        comp = backend.complete_json(req["system"], repair_prompt(req, comp, report["errors"]), req["schema"])

write_request() saves the same thing as AI-TV's manual writing stages do (<stage>.request.md and
<stage>.schema.json), so a person or Claude Code can answer it by hand.

Steering is optional and comes in three layers, all plain text the caller can leave out:
  - cue: what the music is for (theme, jingle, sting, bed, credits), from midigen/cues/*.md
  - styles: genre touchstones, from midigen/styles/*.md; one, several (a blend) or none
  - context: whatever the production knows (format, era, scene, product), passed through as written
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import catalog, spec
from .config import Config, load

PROMPTS = Path(__file__).parent / "prompts"
CUES = Path(__file__).parent / "cues"
STYLES = Path(__file__).parent / "styles"


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def cards(folder: Path) -> dict[str, dict]:
    """Cards by id (file stem): {"id", "name", "aliases", "tempo", "text"}. Front matter is `key: value` lines."""
    out = {}
    for f in sorted(folder.glob("*.md")):
        raw = f.read_text()
        meta, body = {}, raw
        if raw.startswith("---\n"):
            head, _, body = raw[4:].partition("\n---\n")
            meta = dict(line.split(":", 1) for line in head.splitlines() if ":" in line)
            meta = {k.strip(): v.strip() for k, v in meta.items()}
        out[f.stem] = {"id": f.stem, "name": meta.get("name", f.stem),
                       "aliases": [a.strip() for a in meta.get("aliases", "").split(",") if a.strip()],
                       "tempo": meta.get("tempo", ""), "text": body.strip()}
    return out


def find_card(folder: Path, name: str) -> dict:
    """Look a card up by id, name or alias, ignoring case and punctuation. Raises ValueError."""
    want = _key(name)
    for card in cards(folder).values():
        if want in {_key(card["id"]), _key(card["name"]), *(_key(a) for a in card["aliases"])}:
            return card
    raise ValueError(f"no {folder.name[:-1]} card called {name!r}; have: "
                     + ", ".join(c["name"] for c in cards(folder).values()))


def system_prompt() -> str:
    return (PROMPTS / "system.md").read_text()


def timing_table(target_seconds: float | None, tempos: list[int] | None = None) -> str:
    if not target_seconds:
        return ""
    rows = ["| tempo | one 4/4 bar | bars for the target |", "|---|---|---|"]
    for bpm in tempos or (60, 72, 84, 96, 108, 120, 132, 150, 174):
        bar = spec.bar_seconds(bpm)
        n = spec.bars_for(target_seconds, bpm)
        rows.append(f"| {bpm} | {bar:.2f} s | {n} bars = {n * bar:.1f} s |")
    return "\n".join(rows)


def style_tempos(style_cards: list[dict]) -> list[int] | None:
    """Example tempos spread across the styles' usual ranges, so the table doesn't nudge toward one tempo."""
    found = set()
    for c in style_cards:
        lo, _, hi = c["tempo"].partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            lo, hi = int(lo), int(hi)
            found.update(round(lo + (hi - lo) * k / 4) for k in range(5))
    return sorted(found) or None


def example(cat: dict) -> dict | None:
    """A tiny valid composition using real presets, to show the shape. None if no fonts."""
    mel = kit = None
    for f in cat.get("soundfonts", []):
        for p in f["presets"]:
            if mel is None and p["kind"] == "melodic" and p.get("gm_family") in ("piano", "brass", "synth lead"):
                mel = (f["id"], p)
            if kit is None and p["kind"] == "drum kit":
                kit = (f["id"], p)
    if mel is None:
        for f in cat.get("soundfonts", []):
            mel = mel or next(((f["id"], p) for p in f["presets"] if p["kind"] == "melodic"), None)
    if mel is None:
        return None
    tracks = [{"name": "Stab", "role": "chord stabs", "soundfont": mel[0], "bank": mel[1]["bank"],
               "program": mel[1]["program"], "channel": 1, "volume": 95, "pan": 70, "reverb": 50,
               "notes": [[1, 1, ["C4", "E4", "G4"], 0.5, 100], [1, 2.5, ["D4", "F4", "A4"], 0.5, 90],
                         [1, 4, ["B3", "D4", "G4"], 1, 100], [2, 1, ["C4", "E4", "G4", "C5"], 3, 110]]}]
    if kit:
        tracks.append({"name": "Drums", "soundfont": kit[0], "bank": 128, "program": kit[1]["program"],
                       "channel": 10, "volume": 100, "reverb": 25,
                       "play": [{"pattern": "beat", "bar": 1}],
                       "notes": [[2, 1, ["kick", "crash"], 1, 120]]})
    return {
        "format": spec.FORMAT, "title": "Example sting", "description": "Two-bar major-key button: three "
        "stabs walking up to a held tonic chord.",
        "arrangement_notes": "C major. Bar 1: C - Dm - G7 stabs; bar 2: held C with crash. Stabs carry the motif.",
        "tempo": 120, "time_signature": "4/4", "key": "C major", "bars": 2, "target_seconds": 4,
        "sections": [{"name": "sting", "bar": 1, "bars": 2}],
        "mix": {"reverb": "room"},
        "patterns": {"beat": {"bars": 1, "notes": [
            [1, 1, "kick", 0.5, 110], [1, 2, "snare", 0.5, 100], [1, 2.5, "kick", 0.5, 80],
            [1, 3, "kick", 0.5, 105], [1, 4, "snare", 0.5, 105], [1, 4.5, "snare", 0.25, 70]]}}
        if kit else {},
        "tracks": tracks,
    }


def build_request(brief: str, target_seconds: float | None = None, cfg: Config | None = None,
                  soundfonts: list[str] | None = None, families: list[str] | None = None,
                  notes: str = "", cue: str | None = None, styles: list[str] | None = None,
                  context: str = "") -> dict:
    """{"system", "prompt", "schema"} for one composition.

    cue and styles name cards (see cards()); context is free text from the production.
    soundfonts/families narrow the catalogue."""
    cue_card = find_card(CUES, cue) if cue else None
    style_cards = [find_card(STYLES, s) for s in styles or []]
    tempos = style_tempos(style_cards)
    cfg = cfg or load()
    cat = catalog.load(cfg)
    if not cat.get("soundfonts"):
        raise RuntimeError("no SoundFonts are installed: upload some on the AI TV SoundFonts page "
                           "(or copy .sf2 files into " + ", ".join(str(d) for d in cfg.soundfont_dirs) + ")")
    parts = ["# The brief", "", brief.strip(), ""]
    if target_seconds:
        parts += [f"**Duration: about {target_seconds:g} seconds.** Set `target_seconds` to {target_seconds:g} "
                  "and choose tempo and bars to match (the rendered reverb tail is extra and doesn't count).", "",
                  timing_table(target_seconds, tempos), ""]
    if cue_card:
        parts += [f"# What the music is for: {cue_card['name']}", "", cue_card["text"], ""]
    if style_cards:
        blend = len(style_cards) > 1
        parts += ["# Style touchstones", "",
                  ("A blend of these styles. " if blend else "") +
                  "These describe what the style usually sounds like: a starting point, not a checklist. "
                  "Make your own choices within it, and break any of these when the piece is better for it. "
                  "Pick the actual sounds from the catalogue.", ""]
        for c in style_cards:
            tempo = f" (usual tempo {c['tempo']} bpm)" if c["tempo"] else ""
            parts += [f"## {c['name']}{tempo}", "", c["text"], ""]
    if context:
        parts += ["# About the production", "", context.strip(), ""]
    if notes:
        parts += ["# Also", "", notes.strip(), ""]
    ex = example(cat)
    if ex:
        parts += ["# Example of the format (only the shape: its key, tempo, sounds and style are arbitrary, "
                  "so don't copy them)", "", "```json", json.dumps(ex, indent=1), "```", ""]
    parts += [catalog.to_markdown(cat, soundfonts, families), "",
              "Answer with the composition JSON only."]
    return {"system": system_prompt(), "prompt": "\n".join(parts), "schema": spec.SCHEMA}


def repair_prompt(request: dict, comp: dict, errors: list[str], warnings: list[str] | None = None) -> str:
    """Follow-up prompt: the original request, the answer, and what's wrong with it."""
    lines = [request["prompt"], "", "# Your previous answer", "", "```json", json.dumps(comp, indent=1), "```", "",
             "# It can't be rendered yet. Fix these problems and answer with the whole corrected JSON:", ""]
    lines += [f"- {e}" for e in errors]
    if warnings:
        lines += ["", "Also worth fixing if it's a mistake (not required):", ""] + [f"- {w}" for w in warnings]
    return "\n".join(lines)


def write_request(request: dict, folder: str | Path, stage: str = "music") -> dict[str, Path]:
    """Save like AI-TV's manual writing stages: <stage>.request.md and <stage>.schema.json."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = {"request": folder / f"{stage}.request.md", "schema": folder / f"{stage}.schema.json",
             "answer": folder / f"{stage}.json"}
    paths["request"].write_text(f"# {stage}\n\n## Instructions\n\n{request['system']}\n\n## Request\n\n"
                                f"{request['prompt']}\n")
    paths["schema"].write_text(json.dumps(request["schema"], indent=2) + "\n")
    return paths
