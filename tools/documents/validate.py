"""Check extracted invoice fields against things that can actually be checked.

WHY THIS EXISTS. Structured extraction returns the right SHAPE, never
guaranteed truth: the OCR beneath it can lose a character (measured
2026-08-05 - a header reading `NIF` came back as `NF`), and the model on top
can infer a plausible value that was never on the page. Both failures look
exactly like success in a spreadsheet.

So every field with a checkable shape gets checked, and the rest are reported
as unverifiable rather than quietly trusted:

  * a Spanish NIF/NIE ends in a letter derived from its digits
  * a CIF ends in a control character derived from its digits
  * an IBAN is mod-97 == 1 over its rearranged digits
  * subtotal + tax == total, which catches a single wrong digit in any of them
  * a date must parse, and an invoice dated in the future is suspect

Nothing here rejects a document. It RETURNS WARNINGS, because the human
deciding whether to pay something is better placed than a checksum - the point
is that he is told, not that the tool decides.
"""
import re
from datetime import date, datetime

EU_VAT_PREFIX = ("AT|BE|BG|HR|CY|CZ|DK|EE|EL|ES|FI|FR|DE|GB|GR|HU|IE|IT|LV|"
                 "LT|LU|MT|NL|PL|PT|RO|SE|SI|SK|XI|CH|NO")
NIF_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
CIF_CONTROL = "JABCDEFGHI"
NIE_PREFIX = {"X": "0", "Y": "1", "Z": "2"}


def clean(value):
    return re.sub(r"[\s.\-/]", "", str(value or "")).upper()


def check_nif(value):
    """-> (ok, note) for a Spanish NIF, NIE or CIF. ok=None when not recognised."""
    v = clean(value)
    if not v:
        return None, "no tax id found"
    m = re.fullmatch(r"(\d{8})([A-Z])", v)
    if m:                                             # NIF
        want = NIF_LETTERS[int(m[1]) % 23]
        return (m[2] == want,
                "checksum ok" if m[2] == want else f"letter should be {want}")
    m = re.fullmatch(r"([XYZ])(\d{7})([A-Z])", v)
    if m:                                             # NIE
        want = NIF_LETTERS[int(NIE_PREFIX[m[1]] + m[2]) % 23]
        return (m[3] == want,
                "checksum ok" if m[3] == want else f"letter should be {want}")
    m = re.fullmatch(r"([ABCDEFGHJKLMNPQRSUVW])(\d{7})([0-9A-J])", v)
    if m:                                             # CIF
        digits = m[2]
        even = sum(int(d) for d in digits[1::2])
        odd = sum(sum(divmod(int(d) * 2, 10)) for d in digits[0::2])
        ctrl = (10 - (even + odd) % 10) % 10
        if m[3] == str(ctrl) or m[3] == CIF_CONTROL[ctrl]:
            return True, "checksum ok"
        return False, f"control should be {ctrl} or {CIF_CONTROL[ctrl]}"
    # An EU VAT number is a real country code followed by something with
    # digits in it. Without both tests "NOTATAXID" reads as a foreign VAT
    # number and gets reported as UNCHECKED - which is worse than failing,
    # because unchecked sounds deliberate.
    m = re.fullmatch(rf"({EU_VAT_PREFIX})([0-9A-Z]{{2,13}})", v)
    if m and sum(c.isdigit() for c in m[2]) >= 2:
        if m[1] == "ES":                      # ES + a Spanish id: check it
            return check_nif(m[2])
        return None, f"{m[1]} VAT number - format not verifiable here"
    return False, "not a recognisable NIF, NIE, CIF or EU VAT number"


def check_iban(value):
    """-> (ok, note). mod-97 == 1, the standard IBAN check."""
    v = clean(value)
    if not v:
        return None, "no IBAN found"
    if not re.fullmatch(r"[A-Z]{2}\d{2}[0-9A-Z]{10,30}", v):
        return False, "not IBAN-shaped"
    moved = v[4:] + v[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    return (int(digits) % 97 == 1,
            "checksum ok" if int(digits) % 97 == 1 else "checksum FAILS")


def check_totals(subtotal, tax, total, tolerance=0.02):
    """-> (ok, note). Arithmetic catches one wrong digit anywhere in the three."""
    have = [x for x in (subtotal, tax, total) if isinstance(x, (int, float))]
    if len(have) < 3:
        return None, "not all of subtotal/tax/total were found"
    diff = abs((subtotal + tax) - total)
    if diff <= tolerance:
        return True, "subtotal + tax = total"
    return False, (f"subtotal {subtotal} + tax {tax} = {subtotal + tax}, "
                   f"but total says {total} (off by {diff:.2f})")


def check_date(value, field="date"):
    v = str(value or "").strip()
    if not v:
        return None, f"no {field} found"
    try:
        d = datetime.strptime(v[:10], "%Y-%m-%d").date()
    except ValueError:
        return False, f"{field} {v!r} is not YYYY-MM-DD"
    if d > date.today():
        return False, f"{field} {d} is in the future"
    if d.year < 2000:
        return False, f"{field} {d} looks wrong"
    return True, "plausible"


def validate_invoice(data, today=None):
    """-> {ok, checks:[{field, result, note}], warnings:[str]}.

    `ok` means nothing FAILED - not that everything was verified. Fields that
    could not be checked are reported as such, so "ok" never quietly means
    "absent".
    """
    checks, warnings = [], []

    def record(field, result, note):
        checks.append({"field": field, "result":
                       "ok" if result is True else
                       "FAIL" if result is False else "unchecked", "note": note})
        if result is False:
            warnings.append(f"{field}: {note}")

    if not data.get("is_invoice", True):
        warnings.append("the model says this is not an invoice "
                        f"({data.get('document_kind') or 'unknown kind'})")
    record("supplier_tax_id", *check_nif(data.get("supplier_tax_id")))
    if data.get("customer_tax_id"):
        record("customer_tax_id", *check_nif(data.get("customer_tax_id")))
    if data.get("iban"):
        record("iban", *check_iban(data.get("iban")))
    record("totals", *check_totals(data.get("subtotal"), data.get("tax_amount"),
                                   data.get("total")))
    record("issue_date", *check_date(data.get("issue_date"), "issue_date"))
    for field in ("invoice_number", "supplier_name", "total"):
        if data.get(field) in (None, "", []):
            warnings.append(f"{field}: missing — a ledger row needs it")
    return {"ok": not warnings, "checks": checks, "warnings": warnings}
