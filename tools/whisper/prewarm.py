#!/usr/bin/env python3
"""Pre-download the whisper model so the first voice note isn't slow.

The model cache (~/.cache/huggingface) is machine-local and does NOT travel in
the zip, so each new machine downloads once. install.bat runs this best-effort
after installing faster-whisper. Idempotent: instant when already cached.
Honors WHISPER_MODEL (default "base").
"""
import os
import sys


def main():
    size = os.environ.get("WHISPER_MODEL", "base")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed", file=sys.stderr)
        return 3
    print(f"pre-warming whisper model '{size}' (first time downloads ~140MB, then cached)...")
    WhisperModel(size, device="cpu", compute_type="int8")
    print("whisper model ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
