"""FluidSynth offline rendering: MIDI in, float audio out, then mixing and WAV writing.

A standard MIDI file can't say which SoundFont a track uses, so each stem (one SoundFont +
channel) is rendered by its own FluidSynth run with only that font loaded, then the stems
are summed. FluidSynth's reverb and chorus are linear, so the sum sounds like one synth, and
each font can have its own settings (config [soundfonts.<id>].fluidsynth).
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .config import Config


class RenderError(RuntimeError):
    pass


def fluidsynth_version(cfg: Config) -> str:
    try:
        out = subprocess.run([cfg.fluidsynth, "--version"], capture_output=True, text=True, timeout=10).stdout
    except FileNotFoundError:
        raise RenderError(f"FluidSynth not found ({cfg.fluidsynth}); install it with: sudo apt install fluidsynth")
    return out.splitlines()[0].strip() if out else "unknown"


def fluid_render(cfg: Config, soundfont: Path, midi: Path, settings: dict, sample_rate: int) -> np.ndarray:
    """Render one MIDI file with one SoundFont. Returns float32 frames x 2 (unclipped)."""
    with tempfile.TemporaryDirectory(prefix="midigen-") as tmp:
        raw = Path(tmp) / "out.raw"
        cmd = [cfg.fluidsynth, "-ni", "-q", "-F", str(raw), "-T", "raw", "-O", "float", "-E", "little",
               "-r", str(sample_rate)]
        for name, value in settings.items():
            cmd += ["-o", f"{name}={_setting(value)}"]
        cmd += [str(soundfont), str(midi)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise RenderError(f"FluidSynth not found ({cfg.fluidsynth}); install it with: sudo apt install fluidsynth")
        if proc.returncode != 0 or not raw.exists():
            raise RenderError(f"fluidsynth failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
        return np.fromfile(raw, dtype="<f4").reshape(-1, 2)


def _setting(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def render_many(cfg: Config, jobs: list[tuple[Path, Path, dict]], sample_rate: int) -> list[np.ndarray]:
    """[(soundfont, midi, settings), ...] -> audio per job, in order."""
    workers = max(1, int(cfg.render.get("jobs", 2)))
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda j: fluid_render(cfg, j[0], j[1], j[2], sample_rate), jobs))


def mix(stems: list[np.ndarray]) -> np.ndarray:
    length = max((len(s) for s in stems), default=0)
    out = np.zeros((length, 2), dtype=np.float64)
    for s in stems:
        out[: len(s)] += s
    return out


def finish(audio: np.ndarray, sample_rate: int, end_seconds: float, peak_dbfs: float,
           silence_db: float = -70.0, fade_ms: float = 30.0) -> tuple[np.ndarray, float]:
    """Cut to the last sound before end_seconds, fade the very end, normalise the peak.

    Returns (audio, gain_db applied)."""
    audio = audio[: int(end_seconds * sample_rate)]
    level = np.abs(audio).max(axis=1) if len(audio) else np.zeros(0)
    peak = float(level.max()) if len(level) else 0.0
    if peak <= 0:
        return audio, 0.0
    loud = np.nonzero(level > peak * 10 ** (silence_db / 20))[0]
    audio = audio[: int(loud[-1]) + 1 + int(0.05 * sample_rate)].copy()
    fade = min(len(audio), int(fade_ms / 1000 * sample_rate))
    if fade:
        audio[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
    gain = 10 ** (peak_dbfs / 20) / peak
    return audio * gain, 20 * np.log10(gain)


def write_wav(path: Path, audio: np.ndarray, sample_rate: int, bits: int = 16) -> None:
    audio = np.clip(audio, -1.0, 1.0)
    if bits == 24:
        ints = np.round(audio * 8388607).astype("<i4").reshape(-1)
        data = np.stack([ints & 0xFF, (ints >> 8) & 0xFF, (ints >> 16) & 0xFF], axis=1).astype(np.uint8).tobytes()
        width = 3
    elif bits == 16:
        data = np.round(audio * 32767).astype("<i2").tobytes()
        width = 2
    else:
        raise RenderError(f"unsupported bit depth {bits} (use 16 or 24)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(width)
        w.setframerate(sample_rate)
        w.writeframes(data)


def levels(audio: np.ndarray, sample_rate: int) -> dict:
    """Peak and RMS in dBFS, plus when sound first and last rises above -50 dB of its own peak."""
    if not len(audio):
        return {"peak_dbfs": None, "rms_dbfs": None, "first_sound_s": None, "last_sound_s": None}
    mono = np.abs(audio).max(axis=1)
    peak = float(mono.max())
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    db = lambda x: round(float(20 * np.log10(x)), 1) if x > 0 else None
    loud = np.nonzero(mono > peak * 10 ** (-50 / 20))[0] if peak > 0 else []
    return {
        "peak_dbfs": db(peak), "rms_dbfs": db(rms),
        "first_sound_s": round(float(loud[0] / sample_rate), 3) if len(loud) else None,
        "last_sound_s": round(float(loud[-1] / sample_rate), 3) if len(loud) else None,
    }
