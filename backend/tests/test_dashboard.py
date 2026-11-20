"""Home dashboard: every card, personal vs family view, accounting rules (transfers never
count), isolation and a bounded number of SQL statements.

Service-level calls pass a fixed ``now`` so periods are deterministic.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import event

from app.db.session import get_database
from app.models import (
    AccountType,
    GoalStatus,
    RecurrenceFrequency,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    User,
)
from app.services.dashboard import DashboardService
from app.services.reports import ReportParams
from tests.test_reports import (
    API,
    FOOD,
    INCOME_CAT,
    RENT,
    Actor,
    add_account,
    add_goal,
    add_rule,
    add_tx,
    at,
    builtin,
    contribute,
    create_family,
    join,
    mutate_tx,
    user,
)

NOW = at("2026-11-20T06:00:00Z")  # 11:30 IST, Friday 20 Nov 2026
CATS = [
    builtin(n)
    for n in (
        "Food & Dining",
        "Groceries",
        "Utilities",
        "Electricity",
        "Internet",
        "Mobile Recharge",
        "Transportation",
        "Fuel",
    )
]


async def dashboard(who: Actor, now: datetime = NOW, **params: Any) -> dict[str, Any]:
    async with get_database().sessionmaker() as s:
        u = await s.get(User, who.uid)
        assert u is not None
        out = await DashboardService(s).get(u, ReportParams(**params), now=now)
        return dict(out.model_dump(mode="json"))


async def count_queries(fn: Callable[[], Awaitable[Any]]) -> int:
    engine = get_database().engine.sync_engine
    seen: list[str] = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        await fn()
    finally:
        event.remove(engine, "before_cursor_execute", on_execute)
    return len(seen)


async def personal_story(client, asha: Actor, ravi: Actor, fid: str) -> None:
    bank = await add_account(asha, "Bank", opening="10000")
    cash = await add_account(asha, "Cash", type_=AccountType.CASH, opening="500")
    card = await add_account(asha, "Card", type_=AccountType.CREDIT_CARD)
    await add_account(asha, "Old", opening="999", is_active=False)
    await add_account(asha, "Dollars", opening="50", currency="USD")

    await add_tx(
        asha,
        "50000",
        "2026-11-01T05:00:00Z",
        type_=TransactionType.INCOME,
        category_id=INCOME_CAT,
        account_id=bank,
    )
    for i, (cat, amount) in enumerate(zip(CATS, range(800, 0, -100), strict=True)):
        await add_tx(
            asha,
            str(amount),
            at(f"2026-11-{2 + i:02d}T05:00:00Z"),
            category_id=cat,
            account_id=bank,
        )
    await add_tx(
        asha,
        "50",
        "2026-11-10T05:00:00Z",
        type_=TransactionType.REFUND,
        category_id=CATS[0],
        account_id=bank,
    )
    await add_tx(
        asha,
        "1000",
        "2026-11-11T05:00:00Z",
        type_=TransactionType.TRANSFER,
        account_id=bank,
        to_account_id=cash,
    )
    await add_tx(asha, "2000", "2026-11-12T05:00:00Z", category_id=RENT, account_id=card)
    await add_tx(asha, "1234", "2026-10-15T05:00:00Z", category_id=FOOD)  # last month
    # Pending UPI payments: mine (personal + family) need my confirmation; Ravi's don't.
    upi = {"source": TransactionSource.UPI_INTENT, "status": TransactionStatus.PENDING}
    await add_tx(asha, "99", "2026-11-19T10:00:00Z", category_id=FOOD, merchant="Tea", **upi)
    await add_tx(asha, "88", "2026-11-18T10:00:00Z", family_id=fid, **upi)
    await add_tx(ravi, "77", "2026-11-18T10:00:00Z", family_id=fid, **upi)
    # Not shown anywhere.
    gone = await add_tx(asha, "5000", "2026-11-19T11:00:00Z", category_id=FOOD)
    await mutate_tx(gone, deleted_at=NOW)
    await add_tx(
        asha, "6000", "2026-11-19T12:00:00Z", category_id=FOOD, status=TransactionStatus.FAILED
    )
    # Family rows: mine count as "family spending I recorded"; Ravi's don't (personal view).
    await add_tx(asha, "700", "2026-11-13T05:00:00Z", family_id=fid, category_id=FOOD)
    await add_tx(ravi, "9000", "2026-11-13T05:00:00Z", family_id=fid, category_id=FOOD)
    joint = await add_account(asha, "Joint", opening="3000", family_id=fid)
    joint2 = await add_account(asha, "Joint savings", family_id=fid)
    await add_tx(
        ravi,
        "5000",
        "2026-11-14T05:00:00Z",
        type_=TransactionType.TRANSFER,
        family_id=fid,
        account_id=joint,
        to_account_id=joint2,
    )

    await add_rule(asha, "Internet bill", "999", RecurrenceFrequency.MONTHLY, date(2026, 11, 23))
    await add_rule(
        asha,
        "Family rent",
        "20000",
        RecurrenceFrequency.MONTHLY,
        date(2026, 11, 22),
        family_id=fid,
    )
    for name, target, saved in (
        ("Laptop", "10000", "1000"),
        ("Trip", "10000", "5000"),
        ("Phone", "10000", "9000"),
        ("Bike", "10000", "3000"),
    ):
        g = await add_goal(asha, name, target)
        await contribute(asha, g, saved, "2026-11-01T05:00:00Z")
    done = await add_goal(asha, "Archived", "100", status=GoalStatus.ARCHIVED)
    await contribute(asha, done, "100", "2026-11-01T05:00:00Z")

    r = await client.post(
        f"{API}/budgets",
        headers=asha.h,
        json={"name": "Monthly", "amount": "20000.00", "start_date": "2026-11-01"},
    )
    assert r.status_code == 201, r.text


async def test_personal_dashboard(client):
    asha = await user(client, "asha")
    ravi = await user(client, "ravi")
    fid = await create_family(client, asha)
    await join(client, asha, fid, ravi)
    await personal_story(client, asha, ravi, fid)

    d = await dashboard(asha)
    assert d["meta"]["scope"] == "personal"
    assert d["meta"]["currency"] == "INR"
    assert set(d["meta"]["currencies"]) == {"INR", "USD"}
    assert d["meta"]["range"] == {
        "preset": "this_month",
        "start_date": "2026-11-01",
        "end_date": "2026-11-30",
    }

    bal = d["balance"]
    # bank = 10000 + 50000 - 3600 + 50 - 1000; cash = 500 + 1000; card = -2000
    assert {a["name"]: a["balance"] for a in bal["accounts"]} == {
        "Bank": "55450.00",
        "Cash": "1500.00",
    }
    assert bal["available"] == "56950.00"
    assert bal["liabilities"] == [
        {
            "id": bal["liabilities"][0]["id"],
            "name": "Card",
            "type": "credit_card",
            "balance": "-2000.00",
        }
    ]
    assert (bal["liabilities_total"], bal["net_worth"]) == ("-2000.00", "54950.00")

    # Transfers (1000 personal, 5000 family) and the refund never inflate the totals.
    assert d["totals"] == {
        "income": "50000.00",
        "expense": "5550.00",
        "net_savings": "44450.00",
        "savings_rate": 88.9,
    }
    assert d["spending_split"] == {
        "view": "personal",
        "personal": "5550.00",
        "family": "700.00",
        "members": [],
    }

    cats = d["spending_by_category"]
    assert len(cats) == 7
    assert [(c["name"], c["amount"]) for c in cats[:3]] == [
        ("Rent", "2000.00"),
        ("Food & Dining", "750.00"),
        ("Groceries", "700.00"),
    ]
    other = cats[-1]
    assert (other["name"], other["amount"], other["count"], other["is_other"]) == (
        "Other",
        "600.00",
        3,
        True,
    )
    assert other["percent"] == 10.8

    trend = d["monthly_trend"]
    assert [t["month"] for t in trend] == [
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
        "2026-10",
        "2026-11",
    ]
    assert trend[-2]["expense"] == "1234.00"
    assert (trend[-1]["income"], trend[-1]["expense"]) == ("50000.00", "5550.00")

    recent = d["recent_transactions"]
    assert len(recent) == 8
    assert recent[0]["amount"] == "99.00"
    assert recent[0]["status"] == "pending"
    assert recent[0]["merchant"] == "Tea"
    assert recent[0]["category"]["name"] == "Food & Dining"
    assert recent[0]["created_by_name"] == "Asha"
    assert all(r["amount"] not in ("5000.00", "6000.00", "700.00") for r in recent)

    assert d["pending_upi_count"] == 2
    assert [(b["name"], b["due_date"], b["days_until_due"]) for b in d["upcoming_bills"]] == [
        ("Internet bill", "2026-11-23", 3)
    ]

    sav = d["savings"]
    assert sav["active_count"] == 4
    assert [g["name"] for g in sav["goals"]] == ["Phone", "Trip", "Bike"]
    assert (sav["target_total"], sav["saved_total"]) == ("40000.00", "18000.00")

    month = d["budget"]["month"]
    assert (month["total_budget"], month["spent"], month["remaining"]) == (
        "20000.00",
        "5550.00",
        "14450.00",
    )
    assert d["budget"]["warnings"] == []

    usd = await dashboard(asha, currency="USD")
    assert usd["meta"]["currency"] == "USD"
    assert usd["balance"]["available"] == "50.00"
    assert usd["totals"]["expense"] == "0.00"


async def test_family_dashboard(client):
    asha = await user(client, "asha")
    ravi = await user(client, "ravi")
    fid = await create_family(client, asha)
    await join(client, asha, fid, ravi)
    await personal_story(client, asha, ravi, fid)

    d = await dashboard(ravi, family_id=uuid.UUID(fid))
    assert d["meta"]["scope"] == "family"
    assert d["totals"]["expense"] == "9700.00"  # the 5000 family transfer never counts
    assert d["totals"]["income"] == "0.00"
    split = d["spending_split"]
    assert (split["view"], split["family"], split["personal"]) == ("family", "9700.00", "9000.00")
    assert [(m["full_name"], m["expense"], m["percent"]) for m in split["members"]] == [
        ("Ravi", "9000.00", 92.8),
        ("Asha", "700.00", 7.2),
    ]
    assert d["balance"]["available"] == "3000.00"  # -2000 + 5000
    assert d["pending_upi_count"] == 2  # every pending family UPI payment
    assert [b["name"] for b in d["upcoming_bills"]] == ["Family rent"]
    assert d["savings"]["active_count"] == 0
    assert d["budget"]["scope"] == "family"
    assert {r["amount"] for r in d["recent_transactions"]} == {
        "700.00",
        "9000.00",
        "5000.00",
        "88.00",
        "77.00",
    }
    assert all(r["category"] is None or r["category"]["name"] for r in d["recent_transactions"])


async def test_dashboard_api_and_isolation(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    fid_b = await create_family(client, bob, "Bob Family")
    await add_tx(bob, "500", datetime.now(UTC), category_id=FOOD)
    await add_tx(bob, "600", datetime.now(UTC), family_id=fid_b, category_id=FOOD)
    await add_account(bob, "Bob bank", opening="12345")

    r = await client.get(f"{API}/dashboard", headers=asha.h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {
        "meta",
        "balance",
        "totals",
        "spending_split",
        "budget",
        "recent_transactions",
        "upcoming_bills",
        "savings",
        "spending_by_category",
        "monthly_trend",
        "pending_upi_count",
    }
    assert d["totals"]["expense"] == "0.00"
    assert d["balance"]["available"] == "0.00"
    assert d["recent_transactions"] == []
    assert d["spending_by_category"] == []
    assert len(d["monthly_trend"]) == 6

    r = await client.get(f"{API}/dashboard", headers=asha.h, params={"family_id": fid_b})
    assert r.status_code == 404
    r = await client.get(f"{API}/dashboard", headers=bob.h, params={"preset": "today"})
    assert r.status_code == 200
    assert r.json()["meta"]["range"]["preset"] == "today"
    r = await client.get(f"{API}/dashboard")
    assert r.status_code == 401


async def test_dashboard_query_count_is_bounded(client):
    asha = await user(client, "asha")
    ravi = await user(client, "ravi")
    fid = await create_family(client, asha)
    await join(client, asha, fid, ravi)
    await personal_story(client, asha, ravi, fid)

    async def run(**params: Any) -> int:
        async with get_database().sessionmaker() as s:
            u = await s.get(User, asha.uid)
            assert u is not None
            svc = DashboardService(s)
            return await count_queries(lambda: svc.get(u, ReportParams(**params), now=NOW))

    personal = await run()
    family = await run(family_id=uuid.UUID(fid))
    # 21 / 22 today with one budget (the budget card adds ~3 statements per active budget).
    assert personal <= 24, personal
    assert family <= 24, family

    # Much more data of every kind (but the same number of budgets): same statement count.
    for i in range(15):
        acc = await add_account(asha, f"Extra {i}", opening="10")
        await add_tx(asha, "10", "2026-11-15T05:00:00Z", category_id=CATS[i % 8], account_id=acc)
        await add_tx(asha, "10", "2026-11-15T05:00:00Z", family_id=fid, category_id=FOOD)
        await add_tx(ravi, "10", "2026-11-15T05:00:00Z", family_id=fid)
    for i in range(4):
        g = await add_goal(asha, f"Goal {i}", "1000")
        await contribute(asha, g, "10", "2026-11-01T05:00:00Z")
        await add_rule(asha, f"Bill {i}", "10", RecurrenceFrequency.WEEKLY, date(2026, 11, 1))
    assert await run() == personal
    assert await run(family_id=uuid.UUID(fid)) == family
