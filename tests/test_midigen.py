import json
import wave

import mido
import pytest

from midigen import api, catalog, gm, prompt, sf2, spec


def comp(**over):
    c = {"format": "midigen/1", "title": "t", "tempo": 120, "time_signature": "4/4", "bars": 2,
         "tracks": [{"name": "Piano", "soundfont": "testfont", "bank": 0, "program": 0, "channel": 1,
                     "notes": [[1, 1, "C4", 1, 90], [2, 1, ["C4", "E4", "G4"], 4, 90]]}]}
    c.update(over)
    return c


def test_sf2_reads_presets_and_keys(cfg):
    font = sf2.read(catalog.discover(cfg)[0][1])
    assert len(font.presets) > 100
    assert any(p.bank == 128 for p in font.presets)
    assert all(p.keys for p in font.presets if p.bank == 0)


def test_catalogue_has_measurements_and_markdown(cat):
    f = cat["soundfonts"][0]
    assert f["id"] == "testfont"
    pad = catalog.find_preset(cat, "testfont", 0, 89)
    assert pad["measured"]["attack_ms"] > catalog.find_preset(cat, "testfont", 0, 0)["measured"]["attack_ms"]
    md = catalog.to_markdown(cat)
    assert "| 0:89 |" in md and "Drum kits" in md


def test_note_names():
    assert gm.note_number("C4") == 60 and gm.note_number("Bb2") == 46 and gm.note_number("F#3") == 54
    with pytest.raises(ValueError):
        gm.note_number("H3")


def test_valid_composition(cat):
    r = spec.check(comp(), cat)
    assert r.ok, r.errors
    assert r.piece.seconds == 4.0
    assert len(r.piece.tracks[0].notes) == 4


@pytest.mark.parametrize("change, expect", [
    (lambda c: c["tracks"][0].update(soundfont="nope"), "isn't available"),
    (lambda c: c["tracks"][0].update(program=0, bank=5), "no preset"),
    (lambda c: c["tracks"][0].update(channel=17), "outside 1-16"),
    (lambda c: c["tracks"][0].update(channel=10), "drum channel"),
    (lambda c: c["tracks"][0]["notes"].append([2, 4, "C4", 2, 90]), "after the last bar"),
    (lambda c: c["tracks"][0]["notes"].append([2, 5, "C4", 0.5, 90]), "beat 5"),
    (lambda c: c["tracks"][0]["notes"].append([3, 1, "C4", 0.5, 90]), "bar 3"),
    (lambda c: c["tracks"][0]["notes"].append([1, 1, "C4", -1, 90]), "positive"),
    (lambda c: c["tracks"][0]["notes"].append([1, 1, "C4", 1, 200]), "1-127"),
    (lambda c: c["tracks"][0]["notes"].append([1, 1, 130, 1, 90]), "0-127"),
    (lambda c: c["tracks"][0].update(play=[{"pattern": "x", "bar": 1}]), "no pattern"),
])
def test_invalid_compositions(cat, change, expect):
    c = comp()
    change(c)
    r = spec.check(c, cat)
    assert not r.ok
    assert any(expect in e for e in r.errors), r.errors


def test_soft_rules(cat):
    r = spec.check(comp(target_seconds=20), cat)            # 4 s against a 20 s target: warn, don't block
    assert r.ok and any("target_seconds" in w for w in r.warnings)
    c = comp(bars=3, patterns={"p": {"bars": 1, "notes": [[1, 4, "C4", 2, 90]]}})
    c["tracks"][0]["play"] = [{"pattern": "p", "bar": 1}]   # rings a beat into bar 2: fine
    assert spec.check(c, cat).ok
    c["tracks"][0]["play"] = [{"pattern": "p", "bar": 3}]   # rings past the end of the piece: not fine
    assert any("after the last bar" in e for e in spec.check(c, cat).errors)


def test_channel_conflict_and_drums(cat, kit):
    c = comp()
    c["tracks"].append({**c["tracks"][0], "name": "Other", "program": 5})
    assert any("already used" in e for e in spec.check(c, cat).errors)
    c = comp()
    c["tracks"].append({"name": "Drums", "soundfont": "testfont", "bank": 128, "program": kit["program"],
                        "channel": 10, "notes": [[1, 1, "kick", 0.5, 100], [1, 1, "wobble", 0.5, 100]]})
    assert any("drum names" in e for e in spec.check(c, cat).errors)


def test_patterns_repeat_and_overlap_fix(cat):
    c = comp(bars=4, patterns={"riff": {"bars": 1, "notes": [[1, 1, "C3", 2, 90], [1, 2, "C3", 1, 80]]}})
    c["tracks"][0]["notes"] = []
    c["tracks"][0]["play"] = [{"pattern": "riff", "bar": 2, "repeat": 3, "transpose": 2}]
    r = spec.check(c, cat)
    assert r.ok, r.errors
    notes = r.piece.tracks[0].notes
    assert len(notes) == 6 and notes[0].start == 4 and notes[0].pitch == 50
    assert notes[0].length == 1          # shortened: it overlapped the next D3
    assert any("overlapped" in w for w in r.warnings)


def test_render_end_to_end(cfg, cat, kit, tmp_path):
    c = comp(bars=4, target_seconds=8, key="C major", mix={"reverb": "hall"},
             sections=[{"name": "a", "bar": 1, "bars": 4}])
    c["tracks"].append({"name": "Drums", "soundfont": "testfont", "bank": 128, "program": kit["program"],
                        "channel": 10, "notes": [[b, 1, "kick", 0.5, 100] for b in (3, 4)]})
    c["tracks"][0]["controls"] = [{"bar": 1, "beat": 1, "cc": "expression", "value": 40,
                                   "to_bar": 2, "to_beat": 1, "to_value": 127}]
    c["tracks"][0]["bends"] = [{"bar": 1, "beat": 2, "semitones": 0, "to_bar": 1, "to_beat": 3, "to_semitones": 1}]
    r = api.render(c, tmp_path, "song", cfg)
    assert r["midi"]["hanging_notes"] == 0 and r["midi"]["channels"] == [1, 10]
    assert 8 <= r["wav_seconds"] <= 8 + cfg.render["tail_seconds"] + 0.2
    assert r["levels"]["peak_dbfs"] == -1.0
    drums = next(s for s in r["stems"] if s["channel"] == 10)
    assert drums["levels_in_mix"]["first_sound_s"] >= 3.9          # kicks start at bar 3 = 4 s
    with wave.open(r["files"]["wav"]) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 44100
    mid = mido.MidiFile(r["files"]["midi"])
    assert any(m.type == "control_change" and m.control == 0 for m in mid.tracks[1])
    assert json.loads((tmp_path / "song.json").read_text()) == c


def test_invalid_render_raises(cfg, tmp_path):
    with pytest.raises(api.CompositionError):
        api.render(comp(bars=0), tmp_path, cfg=cfg)


def test_audition_phrases_are_valid(cat):
    for p in cat["soundfonts"][0]["presets"]:
        r = spec.check(api.audition_composition("testfont", p), cat)
        assert r.ok, (p["name"], r.errors)


def test_prompt_request(cfg, tmp_path):
    req = prompt.build_request("an 80s jingle", 15, cfg)
    assert "midigen/1" in req["system"] and "an 80s jingle" in req["prompt"] and req["schema"] is spec.SCHEMA
    paths = prompt.write_request(req, tmp_path)
    assert paths["request"].read_text().startswith("# music\n\n## Instructions")
    assert "Fix these problems" in prompt.repair_prompt(req, comp(), ["bad"])


def test_two_soundfonts_render_as_separate_stems(cfg, tmp_path):
    import shutil
    from midigen import config
    sfdir = tmp_path / "sf"
    sfdir.mkdir()
    src = catalog.discover(cfg)[0][1].resolve()
    shutil.copy(src, sfdir / "Font A.sf2")
    shutil.copy(src, sfdir / "font_b.sf2")               # real copies: symlinks count once
    c2 = config.load()
    c2.data = {**c2.data, "paths": {**c2.data["paths"], "soundfont_dirs": [str(sfdir)],
                                    "catalog": str(tmp_path / "cat"), "notes": str(tmp_path / "n.toml")},
               "soundfonts": {"font_b": {"fluidsynth": {"synth.gain": 0.25}}}}
    cat2 = catalog.build(c2, measure=False)
    assert [f["id"] for f in cat2["soundfonts"]] == ["font_a", "font_b"]
    c = comp()
    c["tracks"].append({**c["tracks"][0], "name": "Pad", "soundfont": "font_b", "program": 89, "channel": 2})
    c["tracks"][0]["soundfont"] = "font_a"
    r = api.render(c, tmp_path / "out", "two", c2)
    stems = {s["soundfont"]: s for s in r["stems"]}
    assert set(stems) == {"font_a", "font_b"}
    assert stems["font_b"]["settings"]["synth.gain"] == 0.25 and stems["font_a"]["settings"]["synth.gain"] == 0.5
    assert all(s["levels_in_mix"]["peak_dbfs"] is not None for s in r["stems"])


def test_broken_soundfont_is_reported_not_fatal(cfg, tmp_path):
    from midigen import config
    sfdir = tmp_path / "sf"
    sfdir.mkdir()
    (sfdir / "good.sf2").symlink_to(catalog.discover(cfg)[0][1].resolve())
    data = bytearray((sfdir / "good.sf2").read_bytes())
    (sfdir / "broken.sf2").write_bytes(bytes(data[:len(data) // 2]) + b"\0" * 64)   # truncated pdta
    (sfdir / "notsf.sf2").write_bytes(b"hello")
    c2 = config.load()
    c2.data = {**c2.data, "paths": {**c2.data["paths"], "soundfont_dirs": [str(sfdir)],
                                    "catalog": str(tmp_path / "cat"), "notes": str(tmp_path / "n.toml")}}
    cat2 = catalog.build(c2, measure=False)
    assert [f["id"] for f in cat2["soundfonts"]] == ["good"]
    assert {e["id"] for e in cat2["errors"]} == {"broken", "notsf"}


def test_cue_and_style_cards(cfg):
    styles = prompt.cards(prompt.STYLES)
    cues = prompt.cards(prompt.CUES)
    assert len(styles) == 12 and set(cues) == {"theme", "jingle", "sting", "bed", "credits"}
    assert all(c["text"] and c["tempo"] for c in styles.values())
    assert prompt.find_card(prompt.STYLES, "Ethno-ambient/tribal ambient")["id"] == "ethno_ambient"
    assert prompt.find_card(prompt.STYLES, "atmospheric DnB")["id"] == "atmospheric_dnb"
    assert prompt.find_card(prompt.CUES, "voiceover bed")["id"] == "bed"
    with pytest.raises(ValueError):
        prompt.find_card(prompt.STYLES, "polka")
    req = prompt.build_request("a theme for a fake 90s sitcom", 20, cfg, cue="theme",
                               styles=["muzak", "80s smooth jazz"], context="Format: sitcom_90s")
    p = req["prompt"]
    assert "# What the music is for: Theme" in p and "A blend of these styles" in p
    assert "## 80s smooth jazz (usual tempo 80-105 bpm)" in p and "Format: sitcom_90s" in p
    plain = prompt.build_request("anything", None, cfg)["prompt"]
    assert "Style touchstones" not in plain and "What the music is for" not in plain
