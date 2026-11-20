"""Reports: accounting rules (transfers/adjustments never income or expense, refunds reduce
spending, pending/failed/cancelled/deleted excluded), timezone bucketing, zero-filled series,
member / personal-vs-family splits, balance history, savings / recurring / budget / UPI
reports, CSV export (formula-injection safe) and tenant isolation.

Rows are inserted directly through the models; API calls use explicit ``from``/``to`` ranges so
results never depend on the wall clock.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update

from app.db.session import get_database
from app.models import (
    Account,
    AccountType,
    CategoryKind,
    ContributionKind,
    PaymentMethod,
    RecordScope,
    RecurrenceFrequency,
    RecurringTransaction,
    SavingsContribution,
    SavingsGoal,
    Transaction,
    TransactionCategory,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    UpiAttemptStatus,
    UpiPaymentAttempt,
    User,
)
from app.services import ledger
from app.services.reports import csv_cell, iter_csv, resolve_range
from tests.conftest import BUILTIN_CATEGORIES, bearer, register_verified

API = "/api/v1"
NOV = {"from": "2026-11-01", "to": "2026-11-30"}


def builtin(name: str) -> str:
    return next(c["id"] for c in BUILTIN_CATEGORIES if c["name"] == name)


FOOD = builtin("Food & Dining")
GROCERIES = builtin("Groceries")
RENT = builtin("Rent")
INCOME_CAT = next(c["id"] for c in BUILTIN_CATEGORIES if c["kind"] == "income")


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


# --- helpers (also used by test_dashboard.py) ----------------------------------------------


class Actor:
    def __init__(self, auth: dict[str, Any]) -> None:
        self.id = auth["user"]["id"]
        self.uid = uuid.UUID(self.id)
        self.email = auth["user"]["email"]
        self.name = auth["user"]["full_name"]
        self.h = bearer(auth["tokens"])


async def user(client, name: str) -> Actor:
    return Actor(
        await register_verified(client, email=f"{name}@example.com", full_name=name.title())
    )


async def create_family(client, owner: Actor, name: str = "Rao Family") -> str:
    r = await client.post(f"{API}/families", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def join(client, owner: Actor, family_id: str, who: Actor, role: str = "member") -> None:
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


def _scope(family_id: str | None) -> dict[str, Any]:
    return {
        "scope": RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
        "family_id": uuid.UUID(family_id) if family_id else None,
    }


async def add_tx(
    owner: Actor,
    amount: str,
    occurred_at: datetime | str,
    *,
    type_: TransactionType = TransactionType.EXPENSE,
    family_id: str | None = None,
    category_id: str | None = None,
    status: TransactionStatus = TransactionStatus.COMPLETED,
    account_id: uuid.UUID | None = None,
    to_account_id: uuid.UUID | None = None,
    sign: int = 1,
    **kw: Any,
) -> uuid.UUID:
    if isinstance(occurred_at, str):
        occurred_at = at(occurred_at)
    async with get_database().sessionmaker() as s:
        tx = Transaction(
            owner_user_id=owner.uid,
            **_scope(family_id),
            type=type_,
            status=status,
            amount=Decimal(amount),
            occurred_at=occurred_at,
            category_id=uuid.UUID(category_id) if category_id else None,
            account_id=account_id,
            to_account_id=to_account_id,
            adjustment_sign=sign if type_ is TransactionType.ADJUSTMENT else None,
            **kw,
        )
        s.add(tx)
        await s.commit()
        return tx.id


async def add_account(
    owner: Actor,
    name: str = "Bank",
    *,
    type_: AccountType = AccountType.BANK,
    opening: str = "0",
    family_id: str | None = None,
    **kw: Any,
) -> uuid.UUID:
    async with get_database().sessionmaker() as s:
        acc = Account(
            owner_user_id=owner.uid,
            **_scope(family_id),
            name=name,
            type=type_,
            opening_balance=Decimal(opening),
            **kw,
        )
        s.add(acc)
        await s.commit()
        return acc.id


async def add_category(owner: Actor, name: str, parent: str | None = None, **kw: Any) -> str:
    async with get_database().sessionmaker() as s:
        cat = TransactionCategory(
            owner_user_id=owner.uid,
            name=name,
            kind=kw.pop("kind", CategoryKind.EXPENSE),
            parent_id=uuid.UUID(parent) if parent else None,
            **kw,
        )
        s.add(cat)
        await s.commit()
        return str(cat.id)


async def mutate_tx(tx_id: uuid.UUID, **values: Any) -> None:
    async with get_database().sessionmaker() as s:
        await s.execute(update(Transaction).where(Transaction.id == tx_id).values(**values))
        await s.commit()


async def set_timezone(who: Actor, tz: str) -> None:
    async with get_database().sessionmaker() as s:
        await s.execute(update(User).where(User.id == who.uid).values(timezone=tz))
        await s.commit()


async def add_goal(
    owner: Actor, name: str, target: str, *, family_id: str | None = None, **kw: Any
) -> uuid.UUID:
    async with get_database().sessionmaker() as s:
        goal = SavingsGoal(
            owner_user_id=owner.uid,
            **_scope(family_id),
            name=name,
            target_amount=Decimal(target),
            **kw,
        )
        s.add(goal)
        await s.commit()
        return goal.id


async def contribute(
    owner: Actor, goal_id: uuid.UUID, amount: str, when: str, *, withdraw: bool = False
) -> None:
    async with get_database().sessionmaker() as s:
        s.add(
            SavingsContribution(
                goal_id=goal_id,
                user_id=owner.uid,
                kind=ContributionKind.WITHDRAWAL if withdraw else ContributionKind.DEPOSIT,
                amount=Decimal(amount),
                contributed_at=at(when),
            )
        )
        await s.commit()


async def add_rule(
    owner: Actor,
    name: str,
    amount: str,
    frequency: RecurrenceFrequency,
    start: date,
    *,
    family_id: str | None = None,
    **kw: Any,
) -> uuid.UUID:
    async with get_database().sessionmaker() as s:
        rule = RecurringTransaction(
            owner_user_id=owner.uid,
            **_scope(family_id),
            name=name,
            type=kw.pop("type", TransactionType.EXPENSE),
            amount=Decimal(amount),
            frequency=frequency,
            start_date=start,
            next_due_date=kw.pop("next_due_date", start),
            **kw,
        )
        s.add(rule)
        await s.commit()
        return rule.id


async def add_upi_attempt(
    owner: Actor,
    amount: str,
    status: UpiAttemptStatus,
    *,
    vpa: str = "shop@okbank",
    name: str | None = "Corner Shop",
    when: str = "2026-11-10T06:00:00Z",
    family_id: str | None = None,
) -> uuid.UUID:
    async with get_database().sessionmaker() as s:
        attempt = UpiPaymentAttempt(
            owner_user_id=owner.uid,
            **_scope(family_id),
            payee_vpa=vpa,
            payee_name=name,
            amount=Decimal(amount),
            tr_ref=uuid.uuid4().hex[:30],
            idempotency_key=uuid.uuid4().hex,
            status=status,
            initiated_at=at(when),
        )
        s.add(attempt)
        await s.commit()
        return attempt.id


async def get(client, who: Actor, path: str, **params: Any) -> dict[str, Any]:
    r = await client.get(f"{API}{path}", headers=who.h, params=params)
    assert r.status_code == 200, r.text
    return dict(r.json())


def by(items: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {i[key]: i for i in items}


async def ledger_story(who: Actor, family_id: str | None = None) -> dict[str, uuid.UUID]:
    """November: income 5000; expenses 1200 (food, card) + 800 (groceries, upi); refund 200
    (food, card); transfer 3000; adjustments +500/-100. Spending = 1800, income = 5000."""
    bank = await add_account(who, "Bank", opening="1000", family_id=family_id)
    cash = await add_account(who, "Cash", type_=AccountType.CASH, family_id=family_id)
    kw: dict[str, Any] = {"family_id": family_id, "account_id": bank}
    await add_tx(
        who,
        "5000",
        "2026-11-02T05:00:00Z",
        type_=TransactionType.INCOME,
        category_id=INCOME_CAT,
        **kw,
    )
    await add_tx(
        who,
        "1200",
        "2026-11-05T05:00:00Z",
        category_id=FOOD,
        payment_method=PaymentMethod.CARD,
        **kw,
    )
    await add_tx(
        who,
        "800",
        "2026-11-06T05:00:00Z",
        category_id=GROCERIES,
        payment_method=PaymentMethod.UPI,
        **kw,
    )
    await add_tx(
        who,
        "200",
        "2026-11-07T05:00:00Z",
        type_=TransactionType.REFUND,
        category_id=FOOD,
        payment_method=PaymentMethod.CARD,
        **kw,
    )
    await add_tx(
        who,
        "3000",
        "2026-11-08T05:00:00Z",
        type_=TransactionType.TRANSFER,
        to_account_id=cash,
        payment_method=PaymentMethod.BANK_TRANSFER,
        **kw,
    )
    await add_tx(who, "500", "2026-11-09T05:00:00Z", type_=TransactionType.ADJUSTMENT, **kw)
    await add_tx(
        who, "100", "2026-11-09T06:00:00Z", type_=TransactionType.ADJUSTMENT, sign=-1, **kw
    )
    return {"bank": bank, "cash": cash}


# --- pure helpers ------------------------------------------------------------------------


def test_resolve_range_presets():
    today = date(2026, 11, 20)  # a Friday
    assert resolve_range(None, None, None, today) == (
        "this_month",
        date(2026, 11, 1),
        date(2026, 11, 30),
    )
    assert resolve_range("today", None, None, today)[1:] == (today, today)
    assert resolve_range("this_week", None, None, today)[1:] == (
        date(2026, 11, 16),
        date(2026, 11, 22),
    )
    assert resolve_range("last_month", None, None, date(2026, 1, 5))[1:] == (
        date(2025, 12, 1),
        date(2025, 12, 31),
    )
    assert resolve_range(None, date(2026, 1, 1), date(2026, 1, 31), today)[0] == "custom"
    assert resolve_range(None, None, None, today, default_months=12)[1:] == (
        date(2025, 12, 1),
        date(2026, 11, 30),
    )


@pytest.mark.parametrize(
    ("preset", "frm", "to", "code"),
    [
        ("this_month", date(2026, 1, 1), date(2026, 1, 2), "range_conflict"),
        ("custom", date(2026, 1, 1), None, "range_required"),
        (None, None, date(2026, 1, 1), "range_required"),
        (None, date(2026, 2, 1), date(2026, 1, 1), "range_invalid"),
        (None, date(2000, 1, 1), date(2026, 1, 1), "range_too_large"),
    ],
)
def test_resolve_range_errors(preset, frm, to, code):
    from app.core.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError) as exc:
        resolve_range(preset, frm, to, date(2026, 11, 20))
    assert exc.value.code == code


def test_csv_cells_are_formula_safe():
    for bad in ("=1+1", "+1", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        assert csv_cell(bad) == "'" + bad
    assert csv_cell("Groceries") == "Groceries"
    assert csv_cell(Decimal("-50")) == "-50.00"  # numbers we generate stay numeric
    assert csv_cell(None) == ""
    text = "".join(iter_csv(["a", "b"], [["=cmd", Decimal("1.5")]]))
    assert text == "a,b\r\n'=cmd,1.50\r\n"


# --- accounting rules --------------------------------------------------------------------


async def test_transfers_and_adjustments_never_count_in_any_report(client):
    """Critical scenario 6."""
    asha = await user(client, "asha")
    await ledger_story(asha)

    spending = await get(client, asha, "/reports/spending", granularity="month", **NOV)
    assert spending["total"] == "1800.00"
    assert spending["meta"]["currency"] == "INR"
    assert [i["amount"] for i in spending["items"]] == ["1800.00"]

    ive = await get(client, asha, "/reports/income-vs-expense", **NOV)
    assert (ive["income"], ive["expense"], ive["net"]) == ("5000.00", "1800.00", "3200.00")
    assert ive["savings_rate"] == 64.0

    cats = await get(client, asha, "/reports/categories", **NOV)
    assert cats["total"] == "1800.00"
    slices = by(cats["items"], "name")
    assert set(slices) == {"Food & Dining", "Groceries"}  # nothing uncategorised (transfers)
    assert slices["Food & Dining"]["amount"] == "1000.00"  # 1200 - 200 refund
    assert slices["Food & Dining"]["percent"] == 55.6

    income_cats = await get(client, asha, "/reports/categories", kind="income", **NOV)
    assert income_cats["total"] == "5000.00"

    methods = await get(client, asha, "/reports/payment-methods", **NOV)
    assert methods["total"] == "1800.00"
    assert {m["payment_method"]: m["amount"] for m in methods["items"]} == {
        "card": "1000.00",
        "upi": "800.00",
    }

    pvf = await get(client, asha, "/reports/personal-vs-family", **NOV)
    assert (pvf["personal"], pvf["family"]) == ("1800.00", "0.00")


async def test_transfers_never_count_in_family_reports(client):
    owner = await user(client, "owner")
    member = await user(client, "member")
    fid = await create_family(client, owner)
    await join(client, owner, fid, member)
    await ledger_story(member, family_id=fid)

    members = await get(client, owner, "/reports/members", family_id=fid, **NOV)
    assert members["total"] == "1800.00"
    rows = by(members["items"], "full_name")
    assert rows["Member"]["expense"] == "1800.00"
    assert rows["Member"]["income"] == "5000.00"
    assert rows["Member"]["count"] == 3  # 2 expenses + 1 refund
    spending = await get(client, owner, "/reports/spending", family_id=fid, **NOV)
    assert spending["total"] == "1800.00"
    ive = await get(client, owner, "/reports/income-vs-expense", family_id=fid, **NOV)
    assert (ive["income"], ive["expense"]) == ("5000.00", "1800.00")


async def test_refunds_reduce_category_spend_and_drill_down(client):
    asha = await user(client, "asha")
    dining_out = await add_category(asha, "Dining Out", parent=FOOD)
    await add_tx(asha, "400", "2026-11-03T06:00:00Z", category_id=dining_out)
    await add_tx(asha, "100", "2026-11-04T06:00:00Z", category_id=FOOD)
    await add_tx(
        asha, "150", "2026-11-05T06:00:00Z", category_id=dining_out, type_=TransactionType.REFUND
    )
    await add_tx(asha, "300", "2026-11-05T06:00:00Z", category_id=RENT)
    await add_tx(asha, "50", "2026-11-05T06:00:00Z")  # uncategorised

    cats = await get(client, asha, "/reports/categories", **NOV)
    slices = by(cats["items"], "name")
    assert cats["total"] == "700.00"
    assert slices["Food & Dining"]["amount"] == "350.00"  # 400 + 100 - 150, rolled up
    assert slices["Food & Dining"]["has_children"] is True
    assert slices["Rent"]["has_children"] is False
    assert slices["Uncategorized"]["category_id"] is None
    assert [i["name"] for i in cats["items"]] == ["Food & Dining", "Rent", "Uncategorized"]

    drill = await get(client, asha, "/reports/categories", parent_id=FOOD, **NOV)
    assert drill["parent"]["name"] == "Food & Dining"
    assert drill["total"] == "350.00"
    assert {i["name"]: i["amount"] for i in drill["items"]} == {
        "Dining Out": "250.00",
        "Food & Dining": "100.00",
    }


async def test_pending_failed_cancelled_and_deleted_rows_are_excluded(client):
    asha = await user(client, "asha")
    await add_tx(asha, "100", "2026-11-03T06:00:00Z", category_id=FOOD)
    for status in (
        TransactionStatus.PENDING,
        TransactionStatus.FAILED,
        TransactionStatus.CANCELLED,
    ):
        await add_tx(asha, "1000", "2026-11-03T06:00:00Z", category_id=FOOD, status=status)
    gone = await add_tx(asha, "1000", "2026-11-03T06:00:00Z", category_id=FOOD)
    await mutate_tx(gone, deleted_at=at("2026-11-04T00:00:00Z"))
    await add_tx(
        asha,
        "999",
        "2026-11-03T06:00:00Z",
        type_=TransactionType.INCOME,
        status=TransactionStatus.PENDING,
    )

    assert (await get(client, asha, "/reports/spending", **NOV))["total"] == "100.00"
    assert (await get(client, asha, "/reports/categories", **NOV))["total"] == "100.00"
    ive = await get(client, asha, "/reports/income-vs-expense", **NOV)
    assert (ive["income"], ive["expense"]) == ("0.00", "100.00")
    assert (await get(client, asha, "/reports/payment-methods", **NOV))["items"] == [
        {"payment_method": "unspecified", "amount": "100.00", "count": 1, "percent": 100.0}
    ]


# --- dates ---------------------------------------------------------------------------------


async def test_timezone_bucketing_uses_the_callers_local_date(client):
    asha = await user(client, "asha")  # default timezone Asia/Kolkata (UTC+5:30)
    await add_tx(asha, "10", "2026-11-20T18:00:00Z")  # 23:30 IST on the 20th
    await add_tx(asha, "20", "2026-11-20T18:45:00Z")  # 00:15 IST on the 21st
    await add_tx(asha, "40", "2026-11-19T18:29:00Z")  # 23:59 IST on the 19th (outside)

    r = await get(
        client,
        asha,
        "/reports/spending",
        granularity="day",
        **{"from": "2026-11-20", "to": "2026-11-21"},
    )
    assert r["meta"]["timezone"] == "Asia/Kolkata"
    assert [(i["period"], i["amount"]) for i in r["items"]] == [
        ("2026-11-20", "10.00"),
        ("2026-11-21", "20.00"),
    ]

    await set_timezone(asha, "UTC")
    r = await get(
        client,
        asha,
        "/reports/spending",
        granularity="day",
        **{"from": "2026-11-20", "to": "2026-11-21"},
    )
    assert [i["amount"] for i in r["items"]] == ["30.00", "0.00"]


async def test_series_are_zero_filled(client):
    asha = await user(client, "asha")
    await add_tx(asha, "70", "2026-11-10T06:00:00Z")

    days = await get(client, asha, "/reports/spending", granularity="day", **NOV)
    assert len(days["items"]) == 30
    assert sum(Decimal(i["amount"]) for i in days["items"]) == Decimal("70")
    assert days["items"][0] == {
        "period": "2026-11-01",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "amount": "0.00",
    }
    assert days["average"] == "2.33"

    # 1 Nov 2026 is a Sunday and 30 Nov a Monday: six Monday-Sunday weeks, clipped to range.
    weeks = await get(client, asha, "/reports/spending", granularity="week", **NOV)
    assert [i["period"] for i in weeks["items"]] == [
        "2026-W44",
        "2026-W45",
        "2026-W46",
        "2026-W47",
        "2026-W48",
        "2026-W49",
    ]
    assert weeks["items"][0]["start_date"] == "2026-11-01"
    assert weeks["items"][-1]["end_date"] == "2026-11-30"
    assert weeks["items"][2]["amount"] == "70.00"

    months = await get(
        client, asha, "/reports/income-vs-expense", **{"from": "2026-10-15", "to": "2027-01-10"}
    )
    assert [i["month"] for i in months["items"]] == ["2026-10", "2026-11", "2026-12", "2027-01"]
    assert [i["expense"] for i in months["items"]] == ["0.00", "70.00", "0.00", "0.00"]

    years = await get(
        client,
        asha,
        "/reports/spending",
        granularity="year",
        **{"from": "2025-06-01", "to": "2026-12-31"},
    )
    assert [(i["period"], i["amount"]) for i in years["items"]] == [
        ("2025", "0.00"),
        ("2026", "70.00"),
    ]

    r = await client.get(
        f"{API}/reports/spending",
        headers=asha.h,
        params={"granularity": "day", "from": "2020-01-01", "to": "2026-01-01"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "too_many_buckets"


async def test_month_reports_default_to_last_n_months(client):
    asha = await user(client, "asha")
    r = await get(client, asha, "/reports/income-vs-expense", months=3)
    assert len(r["items"]) == 3
    assert r["meta"]["range"]["preset"] == "custom"
    r = await get(client, asha, "/reports/personal-vs-family")
    assert len(r["items"]) == 6
    r = await get(client, asha, "/reports/spending")
    assert r["meta"]["range"]["preset"] == "this_month"


# --- members / personal vs family ------------------------------------------------------------


async def test_members_report_breaks_family_spending_down_per_member(client):
    owner = await user(client, "owner")
    member = await user(client, "member")
    viewer = await user(client, "viewer")
    fid = await create_family(client, owner)
    await join(client, owner, fid, member)
    await join(client, owner, fid, viewer, "viewer")

    await add_tx(owner, "300", "2026-11-03T06:00:00Z", family_id=fid, category_id=FOOD)
    await add_tx(member, "200", "2026-11-03T06:00:00Z", family_id=fid, category_id=FOOD)
    await add_tx(
        member,
        "50",
        "2026-11-04T06:00:00Z",
        family_id=fid,
        category_id=FOOD,
        type_=TransactionType.REFUND,
    )
    await add_tx(owner, "9999", "2026-11-03T06:00:00Z")  # owner's personal row: not family

    r = await get(client, viewer, "/reports/members", family_id=fid, **NOV)
    assert r["total"] == "450.00"
    assert [(m["full_name"], m["expense"], m["percent"]) for m in r["items"]] == [
        ("Owner", "300.00", 66.7),
        ("Member", "150.00", 33.3),
        ("Viewer", "0.00", 0.0),
    ]
    assert all(m["is_active_member"] for m in r["items"])

    # A member who left keeps their history (flagged as no longer active).
    rr = await client.post(f"{API}/families/{fid}/leave", headers=member.h)
    assert rr.status_code in (200, 204), rr.text
    r = await get(client, owner, "/reports/members", family_id=fid, **NOV)
    left = by(r["items"], "full_name")["Member"]
    assert left["is_active_member"] is False
    assert left["expense"] == "150.00"

    r = await client.get(f"{API}/reports/members", headers=owner.h, params=NOV)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "family_scope_required"


async def test_personal_vs_family_counts_only_my_family_rows(client):
    asha = await user(client, "asha")
    ravi = await user(client, "ravi")
    fid = await create_family(client, asha, "Home")
    await join(client, asha, fid, ravi)
    fid2 = await create_family(client, asha, "Parents")

    await add_tx(asha, "100", "2026-10-10T06:00:00Z")
    await add_tx(asha, "250", "2026-11-10T06:00:00Z")
    await add_tx(asha, "400", "2026-11-11T06:00:00Z", family_id=fid)
    await add_tx(asha, "60", "2026-11-12T06:00:00Z", family_id=fid2)
    await add_tx(ravi, "7000", "2026-11-11T06:00:00Z", family_id=fid)  # recorded by Ravi
    await add_tx(ravi, "8000", "2026-11-11T06:00:00Z")  # Ravi's personal

    r = await get(
        client, asha, "/reports/personal-vs-family", **{"from": "2026-10-01", "to": "2026-11-30"}
    )
    assert [(i["month"], i["personal"], i["family"]) for i in r["items"]] == [
        ("2026-10", "100.00", "0.00"),
        ("2026-11", "250.00", "460.00"),
    ]
    assert (r["personal"], r["family"]) == ("350.00", "460.00")
    assert [(f["name"], f["amount"]) for f in r["families"]] == [
        ("Home", "400.00"),
        ("Parents", "60.00"),
    ]

    rr = await client.get(
        f"{API}/reports/personal-vs-family", headers=asha.h, params={"family_id": fid}
    )
    assert rr.status_code == 422
    assert rr.json()["error"]["code"] == "personal_scope_only"


# --- isolation -------------------------------------------------------------------------------


REPORTS = [
    "spending",
    "income-vs-expense",
    "categories",
    "members",
    "payment-methods",
    "savings-trend",
    "budget-performance",
    "recurring-summary",
    "upi",
]


async def test_personal_reports_never_include_other_users_rows(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    fid = await create_family(client, asha)
    await join(client, asha, fid, bob)
    await add_tx(bob, "5000", "2026-11-05T06:00:00Z", category_id=FOOD)  # Bob's personal
    await add_tx(bob, "700", "2026-11-05T06:00:00Z", family_id=fid, category_id=FOOD)
    await add_tx(asha, "10", "2026-11-05T06:00:00Z", category_id=FOOD)
    await add_goal(bob, "Bob's car", "100000")
    await add_rule(bob, "Bob's rent", "9000", RecurrenceFrequency.MONTHLY, date(2026, 11, 1))
    await add_upi_attempt(bob, "123", UpiAttemptStatus.SUCCESS_REPORTED)

    assert (await get(client, asha, "/reports/spending", **NOV))["total"] == "10.00"
    assert (await get(client, asha, "/reports/categories", **NOV))["total"] == "10.00"
    assert (await get(client, asha, "/reports/savings-trend", **NOV))["goal_count"] == 0
    assert (await get(client, asha, "/reports/recurring-summary", **NOV))["active_rules"] == 0
    assert (await get(client, asha, "/reports/upi", **NOV))["attempts"] == 0
    pvf = await get(client, asha, "/reports/personal-vs-family", **NOV)
    assert (pvf["personal"], pvf["family"]) == ("10.00", "0.00")
    # The family view shows the shared row, never Bob's personal one.
    fam = await get(client, asha, "/reports/spending", family_id=fid, **NOV)
    assert fam["total"] == "700.00"


async def test_other_family_and_foreign_ids_are_404(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    fid_b = await create_family(client, bob, "Bob Family")
    bob_account = await add_account(bob, "Bob Bank", opening="100")
    bob_cat = await add_category(bob, "Bob Stuff")

    for name in REPORTS:
        r = await client.get(f"{API}/reports/{name}", headers=asha.h, params={"family_id": fid_b})
        assert r.status_code == 404, (name, r.text)
        r = await client.get(
            f"{API}/reports/export.csv",
            headers=asha.h,
            params={"report": name, "family_id": fid_b},
        )
        assert r.status_code == 404, (name, r.text)
    r = await client.get(f"{API}/dashboard", headers=asha.h, params={"family_id": fid_b})
    assert r.status_code == 404
    r = await client.get(
        f"{API}/reports/account-balances", headers=asha.h, params={"account_id": str(bob_account)}
    )
    assert r.status_code == 404
    r = await client.get(f"{API}/reports/categories", headers=asha.h, params={"parent_id": bob_cat})
    assert r.status_code == 404
    r = await client.get(f"{API}/reports/spending", params=NOV)
    assert r.status_code == 401


# --- account balance history -----------------------------------------------------------------


async def test_balance_history_ends_at_the_current_balance(client):
    asha = await user(client, "asha")
    await set_timezone(asha, "UTC")
    now = datetime.now(UTC)
    today = now.date()

    def days_ago(n: int, hour: int = 10) -> datetime:
        d = date.fromordinal(today.toordinal() - n)
        return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)

    bank = await add_account(asha, "Bank", opening="1000")
    cash = await add_account(asha, "Cash", type_=AccountType.CASH)
    await add_tx(asha, "300", days_ago(40), account_id=bank)  # before the window
    await add_tx(asha, "2000", days_ago(5), type_=TransactionType.INCOME, account_id=bank)
    await add_tx(asha, "150", days_ago(3), account_id=bank)
    await add_tx(
        asha,
        "500",
        days_ago(2),
        type_=TransactionType.TRANSFER,
        account_id=bank,
        to_account_id=cash,
    )
    await add_tx(
        asha,
        "100",
        days_ago(2),
        type_=TransactionType.TRANSFER,
        account_id=cash,
        to_account_id=bank,
    )
    await add_tx(asha, "25", days_ago(1), type_=TransactionType.REFUND, account_id=bank)
    await add_tx(
        asha, "10", days_ago(1), type_=TransactionType.ADJUSTMENT, sign=-1, account_id=bank
    )
    await add_tx(  # pending: not part of the balance
        asha, "999", days_ago(0, 0), account_id=bank, status=TransactionStatus.PENDING
    )

    async with get_database().sessionmaker() as s:
        current = (await ledger.account_balances(s, [bank]))[bank]
    assert current == Decimal("2165.00")  # 1000 - 300 + 2000 - 150 - 500 + 100 + 25 - 10

    r = await get(client, asha, "/reports/account-balances", account_id=str(bank), days=10)
    assert r["current_balance"] == "2165.00"
    assert len(r["items"]) == 10
    assert r["items"][-1] == {
        "date": today.isoformat(),
        "balance": "2165.00",
        "change": "0.00",
    }
    hist = {i["date"]: (i["balance"], i["change"]) for i in r["items"]}
    assert hist[days_ago(6).date().isoformat()] == ("700.00", "0.00")
    assert hist[days_ago(5).date().isoformat()] == ("2700.00", "2000.00")
    assert hist[days_ago(2).date().isoformat()] == ("2150.00", "-400.00")
    assert r["items"][0]["balance"] == "700.00"

    cash_r = await get(client, asha, "/reports/account-balances", account_id=str(cash), days=3)
    assert cash_r["items"][-1]["balance"] == "400.00"
    assert cash_r["current_balance"] == "400.00"

    csv_r = await client.get(
        f"{API}/reports/export.csv",
        headers=asha.h,
        params={"report": "account-balances", "account_id": str(bank), "days": 3},
    )
    assert csv_r.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_r.text)))
    assert rows[0] == ["date", "account", "balance", "change", "currency"]
    assert rows[-1][2] == "2165.00"


async def test_family_account_balance_is_visible_to_members_only(client):
    owner = await user(client, "owner")
    member = await user(client, "member")
    outsider = await user(client, "outsider")
    fid = await create_family(client, owner)
    await join(client, owner, fid, member)
    joint = await add_account(owner, "Joint", opening="500", family_id=fid)
    await add_tx(member, "120", "2026-11-05T06:00:00Z", family_id=fid, account_id=joint)

    r = await get(client, member, "/reports/account-balances", account_id=str(joint), days=1)
    assert r["current_balance"] == "380.00"
    assert r["meta"]["scope"] == "family"
    rr = await client.get(
        f"{API}/reports/account-balances", headers=outsider.h, params={"account_id": str(joint)}
    )
    assert rr.status_code == 404


# --- savings / recurring / budgets -----------------------------------------------------------


async def test_savings_trend_is_cumulative_across_goals(client):
    asha = await user(client, "asha")
    g1 = await add_goal(asha, "Laptop", "80000")
    g2 = await add_goal(asha, "Trip", "20000")
    await add_goal(asha, "Dollars", "500", currency="USD")
    await contribute(asha, g1, "1000", "2026-08-15T06:00:00Z")  # before the range
    await contribute(asha, g1, "500", "2026-10-15T06:00:00Z")
    await contribute(asha, g2, "300", "2026-10-31T19:00:00Z")  # 1 Nov 00:30 IST
    await contribute(asha, g1, "200", "2026-11-20T06:00:00Z", withdraw=True)
    await contribute(asha, g2, "999", "2027-01-05T06:00:00Z")  # after the range

    r = await get(
        client, asha, "/reports/savings-trend", **{"from": "2026-09-01", "to": "2026-11-30"}
    )
    assert r["goal_count"] == 2  # the USD goal is reported under currency=USD
    assert r["target_total"] == "100000.00"
    assert r["current_total"] == "2599.00"
    assert [(i["month"], i["deposits"], i["withdrawals"], i["balance"]) for i in r["items"]] == [
        ("2026-09", "0.00", "0.00", "1000.00"),
        ("2026-10", "500.00", "0.00", "1500.00"),
        ("2026-11", "300.00", "200.00", "1600.00"),
    ]
    assert r["meta"]["currencies"] == []  # no transactions/accounts yet

    usd = await get(client, asha, "/reports/savings-trend", currency="USD", **NOV)
    assert (usd["meta"]["currency"], usd["goal_count"]) == ("USD", 1)


async def test_recurring_summary_normalises_to_monthly(client):
    asha = await user(client, "asha")
    start = date(2026, 1, 1)
    await add_rule(
        asha, "Rent", "15000", RecurrenceFrequency.MONTHLY, start, category_id=uuid.UUID(RENT)
    )
    await add_rule(
        asha, "Milk", "100", RecurrenceFrequency.WEEKLY, start, category_id=uuid.UUID(GROCERIES)
    )
    await add_rule(asha, "Insurance", "24000", RecurrenceFrequency.YEARLY, start, interval_count=2)
    await add_rule(asha, "Paper", "10", RecurrenceFrequency.DAILY, start)
    await add_rule(
        asha,
        "Salary",
        "90000",
        RecurrenceFrequency.MONTHLY,
        start,
        type=TransactionType.INCOME,
        category_id=uuid.UUID(INCOME_CAT),
    )
    await add_rule(asha, "Paused gym", "2000", RecurrenceFrequency.MONTHLY, start, is_paused=True)
    await add_rule(
        asha,
        "Old plan",
        "500",
        RecurrenceFrequency.MONTHLY,
        start,
        end_date=date(2026, 3, 1),
        next_due_date=date(2026, 4, 1),
    )

    r = await get(client, asha, "/reports/recurring-summary")
    assert r["active_rules"] == 5
    # 15000 + 100*52/12 + 24000/24 + 10*365/12 = 15000 + 433.33 + 1000 + 304.17
    assert r["monthly_expense"] == "16737.50"
    assert r["monthly_income"] == "90000.00"
    assert r["monthly_transfer"] == "0.00"
    cats = {c["name"]: c["monthly_amount"] for c in r["by_category"]}
    assert cats == {
        "Rent": "15000.00",
        "Uncategorized": "1304.17",
        "Groceries": "433.33",
    }
    freq = {f["frequency"]: (f["monthly_amount"], f["rule_count"]) for f in r["by_frequency"]}
    assert freq["weekly"] == ("433.33", 1)
    assert freq["yearly"] == ("1000.00", 1)
    assert r["rules"][0]["name"] == "Salary"


async def test_budget_performance_uses_budget_history(client):
    asha = await user(client, "asha")
    r = await client.post(
        f"{API}/budgets",
        headers=asha.h,
        json={"name": "Monthly", "amount": "1000.00", "start_date": "2026-01-01"},
    )
    assert r.status_code == 201, r.text
    await add_tx(asha, "1500", datetime.now(UTC), category_id=FOOD)
    await add_tx(
        asha,
        "1",
        datetime.now(UTC),
        type_=TransactionType.TRANSFER,
        account_id=await add_account(asha, "A"),
        to_account_id=await add_account(asha, "B"),
    )

    rep = await get(client, asha, "/reports/budget-performance", periods=3)
    (b,) = rep["budgets"]
    assert b["name"] == "Monthly"
    assert len(b["items"]) == 3
    current = b["items"][-1]
    assert current["is_current"] is True
    assert (current["spent"], current["status"]) == ("1500.00", "exceeded")
    assert b["exceeded_count"] == 1
    assert b["average_percent"] == 50.0


# --- UPI -----------------------------------------------------------------------------------------


async def test_upi_report(client):
    asha = await user(client, "asha")
    empty = await get(client, asha, "/reports/upi", **NOV)
    assert empty["attempts"] == 0
    assert empty["success_reported_rate"] == 0.0
    assert empty["by_status"] == [] and empty["top_payees"] == []

    ok1 = await add_upi_attempt(asha, "300", UpiAttemptStatus.SUCCESS_REPORTED)
    ok2 = await add_upi_attempt(
        asha, "200", UpiAttemptStatus.SUCCESS_REPORTED, vpa="cafe@ok", name="Cafe"
    )
    await add_upi_attempt(asha, "100", UpiAttemptStatus.SUCCESS_REPORTED)
    await add_upi_attempt(asha, "50", UpiAttemptStatus.FAILURE_REPORTED)
    pend = await add_upi_attempt(asha, "75", UpiAttemptStatus.LAUNCHED)
    await add_upi_attempt(
        asha, "999", UpiAttemptStatus.SUCCESS_REPORTED, when="2026-12-02T06:00:00Z"
    )

    up = {"source": TransactionSource.UPI_INTENT, "payment_method": PaymentMethod.UPI}
    await add_tx(asha, "300", "2026-11-10T06:00:00Z", category_id=FOOD, upi_attempt_id=ok1, **up)
    await add_tx(
        asha, "200", "2026-11-10T06:00:00Z", category_id=GROCERIES, upi_attempt_id=ok2, **up
    )
    await add_tx(
        asha,
        "75",
        "2026-11-10T06:00:00Z",
        category_id=FOOD,
        upi_attempt_id=pend,
        status=TransactionStatus.PENDING,
        **up,
    )

    r = await get(client, asha, "/reports/upi", **NOV)
    assert r["attempts"] == 5
    assert r["attempted_amount"] == "725.00"
    assert (r["success_reported"], r["success_reported_amount"]) == (3, "600.00")
    assert r["success_reported_rate"] == 60.0
    assert r["open_attempts"] == 1
    assert r["pending_confirmation"] == 1
    assert r["confirmed_spend"] == "500.00"  # the pending one is not spent yet
    assert {s["status"]: s["count"] for s in r["by_status"]} == {
        "launched": 1,
        "success_reported": 3,
        "failure_reported": 1,
    }
    assert [(p["payee_vpa"], p["count"], p["amount"]) for p in r["top_payees"]] == [
        ("shop@okbank", 2, "400.00"),
        ("cafe@ok", 1, "200.00"),
    ]
    assert {c["name"]: c["amount"] for c in r["by_category"]} == {
        "Food & Dining": "300.00",
        "Groceries": "200.00",
    }


# --- CSV export ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [*REPORTS, "personal-vs-family"])
async def test_every_report_exports_csv(client, name):
    asha = await user(client, "asha")
    fid = await create_family(client, asha)
    await add_tx(asha, "10", "2026-11-05T06:00:00Z", category_id=FOOD, family_id=fid)
    await add_tx(asha, "10", "2026-11-05T06:00:00Z", category_id=FOOD)
    params: dict[str, Any] = {"report": name, **NOV}
    if name == "members":
        params["family_id"] = fid
    r = await client.get(f"{API}/reports/export.csv", headers=asha.h, params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith(f'attachment; filename="{name}-')
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][-1] == "currency"


async def test_csv_export_escapes_formula_cells(client):
    asha = await user(client, "asha")
    evil = await add_category(asha, '=HYPERLINK("http://evil.example","x")')
    at_cat = await add_category(asha, "@SUM(A1)")
    refund_only = await add_category(asha, "-Refunds")
    await add_tx(asha, "40", "2026-11-05T06:00:00Z", category_id=evil)
    await add_tx(asha, "30", "2026-11-05T06:00:00Z", category_id=at_cat)
    await add_tx(
        asha, "5", "2026-11-05T06:00:00Z", category_id=refund_only, type_=TransactionType.REFUND
    )
    r = await client.get(
        f"{API}/reports/export.csv", headers=asha.h, params={"report": "categories", **NOV}
    )
    assert r.status_code == 200
    rows = {row[1]: row for row in csv.reader(io.StringIO(r.text))}
    assert '\'=HYPERLINK("http://evil.example","x")' in rows
    assert "'@SUM(A1)" in rows
    assert rows["'-Refunds"][2] == "-5.00"  # generated numbers are not escaped
    assert all(not k.startswith(("=", "@", "+")) for k in rows)


async def test_report_validation(client):
    asha = await user(client, "asha")
    for params in (
        {"preset": "yesterday"},
        {"granularity": "hour"},
        {"from": "2026-11-30", "to": "2026-11-01"},
        {"preset": "this_month", "from": "2026-11-01", "to": "2026-11-02"},
        {"currency": "RUPEES"},
    ):
        r = await client.get(f"{API}/reports/spending", headers=asha.h, params=params)
        assert r.status_code == 422, params
    r = await client.get(f"{API}/reports/export.csv", headers=asha.h, params={"report": "nope"})
    assert r.status_code == 422
    r = await client.get(f"{API}/reports/account-balances", headers=asha.h)
    assert r.status_code == 422
