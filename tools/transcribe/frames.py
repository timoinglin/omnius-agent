"""Pull the frames that matter out of a long meeting recording.

A 115-minute call is mostly two faces talking, and none of that is worth a
screenshot. What IS worth keeping is the moment the SCREEN changes - a shared
window, a dashboard, a tool being demonstrated. "What did he show me for HR"
is answered by those frames and by nothing else.

So: ffmpeg scene detection rather than a frame every N seconds, then a cap, so
a two-hour call does not turn into four hundred images nobody will look at.

    python tools\\meeting\\frames.py <video> --out <dir> [--max 80] [--threshold 0.30]

Writes `<out>/frames/t<seconds>.jpg` (the timestamp IS the filename, so a
frame can always be tied back to the transcript) plus `frames.json`.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def scene_times(video, threshold, start=0.0, until=None):
    """-> [seconds] where the picture changes materially.

    One decode pass, no images written: ffmpeg prints the timestamps and we
    decide afterwards which to keep. Extracting first and culling later would
    mean writing hundreds of JPEGs to throw most away.

    `start`/`until` bound the scan. That matters more than it looks: a screen
    share nested inside a meeting window occupies a fraction of the frame, so
    page-to-page navigation barely moves the whole-frame score. The answer is a
    LOWER threshold over the minutes that actually matter, not over two hours -
    scanning the lot that sensitively returns hundreds of talking-head frames.
    """
    seek = ["-ss", f"{start:.2f}"] if start else []
    if until is not None:
        seek += ["-t", f"{max(0.0, until - start):.2f}"]
    cmd = ["ffmpeg", "-hide_banner", *seek, "-i", str(video),
           "-filter:v", f"select='gt(scene,{threshold})',showinfo",
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    times = []
    for m in re.finditer(r"pts_time:([0-9.]+)", p.stderr or ""):
        try:
            # -ss before -i restarts the clock, so put it back on the meeting's
            # timeline - the filename IS the timestamp and has to stay true.
            times.append(float(m.group(1)) + start)
        except ValueError:
            continue
    return sorted(set(times))


def thin(times, max_frames, min_gap):
    """Keep scene changes that are far enough apart, then cap.

    Two rules, both about not wasting his attention: a burst of changes inside
    a few seconds is one event (a window opening, an animation), and a hard cap
    keeps the set reviewable. When there are more than the cap, keep them EVENLY
    spread rather than the first N - otherwise a busy first ten minutes eats the
    whole budget and the last hour is invisible.
    """
    spaced, last = [], -1e9
    for t in times:
        if t - last >= min_gap:
            spaced.append(t)
            last = t
    if len(spaced) <= max_frames:
        return spaced
    step = len(spaced) / max_frames
    return [spaced[int(i * step)] for i in range(max_frames)]


def grab(video, seconds, dest, width):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{seconds:.2f}",
                    "-i", str(video), "-frames:v", "1",
                    "-vf", f"scale={width}:-2", str(dest)],
                   check=True, capture_output=True)


def stamp(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main(argv=None):
    p = argparse.ArgumentParser(prog="frames.py", description=__doc__.split("\n")[0])
    p.add_argument("video")
    p.add_argument("--out", required=True)
    p.add_argument("--max", type=int, default=80, help="hard cap on frames kept")
    p.add_argument("--threshold", type=float, default=0.30,
                   help="scene-change sensitivity, 0-1 (lower = more frames)")
    p.add_argument("--min-gap", type=float, default=20.0,
                   help="seconds; collapses a burst of changes into one event")
    p.add_argument("--width", type=int, default=1024,
                   help="frames must stay readable - UI text is the point")
    p.add_argument("--from", dest="start", type=float, default=0.0,
                   help="seconds; scan only from here (a demo, not the chat)")
    p.add_argument("--to", dest="until", type=float, default=None,
                   help="seconds; scan only up to here")
    args = p.parse_args(argv)

    out = Path(args.out)
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    span = "" if not (args.start or args.until) else \
        f" over {stamp(args.start)}–{stamp(args.until) if args.until else 'end'}"
    print(f"[1/3] scanning for scene changes (one decode pass){span} …", flush=True)
    times = scene_times(args.video, args.threshold, args.start, args.until)
    print(f"      {len(times)} raw scene change(s)", flush=True)

    keep = thin(times, args.max, args.min_gap)
    print(f"[2/3] keeping {len(keep)} after spacing ≥{args.min_gap:.0f}s "
          f"and capping at {args.max}", flush=True)

    print("[3/3] extracting …", flush=True)
    made = []
    for t in keep:
        name = f"t{int(t):05d}.jpg"
        try:
            grab(args.video, t, frames_dir / name, args.width)
        except subprocess.CalledProcessError:
            continue
        made.append({"seconds": round(t, 2), "time": stamp(t),
                     "file": str((frames_dir / name).resolve())})

    # A denser second pass over one demo must ADD to the index, not replace it -
    # the wide pass and the close pass are both true, at different scales.
    index = out / "frames.json"
    kept = {}
    if index.is_file():
        try:
            for f in json.loads(index.read_text(encoding="utf-8"))["frames"]:
                kept[f["file"]] = f
        except (ValueError, KeyError, OSError):
            pass                      # an unreadable index is rebuilt, not fatal
    kept.update({f["file"]: f for f in made})
    frames = sorted(kept.values(), key=lambda f: f["seconds"])
    index.write_text(
        json.dumps({"video": str(args.video), "threshold": args.threshold,
                    "rawSceneChanges": len(times), "kept": len(frames),
                    "frames": frames}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"      {len(made)} frame(s) -> {frames_dir} "
          f"({len(frames)} in the index)", flush=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
