"""Pure UPI URI helpers (app/services/upi_uri.py) — no database needed."""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import parse_qsl

import pytest

from app.services.upi_uri import (
    MAX_PAYLOAD_BYTES,
    UpiUriError,
    build_upi_uri,
    clean_text,
    is_credential_key,
    is_tr_ref,
    new_tr_ref,
    parse_upi_uri,
    return_hint,
    sanitize_return,
)


def code_of(payload: str) -> str:
    with pytest.raises(UpiUriError) as exc:
        parse_upi_uri(payload)
    return exc.value.code


# --- parse: happy paths -------------------------------------------------------------------


def test_parse_full_merchant_qr():
    p = parse_upi_uri(
        "upi://pay?pa=Ravi.Kirana-01@OKAXIS&pn=Ravi%20Kirana%20Store&am=250.5&cu=INR"
        "&tn=Groceries+for+Nov&mc=5411&tr=ORD12345&mode=02&purpose=00"
        "&url=https://shop.example/inv/1&mam=10&sign=abc"
    )
    assert p.pa == "ravi.kirana-01@okaxis"
    assert p.pn == "Ravi Kirana Store"
    assert p.am == Decimal("250.50")
    assert p.cu == "INR"
    assert p.tn == "Groceries for Nov"  # '+' is a space in QR payloads
    assert p.mc == "5411" and p.tr == "ORD12345"
    assert p.mode == "02" and p.purpose == "00"
    assert p.url == "https://shop.example/inv/1"
    assert p.unknown == {"mam": "10", "sign": "abc"}
    assert p.warnings == []


def test_parse_minimal_and_case_insensitive_scheme_host_and_keys():
    for payload in (
        "upi://pay?pa=amma@ybl",
        "UPI://PAY?PA=AMMA@YBL",
        "Upi://Pay/?pa=amma@ybl",
        "  upi://pay?pa=amma@ybl&  ",
    ):
        p = parse_upi_uri(payload)
        assert p.pa == "amma@ybl", payload
        assert p.am is None and p.pn is None and p.tn is None and p.mc is None


def test_parse_percent_decoding_and_unicode():
    p = parse_upi_uri(
        "upi://pay?pa=a.b@okicici&pn=%E0%A4%B0%E0%A4%B5%E0%A4%BF&tn=50%25%20off%26more"
    )
    assert p.pn == "रवि"
    assert p.tn == "50% off&more"
    # a literal '+' must be percent-encoded to survive
    assert parse_upi_uri("upi://pay?pa=ab@ybl&tn=1%2B1").tn == "1+1"


def test_parse_missing_amount_and_zero_amount():
    assert parse_upi_uri("upi://pay?pa=ab@ybl&am=").am is None
    p = parse_upi_uri("upi://pay?pa=ab@ybl&am=0.00")
    assert p.am is None and p.warnings == ["zero_amount_ignored"]


def test_parse_currency_lowercase_inr_ok():
    assert parse_upi_uri("upi://pay?pa=ab@ybl&cu=inr").cu == "INR"


def test_parse_strips_control_and_bidi_characters_and_caps_lengths():
    p = parse_upi_uri("upi://pay?pa=ab@ybl&pn=Evil%E2%80%AEgnp.exe%0A%0Dname&tn=" + "x" * 400)
    assert p.pn == "Evilgnp.exe name"
    assert p.tn is not None and len(p.tn) == 255
    long_name = parse_upi_uri("upi://pay?pa=ab@ybl&pn=" + "N" * 300).pn
    assert long_name is not None and len(long_name) == 120


def test_parse_drops_bad_optional_fields_with_warnings():
    p = parse_upi_uri(
        "upi://pay?pa=ab@ybl&mc=12a4&tr=bad%20ref%3Cscript%3E&url=javascript:alert(1)"
    )
    assert p.mc is None and p.tr is None and p.url is None
    assert set(p.warnings) == {"merchant_code_ignored", "reference_ignored", "url_ignored"}
    assert parse_upi_uri("upi://pay?pa=ab@ybl&tr=" + "A" * 36).tr is None


def test_parse_unknown_params_are_capped_and_credentials_dropped():
    extra = "&".join(f"k{i}=v{i}" for i in range(30))
    p = parse_upi_uri(f"upi://pay?pa=ab@ybl&upi_pin=1234&mPin=1&{extra}&bad%20key=1")
    assert "upi_pin" not in p.unknown and "mpin" not in p.unknown
    assert len(p.unknown) == 20
    assert "param_dropped" in p.warnings


# --- parse: rejections --------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "javascript:alert(1)//upi://pay?pa=ab@ybl",
        "http://pay?pa=ab@ybl",
        "https://evil.example/upi://pay?pa=ab@ybl",
        "intent://pay?pa=ab@ybl#Intent;scheme=upi;end",
        "upi://mandate?pa=ab@ybl",
        "upi://pay.evil.com?pa=ab@ybl",
        "upi://payx?pa=ab@ybl",
        "upi:pay?pa=ab@ybl",
        "upi://pay/extra?pa=ab@ybl",
        " upi ://pay?pa=ab@ybl",
        "data:text/html,<script>",
        "",
    ],
)
def test_parse_rejects_non_upi_payloads(payload):
    assert code_of(payload) == "not_upi_uri"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("upi://pay", "missing_vpa"),
        ("upi://pay?", "missing_vpa"),
        ("upi://pay?pn=Shop&am=10", "missing_vpa"),
        ("upi://pay?pa=", "missing_vpa"),
        ("upi://pay?pa=plainname", "invalid_vpa"),
        ("upi://pay?pa=x@bank", "invalid_vpa"),
        ("upi://pay?pa=abc@1bank", "invalid_vpa"),
        ("upi://pay?pa=ab%20c@bank", "invalid_vpa"),
        ("upi://pay?pa=abc@bank@x", "invalid_vpa"),
        ("upi://pay?pa=abc@ba'nk", "invalid_vpa"),
        ("upi://pay?pa=<script>@ybl", "invalid_vpa"),
        ("upi://pay?pa=" + "a" * 250 + "@okaxis", "invalid_vpa"),
        ("upi://pay?pa=ab@ybl&am=-5", "invalid_amount"),
        ("upi://pay?pa=ab@ybl&am=1.005", "invalid_amount"),
        ("upi://pay?pa=ab@ybl&am=1,000", "invalid_amount"),
        ("upi://pay?pa=ab@ybl&am=1e3", "invalid_amount"),
        ("upi://pay?pa=ab@ybl&am=NaN", "invalid_amount"),
        ("upi://pay?pa=ab@ybl&am=abc", "invalid_amount"),
        ("upi://pay?pa=ab@ybl&cu=USD", "unsupported_currency"),
        ("upi://pay?pa=ab@ybl&pa=evil@ybl", "duplicate_param"),
        ("upi://pay?pa=ab@ybl&AM=10&am=1000", "duplicate_param"),
        ("upi://pay?pa=ab@ybl&pn=%FF%FE", "invalid_upi_uri"),
    ],
)
def test_parse_rejects_invalid_fields(payload, code):
    assert code_of(payload) == code


def test_parse_payload_length_cap():
    base = "upi://pay?pa=ab@ybl&tn="
    ok = base + "a" * (MAX_PAYLOAD_BYTES - len(base))
    assert parse_upi_uri(ok).pa == "ab@ybl"
    assert code_of(ok + "a") == "payload_too_long"
    # multi-byte characters count as bytes
    assert code_of(base + "₹" * 700) == "payload_too_long"


def test_parse_rejects_non_string():
    with pytest.raises(UpiUriError):
        parse_upi_uri(None)  # type: ignore[arg-type]


# --- build --------------------------------------------------------------------------------


def test_build_uri_encoding_and_format():
    uri = build_upi_uri(
        pa="Ravi@OKAXIS",
        pn="Ravi & Sons + Co",
        am=Decimal("5"),
        tr="FBABCDEFGHIJKLMNOPQR",
        tn="Rent for Nov=paid?",
        mc="5411",
    )
    assert uri == (
        "upi://pay?pa=ravi@okaxis&pn=Ravi%20%26%20Sons%20%2B%20Co&am=5.00&cu=INR"
        "&tn=Rent%20for%20Nov%3Dpaid%3F&tr=FBABCDEFGHIJKLMNOPQR&mc=5411"
    )
    assert "+" not in uri


def test_build_uri_defaults_note_trim_and_optional_fields():
    uri = build_upi_uri(pa="ab@ybl", pn=None, am=Decimal("99.9"), tr="FBX", tn="  " + "n" * 100)
    params = dict(parse_qsl(uri.split("?", 1)[1]))
    assert params["pn"] == "ab"  # falls back to the VPA handle
    assert params["am"] == "99.90"
    assert len(params["tn"]) == 80
    assert "mc" not in params
    no_note = build_upi_uri(pa="ab@ybl", pn="A", am=Decimal("1"), tr="FBX", tn="   ", mc="12")
    assert "tn=" not in no_note and "mc=" not in no_note
    # parameter order is fixed
    assert re.match(r"^upi://pay\?pa=[^&]+&pn=[^&]+&am=[^&]+&cu=INR&tr=FBX$", no_note)


def test_build_uri_rejects_bad_input():
    with pytest.raises(UpiUriError):
        build_upi_uri(pa="nope", pn="A", am=Decimal("1"), tr="FBX")
    with pytest.raises(UpiUriError):
        build_upi_uri(pa="ab@ybl", pn="A", am=Decimal("0"), tr="FBX")
    with pytest.raises(UpiUriError):
        build_upi_uri(pa="ab@ybl", pn="A", am=Decimal("1"), tr="bad ref&x=1")


def test_build_then_parse_round_trip():
    tr = new_tr_ref()
    uri = build_upi_uri(
        pa="shop.42@okhdfcbank",
        pn="Shree Ganesh Medicals",
        am=Decimal("1234.5"),
        tr=tr,
        tn="Medicines #12 for amma (50% off)",
        mc="5912",
    )
    p = parse_upi_uri(uri)
    assert p.pa == "shop.42@okhdfcbank"
    assert p.pn == "Shree Ganesh Medicals"
    assert p.am == Decimal("1234.50")
    assert p.tn == "Medicines #12 for amma (50% off)"
    assert p.tr == tr and p.mc == "5912" and p.cu == "INR"
    assert p.warnings == [] and p.unknown == {}


# --- references ---------------------------------------------------------------------------


def test_tr_refs_are_unique_short_and_alphanumeric():
    refs = {new_tr_ref() for _ in range(2000)}
    assert len(refs) == 2000
    for ref in refs:
        assert len(ref) == 20 and ref.startswith("FB") and ref.isalnum() and ref.isupper()
        assert re.fullmatch(r"FB[A-Z2-7]{18}", ref)
        assert is_tr_ref(ref)
    assert not is_tr_ref("FBshort") and not is_tr_ref("XX" + "A" * 18)


# --- credentials & return data ------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "pin",
        "PIN",
        "upi_pin",
        "upiPin",
        "mpin",
        "mPin",
        "MPIN",
        "atm_pin",
        "password",
        "newPassword",
        "passcode",
        "cvv",
        "card_cvv",
        "otp",
        "otp_code",
        "card_number",
        "client_secret",
        "credentials",
    ],
)
def test_credential_keys_detected(key):
    assert is_credential_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "payee_vpa",
        "amount",
        "note",
        "shipping",
        "mapping",
        "spinner",
        "opinion",
        "status",
        "txnId",
        "responseCode",
        "ApprovalRefNo",
        "pn",
        "pa",
        "tn",
    ],
)
def test_ordinary_keys_not_flagged(key):
    assert not is_credential_key(key)


def test_sanitize_return_string():
    raw = (
        "txnId=AXI123&responseCode=00&Status=SUCCESS&txnRef=FBABC&ApprovalRefNo=432109876543"
        "&upiPin=1234&MPIN=9999&bad key=1&note=" + "z" * 500
    )
    s = sanitize_return(raw)
    assert s is not None and s["format"] == "string"
    fields = s["fields"]
    assert fields["Status"] == "SUCCESS" and fields["txnId"] == "AXI123"
    assert "upiPin" not in fields and "MPIN" not in fields and "bad key" not in fields
    assert len(fields["note"]) == 200
    assert s["dropped"] == 3
    assert "1234" not in str(s) and "9999" not in str(s)


def test_sanitize_return_object_and_limits():
    raw = {
        "Status": "FAILURE",
        "responseCode": 22,
        "ok": True,
        "nested": {"pin": "1234"},
        "list": [1, 2],
        "password": "x",
        **{f"f{i}": "v" for i in range(40)},
    }
    s = sanitize_return(raw)
    assert s is not None and s["format"] == "object"
    assert s["fields"]["responseCode"] == "22" and s["fields"]["ok"] == "true"
    assert "nested" not in s["fields"] and "password" not in s["fields"]
    assert len(s["fields"]) == 30 and s["truncated"] is True
    assert sanitize_return(None) is None
    huge = sanitize_return("Status=SUCCESS&x=" + "y" * 10_000)
    assert huge is not None and huge["truncated"] is True


def test_return_hint():
    s = sanitize_return(
        "Status=success&txnId=T1&responseCode=00&ApprovalRefNo=123456789012&txnRef=FBX"
    )
    hint = return_hint(s, "FBX")
    assert hint == {
        "status": "success",
        "txn_id": "T1",
        "response_code": "00",
        "approval_ref": "123456789012",
        "txn_ref": "FBX",
        "reference_matches": True,
        "suggested_utr": "123456789012",
    }
    assert return_hint(sanitize_return("status=FAILED&txnRef=OTHER"), "FBX")["status"] == "failure"
    assert return_hint(sanitize_return("Status=SUBMITTED"), "FBX")["reference_matches"] is None
    assert return_hint(sanitize_return("Status=weird&ApprovalRefNo=a b"), "FBX") == {
        "status": "unknown",
        "txn_id": None,
        "response_code": None,
        "approval_ref": "a b",
        "txn_ref": None,
        "reference_matches": None,
        "suggested_utr": None,
    }
    assert return_hint(None, "FBX") is None


def test_clean_text():
    assert clean_text(None, 10) is None
    assert clean_text("   ", 10) is None
    assert clean_text(" a\t b\n\nc ", 10) == "a b c"
    assert clean_text("‮abc\u0000", 10) == "abc"
    assert clean_text("abcdef", 3) == "abc"
