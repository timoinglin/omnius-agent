#!/usr/bin/env python3
"""tools\\whisper - audio -> text.

Contract (tools\\whisper\\README.md): transcribe.py <audio-file> -> transcript on
stdout, non-zero exit + stderr on failure. Accepts anything ffmpeg can read
(.ogg Discord voice notes, .mp3, .wav, .m4a, ...). Language auto-detected.
Default engine: local faster-whisper (installed by root install.bat).
"""
import sys
from pathlib import Path


USAGE = ("usage: transcribe.py <audio-file>   -> transcript on stdout\n"
         "  env: WHISPER_MODEL=tiny|base|small|medium|large-v3  (default base)\n"
         "  accepts anything ffmpeg reads: .ogg voice notes, .mp3, .wav, .m4a")


def main():
    args = sys.argv[1:]
    # --help is the first thing anyone tries on an unfamiliar CLI. Treating it
    # as a filename answered "file not found: --help" AND exited 0 - a success
    # code for a nonsense error, which is worse than either alone.
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip() + "\n\n" + USAGE)
        return 0
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    audio = Path(args[0])
    if not audio.exists():
        print(f"file not found: {audio}", file=sys.stderr)
        return 2
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        # NAME THE INTERPRETER. The old message said "run install.bat", which on
        # 2026-08-12 was both wrong and expensive: install.bat HAD run - into
        # Python 3.11 - and miniconda later took the front of PATH, so `python`
        # became a different interpreter with none of the tool deps. A voice note
        # came back "not installed" on a machine where it demonstrably was, and
        # finding that out meant probing three interpreters by hand. A missing
        # import here almost never means "never installed"; it means THIS python
        # is not the one that was installed into.
        print(f"faster-whisper is not installed for {sys.executable}\n"
              f"  install it there:  \"{sys.executable}\" -m pip install faster-whisper\n"
              f"  (if install.bat already ran, it used a different interpreter - "
              f"whichever `python` resolved to at the time)", file=sys.stderr)
        return 3
    import os
    size = os.environ.get("WHISPER_MODEL", "base")  # tiny/base/small/medium/large-v3
    model = WhisperModel(size, device="cpu", compute_type="int8")
    # initial_prompt biases decoding toward our vocabulary. Measured 2026-07-31 on
    # real voice notes that `base` had mangled: "a sonmio" -> "Omnius" and
    # "task steering" -> "Task Scheduler", for +0.5s. The SAME notes through
    # `small` cost 5-11s each plus a 14s model load and were no better - so the
    # glossary, not a bigger model, is the win. Keep this list TIGHT: a prompt is
    # a bias, and stuffing it makes Whisper insert words that were never said.
    prompt = os.environ.get("WHISPER_PROMPT") or (
        "Omnius, Discord, watchdog, daybook, fleet-status, orchestrator, Claude, "
        "Task Scheduler, Windows, GitHub, commit, push, backup, skills, Whisper, "
        "Remotion, Pexip, Firebase, The Campus, Colegia, <your-org>, PWA, WebRTC."
    )
    segments, _info = model.transcribe(str(audio), vad_filter=True,
                                       initial_prompt=prompt or None)
    print(" ".join(s.text.strip() for s in segments).strip())
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
