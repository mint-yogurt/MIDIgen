# MIDIgen

> **⚠️ Beta (v0.1).** This is an early, working version and it **will be revised**. The composition
> format, the prompt wording, the cue and style cards, the catalogue fields, the command-line options
> and the validator's rules may all change between versions, sometimes without backwards compatibility.
> Don't build on it as a stable API yet. Expect the results to be uneven: the music is only as good as
> the model writing it and the prompt guiding it, and both are still being tuned.

General MIDI composition for an LLM, rendered with FluidSynth. A standalone tool that the AI TV
pipeline (`~/ai_tv`) calls: it does not call any LLM itself.

```
brief ──► midigen prompt ──► (LLM writes a midigen/1 JSON) ──► midigen render ──► .mid + .wav
              ▲                                                    │
   SoundFont catalogue (presets, mapped keys, measured sound)      └─► FluidSynth, one stem per SoundFont
```

- **SoundFonts** are yours: upload `.sf2` files on the AI TV **SoundFonts** page (or copy them
  into `~/ai_tv/library/soundfonts/`). Nothing else is catalogued; no system or downloaded fonts.
- **Catalogue**: every preset's bank, program and name, the keys that actually have samples, the
  GM slot it sits in, and measurements from rendering one held note (attack, sustain, release,
  brightness, level). Your listening notes (`config/notes.toml`) are added as `heard`.
- **Composition format** `midigen/1`: JSON in bars and beats, with reusable patterns. It's
  validated against the catalogue before anything is rendered.
- **Rendering**: a standard type-1 MIDI file, plus a WAV rendered by FluidSynth with the exact
  SoundFont each track asked for. Headless, faster than real time (~50× on this machine).

## Commands

```bash
cd ~/MIDIgen && source .venv/bin/activate      # or call ~/MIDIgen/.venv/bin/midigen directly

midigen catalog                    # scan + measure new/changed SoundFonts; --refresh redoes everything
midigen catalog --markdown         # the catalogue as the LLM sees it (--soundfont ID, --family "synth pad")
midigen prompt "brief" --seconds 15 --out DIR    # DIR/music.request.md + music.schema.json
midigen prompt "brief" --cue bed --style mallsoft --style "80s smooth jazz" --context @scene.txt
midigen cues                       # the cue cards: what music is for (theme, jingle, sting, bed, credits)
midigen styles                     # the style cards (genres)
midigen schema                     # the composition JSON schema
midigen validate song.json         # errors (exit 2) and warnings, plus a per-track summary
midigen render song.json --out DIR # DIR/song.mid, .wav, .json (the composition), .render.json
midigen audition sc55 0:89         # test phrase on one preset (cached in catalog/auditions/)
midigen compare 89 --out DIR       # program 89 on every SoundFont, for A/B listening
python -m pytest                   # 25 tests, ~2 s
```

Every command takes `--json` for machine-readable output. Exit codes: 0 fine, 2 invalid
composition, 1 anything else.

## How AI TV calls it

`[tools] midigen` in `~/ai_tv/config/aitv.toml` points at `~/MIDIgen/.venv/bin/midigen`, the same
pattern as Pocket TTS. A future music stage would:

1. `midigen prompt @brief.txt --seconds 30 --out productions/<id>/writing --stage music --json` writes
   `music.request.md` and `music.schema.json` in exactly the shape AI TV's manual writing stages use.
   With an API backend, call `llm.complete_json(system, prompt, schema)` with the pieces from
   `midigen prompt --json` instead.
2. Validate the answer with `midigen validate music.json --json`. If it isn't `ok`, send the errors
   back: `midigen.prompt.repair_prompt(request, comp, errors)` builds that follow-up message.
3. `midigen render music.json --out productions/<id>/music --json`. The JSON result lists the files,
   duration, levels and per-track timings.

Already wired into the web UI:
- **SoundFonts** page: upload, list, delete and re-scan fonts. Each font has its own page with
  every preset, its measurements and a play button that renders an audition on demand.
- **Tests** page: a `.mid` gets a piano roll that plays (and seeks) the `.wav` rendered next to it.
  Anything written to `~/ai_tv/test_outputs/<area>/<folder>/` shows up there.

From Python in the same venv: `midigen.api.render(comp, out_dir)`, `api.validate(comp)`,
`api.audition(...)`, `midigen.prompt.build_request(brief, target_seconds)`.

## Steering: cues, styles and context

The brief is free text and can say anything. Three optional layers add steering on top of it without
dictating the music:

- **Cue (`--cue`)**: what the music is *for*. Each cue has functional rules:
  - **theme**: hook in the first bars, ends on a button
  - **jingle**: singable hook, room for a voiceover in the middle, final hit
  - **sting**: one gesture, 2-6 s
  - **bed**: stays out of the voice's register, steady, cuttable at any bar line, low volumes
  - **credits**: reprise and resolve

  These are constraints of the job, not of style.
- **Styles (`--style`, repeatable)**: genre touchstones. Each card covers the usual tempo range, feel,
  harmony, typical instrument families, what makes the style recognisable, and what to avoid. They're
  framed to the LLM as "a starting point, not a checklist; break any of these when the piece is better
  for it". Two or more styles are presented as a blend. The tempo table in the request follows the
  styles' ranges, so it doesn't nudge every piece toward the same tempo.
- **Context (`--context`)**: whatever the production knows (the format, era, product, scene), passed through
  as written.

Leaving all three out gives the bare brief, as before. The cards are plain Markdown in `midigen/cues/` and
`midigen/styles/`, with `name`, `aliases` and `tempo` at the top. Edit them, or add a file to add a style.
Names are matched loosely, so "Ethno-ambient/tribal ambient", "tribal ambient" and "ethno_ambient" are
all the same card.

Styles so far: utopian scholastic, atmospheric drum and bass, 90s new age, mallsoft, corporate new age,
muzak, ethno-ambient / tribal ambient, Fourth World, exotica, corporate smooth rock, ambient,
80s smooth jazz.

Planned for AI TV (not built yet): a `music` stage that picks the cue from where the music goes, passes
the production's rolls and scene as context, and picks the style with a weighted `music_style` roll per
format (pinnable like any other roll). That keeps the variety coming from the dice rather than from the
prompt.

## The composition format (midigen/1)

The full explanation the LLM gets is `midigen/prompts/system.md`. The core rules:

- A beat is a quarter note; `tempo` is quarter notes per minute. Bars and beats count from 1.
- A note is `[bar, beat, pitch, length_in_beats, velocity]`. Pitch is a number, `"C4"` (= 60), a
  drum name on channel 10 (`"kick"`), or a list for a chord.
- `patterns` are reusable clips; a track's `play` places them (`bar`, `repeat`, `transpose`, `velocity`).
- Tracks name `soundfont` (catalogue id), `bank`, `program`, `channel` (1-16; 10 = drums only),
  `volume`, `pan`, `reverb`, `chorus`, plus `controls` (with ramps), `bends`, `program_changes`.
- `bars` is the length; notes must end by then. The reverb tail is rendered automatically.
- `mix.reverb` picks a room (dry, room, plate, hall, cathedral) from `config/midigen.toml`.

**Errors** stop the render: unknown SoundFont or preset, bad channel, drum kit off channel 10 or a
melodic sound on it, two sounds on one channel, notes past the end, bad bars, beats, pitches,
lengths or velocities, notes the preset has no samples for (they would be silent), unknown
patterns.

**Warnings** don't stop it: notes outside an instrument family's usual range, overlapping notes on
the same key (shortened automatically), duplicate notes (merged), very high polyphony, unused
patterns, and a duration more than 20% (or 1 s) off `target_seconds`. Notes in a pattern may ring past
the pattern's end; only the end of the piece is a hard limit.

## Rendering

One MIDI stem per SoundFont and channel, each rendered by its own `fluidsynth -F` run with only
that font loaded, then summed. The reverb and chorus are linear, so the sum sounds like one synth.

- The finished mix is trimmed once it's silent, faded over the last 30 ms and peak-normalised to
  `peak_dbfs` (-1 dBFS).
- Settings live in `config/midigen.toml`: sample rate, bit depth, tail, gain, polyphony, reverb
  rooms, and per-SoundFont FluidSynth overrides (`[soundfonts.<id>].fluidsynth`). A hot or very
  wet font can be tamed there without affecting the others.
- `--stems` keeps each stem as its own WAV, for ducking under dialogue later.

A `.mid` can't name SoundFonts. On its own it plays back with General MIDI programs and banks on any
synth. The `.json` next to it is the source of truth for which font each track used.

## Measured descriptors

`midigen catalog` renders each melodic preset once: one held note near C4, velocity 100, dry, 2 s
on and 2.5 s release. From that it records:

| field | meaning | words used |
| --- | --- | --- |
| `attack_ms` | time to 90% of peak loudness | instant <20, fast <90, soft <350, else slow swell |
| `sustain_db` | loudness just before release, relative to peak | holds > -6, fades while held > -20, decays > -40, else one-shot |
| `release_ms` | time after release to fall 40 dB | ≥1000 adds "long release" |
| `brightness` | spectral centroid ÷ the note's fundamental | dark <2.5, mellow <5, bright <10, else very bright |
| `level_db` | peak at full gain, dBFS | for balancing volumes |

These are rough, and one note can't capture everything (filter sweeps, LFOs, key-dependent samples).
The best descriptions are listening notes: add them to `config/notes.toml` as
`"bank:program" = "..."` under `[<soundfont id>]`. They appear as `heard` in the catalogue and on
the preset pages.

## Layout

```
midigen/        sf2.py (RIFF reader), catalog.py, gm.py, spec.py (format + validator), midi.py,
                render.py (FluidSynth + mixing), api.py, prompt.py, prompts/system.md, cues/*.md, styles/*.md, cli.py
config/         midigen.toml, notes.toml
catalog/        generated: catalog.json, catalog.md, auditions/   (not in git)
tests/          pytest; renders with /usr/share/sounds/sf2/TimGM6mb.sf2 as a fixture only
```

## Extending

- **New SoundFonts**: upload, done. To describe or tame one, add `[soundfonts.<id>]` to the config.
- **New controllers or drum names**: `spec.CONTROLLERS`, `gm.DRUM_ALIASES`.
- **Prompt craft**: edit `midigen/prompts/system.md`. The catalogue and example are filled in by `prompt.py`.
- **SF3 (compressed) fonts** aren't supported by the parser yet.

## What the catalogue knows about a font's layout

- **General MIDI layout** (bank 0 has 120+ presets): every preset gets its GM slot and family. Family
  ranges give soft register warnings.
- **Custom layout** (like the JV-1080 sets: patches in alphabetical order): no GM slot is claimed.
  The LLM chooses by name, measurements and listening notes.
- **One-shot presets**: the samples have no loop, so a note is silent once its recording ends. The
  catalogue shows `one-shot N s`, and the validator warns when notes are written longer than that.
- **Single-sample presets**: `root` is the recording pitch. Far from it the sound is pitch-shifted
  (chipmunk above, sludge below).
- Drum kits are recognised at bank 128 (GS/GM). XG-style kits at bank 127 or GM2 kits at 120 would
  need adding.

## The first two pieces

`~/ai_tv/test_outputs/music/2026-11-12_jingle/` and `2026-11-12_utopian_scholastic/` hold the request
the prompt generator made, the answer (`music.json`), the `.mid`, `.wav`, render report and `notes.md`.
The WAVs are also Library assets 20 and 21.
