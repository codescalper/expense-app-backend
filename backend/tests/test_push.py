"""Expo push sender (app/services/push.py) against httpx.MockTransport — never the network."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select, update

from app.core.security import utcnow
from app.db.session import get_database
from app.models import DeviceToken, Notification, NotificationCategory, PushStatus
from app.repositories.notifications import PUSH_ATTEMPTS_KEY
from app.services.notify import notify
from app.services.push import PushRunResult, deep_link, mask_token, send_due_pushes
from tests.conftest import bearer, login, register_verified

API = "/api/v1"


class Expo:
    """Records each push/send request; ``reply`` builds the response for a list of messages."""

    def __init__(self, reply: Callable[[list[dict[str, Any]]], httpx.Response] | None = None):
        self.requests: list[list[dict[str, Any]]] = []
        self.headers: list[httpx.Headers] = []
        self.reply = reply or (lambda msgs: ok_tickets(msgs))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://exp.host/--/api/v2/push/send"
        messages = json.loads(request.content)
        self.requests.append(messages)
        self.headers.append(request.headers)
        return self.reply(messages)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [m for batch in self.requests for m in batch]


def ok_tickets(messages: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200, json={"data": [{"status": "ok", "id": uuid.uuid4().hex} for _ in messages]}
    )


def tickets_by_token(errors: dict[str, str]) -> Callable[[list[dict[str, Any]]], httpx.Response]:
    """``errors`` maps token → Expo error code; other tokens succeed."""

    def reply(messages: list[dict[str, Any]]) -> httpx.Response:
        data = []
        for m in messages:
            code = errors.get(m["to"])
            if code is None:
                data.append({"status": "ok", "id": uuid.uuid4().hex})
            else:
                data.append(
                    {
                        "status": "error",
                        "message": f'"{m["to"]}" failed',
                        "details": {"error": code},
                    }
                )
        return httpx.Response(200, json={"data": data})

    return reply


def tok(n: int) -> str:
    return f"ExponentPushToken[device{n:04d}xxxxxxxxxxxxxx]"


async def signed_in(
    client, email: str = "asha@example.com", sessions: int = 1
) -> tuple[uuid.UUID, list[dict[str, str]]]:
    """Verified user with ``sessions`` signed-in sessions → (user_id, headers per session)."""
    auth = await register_verified(client, email=email)
    headers = [bearer(auth["tokens"])]
    for _ in range(sessions - 1):
        r = await login(client, email)
        assert r.status_code == 200, r.text
        headers.append(bearer(r.json()["tokens"]))
    return uuid.UUID(auth["user"]["id"]), headers


async def register(client, headers: dict[str, str], token: str, platform: str = "android") -> None:
    r = await client.post(
        f"{API}/notifications/devices", headers=headers, json={"token": token, "platform": platform}
    )
    assert r.status_code == 200, r.text


async def make(user_id: uuid.UUID, n: int = 1, **kw: Any) -> None:
    kw.setdefault("category", NotificationCategory.BUDGET_ALERT)
    async with get_database().sessionmaker() as s:
        for i in range(n):
            await notify(
                s,
                user_id=user_id,
                title=kw.get("title", f"Budget alert {i}"),
                body="You have used 75% of Groceries",
                dedupe_key=f"{uuid.uuid4()}",
                **{k: v for k, v in kw.items() if k != "title"},
            )
        await s.commit()


async def notifications() -> list[Notification]:
    async with get_database().sessionmaker() as s:
        return list(await s.scalars(select(Notification).order_by(Notification.created_at)))


async def devices() -> dict[str, DeviceToken]:
    async with get_database().sessionmaker() as s:
        return {d.token: d for d in await s.scalars(select(DeviceToken))}


async def run(settings, expo: Expo) -> PushRunResult:
    return await send_due_pushes(get_database(), settings, transport=expo.transport)


# --- tests ---------------------------------------------------------------------------------


async def test_success_marks_sent_with_payload(client, settings):
    uid, (h,) = await signed_in(client)
    await register(client, h, tok(1))
    budget_id = str(uuid.uuid4())
    await make(uid, data={"budget_id": budget_id})
    await make(uid, title="Later", scheduled_for=utcnow() + timedelta(hours=1))
    expo = Expo()
    result = await run(settings, expo)
    assert result.claimed == 1 and result.sent == 1 and result.requests == 1
    (message,) = expo.messages
    note = next(n for n in await notifications() if n.title != "Later")
    assert message == {
        "to": tok(1),
        "title": "Budget alert 0",
        "body": "You have used 75% of Groceries",
        "data": {
            "budget_id": budget_id,
            "category": "budget_alert",
            "notification_id": str(note.id),
            "url": f"familybudget://budget/{budget_id}",
        },
        "sound": "default",
        "channelId": "default",
        "priority": "high",
    }
    assert "authorization" not in expo.headers[0]
    assert note.push_status == PushStatus.SENT and note.sent_at is not None
    later = next(n for n in await notifications() if n.title == "Later")
    assert later.push_status == PushStatus.PENDING  # not due yet

    # nothing left to do → no request at all
    expo2 = Expo()
    assert (await run(settings, expo2)).claimed == 0 and expo2.requests == []


async def test_access_token_header(client, settings):
    from pydantic import SecretStr

    uid, (h,) = await signed_in(client)
    await register(client, h, tok(1))
    await make(uid)
    settings.expo_access_token = SecretStr("expo-secret")
    expo = Expo()
    await run(settings, expo)
    assert expo.headers[0]["authorization"] == "Bearer expo-secret"


async def test_device_not_registered_disables_token(client, settings):
    uid, (h1, h2) = await signed_in(client, sessions=2)
    await register(client, h1, tok(1))
    await register(client, h2, tok(2))
    await make(uid)
    expo = Expo(tickets_by_token({tok(2): "DeviceNotRegistered"}))
    result = await run(settings, expo)
    assert result.sent == 1 and result.devices_disabled == 1
    assert len(expo.messages) == 2
    (note,) = await notifications()
    assert note.push_status == PushStatus.SENT  # delivered to the other device
    d = await devices()
    assert d[tok(1)].disabled_at is None and d[tok(2)].disabled_at is not None

    # next notification only goes to the remaining device; if that one is gone too → skipped
    await make(uid, title="Second")
    expo = Expo(tickets_by_token({tok(1): "DeviceNotRegistered"}))
    result = await run(settings, expo)
    assert [m["to"] for m in expo.messages] == [tok(1)]
    assert result.skipped == 1
    second = next(n for n in await notifications() if n.title == "Second")
    assert second.push_status == PushStatus.SKIPPED
    assert all(x.disabled_at is not None for x in (await devices()).values())


async def test_other_errors_mark_failed(client, settings):
    uid, (h,) = await signed_in(client)
    await register(client, h, tok(1))
    await make(uid)
    result = await run(settings, Expo(tickets_by_token({tok(1): "MessageRateExceeded"})))
    assert result.failed == 1
    (note,) = await notifications()
    assert note.push_status == PushStatus.FAILED and note.sent_at is None
    assert (await devices())[tok(1)].disabled_at is None  # token itself is fine

    # a request Expo rejects outright (4xx) fails its notifications too, without retrying
    await make(uid, title="Rejected")
    expo = Expo(
        lambda _m: httpx.Response(
            400, json={"errors": [{"code": "VALIDATION_ERROR", "message": "x"}]}
        )
    )
    assert (await run(settings, expo)).failed == 1
    rejected = next(n for n in await notifications() if n.title == "Rejected")
    assert rejected.push_status == PushStatus.FAILED


async def test_no_tokens_skipped_without_request(client, settings):
    uid, _ = await signed_in(client)
    await make(uid)
    expo = Expo()
    result = await run(settings, expo)
    assert result.skipped == 1 and expo.requests == []
    (note,) = await notifications()
    assert note.push_status == PushStatus.SKIPPED


async def test_revoked_or_expired_session_token_disabled(client, settings):
    uid, (h1, h2, h3) = await signed_in(client, sessions=3)
    await register(client, h1, tok(1))
    await register(client, h2, tok(2))
    await register(client, h3, tok(3))
    assert (await client.post(f"{API}/auth/logout", headers=h1)).status_code == 204
    # logout disables its own device token immediately (no wait for the sender)
    assert (await devices())[tok(1)].disabled_at is not None
    async with get_database().sessionmaker() as s:  # session 3 simply expired
        from app.models import UserSession

        sid3 = (await devices())[tok(3)].session_id
        await s.execute(
            update(UserSession)
            .where(UserSession.id == sid3)
            .values(expires_at=utcnow() - timedelta(minutes=1))
        )
        await s.commit()
    await make(uid)
    expo = Expo()
    result = await run(settings, expo)
    assert [m["to"] for m in expo.messages] == [tok(2)]
    # only the expired session's token is left for the sender to disable
    assert result.sent == 1 and result.devices_disabled == 1
    d = await devices()
    assert d[tok(1)].disabled_at is not None and d[tok(3)].disabled_at is not None
    assert d[tok(2)].disabled_at is None

    # once every session is gone, notifications are skipped and nothing is sent
    await client.post(f"{API}/auth/logout", headers=h2)
    await make(uid, title="After logout")
    expo = Expo()
    result = await run(settings, expo)
    assert expo.requests == [] and result.skipped == 1


async def test_batches_and_request_chunks(client, settings):
    uid, (h1, h2) = await signed_in(client, sessions=2)
    other, (oh,) = await signed_in(client, email="bob@example.com")
    await register(client, h1, tok(1))
    await register(client, h2, tok(2))
    await register(client, oh, tok(3))
    await make(uid, 120)
    await make(other, 5)
    expo = Expo()
    result = await run(settings, expo)
    assert result.claimed == 125 and result.sent == 125
    assert all(len(batch) <= 100 for batch in expo.requests)
    assert len(expo.messages) == 120 * 2 + 5
    # 2 claimed batches (100 + 25 notifications, ~2 messages each) → 3 requests
    assert len(expo.requests) == 3
    assert {n.push_status for n in await notifications()} == {PushStatus.SENT}
    # every message is addressed to its own user's devices
    ids = {str(n.id): n.user_id for n in await notifications()}
    for m in expo.messages:
        expected = {tok(1), tok(2)} if ids[m["data"]["notification_id"]] == uid else {tok(3)}
        assert m["to"] in expected


async def test_push_disabled_marks_skipped(client, settings):
    uid, (h,) = await signed_in(client)
    await register(client, h, tok(1))
    await make(uid, 3)
    settings.push_enabled = False
    expo = Expo()
    result = await run(settings, expo)
    assert result.skipped == 3 and expo.requests == []
    assert {n.push_status for n in await notifications()} == {PushStatus.SKIPPED}


async def test_network_failure_retries_then_gives_up(client, settings):
    uid, (h,) = await signed_in(client)
    await register(client, h, tok(1))
    await make(uid, title="Fresh")

    def boom(_m: list[dict[str, Any]]) -> httpx.Response:
        raise httpx.ConnectError("down")

    # a fresh notification stays pending however often the network fails
    for attempt in range(1, 5):
        result = await run(settings, Expo(boom))
        assert result.retrying == 1 and result.failed == 0
        (note,) = await notifications()
        assert note.push_status == PushStatus.PENDING
        assert note.data[PUSH_ATTEMPTS_KEY] == attempt
    # the attempt counter never leaks to the API
    items = (await client.get(f"{API}/notifications", headers=h)).json()["items"]
    assert PUSH_ATTEMPTS_KEY not in items[0]["data"]

    # 5xx/429 are transient too; it recovers when Expo is back
    assert (await run(settings, Expo(lambda _m: httpx.Response(503)))).retrying == 1
    assert (await run(settings, Expo())).sent == 1

    # an old notification fails after the 3rd consecutive failure
    await make(uid, title="Old", scheduled_for=utcnow() - timedelta(hours=2))
    outcomes = [await run(settings, Expo(boom)) for _ in range(3)]
    assert [r.retrying for r in outcomes] == [1, 1, 0] and outcomes[-1].failed == 1
    old = next(n for n in await notifications() if n.title == "Old")
    assert old.push_status == PushStatus.FAILED


async def test_inactive_user_gets_no_push(client, settings):
    uid, (h,) = await signed_in(client)
    await register(client, h, tok(1))
    await make(uid)
    async with get_database().sessionmaker() as s:
        from app.models import User

        await s.execute(update(User).where(User.id == uid).values(is_active=False))
        await s.commit()
    expo = Expo()
    assert (await run(settings, expo)).skipped == 1 and expo.requests == []


def test_deep_links_and_masking():
    i = str(uuid.uuid4())
    c = NotificationCategory
    assert deep_link(c.BUDGET_ALERT, {}) == "familybudget://budgets"
    assert deep_link(c.BILL_UPCOMING, {"recurring_id": i}) == f"familybudget://recurring/{i}"
    assert deep_link(c.RECURRING_REMINDER, {}) == "familybudget://recurring"
    assert deep_link(c.UPI_PENDING, {"upi_attempt_id": i}) == f"familybudget://upi/{i}"
    assert (
        deep_link(c.PAYMENT_CONFIRMATION, {"transaction_id": i})
        == f"familybudget://transaction/{i}"
    )
    assert deep_link(c.FAMILY_INVITATION, {}) == "familybudget://family/invitations"
    fam = uuid.uuid4()
    assert deep_link(c.FAMILY_MEMBER_JOINED, {}, family_id=fam) == f"familybudget://family/{fam}"
    assert deep_link(c.FAMILY_SHARED_EXPENSE, {"transaction_id": i}).endswith(f"/{i}")
    assert deep_link(c.SAVINGS_MILESTONE, {"goal_id": i}) == f"familybudget://savings/{i}"
    assert deep_link(c.UNUSUAL_SPENDING, {"date": "bad"}) == "familybudget://transactions"
    assert deep_link(c.SYSTEM, {}) == "familybudget://notifications"
    # producer-supplied links must use the app scheme; injected ids fall back to the list
    assert deep_link(c.SYSTEM, {"url": "familybudget://reports"}) == "familybudget://reports"
    assert deep_link(c.SYSTEM, {"url": "https://evil.example"}) == "familybudget://notifications"
    assert deep_link(c.BUDGET_ALERT, {"budget_id": "../x?y"}) == "familybudget://budgets"
    assert mask_token(tok(7)) == "ExponentPushToken[…xxxx]"
