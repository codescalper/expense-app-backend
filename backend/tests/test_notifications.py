"""Notification inbox, preferences, device tokens, family notifications, unusual spending."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from app.core.security import utcnow
from app.db.session import get_database
from app.models import (
    DeviceToken,
    Notification,
    NotificationCategory,
    PushStatus,
    RecordScope,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
)
from app.services.notifications import (
    check_unusual_spending,
    disable_tokens_for_sessions,
    disable_tokens_for_user,
    run_unusual_spending_check,
)
from app.services.notify import notify
from tests.conftest import bearer, login, register_verified

API = "/api/v1"
N = f"{API}/notifications"
TOKEN_A = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
TOKEN_B = "ExpoPushToken[bbbbbbbbbbbbbbbbbbbbbb]"


class Actor:
    def __init__(self, auth: dict[str, Any]) -> None:
        self.id = uuid.UUID(auth["user"]["id"])
        self.email = auth["user"]["email"]
        self.h = bearer(auth["tokens"])


async def user(client, name: str) -> Actor:
    return Actor(
        await register_verified(client, email=f"{name}@example.com", full_name=name.title())
    )


async def make(user_id: uuid.UUID, category=NotificationCategory.SYSTEM, **kw: Any) -> bool:
    kw.setdefault("title", "Hello")
    kw.setdefault("body", "Body text")
    kw.setdefault("dedupe_key", uuid.uuid4().hex)
    async with get_database().sessionmaker() as s:
        created = await notify(s, user_id=user_id, category=category, **kw)
        await s.commit()
    return created


async def rows(user_id: uuid.UUID) -> list[Notification]:
    async with get_database().sessionmaker() as s:
        return list(
            await s.scalars(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.created_at)
            )
        )


async def device_rows() -> dict[str, DeviceToken]:
    async with get_database().sessionmaker() as s:
        return {d.token: d for d in await s.scalars(select(DeviceToken))}


# --- inbox -------------------------------------------------------------------------------


async def test_list_unread_mark_delete_and_isolation(client):
    asha, bob = await user(client, "asha"), await user(client, "bob")
    budget_id = str(uuid.uuid4())
    await make(asha.id, title="First", dedupe_key="sys:1")
    await make(
        asha.id,
        NotificationCategory.BUDGET_ALERT,
        title="Budget 75%",
        dedupe_key="budget:x:75",
        data={"budget_id": budget_id},
    )
    await make(asha.id, title="Later", scheduled_for=utcnow() + timedelta(days=1))
    await make(bob.id, title="Bob's")

    r = await client.get(N, headers=asha.h)
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["total"] == 2 and page["has_more"] is False
    assert [n["title"] for n in page["items"]] == ["Budget 75%", "First"]  # newest first
    budget = page["items"][0]
    assert budget["category"] == "budget_alert" and budget["read"] is False
    assert budget["url"] == f"familybudget://budget/{budget_id}"
    assert budget["data"] == {"budget_id": budget_id, "category": "budget_alert"}
    assert page["items"][1]["url"] == "familybudget://notifications"

    r = await client.get(N, headers=asha.h, params={"category": "budget_alert"})
    assert [n["title"] for n in r.json()["items"]] == ["Budget 75%"]
    r = await client.get(N, headers=asha.h, params={"limit": 1})
    assert r.json()["has_more"] is True and len(r.json()["items"]) == 1
    assert (await client.get(N, headers=asha.h, params={"category": "nope"})).status_code == 422
    assert (await client.get(f"{N}/unread-count", headers=asha.h)).json() == {"count": 2}

    # mark one read (idempotent)
    r = await client.post(f"{N}/{budget['id']}/read", headers=asha.h)
    assert r.status_code == 200 and r.json()["read"] is True and r.json()["read_at"]
    assert (await client.post(f"{N}/{budget['id']}/read", headers=asha.h)).status_code == 200
    assert (await client.get(f"{N}/unread-count", headers=asha.h)).json() == {"count": 1}
    r = await client.get(N, headers=asha.h, params={"unread_only": True})
    assert [n["title"] for n in r.json()["items"]] == ["First"]

    # isolation: bob cannot see, read or delete asha's; nobody sees not-yet-due rows
    first_id = page["items"][1]["id"]
    later = next(n for n in await rows(asha.id) if n.title == "Later")
    assert [n["title"] for n in (await client.get(N, headers=bob.h)).json()["items"]] == ["Bob's"]
    assert (await client.post(f"{N}/{first_id}/read", headers=bob.h)).status_code == 404
    assert (await client.delete(f"{N}/{first_id}", headers=bob.h)).status_code == 404
    assert (await client.post(f"{N}/{later.id}/read", headers=asha.h)).status_code == 404
    assert (await client.post(f"{N}/{uuid.uuid4()}/read", headers=asha.h)).status_code == 404
    assert (await client.get(N)).status_code == 401

    # mark all read only touches my due rows
    r = await client.post(f"{N}/read-all", headers=asha.h)
    assert r.status_code == 200 and r.json() == {"updated": 1}
    assert (await client.get(f"{N}/unread-count", headers=asha.h)).json() == {"count": 0}
    assert (await client.get(f"{N}/unread-count", headers=bob.h)).json() == {"count": 1}
    assert next(n for n in await rows(asha.id) if n.title == "Later").read_at is None

    # delete: gone from the API, but the dedupe key still blocks re-creation
    assert (await client.delete(f"{N}/{budget['id']}", headers=asha.h)).status_code == 204
    assert (await client.delete(f"{N}/{budget['id']}", headers=asha.h)).status_code == 404
    assert [n["title"] for n in (await client.get(N, headers=asha.h)).json()["items"]] == ["First"]
    assert await make(asha.id, NotificationCategory.BUDGET_ALERT, dedupe_key="budget:x:75") is False


async def test_read_all_by_category_and_delete_cancels_pending_push(client):
    asha = await user(client, "asha")
    await make(asha.id, NotificationCategory.BUDGET_ALERT)
    await make(asha.id, NotificationCategory.SYSTEM)
    r = await client.post(f"{N}/read-all", headers=asha.h, params={"category": "system"})
    assert r.json() == {"updated": 1}
    items = (await client.get(N, headers=asha.h)).json()["items"]
    unread = {n["category"]: n for n in items if not n["read"]}
    assert set(unread) == {"budget_alert"}
    assert unread["budget_alert"]["push_status"] == "pending"
    await client.delete(f"{N}/{unread['budget_alert']['id']}", headers=asha.h)
    deleted = next(n for n in await rows(asha.id) if n.category == "budget_alert")
    assert deleted.push_status == PushStatus.SKIPPED and deleted.read_at is not None


# --- preferences -------------------------------------------------------------------------


async def test_preferences_defaults_updates_and_notify_respects_them(client):
    asha, bob = await user(client, "asha"), await user(client, "bob")
    r = await client.get(f"{N}/preferences", headers=asha.h)
    assert r.status_code == 200
    prefs = r.json()
    assert [p["category"] for p in prefs] == [c.value for c in NotificationCategory]
    assert all(p["push_enabled"] and p["in_app_enabled"] for p in prefs)

    await make(asha.id, NotificationCategory.SAVINGS_MILESTONE, dedupe_key="goal:1")
    r = await client.put(
        f"{N}/preferences",
        headers=asha.h,
        json=[
            {"category": "budget_alert", "push_enabled": False, "in_app_enabled": True},
            {"category": "system", "push_enabled": False, "in_app_enabled": False},
            {"category": "savings_milestone", "push_enabled": False, "in_app_enabled": True},
        ],
    )
    assert r.status_code == 200, r.text
    by_cat = {p["category"]: p for p in r.json()}
    assert len(by_cat) == len(NotificationCategory)
    assert by_cat["budget_alert"] == {
        "category": "budget_alert",
        "push_enabled": False,
        "in_app_enabled": True,
    }
    assert by_cat["system"]["in_app_enabled"] is False
    assert by_cat["bill_upcoming"]["push_enabled"] is True
    # a push already queued for a category just muted is not sent any more
    assert (await rows(asha.id))[0].push_status == PushStatus.SKIPPED

    # update one again (upsert)
    r = await client.put(
        f"{N}/preferences",
        headers=asha.h,
        json=[{"category": "budget_alert", "push_enabled": True, "in_app_enabled": True}],
    )
    assert {p["category"]: p for p in r.json()}["budget_alert"]["push_enabled"] is True
    r = await client.put(
        f"{N}/preferences",
        headers=asha.h,
        json=[{"category": "budget_alert", "push_enabled": False, "in_app_enabled": True}],
    )

    # notify() now: push off → stored but skipped; both off → not stored at all
    assert await make(asha.id, NotificationCategory.BUDGET_ALERT, dedupe_key="b:1") is True
    assert await make(asha.id, NotificationCategory.SYSTEM, dedupe_key="s:1") is False
    stored = {n.dedupe_key: n for n in await rows(asha.id)}
    assert stored["b:1"].push_status == PushStatus.SKIPPED and "s:1" not in stored
    # bob's preferences are untouched
    assert all(
        p["push_enabled"] for p in (await client.get(f"{N}/preferences", headers=bob.h)).json()
    )

    for bad in (
        [{"category": "system", "push_enabled": True, "in_app_enabled": True}] * 2,
        [{"category": "nope", "push_enabled": True, "in_app_enabled": True}],
        [{"category": "system", "push_enabled": True}],
        [{"category": "system", "push_enabled": True, "in_app_enabled": True, "x": 1}],
        {"category": "system", "push_enabled": True, "in_app_enabled": True},
    ):
        r = await client.put(f"{N}/preferences", headers=asha.h, json=bad)
        assert r.status_code == 422, bad


# --- devices -----------------------------------------------------------------------------


async def test_device_registration_validation_upsert_rebind_and_delete(client):
    asha_auth = await register_verified(client, email="asha@example.com")
    asha = Actor(asha_auth)
    bob = await user(client, "bob")
    for bad in (
        {"token": "abc", "platform": "ios"},
        {"token": "ExponentPushToken[]", "platform": "ios"},
        {"token": "ExponentPushToken[abc def]", "platform": "ios"},
        {"token": "FCMToken[abc]", "platform": "android"},
        {"token": TOKEN_A, "platform": "web"},
        {"token": TOKEN_A},
        {"token": TOKEN_A, "platform": "ios", "user_id": str(bob.id)},
    ):
        r = await client.post(f"{N}/devices", headers=asha.h, json=bad)
        assert r.status_code == 422, bad
    assert (await client.post(f"{N}/devices", json={"token": TOKEN_A})).status_code == 401

    r = await client.post(
        f"{N}/devices", headers=asha.h, json={"token": TOKEN_A, "platform": "ios"}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["platform"] == "ios" and out["token_hint"] == "ExponentPushToken[…aaaa]"
    assert TOKEN_A not in r.text
    r = await client.post(
        f"{N}/devices", headers=asha.h, json={"token": TOKEN_A, "platform": "android"}
    )
    assert r.json()["id"] == out["id"] and r.json()["platform"] == "android"  # upsert
    devices = await device_rows()
    assert len(devices) == 1 and devices[TOKEN_A].user_id == asha.id
    assert devices[TOKEN_A].session_id is not None
    asha_session = devices[TOKEN_A].session_id

    # a new token from the same session retires the old one
    r = await client.post(
        f"{N}/devices", headers=asha.h, json={"token": TOKEN_B, "platform": "ios"}
    )
    assert r.status_code == 200
    devices = await device_rows()
    assert devices[TOKEN_A].disabled_at is not None and devices[TOKEN_B].disabled_at is None
    # re-registering a disabled token re-enables it (and retires B)
    await client.post(f"{N}/devices", headers=asha.h, json={"token": TOKEN_A, "platform": "ios"})
    devices = await device_rows()
    assert devices[TOKEN_A].disabled_at is None and devices[TOKEN_B].disabled_at is not None

    # the phone signs in as bob: the token is re-bound to bob's user and session
    r = await client.post(f"{N}/devices", headers=bob.h, json={"token": TOKEN_A, "platform": "ios"})
    assert r.status_code == 200 and r.json()["id"] == out["id"]
    devices = await device_rows()
    assert devices[TOKEN_A].user_id == bob.id and devices[TOKEN_A].session_id != asha_session

    # delete: only your own
    path = f"{N}/devices/{quote(TOKEN_A, safe='')}"
    assert (await client.delete(path, headers=asha.h)).status_code == 404
    assert (await client.delete(path, headers=bob.h)).status_code == 204
    assert (await client.delete(path, headers=bob.h)).status_code == 404
    assert (
        await client.delete(f"{N}/devices/{quote(TOKEN_B, safe='')}", headers=asha.h)
    ).status_code == 204
    assert await device_rows() == {}


async def test_disable_token_helpers_for_auth(client):
    first = await register_verified(client, email="asha@example.com")
    second = (await login(client)).json()
    uid = uuid.UUID(first["user"]["id"])
    for auth, token in ((first, TOKEN_A), (second, TOKEN_B)):
        r = await client.post(
            f"{N}/devices", headers=bearer(auth["tokens"]), json={"token": token, "platform": "ios"}
        )
        assert r.status_code == 200
    devices = await device_rows()
    async with get_database().sessionmaker() as s:
        assert await disable_tokens_for_sessions(s, [devices[TOKEN_A].session_id]) == 1
        await s.commit()
    devices = await device_rows()
    assert devices[TOKEN_A].disabled_at is not None and devices[TOKEN_B].disabled_at is None
    async with get_database().sessionmaker() as s:
        n = await disable_tokens_for_user(s, uid, except_session_id=devices[TOKEN_B].session_id)
        assert n == 0
        assert await disable_tokens_for_user(s, uid) == 1
        assert await disable_tokens_for_sessions(s, []) == 0
        await s.commit()
    assert all(d.disabled_at is not None for d in (await device_rows()).values())


# --- family notifications ----------------------------------------------------------------


async def test_family_invitation_and_member_joined_notifications(client):
    owner, admin, bob = (
        await user(client, "owner"),
        await user(client, "admin"),
        await user(client, "bob"),
    )
    fam = (await client.post(f"{API}/families", headers=owner.h, json={"name": "Raos"})).json()

    async def invite(email: str, role: str = "member") -> dict[str, Any]:
        r = await client.post(
            f"{API}/families/{fam['id']}/invitations",
            headers=owner.h,
            json={"email": email, "role": role},
        )
        assert r.status_code == 201, r.text
        return dict(r.json())

    inv = await invite(admin.email, "admin")
    r = await client.post(f"{API}/families/invitations/{inv['id']}/accept", headers=admin.h)
    assert r.status_code == 200, r.text

    invite_notes = [n for n in (await client.get(N, headers=admin.h)).json()["items"]]
    assert [n["category"] for n in invite_notes] == ["family_invitation"]
    note = invite_notes[0]
    assert note["data"]["invitation_id"] == inv["id"]
    assert note["data"]["family_id"] == fam["id"]
    assert note["url"] == f"familybudget://family/invitations/{inv['id']}"
    assert "Owner" in note["body"] and "Raos" in note["body"]
    assert inv["token"] not in str(note)

    joined = (await client.get(N, headers=owner.h)).json()["items"]
    assert [n["category"] for n in joined] == ["family_member_joined"]
    assert joined[0]["url"] == f"familybudget://family/{fam['id']}"
    assert joined[0]["family_id"] == fam["id"] and "Admin" in joined[0]["body"]

    # an unknown address gets only the email; bob joins via link token
    await invite("stranger@example.com")
    inv = await invite(bob.email)
    r = await client.post(
        f"{API}/families/invitations/accept", headers=bob.h, json={"token": inv["token"]}
    )
    assert r.status_code == 200
    async with get_database().sessionmaker() as s:
        count = len(list(await s.scalars(select(Notification))))
    assert count == 5  # admin: invite; owner: joined x2; admin: joined(bob); bob: invite
    for who in (owner, admin):
        cats = [n["category"] for n in (await client.get(N, headers=who.h)).json()["items"]]
        assert cats.count("family_member_joined") == (2 if who is owner else 1)
    bob_cats = [n["category"] for n in (await client.get(N, headers=bob.h)).json()["items"]]
    assert bob_cats == ["family_invitation"]  # never notified about joining himself


# --- unusual spending --------------------------------------------------------------------

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # 17:30 in Asia/Kolkata


def _tx(owner: uuid.UUID, amount: str, at: datetime, **kw: Any) -> Transaction:
    kw.setdefault("type", TransactionType.EXPENSE)
    return Transaction(
        owner_user_id=owner,
        scope=RecordScope.PERSONAL,
        amount=Decimal(amount),
        occurred_at=at,
        **kw,
    )


async def _add(*txs: Transaction) -> None:
    async with get_database().sessionmaker() as s:
        s.add_all(txs)
        await s.commit()


async def _check(user_id: uuid.UUID, now: datetime = NOW) -> bool:
    async with get_database().sessionmaker() as s:
        u = await s.get(User, user_id)
        assert u is not None
        created = await check_unusual_spending(s, u, now)
        await s.commit()
    return created


async def test_unusual_spending_against_baseline(client):
    asha = await user(client, "asha")
    # 28 previous days at 500/day -> average 500 -> threshold max(3 x 500, 1000) = 1500
    await _add(*[_tx(asha.id, "500", NOW - timedelta(days=d)) for d in range(1, 29)])
    # ignored: a pending UPI payment, income, a splurge just outside the 28-day window
    await _add(
        _tx(asha.id, "5000", NOW, status=TransactionStatus.PENDING),
        _tx(asha.id, "5000", NOW, type=TransactionType.INCOME),
        _tx(asha.id, "90000", NOW - timedelta(days=29, hours=8)),
        _tx(asha.id, "1400", NOW - timedelta(hours=1)),
    )
    assert await _check(asha.id) is False  # 1400 <= 1500
    # refunds reduce spending: 1400 + 300 - 200 = 1500, still not above the threshold
    await _add(_tx(asha.id, "300", NOW), _tx(asha.id, "200", NOW, type=TransactionType.REFUND))
    assert await _check(asha.id) is False
    await _add(_tx(asha.id, "1", NOW))
    assert await _check(asha.id) is True  # 1501 > 1500
    assert await _check(asha.id) is False  # once per day
    (note,) = await rows(asha.id)
    assert note.category == NotificationCategory.UNUSUAL_SPENDING
    assert note.dedupe_key == f"unusual:{asha.id}:2026-11-20"
    assert note.data["amount"] == "1501.00" and note.data["daily_average"] == "500.00"
    assert note.data["date"] == "2026-11-20" and note.data["currency"] == "INR"
    assert "₹1,501" in note.body and "₹500" in note.body
    items = (await client.get(N, headers=asha.h)).json()["items"]
    assert items[0]["url"] == "familybudget://transactions?date=2026-11-20"
    # next day, nothing spent yet
    assert await _check(asha.id, NOW + timedelta(days=1)) is False


async def test_unusual_spending_floor_timezones_and_runner(client):
    asha, bob, carol = [await user(client, n) for n in ("asha", "bob", "carol")]
    async with get_database().sessionmaker() as s:
        b = await s.get(User, bob.id)
        c = await s.get(User, carol.id)
        assert b is not None and c is not None
        b.timezone = "America/New_York"  # NOW = 07:00 there
        c.is_active = False
        await s.commit()

    # asha (Asia/Kolkata): 19:00 UTC yesterday is 00:30 today in India
    await _add(_tx(asha.id, "900", datetime(2026, 11, 19, 19, 0, tzinfo=UTC)))
    # bob: 04:00 UTC today is still yesterday (23:00) in New York -> baseline only
    await _add(_tx(bob.id, "1100", datetime(2026, 11, 20, 4, 0, tzinfo=UTC)))
    await _add(_tx(carol.id, "50000", NOW))
    async with get_database().sessionmaker() as s:
        assert await run_unusual_spending_check(s, NOW) == 0  # 900 <= 1000 floor; bob 0 today
        await s.commit()

    await _add(_tx(asha.id, "200", NOW), _tx(bob.id, "2000", NOW - timedelta(hours=1)))
    async with get_database().sessionmaker() as s:
        assert await run_unusual_spending_check(s, NOW) == 2
        await s.commit()
    async with get_database().sessionmaker() as s:
        assert await run_unusual_spending_check(s, NOW) == 0  # idempotent
        await s.commit()
    (bob_note,) = await rows(bob.id)
    assert bob_note.dedupe_key == f"unusual:{bob.id}:2026-11-20"
    assert bob_note.data["amount"] == "2000.00" and bob_note.data["daily_average"] == "39.29"
    assert await rows(carol.id) == []  # inactive users are skipped


async def test_sign_out_paths_disable_push_tokens(client):
    """Logout, revoking a session and logout-all stop pushes to those devices immediately."""
    from sqlalchemy import select as _select

    from app.db.session import get_database as _db
    from app.models import DeviceToken
    from tests.conftest import bearer as _bearer
    from tests.conftest import login as _login
    from tests.conftest import register_verified as _register

    first = await _register(client, email="push-owner@example.com")
    second = (await _login(client, email="push-owner@example.com")).json()
    third = (await _login(client, email="push-owner@example.com")).json()
    tokens = {
        "first": "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]",
        "second": "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]",
        "third": "ExponentPushToken[cccccccccccccccccccccc]",
    }
    for name, auth in (("first", first), ("second", second), ("third", third)):
        r = await client.post(
            "/api/v1/notifications/devices",
            headers=_bearer(auth["tokens"]),
            json={"token": tokens[name], "platform": "android"},
        )
        assert r.status_code == 200, r.text

    async def disabled() -> set[str]:
        async with _db().sessionmaker() as s:
            rows = await s.scalars(_select(DeviceToken).where(DeviceToken.disabled_at.is_not(None)))
            return {t.token for t in rows}

    assert await disabled() == set()
    assert (
        await client.post("/api/v1/auth/logout", headers=_bearer(first["tokens"]))
    ).status_code == 204
    assert await disabled() == {tokens["first"]}

    sessions = (
        await client.get("/api/v1/users/me/sessions", headers=_bearer(third["tokens"]))
    ).json()
    other = next(s for s in sessions if not s["is_current"])
    r = await client.delete(
        f"/api/v1/users/me/sessions/{other['id']}", headers=_bearer(third["tokens"])
    )
    assert r.status_code == 204
    assert await disabled() == {tokens["first"], tokens["second"]}

    r = await client.post("/api/v1/auth/logout-all", headers=_bearer(third["tokens"]))
    assert r.status_code == 204
    assert await disabled() == set(tokens.values())
