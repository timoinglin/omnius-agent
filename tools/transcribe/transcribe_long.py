"""Transcribe a LONG recording by splitting it across CPU workers.

WHY THIS EXISTS. A 115-minute meeting on this laptop measured 1.4x realtime
with faster-whisper `small` - 82 minutes single-threaded - and 8.8x with
`base`, which mangles exactly what matters here: product and tool names. He
will ask "what tools did he show me for HR", so a fast wrong answer is worse
than a slow right one.

So: keep `small`, and buy the time back with cores. Chunks are transcribed in
parallel by separate processes, then merged back onto one timeline.

Two details that make the merge honest:
  * chunks OVERLAP, because a hard cut lands mid-word and both sides lose it
  * on merge, segments starting inside the overlap of the PREVIOUS chunk are
    dropped, so the seam does not stutter or duplicate a sentence

    python tools\\meeting\\transcribe_long.py <video-or-audio> --out <dir>
                                              [--model small] [--workers 3]
                                              [--language es]
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

CHUNK_SECONDS = 600          # 10 minutes: long enough to keep context, short
OVERLAP_SECONDS = 6          # enough that a stalled worker is not the whole job

# Seeding the decoder with domain words measurably improves proper nouns -
# which is the whole reason for choosing the slower model.
#
# The shipped default names no company, product or person: this file travels in
# the release, and a stranger's transcripts should not be primed with someone
# else's employer. YOUR OWN vocabulary - company, colleagues, product names,
# whatever recurs in your recordings - belongs in `config\transcribe.ini`, which
# is gitignored and never ships. See `config\transcribe.example.ini`.
BASE_PROMPT = (
    "Reunión de trabajo sobre tecnología, software, IA, CRM y recursos humanos. "
    "Se mencionan herramientas, integraciones, automatizaciones y dashboards."
)


def default_prompt():
    """BASE_PROMPT plus the operator's own vocabulary, when configured."""
    try:
        import omnius_config as ocfg
        extra = str((ocfg.load("transcribe").get("whisper") or {}).get("vocabulary") or "").strip()
    except Exception:                                            # noqa: BLE001
        extra = ""
    return f"{BASE_PROMPT} {extra}".strip() if extra else BASE_PROMPT


DEFAULT_PROMPT = default_prompt()


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def extract_audio(src, dest):
    """-> mono 16 kHz wav, which is what whisper wants anyway."""
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-vn", str(dest)])
    return dest


def duration_of(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout
    return float(out.decode().strip())


def cut(src, start, length, dest):
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length),
         "-i", str(src), "-ac", "1", "-ar", "16000", str(dest)])
    return dest


def transcribe_chunk(job):
    """Runs in a worker process: one chunk -> segments on the GLOBAL timeline."""
    path, offset, model_size, language, prompt, threads = job
    # Shared loader: it routes around a PyAV that an OS policy refuses to load
    # (Windows Smart App Control, 2026-08-31) by decoding with ffmpeg instead -
    # which this file already depends on for `cut`, so nothing new is required.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "whisper"))
    from transcribe import load_whisper_model, decode_with_ffmpeg
    WhisperModel, decode_here = load_whisper_model()
    model = WhisperModel(model_size, device="cpu", compute_type="int8",
                         cpu_threads=threads)
    segments, info = model.transcribe(
        decode_with_ffmpeg(path) if decode_here else str(path),
        language=language, vad_filter=True,
        initial_prompt=prompt, condition_on_previous_text=False)
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if text:
            out.append({"start": round(s.start + offset, 2),
                        "end": round(s.end + offset, 2), "text": text})
    return {"offset": offset, "segments": out,
            "language": getattr(info, "language", language)}


def merge(results, overlap):
    """Flatten chunk results, dropping the duplicated tail of each seam."""
    ordered = sorted(results, key=lambda r: r["offset"])
    merged, boundary = [], -1.0
    for i, res in enumerate(ordered):
        for seg in res["segments"]:
            # Anything starting before the previous chunk really ended is the
            # overlap speaking twice.
            if i and seg["start"] < boundary - overlap / 2:
                continue
            if merged and seg["start"] < merged[-1]["end"] - 0.5 \
                    and seg["text"] == merged[-1]["text"]:
                continue
            merged.append(seg)
        if res["segments"]:
            boundary = res["segments"][-1]["end"]
    return merged


def as_text(segments):
    lines = []
    for s in segments:
        m, sec = divmod(int(s["start"]), 60)
        h, m = divmod(m, 60)
        stamp = f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"
        lines.append(f"[{stamp}] {s['text']}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="transcribe_long.py",
                                description=__doc__.split("\n")[0])
    p.add_argument("source")
    p.add_argument("--out", required=True, help="folder for the results")
    p.add_argument("--model", default="small")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--language", default="es")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--chunk", type=int, default=CHUNK_SECONDS)
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_chunks"
    work.mkdir(exist_ok=True)
    started = time.time()

    print(f"[1/4] extracting audio …", flush=True)
    audio = extract_audio(args.source, work / "full.wav")
    total = duration_of(audio)
    print(f"      {total/60:.1f} min of audio", flush=True)

    n = max(1, math.ceil(total / args.chunk))
    threads = max(1, (os.cpu_count() or 4) // max(1, args.workers))
    print(f"[2/4] cutting into {n} chunk(s), {args.workers} worker(s) "
          f"x {threads} thread(s) …", flush=True)
    jobs = []
    for i in range(n):
        start = max(0.0, i * args.chunk - (OVERLAP_SECONDS if i else 0))
        length = min(args.chunk + OVERLAP_SECONDS, total - start)
        piece = cut(audio, start, length, work / f"chunk{i:03d}.wav")
        jobs.append((piece, start, args.model, args.language, args.prompt, threads))

    print(f"[3/4] transcribing with `{args.model}` …", flush=True)
    results, done = [], 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(transcribe_chunk, jobs):
            results.append(res)
            done += 1
            elapsed = time.time() - started
            rate = (done * args.chunk) / max(elapsed, 1)
            left = max(0.0, (total - done * args.chunk)) / max(rate, 0.01)
            print(f"      chunk {done}/{n} done — {elapsed/60:.1f} min elapsed, "
                  f"~{left/60:.0f} min left", flush=True)

    segments = merge(results, OVERLAP_SECONDS)
    words = sum(len(s["text"].split()) for s in segments)
    payload = {"source": str(args.source), "durationSeconds": round(total, 1),
               "model": args.model, "language": results[0]["language"] if results else args.language,
               "segments": segments, "segmentCount": len(segments),
               "wordCount": words,
               "transcribedInSeconds": round(time.time() - started, 1)}
    (out_dir / "transcript.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "transcript.txt").write_text(as_text(segments), encoding="utf-8")

    for f in work.glob("*.wav"):          # the audio is large; the text is not
        f.unlink(missing_ok=True)
    work.rmdir()
    print(f"[4/4] done in {(time.time()-started)/60:.1f} min — "
          f"{len(segments)} segments, {words} words", flush=True)
    print(f"      {out_dir / 'transcript.txt'}", flush=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
