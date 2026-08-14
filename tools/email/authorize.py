"""Sign in once, get a refresh token. Run at the desk, not from Discord.

    python tools\\email\\authorize.py [--account <label>]

Prints a short code and a URL. Open the URL, type the code, sign in with the
mailbox's own account, approve. This ends by printing ONE line to paste into
root .env.

IT DOES NOT WRITE .env ITSELF, on purpose. A credential is his to place: the
one-line-to-paste shape means the token never passes through an agent's hands,
never lands in a transcript, and never gets written to the wrong file by a
tool that guessed. Same reason config only ever NAMES the .env key.

The token it produces is a REFRESH token: it stays valid for months of daily
use and buys short-lived access tokens as needed. If it ever stops working
(password change, consent revoked, long disuse) the fix is to run this again.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import graph  # noqa: E402
import omnius_config as ocfg  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="authorize.py", description=__doc__.split("\n")[0])
    p.add_argument("--account", help="account label from config\\email.ini")
    p.add_argument("--write", action="store_true",
                   help="save the token straight into root .env instead of "
                        "printing it (never shows the value)")
    args = p.parse_args(argv)

    cfg = ocfg.load("email")
    accounts = ocfg.group(cfg, "account")
    if not accounts:
        print("No mail accounts configured. Copy config\\email.example.ini to "
              "config\\email.ini first.", file=sys.stderr)
        return 2
    label = args.account or ocfg.get(cfg, "email", "default") or sorted(accounts)[0]
    if label not in accounts:
        print(f"Unknown account {label!r}. Configured: {', '.join(sorted(accounts))}",
              file=sys.stderr)
        return 2
    body = accounts[label]
    tenant = str(body.get("tenant_id") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    token_key = str(body.get("token_env") or "").strip()
    if not (tenant and client_id and token_key):
        print(f"[account.{label}] needs tenant_id, client_id and token_env "
              f"for the Graph provider.", file=sys.stderr)
        return 2

    try:
        start = graph.start_device_code(tenant, client_id)
    except graph.GraphError as e:
        print(f"Could not start sign-in: {e}", file=sys.stderr)
        return 1

    print()
    print("  " + "=" * 66)
    print(f"  Sign in as: {body.get('user') or label}")
    print(f"  1. Open   : {start.get('verification_uri')}")
    print(f"  2. Code   : {start.get('user_code')}")
    print("  " + "=" * 66)
    print("  Waiting for you to finish… (Ctrl+C to give up)")
    try:
        token = graph.poll_device_code(
            tenant, client_id, start["device_code"],
            int(start.get("interval", 5)), int(start.get("expires_in", 900)),
            on_wait=lambda: print("  …", end="", flush=True))
    except graph.GraphError as e:
        print(f"\n  Sign-in failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n  Cancelled - nothing was saved.", file=sys.stderr)
        return 1

    refresh = token.get("refresh_token")
    if not refresh:
        print("\n  Microsoft returned no refresh token. 'offline_access' is "
              "probably missing from the app's delegated permissions.",
              file=sys.stderr)
        return 1

    if args.write:
        try:
            wrote = write_env(token_key, refresh)
        except OSError as e:
            print(f"\n  Could not write .env ({e}). Falling back to printing it.",
                  file=sys.stderr)
        else:
            # The value is never printed in this path: it goes from Microsoft
            # into the file without appearing on screen, in a scrollback, or in
            # any transcript.
            print(f"\n\n  Signed in. {wrote} {token_key} in {ROOT / '.env'} "
                  f"({len(refresh)} chars, not shown).")
            print("  Check it with:  python tools\\email\\mail.py accounts\n")
            return 0

    print("\n\n  Signed in. Add this ONE line to the end of your root .env:\n")
    print(f"{token_key}={refresh}")
    print(f"\n  ({len(refresh)} characters — it is long, copy the whole line.)")
    print("  Then check it with:  python tools\\email\\mail.py accounts\n")
    return 0


def write_env(key, value):
    """Set key=value in root .env, replacing any existing line. -> 'wrote'|'updated'.

    Only ever called with --write. Refuses a UTF-16 .env rather than rewriting
    it as UTF-8: PowerShell 5.1's `>` produces UTF-16, api.py already decodes
    it, and silently changing a working file's encoding to append one line is
    exactly the kind of helpfulness that costs an evening.
    """
    env_path = ROOT / ".env"
    raw = env_path.read_bytes() if env_path.exists() else b""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise OSError("your .env is UTF-16; refusing to rewrite it as UTF-8")
    text = raw.decode("utf-8-sig", errors="replace")
    lines, replaced = [], False
    for line in text.splitlines():
        if line.strip().upper().startswith(f"{key.upper()}="):
            lines.append(f"{key}={value}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# Microsoft Graph refresh token, written by "
                     f"tools\\email\\authorize.py")
        lines.append(f"{key}={value}")
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(env_path)
    return "updated" if replaced else "wrote"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
