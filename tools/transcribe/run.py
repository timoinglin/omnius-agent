"""One recording -> a finished archive, with NO Claude attached to the wait.

WHY THIS EXISTS. Transcribing a two-hour call takes ~25 minutes. If a desk sits
in a turn for those 25 minutes, that desk answers nothing - which is exactly the
complaint that produced this design ("i cannot ask or use omnius anymore until
that job is finished"). But the grinding part is ffmpeg and whisper: it needs no
model, no context, no tokens.

So the desk starts THIS, detached, and returns in seconds. Python grinds alone.
When it finishes it drops an envelope into `state\\inbox\\tool.transcribe\\`,
the watchdog's normal scan picks it up, and a FRESH run does the part that
actually needs a brain: reading the transcript, aiming the dense frames, writing
notes.md. No run is ever long, and nothing new had to be invented - the bus
already carried scheduled jobs this exact way (watchdog.py, `fire` loop).

    python tools\\transcribe\\run.py <video-or-audio> [--slug my-name]
                                     [--title "..."] [--model small]
                                     [--no-frames]     # transcript only
                                     [--detach]        # start and return

`--detach` is what a desk uses. Without it this runs in the foreground, which is
what you want when testing or when you are sitting at the keyboard anyway.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ARCHIVE = ROOT / "media" / "recordings"
INBOX = ROOT / "state" / "inbox" / "tool.transcribe"
SESSION = "tool.transcribe"

# Audio-only inputs skip the frame pass entirely - there is nothing to see, and
# ffmpeg would otherwise spend a full decode pass proving it.
AUDIO_ONLY = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".wma"}


def slugify(text):
    """A folder name that survives Windows, git and a phone screen."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return re.sub(r"-{2,}", "-", text)[:60] or "recording"


def recorded_at(src):
    """When the recording HAPPENED, not when we got round to processing it.

    Falls back to mtime. Both are guesses, but a file named
    `2026-08-06 09-07-13_Meet_con_Nacho.mp4` is telling us plainly, and getting
    the year folder wrong makes an archive hard to find later.
    """
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", src.name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(src.stat().st_mtime)
    except OSError:
        return datetime.now()


def has_video(src):
    if src.suffix.lower() in AUDIO_ONLY:
        return False
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=120).stdout
        return "video" in (out or "")
    except (OSError, subprocess.SubprocessError):
        return True          # unsure -> try; a pointless pass beats a missing one


def announce(payload, channel_id=None):
    """Drop an envelope so the watchdog wakes a run that can think.

    Deliberately the same shape the scheduler writes (watchdog.py). Writing the
    file IS the handoff - `ensure_runners()` scans the inbox, so no process here
    needs to reach into the watchdog.

    CARRY THE CHANNEL. The first smoke test (2026-08-06) finished, woke the desk,
    and the desk said nothing - reasoning, in its own words, "the envelope
    carried no channelId, nothing on Discord asked". That is the right instinct
    for a heartbeat and exactly wrong here: he sends a recording and walks away,
    so the completion IS the message. Stamping the channel he asked from removes
    the inference; `--channel` is passed by the desk, and `brief()` tells the run
    to post regardless.
    """
    try:
        INBOX.mkdir(parents=True, exist_ok=True)
        mid = f"transcribe-{int(time.time())}"
        tmp = INBOX / f".{mid}.part"
        tmp.write_text(json.dumps({
            "id": mid, "from": "transcribe-job", "channel": None,
            "channelId": channel_id, "category": None,
            "ts": datetime.now().astimezone().isoformat(),
            "text": payload, "files": []}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        # Rename last: a half-written envelope must never be read as a whole one.
        tmp.replace(INBOX / f"{mid}.json")
        return True
    except OSError as e:
        print(f"[announce] FAILED to write envelope: {e}", file=sys.stderr)
        return False


def empty_transcript(out_dir):
    """True when the transcriber wrote a transcript with nothing in it."""
    try:
        data = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False          # unreadable is a different problem; don't mask it
    return not data.get("segments")


def brief(out_dir, src, title, video, seconds, failed=None, test=False):
    """The text the follow-up run reads. It is an instruction, not a report -
    that run starts cold and this is all it gets."""
    if failed:
        return (f"TRANSCRIBE JOB FAILED for `{src}`.\n"
                f"Stage: {failed}\nArchive: {out_dir}\n"
                f"Read `job.log` in that folder, tell him plainly what broke and "
                f"what would fix it. Do NOT write notes.md for a failed job.")
    if test:
        # A smoke test must not leave marks on his real records. The index and
        # the daybook are for recordings he actually cares about.
        return (f"TRANSCRIBE SMOKE TEST DONE — archive: {out_dir}\n"
                f"Took {seconds/60:.1f} min. Confirm `transcript.txt` has real "
                f"words in it. Do NOT write notes.md, do NOT touch INDEX.md, do "
                f"NOT touch the daybook.\n"
                f"POST the one-line result to Discord anyway — a silent "
                f"completion is the exact failure this test exists to catch.")
    return (
        f"TRANSCRIBE JOB DONE — “{title}”\n"
        f"Source: {src}\n"
        f"Archive: {out_dir}\n"
        f"Took: {seconds/60:.1f} min. Transcript"
        f"{' + wide frame pass are' if video else ' is'} written.\n\n"
        f"Now do the part that needs reading:\n"
        f"1. Read `transcript.txt` in full.\n"
        f"2. Aim dense frame passes at the sections that matter "
        f"(`frames.py --from/--to --threshold 0.06 --min-gap 8 --width 1600`)"
        f"{' — SKIP, no frames were extracted for this job.' if not video else '.'}\n"
        f"3. Write `notes.md`: summary, chronological map with timestamps, "
        f"tools/entities shown, decisions, actions, people, and an ASR "
        f"name-correction table.\n"
        f"4. Add one line to `media\\recordings\\INDEX.md`.\n"
        f"5. Add a POINTER line to the daybook — never the summary "
        f"(`daybook\\notes\\` is pushed to GitHub).\n"
        f"6. POST him a short summary: what it was, how long, and 3-5 things "
        f"worth knowing.\n\n"
        f"On posting — this is NOT the narration the quiet rule forbids. He "
        f"sent this and walked away; the job finished minutes or hours later, "
        f"so the completion is genuinely new information he cannot see. "
        f"ALWAYS post. If this envelope carries a `channelId`, answer there — "
        f"that is where he asked. Otherwise post to your own channel.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="run.py", description=__doc__.split("\n")[0])
    p.add_argument("source")
    p.add_argument("--slug", help="folder name; defaults to the file name")
    p.add_argument("--title", help="human title; defaults to the file name")
    p.add_argument("--model", default="small")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--language", default="es")
    p.add_argument("--max-frames", type=int, default=40)
    p.add_argument("--no-frames", action="store_true",
                   help="transcript only; skip the frame pass even on a video")
    p.add_argument("--detach", action="store_true",
                   help="re-launch self in the background and return at once")
    p.add_argument("--test", action="store_true",
                   help="smoke test: skip the index and the daybook pointer")
    p.add_argument("--channel", default=None,
                   help="Discord channel id he asked from; the reply goes back there")
    args = p.parse_args(argv)

    src = Path(args.source).expanduser()
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return 2

    when = recorded_at(src)
    title = args.title or src.stem
    slug = args.slug or slugify(f"{when:%Y-%m-%d}-{src.stem}")
    out_dir = ARCHIVE / f"{when:%Y}" / slug

    if args.detach:
        # The desk's exit must not take the job with it, so: no inherited
        # handles, own process group, output to the archive's own log.
        out_dir.mkdir(parents=True, exist_ok=True)
        log = open(out_dir / "job.log", "ab", buffering=0)
        cmd = [sys.executable, str(Path(__file__).resolve()), str(src),
               "--slug", slug, "--title", title, "--model", args.model,
               "--workers", str(args.workers), "--language", args.language,
               "--max-frames", str(args.max_frames)]             + (["--no-frames"] if args.no_frames else [])             + (["--test"] if args.test else [])             + (["--channel", args.channel] if args.channel else [])
        flags = 0
        if os.name == "nt":
            flags = (subprocess.CREATE_NO_WINDOW
                     | subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                creationflags=flags,
                                start_new_session=(os.name != "nt"),
                                cwd=str(ROOT))
        print(json.dumps({"started": True, "pid": proc.pid, "archive": str(out_dir),
                          "title": title, "log": str(out_dir / "job.log")}, indent=1))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    video = has_video(src)
    # `video` stays truthful in job.json - a future session may want a frame at a
    # timestamp nobody asked for yet. Only the frame PASS is skipped.
    frames_wanted = video and not args.no_frames
    print(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  {title}", flush=True)
    print(f"    source: {src}\n    archive: {out_dir}\n"
          f"    video: {video}  frames: {frames_wanted}", flush=True)

    stage = "transcribe"
    try:
        subprocess.run([sys.executable, str(HERE / "transcribe_long.py"), str(src),
                        "--out", str(out_dir), "--model", args.model,
                        "--workers", str(args.workers), "--language", args.language],
                       check=True, cwd=str(ROOT))
        if empty_transcript(out_dir):
            # A recording whose audio never arrived transcribes "successfully"
            # into nothing: whisper's VAD hears silence and returns no segments,
            # in seconds. Reporting DONE for that sends the next run to read an
            # empty file and guess. 2026-08-14: an OBS capture with no audio
            # input routed produced a 2 kb/s track at a flat -91 dB.
            print("!!! transcript is empty - the source has no speech", flush=True)
            announce(brief(out_dir, src, title, False, time.time() - started,
                           failed="transcribe (0 segments - the audio track is "
                                  "silent or absent; verify with `ffmpeg -i <src> "
                                  "-vn -af volumedetect -f null NUL`)"),
                     args.channel)
            return 1
        if frames_wanted:
            stage = "frames"
            subprocess.run([sys.executable, str(HERE / "frames.py"), str(src),
                            "--out", str(out_dir), "--max", str(args.max_frames),
                            "--threshold", "0.30"], check=True, cwd=str(ROOT))
    except subprocess.CalledProcessError as e:
        print(f"!!! {stage} failed, exit {e.returncode}", flush=True)
        announce(brief(out_dir, src, title, frames_wanted, 0, failed=stage, test=args.test), args.channel)
        return 1

    took = time.time() - started
    # The source path is the one thing the archive cannot regenerate: without it
    # no future session can pull a NEW frame at a timestamp nobody asked for yet.
    (out_dir / "job.json").write_text(json.dumps(
        {"title": title, "source": str(src), "recordedAt": when.isoformat(),
         "hasVideo": video, "model": args.model,
         "processedAt": datetime.now().astimezone().isoformat(),
         "tookSeconds": round(took, 1)}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"=== done in {took/60:.1f} min", flush=True)
    announce(brief(out_dir, src, title, frames_wanted, took, test=args.test), args.channel)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
