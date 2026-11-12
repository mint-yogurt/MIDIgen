"""General MIDI reference tables: program names, families, the percussion key map, note names."""

from __future__ import annotations

import re

PROGRAMS = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano",
    "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ", "Accordion", "Harmonica",
    "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)",
    "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass", "Slap Bass 1",
    "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs",
    "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)", "Lead 5 (charang)",
    "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)", "Pad 6 (metallic)",
    "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)", "FX 5 (brightness)",
    "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet", "Telephone Ring", "Helicopter", "Applause",
    "Gunshot",
]

FAMILIES = [
    "piano", "chromatic percussion", "organ", "guitar", "bass", "strings", "ensemble", "brass",
    "reed", "pipe", "synth lead", "synth pad", "synth effects", "ethnic", "percussive", "sound effects",
]

# Typical playable range per family (MIDI notes), used only for soft warnings.
FAMILY_RANGE = {
    "bass": (24, 67), "piano": (21, 108), "synth pad": (24, 96), "brass": (34, 84), "reed": (36, 92),
    "pipe": (55, 100), "strings": (28, 103), "guitar": (40, 88),
}

# GM percussion key map (channel 10). 27-34 are the common GS additions.
DRUMS = {
    27: "High Q", 28: "Slap", 29: "Scratch Push", 30: "Scratch Pull", 31: "Sticks", 32: "Square Click",
    33: "Metronome Click", 34: "Metronome Bell",
    35: "Acoustic Bass Drum", 36: "Bass Drum 1", 37: "Side Stick", 38: "Acoustic Snare", 39: "Hand Clap",
    40: "Electric Snare", 41: "Low Floor Tom", 42: "Closed Hi-Hat", 43: "High Floor Tom", 44: "Pedal Hi-Hat",
    45: "Low Tom", 46: "Open Hi-Hat", 47: "Low-Mid Tom", 48: "Hi-Mid Tom", 49: "Crash Cymbal 1", 50: "High Tom",
    51: "Ride Cymbal 1", 52: "Chinese Cymbal", 53: "Ride Bell", 54: "Tambourine", 55: "Splash Cymbal",
    56: "Cowbell", 57: "Crash Cymbal 2", 58: "Vibraslap", 59: "Ride Cymbal 2", 60: "Hi Bongo", 61: "Low Bongo",
    62: "Mute Hi Conga", 63: "Open Hi Conga", 64: "Low Conga", 65: "High Timbale", 66: "Low Timbale",
    67: "High Agogo", 68: "Low Agogo", 69: "Cabasa", 70: "Maracas", 71: "Short Whistle", 72: "Long Whistle",
    73: "Short Guiro", 74: "Long Guiro", 75: "Claves", 76: "Hi Wood Block", 77: "Low Wood Block",
    78: "Mute Cuica", 79: "Open Cuica", 80: "Mute Triangle", 81: "Open Triangle",
}

# Short names a composer can use instead of numbers on a drum track.
DRUM_ALIASES = {
    "kick": 36, "kick2": 35, "snare": 38, "snare2": 40, "rim": 37, "sidestick": 37, "clap": 39,
    "hat": 42, "closed_hat": 42, "pedal_hat": 44, "open_hat": 46,
    "tom_floor_low": 41, "tom_floor": 43, "tom_low": 45, "tom_lowmid": 47, "tom_mid": 48, "tom_high": 50,
    "crash": 49, "crash2": 57, "ride": 51, "ride_bell": 53, "china": 52, "splash": 55,
    "tambourine": 54, "cowbell": 56, "vibraslap": 58, "bongo_high": 60, "bongo_low": 61,
    "conga_mute": 62, "conga_high": 63, "conga_low": 64, "timbale_high": 65, "timbale_low": 66,
    "agogo_high": 67, "agogo_low": 68, "cabasa": 69, "shaker": 70, "maracas": 70, "whistle": 71,
    "guiro": 73, "claves": 75, "woodblock_high": 76, "woodblock_low": 77, "triangle": 81, "triangle_mute": 80,
    "sticks": 31,
}

NOTE_RE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d)$")
SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def family(program: int) -> str:
    return FAMILIES[program // 8]


def note_number(name: str) -> int:
    """'C4' -> 60 (middle C), 'F#2' -> 42, 'Bb3' -> 58. Raises ValueError."""
    m = NOTE_RE.match(name.strip())
    if not m:
        raise ValueError(f"not a note name: {name!r} (use e.g. C4, F#3, Bb2; C4 = 60 = middle C)")
    letter, acc, octave = m.groups()
    n = (int(octave) + 1) * 12 + SEMITONE[letter.upper()] + {"#": 1, "b": -1, "": 0}[acc]
    if not 0 <= n <= 127:
        raise ValueError(f"{name} is outside MIDI notes 0-127")
    return n


def note_name(n: int) -> str:
    return f"{NAMES[n % 12]}{n // 12 - 1}"
