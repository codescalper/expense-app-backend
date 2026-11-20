"""UPI deep-link helpers (spec §6) — pure functions, no I/O.

* :func:`parse_upi_uri` — parse a scanned UPI QR payload (``upi://pay?pa=…&pn=…&am=…``).
  Everything in a QR code is **untrusted input**: the scheme/host must be exactly
  ``upi://pay``, the payee VPA must be valid, amounts/currency/merchant code are validated, free
  text is stripped of control/bidi characters and capped, ambiguous duplicates are rejected.
* :func:`build_upi_uri` — the intent URI we hand to the OS app chooser, RFC 3986
  percent-encoded (spaces as ``%20``, never ``+``), amount with exactly two decimals, note ≤ 80
  characters and ``tr`` = our own reference.
* :func:`new_tr_ref` — our transaction reference: ``"FB"`` + 18 upper-case base32 characters.
* :func:`sanitize_return` / :func:`return_hint` — store whatever a UPI app hands back on
  return (untrusted) without credential-like keys, and read a *hint* from it. A hint never
  changes money state; the user confirms the outcome.
* :func:`is_credential_key` — field names that look like payment credentials (UPI PIN, MPIN,
  passwords, CVV, OTP…). We never accept or store such values.
"""

from __future__ import annotations

import base64
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import quote, unquote_plus

from app.schemas.payees import VPA_MAX_LENGTH, VPA_RE

MAX_PAYLOAD_BYTES = 2048
MAX_NAME_LENGTH = 120  # payees.name / upi_payment_attempts.payee_name
MAX_TEXT_LENGTH = 255
MAX_URI_NOTE_LENGTH = 80  # upi_payment_attempts.note; UPI apps truncate long notes anyway
MAX_TR_LENGTH = 35  # UPI spec limit for ``tr``
MAX_UNKNOWN_PARAMS = 20
MAX_UNKNOWN_KEY_LENGTH = 32
MAX_UNKNOWN_VALUE_LENGTH = 256
TR_PREFIX = "FB"
TR_RANDOM_LENGTH = 18

_PREFIX_RE = re.compile(r"^upi://pay/?$", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"^\d{1,12}(?:\.\d{1,2})?$")
_MC_RE = re.compile(r"^\d{4}$")
_TR_RE = re.compile(r"^[A-Za-z0-9._/:-]{1,35}$")
_TR_REF_RE = re.compile(rf"^{TR_PREFIX}[A-Z2-7]{{{TR_RANDOM_LENGTH}}}$")
_PARAM_KEY_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")
_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
_BIDI_CONTROLS = frozenset("‎‏‪‫‬‭‮⁦⁧⁨⁩؜﻿")
_WHITESPACE_RUN = re.compile(r"\s+")
CENT = Decimal("0.01")

# Parameters with a defined meaning. Repeating any of them is ambiguous (different UPI apps pick
# different occurrences), so a payload that does is rejected.
KNOWN_PARAMS = ("pa", "pn", "am", "cu", "tn", "mc", "tr", "mode", "purpose", "url")


class UpiUriError(ValueError):
    """An unusable UPI payload. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- credentials ----------------------------------------------------------------------

_CREDENTIAL_EXACT = frozenset(
    {
        "pin",
        "mpin",
        "upipin",
        "atmpin",
        "tpin",
        "otp",
        "pwd",
        "pass",
        "password",
        "passwd",
        "passcode",
        "cvv",
        "cvv2",
        "cvc",
        "cardnumber",
        "cardno",
        "secret",
        "credential",
        "credentials",
    }
)
_CREDENTIAL_PARTS = (
    "password",
    "passwd",
    "passcode",
    "cvv",
    "mpin",
    "upipin",
    "secret",
    "credential",
    "cardnumber",
)
_CREDENTIAL_TOKENS = frozenset({"pin", "otp", "pwd", "cvv", "cvc", "mpin"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def is_credential_key(key: str) -> bool:
    """True for names that look like a payment credential: ``pin``, ``upi_pin``, ``mPin``,
    ``password``, ``cvv``, ``otp``, ``card_number``… (``shipping`` or ``mapping`` are fine)."""
    lowered = key.lower()
    squashed = _TOKEN_SPLIT.sub("", lowered)
    if squashed in _CREDENTIAL_EXACT or any(part in squashed for part in _CREDENTIAL_PARTS):
        return True
    tokens = _TOKEN_SPLIT.split(_CAMEL_BOUNDARY.sub(" ", key).lower())
    return any(token in _CREDENTIAL_TOKENS for token in tokens)


# --- text helpers ---------------------------------------------------------------------


def clean_text(value: str | None, max_length: int) -> str | None:
    """Drop control and bidi-override characters, collapse whitespace, trim, cap length.
    Returns ``None`` for an empty result."""
    if value is None:
        return None
    kept = "".join(
        " " if ch in "\t\r\n" else ch
        for ch in value
        if ch in "\t\r\n" or (unicodedata.category(ch) != "Cc" and ch not in _BIDI_CONTROLS)
    )
    text = _WHITESPACE_RUN.sub(" ", kept).strip()[:max_length].strip()
    return text or None


def format_amount(amount: Decimal) -> str:
    """Exactly two decimals, no exponent (``Decimal("5")`` → ``"5.00"``)."""
    return f"{amount.quantize(CENT):f}"


def parse_amount(value: str) -> Decimal:
    """A positive amount with at most two decimals (``"250"``, ``"99.5"``, ``"1200.00"``)."""
    if not _AMOUNT_RE.match(value):
        raise UpiUriError("invalid_amount", "The amount must be a number with up to 2 decimals")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - the regex already guarantees it
        raise UpiUriError("invalid_amount", "Invalid amount") from exc
    if amount <= 0:
        raise UpiUriError("invalid_amount", "The amount must be greater than zero")
    return amount.quantize(CENT)


def normalize_payee_vpa(value: str | None) -> str:
    """Lower-cased, validated VPA (``name@handle``)."""
    if value is None or not value.strip():
        raise UpiUriError("missing_vpa", "The QR code has no payee UPI ID (pa)")
    vpa = value.strip().lower()
    if len(vpa) > VPA_MAX_LENGTH or not VPA_RE.match(vpa):
        raise UpiUriError("invalid_vpa", "The payee UPI ID is not valid")
    return vpa


# --- parse ----------------------------------------------------------------------------


@dataclass
class ParsedUpiUri:
    pa: str
    pn: str | None = None
    am: Decimal | None = None
    cu: str = "INR"
    tn: str | None = None
    mc: str | None = None
    tr: str | None = None
    mode: str | None = None
    purpose: str | None = None
    url: str | None = None
    unknown: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_upi_uri(payload: str) -> ParsedUpiUri:
    """Parse and validate a ``upi://pay?…`` payload. Raises :class:`UpiUriError`.

    * scheme and host are case-insensitive but must be exactly ``upi://pay`` (``javascript:``,
      ``http(s)``, ``intent:``, ``upi://mandate``… are rejected); payload ≤ 2 KB;
    * parameter names are case-insensitive; values are percent-decoded (``+`` = space); a
      repeated known parameter is rejected;
    * ``pa`` is required and must be a valid VPA (returned lower-cased);
    * ``am`` (optional) must be > 0 with ≤ 2 decimals; ``am=0`` is treated as "no amount"
      (warning ``zero_amount_ignored``);
    * ``cu`` (optional) must be ``INR``;
    * ``mc`` must be 4 digits, ``tr`` ≤ 35 safe characters and ``url`` http(s) — otherwise
      they are dropped with a warning (they never affect the payment we build);
    * unknown parameters are kept (≤ 20, capped), except credential-like ones.
    """
    if not isinstance(payload, str):
        raise UpiUriError("invalid_upi_uri", "The QR payload must be text")
    text = payload.strip()
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise UpiUriError("payload_too_long", "The QR payload is too long")
    prefix, sep, query = text.partition("?")
    if not _PREFIX_RE.match(prefix):
        raise UpiUriError("not_upi_uri", "This is not a UPI payment QR code (upi://pay)")
    if not sep or not query:
        raise UpiUriError("missing_vpa", "The QR code has no payee UPI ID (pa)")

    known: dict[str, str] = {}
    unknown: dict[str, str] = {}
    warnings: list[str] = []
    for part in query.split("&"):
        if not part:
            continue
        raw_key, _, raw_value = part.partition("=")
        try:
            key = unquote_plus(raw_key, errors="strict").strip().lower()
            value = unquote_plus(raw_value, errors="strict")
        except UnicodeDecodeError as exc:
            raise UpiUriError("invalid_upi_uri", "The QR payload is not valid UTF-8") from exc
        if key in KNOWN_PARAMS:
            if key in known:
                raise UpiUriError("duplicate_param", f"The QR code repeats the '{key}' parameter")
            known[key] = value
            continue
        if not _PARAM_KEY_RE.match(key) or is_credential_key(key):
            _warn(warnings, "param_dropped")
            continue
        if key in unknown:
            continue
        if len(unknown) >= MAX_UNKNOWN_PARAMS:
            _warn(warnings, "param_dropped")
            continue
        unknown[key] = clean_text(value, MAX_UNKNOWN_VALUE_LENGTH) or ""

    result = ParsedUpiUri(pa=normalize_payee_vpa(known.get("pa")), unknown=unknown)
    result.pn = clean_text(known.get("pn"), MAX_NAME_LENGTH)
    result.tn = clean_text(known.get("tn"), MAX_TEXT_LENGTH)

    am = (known.get("am") or "").strip()
    if am:
        if _AMOUNT_RE.match(am) and Decimal(am) == 0:
            _warn(warnings, "zero_amount_ignored")
        else:
            result.am = parse_amount(am)

    cu = (known.get("cu") or "").strip()
    if cu and cu.upper() != "INR":
        raise UpiUriError("unsupported_currency", "Only INR UPI payments are supported")

    mc = (known.get("mc") or "").strip()
    if mc:
        if _MC_RE.match(mc):
            result.mc = mc
        else:
            _warn(warnings, "merchant_code_ignored")

    tr = (known.get("tr") or "").strip()
    if tr:
        if _TR_RE.match(tr):
            result.tr = tr
        else:
            _warn(warnings, "reference_ignored")

    url = (known.get("url") or "").strip()
    if url:
        if len(url) <= MAX_TEXT_LENGTH and _URL_RE.match(url):
            result.url = url
        else:
            _warn(warnings, "url_ignored")

    result.mode = clean_text(known.get("mode"), 8)
    result.purpose = clean_text(known.get("purpose"), 8)
    result.warnings = warnings
    return result


def _warn(warnings: list[str], code: str) -> None:
    if code not in warnings:
        warnings.append(code)


# --- build ----------------------------------------------------------------------------


def _enc(value: str) -> str:
    """RFC 3986 percent-encoding for a query value: unreserved characters and ``@`` stay as is
    (``@`` is a legal query character and some UPI apps expect it raw); space is ``%20``."""
    return quote(value, safe="@")


def build_upi_uri(
    *,
    pa: str,
    pn: str | None,
    am: Decimal,
    tr: str,
    tn: str | None = None,
    mc: str | None = None,
) -> str:
    """``upi://pay?pa=…&pn=…&am=…&cu=INR&tn=…&tr=…[&mc=…]``.

    ``pn`` falls back to the VPA's handle; ``tn`` is cleaned and trimmed to 80 characters
    (omitted when empty); ``mc`` is only added when it is 4 digits."""
    vpa = normalize_payee_vpa(pa)
    if am <= 0:
        raise UpiUriError("invalid_amount", "The amount must be greater than zero")
    if not _TR_RE.match(tr):
        raise UpiUriError("invalid_reference", "Invalid transaction reference")
    name = clean_text(pn, MAX_NAME_LENGTH) or vpa.split("@", 1)[0]
    params = [("pa", vpa), ("pn", name), ("am", format_amount(am)), ("cu", "INR")]
    note = clean_text(tn, MAX_URI_NOTE_LENGTH)
    if note:
        params.append(("tn", note))
    params.append(("tr", tr))
    if mc and _MC_RE.match(mc):
        params.append(("mc", mc))
    return "upi://pay?" + "&".join(f"{k}={_enc(v)}" for k, v in params)


def new_tr_ref() -> str:
    """``"FB"`` + 18 random upper-case base32 characters (90 bits): unique, alphanumeric,
    20 characters (≤ 35 as UPI requires)."""
    random = base64.b32encode(secrets.token_bytes(12)).decode("ascii")
    return TR_PREFIX + random[:TR_RANDOM_LENGTH]


def is_tr_ref(value: str) -> bool:
    return bool(_TR_REF_RE.match(value))


# --- return data (untrusted) -------------------------------------------------------------

MAX_RETURN_BYTES = 4096
MAX_RETURN_FIELDS = 30
MAX_RETURN_KEY_LENGTH = 40
MAX_RETURN_VALUE_LENGTH = 200
_RETURN_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
UTR_PATTERN = r"^[A-Za-z0-9]{6,35}$"  # UTR / RRN / approval reference the user may enter
_UTR_RE = re.compile(UTR_PATTERN)

HintStatus = Literal["success", "failure", "submitted", "unknown"]
_STATUS_MAP: dict[str, HintStatus] = {
    "success": "success",
    "successful": "success",
    "failure": "failure",
    "failed": "failure",
    "fail": "failure",
    "submitted": "submitted",
    "pending": "submitted",
}


def sanitize_return(raw: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Reduce whatever the UPI app returned to a small, credential-free record::

        {"format": "string"|"object", "fields": {key: str}, "dropped": n, "truncated": bool}

    * a string is read as ``key=value&…`` (Android intent response) — the raw text itself is
      never stored;
    * an object keeps scalar values only (nested values are dropped);
    * credential-like keys (PIN, MPIN, password, CVV, OTP…) and odd key names are dropped;
      ≤ 30 fields, keys ≤ 40 and values ≤ 200 characters.
    """
    if raw is None:
        return None
    pairs: list[tuple[str, Any]] = []
    truncated = False
    if isinstance(raw, str):
        fmt = "string"
        text = raw.strip()
        if len(text.encode("utf-8")) > MAX_RETURN_BYTES:
            text = text.encode("utf-8")[:MAX_RETURN_BYTES].decode("utf-8", errors="ignore")
            truncated = True
        for part in text.split("&"):
            if not part:
                continue
            k, _, v = part.partition("=")
            pairs.append((unquote_plus(k).strip(), unquote_plus(v)))
    elif isinstance(raw, Mapping):
        fmt = "object"
        pairs = [(str(k).strip(), v) for k, v in raw.items()]
    else:
        return None

    fields: dict[str, str] = {}
    dropped = 0
    for key, value in pairs:
        if (
            not _RETURN_KEY_RE.match(key)
            or is_credential_key(key)
            or key in fields
            or isinstance(value, Mapping | list | tuple)
        ):
            dropped += 1
            continue
        if len(fields) >= MAX_RETURN_FIELDS:
            dropped += 1
            truncated = True
            continue
        if isinstance(value, bool):
            text_value = "true" if value else "false"
        elif value is None:
            text_value = ""
        else:
            text_value = str(value)
        fields[key] = clean_text(text_value, MAX_RETURN_VALUE_LENGTH) or ""
    return {"format": fmt, "fields": fields, "dropped": dropped, "truncated": truncated}


def _field(fields: Mapping[str, str], *names: str) -> str | None:
    lowered = {k.lower(): v for k, v in fields.items()}
    for name in names:
        value = lowered.get(name)
        if value:
            return value
    return None


def return_hint(sanitized: Mapping[str, Any] | None, tr_ref: str) -> dict[str, Any] | None:
    """What the UPI app *claims* (``Status``, ``txnId``, ``responseCode``, ``ApprovalRefNo``,
    ``txnRef``). Informational only — never used to change money state."""
    if not sanitized:
        return None
    fields: Mapping[str, str] = sanitized.get("fields") or {}
    raw_status = (_field(fields, "status") or "").strip().lower()
    status: HintStatus = _STATUS_MAP.get(raw_status, "unknown")
    txn_ref = _field(fields, "txnref", "tr")
    approval_ref = _field(fields, "approvalrefno", "approvalref")
    return {
        "status": status,
        "txn_id": _field(fields, "txnid"),
        "response_code": _field(fields, "responsecode"),
        "approval_ref": approval_ref,
        "txn_ref": txn_ref,
        "reference_matches": None if txn_ref is None else txn_ref == tr_ref,
        "suggested_utr": approval_ref if approval_ref and _UTR_RE.match(approval_ref) else None,
    }
