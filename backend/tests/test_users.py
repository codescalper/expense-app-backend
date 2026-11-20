from __future__ import annotations

import io
import uuid

from PIL import Image
from sqlalchemy import text

from app.db.session import get_database
from tests.conftest import PASSWORD, bearer, login, register_verified

API = "/api/v1"


def _png(size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


async def test_get_and_update_profile(client):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    me = (await client.get(f"{API}/users/me", headers=h)).json()
    assert me["email"] == "asha@example.com" and me["timezone"] == "Asia/Kolkata"
    assert "password_hash" not in me

    r = await client.patch(
        f"{API}/users/me", headers=h, json={"full_name": "Asha R", "timezone": "Europe/London"}
    )
    assert r.status_code == 200
    assert r.json()["full_name"] == "Asha R" and r.json()["timezone"] == "Europe/London"

    r = await client.patch(f"{API}/users/me", headers=h, json={"timezone": "Mars/Olympus"})
    assert r.status_code == 422
    r = await client.patch(f"{API}/users/me", headers=h, json={"email": "evil@example.com"})
    assert r.status_code == 422  # email is not user-editable here


async def test_change_password_keeps_current_session_only(client):
    first = await register_verified(client)
    second = (await login(client)).json()
    h = bearer(second["tokens"])

    r = await client.post(
        f"{API}/users/me/change-password",
        headers=h,
        json={"current_password": "wrong one entirely", "new_password": "fresh new passphrase"},
    )
    # 400, not 401: the token is fine (clients treat 401 as "refresh the session").
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_password"

    r = await client.post(
        f"{API}/users/me/change-password",
        headers=h,
        json={"current_password": PASSWORD, "new_password": "fresh new passphrase"},
    )
    assert r.status_code == 204
    assert (await client.get(f"{API}/users/me", headers=h)).status_code == 200
    assert (await client.get(f"{API}/users/me", headers=bearer(first["tokens"]))).status_code == 401
    assert (await login(client, password="fresh new passphrase")).status_code == 200


async def test_sessions_list_and_revoke(client):
    first = await register_verified(client)
    second = (await login(client)).json()
    h = bearer(second["tokens"])
    sessions = (await client.get(f"{API}/users/me/sessions", headers=h)).json()
    assert len(sessions) == 2
    current = [s for s in sessions if s["is_current"]]
    other = [s for s in sessions if not s["is_current"]]
    assert len(current) == 1 and len(other) == 1
    assert other[0]["device_name"] == "Pixel"

    r = await client.delete(f"{API}/users/me/sessions/{other[0]['id']}", headers=h)
    assert r.status_code == 204
    assert (await client.get(f"{API}/users/me", headers=bearer(first["tokens"]))).status_code == 401


async def test_cannot_see_or_revoke_another_users_session(client):
    """IDOR: user B must not be able to touch user A's sessions."""
    alice = await register_verified(client, email="alice@example.com")
    bob = await register_verified(client, email="bob@example.com", full_name="Bob")
    alice_sessions = (
        await client.get(f"{API}/users/me/sessions", headers=bearer(alice["tokens"]))
    ).json()
    bob_sessions = (
        await client.get(f"{API}/users/me/sessions", headers=bearer(bob["tokens"]))
    ).json()
    assert {s["id"] for s in alice_sessions}.isdisjoint({s["id"] for s in bob_sessions})

    r = await client.delete(
        f"{API}/users/me/sessions/{alice_sessions[0]['id']}", headers=bearer(bob["tokens"])
    )
    assert r.status_code == 404
    assert (await client.get(f"{API}/users/me", headers=bearer(alice["tokens"]))).status_code == 200


async def test_avatar_upload_fetch_delete(client):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    assert (await client.get(f"{API}/users/me/avatar", headers=h)).status_code == 404

    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("me.png", _png(), "image/png")}
    )
    assert r.status_code == 200 and r.json()["has_avatar"] is True

    r = await client.get(f"{API}/users/me/avatar", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(r.content))
    assert img.format == "JPEG" and img.size[0] == img.size[1]  # re-encoded, square-cropped

    assert (await client.get(f"{API}/users/me/avatar")).status_code == 401  # not public
    assert (await client.delete(f"{API}/users/me/avatar", headers=h)).status_code == 204
    assert (await client.get(f"{API}/users/me/avatar", headers=h)).status_code == 404


async def test_avatar_rejects_non_images_and_oversize(client, settings):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    fake = b"<?php echo 'hi'; ?>"
    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("x.png", fake, "image/png")}
    )
    assert r.status_code == 400
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, format="GIF")
    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("x.gif", gif.getvalue(), "image/gif")}
    )
    assert r.status_code == 400

    settings.max_avatar_bytes = 1000
    r = await client.put(
        f"{API}/users/me/avatar",
        headers=h,
        files={"file": ("big.png", _png((400, 400)) + b"\0" * 2000, "image/png")},
    )
    assert r.status_code == 413


async def test_delete_account(client):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    r = await client.post(f"{API}/users/me/delete", headers=h, json={"password": "not my password"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_password"
    r = await client.post(f"{API}/users/me/delete", headers=h, json={"password": PASSWORD})
    assert r.status_code == 204
    assert (await client.get(f"{API}/users/me", headers=h)).status_code == 401
    assert (await login(client)).status_code == 401
    # The email address is released and can register again.
    again = await register_verified(client)
    assert again["user"]["id"] != auth["user"]["id"]


# --- regression tests: security review -------------------------------------------------------


async def test_wrong_reauth_passwords_are_throttled(client, settings):
    settings.reauth_max_failures = 3
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    for _ in range(3):
        r = await client.post(f"{API}/users/me/delete", headers=h, json={"password": "nope nope"})
        assert r.status_code == 400
    # Locked for both re-auth endpoints, even with the right password.
    r = await client.post(
        f"{API}/users/me/change-password",
        headers=h,
        json={"current_password": PASSWORD, "new_password": "fresh new passphrase"},
    )
    assert r.status_code == 429 and "Retry-After" in r.headers
    r = await client.post(f"{API}/users/me/delete", headers=h, json={"password": PASSWORD})
    assert r.status_code == 429
    # The session itself is untouched.
    assert (await client.get(f"{API}/users/me", headers=h)).status_code == 200


async def test_reauth_requests_rate_limited_per_user(client, settings):
    settings.reauth_rate_limit = 2
    settings.reauth_max_failures = 100
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    for _ in range(2):
        r = await client.post(f"{API}/users/me/delete", headers=h, json={"password": "nope nope"})
        assert r.status_code == 400
    r = await client.post(f"{API}/users/me/delete", headers=h, json={"password": "nope nope"})
    assert r.status_code == 429


async def test_delete_account_scrubs_session_device_data(client):
    auth = await register_verified(client)
    await login(client)  # a second session
    r = await client.post(
        f"{API}/users/me/delete", headers=bearer(auth["tokens"]), json={"password": PASSWORD}
    )
    assert r.status_code == 204
    async with get_database().sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT device_name, user_agent, ip_address, revoked_at FROM user_sessions "
                    "WHERE user_id = :u"
                ),
                {"u": uuid.UUID(auth["user"]["id"])},
            )
        ).all()
    assert len(rows) == 2
    assert all(r[0] is None and r[1] is None and r[2] is None and r[3] is not None for r in rows)


async def test_avatar_rejects_huge_dimensions_before_decoding(client):
    """A tiny file declaring a huge canvas (decompression bomb) is refused from its header."""
    auth = await register_verified(client)
    buf = io.BytesIO()
    Image.new("1", (5000, 4000)).save(buf, format="PNG")  # 20 MP, a few KB on disk
    assert len(buf.getvalue()) < 100_000
    r = await client.put(
        f"{API}/users/me/avatar",
        headers=bearer(auth["tokens"]),
        files={"file": ("bomb.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "image_too_large"


async def test_avatar_uploads_rate_limited_per_user(client, settings):
    settings.avatar_upload_limit = 2
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    for _ in range(2):
        r = await client.put(
            f"{API}/users/me/avatar", headers=h, files={"file": ("me.png", _png(), "image/png")}
        )
        assert r.status_code == 200
    r = await client.put(
        f"{API}/users/me/avatar", headers=h, files={"file": ("me.png", _png(), "image/png")}
    )
    assert r.status_code == 429


async def test_profile_name_control_characters_stripped(client):
    auth = await register_verified(client)
    h = bearer(auth["tokens"])
    r = await client.patch(f"{API}/users/me", headers=h, json={"full_name": "A\nB\u202e\tC"})
    assert r.status_code == 200 and r.json()["full_name"] == "A B C"
    r = await client.patch(f"{API}/users/me", headers=h, json={"full_name": "\r\n"})
    assert r.status_code == 422
    # Joiners used by Indic scripts are kept.
    r = await client.patch(
        f"{API}/users/me", headers=h, json={"full_name": "\u0915\u094d\u200d\u0937"}
    )
    assert r.json()["full_name"] == "\u0915\u094d\u200d\u0937"


async def test_delete_account_requires_family_ownership_transfer(client):
    owner = await register_verified(client, email="owner@example.com", full_name="Owner")
    bob = await register_verified(client, email="bob@example.com", full_name="Bob")
    oh, bh = bearer(owner["tokens"]), bearer(bob["tokens"])
    r = await client.post(f"{API}/families", headers=oh, json={"name": "Rao Family"})
    assert r.status_code == 201, r.text
    family_id = r.json()["id"]
    r = await client.post(
        f"{API}/families/{family_id}/invitations",
        headers=oh,
        json={"email": "bob@example.com", "role": "member"},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/families/invitations/accept", headers=bh, json={"token": r.json()["token"]}
    )
    assert r.status_code == 200, r.text

    r = await client.post(f"{API}/users/me/delete", headers=oh, json={"password": PASSWORD})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "family_ownership_transfer_required"
    assert (await client.get(f"{API}/users/me", headers=oh)).status_code == 200  # untouched

    # A plain member can leave by deleting; then the owner is alone and may delete too,
    # which also soft-deletes the now-empty family.
    r = await client.post(f"{API}/users/me/delete", headers=bh, json={"password": PASSWORD})
    assert r.status_code == 204
    r = await client.post(f"{API}/users/me/delete", headers=oh, json={"password": PASSWORD})
    assert r.status_code == 204
    async with get_database().sessionmaker() as s:
        deleted_at = (
            await s.execute(
                text("SELECT deleted_at FROM families WHERE id = :f"), {"f": uuid.UUID(family_id)}
            )
        ).scalar_one()
    assert deleted_at is not None
