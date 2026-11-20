from __future__ import annotations

import io
import re
import uuid
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.core.errors import ConflictError
from app.core.security import utcnow
from app.db.session import get_database
from app.models.family import Family, FamilyInvitation, FamilyMember
from app.models.system import AuditLog
from tests.conftest import bearer, emails_to, register_verified

API = "/api/v1"


# --- helpers ---------------------------------------------------------------------------


class Actor:
    def __init__(self, auth: dict[str, Any]) -> None:
        self.id = auth["user"]["id"]
        self.email = auth["user"]["email"]
        self.h = bearer(auth["tokens"])


async def user(client, name: str) -> Actor:
    return Actor(
        await register_verified(client, email=f"{name}@example.com", full_name=name.title())
    )


async def create_family(client, owner: Actor, name: str = "Rao Family") -> dict[str, Any]:
    r = await client.post(f"{API}/families", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return dict(r.json())


async def invite(client, by: Actor, family_id: str, email: str, role: str = "member"):
    return await client.post(
        f"{API}/families/{family_id}/invitations", headers=by.h, json={"email": email, "role": role}
    )


async def join(client, owner: Actor, family_id: str, who: Actor, role: str) -> None:
    r = await invite(client, owner, family_id, who.email, role)
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/families/invitations/accept", headers=who.h, json={"token": r.json()["token"]}
    )
    assert r.status_code == 200, r.text


async def household(client) -> tuple[dict[str, Any], dict[str, Actor]]:
    """Family with one user per role."""
    people = {n: await user(client, n) for n in ("owner", "admin", "member", "viewer")}
    fam = await create_family(client, people["owner"])
    for role in ("admin", "member", "viewer"):
        await join(client, people["owner"], fam["id"], people[role], role)
    return fam, people


async def audit_actions(family_id: str) -> list[str]:
    async with get_database().sessionmaker() as s:
        rows = await s.scalars(
            select(AuditLog.action)
            .where(AuditLog.family_id == uuid.UUID(family_id))
            .order_by(AuditLog.created_at)
        )
        return list(rows)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


# --- create / list / get ---------------------------------------------------------------


async def test_create_list_get_and_update_family(client):
    asha = await user(client, "asha")
    fam = await create_family(client, asha)
    assert fam["my_role"] == "owner" and fam["currency"] == "INR" and fam["has_image"] is False
    assert [m["role"] for m in fam["members"]] == ["owner"]
    assert fam["members"][0]["email"] == asha.email

    listed = (await client.get(f"{API}/families", headers=asha.h)).json()
    assert len(listed) == 1
    assert listed[0]["id"] == fam["id"]
    assert listed[0]["my_role"] == "owner" and listed[0]["member_count"] == 1

    got = await client.get(f"{API}/families/{fam['id']}", headers=asha.h)
    assert got.status_code == 200 and got.json()["name"] == "Rao Family"

    r = await client.patch(
        f"{API}/families/{fam['id']}", headers=asha.h, json={"name": "Raos", "currency": "USD"}
    )
    assert r.status_code == 200 and r.json()["name"] == "Raos" and r.json()["currency"] == "USD"

    for bad in ({"name": ""}, {"name": "x" * 81}, {"name": "A", "currency": "inr"}, {"x": 1}):
        r = await client.post(f"{API}/families", headers=asha.h, json=bad)
        assert r.status_code == 422, bad
    assert (await client.get(f"{API}/families")).status_code == 401

    assert await audit_actions(fam["id"]) == ["family.created", "family.updated"]


# --- invitations -----------------------------------------------------------------------


async def test_invite_and_accept_by_id(client):
    owner = await user(client, "owner")
    bob = await user(client, "bob")
    fam = await create_family(client, owner)

    r = await invite(client, owner, fam["id"], "BOB@Example.com", "admin")
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["email"] == "bob@example.com" and inv["role"] == "admin"
    assert inv["status"] == "pending" and len(inv["token"]) >= 40
    assert inv["deep_link"].startswith("familybudget://invite?token=")

    mails = await emails_to("bob@example.com")
    assert "Owner invited you to join Rao Family" in mails[0]["subject"]
    assert inv["token"] in mails[0]["body"]

    # The token is never exposed again.
    pending = (await client.get(f"{API}/families/{fam['id']}/invitations", headers=owner.h)).json()
    assert [p["id"] for p in pending] == [inv["id"]] and "token" not in pending[0]

    incoming = (await client.get(f"{API}/families/invitations/incoming", headers=bob.h)).json()
    assert len(incoming) == 1
    assert incoming[0]["family_name"] == "Rao Family" and incoming[0]["invited_by_name"] == "Owner"

    r = await client.post(f"{API}/families/invitations/{inv['id']}/accept", headers=bob.h)
    assert r.status_code == 200, r.text
    assert r.json()["my_role"] == "admin" and r.json()["member_count"] == 2

    detail = (await client.get(f"{API}/families/{fam['id']}", headers=bob.h)).json()
    assert {(m["email"], m["role"]) for m in detail["members"]} == {
        ("owner@example.com", "owner"),
        ("bob@example.com", "admin"),
    }
    assert (await client.get(f"{API}/families/invitations/incoming", headers=bob.h)).json() == []
    assert (
        await client.get(f"{API}/families/{fam['id']}/invitations", headers=owner.h)
    ).json() == []

    # Single use.
    r = await client.post(f"{API}/families/invitations/{inv['id']}/accept", headers=bob.h)
    assert r.status_code == 409
    assert "family.member_joined" in await audit_actions(fam["id"])


async def test_preview_and_accept_by_token(client):
    owner = await user(client, "owner")
    carol = await user(client, "carol")
    fam = await create_family(client, owner)
    # A link may be opened by an account with a different email (shared over chat).
    token = (await invite(client, owner, fam["id"], "carol.other@example.com", "viewer")).json()[
        "token"
    ]

    r = await client.post(
        f"{API}/families/invitations/preview", headers=carol.h, json={"token": token}
    )
    assert r.status_code == 200
    assert r.json()["family_name"] == "Rao Family" and r.json()["role"] == "viewer"
    assert r.json()["status"] == "pending" and r.json()["already_member"] is False

    bad = await client.post(
        f"{API}/families/invitations/accept", headers=carol.h, json={"token": "x" * 43}
    )
    assert bad.status_code == 404

    r = await client.post(
        f"{API}/families/invitations/accept", headers=carol.h, json={"token": token}
    )
    assert r.status_code == 200 and r.json()["my_role"] == "viewer"
    r = await client.post(
        f"{API}/families/invitations/accept", headers=carol.h, json={"token": token}
    )
    assert r.status_code == 409


async def test_reject_by_id_and_by_token(client):
    owner = await user(client, "owner")
    bob = await user(client, "bob")
    fam = await create_family(client, owner)
    inv = (await invite(client, owner, fam["id"], bob.email)).json()
    r = await client.post(f"{API}/families/invitations/{inv['id']}/reject", headers=bob.h)
    assert r.status_code == 204
    r = await client.post(f"{API}/families/invitations/{inv['id']}/accept", headers=bob.h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invitation_not_pending"

    token = (await invite(client, owner, fam["id"], bob.email)).json()["token"]
    r = await client.post(
        f"{API}/families/invitations/reject", headers=bob.h, json={"token": token}
    )
    assert r.status_code == 204
    assert (await client.get(f"{API}/families", headers=bob.h)).json() == []
    assert "family.invitation_rejected" in await audit_actions(fam["id"])


async def test_wrong_user_cannot_accept_someone_elses_invitation(client):
    owner = await user(client, "owner")
    bob = await user(client, "bob")
    mallory = await user(client, "mallory")
    fam = await create_family(client, owner)
    inv = (await invite(client, owner, fam["id"], bob.email)).json()

    assert (
        await client.get(f"{API}/families/invitations/incoming", headers=mallory.h)
    ).json() == []
    for action in ("accept", "reject"):
        r = await client.post(f"{API}/families/invitations/{inv['id']}/{action}", headers=mallory.h)
        assert r.status_code == 404, action
    r = await client.post(f"{API}/families/invitations/{uuid.uuid4()}/accept", headers=bob.h)
    assert r.status_code == 404
    assert (await client.get(f"{API}/families/{fam['id']}", headers=mallory.h)).status_code == 404

    # Bob's invitation is untouched.
    r = await client.post(f"{API}/families/invitations/{inv['id']}/accept", headers=bob.h)
    assert r.status_code == 200


async def test_revoked_and_expired_invitations(client):
    owner = await user(client, "owner")
    bob = await user(client, "bob")
    fam = await create_family(client, owner)

    inv = (await invite(client, owner, fam["id"], bob.email)).json()
    r = await client.delete(f"{API}/families/{fam['id']}/invitations/{inv['id']}", headers=owner.h)
    assert r.status_code == 204
    r = await client.delete(f"{API}/families/{fam['id']}/invitations/{inv['id']}", headers=owner.h)
    assert r.status_code == 409
    r = await client.post(
        f"{API}/families/invitations/accept", headers=bob.h, json={"token": inv["token"]}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "invitation_not_pending"

    inv2 = (await invite(client, owner, fam["id"], bob.email)).json()
    async with get_database().sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE family_invitations SET expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": inv2["id"]},
        )
        await s.commit()
    assert (await client.get(f"{API}/families/invitations/incoming", headers=bob.h)).json() == []
    assert (
        await client.get(f"{API}/families/{fam['id']}/invitations", headers=owner.h)
    ).json() == []
    preview = await client.post(
        f"{API}/families/invitations/preview", headers=bob.h, json={"token": inv2["token"]}
    )
    assert preview.json()["status"] == "expired"
    r = await client.post(f"{API}/families/invitations/{inv2['id']}/accept", headers=bob.h)
    assert r.status_code == 410 and r.json()["error"]["code"] == "invitation_expired"
    async with get_database().sessionmaker() as s:
        row = await s.get(FamilyInvitation, uuid.UUID(inv2["id"]))
        assert row is not None and row.status.value == "expired"
    assert (await client.get(f"{API}/families", headers=bob.h)).json() == []

    # A fresh invitation still works afterwards.
    inv3 = (await invite(client, owner, fam["id"], bob.email)).json()
    r = await client.post(f"{API}/families/invitations/{inv3['id']}/accept", headers=bob.h)
    assert r.status_code == 200


async def test_reinvite_replaces_pending_and_member_cannot_be_invited(client):
    owner = await user(client, "owner")
    bob = await user(client, "bob")
    fam = await create_family(client, owner)
    first = (await invite(client, owner, fam["id"], bob.email)).json()
    second = (await invite(client, owner, fam["id"], bob.email, "viewer")).json()
    assert first["id"] != second["id"]
    pending = (await client.get(f"{API}/families/{fam['id']}/invitations", headers=owner.h)).json()
    assert [p["id"] for p in pending] == [second["id"]]
    r = await client.post(
        f"{API}/families/invitations/accept", headers=bob.h, json={"token": first["token"]}
    )
    assert r.status_code == 409  # replaced -> revoked

    r = await client.post(
        f"{API}/families/invitations/accept", headers=bob.h, json={"token": second["token"]}
    )
    assert r.status_code == 200 and r.json()["my_role"] == "viewer"
    r = await invite(client, owner, fam["id"], bob.email)
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_member"
    r = await invite(client, owner, fam["id"], "x@example.com", "owner")
    assert r.status_code == 422


async def test_invite_creation_is_rate_limited(client, monkeypatch):
    import app.services.families as families_service

    monkeypatch.setattr(families_service, "INVITE_RATE_LIMIT", 2)
    owner = await user(client, "owner")
    fam = await create_family(client, owner)
    for i in range(2):
        assert (await invite(client, owner, fam["id"], f"p{i}@example.com")).status_code == 201
    r = await invite(client, owner, fam["id"], "p9@example.com")
    assert r.status_code == 429 and "Retry-After" in r.headers


# --- role matrix -----------------------------------------------------------------------


async def test_member_and_viewer_cannot_manage(client):
    fam, p = await household(client)
    fid = fam["id"]
    owner_id = p["owner"].id
    for who in ("member", "viewer"):
        h = p[who].h
        assert (await client.get(f"{API}/families/{fid}", headers=h)).status_code == 200
        forbidden = [
            await invite(client, p[who], fid, "new@example.com"),
            await client.get(f"{API}/families/{fid}/invitations", headers=h),
            await client.patch(f"{API}/families/{fid}", headers=h, json={"name": "Mine"}),
            await client.patch(
                f"{API}/families/{fid}/members/{p['viewer'].id}", headers=h, json={"role": "member"}
            ),
            await client.delete(f"{API}/families/{fid}/members/{p['admin'].id}", headers=h),
            await client.delete(f"{API}/families/{fid}/members/{owner_id}", headers=h),
            await client.post(
                f"{API}/families/{fid}/transfer-ownership", headers=h, json={"user_id": p[who].id}
            ),
            await client.delete(f"{API}/families/{fid}", headers=h),
            await client.put(
                f"{API}/families/{fid}/image",
                headers=h,
                files={"file": ("f.png", _png(), "image/png")},
            ),
            await client.delete(f"{API}/families/{fid}/image", headers=h),
        ]
        for r in forbidden:
            assert r.status_code == 403, (who, r.request.method, r.request.url, r.text)
            assert r.json()["error"]["code"] == "insufficient_family_role"
    detail = (await client.get(f"{API}/families/{fid}", headers=p["owner"].h)).json()
    assert len(detail["members"]) == 4 and detail["name"] == "Rao Family"


async def test_admin_limits(client):
    fam, p = await household(client)
    fid = fam["id"]
    a = p["admin"].h
    owner_id = p["owner"].id

    r = await client.patch(
        f"{API}/families/{fid}/members/{owner_id}", headers=a, json={"role": "member"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "cannot_manage_owner"
    r = await client.delete(f"{API}/families/{fid}/members/{owner_id}", headers=a)
    assert r.status_code == 403
    r = await client.patch(
        f"{API}/families/{fid}/members/{p['member'].id}", headers=a, json={"role": "owner"}
    )
    assert r.status_code == 422  # owner is never grantable
    assert (await client.delete(f"{API}/families/{fid}", headers=a)).status_code == 403
    r = await client.post(
        f"{API}/families/{fid}/transfer-ownership", headers=a, json={"user_id": p["admin"].id}
    )
    assert r.status_code == 403

    # What admins may do.
    assert (await invite(client, p["admin"], fid, "new@example.com", "admin")).status_code == 201
    r = await client.patch(
        f"{API}/families/{fid}/members/{p['member'].id}", headers=a, json={"role": "viewer"}
    )
    assert r.status_code == 200
    assert {m["user_id"]: m["role"] for m in r.json()["members"]}[p["member"].id] == "viewer"
    assert (
        await client.delete(f"{API}/families/{fid}/members/{p['viewer'].id}", headers=a)
    ).status_code == 204
    r = await client.delete(f"{API}/families/{fid}/members/{p['admin'].id}", headers=a)
    assert r.status_code == 400 and r.json()["error"]["code"] == "use_leave"
    r = await client.patch(f"{API}/families/{fid}", headers=a, json={"name": "Renamed"})
    assert r.status_code == 200
    assert (await client.get(f"{API}/families/{fid}", headers=p["viewer"].h)).status_code == 404
    r = await client.delete(f"{API}/families/{fid}/members/{p['viewer'].id}", headers=a)
    assert r.status_code == 404  # no longer a member

    actions = await audit_actions(fid)
    assert "family.member_role_changed" in actions and "family.member_removed" in actions


async def test_transfer_ownership(client):
    fam, p = await household(client)
    fid = fam["id"]
    o = p["owner"].h

    r = await client.post(
        f"{API}/families/{fid}/transfer-ownership", headers=o, json={"user_id": p["owner"].id}
    )
    assert r.status_code == 400
    r = await client.post(
        f"{API}/families/{fid}/transfer-ownership", headers=o, json={"user_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404

    r = await client.post(
        f"{API}/families/{fid}/transfer-ownership", headers=o, json={"user_id": p["member"].id}
    )
    assert r.status_code == 200, r.text
    roles = {m["user_id"]: m["role"] for m in r.json()["members"]}
    assert roles[p["member"].id] == "owner" and roles[p["owner"].id] == "admin"
    assert r.json()["my_role"] == "admin"
    assert list(roles.values()).count("owner") == 1

    assert (await client.delete(f"{API}/families/{fid}", headers=o)).status_code == 403
    r = await client.post(f"{API}/families/{fid}/leave", headers=p["member"].h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "owner_must_transfer"
    assert "family.ownership_transferred" in await audit_actions(fid)
    assert (await client.delete(f"{API}/families/{fid}", headers=p["member"].h)).status_code == 204


async def test_leave_rules_and_rejoin(client):
    fam, p = await household(client)
    fid = fam["id"]
    r = await client.post(f"{API}/families/{fid}/leave", headers=p["owner"].h)
    assert r.status_code == 409

    assert (
        await client.post(f"{API}/families/{fid}/leave", headers=p["member"].h)
    ).status_code == 204
    assert (await client.get(f"{API}/families/{fid}", headers=p["member"].h)).status_code == 404
    assert (
        await client.post(f"{API}/families/{fid}/leave", headers=p["member"].h)
    ).status_code == 404
    assert (await client.get(f"{API}/families", headers=p["member"].h)).json() == []

    # Can be invited again; history keeps the old membership row.
    await join(client, p["owner"], fid, p["member"], "viewer")
    async with get_database().sessionmaker() as s:
        rows = list(
            await s.scalars(
                select(FamilyMember).where(FamilyMember.user_id == uuid.UUID(p["member"].id))
            )
        )
    assert len(rows) == 2 and sum(r.left_at is None for r in rows) == 1
    assert "family.member_left" in await audit_actions(fid)


async def test_delete_family(client):
    fam, p = await household(client)
    fid = fam["id"]
    pending = (await invite(client, p["owner"], fid, "late@example.com")).json()
    assert (await client.delete(f"{API}/families/{fid}", headers=p["owner"].h)).status_code == 204
    for who in p.values():
        assert (await client.get(f"{API}/families/{fid}", headers=who.h)).status_code == 404
        assert (await client.get(f"{API}/families", headers=who.h)).json() == []
    late = await user(client, "late")
    r = await client.post(
        f"{API}/families/invitations/accept", headers=late.h, json={"token": pending["token"]}
    )
    assert r.status_code == 404
    async with get_database().sessionmaker() as s:
        family = await s.get(Family, uuid.UUID(fid))
        assert family is not None and family.deleted_at is not None  # soft delete
        inv = await s.get(FamilyInvitation, uuid.UUID(pending["id"]))
        assert inv is not None and inv.status.value == "revoked"


async def test_family_image(client):
    fam, p = await household(client)
    fid = fam["id"]
    r = await client.put(
        f"{API}/families/{fid}/image",
        headers=p["admin"].h,
        files={"file": ("f.png", _png(), "image/png")},
    )
    assert r.status_code == 200 and r.json()["has_image"] is True
    r = await client.get(f"{API}/families/{fid}/image", headers=p["viewer"].h)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    r = await client.put(
        f"{API}/families/{fid}/image",
        headers=p["owner"].h,
        files={"file": ("x.png", b"nope", "image/png")},
    )
    assert r.status_code == 400
    assert (
        await client.delete(f"{API}/families/{fid}/image", headers=p["owner"].h)
    ).status_code == 204
    assert (
        await client.get(f"{API}/families/{fid}/image", headers=p["viewer"].h)
    ).status_code == 404


# --- isolation -------------------------------------------------------------------------


async def test_cross_family_isolation_every_endpoint_404(client):
    alice = await user(client, "alice")
    bob = await user(client, "bob")
    carol = await user(client, "carol")
    fam_a = await create_family(client, alice, "A")
    fam_b = await create_family(client, bob, "B")
    await join(client, bob, fam_b["id"], carol, "member")
    await client.put(
        f"{API}/families/{fam_b['id']}/image",
        headers=bob.h,
        files={"file": ("f.png", _png(), "image/png")},
    )
    inv_b = (await invite(client, bob, fam_b["id"], "someone@example.com")).json()
    b = fam_b["id"]
    h = alice.h  # admin/owner of A, stranger to B

    calls = [
        ("GET", f"/families/{b}", None),
        ("PATCH", f"/families/{b}", {"name": "pwned"}),
        ("DELETE", f"/families/{b}", None),
        ("GET", f"/families/{b}/image", None),
        ("DELETE", f"/families/{b}/image", None),
        ("POST", f"/families/{b}/invitations", {"email": "x@example.com", "role": "admin"}),
        ("GET", f"/families/{b}/invitations", None),
        ("DELETE", f"/families/{b}/invitations/{inv_b['id']}", None),
        ("PATCH", f"/families/{b}/members/{carol.id}", {"role": "viewer"}),
        ("DELETE", f"/families/{b}/members/{carol.id}", None),
        ("POST", f"/families/{b}/transfer-ownership", {"user_id": alice.id}),
        ("POST", f"/families/{b}/leave", None),
        # B's invitation addressed through A's family id must not resolve either.
        ("DELETE", f"/families/{fam_a['id']}/invitations/{inv_b['id']}", None),
        # B's member through A's family.
        ("PATCH", f"/families/{fam_a['id']}/members/{carol.id}", {"role": "viewer"}),
        ("DELETE", f"/families/{fam_a['id']}/members/{bob.id}", None),
        ("POST", f"/families/{fam_a['id']}/transfer-ownership", {"user_id": bob.id}),
    ]
    for method, path, body in calls:
        r = await client.request(method, f"{API}{path}", headers=h, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "not_found"
    r = await client.put(
        f"{API}/families/{b}/image", headers=h, files={"file": ("f.png", _png(), "image/png")}
    )
    assert r.status_code == 404

    assert [f["id"] for f in (await client.get(f"{API}/families", headers=h)).json()] == [
        fam_a["id"]
    ]
    detail_b = (await client.get(f"{API}/families/{b}", headers=bob.h)).json()
    assert detail_b["name"] == "B" and len(detail_b["members"]) == 2
    assert (await client.get(f"{API}/families/{b}/invitations", headers=bob.h)).json()[0][
        "id"
    ] == inv_b["id"]
    # A random id is indistinguishable from someone else's family.
    r = await client.get(f"{API}/families/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404


# --- account-deletion hook -------------------------------------------------------------


async def test_assert_user_can_delete_account(client, settings):
    from app.services.families import FamilyService

    owner = await user(client, "owner")
    solo = await user(client, "solo")
    bob = await user(client, "bob")
    fam = await create_family(client, owner)
    solo_fam = await create_family(client, solo, "Solo")
    await join(client, owner, fam["id"], bob, "member")

    async with get_database().sessionmaker() as s:
        svc = FamilyService(s, settings)
        with pytest.raises(ConflictError) as exc:
            await svc.assert_user_can_delete_account(uuid.UUID(owner.id))
        assert exc.value.code == "family_ownership_transfer_required"
        assert exc.value.details == {"family_ids": [fam["id"]]}
        await svc.assert_user_can_delete_account(uuid.UUID(bob.id))  # plain member: fine
        await svc.assert_user_can_delete_account(uuid.UUID(solo.id))  # sole member: fine

        await svc.detach_user_for_account_deletion(uuid.UUID(solo.id), utcnow())
        await s.commit()
    assert (await client.get(f"{API}/families/{solo_fam['id']}", headers=solo.h)).status_code == 404


def test_invitation_email_has_deep_link():
    from app.services.family_emails import family_invitation_email

    mail = family_invitation_email(
        to="x@example.com",
        inviter_name="Asha",
        family_name="Raos",
        role="viewer",
        token="tok-en_123",
        expires_days=7,
    )
    assert "familybudget://invite?token=tok-en_123" in mail.body
    assert re.search(r"read-only", mail.body)
