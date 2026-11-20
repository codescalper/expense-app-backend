from __future__ import annotations

import io
import os
import zlib
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.errors import BadRequestError
from app.db.session import get_database
from app.models.finance import TransactionAttachment
from app.models.system import AuditLog
from app.services.storage import process_receipt
from tests.helpers_catalog import API, household, insert_tx, user

MB = 1024 * 1024


# --- file fixtures ---------------------------------------------------------------------


def jpeg_with_exif(size: tuple[int, int] = (3000, 1500), orientation: int = 1) -> bytes:
    img = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SpyCam Inc"  # Make
    exif[0x0131] = "secret-software"  # Software
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    data = buf.getvalue()
    assert b"SpyCam" in data
    return data


def png(size: tuple[int, int] = (40, 30), mode: str = "RGBA") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, 0).save(buf, format="PNG")
    return buf.getvalue()


def webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (0, 128, 0)).save(buf, format="WEBP")
    return buf.getvalue()


def pdf(
    catalog_extra: bytes = b"", trailer_extra: bytes = b"", extra_objects: bytes = b""
) -> bytes:
    # The content stream deliberately contains "/JS" and "/Launch" as *stream data* (font
    # resource names) — only dictionaries are inspected, so these must not trigger rejection.
    content = b"BT /JS 12 Tf (Total: Rs 450) Tj /Launch 1 Tf ET"
    return (
        b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R " + catalog_extra + b">>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream\nendobj\n"
        + extra_objects
        + b"trailer\n<< /Root 1 0 R "
        + trailer_extra
        + b">>\n%%EOF\n"
    )


def object_stream(payload: bytes) -> bytes:
    data = zlib.compress(payload)
    return (
        b"5 0 obj\n<< /Type /ObjStm /N 1 /First 6 /Filter /FlateDecode /Length "
        + str(len(data)).encode()
        + b" >>\nstream\n"
        + data
        + b"\nendstream\nendobj\n"
    )


async def upload(
    client, who, tx_id: str, data: bytes, name: str = "r.jpg", mime: str = "image/jpeg"
):
    return await client.post(
        f"{API}/transactions/{tx_id}/attachments",
        headers=who.h,
        files={"file": (name, data, mime)},
    )


def error_code(r: Any) -> str:
    return str(r.json()["error"]["code"])


# --- processing (unit) -----------------------------------------------------------------


def test_process_receipt_rules():
    out = process_receipt(jpeg_with_exif((3000, 1500), orientation=6), 5 * MB)
    assert out.content_type == "image/jpeg" and out.ext == "jpg"
    with Image.open(io.BytesIO(out.data)) as img:
        assert img.format == "JPEG"
        assert img.size == (1000, 2000)  # rotated per EXIF, long side 2000, aspect kept
        assert len(img.getexif()) == 0 and "exif" not in img.info
    assert b"SpyCam" not in out.data and b"secret-software" not in out.data

    small = process_receipt(png((40, 30)), 5 * MB)  # never upscaled; alpha flattened
    with Image.open(io.BytesIO(small.data)) as img:
        assert img.size == (40, 30) and img.mode == "RGB"
    assert process_receipt(webp(), 5 * MB).content_type == "image/jpeg"

    good_pdf = pdf()
    assert process_receipt(good_pdf, 5 * MB).data == good_pdf
    assert process_receipt(pdf(extra_objects=object_stream(b"6 0 << /A 1 >>")), 5 * MB)

    for bad, code in (
        (pdf(b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >> "), "unsafe_pdf"),
        (pdf(b"/OpenAction << /S /Launch /F (cmd.exe) >> "), "unsafe_pdf"),
        (pdf(b"/OpenAction << /S /Java#53cript /J#53 (x) >> "), "unsafe_pdf"),
        (pdf(b"/Names << /EmbeddedFiles 9 0 R >> "), "unsafe_pdf"),
        (pdf(extra_objects=object_stream(b"6 0 << /S /JavaScript /JS (x) >>")), "unsafe_pdf"),
        (pdf(trailer_extra=b"/Encrypt 7 0 R "), "pdf_encrypted"),
        (b"%PDF-1.4\n garbage without trailer", "invalid_file"),
        (b"GIF89a" + b"\x00" * 100, "unsupported_file_type"),
        (b"<html><script>alert(1)</script></html>", "unsupported_file_type"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "invalid_file"),
        (b"\xff\xd8\xff\xe0" + os.urandom(500), "invalid_file"),
        (png((4097, 4096), mode="L"), "image_too_large"),
        (b"", "invalid_file"),
    ):
        with pytest.raises(BadRequestError) as exc:
            process_receipt(bad, 5 * MB)
        assert exc.value.code == code, (code, bad[:40])


# --- upload / list / download / delete -------------------------------------------------


async def test_upload_list_download_delete_image(client):
    asha = await user(client, "asha")
    tx = await insert_tx(asha)
    r = await upload(client, asha, tx, jpeg_with_exif(), name="../../etc/passwd.jpg")
    assert r.status_code == 201, r.text
    att = r.json()
    assert att["kind"] == "image" and att["content_type"] == "image/jpeg"
    assert att["transaction_id"] == tx and att["uploaded_by_user_id"] == asha.id
    assert att["download_url"] == f"/api/v1/transactions/{tx}/attachments/{att['id']}/download"

    listed = await client.get(f"{API}/transactions/{tx}/attachments", headers=asha.h)
    assert [a["id"] for a in listed.json()] == [att["id"]]

    d = await client.get(att["download_url"], headers=asha.h)
    assert d.status_code == 200
    assert d.headers["content-type"] == "image/jpeg"
    assert d.headers["content-disposition"].startswith("attachment;")
    assert "receipt-" in d.headers["content-disposition"]
    assert d.headers["x-content-type-options"] == "nosniff"
    assert len(d.content) == att["size_bytes"]
    with Image.open(io.BytesIO(d.content)) as img:
        assert img.size == (2000, 1000) and len(img.getexif()) == 0
    assert b"SpyCam" not in d.content
    assert (await client.get(att["download_url"])).status_code == 401

    r = await client.delete(f"{API}/transactions/{tx}/attachments/{att['id']}", headers=asha.h)
    assert r.status_code == 204
    assert (await client.get(f"{API}/transactions/{tx}/attachments", headers=asha.h)).json() == []
    assert (await client.get(att["download_url"], headers=asha.h)).status_code == 404
    r = await client.delete(f"{API}/transactions/{tx}/attachments/{att['id']}", headers=asha.h)
    assert r.status_code == 404
    async with get_database().sessionmaker() as s:
        row = await s.scalar(select(TransactionAttachment))
        assert row is not None and row.deleted_at is not None  # soft delete
        actions = set(await s.scalars(select(AuditLog.action)))
    assert {"transaction.attachment_added", "transaction.attachment_deleted"} <= actions


async def test_pdf_upload_and_download(client):
    asha = await user(client, "asha")
    tx = await insert_tx(asha)
    doc = pdf()
    r = await upload(client, asha, tx, doc, name="bill.pdf", mime="application/pdf")
    assert r.status_code == 201, r.text
    att = r.json()
    assert att["kind"] == "pdf" and att["content_type"] == "application/pdf"
    d = await client.get(att["download_url"], headers=asha.h)
    assert d.headers["content-type"] == "application/pdf"
    assert d.headers["content-disposition"].startswith("attachment;")
    assert ".pdf" in d.headers["content-disposition"]
    assert d.content == doc


async def test_rejected_uploads(client):
    asha = await user(client, "asha")
    tx = await insert_tx(asha)
    cases = (
        # disguised: script/HTML claiming to be an image, a fake PNG, an executable "pdf"
        (b"<?php system($_GET['c']); ?>", "x.jpg", "image/jpeg", 400, "unsupported_file_type"),
        (b"\x89PNG\r\n\x1a\n<script>alert(1)</script>", "x.png", "image/png", 400, "invalid_file"),
        (b"MZ\x90\x00" + b"\x00" * 64, "x.pdf", "application/pdf", 400, "unsupported_file_type"),
        (
            pdf(b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >> "),
            "x.pdf",
            "application/pdf",
            400,
            "unsafe_pdf",
        ),
        (png((4097, 4096), mode="L"), "huge.png", "image/png", 400, "image_too_large"),
        (
            b"\xff\xd8\xff\xe0" + b"\x00" * (5 * MB),
            "big.jpg",
            "image/jpeg",
            413,
            "payload_too_large",
        ),
    )
    for data, name, mime, status, code in cases:
        r = await upload(client, asha, tx, data, name=name, mime=mime)
        assert r.status_code == status and error_code(r) == code, (name, r.text)
    assert (await client.get(f"{API}/transactions/{tx}/attachments", headers=asha.h)).json() == []


async def test_attachment_access_rules(client):
    fam, p = await household(client)
    stranger = await user(client, "stranger")
    personal_tx = await insert_tx(p["owner"])
    member_tx = await insert_tx(p["member"], family_id=fam)
    admin_tx = await insert_tx(p["admin"], family_id=fam)
    deleted_tx = await insert_tx(p["owner"], deleted=True)

    # other user's personal transaction / unknown / deleted → 404 for everything
    att = (await upload(client, p["owner"], personal_tx, png())).json()
    for who, tx in ((p["admin"], personal_tx), (stranger, member_tx), (p["owner"], deleted_tx)):
        assert (await upload(client, who, tx, png())).status_code == 404
        r = await client.get(f"{API}/transactions/{tx}/attachments", headers=who.h)
        assert r.status_code == 404
    r = await client.get(
        f"{API}/transactions/{personal_tx}/attachments/{att['id']}/download", headers=p["admin"].h
    )
    assert r.status_code == 404
    r = await client.delete(
        f"{API}/transactions/{personal_tx}/attachments/{att['id']}", headers=stranger.h
    )
    assert r.status_code == 404
    # an attachment id under the wrong transaction → 404
    r = await client.get(
        f"{API}/transactions/{member_tx}/attachments/{att['id']}/download", headers=p["owner"].h
    )
    assert r.status_code == 404

    # family transactions: member writes own; viewer reads only; admin writes any
    member_att = await upload(client, p["member"], member_tx, png())
    assert member_att.status_code == 201
    r = await upload(client, p["viewer"], member_tx, png())
    assert r.status_code == 403 and error_code(r) == "insufficient_family_role"
    assert (await upload(client, p["member"], admin_tx, png())).status_code == 403
    assert (await upload(client, p["admin"], member_tx, png())).status_code == 201

    listed = await client.get(f"{API}/transactions/{member_tx}/attachments", headers=p["viewer"].h)
    assert listed.status_code == 200 and len(listed.json()) == 2
    url = member_att.json()["download_url"]
    assert (await client.get(url, headers=p["viewer"].h)).status_code == 200
    r = await client.delete(url.removesuffix("/download"), headers=p["viewer"].h)
    assert r.status_code == 403
    assert (await client.get(url, headers=stranger.h)).status_code == 404
    r = await client.delete(url.removesuffix("/download"), headers=p["owner"].h)
    assert r.status_code == 204


async def test_attachment_limit_and_rate_limit(client, settings):
    asha = await user(client, "asha")
    tx = await insert_tx(asha)
    for _ in range(10):
        assert (await upload(client, asha, tx, png())).status_code == 201
    r = await upload(client, asha, tx, png())
    assert r.status_code == 409 and error_code(r) == "attachment_limit"

    settings.receipt_upload_limit = 12
    other = await insert_tx(asha)
    assert (await upload(client, asha, other, png())).status_code == 201  # 12th upload
    r = await upload(client, asha, other, png())
    assert r.status_code == 429 and "retry-after" in r.headers
