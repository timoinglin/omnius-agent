#!/usr/bin/env python3
"""Turn an audio file into the three things a Discord VOICE NOTE needs.

A voice note is the thing that plays inline in the chat with a waveform and a
duration, instead of a `.mp3` he has to download and open. For the owner that
is not decoration: he reads with difficulty, and audio is his accessible
version of a reply - a download is a wall in front of it.

Discord asks for three things TOGETHER (docs/resources/message, "Voice
Messages"), and any one of them missing means an ordinary attachment:

  * the audio as **Ogg/Opus**,
  * the message flag `IS_VOICE_MESSAGE` (1 << 13 = 8192),
  * the attachment carrying `duration_secs` and `waveform` - base64 of at most
    256 bytes, one byte of amplitude per point.

Plus two shape rules that bite in code, not in prose: a voice note is exactly
ONE attachment and carries NO content. Text that belongs with it is a separate
message.

This module owns only the file half - convert, measure, draw the wave.
`api.send_voice_message()` owns the POST, and `watchdog.flush_outboxes()` owns
the decision and the fallback. **Every failure here raises VoiceError**, never
a half-made file: the caller's job is to fall back to a plain attachment, and
an audio reply that arrives as a download still arrives. The watchdog is the
only always-on piece of Omnius - nothing in this path may be able to stop it.

ffmpeg does the work (it is in PATH; the whisper and tts tools rely on it too).
"""
import base64
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "state" / "voice"

# What we will offer to send as a voice note. Deliberately audio-only: a video
# has no waveform to draw and Discord refuses it.
AUDIO_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".mp4a",
              ".aac", ".flac", ".wma", ".webm"}

# Discord's own cap. More points are rejected, fewer are accepted and just look
# coarse - which is why a short clip draws fewer rather than stretching.
WAVEFORM_MAX = 256
POINTS_PER_SEC = 10          # ~one bar per 100 ms, the shape the client draws
PCM_RATE = 8000              # analysis only; the sent audio stays 48 kHz
CACHE_TTL = 24 * 3600        # converted copies are disposable, the source is archived


class VoiceError(Exception):
    """Could not make a voice note. The caller attaches the file normally."""


def is_audio(path):
    return Path(str(path)).suffix.lower() in AUDIO_EXTS


def _run(cmd, **kw):
    """Run ffmpeg. EVERY way the OS can refuse to start it becomes a VoiceError.

    `except FileNotFoundError` was too narrow and cost the owner three replies
    on 2026-09-02: Windows App Control blocked ffmpeg.exe with WinError 4551,
    which is a bare OSError. It escaped this module, escaped the caller's
    `except (VoiceError, ApiError)`, and took the whole reply down with it -
    audio AND text, renamed `.bad`, nothing delivered. FileNotFoundError is an
    OSError, so the narrow case is still covered; the point is that a policy,
    a permission or a broken exe now falls back like anything else.
    """
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120, **kw)
    except subprocess.TimeoutExpired:
        raise VoiceError("ffmpeg timed out")
    except OSError as e:
        raise VoiceError(f"cannot run ffmpeg: {e}")


def _pcm(src):
    """Mono 16-bit PCM of the whole file, for duration and waveform."""
    r = _run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(src),
              "-vn", "-f", "s16le", "-ac", "1", "-ar", str(PCM_RATE), "-"])
    if r.returncode != 0 or len(r.stdout) < 2:
        detail = (r.stderr or b"").decode(errors="replace").strip()[:200]
        raise VoiceError(f"cannot decode audio: {detail or 'no samples'}")
    return r.stdout


def waveform(pcm, duration):
    """Base64 of one amplitude byte per point, peak per bucket, normalised.

    Normalising against the file's own peak is what keeps a quietly-recorded
    voice from arriving as a flat line - Discord draws these bytes literally,
    so an un-normalised quiet clip looks like silence and reads as broken.
    """
    n = max(1, min(WAVEFORM_MAX, int(duration * POINTS_PER_SEC) or 1))
    total = len(pcm) // 2
    if total == 0:
        raise VoiceError("no samples to draw")
    peaks, step = [], total / n
    for i in range(n):
        a, b = int(i * step), max(int(i * step) + 1, int((i + 1) * step))
        chunk = pcm[a * 2:min(b, total) * 2]
        hi = 0
        for j in range(0, len(chunk) - 1, 2):
            v = int.from_bytes(chunk[j:j + 2], "little", signed=True)
            hi = max(hi, -v if v < 0 else v)
        peaks.append(hi)
    top = max(peaks) or 1
    return base64.b64encode(bytes(min(255, p * 255 // top) for p in peaks)).decode()


def _cache_dir():
    CACHE.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - CACHE_TTL
    for old in CACHE.glob("*.ogg"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return CACHE


def prepare(src):
    """-> {"path", "duration_secs", "waveform"}; raises VoiceError on anything.

    The returned path is a converted copy in state\\voice\\ - the original is
    left alone, because media\\sent\\ archives what was actually asked for.
    """
    src = Path(str(src))
    if not src.is_file():
        raise VoiceError(f"no such file: {src}")
    if not is_audio(src):
        raise VoiceError(f"not an audio file: {src.name}")
    if not shutil.which("ffmpeg"):
        raise VoiceError("ffmpeg is not on PATH")

    pcm = _pcm(src)
    duration = round(len(pcm) / 2 / PCM_RATE, 2)
    if duration <= 0:
        raise VoiceError("audio has no length")
    wave = waveform(pcm, duration)

    out = _cache_dir() / f"{src.stem}-{int(src.stat().st_mtime)}.ogg"
    if not out.exists():
        tmp = out.with_suffix(".part.ogg")
        r = _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src),
                  "-vn", "-c:a", "libopus", "-b:a", "32k",
                  "-ar", "48000", "-ac", "1", str(tmp)])
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            detail = (r.stderr or b"").decode(errors="replace").strip()[:200]
            raise VoiceError(f"opus encode failed: {detail or 'empty output'}")
        tmp.replace(out)
    return {"path": str(out), "duration_secs": duration, "waveform": wave}


if __name__ == "__main__":                       # smoke test: voice.py <file>
    import json
    import sys
    if len(sys.argv) != 2:
        print("usage: voice.py <audio file>", file=sys.stderr)
        raise SystemExit(2)
    try:
        info = prepare(sys.argv[1])
    except VoiceError as e:
        print(f"not sendable as a voice note: {e}", file=sys.stderr)
        raise SystemExit(1)
    info["waveform"] = info["waveform"][:32] + f"... ({len(info['waveform'])} chars)"
    print(json.dumps(info, indent=2))
