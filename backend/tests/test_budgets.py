"""Budgets: period math, live spending rules, carry-forward, alerts, permissions, isolation.

Transactions are inserted directly through the model (the transactions API is built
separately). Service-level calls pass an explicit ``now`` so period boundaries are
deterministic.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update

from app.db.session import get_database
from app.models import (
    BudgetPeriod,
    CategoryKind,
    Notification,
    NotificationCategory,
    NotificationPreference,
    RecordScope,
    Transaction,
    TransactionCategory,
    TransactionStatus,
    TransactionType,
    User,
)
from app.services.budgets import (
    BudgetService,
    Period,
    current_period,
    period_containing,
    zone,
)
from app.services.notify import notify
from tests.conftest import BUILTIN_CATEGORIES, bearer, register_verified

API = "/api/v1"


def builtin(name: str) -> str:
    return next(c["id"] for c in BUILTIN_CATEGORIES if c["name"] == name)


FOOD = builtin("Food & Dining")
GROCERIES = builtin("Groceries")
RENT = builtin("Rent")
INCOME_CAT = next(c["id"] for c in BUILTIN_CATEGORIES if c["kind"] == "income")


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


# --- shared helpers (also used by test_savings.py) ---------------------------------------


class Actor:
    def __init__(self, auth: dict[str, Any]) -> None:
        self.id = auth["user"]["id"]
        self.uid = uuid.UUID(self.id)
        self.email = auth["user"]["email"]
        self.h = bearer(auth["tokens"])


async def user(client, name: str) -> Actor:
    return Actor(
        await register_verified(client, email=f"{name}@example.com", full_name=name.title())
    )


async def create_family(client, owner: Actor, name: str = "Rao Family") -> str:
    r = await client.post(f"{API}/families", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def join(client, owner: Actor, family_id: str, who: Actor, role: str) -> None:
    r = await client.post(
        f"{API}/families/{family_id}/invitations",
        headers=owner.h,
        json={"email": who.email, "role": role},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/families/invitations/accept", headers=who.h, json={"token": r.json()["token"]}
    )
    assert r.status_code == 200, r.text


async def household(client, prefix: str = "") -> tuple[str, dict[str, Actor]]:
    """A family with one user per role (owner, admin, member, viewer)."""
    people = {r: await user(client, f"{prefix}{r}") for r in ("owner", "admin", "member", "viewer")}
    fid = await create_family(client, people["owner"], name=f"{prefix or 'Rao'} Family")
    for role in ("admin", "member", "viewer"):
        await join(client, people["owner"], fid, people[role], role)
    return fid, people


async def add_tx(
    owner: Actor,
    amount: str,
    occurred_at: datetime,
    *,
    type_: TransactionType = TransactionType.EXPENSE,
    family_id: str | None = None,
    category_id: str | None = None,
    status: TransactionStatus = TransactionStatus.COMPLETED,
    **kw: Any,
) -> uuid.UUID:
    async with get_database().sessionmaker() as s:
        tx = Transaction(
            owner_user_id=owner.uid,
            scope=RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
            family_id=uuid.UUID(family_id) if family_id else None,
            type=type_,
            status=status,
            amount=Decimal(amount),
            occurred_at=occurred_at,
            category_id=uuid.UUID(category_id) if category_id else None,
            adjustment_sign=1 if type_ is TransactionType.ADJUSTMENT else None,
            **kw,
        )
        s.add(tx)
        await s.commit()
        return tx.id


async def mutate_tx(tx_id: uuid.UUID, **values: Any) -> None:
    async with get_database().sessionmaker() as s:
        await s.execute(update(Transaction).where(Transaction.id == tx_id).values(**values))
        await s.commit()


async def set_timezone(who: Actor, tz: str) -> None:
    async with get_database().sessionmaker() as s:
        await s.execute(update(User).where(User.id == who.uid).values(timezone=tz))
        await s.commit()


async def add_category(owner: Actor, name: str, parent: str | None = None, **kw: Any) -> str:
    async with get_database().sessionmaker() as s:
        cat = TransactionCategory(
            owner_user_id=kw.pop("owner_user_id", owner.uid),
            name=name,
            kind=kw.pop("kind", CategoryKind.EXPENSE),
            parent_id=uuid.UUID(parent) if parent else None,
            **kw,
        )
        s.add(cat)
        await s.commit()
        return str(cat.id)


async def with_service(who: Actor, fn: Callable[[BudgetService, User], Awaitable[Any]]) -> Any:
    async with get_database().sessionmaker() as s:
        u = await s.get(User, who.uid)
        assert u is not None
        return await fn(BudgetService(s), u)


async def budget_at(who: Actor, budget_id: str, now: datetime) -> dict[str, Any]:
    out = await with_service(who, lambda svc, u: svc.get_budget(u, uuid.UUID(budget_id), now))
    return dict(out.model_dump(mode="json"))


async def create_budget(client, who: Actor, **body: Any) -> dict[str, Any]:
    payload = {"name": "Monthly", "amount": "1000.00", "start_date": "2026-01-01", **body}
    r = await client.post(f"{API}/budgets", headers=who.h, json=payload)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def notifications(dedupe_prefix: str) -> list[tuple[uuid.UUID, str]]:
    async with get_database().sessionmaker() as s:
        rows = await s.execute(
            select(Notification.user_id, Notification.dedupe_key)
            .where(Notification.dedupe_key.like(f"{dedupe_prefix}%"))
            .order_by(Notification.dedupe_key, Notification.user_id)
        )
        return [(u, k) for u, k in rows]


async def evaluate(now: datetime) -> int:
    async with get_database().sessionmaker() as s:
        return await BudgetService.evaluate_alerts(s, now)


# --- pure period math --------------------------------------------------------------------


def test_month_and_week_periods():
    m = period_containing(BudgetPeriod.MONTHLY, date(2026, 12, 31))
    assert (m.key, m.start, m.end) == ("2026-12", date(2026, 12, 1), date(2026, 12, 31))
    feb = period_containing(BudgetPeriod.MONTHLY, date(2028, 2, 10))
    assert feb.end == date(2028, 2, 29)  # leap year

    # Weeks are Monday..Sunday; ISO week-year at the year boundary.
    w = period_containing(BudgetPeriod.WEEKLY, date(2027, 1, 1))  # a Friday
    assert (w.start, w.end, w.key) == (date(2026, 12, 28), date(2027, 1, 3), "2026-W53")
    sunday = period_containing(BudgetPeriod.WEEKLY, date(2026, 11, 22))
    monday = period_containing(BudgetPeriod.WEEKLY, date(2026, 11, 23))
    assert sunday.start == date(2026, 11, 16) and monday.start == date(2026, 11, 23)
    assert sunday.key == "2026-W47" and monday.key == "2026-W48"


def test_utc_ranges_follow_the_owner_timezone():
    nov = Period("2026-11", date(2026, 11, 1), date(2026, 11, 30))
    assert nov.utc_range(zone("Asia/Kolkata")) == (
        at("2026-10-31T18:30:00Z"),
        at("2026-11-30T18:30:00Z"),
    )
    assert nov.utc_range(zone("UTC")) == (at("2026-11-01T00:00:00Z"), at("2026-12-01T00:00:00Z"))
    # DST zone: local midnight is converted with the right offset on each side.
    mar = Period("2026-03", date(2026, 3, 1), date(2026, 3, 31))
    assert mar.utc_range(zone("Europe/London")) == (
        at("2026-03-01T00:00:00Z"),
        at("2026-03-31T23:00:00Z"),
    )
    assert zone("Not/AZone").key == "UTC"


def test_current_period_states():
    class B:  # minimal stand-in for the model
        def __init__(self, period, start, end=None):
            self.period, self.start_date, self.end_date = period, start, end

    monthly = B(BudgetPeriod.MONTHLY, date(2026, 11, 15), date(2027, 1, 10))
    assert current_period(monthly, date(2026, 11, 2)) == (  # same month as start → current
        Period("2026-11", date(2026, 11, 1), date(2026, 11, 30)),
        "current",
    )
    assert current_period(monthly, date(2026, 10, 31))[1] == "upcoming"
    assert current_period(monthly, date(2027, 1, 31))[0].key == "2027-01"
    p, state = current_period(monthly, date(2027, 2, 1))
    assert (p.key, state) == ("2027-01", "ended")

    custom = B(BudgetPeriod.CUSTOM, date(2026, 11, 10), date(2026, 11, 20))
    assert current_period(custom, date(2026, 11, 9))[1] == "upcoming"
    assert current_period(custom, date(2026, 11, 20))[1] == "current"
    assert current_period(custom, date(2026, 11, 21))[1] == "ended"


# --- timezone boundaries -----------------------------------------------------------------


async def test_month_boundary_in_owner_timezone(client):
    asha = await user(client, "asha")  # default timezone Asia/Kolkata
    b = await create_budget(client, asha)
    # 18:29:59Z on 30 Nov = 23:59:59 IST (November); 18:30Z = 00:00 IST on 1 Dec.
    await add_tx(asha, "100.00", at("2026-11-30T18:29:59Z"))
    await add_tx(asha, "40.00", at("2026-11-30T18:30:00Z"))

    nov = await budget_at(asha, b["id"], at("2026-11-30T18:00:00Z"))
    assert nov["current"]["period_key"] == "2026-11" and nov["current"]["spent"] == "100.00"
    dec = await budget_at(asha, b["id"], at("2026-11-30T18:30:00Z"))
    assert dec["current"]["period_key"] == "2026-12" and dec["current"]["spent"] == "40.00"
    assert dec["current"]["days_left"] == 31

    # The same rows for a UTC owner both fall in November.
    await set_timezone(asha, "UTC")
    utc = await budget_at(asha, b["id"], at("2026-11-30T19:00:00Z"))
    assert utc["current"]["period_key"] == "2026-11" and utc["current"]["spent"] == "140.00"
    assert utc["current"]["days_left"] == 1


async def test_weekly_budget_monday_to_sunday(client):
    asha = await user(client, "asha")
    b = await create_budget(client, asha, period="weekly", amount="500.00")
    # Sunday 22 Nov 23:30 IST = 18:00Z; Monday 23 Nov 00:00 IST = 22 Nov 18:30Z.
    await add_tx(asha, "70.00", at("2026-11-22T18:00:00Z"))
    await add_tx(asha, "30.00", at("2026-11-22T18:30:00Z"))
    await add_tx(asha, "5.00", at("2026-11-15T18:29:00Z"))  # Sunday 15 Nov IST (prev week)
    w47 = await budget_at(asha, b["id"], at("2026-11-20T06:00:00Z"))
    assert w47["current"]["period_key"] == "2026-W47"
    assert (w47["current"]["start_date"], w47["current"]["end_date"]) == (
        "2026-11-16",
        "2026-11-22",
    )
    assert w47["current"]["spent"] == "70.00"
    w48 = await budget_at(asha, b["id"], at("2026-11-23T00:00:00Z"))
    assert w48["current"]["period_key"] == "2026-W48" and w48["current"]["spent"] == "30.00"


async def test_custom_period(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    for bad in (
        {"period": "custom", "start_date": "2026-11-01"},
        {"period": "custom", "start_date": "2026-11-10", "end_date": "2026-11-01"},
    ):
        r = await client.post(
            f"{API}/budgets", headers=asha.h, json={"name": "x", "amount": "10", **bad}
        )
        assert r.status_code == 422, bad
    b = await create_budget(
        client,
        asha,
        name="Diwali",
        period="custom",
        start_date="2026-11-05",
        end_date="2026-11-14",
        amount="2000.00",
    )
    await add_tx(asha, "1.00", at("2026-11-04T23:59:59Z"))  # before
    await add_tx(asha, "500.00", at("2026-11-05T00:00:00Z"))
    await add_tx(asha, "250.00", at("2026-11-14T23:59:59Z"))
    await add_tx(asha, "9.00", at("2026-11-15T00:00:00Z"))  # after
    cur = (await budget_at(asha, b["id"], at("2026-11-10T12:00:00Z")))["current"]
    assert cur["period_key"] == "custom-2026-11-05" and cur["state"] == "current"
    assert (cur["spent"], cur["remaining"], cur["percent"], cur["days_left"]) == (
        "750.00",
        "1250.00",
        37.5,
        5,
    )
    ended = (await budget_at(asha, b["id"], at("2026-12-01T00:00:00Z")))["current"]
    assert (ended["state"], ended["days_left"], ended["spent"]) == ("ended", 0, "750.00")
    upcoming = (await budget_at(asha, b["id"], at("2026-11-01T00:00:00Z")))["current"]
    assert (upcoming["state"], upcoming["days_left"]) == ("upcoming", 10)


# --- accounting rules / scenario 7 -----------------------------------------------------


async def test_only_counted_spending_and_refunds(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    b = await create_budget(client, asha)
    t = at("2026-11-10T10:00:00Z")
    await add_tx(asha, "300.00", t)
    await add_tx(asha, "50.00", t, type_=TransactionType.REFUND)
    await add_tx(asha, "999.00", t, type_=TransactionType.INCOME)
    await add_tx(asha, "999.00", t, type_=TransactionType.ADJUSTMENT)
    await add_tx(asha, "999.00", t, status=TransactionStatus.PENDING)
    await add_tx(asha, "999.00", t, status=TransactionStatus.FAILED)
    await add_tx(asha, "999.00", t, status=TransactionStatus.CANCELLED)
    await add_tx(asha, "999.00", t, deleted_at=t)
    async with get_database().sessionmaker() as s:
        from app.models import Account, AccountType

        a1 = Account(owner_user_id=asha.uid, name="A", type=AccountType.BANK)
        a2 = Account(owner_user_id=asha.uid, name="B", type=AccountType.CASH)
        s.add_all([a1, a2])
        await s.commit()
        a1_id, a2_id = a1.id, a2.id
    await add_tx(
        asha, "999.00", t, type_=TransactionType.TRANSFER, account_id=a1_id, to_account_id=a2_id
    )
    cur = (await budget_at(asha, b["id"], at("2026-11-20T00:00:00Z")))["current"]
    assert cur["spent"] == "250.00"
    assert (cur["remaining"], cur["percent"], cur["status"]) == ("750.00", 25.0, "ok")


async def test_budget_numbers_follow_edits_deletes_restores_and_recategorisation(client):
    """Critical scenario 7: spend is computed at read time from the live rows."""
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    b = await create_budget(
        client,
        asha,
        categories=[{"category_id": FOOD, "amount": "600.00"}],
    )
    now = at("2026-11-20T00:00:00Z")
    t = at("2026-11-10T10:00:00Z")

    async def numbers() -> tuple[str, str]:
        d = await budget_at(asha, b["id"], now)
        return d["current"]["spent"], d["category_breakdown"][0]["spent"]

    tx = await add_tx(asha, "400.00", t, category_id=FOOD)
    assert await numbers() == ("400.00", "400.00")
    await mutate_tx(tx, amount=Decimal("650.00"))  # edit amount
    d = await budget_at(asha, b["id"], now)
    assert d["category_breakdown"][0]["status"] == "exceeded"
    assert await numbers() == ("650.00", "650.00")
    await mutate_tx(tx, category_id=uuid.UUID(RENT))  # re-categorise out of the budget
    assert await numbers() == ("0.00", "0.00")
    await mutate_tx(tx, category_id=uuid.UUID(FOOD))
    await mutate_tx(tx, deleted_at=t)  # soft delete
    assert await numbers() == ("0.00", "0.00")
    await mutate_tx(tx, deleted_at=None)  # restore
    assert await numbers() == ("650.00", "650.00")
    await mutate_tx(tx, occurred_at=at("2026-10-31T10:00:00Z"))  # moved to another month
    assert await numbers() == ("0.00", "0.00")
    await mutate_tx(tx, occurred_at=t, status=TransactionStatus.PENDING)
    assert await numbers() == ("0.00", "0.00")
    await mutate_tx(tx, status=TransactionStatus.COMPLETED)  # e.g. UPI payment confirmed
    assert await numbers() == ("650.00", "650.00")
    await mutate_tx(tx, type=TransactionType.REFUND)  # type change flips the sign
    assert await numbers() == ("-650.00", "-650.00")


async def test_category_limits_cover_subcategories(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    sub = await add_category(asha, "Restaurants", parent=FOOD)
    subsub = await add_category(asha, "Cafes", parent=sub)
    b = await create_budget(
        client,
        asha,
        name="Food+Groceries",
        amount="5000.00",
        categories=[
            {"category_id": FOOD, "amount": "1000.00"},
            {"category_id": GROCERIES, "amount": "3000.00"},
        ],
    )
    assert [c["name"] for c in b["categories"]] == ["Food & Dining", "Groceries"]
    t = at("2026-11-05T10:00:00Z")
    await add_tx(asha, "200.00", t, category_id=FOOD)
    await add_tx(asha, "300.00", t, category_id=sub)
    await add_tx(asha, "350.00", t, category_id=subsub)
    await add_tx(asha, "50.00", t, category_id=sub, type_=TransactionType.REFUND)
    await add_tx(asha, "1200.00", t, category_id=GROCERIES)
    await add_tx(asha, "9000.00", t, category_id=RENT)  # not in this budget
    await add_tx(asha, "77.00", t)  # uncategorised: not in a category budget
    d = await budget_at(asha, b["id"], at("2026-11-20T00:00:00Z"))
    assert d["current"]["spent"] == "2000.00"
    food, groc = d["category_breakdown"]
    assert (food["name"], food["spent"], food["remaining"], food["percent"], food["status"]) == (
        "Food & Dining",
        "800.00",
        "200.00",
        80.0,
        "warning",
    )
    assert (groc["spent"], groc["status"]) == ("1200.00", "ok")

    # A budget without category limits counts everything.
    overall = await create_budget(client, asha, name="All")
    d = await budget_at(asha, overall["id"], at("2026-11-20T00:00:00Z"))
    assert d["current"]["spent"] == "11077.00" and d["current"]["status"] == "exceeded"
    assert d["category_breakdown"] == []


async def test_category_validation(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    bobs = await add_category(bob, "Bob's")
    income = await add_category(asha, "Side gig", kind=CategoryKind.INCOME)
    for cats in (
        [{"category_id": bobs, "amount": "1"}],  # someone else's category
        [{"category_id": INCOME_CAT, "amount": "1"}],  # income category
        [{"category_id": income, "amount": "1"}],
        [{"category_id": str(uuid.uuid4()), "amount": "1"}],
        [{"category_id": FOOD, "amount": "1"}, {"category_id": FOOD, "amount": "2"}],
        [{"category_id": FOOD, "amount": "0"}],
    ):
        r = await client.post(
            f"{API}/budgets",
            headers=asha.h,
            json={"name": "x", "amount": "10", "categories": cats},
        )
        assert r.status_code == 422, (cats, r.text)


async def test_thresholds_validation_and_defaults(client):
    asha = await user(client, "asha")
    b = await create_budget(client, asha)
    assert b["alert_thresholds"] == [50, 75, 90, 100]
    assert b["currency"] == "INR" and b["carry_forward"] is False and b["is_active"] is True
    b = await create_budget(client, asha, alert_thresholds=[100, 80, 80, 150])
    assert b["alert_thresholds"] == [80, 100, 150]
    for bad in ([], [0], [201], [50, -1]):
        r = await client.post(
            f"{API}/budgets",
            headers=asha.h,
            json={"name": "x", "amount": "10", "alert_thresholds": bad},
        )
        assert r.status_code == 422, bad
    for bad in ({"amount": "0"}, {"amount": "-5"}, {"name": ""}, {"nope": 1}):
        r = await client.post(f"{API}/budgets", headers=asha.h, json={"name": "x", **bad})
        assert r.status_code == 422, bad


# --- carry forward -----------------------------------------------------------------------


async def test_carry_forward(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    b = await create_budget(client, asha, start_date="2026-09-15", carry_forward=True)
    await add_tx(asha, "5000.00", at("2026-08-20T00:00:00Z"))  # before the first period
    await add_tx(asha, "700.00", at("2026-09-20T00:00:00Z"))  # September: 300 unspent
    await add_tx(asha, "1200.00", at("2026-10-20T00:00:00Z"))  # October: overspent by 200
    await add_tx(asha, "100.00", at("2026-11-02T00:00:00Z"))

    sep = (await budget_at(asha, b["id"], at("2026-09-30T00:00:00Z")))["current"]
    assert (sep["carried"], sep["limit"]) == ("0.00", "1000.00")  # first period: no carry
    octo = (await budget_at(asha, b["id"], at("2026-10-31T00:00:00Z")))["current"]
    assert (octo["carried"], octo["limit"], octo["spent"], octo["remaining"]) == (
        "300.00",
        "1300.00",
        "1200.00",
        "100.00",
    )
    assert octo["status"] == "warning"
    nov = (await budget_at(asha, b["id"], at("2026-11-15T00:00:00Z")))["current"]
    assert (nov["carried"], nov["limit"], nov["remaining"]) == ("-200.00", "800.00", "700.00")

    # History: oldest first, never before the first period, carry per period.
    r = await client.get(f"{API}/budgets/{b['id']}/history", headers=asha.h, params={"periods": 6})
    assert r.status_code == 200
    # (the API uses the real clock; compute via service for a fixed date)
    hist = await with_service(
        asha, lambda svc, u: svc.history(u, uuid.UUID(b["id"]), 6, at("2026-11-15T00:00:00Z"))
    )
    rows = [(i.period_key, str(i.limit), str(i.spent), i.is_current) for i in hist.items]
    assert rows == [
        ("2026-09", "1000.00", "700.00", False),
        ("2026-10", "1300.00", "1200.00", False),
        ("2026-11", "800.00", "100.00", True),
    ]
    two = await with_service(
        asha, lambda svc, u: svc.history(u, uuid.UUID(b["id"]), 2, at("2026-11-15T00:00:00Z"))
    )
    assert [i.period_key for i in two.items] == ["2026-10", "2026-11"]
    assert (
        await client.get(f"{API}/budgets/{b['id']}/history?periods=0", headers=asha.h)
    ).status_code == 422

    # Without carry-forward the limit is just the amount.
    r = await client.patch(
        f"{API}/budgets/{b['id']}", headers=asha.h, json={"carry_forward": False}
    )
    assert r.status_code == 200
    nov = (await budget_at(asha, b["id"], at("2026-11-15T00:00:00Z")))["current"]
    assert (nov["carried"], nov["limit"]) == ("0.00", "1000.00")


# --- family & member budgets, permissions ----------------------------------------------


async def test_family_and_member_budgets(client):
    fid, p = await household(client)
    for who in p.values():
        await set_timezone(who, "UTC")
    t = at("2026-11-10T00:00:00Z")
    await add_tx(p["owner"], "100.00", t, family_id=fid)
    await add_tx(p["member"], "40.00", t, family_id=fid)
    await add_tx(p["member"], "999.00", t)  # member's personal row: never in a family budget
    fam = await create_budget(client, p["admin"], scope="family", family_id=fid, name="Household")
    assert fam["scope"] == "family" and fam["family_id"] == fid and fam["can_edit"] is True
    mem = await create_budget(
        client,
        p["owner"],
        scope="family",
        family_id=fid,
        name="Kid pocket money",
        member_user_id=p["member"].id,
        amount="50.00",
    )
    now = at("2026-11-20T00:00:00Z")
    assert (await budget_at(p["viewer"], fam["id"], now))["current"]["spent"] == "140.00"
    m = await budget_at(p["member"], mem["id"], now)
    assert m["current"]["spent"] == "40.00" and m["current"]["status"] == "warning"
    assert m["can_edit"] is False

    # Personal budget of the member ignores family rows.
    personal = await create_budget(client, p["member"], name="Mine")
    assert (await budget_at(p["member"], personal["id"], now))["current"]["spent"] == "999.00"

    # member_user_id rules.
    outsider = await user(client, "outsider")
    for body in (
        {"scope": "family", "family_id": fid, "member_user_id": outsider.id},
        {"member_user_id": p["member"].id},  # personal budget
    ):
        r = await client.post(
            f"{API}/budgets", headers=p["owner"].h, json={"name": "x", "amount": "1", **body}
        )
        assert r.status_code == 422, body
    r = await client.patch(
        f"{API}/budgets/{mem['id']}", headers=p["owner"].h, json={"member_user_id": outsider.id}
    )
    assert r.status_code == 422
    r = await client.patch(
        f"{API}/budgets/{mem['id']}", headers=p["owner"].h, json={"member_user_id": None}
    )
    assert r.status_code == 200 and r.json()["member_user_id"] is None

    # Family currency is the default for family budgets.
    r = await client.patch(f"{API}/families/{fid}", headers=p["owner"].h, json={"currency": "USD"})
    assert r.status_code == 200
    usd = await create_budget(client, p["owner"], scope="family", family_id=fid)
    assert usd["currency"] == "USD"

    # Lists: personal vs family scope.
    r = await client.get(f"{API}/budgets", headers=p["member"].h)
    assert [x["name"] for x in r.json()] == ["Mine"]
    r = await client.get(f"{API}/budgets", headers=p["viewer"].h, params={"family_id": fid})
    assert r.status_code == 200 and len(r.json()) == 3
    assert all(x["can_edit"] is False for x in r.json())
    assert "category_breakdown" not in r.json()[0] and "current" in r.json()[0]


async def test_permission_matrix(client):
    fid, p = await household(client)
    body = {"name": "Groceries", "amount": "100.00", "scope": "family", "family_id": fid}
    expected = {"owner": 201, "admin": 201, "member": 403, "viewer": 403}
    for role, code in expected.items():
        r = await client.post(f"{API}/budgets", headers=p[role].h, json=body)
        assert r.status_code == code, (role, r.text)
    outsider = await user(client, "outsider")
    assert (await client.post(f"{API}/budgets", headers=outsider.h, json=body)).status_code == 404

    bid = (
        await client.get(f"{API}/budgets", headers=p["owner"].h, params={"family_id": fid})
    ).json()[0]["id"]
    for role in ("owner", "admin", "member", "viewer"):
        assert (await client.get(f"{API}/budgets/{bid}", headers=p[role].h)).status_code == 200
        r = await client.get(f"{API}/budgets/{bid}/history", headers=p[role].h)
        assert r.status_code == 200
    for role in ("member", "viewer"):
        r = await client.patch(f"{API}/budgets/{bid}", headers=p[role].h, json={"amount": "5"})
        assert r.status_code == 403
        assert (await client.delete(f"{API}/budgets/{bid}", headers=p[role].h)).status_code == 403
    r = await client.patch(f"{API}/budgets/{bid}", headers=p["admin"].h, json={"amount": "250.5"})
    assert r.status_code == 200 and r.json()["amount"] == "250.50"
    assert (await client.delete(f"{API}/budgets/{bid}", headers=p["admin"].h)).status_code == 204
    assert (await client.get(f"{API}/budgets/{bid}", headers=p["owner"].h)).status_code == 404

    async with get_database().sessionmaker() as s:
        actions = list(
            await s.scalars(
                text(
                    "SELECT action FROM audit_logs WHERE entity_type = 'budget' ORDER BY created_at"
                )
            )
        )
    assert actions.count("budget.created") == 2
    assert actions[-2:] == ["budget.updated", "budget.deleted"]


async def test_isolation(client):
    a = await user(client, "alice")
    b = await user(client, "bob")
    mine = await create_budget(client, b, name="Bob private")
    fid_b, pb = await household(client, prefix="b")
    r = await client.get(f"{API}/budgets", headers=pb["owner"].h, params={"family_id": fid_b})
    assert r.json() == []
    fam_budget = await create_budget(client, pb["owner"], scope="family", family_id=fid_b)
    for bid in (mine["id"], fam_budget["id"]):
        assert (await client.get(f"{API}/budgets/{bid}", headers=a.h)).status_code == 404
        assert (await client.get(f"{API}/budgets/{bid}/history", headers=a.h)).status_code == 404
        r = await client.patch(f"{API}/budgets/{bid}", headers=a.h, json={"amount": "1"})
        assert r.status_code == 404
        assert (await client.delete(f"{API}/budgets/{bid}", headers=a.h)).status_code == 404
    for path in ("/budgets", "/budgets/overview"):
        r = await client.get(f"{API}{path}", headers=a.h, params={"family_id": fid_b})
        assert r.status_code == 404
    assert (await client.get(f"{API}/budgets", headers=a.h)).json() == []
    assert (await client.get(f"{API}/budgets")).status_code == 401
    # Bob's private budget is not in his family's list, and family members can't see it.
    r = await client.get(f"{API}/budgets/{mine['id']}", headers=pb["owner"].h)
    assert r.status_code == 404


async def test_left_member_loses_access(client):
    fid, p = await household(client)
    fam = await create_budget(client, p["owner"], scope="family", family_id=fid)
    r = await client.post(f"{API}/families/{fid}/leave", headers=p["viewer"].h)
    assert r.status_code in (200, 204), r.text
    assert (
        await client.get(f"{API}/budgets/{fam['id']}", headers=p["viewer"].h)
    ).status_code == 404


# --- overview ------------------------------------------------------------------------------


async def test_overview(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    now = at("2026-11-20T00:00:00Z")
    t = at("2026-11-10T00:00:00Z")
    await add_tx(asha, "300.00", t, category_id=FOOD)
    await add_tx(asha, "200.00", t, category_id=RENT)

    async def overview() -> dict[str, Any]:
        out = await with_service(asha, lambda svc, u: svc.overview(u, None, now))
        return dict(out.model_dump(mode="json"))

    o = await overview()
    assert o["month"]["basis"] == "none" and o["month"]["spent"] == "500.00"
    assert o["month"]["total_budget"] == "0.00" and o["warnings"] == []

    food = await create_budget(
        client,
        asha,
        name="Food",
        amount="350.00",
        categories=[{"category_id": FOOD, "amount": "350.00"}],
    )
    o = await overview()
    assert (o["month"]["basis"], o["month"]["total_budget"], o["month"]["spent"]) == (
        "categories",
        "350.00",
        "300.00",
    )
    assert [w["name"] for w in o["warnings"]] == ["Food"]
    assert o["warnings"][0]["status"] == "warning" and o["warnings"][0]["percent"] == 85.7

    await create_budget(client, asha, name="Everything", amount="400.00")
    await create_budget(client, asha, name="Weekly", period="weekly", amount="10000.00")
    await create_budget(client, asha, name="Paused", amount="1.00", is_active=False)
    o = await overview()
    assert (o["month"]["basis"], o["month"]["budget_count"]) == ("overall", 1)
    assert (o["month"]["total_budget"], o["month"]["spent"], o["month"]["remaining"]) == (
        "400.00",
        "500.00",
        "-100.00",
    )
    assert o["month"]["status"] == "exceeded"
    assert [w["name"] for w in o["warnings"]] == ["Everything", "Food"]
    assert o["warnings"][0]["status"] == "exceeded"
    assert food["id"] in {w["budget_id"] for w in o["warnings"]}

    r = await client.get(f"{API}/budgets/overview", headers=asha.h)
    assert r.status_code == 200 and r.json()["scope"] == "personal"
    r = await client.get(f"{API}/budgets", headers=asha.h, params={"is_active": "false"})
    assert [x["name"] for x in r.json()] == ["Paused"]


# --- alerts --------------------------------------------------------------------------------


async def test_alerts_fire_once_per_threshold_per_period(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    b = await create_budget(client, asha, name="Food", start_date="2026-11-01")
    prefix = f"budget:{b['id']}:"
    now = at("2026-11-20T00:00:00Z")

    assert await evaluate(now) == 0
    await add_tx(asha, "600.00", at("2026-11-05T00:00:00Z"))
    assert await evaluate(now) == 1
    assert [k for _, k in await notifications(prefix)] == [f"{prefix}2026-11:50"]
    assert await evaluate(now) == 0  # no duplicates

    await add_tx(asha, "320.00", at("2026-11-06T00:00:00Z"))  # 92 %
    assert await evaluate(now) == 2
    await add_tx(asha, "100.00", at("2026-11-07T00:00:00Z"))  # 102 %
    assert await evaluate(now) == 2  # 100 + exceeded
    assert await evaluate(now) == 0
    keys = sorted(k for _, k in await notifications(prefix))
    assert keys == sorted(f"{prefix}2026-11:{x}" for x in ("50", "75", "90", "100", "exceeded"))
    async with get_database().sessionmaker() as s:
        n = await s.scalar(
            select(Notification).where(Notification.dedupe_key == f"{prefix}2026-11:exceeded")
        )
        assert n is not None and n.category is NotificationCategory.BUDGET_ALERT
        assert n.user_id == asha.uid and n.data["budget_id"] == b["id"]
        assert "exceeded" in n.title and "₹1,020.00" in n.body

    # A refund drops below 90 % — already-fired alerts are not repeated or revoked.
    await add_tx(asha, "200.00", at("2026-11-08T00:00:00Z"), type_=TransactionType.REFUND)
    assert await evaluate(now) == 0

    # New period: fires again.
    await add_tx(asha, "760.00", at("2026-12-02T00:00:00Z"))
    assert await evaluate(at("2026-12-03T00:00:00Z")) == 2
    assert {k for _, k in await notifications(f"{prefix}2026-12")} == {
        f"{prefix}2026-12:50",
        f"{prefix}2026-12:75",
    }

    # Inactive / deleted budgets are skipped.
    await client.patch(f"{API}/budgets/{b['id']}", headers=asha.h, json={"is_active": False})
    await add_tx(asha, "5000.00", at("2026-12-04T00:00:00Z"))
    assert await evaluate(at("2026-12-05T00:00:00Z")) == 0


async def test_alert_recipients_and_preferences(client):
    fid, p = await household(client)
    for who in p.values():
        await set_timezone(who, "UTC")
    fam = await create_budget(client, p["owner"], scope="family", family_id=fid, name="House")
    mem = await create_budget(
        client,
        p["admin"],
        scope="family",
        family_id=fid,
        name="Member",
        member_user_id=p["member"].id,
        amount="500.00",
        alert_thresholds=[100],
    )
    # The admin turned budget alerts off completely.
    async with get_database().sessionmaker() as s:
        s.add(
            NotificationPreference(
                user_id=p["admin"].uid,
                category=NotificationCategory.BUDGET_ALERT,
                in_app_enabled=False,
                push_enabled=False,
            )
        )
        await s.commit()
    await add_tx(p["member"], "1000.00", at("2026-11-05T00:00:00Z"), family_id=fid)
    await evaluate(at("2026-11-20T00:00:00Z"))

    fam_rows = await notifications(f"budget:{fam['id']}:2026-11:")
    assert {u for u, _ in fam_rows} == {p["owner"].uid}  # owner/admins only (admin opted out)
    assert len(fam_rows) == 4  # 50, 75, 90, 100 (not exceeded: spent == limit)
    mem_rows = await notifications(f"budget:{mem['id']}:2026-11:")
    assert {u for u, _ in mem_rows} == {p["owner"].uid, p["member"].uid}
    assert {k.rsplit(":", 1)[1] for _, k in mem_rows} == {"100", "exceeded"}
    viewer_rows = [u for u, _ in fam_rows + mem_rows if u == p["viewer"].uid]
    assert viewer_rows == []
    async with get_database().sessionmaker() as s:
        fam_ids = set(
            await s.scalars(
                select(Notification.family_id).where(Notification.dedupe_key.like("budget:%"))
            )
        )
    assert fam_ids == {uuid.UUID(fid)}


async def test_worker_registers_periodic_alerts():
    from app.workers.jobs import discover

    _, periodic = discover()
    task = next(t for t in periodic if t.name == "budget_alerts")
    assert task.every.total_seconds() == 600


async def test_notify_dedupe_is_shared_with_other_producers(client):
    """Sanity check: a dedupe key already used makes notify() a no-op (what alerts rely on)."""
    asha = await user(client, "asha")
    async with get_database().sessionmaker() as s:
        args = {
            "user_id": asha.uid,
            "category": NotificationCategory.BUDGET_ALERT,
            "title": "t",
            "body": "b",
            "dedupe_key": "budget:x:2026-11:50",
        }
        assert await notify(s, **args) is True
        assert await notify(s, **args) is False
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(Notification)) == 1
