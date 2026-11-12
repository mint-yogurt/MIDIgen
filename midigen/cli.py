"""midigen command line. Every command can print JSON (--json) for the AI-TV worker to read.

Exit codes: 0 fine, 2 the composition is invalid (errors listed), 1 anything else.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import api, catalog, prompt, spec
from .config import load


def _print(data, as_json: bool, text: str = "") -> None:
    if as_json:
        print(json.dumps(data, indent=1))
    elif text:
        print(text)


def _preset(text: str) -> tuple[int, int]:
    bank, _, program = text.partition(":")
    return int(bank), int(program)


def cmd_catalog(a) -> int:
    cfg = load(a.config)
    cat = catalog.build(cfg, measure=not a.no_measure, force=a.refresh) if (a.refresh or not a.cached) \
        else catalog.load(cfg)
    if a.markdown:
        print(catalog.to_markdown(cat, a.soundfont or None, a.family or None))
        return 0
    summary = {"catalog": str(cfg.catalog_dir / "catalog.json"),
               "soundfont_dirs": [str(d) for d in cfg.soundfont_dirs],
               "soundfonts": [{k: f[k] for k in ("id", "file", "path", "preset_count", "banks", "sample_mb")}
                              | {"name": f["info"].get("name", ""), "measured": bool(f.get("analysis"))}
                              for f in cat["soundfonts"]],
               "errors": cat["errors"]}
    lines = [f"{f['id']:<24} {f['preset_count']:>4} presets  banks {f['banks']}  {f['file']}"
             for f in cat["soundfonts"]] or ["no SoundFonts found in " + ", ".join(summary["soundfont_dirs"])]
    lines += [f"unreadable: {e['file']}: {e['error']}" for e in cat["errors"]]
    _print(summary, a.json, "\n".join(lines))
    return 0


def cmd_prompt(a) -> int:
    cfg = load(a.config)
    brief = Path(a.brief[1:]).read_text() if a.brief.startswith("@") else a.brief
    context = Path(a.context[1:]).read_text() if a.context and a.context.startswith("@") else a.context or ""
    req = prompt.build_request(brief, a.seconds, cfg, a.soundfont or None, a.family or None, a.notes or "",
                               a.cue, a.style or None, context)
    if a.out:
        paths = prompt.write_request(req, a.out, a.stage)
        _print({k: str(v) for k, v in paths.items()}, a.json, f"wrote {paths['request']} and {paths['schema']}")
    elif a.json:
        print(json.dumps(req, indent=1))
    else:
        print(f"# Instructions\n\n{req['system']}\n\n# Request\n\n{req['prompt']}")
    return 0


def _load_comp(path: str) -> dict:
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())


def cmd_validate(a) -> int:
    report = api.validate(_load_comp(a.composition), load(a.config))
    lines = [("valid" if report["ok"] else "INVALID")]
    lines += [f"error: {e}" for e in report["errors"]] + [f"warning: {w}" for w in report["warnings"]]
    if report.get("summary"):
        s = report["summary"]
        lines.append(f"{s['title']}: {s['bars']} bars of {s['time_signature']}, {s['seconds']} s")
        lines += [f"  ch{t['channel']:>2} {t['name']:<16} {t['soundfont']} {t['bank']}:{t['program']} "
                  f"{t['preset']:<20} {t['notes']:>4} notes, from bar {t['first_note_bar']}" for t in s["tracks"]]
    _print(report, a.json, "\n".join(lines))
    return 0 if report["ok"] else 2


def cmd_render(a) -> int:
    cfg = load(a.config)
    comp = _load_comp(a.composition)
    name = a.name or (Path(a.composition).stem if a.composition != "-" else None)
    try:
        result = api.render(comp, a.out, name, cfg, keep_stems=a.stems or None)
    except api.CompositionError as e:
        _print({"ok": False, "errors": e.errors, "warnings": e.warnings}, a.json, str(e))
        return 2
    result["ok"] = True
    lines = [f"wrote {result['files']['midi']}", f"wrote {result['files']['wav']} ({result['wav_seconds']} s, "
             f"rendered in {result['render_seconds']} s)"] + [f"warning: {w}" for w in result["warnings"]]
    _print(result, a.json, "\n".join(lines))
    return 0


def cmd_audition(a) -> int:
    bank, program = _preset(a.preset)
    try:
        r = api.audition(a.soundfont, bank, program, a.phrase, a.out, load(a.config))
    except api.CompositionError as e:
        _print({"ok": False, "errors": e.errors}, a.json, str(e))
        return 2
    _print(r, a.json, r["files"]["wav"])
    return 0


def cmd_compare(a) -> int:
    rs = api.compare(a.program, a.bank, a.phrase, a.out, a.soundfont or None, load(a.config))
    _print([r["files"] for r in rs], a.json, "\n".join(r["files"]["wav"] for r in rs) or "no matching presets")
    return 0


def cmd_cards(a) -> int:
    folder = prompt.CUES if a.cmd == "cues" else prompt.STYLES
    found = list(prompt.cards(folder).values())
    lines = [f"{c['id']:<22} {c['name']}" + (f"  ({c['tempo']} bpm)" if c["tempo"] else "")
             + (f"\n{'':<22} also: {', '.join(c['aliases'])}" if c["aliases"] else "") for c in found]
    _print(found, a.json, "\n".join(lines))
    return 0


def cmd_schema(a) -> int:
    print(json.dumps(spec.SCHEMA, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="midigen", description="General MIDI composition and FluidSynth rendering.")
    p.add_argument("--config", help="config file (default config/midigen.toml or $MIDIGEN_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--json", action="store_true", help="print JSON")
        return sp

    s = common(sub.add_parser("catalog", help="scan SoundFonts and (re)build the instrument catalogue"))
    s.add_argument("--refresh", action="store_true", help="re-read and re-measure every file")
    s.add_argument("--cached", action="store_true", help="don't re-scan unless files changed")
    s.add_argument("--no-measure", action="store_true", help="skip the measuring renders")
    s.add_argument("--markdown", action="store_true", help="print the LLM-readable catalogue")
    s.add_argument("--soundfont", action="append", help="only this SoundFont id (repeatable)")
    s.add_argument("--family", action="append", help="only this GM family, e.g. 'synth pad', or 'drums'")
    s.set_defaults(fn=cmd_catalog)

    s = common(sub.add_parser("prompt", help="make the LLM request for a musical brief"))
    s.add_argument("brief", help="the brief, or @file to read it from a file")
    s.add_argument("--seconds", type=float, help="target duration")
    s.add_argument("--notes", help="extra instructions appended to the request")
    s.add_argument("--cue", help="what the music is for: theme, jingle, sting, bed, credits (see `midigen cues`)")
    s.add_argument("--style", action="append", help="a style card (see `midigen styles`); repeat to blend")
    s.add_argument("--context", help="what the production knows (format, era, scene...), or @file")
    s.add_argument("--soundfont", action="append", help="only offer this SoundFont (repeatable)")
    s.add_argument("--family", action="append", help="only offer this GM family (repeatable)")
    s.add_argument("--out", help="write <stage>.request.md and <stage>.schema.json here")
    s.add_argument("--stage", default="music", help="file name stem for --out (default music)")
    s.set_defaults(fn=cmd_prompt)

    s = common(sub.add_parser("validate", help="check a composition JSON (or - for stdin)"))
    s.add_argument("composition")
    s.set_defaults(fn=cmd_validate)

    s = common(sub.add_parser("render", help="validate, write .mid and render .wav"))
    s.add_argument("composition")
    s.add_argument("--out", required=True, help="output folder")
    s.add_argument("--name", help="file name stem (default: the composition file's)")
    s.add_argument("--stems", action="store_true", help="also keep per-track stems")
    s.set_defaults(fn=cmd_render)

    s = common(sub.add_parser("audition", help="render a test phrase on one preset"))
    s.add_argument("soundfont")
    s.add_argument("preset", help="bank:program, e.g. 0:89 or 128:0")
    s.add_argument("--phrase", choices=api.PHRASES, default="auto")
    s.add_argument("--out", help="folder (default: the audition cache under catalog/)")
    s.set_defaults(fn=cmd_audition)

    s = common(sub.add_parser("compare", help="one program across every SoundFont, for A/B listening"))
    s.add_argument("program", type=int)
    s.add_argument("--bank", type=int)
    s.add_argument("--phrase", choices=api.PHRASES, default="auto")
    s.add_argument("--soundfont", action="append")
    s.add_argument("--out", help="folder (default: the audition cache)")
    s.set_defaults(fn=cmd_compare)

    for name, text in (("cues", "list the cue cards (what music is for)"), ("styles", "list the style cards")):
        s = common(sub.add_parser(name, help=text))
        s.set_defaults(fn=cmd_cards)

    s = sub.add_parser("schema", help="print the composition JSON schema")
    s.set_defaults(fn=cmd_schema)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"midigen: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
