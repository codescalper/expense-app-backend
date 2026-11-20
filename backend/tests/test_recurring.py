"""Recurring rules API, generation engine, payment status and reminders (spec §9, §17.8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.db.session import get_database
from app.models import (
    Account,
    AccountType,
    CategoryKind,
    Notification,
    NotificationCategory,
    Payee,
    RecordScope,
    RecurringTransaction,
    Transaction,
    TransactionCategory,
    TransactionStatus,
    TransactionType,
    User,
)
from app.repositories.recurring import RecurringRepository
from app.services import ledger
from app.services.recurring import RecurringService
from tests.conftest import BUILTIN_CATEGORIES, bearer, register_verified

API = "/api/v1"
IST = ZoneInfo("Asia/Kolkata")
# 2026-11-20 11:30 in Asia/Kolkata (the default users.timezone).
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
EXPENSE_CAT = next(c["id"] for c in BUILTIN_CATEGORIES if c["kind"] == "expense")
INCOME_CAT = next(c["id"] for c in BUILTIN_CATEGORIES if c["kind"] == "income")


# --- helpers ---------------------------------------------------------------------------


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


async def household(client) -> tuple[str, dict[str, Actor]]:
    people = {n: await user(client, n) for n in ("owner", "admin", "member", "member2", "viewer")}
    fid = await create_family(client, people["owner"])
    for name, role in (
        ("admin", "admin"),
        ("member", "member"),
        ("member2", "member"),
        ("viewer", "viewer"),
    ):
        await join(client, people["owner"], fid, people[name], role)
    return fid, people


async def add_account(owner: Actor, family_id: str | None = None, name: str = "Bank") -> str:
    async with get_database().sessionmaker() as s:
        acc = Account(
            owner_user_id=owner.uid,
            scope=RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
            family_id=uuid.UUID(family_id) if family_id else None,
            name=name,
            type=AccountType.BANK,
        )
        s.add(acc)
        await s.commit()
        return str(acc.id)


async def add_payee(owner: Actor, family_id: str | None = None) -> str:
    async with get_database().sessionmaker() as s:
        payee = Payee(
            owner_user_id=owner.uid,
            scope=RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
            family_id=uuid.UUID(family_id) if family_id else None,
            name="Landlord",
        )
        s.add(payee)
        await s.commit()
        return str(payee.id)


async def add_category(owner: Actor, kind: CategoryKind, family_id: str | None = None) -> str:
    async with get_database().sessionmaker() as s:
        cat = TransactionCategory(
            owner_user_id=None if family_id else owner.uid,
            family_id=uuid.UUID(family_id) if family_id else None,
            created_by_user_id=owner.uid,
            name=f"Custom {kind.value}",
            kind=kind,
        )
        s.add(cat)
        await s.commit()
        return str(cat.id)


async def create_rule(client, actor: Actor, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Rent",
        "type": "expense",
        "amount": "15000.00",
        "frequency": "monthly",
        "start_date": "2026-11-20",
        **overrides,
    }
    r = await client.post(f"{API}/recurring", headers=actor.h, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def generate(now: datetime = NOW) -> int:
    async with get_database().sessionmaker() as s:
        return await RecurringService.generate_due(s, now)


async def remind(now: datetime = NOW) -> int:
    async with get_database().sessionmaker() as s:
        return await RecurringService.send_reminders(s, now)


async def generated(rule_id: str) -> list[Transaction]:
    async with get_database().sessionmaker() as s:
        rows = await s.scalars(
            select(Transaction)
            .where(Transaction.recurring_id == uuid.UUID(rule_id))
            .order_by(Transaction.period_key)
        )
        return list(rows)


async def rule_row(rule_id: str) -> RecurringTransaction:
    async with get_database().sessionmaker() as s:
        row = await s.get(RecurringTransaction, uuid.UUID(rule_id))
        assert row is not None
        return row


async def set_rule(rule_id: str, **values: Any) -> None:
    async with get_database().sessionmaker() as s:
        await s.execute(
            update(RecurringTransaction)
            .where(RecurringTransaction.id == uuid.UUID(rule_id))
            .values(**values)
        )
        await s.commit()


async def notifications(
    who: Actor, category: NotificationCategory | None = None
) -> list[Notification]:
    async with get_database().sessionmaker() as s:
        stmt = select(Notification).where(Notification.user_id == who.uid)
        if category is not None:
            stmt = stmt.where(Notification.category == category)
        return list(await s.scalars(stmt.order_by(Notification.created_at)))


# --- CRUD & validation -----------------------------------------------------------------


async def test_create_get_list_update_delete_personal_rule(client):
    asha = await user(client, "asha")
    acc = await add_account(asha)
    payee = await add_payee(asha)
    rule = await create_rule(
        client,
        asha,
        account_id=acc,
        payee_id=payee,
        category_id=EXPENSE_CAT,
        amount=15000,
        description="Flat 4B",
        payment_method="upi",
        reminder_days_before=3,
    )
    assert rule["scope"] == "personal" and rule["family_id"] is None
    assert rule["amount"] == "15000.00" and rule["currency"] == "INR"
    assert rule["next_due_date"] == "2026-11-20" and rule["status"] == "active"
    assert rule["interval_count"] == 1 and rule["auto_generate"] is True
    assert rule["latest_occurrence"] is None and rule["last_generated_at"] is None

    got = await client.get(f"{API}/recurring/{rule['id']}", headers=asha.h)
    assert got.status_code == 200 and got.json()["name"] == "Rent"

    listed = (await client.get(f"{API}/recurring", headers=asha.h)).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == rule["id"]
    assert listed["has_more"] is False and listed["limit"] == 50

    r = await client.patch(
        f"{API}/recurring/{rule['id']}",
        headers=asha.h,
        json={"amount": "16000.50", "end_date": "2027-10-20", "payee_id": None},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amount"] == "16000.50" and body["end_date"] == "2027-10-20"
    assert body["payee_id"] is None and body["next_due_date"] == "2026-11-20"

    r = await client.patch(f"{API}/recurring/{rule['id']}", headers=asha.h, json={"end_date": None})
    assert r.status_code == 200 and r.json()["end_date"] is None

    for bad in ({"name": None}, {"amount": "0"}, {"bogus": 1}, {"scope": "family"}):
        r = await client.patch(f"{API}/recurring/{rule['id']}", headers=asha.h, json=bad)
        assert r.status_code == 422, bad

    r = await client.delete(f"{API}/recurring/{rule['id']}", headers=asha.h)
    assert r.status_code == 204
    assert (await client.get(f"{API}/recurring/{rule['id']}", headers=asha.h)).status_code == 404
    assert (await client.get(f"{API}/recurring", headers=asha.h)).json()["total"] == 0
    assert (await client.get(f"{API}/recurring")).status_code == 401


async def test_create_validation(client):
    asha = await user(client, "asha")
    ravi = await user(client, "ravi")
    acc = await add_account(asha)
    acc2 = await add_account(asha, name="Cash")
    ravi_acc = await add_account(ravi)
    fid = await create_family(client, asha)
    fam_acc = await add_account(asha, family_id=fid)
    my_income_cat = await add_category(asha, CategoryKind.INCOME)
    ravi_cat = await add_category(ravi, CategoryKind.EXPENSE)
    ravi_payee = await add_payee(ravi)
    base = {"name": "Rent", "type": "expense", "frequency": "monthly", "start_date": "2026-11-01"}

    schema_errors = [
        {**base, "amount": "0"},
        {**base, "amount": "-5"},
        {**base, "amount": "10.005"},
        {**base, "amount": "1000000000000"},
        {**base, "amount": "10", "type": "refund"},
        {**base, "amount": "10", "frequency": "hourly"},
        {**base, "amount": "10", "interval_count": 0},
        {**base, "amount": "10", "end_date": "2026-10-31"},
        {**base, "amount": "10", "reminder_days_before": -1},
        {**base, "amount": "10", "scope": "family"},
        {**base, "amount": "10", "family_id": fid},
        {**base, "amount": "10", "to_account_id": acc2},
        {**base, "amount": "10", "type": "transfer", "account_id": acc},
        {**base, "amount": "10", "type": "transfer", "account_id": acc, "to_account_id": acc},
        {
            **base,
            "amount": "10",
            "type": "transfer",
            "account_id": acc,
            "to_account_id": acc2,
            "category_id": EXPENSE_CAT,
        },
        {**base, "amount": "10", "name": ""},
        {**base, "amount": "10", "unknown": True},
        {**base, "amount": "10", "currency": "inr"},
    ]
    for body in schema_errors:
        r = await client.post(f"{API}/recurring", headers=asha.h, json=body)
        assert r.status_code == 422, body

    reference_errors = [
        ({"account_id": ravi_acc}, "account_id"),  # someone else's account
        ({"account_id": fam_acc}, "account_id"),  # family account on a personal rule
        ({"account_id": str(uuid.uuid4())}, "account_id"),
        ({"category_id": INCOME_CAT}, "category_id"),  # wrong kind
        ({"category_id": my_income_cat}, "category_id"),
        ({"category_id": ravi_cat}, "category_id"),
        ({"payee_id": ravi_payee}, "payee_id"),
    ]
    for extra, field in reference_errors:
        r = await client.post(
            f"{API}/recurring", headers=asha.h, json={**base, "amount": "10", **extra}
        )
        assert r.status_code == 422, extra
        assert r.json()["error"]["code"] == "validation_error"
        assert [d["field"] for d in r.json()["error"]["details"]] == [field]

    ok = await create_rule(
        client,
        asha,
        name="Salary",
        type="income",
        amount="90000",
        category_id=my_income_cat,
        account_id=acc,
    )
    assert ok["type"] == "income"
    transfer = await create_rule(
        client,
        asha,
        name="Move to savings",
        type="transfer",
        amount="5000",
        account_id=acc,
        to_account_id=acc2,
        frequency="weekly",
        interval_count=2,
    )
    assert transfer["to_account_id"] == acc2 and transfer["interval_count"] == 2
    # merged validation on update: an expense cannot keep a to_account
    r = await client.patch(
        f"{API}/recurring/{transfer['id']}", headers=asha.h, json={"type": "expense"}
    )
    assert r.status_code == 422
    # changing type re-checks the existing category's kind
    r = await client.patch(f"{API}/recurring/{ok['id']}", headers=asha.h, json={"type": "expense"})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"] == "category_id"


async def test_isolation_between_users_and_families(client):
    asha = await user(client, "asha")
    ravi = await user(client, "ravi")
    asha_fid = await create_family(client, asha, "Asha fam")
    ravi_fid = await create_family(client, ravi, "Ravi fam")
    mine = await create_rule(client, asha)
    fam_rule = await create_rule(client, asha, scope="family", family_id=asha_fid)
    await generate()
    tx_id = str((await generated(mine["id"]))[0].id)

    # Ravi sees nothing of Asha's, personal or family.
    for rule_id in (mine["id"], fam_rule["id"]):
        assert (await client.get(f"{API}/recurring/{rule_id}", headers=ravi.h)).status_code == 404
        r = await client.patch(f"{API}/recurring/{rule_id}", headers=ravi.h, json={"name": "x"})
        assert r.status_code == 404
        for action in ("pause", "resume"):
            r = await client.post(f"{API}/recurring/{rule_id}/{action}", headers=ravi.h)
            assert r.status_code == 404
        r = await client.get(f"{API}/recurring/{rule_id}/occurrences", headers=ravi.h)
        assert r.status_code == 404
        assert (
            await client.delete(f"{API}/recurring/{rule_id}", headers=ravi.h)
        ).status_code == 404
    for action in ("mark-paid", "skip", "unmark"):
        r = await client.post(f"{API}/recurring/occurrences/{tx_id}/{action}", headers=ravi.h)
        assert r.status_code == 404
    assert (await client.get(f"{API}/recurring", headers=ravi.h)).json()["total"] == 0
    r = await client.get(f"{API}/recurring", headers=ravi.h, params={"family_id": asha_fid})
    assert r.status_code == 404
    r = await client.get(
        f"{API}/recurring/upcoming", headers=ravi.h, params={"family_id": asha_fid}
    )
    assert r.status_code == 404
    assert (await client.get(f"{API}/recurring/upcoming", headers=ravi.h)).json() == []
    # Nor can Asha create in Ravi's family.
    r = await client.post(
        f"{API}/recurring",
        headers=asha.h,
        json={
            "name": "x",
            "type": "expense",
            "amount": "1",
            "frequency": "daily",
            "start_date": "2026-11-20",
            "scope": "family",
            "family_id": ravi_fid,
        },
    )
    assert r.status_code == 404

    # Personal list never includes family rules and vice versa.
    personal = (await client.get(f"{API}/recurring", headers=asha.h)).json()["items"]
    assert [r["id"] for r in personal] == [mine["id"]]
    family = (
        await client.get(f"{API}/recurring", headers=asha.h, params={"family_id": asha_fid})
    ).json()["items"]
    assert [r["id"] for r in family] == [fam_rule["id"]]
    # Unknown occurrence ids and non-recurring transactions are 404.
    r = await client.post(f"{API}/recurring/occurrences/{uuid.uuid4()}/skip", headers=asha.h)
    assert r.status_code == 404


async def test_family_roles(client):
    fid, p = await household(client)
    fam_acc = await add_account(p["owner"], family_id=fid)
    fam_cat = await add_category(p["owner"], CategoryKind.EXPENSE, family_id=fid)
    personal_acc = await add_account(p["member"])
    body = {
        "scope": "family",
        "family_id": fid,
        "account_id": fam_acc,
        "category_id": fam_cat,
    }
    r = await client.post(
        f"{API}/recurring",
        headers=p["viewer"].h,
        json={
            "name": "x",
            "type": "expense",
            "amount": "1",
            "frequency": "monthly",
            "start_date": "2026-11-20",
            **body,
        },
    )
    assert r.status_code == 403
    member_rule = await create_rule(client, p["member"], name="Internet", **body)
    assert member_rule["owner_user_id"] == p["member"].id and member_rule["family_id"] == fid
    admin_rule = await create_rule(client, p["admin"], name="Electricity", **body)

    # A personal account cannot be used in a family rule.
    r = await client.patch(
        f"{API}/recurring/{member_rule['id']}",
        headers=p["member"].h,
        json={"account_id": personal_acc},
    )
    assert r.status_code == 422

    # Members edit only their own rules; admins edit any; viewers none.
    r = await client.patch(
        f"{API}/recurring/{member_rule['id']}", headers=p["member"].h, json={"amount": "999"}
    )
    assert r.status_code == 200
    r = await client.patch(
        f"{API}/recurring/{admin_rule['id']}", headers=p["member"].h, json={"amount": "1"}
    )
    assert r.status_code == 403
    r = await client.post(f"{API}/recurring/{member_rule['id']}/pause", headers=p["member2"].h)
    assert r.status_code == 403
    r = await client.post(f"{API}/recurring/{member_rule['id']}/pause", headers=p["viewer"].h)
    assert r.status_code == 403
    r = await client.post(f"{API}/recurring/{member_rule['id']}/pause", headers=p["admin"].h)
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = await client.delete(f"{API}/recurring/{admin_rule['id']}", headers=p["viewer"].h)
    assert r.status_code == 403

    # Everyone in the family can read.
    listed = await client.get(f"{API}/recurring", headers=p["viewer"].h, params={"family_id": fid})
    assert listed.status_code == 200 and listed.json()["total"] == 2
    r = await client.get(
        f"{API}/recurring", headers=p["viewer"].h, params={"family_id": fid, "status": "paused"}
    )
    assert [i["id"] for i in r.json()["items"]] == [member_rule["id"]]
    r = await client.get(
        f"{API}/recurring", headers=p["viewer"].h, params={"family_id": fid, "status": "active"}
    )
    assert [i["id"] for i in r.json()["items"]] == [admin_rule["id"]]

    # Occurrence status follows the rule's write permission.
    await generate()
    tx_id = str((await generated(admin_rule["id"]))[0].id)
    for who, code in (("viewer", 403), ("member", 403), ("member2", 403), ("owner", 200)):
        r = await client.post(f"{API}/recurring/occurrences/{tx_id}/skip", headers=p[who].h)
        assert r.status_code == code, who
    family_txs = await generated(admin_rule["id"])
    assert family_txs[0].scope is RecordScope.FAMILY and str(family_txs[0].family_id) == fid
    assert family_txs[0].owner_user_id == p["admin"].uid


# --- generation engine -----------------------------------------------------------------


async def test_generate_creates_one_pending_transaction_per_due_occurrence(client):
    asha = await user(client, "asha")
    acc = await add_account(asha)
    rule = await create_rule(
        client, asha, account_id=acc, category_id=EXPENSE_CAT, payment_method="upi"
    )
    tomorrow_rule = await create_rule(client, asha, name="Gym", start_date="2026-11-21")

    assert await generate() == 1
    rows = await generated(rule["id"])
    assert len(rows) == 1
    tx = rows[0]
    assert tx.status is TransactionStatus.PENDING
    assert tx.source.value == "recurring" and tx.verification.value == "unverified"
    assert tx.period_key == "2026-11-20" and tx.type is TransactionType.EXPENSE
    assert tx.amount == Decimal("15000.00") and str(tx.account_id) == acc
    assert tx.occurred_at == datetime(2026, 11, 20, 9, 0, tzinfo=IST)
    assert tx.owner_user_id == asha.uid and tx.scope is RecordScope.PERSONAL
    assert tx.description == "Rent" and tx.payment_method.value == "upi"
    assert await generated(tomorrow_rule["id"]) == []

    row = await rule_row(rule["id"])
    assert row.next_due_date == date(2026, 12, 20) and row.last_generated_at == NOW

    # Re-running (same moment or later the same day) never duplicates.
    assert await generate() == 0
    assert await generate(NOW + timedelta(hours=10)) == 0
    assert len(await generated(rule["id"])) == 1

    # Pending never counts in totals; nothing was auto-marked paid.
    async with get_database().sessionmaker() as s:
        totals = await ledger.totals(s, Transaction.owner_user_id == asha.uid)
    assert totals["expense"] == Decimal("0.00")

    got = (await client.get(f"{API}/recurring/{rule['id']}", headers=asha.h)).json()
    assert got["next_due_date"] == "2026-12-20" and got["last_generated_at"] is not None
    assert got["latest_occurrence"]["status"] == "pending"
    assert got["latest_occurrence"]["period_key"] == "2026-11-20"
    occ = (await client.get(f"{API}/recurring/{rule['id']}/occurrences", headers=asha.h)).json()
    assert occ["total"] == 1
    assert occ["items"][0]["transaction_id"] == str(tx.id)
    assert occ["items"][0]["status"] == "pending" and occ["items"][0]["due_date"] == "2026-11-20"


async def test_generation_uses_owner_timezone(client):
    asha = await user(client, "asha")
    async with get_database().sessionmaker() as s:
        await s.execute(
            update(User).where(User.id == asha.uid).values(timezone="America/Los_Angeles")
        )
        await s.commit()
    rule = await create_rule(client, asha)
    # 03:00 UTC on Nov 20 is still Nov 19 in Los Angeles.
    assert await generate(datetime(2026, 11, 20, 3, 0, tzinfo=UTC)) == 0
    assert await generate(datetime(2026, 11, 20, 9, 0, tzinfo=UTC)) == 1
    tx = (await generated(rule["id"]))[0]
    assert tx.occurred_at == datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


async def test_concurrent_generation_never_duplicates(client):
    """Spec §17.8: a recurring transaction is never generated twice for the same period."""
    asha = await user(client, "asha")
    rules = [
        await create_rule(client, asha, name=f"Daily {i}", frequency="daily", start_date=start)
        for i, start in enumerate(["2026-11-15", "2026-11-18", "2026-11-20"])
    ]
    rules.append(await create_rule(client, asha, name="Monthly", start_date="2026-09-20"))
    expected = {rules[0]["id"]: 6, rules[1]["id"]: 3, rules[2]["id"]: 1, rules[3]["id"]: 3}

    db = get_database()
    for _ in range(3):  # several rounds of two workers racing
        async with db.sessionmaker() as s1, db.sessionmaker() as s2:
            await asyncio.gather(
                RecurringService.generate_due(s1, NOW), RecurringService.generate_due(s2, NOW)
            )
    for rule in rules:
        keys = [t.period_key for t in await generated(rule["id"])]
        assert len(keys) == expected[rule["id"]] == len(set(keys)), rule["name"]

    # A stale cursor (e.g. a worker that read the rule before another committed) still cannot
    # duplicate: the unique (recurring_id, period_key) + ON CONFLICT DO NOTHING absorbs it.
    await set_rule(rules[0]["id"], next_due_date=date(2026, 11, 15))
    async with db.sessionmaker() as s1, db.sessionmaker() as s2:
        results = await asyncio.gather(
            RecurringService.generate_due(s1, NOW), RecurringService.generate_due(s2, NOW)
        )
    assert results == [0, 0]
    assert len(await generated(rules[0]["id"])) == 6
    assert (await rule_row(rules[0]["id"])).next_due_date == date(2026, 11, 21)

    # Raw race on the same key from two sessions: exactly one insert wins.
    rule = await rule_row(rules[3]["id"])

    async def insert(session) -> uuid.UUID | None:
        tx_id = await RecurringRepository(session).insert_occurrence(
            {
                "owner_user_id": rule.owner_user_id,
                "scope": RecordScope.PERSONAL,
                "type": TransactionType.EXPENSE,
                "status": TransactionStatus.PENDING,
                "amount": Decimal("1.00"),
                "occurred_at": NOW,
                "recurring_id": rule.id,
                "period_key": "2026-12-20",
            }
        )
        await session.commit()
        return tx_id

    async with db.sessionmaker() as s1, db.sessionmaker() as s2:
        ids = await asyncio.gather(insert(s1), insert(s2))
    assert sorted(i is None for i in ids) == [False, True]
    async with db.sessionmaker() as s:
        dupes = await s.scalar(
            select(func.count()).select_from(
                select(Transaction.recurring_id, Transaction.period_key)
                .where(Transaction.recurring_id.is_not(None))
                .group_by(Transaction.recurring_id, Transaction.period_key)
                .having(func.count() > 1)
                .subquery()
            )
        )
    assert dupes == 0


async def test_catch_up_after_downtime(client):
    asha = await user(client, "asha")
    rule = await create_rule(client, asha, start_date="2026-08-31")
    assert await generate(datetime(2026, 8, 31, 6, 0, tzinfo=UTC)) == 1
    # Worker down until Nov 20: Sep 30 and Oct 31 are caught up; Nov 30 is not due yet.
    assert await generate(NOW) == 2
    keys = [t.period_key for t in await generated(rule["id"])]
    assert keys == ["2026-08-31", "2026-09-30", "2026-10-31"]
    assert (await rule_row(rule["id"])).next_due_date == date(2026, 11, 30)
    assert await generate(datetime(2026, 11, 30, 6, 0, tzinfo=UTC)) == 1
    assert [t.period_key for t in await generated(rule["id"])][-1] == "2026-11-30"

    # Catch-up is capped at 12 occurrences per rule per run and resumes on the next run.
    daily = await create_rule(client, asha, name="Milk", frequency="daily", start_date="2026-10-21")
    assert await generate() == 12
    assert await generate() == 12
    assert await generate() == 7  # Oct 21 .. Nov 20 = 31 occurrences
    assert await generate() == 0
    keys = [t.period_key for t in await generated(daily["id"])]
    assert len(keys) == 31 == len(set(keys))
    assert keys[0] == "2026-10-21" and keys[-1] == "2026-11-20"
    assert all(t.status is TransactionStatus.PENDING for t in await generated(daily["id"]))


async def test_paused_ended_disabled_and_deleted_rules_generate_nothing(client):
    asha = await user(client, "asha")
    paused = await create_rule(client, asha, name="Paused", start_date="2026-11-01")
    r = await client.post(f"{API}/recurring/{paused['id']}/pause", headers=asha.h)
    assert r.status_code == 200 and r.json()["is_paused"] is True
    ended = await create_rule(
        client,
        asha,
        name="Ended",
        frequency="weekly",
        start_date="2026-10-01",
        end_date="2026-10-15",
    )
    manual = await create_rule(
        client,
        asha,
        name="Manual",
        frequency="weekly",
        start_date="2026-10-02",
        auto_generate=False,
    )
    deleted = await create_rule(client, asha, name="Deleted", start_date="2026-11-01")
    await client.delete(f"{API}/recurring/{deleted['id']}", headers=asha.h)

    created = await generate()
    assert created == 3  # only the ended rule's Oct 1, 8 and 15 occurrences
    assert await generated(paused["id"]) == []
    assert await generated(manual["id"]) == []
    assert await generated(deleted["id"]) == []
    assert [t.period_key for t in await generated(ended["id"])] == [
        "2026-10-01",
        "2026-10-08",
        "2026-10-15",
    ]
    ended_out = (await client.get(f"{API}/recurring/{ended['id']}", headers=asha.h)).json()
    assert ended_out["status"] == "ended" and ended_out["next_due_date"] is None
    # Reminder-only rule: no transactions, but its next due date keeps moving forward.
    assert (await rule_row(manual["id"])).next_due_date == date(2026, 11, 20)
    assert await generate(NOW + timedelta(days=60)) == 0  # never anything after end_date
    assert (await rule_row(manual["id"])).next_due_date == date(2027, 1, 22)

    # Resume does not back-fill the occurrences that fell due while paused.
    await set_rule(paused["id"], next_due_date=date(2026, 11, 1))
    r = await client.post(f"{API}/recurring/{paused['id']}/resume", headers=asha.h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    today = datetime.now(IST).date()
    assert date.fromisoformat(r.json()["next_due_date"]) >= today


async def test_schedule_change_moves_cursor_after_generated_periods(client):
    asha = await user(client, "asha")
    rule = await create_rule(client, asha, start_date="2026-11-01")
    assert await generate() == 1
    r = await client.patch(
        f"{API}/recurring/{rule['id']}", headers=asha.h, json={"frequency": "weekly"}
    )
    assert r.status_code == 200 and r.json()["next_due_date"] == "2026-11-08"
    assert await generate() == 2  # Nov 8 and Nov 15; Nov 1 is never regenerated
    assert [t.period_key for t in await generated(rule["id"])] == [
        "2026-11-01",
        "2026-11-08",
        "2026-11-15",
    ]


# --- payment status --------------------------------------------------------------------


async def test_mark_paid_skip_unmark(client):
    asha = await user(client, "asha")
    acc = await add_account(asha)
    other_acc = await add_account(asha, name="Card")
    rule = await create_rule(client, asha, account_id=acc, start_date="2026-10-20")
    await generate()
    oct_tx, nov_tx = await generated(rule["id"])
    url = f"{API}/recurring/occurrences"

    r = await client.post(f"{url}/{oct_tx.id}/mark-paid", headers=asha.h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed" and r.json()["verification"] == "user_confirmed"

    r = await client.post(
        f"{url}/{nov_tx.id}/mark-paid",
        headers=asha.h,
        json={
            "amount": "15250.75",
            "account_id": other_acc,
            "occurred_at": "2026-11-21T18:30:00+05:30",
            "external_ref": "UTR123",
        },
    )
    assert r.status_code == 200, r.text
    paid = r.json()
    assert paid["amount"] == "15250.75" and paid["account_id"] == other_acc
    assert paid["external_ref"] == "UTR123" and paid["period_key"] == "2026-11-20"
    async with get_database().sessionmaker() as s:
        totals = await ledger.totals(s, Transaction.owner_user_id == asha.uid)
    assert totals["expense"] == Decimal("30250.75")

    # Bad inputs
    for bad in (
        {"amount": "0"},
        {"occurred_at": "2026-11-21T18:30:00"},
        {"account_id": str(uuid.uuid4())},
    ):
        r = await client.post(f"{url}/{nov_tx.id}/mark-paid", headers=asha.h, json=bad)
        assert r.status_code == 422, bad

    # A paid occurrence can't be skipped until it is unmarked.
    r = await client.post(f"{url}/{nov_tx.id}/skip", headers=asha.h)
    assert r.status_code == 409
    r = await client.post(f"{url}/{nov_tx.id}/unmark", headers=asha.h)
    assert r.status_code == 200
    assert r.json()["status"] == "pending" and r.json()["verification"] == "unverified"
    r = await client.post(f"{url}/{nov_tx.id}/skip", headers=asha.h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.post(f"{url}/{nov_tx.id}/skip", headers=asha.h)  # idempotent
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    async with get_database().sessionmaker() as s:
        totals = await ledger.totals(s, Transaction.owner_user_id == asha.uid)
    assert totals["expense"] == Decimal("15000.00")

    # Only transactions generated by a rule are accepted here.
    async with get_database().sessionmaker() as s:
        manual = Transaction(
            owner_user_id=asha.uid,
            scope=RecordScope.PERSONAL,
            type=TransactionType.EXPENSE,
            status=TransactionStatus.PENDING,
            amount=Decimal("10.00"),
            occurred_at=NOW,
        )
        s.add(manual)
        await s.commit()
    r = await client.post(f"{url}/{manual.id}/mark-paid", headers=asha.h)
    assert r.status_code == 404

    # Re-running the generator never touches statuses.
    await generate()
    statuses = [t.status for t in await generated(rule["id"])]
    assert statuses == [TransactionStatus.COMPLETED, TransactionStatus.CANCELLED]

    occ = (await client.get(f"{API}/recurring/{rule['id']}/occurrences", headers=asha.h)).json()
    assert [i["period_key"] for i in occ["items"]] == ["2026-11-20", "2026-10-20"]
    assert [i["status"] for i in occ["items"]] == ["cancelled", "completed"]


# --- upcoming --------------------------------------------------------------------------


async def test_upcoming_window_includes_generated_and_overdue(client):
    asha = await user(client, "asha")
    now = datetime.now(UTC)
    today = now.astimezone(IST).date()
    due_today = await create_rule(client, asha, name="Rent", start_date=today.isoformat())
    weekly = await create_rule(
        client,
        asha,
        name="Cleaner",
        amount="500",
        frequency="weekly",
        start_date=(today + timedelta(days=1)).isoformat(),
    )
    overdue = await create_rule(
        client, asha, name="Phone", amount="399", start_date=(today - timedelta(days=3)).isoformat()
    )
    paused = await create_rule(client, asha, name="Paused", start_date=today.isoformat())
    await client.post(f"{API}/recurring/{paused['id']}/pause", headers=asha.h)
    async with get_database().sessionmaker() as s:
        await RecurringService.generate_due(s, now)

    r = await client.get(f"{API}/recurring/upcoming", headers=asha.h, params={"days": 30})
    assert r.status_code == 200, r.text
    items = r.json()
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_rule.setdefault(item["recurring_id"], []).append(item)
    assert paused["id"] not in by_rule
    assert [i["due_date"] for i in items] == sorted(i["due_date"] for i in items)

    (rent,) = [i for i in by_rule[due_today["id"]] if i["due_date"] == today.isoformat()]
    assert rent["status"] == "pending" and rent["transaction_id"] is not None
    assert rent["days_until_due"] == 0 and rent["is_overdue"] is False

    cleaner = by_rule[weekly["id"]]
    assert len(cleaner) == 5 and all(i["status"] == "scheduled" for i in cleaner)
    assert cleaner[0]["transaction_id"] is None and cleaner[0]["amount"] == "500.00"

    phone = by_rule[overdue["id"]]
    assert phone[0]["is_overdue"] is True and phone[0]["status"] == "pending"
    assert phone[0]["days_until_due"] == -3

    # Once paid, it is no longer overdue (and shows as completed if inside the window).
    await client.post(
        f"{API}/recurring/occurrences/{phone[0]['transaction_id']}/mark-paid", headers=asha.h
    )
    await client.post(
        f"{API}/recurring/occurrences/{rent['transaction_id']}/mark-paid", headers=asha.h
    )
    items = (await client.get(f"{API}/recurring/upcoming", headers=asha.h)).json()
    assert not any(i["is_overdue"] for i in items)
    (rent,) = [
        i
        for i in items
        if i["recurring_id"] == due_today["id"] and i["due_date"] == today.isoformat()
    ]
    assert rent["status"] == "completed"
    r = await client.get(
        f"{API}/recurring/upcoming", headers=asha.h, params={"days": 0, "include_overdue": False}
    )
    assert {i["due_date"] for i in r.json()} <= {today.isoformat()}
    r = await client.get(f"{API}/recurring/upcoming", headers=asha.h, params={"days": 400})
    assert r.status_code == 422


# --- reminders -------------------------------------------------------------------------


async def test_upcoming_and_overdue_reminders_are_sent_once(client):
    asha = await user(client, "asha")
    rule = await create_rule(client, asha, start_date="2026-11-22", reminder_days_before=3)
    no_reminder = await create_rule(
        client, asha, name="Quiet", start_date="2026-11-25", reminder_days_before=1
    )

    assert await remind() == 1
    assert await remind() == 0  # dedupe
    assert await remind(NOW + timedelta(days=1)) == 0
    (n,) = await notifications(asha, NotificationCategory.BILL_UPCOMING)
    assert n.dedupe_key == f"recurring:{rule['id']}:2026-11-22:upcoming"
    assert n.title == "Upcoming: Rent" and "on 22 Nov 2026" in n.body
    assert n.data["recurring_id"] == rule["id"] and n.data["due_date"] == "2026-11-22"
    assert not [x for x in await notifications(asha) if no_reminder["id"] in (x.dedupe_key or "")]

    # Due day: the occurrence is generated (pending) — no second upcoming reminder for it.
    due_day = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)
    assert await generate(due_day) == 1
    await remind(due_day)
    assert len(await notifications(asha, NotificationCategory.BILL_UPCOMING)) == 1

    # Still pending the day after → one overdue reminder.
    assert await remind(due_day + timedelta(days=1)) >= 1
    assert await remind(due_day + timedelta(days=1, hours=1)) == 0
    (overdue,) = await notifications(asha, NotificationCategory.RECURRING_REMINDER)
    assert overdue.dedupe_key == f"recurring:{rule['id']}:2026-11-22:overdue"
    assert overdue.data["transaction_id"] == str((await generated(rule["id"]))[0].id)

    # Paid or skipped occurrences get no reminders at all.
    skipped_rule = await create_rule(client, asha, name="Skipped", start_date="2026-11-20")
    await generate()
    tx = (await generated(skipped_rule["id"]))[0]
    r = await client.post(f"{API}/recurring/occurrences/{tx.id}/skip", headers=asha.h)
    assert r.status_code == 200
    await remind()
    await remind(NOW + timedelta(days=1))
    assert not [x for x in await notifications(asha) if skipped_rule["id"] in (x.dedupe_key or "")]


async def test_family_reminder_recipients(client):
    fid, p = await household(client)
    rule = await create_rule(
        client,
        p["member"],
        name="Water",
        scope="family",
        family_id=fid,
        start_date="2026-11-21",
        reminder_days_before=1,
    )
    paused = await create_rule(
        client,
        p["member"],
        name="Paused",
        scope="family",
        family_id=fid,
        start_date="2026-11-21",
    )
    await client.post(f"{API}/recurring/{paused['id']}/pause", headers=p["member"].h)
    personal = await create_rule(client, p["viewer"], name="Mine", start_date="2026-11-21")

    assert await remind() == 4  # member (rule owner) + family owner + admin; viewer's own rule
    for who in ("member", "owner", "admin"):
        got = await notifications(p[who], NotificationCategory.BILL_UPCOMING)
        assert [n.dedupe_key for n in got] == [f"recurring:{rule['id']}:2026-11-21:upcoming"], who
        assert str(got[0].family_id) == fid
    assert await notifications(p["member2"], NotificationCategory.BILL_UPCOMING) == []
    viewer = await notifications(p["viewer"], NotificationCategory.BILL_UPCOMING)
    assert [n.dedupe_key for n in viewer] == [f"recurring:{personal['id']}:2026-11-21:upcoming"]
    assert viewer[0].family_id is None
    assert await remind() == 0


async def test_enabling_auto_generate_does_not_back_fill(client):
    asha = await user(client, "asha")
    today = datetime.now(IST).date()
    rule = await create_rule(
        client,
        asha,
        frequency="weekly",
        start_date=(today - timedelta(days=40)).isoformat(),
        auto_generate=False,
    )
    r = await client.patch(
        f"{API}/recurring/{rule['id']}", headers=asha.h, json={"auto_generate": True}
    )
    assert r.status_code == 200 and r.json()["auto_generate"] is True
    assert date.fromisoformat(r.json()["next_due_date"]) >= today
    async with get_database().sessionmaker() as s:
        await RecurringService.generate_due(s, datetime.now(UTC))
    assert all(t.period_key >= today.isoformat() for t in await generated(rule["id"]))


async def test_upcoming_keeps_overdue_bills_of_paused_rules(client):
    asha = await user(client, "asha")
    now = datetime.now(UTC)
    today = now.astimezone(IST).date()
    rule = await create_rule(
        client, asha, name="Phone", start_date=(today - timedelta(days=2)).isoformat()
    )
    async with get_database().sessionmaker() as s:
        await RecurringService.generate_due(s, now)
    await client.post(f"{API}/recurring/{rule['id']}/pause", headers=asha.h)
    items = (await client.get(f"{API}/recurring/upcoming", headers=asha.h)).json()
    assert [(i["recurring_id"], i["is_overdue"]) for i in items] == [(rule["id"], True)]
