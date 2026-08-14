"""tools\\documents — get the text out of a PDF (or an image).

CONTRACT (stable regardless of engine — see README.md):

    python tools\\documents\\extract.py <file> [--pages 1-5] [--json] [--provider local|mistral]

Prints the document's text on stdout; `--json` prints {pages:[{page,text}], …}.
Exit 0 = ok, 1 = extraction failed, 2 = usage / not configured.

ENGINE ORDER, and the reason for it:

1. LOCAL (PyMuPDF). A digital PDF - one produced by software, which is most
   invoices - already contains real text. Pulling it out is instant, free,
   private, and nothing leaves the machine. Always tried first.
2. AN API (Mistral OCR, LlamaParse) only when local finds no text, which means
   the page is a scan or a photograph. Configured in config\\ai.ini
   [documents]; absent = local only.

WHAT LEAVES THE MACHINE. Local: nothing. API: the whole document. An invoice
carries a supplier, an amount and often a bank account, so the API is opt-in
per install and reported by !config rather than silently on.

There is a THIRD path this tool deliberately does not implement: rendering
pages to PNG so the agent reads them with its own vision. That needs no key at
all and handles scans, but it is a choice the CALLER makes (it costs context,
and it only makes sense for a handful of pages), so it lives in `render`
below rather than being smuggled into `extract`.
"""
import argparse
import base64
import json
import mimetypes
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import omnius_config as ocfg  # noqa: E402

TIMEOUT = 120.0          # OCR of a long scan is genuinely slow
MISTRAL = "https://api.mistral.ai/v1"


class ExtractError(Exception):
    """Failed for a reason worth showing. -> exit 1."""


class UsageError(Exception):
    """Bad arguments or nothing configured. -> exit 2."""


def parse_pages(spec):
    """'3' or '2-5' -> a 0-based set, or None for everything."""
    if not spec:
        return None
    spec = str(spec).strip()
    try:
        if "-" in spec:
            a, b = spec.split("-", 1)
            return set(range(int(a) - 1, int(b)))
        return {int(spec) - 1}
    except ValueError:
        raise UsageError(f"--pages {spec!r} should look like '3' or '2-5'")


# --- local --------------------------------------------------------------------

def have_pymupdf():
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


def local_extract(path, pages=None):
    """-> [{page, text}] using PyMuPDF, or None when it is not installed.

    Returns pages even when they are empty: an empty page is the SIGNAL that
    this is a scan and needs OCR, so it must not be silently dropped.
    """
    if not have_pymupdf():
        return None
    import fitz
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        raise ExtractError(f"cannot open {Path(path).name}: {e}")
    out = []
    try:
        for i, page in enumerate(doc):
            if pages is not None and i not in pages:
                continue
            out.append({"page": i + 1, "text": (page.get_text() or "").strip()})
    finally:
        doc.close()
    return out


def render(path, out_dir, pages=None, dpi=150):
    """PDF pages -> PNG files, so an agent can read a scan with its own eyes.

    The no-key, no-upload answer to a scanned document. Costs context rather
    than money, so it is for a handful of pages, not a batch of fifty.
    """
    if not have_pymupdf():
        raise UsageError("rendering needs PyMuPDF: pip install pymupdf")
    import fitz
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(path))
    made = []
    try:
        for i, page in enumerate(doc):
            if pages is not None and i not in pages:
                continue
            target = out_dir / f"{Path(path).stem}-p{i + 1}.png"
            page.get_pixmap(dpi=dpi).save(str(target))
            made.append(str(target))
    finally:
        doc.close()
    return made


# --- API ----------------------------------------------------------------------

def _post(url, key, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise ExtractError(f"{urllib.parse.urlsplit(url).netloc} said {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise ExtractError(f"cannot reach {url}: {e.reason}")


def _mistral_document(path):
    raw = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "application/pdf"
    kind = "image_url" if mime.startswith("image/") else "document_url"
    b64 = base64.b64encode(raw).decode("ascii")
    return {"type": kind, kind: f"data:{mime};base64,{b64}"}


def mistral_annotate(path, key, schema, pages=None):
    """Structured extraction: a JSON schema in, filled fields out.

    The SAME endpoint as OCR with one extra parameter. This is the mode for
    invoices - describing what an invoice is beats writing a parser for every
    supplier's layout. Plain OCR stays the default because it is cheaper and
    most requests are just "what does this say".

    The shape is guaranteed; the CONTENTS are not. A model asked for a total
    will produce one whether or not the page had it, so anything from here
    goes through validate.py before it reaches a spreadsheet.
    """
    body = {"model": "mistral-ocr-latest", "document": _mistral_document(path),
            "document_annotation_format": schema}
    if pages is not None:
        body["pages"] = sorted(pages)
    out = _post(f"{MISTRAL}/ocr", key, body)
    ann = out.get("document_annotation")
    if ann is None:
        raise ExtractError("the model returned no structured data — check the schema")
    if isinstance(ann, str):
        try:
            ann = json.loads(ann)
        except ValueError:
            raise ExtractError(f"structured data was not JSON: {ann[:200]}")
    pages_out = [{"page": p.get("index", i) + 1,
                  "text": (p.get("markdown") or p.get("text") or "").strip()}
                 for i, p in enumerate(out.get("pages") or [])]
    return ann, pages_out


def mistral_extract(path, key, pages=None):
    """Mistral OCR. Sends the document as a base64 data URL - one request, no
    upload step, nothing left behind in their file storage."""
    body = {"model": "mistral-ocr-latest", "document": _mistral_document(path)}
    if pages is not None:
        body["pages"] = sorted(pages)
    out = _post(f"{MISTRAL}/ocr", key, body)
    got = out.get("pages")
    if not isinstance(got, list):
        raise ExtractError(f"unexpected OCR response: {json.dumps(out)[:300]}")
    return [{"page": p.get("index", i) + 1,
             "text": (p.get("markdown") or p.get("text") or "").strip()}
            for i, p in enumerate(got)]


PROVIDERS = {"mistral": mistral_extract}


def configured():
    """-> (provider, key) from config\\ai.ini [documents], or (None, None)."""
    body = (ocfg.load("ai").get("documents") or {})
    provider = str(body.get("provider") or "").strip().lower()
    if not provider:
        return None, None
    if provider not in PROVIDERS:
        raise UsageError(f"unknown documents provider {provider!r} — "
                         f"use one of: {', '.join(sorted(PROVIDERS))}")
    env_key = str(body.get("api_key_env") or "").strip()
    if not env_key:
        raise UsageError(f"[documents] selects {provider!r} but names no "
                         f"api_key_env holding its key")
    key = ocfg.env_value(env_key)
    if not key:
        raise UsageError(f"[documents] names {env_key}, which is not set in .env")
    return provider, key


SCHEMAS = Path(__file__).resolve().parent / "schemas"


def load_schema(name):
    """-> a JSON schema. `name` is a shipped one (`invoice`) or a path."""
    shipped = SCHEMAS / f"{name}.json"
    p = shipped if shipped.is_file() else Path(name)
    if not p.is_file():
        have = sorted(x.stem for x in SCHEMAS.glob("*.json"))
        raise UsageError(f"no schema {name!r}. Shipped: {', '.join(have) or 'none'}"
                         f" — or give a path to a JSON schema file.")
    try:
        schema = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read schema {p}: {e}")
    schema.pop("_comment", None)
    return schema


def extract_structured(path, schema_name, pages=None):
    """-> {file, engine, schema, data, validation, pages}.

    Always uses the API: structuring needs a model, so a digital PDF is sent
    too. That is a real privacy cost and the reason this is a separate verb
    rather than the default - plain `extract` on a digital invoice still
    sends nothing anywhere.
    """
    path = Path(path)
    if not path.is_file():
        raise UsageError(f"no such file: {path}")
    provider, key = configured()
    if provider != "mistral":
        raise UsageError(
            "structured extraction needs an OCR provider that supports it. "
            "Set [documents] provider = mistral in config\\ai.ini.")
    schema = load_schema(schema_name)
    data, pages_out = mistral_annotate(path, key, schema, pages)
    out = {"file": str(path), "engine": f"{provider}+schema",
           "schema": schema_name, "data": data,
           "pages": pages_out, "chars": sum(len(p["text"]) for p in pages_out)}
    if schema_name == "invoice":
        import validate
        out["validation"] = validate.validate_invoice(data)
    return out


def extract(path, pages=None, force=None):
    """-> {file, engine, pages:[{page,text}], chars}. Local first, API if empty."""
    path = Path(path)
    if not path.is_file():
        raise UsageError(f"no such file: {path}")
    if force in (None, "local"):
        got = local_extract(path, pages)
        if got and any(p["text"] for p in got):
            return {"file": str(path), "engine": "local",
                    "pages": got, "chars": sum(len(p["text"]) for p in got)}
        if force == "local":
            if got is None:
                raise UsageError("PyMuPDF is not installed: pip install pymupdf")
            raise ExtractError("no text found — this looks like a scan; "
                               "configure an OCR provider or render it to images")
    provider, key = (force, None) if force in PROVIDERS else configured()
    if force in PROVIDERS:
        _p, key = configured()
        if _p != force:
            raise UsageError(f"{force!r} is not the configured provider")
    if not provider:
        hint = ("no text found and no OCR provider configured. Either "
                "`pip install pymupdf` (free, local, private) for digital PDFs, "
                "or set [documents] provider in config\\ai.ini for scans.")
        raise UsageError(hint)
    got = PROVIDERS[provider](path, key, pages)
    return {"file": str(path), "engine": provider, "pages": got,
            "chars": sum(len(p["text"]) for p in got)}


def main(argv=None):
    p = argparse.ArgumentParser(prog="extract.py",
                                description=__doc__.split("\n")[0])
    p.add_argument("file")
    p.add_argument("--pages", help="'3' or '2-5' (1-based)")
    p.add_argument("--provider", choices=["local"] + sorted(PROVIDERS))
    p.add_argument("--json", action="store_true")
    p.add_argument("--render-to", help="write page PNGs here instead of extracting")
    p.add_argument("--schema", metavar="NAME|FILE",
                   help="structured extraction: a shipped schema (invoice) or "
                        "a path to one. Always uses the API.")
    args = p.parse_args(argv)
    try:
        pages = parse_pages(args.pages)
        if args.schema:
            got = extract_structured(args.file, args.schema, pages)
            print(json.dumps(got, ensure_ascii=False, indent=2))
            v = got.get("validation")
            if v and v["warnings"]:
                for w in v["warnings"]:
                    print(f"warning: {w}", file=sys.stderr)
            return 0
        if args.render_to:
            made = render(args.file, args.render_to, pages)
            print(json.dumps({"rendered": made}, indent=2) if args.json
                  else "\n".join(made))
            return 0
        got = extract(args.file, pages, args.provider)
        if args.json:
            print(json.dumps(got, ensure_ascii=False, indent=2))
        else:
            for page in got["pages"]:
                print(f"--- page {page['page']} ---")
                print(page["text"])
        return 0
    except UsageError as e:
        print(json.dumps({"error": str(e), "kind": "usage"}), file=sys.stderr)
        return 2
    except ExtractError as e:
        print(json.dumps({"error": str(e), "kind": "extract"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    import urllib.parse  # noqa: E402  (used in error paths)
    sys.exit(main())
