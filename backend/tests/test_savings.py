"""Savings goals: balance math, withdrawal guard, auto status, pacing, history, milestones,
permissions and isolation."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.db.session import get_database
from app.models import Notification, NotificationCategory, User
from app.services.savings import SavingsService, months_between, pacing
from tests.test_budgets import API, Actor, add_tx, at, household, set_timezone, user


async def create_goal(client, who: Actor, **body: Any) -> dict[str, Any]:
    payload = {"name": "Laptop", "target_amount": "1000.00", **body}
    r = await client.post(f"{API}/savings", headers=who.h, json=payload)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def contribute(client, who: Actor, goal_id: str, amount: str, kind: str = "deposit", **kw):
    return await client.post(
        f"{API}/savings/{goal_id}/contributions",
        headers=who.h,
        json={"amount": amount, "kind": kind, **kw},
    )


async def milestone_rows(goal_id: str) -> list[tuple[uuid.UUID, str]]:
    async with get_database().sessionmaker() as s:
        rows = await s.execute(
            select(Notification.user_id, Notification.dedupe_key)
            .where(
                Notification.dedupe_key.like(f"goal:{goal_id}:%"),
                Notification.category == NotificationCategory.SAVINGS_MILESTONE,
            )
            .order_by(Notification.dedupe_key, Notification.user_id)
        )
        return [(u, k) for u, k in rows]


async def service_call(who: Actor, fn) -> Any:
    async with get_database().sessionmaker() as s:
        u = await s.get(User, who.uid)
        assert u is not None
        return await fn(SavingsService(s), u)


# --- pure helpers ------------------------------------------------------------------------


def test_pacing_math():
    assert months_between(date(2026, 11, 20), date(2027, 5, 20)) == 6
    assert months_between(date(2026, 11, 20), date(2027, 5, 19)) == 5
    assert months_between(date(2026, 11, 20), date(2026, 12, 1)) == 1  # minimum 1
    p = pacing(date(2027, 5, 20), Decimal("600.00"), date(2026, 11, 20))
    assert p is not None
    assert (p.months_left, str(p.required_monthly), p.overdue, p.days_left) == (
        6,
        "100.00",
        False,
        181,
    )
    # Rounded up so the target is reached.
    p = pacing(date(2027, 2, 20), Decimal("100.00"), date(2026, 11, 20))
    assert p is not None and str(p.required_monthly) == "33.34"
    overdue = pacing(date(2026, 10, 1), Decimal("250.00"), date(2026, 11, 20))
    assert overdue is not None and overdue.overdue and str(overdue.required_monthly) == "250.00"
    assert overdue.days_left == 0
    assert pacing(None, Decimal("1"), date(2026, 11, 20)) is None
    assert pacing(date(2027, 1, 1), Decimal("0"), date(2026, 11, 20)) is None


# --- math, status, guard, milestones -----------------------------------------------------


async def test_balance_status_and_withdrawal_guard(client):
    asha = await user(client, "asha")
    g = await create_goal(client, asha, icon="laptop", color="#22C55E")
    assert (g["current_amount"], g["remaining_amount"], g["progress_percent"], g["status"]) == (
        "0.00",
        "1000.00",
        0.0,
        "active",
    )
    assert g["scope"] == "personal" and g["currency"] == "INR" and g["can_edit"] is True

    r = await contribute(client, asha, g["id"], "300")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["contribution"]["amount"] == "300.00" and body["contribution"]["kind"] == "deposit"
    assert (body["goal"]["current_amount"], body["goal"]["progress_percent"]) == ("300.00", 30.0)

    r = await contribute(client, asha, g["id"], "300.01", kind="withdrawal")
    assert r.status_code == 409 and r.json()["error"]["code"] == "insufficient_goal_balance"
    r = await contribute(client, asha, g["id"], "100", kind="withdrawal")
    assert r.status_code == 201 and r.json()["goal"]["current_amount"] == "200.00"
    withdrawal_id = r.json()["contribution"]["id"]

    r = await contribute(client, asha, g["id"], "800")
    goal = r.json()["goal"]
    assert (goal["status"], goal["current_amount"], goal["progress_percent"]) == (
        "completed",
        "1000.00",
        100.0,
    )
    assert goal["completed_at"] is not None and goal["pacing"] is None
    deposit_800 = r.json()["contribution"]["id"]

    r = await contribute(client, asha, g["id"], "1", kind="withdrawal")  # back below target
    assert r.json()["goal"]["status"] == "active" and r.json()["goal"]["completed_at"] is None
    r = await contribute(client, asha, g["id"], "1")
    assert r.json()["goal"]["status"] == "completed"

    # Deleting the withdrawal raises the balance (over the target is fine).
    r = await client.delete(
        f"{API}/savings/{g['id']}/contributions/{withdrawal_id}", headers=asha.h
    )
    assert r.status_code == 200 and r.json()["current_amount"] == "1100.00"
    assert r.json()["progress_percent"] == 110.0 and r.json()["remaining_amount"] == "0.00"
    # Withdraw most of it; deleting the 800 deposit would then go negative → 409.
    assert (await contribute(client, asha, g["id"], "1000", kind="withdrawal")).status_code == 201
    r = await client.delete(f"{API}/savings/{g['id']}/contributions/{deposit_800}", headers=asha.h)
    assert r.status_code == 409
    r = await client.delete(f"{API}/savings/{g['id']}/contributions/{uuid.uuid4()}", headers=asha.h)
    assert r.status_code == 404

    # Raising the target re-evaluates status.
    r = await client.patch(f"{API}/savings/{g['id']}", headers=asha.h, json={"target_amount": "50"})
    assert r.json()["status"] == "completed"
    r = await client.patch(
        f"{API}/savings/{g['id']}", headers=asha.h, json={"target_amount": "5000"}
    )
    assert r.json()["status"] == "active" and r.json()["progress_percent"] == 2.0

    r = await client.get(f"{API}/savings/{g['id']}/contributions", headers=asha.h)
    page = r.json()
    assert page["total"] == 5 and len(page["items"]) == 5 and page["has_more"] is False  # 1 deleted
    r = await client.get(
        f"{API}/savings/{g['id']}/contributions", headers=asha.h, params={"limit": 2}
    )
    assert len(r.json()["items"]) == 2 and r.json()["has_more"] is True


async def test_milestones_fire_once(client):
    asha = await user(client, "asha")
    g = await create_goal(client, asha)
    await contribute(client, asha, g["id"], "260")
    assert [k for _, k in await milestone_rows(g["id"])] == [f"goal:{g['id']}:25"]
    r = await contribute(client, asha, g["id"], "500")  # 76 %
    assert {k.rsplit(":", 1)[1] for _, k in await milestone_rows(g["id"])} == {"25", "50", "75"}
    # Down and back up again: no repeats.
    await contribute(client, asha, g["id"], "700", kind="withdrawal")
    await contribute(client, asha, g["id"], "700")
    r = await contribute(client, asha, g["id"], "240")  # exactly 100 %
    assert r.json()["goal"]["status"] == "completed"
    rows = await milestone_rows(g["id"])
    assert sorted(k.rsplit(":", 1)[1] for _, k in rows) == ["100", "25", "50", "75"]
    assert {u for u, _ in rows} == {asha.uid}
    async with get_database().sessionmaker() as s:
        n = await s.scalar(
            select(Notification).where(Notification.dedupe_key == f"goal:{g['id']}:100")
        )
        assert n is not None and n.title == "Goal reached: Laptop" and n.data["milestone"] == 100


async def test_family_goal_milestones_notify_all_members(client):
    fid, p = await household(client)
    g = await create_goal(client, p["member"], scope="family", family_id=fid, target_amount="100")
    assert g["scope"] == "family" and g["owner_user_id"] == p["member"].id
    r = await contribute(client, p["viewer"], g["id"], "50")
    assert r.status_code == 403
    assert (await contribute(client, p["admin"], g["id"], "50")).status_code == 201
    rows = await milestone_rows(g["id"])
    assert {u for u, _ in rows} == {a.uid for a in p.values()}
    assert {k.rsplit(":", 1)[1] for _, k in rows} == {"25", "50"}
    async with get_database().sessionmaker() as s:
        fam_ids = set(
            await s.scalars(
                select(Notification.family_id).where(Notification.dedupe_key.like("goal:%"))
            )
        )
    assert fam_ids == {uuid.UUID(fid)}


async def test_pacing_and_history(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    g = await create_goal(client, asha, target_amount="1200", target_date="2027-05-20")
    gid = uuid.UUID(g["id"])
    for amount, kind, when in (
        ("100", "deposit", "2026-08-31T23:59:00Z"),
        ("200", "deposit", "2026-08-01T00:00:00Z"),
        ("50", "withdrawal", "2026-08-15T00:00:00Z"),
        ("400", "deposit", "2026-10-03T00:00:00Z"),
        ("50", "deposit", "2026-09-01T00:00:00Z"),
    ):
        r = await contribute(client, asha, g["id"], amount, kind=kind, contributed_at=when)
        assert r.status_code == 201, r.text
    # Listed newest first.
    r = await client.get(f"{API}/savings/{g['id']}/contributions", headers=asha.h)
    assert [c["amount"] for c in r.json()["items"]] == [
        "400.00",
        "50.00",
        "100.00",
        "50.00",
        "200.00",
    ]

    now = at("2026-11-20T10:00:00Z")
    goal = await service_call(asha, lambda svc, u: svc.get_goal(u, gid, now))
    assert str(goal.current_amount) == "700.00"
    assert goal.pacing is not None
    assert (goal.pacing.months_left, str(goal.pacing.required_monthly)) == (6, "83.34")

    hist = await service_call(asha, lambda svc, u: svc.history(u, gid, now))
    assert [(i.month, str(i.deposits), str(i.withdrawals), str(i.balance)) for i in hist.items] == [
        ("2026-08", "300.00", "50.00", "250.00"),
        ("2026-09", "50.00", "0.00", "300.00"),
        ("2026-10", "400.00", "0.00", "700.00"),
        ("2026-11", "0.00", "0.00", "700.00"),
    ]
    # Months are bucketed in the owner's timezone: 31 Aug 23:59Z is September in IST.
    await set_timezone(asha, "Asia/Kolkata")
    hist = await service_call(asha, lambda svc, u: svc.history(u, gid, now))
    assert [(i.month, str(i.deposits)) for i in hist.items][:2] == [
        ("2026-08", "200.00"),
        ("2026-09", "150.00"),
    ]
    r = await client.get(f"{API}/savings/{g['id']}/history", headers=asha.h)
    assert r.status_code == 200 and r.json()["items"][0]["balance"] == "150.00"  # IST buckets

    empty = await create_goal(client, asha, name="Empty")
    r = await client.get(f"{API}/savings/{empty['id']}/history", headers=asha.h)
    assert r.json() == {"goal_id": empty["id"], "items": []}


async def test_validation_archive_and_transaction_link(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    for bad in (
        {"target_amount": "0"},
        {"target_amount": "-1"},
        {"color": "red"},
        {"name": ""},
        {"scope": "family"},
        {"family_id": str(uuid.uuid4())},
        {"extra": 1},
    ):
        r = await client.post(
            f"{API}/savings", headers=asha.h, json={"name": "x", "target_amount": "1", **bad}
        )
        assert r.status_code == 422, bad
    g = await create_goal(client, asha)
    for bad in ({"status": "completed"}, {"name": None}, {"target_amount": None}):
        r = await client.patch(f"{API}/savings/{g['id']}", headers=asha.h, json=bad)
        assert r.status_code == 422, bad
    r = await contribute(client, asha, g["id"], "10", contributed_at="2030-01-01T00:00:00Z")
    assert r.status_code == 422
    r = await contribute(client, asha, g["id"], "0")
    assert r.status_code == 422

    # transaction_id must be visible to the caller.
    mine = await add_tx(asha, "10.00", at("2026-11-10T00:00:00Z"))
    theirs = await add_tx(bob, "10.00", at("2026-11-10T00:00:00Z"))
    r = await contribute(client, asha, g["id"], "10", transaction_id=str(theirs))
    assert r.status_code == 404
    r = await contribute(client, asha, g["id"], "10", transaction_id=str(mine))
    assert r.status_code == 201 and r.json()["contribution"]["transaction_id"] == str(mine)

    # Archived goals are frozen; un-archiving restores the automatic status.
    r = await client.patch(f"{API}/savings/{g['id']}", headers=asha.h, json={"status": "archived"})
    assert r.json()["status"] == "archived" and r.json()["pacing"] is None
    assert (await contribute(client, asha, g["id"], "5")).status_code == 409
    r = await client.get(f"{API}/savings", headers=asha.h, params={"status": "archived"})
    assert [x["id"] for x in r.json()] == [g["id"]]
    r = await client.patch(f"{API}/savings/{g['id']}", headers=asha.h, json={"status": "active"})
    assert r.json()["status"] == "active"
    r = await client.patch(
        f"{API}/savings/{g['id']}", headers=asha.h, json={"target_date": None, "icon": "car"}
    )
    assert r.status_code == 200 and r.json()["icon"] == "car"

    assert (await client.delete(f"{API}/savings/{g['id']}", headers=asha.h)).status_code == 204
    assert (await client.get(f"{API}/savings/{g['id']}", headers=asha.h)).status_code == 404
    assert (await client.get(f"{API}/savings", headers=asha.h)).json() == []


# --- permissions & isolation -------------------------------------------------------------


async def test_family_goal_permissions(client):
    fid, p = await household(client)
    body = {"name": "Vacation", "target_amount": "5000", "scope": "family", "family_id": fid}
    codes = {}
    for role in ("owner", "admin", "member", "viewer"):
        r = await client.post(f"{API}/savings", headers=p[role].h, json=body)
        codes[role] = r.status_code
    assert codes == {"owner": 201, "admin": 201, "member": 201, "viewer": 403}
    goals = (
        await client.get(f"{API}/savings", headers=p["viewer"].h, params={"family_id": fid})
    ).json()
    assert len(goals) == 3 and all(g["can_edit"] is False for g in goals)
    by_owner = {g["owner_user_id"]: g for g in goals}
    admin_goal = by_owner[p["admin"].id]["id"]
    member_goal = by_owner[p["member"].id]["id"]
    assert by_owner[p["member"].id]["currency"] == "INR"

    # Members edit only their own goals; owner/admin edit any; viewers none.
    patch = {"name": "Renamed"}
    assert (
        await client.patch(f"{API}/savings/{member_goal}", headers=p["member"].h, json=patch)
    ).status_code == 200
    assert (
        await client.patch(f"{API}/savings/{admin_goal}", headers=p["member"].h, json=patch)
    ).status_code == 403
    assert (
        await client.patch(f"{API}/savings/{member_goal}", headers=p["admin"].h, json=patch)
    ).status_code == 200
    assert (
        await client.patch(f"{API}/savings/{member_goal}", headers=p["viewer"].h, json=patch)
    ).status_code == 403
    assert (
        await client.delete(f"{API}/savings/{admin_goal}", headers=p["member"].h)
    ).status_code == 403

    # Anyone who can create records may contribute to any family goal.
    r = await contribute(client, p["member"], admin_goal, "100")
    assert r.status_code == 201
    member_c = r.json()["contribution"]["id"]
    r = await contribute(client, p["admin"], admin_goal, "100")
    admin_c = r.json()["contribution"]["id"]
    assert (await contribute(client, p["viewer"], admin_goal, "1")).status_code == 403
    # Contribution deletion: own (member) or any (owner/admin).
    url = f"{API}/savings/{admin_goal}/contributions"
    assert (await client.delete(f"{url}/{admin_c}", headers=p["member"].h)).status_code == 403
    assert (await client.delete(f"{url}/{member_c}", headers=p["viewer"].h)).status_code == 403
    assert (await client.delete(f"{url}/{member_c}", headers=p["member"].h)).status_code == 200
    assert (await client.delete(f"{url}/{admin_c}", headers=p["owner"].h)).status_code == 200
    r = await client.get(url, headers=p["viewer"].h)
    assert r.status_code == 200 and r.json()["total"] == 0

    assert (
        await client.delete(f"{API}/savings/{member_goal}", headers=p["admin"].h)
    ).status_code == 204

    async with get_database().sessionmaker() as s:
        from app.models import AuditLog

        actions = list(
            await s.scalars(select(AuditLog.action).where(AuditLog.family_id == uuid.UUID(fid)))
        )
    assert actions.count("savings_goal.created") == 3
    assert actions.count("savings_contribution.created") == 2
    assert actions.count("savings_contribution.deleted") == 2
    assert "savings_goal.deleted" in actions


async def test_savings_isolation(client):
    a = await user(client, "alice")
    b = await user(client, "bob")
    private = await create_goal(client, b, name="Bob private")
    await contribute(client, b, private["id"], "10")
    fid_b, pb = await household(client, prefix="b")
    fam = await create_goal(client, pb["owner"], scope="family", family_id=fid_b)
    for gid in (private["id"], fam["id"]):
        assert (await client.get(f"{API}/savings/{gid}", headers=a.h)).status_code == 404
        assert (
            await client.patch(f"{API}/savings/{gid}", headers=a.h, json={"name": "x"})
        ).status_code == 404
        assert (await client.delete(f"{API}/savings/{gid}", headers=a.h)).status_code == 404
        assert (await client.get(f"{API}/savings/{gid}/history", headers=a.h)).status_code == 404
        assert (
            await client.get(f"{API}/savings/{gid}/contributions", headers=a.h)
        ).status_code == 404
        assert (await contribute(client, a, gid, "1")).status_code == 404
    r = await client.get(f"{API}/savings", headers=a.h, params={"family_id": fid_b})
    assert r.status_code == 404
    r = await client.post(
        f"{API}/savings",
        headers=a.h,
        json={"name": "x", "target_amount": "1", "scope": "family", "family_id": fid_b},
    )
    assert r.status_code == 404
    assert (await client.get(f"{API}/savings", headers=a.h)).json() == []
    # Family members don't see a member's personal goal.
    assert (
        await client.get(f"{API}/savings/{private['id']}", headers=pb["owner"].h)
    ).status_code == 404
    assert (await client.get(f"{API}/savings")).status_code == 401
