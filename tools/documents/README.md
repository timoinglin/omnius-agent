# tools\documents — get the text out of a PDF (built 2026-08-05)

Contract (stable regardless of engine):

| Command | Does |
|---|---|
| `python tools\documents\extract.py <file>` | the document's text on stdout |
| `… --json` | `{file, engine, pages:[{page,text}], chars}` |
| `… --pages 2-5` | only those pages (1-based) |
| `… --provider local\|mistral` | force one engine instead of choosing |
| `… --render-to <dir>` | write page PNGs instead of extracting |
| `… --schema invoice` | **structured extraction** — a JSON schema in, filled fields out, validated |

Exit **0** ok · **1** extraction failed · **2** usage / not configured.

## Two modes

**Plain text** (the default) — "what does this say". Local first, OCR only if the page is a scan.

**Structured** (`--schema`) — "give me these fields". Mistral's document annotation takes a JSON schema and returns it filled. This is the mode for invoices: describing what an invoice *is* beats writing a parser for every supplier's layout. Shipped schemas live in `schemas\`; `--schema <path>` takes your own.

It **always uses the API**, even for a digital PDF, because structuring needs a model. That is a real privacy cost and the reason it is a separate flag rather than the default.

**The shape is guaranteed. The contents are not.** A model asked for a total will produce one whether or not the page had it, and the OCR beneath it can lose a character. So `--schema invoice` runs everything through `validate.py`:

| Check | Catches |
|---|---|
| NIF / NIE / CIF checksum | the `NIF`→`NF` class of OCR error |
| IBAN mod-97 | a transposed digit in a bank account |
| `subtotal + tax = total` | one wrong digit anywhere in the three |
| date parses, not in the future | a misread year |
| required-for-a-ledger fields present | a confident but empty extraction |

Nothing is rejected — **warnings are returned**, because the person deciding whether to pay something is better placed than a checksum. Fields that cannot be checked are reported as `unchecked`, never as `ok`: absent must never read as verified.

Proven on a document that is **not** an invoice (a tax form): it returned `is_invoice: false`, invented no number and no total, and validation flagged all three gaps.

## Three engines, in this order, and the order is the point

**1 · Local (PyMuPDF).** A digital PDF — one produced by software, which is most invoices — already contains real text. Pulling it out is instant, free, private, and burns no context. **Always tried first.**

**2 · An API (Mistral OCR).** Only when local finds *no* text, which is the signal that the page is a scan or a photograph. Empty pages are returned rather than dropped precisely so that signal survives. Configured in `config\ai.ini [documents]`; absent means local-only.

**3 · Render to images** (`--render-to`). Pages become PNGs so an agent reads them with its own vision — no key, no upload, handles scans. Deliberately a **separate verb**: it costs context rather than money, so it is the caller's choice for a handful of pages, never automatic for fifty.

## What leaves the machine

Local: **nothing**. API: **the whole document**. An invoice carries a supplier, an amount and often a bank account, so the API is opt-in per install and `!config` reports whether it is on. That is why local comes first — not performance, privacy.

## Configuration

```ini
# config\ai.ini
[documents]
provider = mistral
api_key_env = MISTRAL_API_KEY   # the NAME of the .env key, never the key
```

The credential lives in root `.env` under that name (`config\README.md`, rule 2). With no provider set, the tool still works on digital PDFs and says plainly what a scan would need.

## Measured, not assumed (2026-08-05)

The same page was tested twice: once as a digital PDF with a real text layer, once rasterised to pixels with that layer destroyed, so OCR output could be scored against what the words actually were.

| | |
|---|---|
| ground truth | 838 chars (text layer) |
| OCR output | 853 chars (pixels only) |
| **similarity** | **99.2%** |
| missed | 1 of 66 words ≥5 chars — an artefact of a letter-spaced heading, not a miss |
| accents | `qué`, `aplicación`, `móvil`, `deberían`, `diseñado` — intact |

Mistral also un-spaced a tracked-out heading that the local engine returns literally as `Q U É  E S  E S T O`, which is a small real advantage on design-heavy documents.

### A real scanned form (2026-08-05)

Better evidence than the synthetic test above: a **filled and signed Spanish Modelo 145**, scanned, 1.3 MB for a single page. Local extraction found **zero** characters — correctly, it is an image — and the fallback did its job.

| | |
|---|---|
| characters recovered | 7,958 |
| time | 7.3 s |
| form markers recognised | 11 of 12 |
| **markdown tables preserved** | **yes** — the reason this is usable for invoices at all |
| headings preserved | yes |

One visible error: a table header reading `NIF` came back as `NF`. A three-letter field lost a character, which for an invoice would be the class of mistake that corrupts a VAT number or an account reference. **So field extraction must validate formats rather than trust the string** — a NIF, an IBAN and a total all have checkable shapes.

Still not tested: a phone photo of paper (angle, shadow, creases), which is harder than a flatbed scan.

## Install

`pip install pymupdf` for the local engine (also installed by root `install.bat`). The API engine needs no install at all — only a key — which makes it the quicker route on a machine where nothing can be installed.
