"""Microsoft Graph engine for tools\\email — Exchange Online without IMAP.

WHY THIS EXISTS. All of his mailboxes are Exchange Online, and Microsoft
turned OFF password ("basic") authentication for IMAP in October 2022 with no
way for any admin to restore it. Proven live on 2026-08-05: the server
advertises AUTH=PLAIN and still answers "AUTHENTICATE failed" for a correct
password. So mail on these accounts needs OAuth2, and once OAuth is required
anyway, Graph is the better door than IMAP+XOAUTH2 - server-side filtering,
a real attachments endpoint, and Microsoft's supported path.

THE FLOW IS DEVICE CODE, AND THAT IS A SECURITY CHOICE. A device-code app is
a PUBLIC client: it has no client secret at all. Nothing sensitive sits in
config, nothing sensitive is ever pasted into a chat, and the only credential
on disk is a refresh token in root .env. Compare the alternative - a client
secret that grants the same access, forever, to whoever reads the file.

WHAT IT CAN REACH. Delegated permissions only (Mail.Read, Mail.Send,
offline_access). Delegated means the app acts AS the signed-in user with that
user's own access, so it is structurally incapable of reading anyone else's
mailbox - which is why registering it was safe in a 166-user tenant.

Message ids here are Graph's own opaque ids, carried whole after the account
label: "<account>/<graph-id>". There is no UIDVALIDITY problem to solve
because Graph ids are stable identifiers rather than positions in a list.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import omnius_config as ocfg  # noqa: E402

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ("https://graph.microsoft.com/Mail.Read "
          "https://graph.microsoft.com/Mail.Send "
          "offline_access")
TIMEOUT = 30.0


class GraphError(Exception):
    """Something the caller should see. mail.py maps this to MailError."""


# --- HTTP ---------------------------------------------------------------------
#
# urllib rather than httpx: this must keep working on a fresh install with no
# pip step, exactly like the rest of tools\. The requests here are three shapes
# and none of them needs a session layer.

def _request(method, url, token=None, body=None, form=None):
    data, headers = None, {"Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error_description": raw[:400]}
        # Return the body for the token endpoint (authorization_pending is a
        # normal, expected 400 while he is still signing in); raise for Graph.
        if "/oauth2/" in url:
            return {"_http_error": e.code, **payload}
        msg = (payload.get("error", {}) or {}).get("message") or raw[:300]
        raise GraphError(f"Graph {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise GraphError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {e.reason}")


# --- auth ---------------------------------------------------------------------

def start_device_code(tenant, client_id):
    """-> the dict Microsoft returns: user_code, verification_uri, device_code."""
    out = _request("POST", f"{AUTHORITY}/{tenant}/oauth2/v2.0/devicecode",
                   form={"client_id": client_id, "scope": SCOPES})
    if "device_code" not in out:
        raise GraphError(_auth_hint(out))
    return out


def poll_device_code(tenant, client_id, device_code, interval, expires_in, on_wait=None):
    """Block until he finishes signing in. -> the token response."""
    deadline = time.time() + min(expires_in, 900)
    wait = max(interval, 3)
    while time.time() < deadline:
        time.sleep(wait)
        out = _request("POST", f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
                       form={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                             "client_id": client_id, "device_code": device_code})
        err = out.get("error")
        if not err:
            return out
        if err == "authorization_pending":
            if on_wait:
                on_wait()
            continue
        if err == "slow_down":
            wait += 5
            continue
        raise GraphError(_auth_hint(out))
    raise GraphError("timed out waiting for the sign-in to complete")


def _auth_hint(out):
    """Turn Microsoft's error codes into the actual next action."""
    err = str(out.get("error") or "")
    desc = str(out.get("error_description") or "")[:300]
    if "AADSTS7000218" in desc or err == "invalid_client":
        return ("the app is not allowed to use device code. In the portal: "
                "Autenticacion -> Configuracion avanzada -> 'Permitir flujos de "
                "cliente publico' -> Si. " + desc)
    if "AADSTS65001" in desc or err == "consent_required":
        return ("consent has not been granted for Mail.Read / Mail.Send. Add "
                "them as DELEGATED permissions, then sign in again (or ask an "
                "admin to click 'Conceder consentimiento'). " + desc)
    if "AADSTS50020" in desc or "AADSTS50034" in desc:
        return f"that account does not exist in this tenant. {desc}"
    if err == "expired_token":
        return "the sign-in window expired - run the command again"
    if err == "invalid_grant":
        return ("the stored refresh token is no longer valid (password changed, "
                "consent revoked, or it simply aged out). Re-run "
                "tools\\email\\authorize.py to get a new one. " + desc)
    return f"{err}: {desc}" if err else (desc or "unexpected response")


def access_token(body):
    """Exchange the stored refresh token for a short-lived access token.

    Called on every command. The refresh token is read from .env by the name
    config gives (token_env), never stored in config itself.
    """
    tenant = str(body.get("tenant_id") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    if not tenant or not client_id:
        raise GraphError("this account has no tenant_id / client_id")
    token_key = str(body.get("token_env") or "").strip()
    if not token_key:
        raise GraphError("this account has no token_env naming the .env key "
                         "that holds its refresh token")
    refresh = ocfg.env_value(token_key)
    if not refresh:
        raise GraphError(
            f"{token_key} is not set in .env. Run "
            f"`python tools\\email\\authorize.py` once to sign in and get it.")
    out = _request("POST", f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
                   form={"grant_type": "refresh_token", "client_id": client_id,
                         "refresh_token": refresh, "scope": SCOPES})
    if "access_token" not in out:
        raise GraphError(_auth_hint(out))
    return out["access_token"]


# --- mail ---------------------------------------------------------------------

def _addr(entry):
    e = ((entry or {}).get("emailAddress") or {})
    name, addr = e.get("name") or "", e.get("address") or ""
    return f"{name} <{addr}>".strip() if name else addr


def list_messages(body, folder, limit, unseen=False, since=None, until=None):
    token = access_token(body)
    folder = folder or "inbox"
    # A date range is an asked-for set, so raise the page size for it rather
    # than silently returning the newest 25 of "all of July".
    top = 200 if (since or until) else max(1, min(limit or 25, 100))
    q = {"$top": str(top),
         "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,"
                    "internetMessageId",
         "$orderby": "receivedDateTime desc"}
    where = []
    if unseen:
        where.append("isRead eq false")
    if since:
        where.append(f"receivedDateTime ge {since.isoformat()}T00:00:00Z")
    if until:
        # `until` already points at the day AFTER the range, so `lt` is right.
        where.append(f"receivedDateTime lt {until.isoformat()}T00:00:00Z")
    if where:
        q["$filter"] = " and ".join(where)
    url = (f"{GRAPH}/me/mailFolders/{urllib.parse.quote(folder)}/messages"
           f"?{urllib.parse.urlencode(q)}")
    out = _request("GET", url, token=token)
    return [{
        "id": m.get("id", ""),
        "from": _addr(m.get("from")),
        "subject": m.get("subject") or "",
        "date": m.get("receivedDateTime") or "",
        "seen": bool(m.get("isRead")),
        "hasAttachments": bool(m.get("hasAttachments")),
        "messageId": m.get("internetMessageId") or "",
    } for m in out.get("value", [])]


def get_message(body, graph_id):
    token = access_token(body)
    q = urllib.parse.urlencode({
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                   "body,hasAttachments,internetMessageId,conversationId"})
    m = _request("GET", f"{GRAPH}/me/messages/{urllib.parse.quote(graph_id)}?{q}",
                 token=token)
    content = ((m.get("body") or {}).get("content") or "")
    kind = ((m.get("body") or {}).get("contentType") or "").lower()
    return {
        "raw_body": content, "is_html": kind == "html",
        "from": _addr(m.get("from")),
        "to": ", ".join(_addr(x) for x in (m.get("toRecipients") or [])),
        "cc": ", ".join(_addr(x) for x in (m.get("ccRecipients") or [])),
        "subject": m.get("subject") or "",
        "date": m.get("receivedDateTime") or "",
        "messageId": m.get("internetMessageId") or "",
        "hasAttachments": bool(m.get("hasAttachments")),
    }


def list_attachments(body, graph_id):
    token = access_token(body)
    out = _request("GET", f"{GRAPH}/me/messages/{urllib.parse.quote(graph_id)}"
                          f"/attachments", token=token)
    return out.get("value", [])


def send(body, to, subject, text, reply_to_id=None):
    """Send, or reply in-thread when reply_to_id is given.

    Graph's /reply endpoint keeps the conversation intact server-side, so the
    In-Reply-To / References juggling IMAP needs does not arise here.
    """
    token = access_token(body)
    if reply_to_id:
        _request("POST", f"{GRAPH}/me/messages/{urllib.parse.quote(reply_to_id)}/reply",
                 token=token, body={"comment": text})
        return {"threaded": True}
    payload = {"message": {
        "subject": subject,
        "body": {"contentType": "Text", "content": text},
        "toRecipients": [{"emailAddress": {"address": a.strip()}}
                         for a in str(to).split(",") if a.strip()]},
        "saveToSentItems": True}
    _request("POST", f"{GRAPH}/me/sendMail", token=token, body=payload)
    return {"threaded": False}
