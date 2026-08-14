"""Offline tests for tools\\documents. No network, no key, no PDF needed.

The weight is on validate.py, because that is the layer standing between a
model's confident guess and a number in his ledger. Structured extraction
returns the right SHAPE and never guaranteed truth: the OCR under it can lose
a character (measured 2026-08-05, `NIF` -> `NF`) and the model over it can
invent a plausible total. Checksums and arithmetic are what catch both.

    python tools\\documents\\test_documents.py
"""
import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import extract  # noqa: E402
import validate  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = _failed = 0


def check(label, cond, hint=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {label}")
    else:
        _failed += 1
        print(f"  [FAIL] {label}  {hint}")


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# --- Spanish tax ids ----------------------------------------------------------
print("== NIF / NIE / CIF ==")
check("a valid NIF passes", validate.check_nif("12345678Z")[0] is True)
check("...and one wrong letter FAILS", validate.check_nif("12345678A")[0] is False)
check("the correction is named, not just refused",
      "should be Z" in validate.check_nif("12345678A")[1])
check("a valid NIE passes", validate.check_nif("X1234567L")[0] is True)
check("a valid CIF passes", validate.check_nif("A58818501")[0] is True)
check("formatting is ignored (dots, dashes, spaces)",
      validate.check_nif("12.345.678-Z")[0] is True)
check("a foreign EU VAT number is UNCHECKED, not failed",
      validate.check_nif("DE811569869")[0] is None,
      "claiming to have verified something we cannot is worse than saying so")
check("an absent id is unchecked, not a failure",
      validate.check_nif("")[0] is None)
check("garbage is refused", validate.check_nif("NOT-A-TAX-ID")[0] is False)
# The exact 2026-08-05 failure: OCR dropped a character from a 9-char id.
check("an id short by one character is caught",
      validate.check_nif("1234567Z")[0] is False,
      "this is the NIF->NF class of OCR error")

# --- IBAN ---------------------------------------------------------------------
print("== IBAN ==")
check("a valid IBAN passes", validate.check_iban("ES9121000418450200051332")[0] is True)
check("one transposed digit FAILS",
      validate.check_iban("ES9121000418450200051323")[0] is False)
check("spaces are ignored, as printed on invoices",
      validate.check_iban("ES91 2100 0418 4502 0005 1332")[0] is True)
check("a non-IBAN string is refused", validate.check_iban("12345")[0] is False)
check("no IBAN is unchecked, not failed", validate.check_iban(None)[0] is None)

# --- arithmetic ---------------------------------------------------------------
print("== totals ==")
check("subtotal + tax = total passes",
      validate.check_totals(100.0, 21.0, 121.0)[0] is True)
check("rounding within 2 cents is tolerated",
      validate.check_totals(100.0, 21.0, 121.01)[0] is True)
check("a wrong digit in the total is caught",
      validate.check_totals(100.0, 21.0, 112.0)[0] is False)
check("...and the arithmetic is shown, so he can see WHICH is wrong",
      "121" in validate.check_totals(100.0, 21.0, 112.0)[1])
check("a missing component is unchecked, not silently 'ok'",
      validate.check_totals(100.0, None, 121.0)[0] is None)

# --- dates --------------------------------------------------------------------
print("== dates ==")
check("a past ISO date is plausible", validate.check_date("2026-07-14")[0] is True)
check("a future invoice date is suspect",
      validate.check_date("2099-01-01")[0] is False)
check("a non-ISO date is refused", validate.check_date("14/07/2026")[0] is False)
check("no date is unchecked", validate.check_date("")[0] is None)

# --- the whole invoice --------------------------------------------------------
print("== a whole invoice ==")
GOOD = {"is_invoice": True, "document_kind": "invoice",
        "invoice_number": "F-2026-014", "issue_date": "2026-07-14",
        "supplier_name": "Proveedor SL", "supplier_tax_id": "A58818501",
        "iban": "ES9121000418450200051332",
        "subtotal": 100.0, "tax_amount": 21.0, "total": 121.0}
v = validate.validate_invoice(GOOD)
check("a clean invoice passes with no warnings", v["ok"] and not v["warnings"])
check("every check is reported, not just failures",
      {c["field"] for c in v["checks"]} >= {"supplier_tax_id", "iban", "totals",
                                            "issue_date"})

BAD = dict(GOOD, supplier_tax_id="A58818500", total=112.0,
           iban="ES9121000418450200051323")
v2 = validate.validate_invoice(BAD)
check("three corrupted fields produce three warnings", len(v2["warnings"]) >= 3)
check("...and each names its field",
      any("supplier_tax_id" in w for w in v2["warnings"])
      and any("iban" in w for w in v2["warnings"])
      and any("totals" in w for w in v2["warnings"]))

MISSING = {"is_invoice": True, "document_kind": "invoice"}
v3 = validate.validate_invoice(MISSING)
check("an empty extraction is NOT 'ok' just because nothing failed",
      not v3["ok"], "absent must never read as verified")
check("...and it says what a ledger row is missing",
      any("invoice_number" in w for w in v3["warnings"])
      and any("total" in w for w in v3["warnings"]))

NOT_INV = dict(GOOD, is_invoice=False, document_kind="delivery_note")
check("a document the model says is NOT an invoice is flagged",
      any("not an invoice" in w for w in validate.validate_invoice(NOT_INV)["warnings"]))

# --- schemas ------------------------------------------------------------------
print("== schemas ==")
inv = extract.load_schema("invoice")
check("the shipped invoice schema loads", inv["type"] == "json_schema")
check("...and the explanatory _comment is stripped before it is sent",
      "_comment" not in inv)
props = inv["json_schema"]["schema"]["properties"]
check("it asks for what a ledger row needs",
      {"invoice_number", "issue_date", "supplier_name", "supplier_tax_id",
       "total", "tax_amount", "iban"} <= set(props))
check("only is_invoice/document_kind are required",
      set(inv["json_schema"]["schema"]["required"]) == {"is_invoice", "document_kind"},
      "a model forced to fill a required field invents one")
check("an unknown schema names what IS available",
      raises(lambda: extract.load_schema("nope"), extract.UsageError))

# --- the ordering rule --------------------------------------------------------
print("== engine ordering ==")
SRC = (HERE / "extract.py").read_text(encoding="utf-8")
_tree = ast.parse(SRC)
_fn = next(n for n in ast.walk(_tree)
           if isinstance(n, ast.FunctionDef) and n.name == "extract_structured")
_body = ast.unparse(_fn)
check("structured extraction REFUSES rather than silently falling back to local",
      "raise UsageError" in _body and "mistral" in _body,
      "local cannot structure anything; pretending otherwise would return nothing")
check("plain extraction still tries local FIRST (privacy, not speed)",
      'force in (None, "local")' in SRC)
check("the privacy cost of --schema is stated where it is chosen",
      "privacy cost" in SRC or "sent" in _fn.body[0].value.value)

print(f"\n==== {_passed} passed, {_failed} failed ====")
sys.exit(1 if _failed else 0)
