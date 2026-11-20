"""Transactions (/api/v1/transactions): CRUD, validation, idempotency, ledger figures,
filters, CSV export, family permissions, tenant isolation, notifications and the internal
helpers used by UPI / recurring."""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.db.session import get_database
from app.models import (
    Notification,
    NotificationCategory,
    RecurrenceFrequency,
    RecurringTransaction,
    Transaction,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    User,
    VerificationStatus,
)
from app.models.system import AuditLog
from app.schemas.transactions import TransactionCreate
from app.services import ledger
from app.services.transactions import InvalidTransactionError, TransactionService, csv_safe
from tests.helpers_transactions import (
    API,
    EXPENSE_CAT,
    INCOME_CAT,
    Actor,
    account,
    add_category,
    add_payee,
    create_family,
    household,
    join,
    tx,
    user,
)


async def summary(client, who: Actor, **params: Any) -> dict[str, Any]:
    r = await client.get(f"{API}/transactions/summary", headers=who.h, params=params)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def listing(client, who: Actor, **params: Any) -> dict[str, Any]:
    r = await client.get(f"{API}/transactions", headers=who.h, params=params)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def row_count(**where: Any) -> int:
    async with get_database().sessionmaker() as s:
        stmt = select(func.count()).select_from(Transaction)
        for k, v in where.items():
            stmt = stmt.where(getattr(Transaction, k) == v)
        return int((await s.execute(stmt)).scalar_one())


async def shared_expense_notifications() -> list[tuple[str, str]]:
    async with get_database().sessionmaker() as s:
        rows = await s.execute(
            select(Notification.user_id, Notification.dedupe_key).where(
                Notification.category == NotificationCategory.FAMILY_SHARED_EXPENSE
            )
        )
        return sorted((str(u), k) for u, k in rows)


# --- CRUD ------------------------------------------------------------------------------


async def test_create_get_update_delete_restore(client):
    asha = await user(client, "asha")
    bank = await account(client, asha, "Bank", opening_balance="1000")
    created = await tx(
        client,
        asha,
        amount="250.5",
        account_id=bank["id"],
        category_id=EXPENSE_CAT,
        description="Dinner",
        merchant="Spice Hut",
        notes="birthday",
        payment_method="card",
        occurred_at="2026-11-18T19:30:00+05:30",
        tags=[" Food ", "food", "Party"],
    )
    assert created["type"] == "expense" and created["amount"] == "250.50"
    assert created["currency"] == "INR" and created["status"] == "completed"
    assert created["source"] == "manual" and created["verification"] == "unverified"
    assert created["scope"] == "personal" and created["family_id"] is None
    assert created["created_by"] == {"id": asha.id, "full_name": "Asha"}
    assert created["account"]["id"] == bank["id"] and created["account"]["name"] == "Bank"
    assert created["to_account"] is None
    assert created["category"]["id"] == EXPENSE_CAT and created["category"]["kind"] == "expense"
    assert created["tags"] == ["food", "party"]
    assert created["attachments_count"] == 0 and created["can_edit"] is True
    assert datetime.fromisoformat(created["occurred_at"]) == datetime(
        2026, 11, 18, 14, 0, tzinfo=UTC
    )
    assert created["deleted_at"] is None

    got = await client.get(f"{API}/transactions/{created['id']}", headers=asha.h)
    assert got.status_code == 200 and got.json() == created

    r = await client.patch(
        f"{API}/transactions/{created['id']}",
        headers=asha.h,
        json={"amount": "300", "tags": ["dinner"], "notes": None, "category_id": None},
    )
    assert r.status_code == 200, r.text
    upd = r.json()
    assert upd["amount"] == "300.00" and upd["tags"] == ["dinner"]
    assert upd["notes"] is None and upd["category"] is None

    assert (
        await client.delete(f"{API}/transactions/{created['id']}", headers=asha.h)
    ).status_code == 204
    assert (
        await client.get(f"{API}/transactions/{created['id']}", headers=asha.h)
    ).status_code == 404
    assert (await listing(client, asha))["total"] == 0
    r = await client.patch(
        f"{API}/transactions/{created['id']}", headers=asha.h, json={"amount": "1"}
    )
    assert r.status_code == 404

    r = await client.post(f"{API}/transactions/{created['id']}/restore", headers=asha.h)
    assert (
        r.status_code == 200 and r.json()["deleted_at"] is None and r.json()["amount"] == "300.00"
    )
    # restoring a live row is a no-op
    r = await client.post(f"{API}/transactions/{created['id']}/restore", headers=asha.h)
    assert r.status_code == 200
    assert (await listing(client, asha))["total"] == 1

    # naive occurred_at is read in the user's zone (Asia/Kolkata); default is "now"
    t2 = await tx(client, asha, occurred_at="2026-11-20T00:15:00")
    assert datetime.fromisoformat(t2["occurred_at"]) == datetime(2026, 11, 19, 18, 45, tzinfo=UTC)
    t3 = await tx(client, asha)
    assert abs(datetime.fromisoformat(t3["occurred_at"]) - datetime.now(UTC)).total_seconds() < 60

    async with get_database().sessionmaker() as s:
        audit = list(
            await s.execute(
                select(AuditLog.action, AuditLog.details)
                .where(AuditLog.entity_id == uuid.UUID(created["id"]))
                .order_by(AuditLog.created_at)
            )
        )
    actions = [a for a, _ in audit]
    assert actions == [
        "transaction.created",
        "transaction.updated",
        "transaction.deleted",
        "transaction.restored",
    ]
    changes = audit[1][1]["changes"]
    assert changes["amount"] == {"before": "250.50", "after": "300.00"}
    assert (
        changes["category_id"]["before"] == EXPENSE_CAT and changes["category_id"]["after"] is None
    )
    assert changes["tags"] == {"before": ["food", "party"], "after": ["dinner"]}
    assert audit[0][1]["type"] == "expense" and audit[0][1]["amount"] == "250.50"


async def test_transfer_adjustment_income_and_refund_shapes(client):
    asha = await user(client, "asha")
    bank = await account(client, asha, "Bank")
    cash = await account(client, asha, "Cash", type="cash")
    t = await tx(
        client, asha, type="transfer", amount="50", account_id=bank["id"], to_account_id=cash["id"]
    )
    assert t["to_account"]["name"] == "Cash" and t["category"] is None
    a = await tx(
        client, asha, type="adjustment", amount="5", account_id=cash["id"], adjustment_sign=1
    )
    assert a["adjustment_sign"] == 1
    i = await tx(client, asha, type="income", amount="10", category_id=INCOME_CAT)
    assert i["category"]["kind"] == "income"
    r = await tx(client, asha, type="refund", amount="10", category_id=EXPENSE_CAT)
    assert r["type"] == "refund"

    # type change drops fields that no longer fit (category kind, adjustment sign, to_account)
    res = await client.patch(
        f"{API}/transactions/{r['id']}", headers=asha.h, json={"type": "income"}
    )
    assert res.status_code == 200 and res.json()["category"] is None
    res = await client.patch(
        f"{API}/transactions/{a['id']}", headers=asha.h, json={"type": "expense"}
    )
    assert res.status_code == 200 and res.json()["adjustment_sign"] is None
    res = await client.patch(
        f"{API}/transactions/{t['id']}", headers=asha.h, json={"type": "expense"}
    )
    assert res.status_code == 200 and res.json()["to_account"] is None
    res = await client.patch(
        f"{API}/transactions/{i['id']}",
        headers=asha.h,
        json={"type": "transfer", "account_id": cash["id"], "to_account_id": bank["id"]},
    )
    assert res.status_code == 200 and res.json()["category"] is None


# --- validation ------------------------------------------------------------------------


async def test_validation_rules(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    bank = await account(client, asha, "Bank")
    cash = await account(client, asha, "Cash", type="cash")
    usd = await account(client, asha, "Wise", currency="USD")
    old = await account(client, asha, "Old")
    await client.post(f"{API}/accounts/{old['id']}/archive", headers=asha.h)
    bobs = await account(client, bob, "Bob Bank")
    bob_cat = await add_category(name="Bob stuff", owner_user_id=bob.id)
    fid = await create_family(client, asha)
    fam_acc = await account(client, asha, "Joint", scope="family", family_id=fid)
    fam_cat = await add_category(name="Family stuff", family_id=fid)
    my_payee = await add_payee(name="Ravi", owner_user_id=asha.id)
    bob_payee = await add_payee(name="Bob's guy", owner_user_id=bob.id)

    def expect(want: int, code: str | None = None, /, **body: Any) -> tuple[int, str | None, dict]:
        return want, code, {"type": "expense", "amount": "10", **body}

    cases = [
        expect(422, amount="0"),
        expect(422, amount="-5"),
        expect(422, amount="1.234"),
        expect(422, amount="1000000000000"),
        expect(422, amount="abc"),
        expect(422, type="gift"),
        expect(422, currency="inr"),
        expect(422, unknown_field=1),
        expect(422, scope="family"),
        expect(422, family_id=fid),
        expect(422, status="failed"),
        expect(422, tags=[f"t{i}" for i in range(11)]),
        expect(422, tags=["x" * 31]),
        expect(422, tags=["  "]),
        expect(422, idempotency_key="has spaces"),
        expect(422, "category_kind_mismatch", category_id=INCOME_CAT),
        expect(422, "category_kind_mismatch", type="income", category_id=EXPENSE_CAT),
        expect(
            422,
            "category_not_allowed",
            type="transfer",
            account_id=bank["id"],
            to_account_id=cash["id"],
            category_id=EXPENSE_CAT,
        ),
        expect(422, "transfer_accounts", type="transfer", account_id=bank["id"]),
        expect(
            422,
            "transfer_same_account",
            type="transfer",
            account_id=bank["id"],
            to_account_id=bank["id"],
        ),
        expect(422, "to_account_not_allowed", account_id=bank["id"], to_account_id=cash["id"]),
        expect(422, "adjustment_sign_required", type="adjustment", account_id=bank["id"]),
        expect(422, "adjustment_account_required", type="adjustment", adjustment_sign=1),
        expect(422, adjustment_sign=2, type="adjustment", account_id=bank["id"]),
        expect(422, "adjustment_sign_not_allowed", adjustment_sign=1),
        expect(422, "currency_mismatch", account_id=usd["id"], currency="INR"),
        expect(
            422,
            "currency_mismatch",
            type="transfer",
            account_id=bank["id"],
            to_account_id=usd["id"],
        ),
        expect(422, "account_inactive", account_id=old["id"]),
        expect(422, "account_scope_mismatch", account_id=fam_acc["id"]),
        expect(
            422,
            "account_scope_mismatch",
            scope="family",
            family_id=fid,
            account_id=bank["id"],
        ),
        expect(404, "not_found", account_id=bobs["id"]),
        expect(404, "not_found", account_id=str(uuid.uuid4())),
        expect(404, "not_found", category_id=bob_cat),
        expect(404, "not_found", category_id=fam_cat),  # family category on a personal row
        expect(404, "not_found", category_id=str(uuid.uuid4())),
        expect(404, "not_found", payee_id=bob_payee),
        expect(422, "payee_scope_mismatch", scope="family", family_id=fid, payee_id=my_payee),
        expect(404, "not_found", scope="family", family_id=str(uuid.uuid4())),
    ]
    for status_code, code, body in cases:
        r = await client.post(f"{API}/transactions", headers=asha.h, json=body)
        assert r.status_code == status_code, (body, r.text)
        if code:
            assert r.json()["error"]["code"] == code, (body, r.text)
    assert await row_count() == 0

    # valid combinations
    usd_tx = await tx(client, asha, account_id=usd["id"])
    assert usd_tx["currency"] == "USD"  # defaults to the account's currency
    fam_tx = await tx(
        client,
        asha,
        scope="family",
        family_id=fid,
        account_id=fam_acc["id"],
        category_id=fam_cat,
        payee_id=await add_payee(name="Kirana", owner_user_id=asha.id, family_id=fid),
    )
    assert fam_tx["category"]["name"] == "Family stuff" and fam_tx["payee"]["name"] == "Kirana"
    mine = await tx(client, asha, payee_id=my_payee, category_id=EXPENSE_CAT)
    assert mine["payee"] == {"id": my_payee, "name": "Ravi", "upi_id": None}

    # update rules
    for body, code in (
        ({"scope": "family"}, "scope_change_not_allowed"),
        ({"family_id": fid}, "scope_change_not_allowed"),
        ({"account_id": old["id"]}, "account_inactive"),
        ({"category_id": INCOME_CAT}, "category_kind_mismatch"),
        ({"amount": None}, None),
        ({"amount": "0"}, None),
        ({"owner_user_id": bob.id}, None),
    ):
        r = await client.patch(f"{API}/transactions/{mine['id']}", headers=asha.h, json=body)
        assert r.status_code == 422, (body, r.text)
        if code:
            assert r.json()["error"]["code"] == code
    r = await client.patch(
        f"{API}/transactions/{fam_tx['id']}", headers=asha.h, json={"scope": "personal"}
    )
    assert r.status_code == 422
    # echoing the unchanged scope is fine; manual rows may change status
    r = await client.patch(
        f"{API}/transactions/{mine['id']}",
        headers=asha.h,
        json={"scope": "personal", "status": "pending"},
    )
    assert r.status_code == 200 and r.json()["status"] == "pending"


# --- idempotency ------------------------------------------------------------------------


async def test_idempotency_key_header_and_body(client):
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    h = {**asha.h, "Idempotency-Key": "client-req-1"}
    body = {"type": "expense", "amount": "99.99", "description": "Coffee"}
    first = await client.post(f"{API}/transactions", headers=h, json=body)
    assert first.status_code == 201
    again = await client.post(f"{API}/transactions", headers=h, json=body)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert first.json()["idempotency_key"] == "client-req-1"
    assert await row_count() == 1

    # body field works the same way
    b1 = await tx(client, asha, idempotency_key="body-key")
    b2 = await tx(client, asha, expect=200, idempotency_key="body-key", amount="1")
    assert b1["id"] == b2["id"] and b2["amount"] == "100.00"
    # same key in header + body is fine, different is rejected
    r = await client.post(
        f"{API}/transactions",
        headers={**asha.h, "Idempotency-Key": "body-key"},
        json={**body, "idempotency_key": "body-key"},
    )
    assert r.status_code == 200 and r.json()["id"] == b1["id"]
    r = await client.post(
        f"{API}/transactions",
        headers={**asha.h, "Idempotency-Key": "a"},
        json={**body, "idempotency_key": "b"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_mismatch"
    r = await client.post(
        f"{API}/transactions", headers={**asha.h, "Idempotency-Key": "bad key!"}, json=body
    )
    assert r.status_code == 422

    # keys are per user
    other = await client.post(
        f"{API}/transactions", headers={**bob.h, "Idempotency-Key": "client-req-1"}, json=body
    )
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]
    # a replay of a since-deleted row still returns the original (never a duplicate)
    await client.delete(f"{API}/transactions/{b1['id']}", headers=asha.h)
    replay = await tx(client, asha, expect=200, idempotency_key="body-key")
    assert replay["id"] == b1["id"] and replay["deleted_at"] is not None
    assert await row_count(owner_user_id=uuid.UUID(asha.id)) == 2


async def test_idempotency_concurrent_requests_create_one_row(client):
    asha = await user(client, "asha")
    h = {**asha.h, "Idempotency-Key": "race-1"}
    body = {"type": "expense", "amount": "10", "tags": ["race"]}
    responses = await asyncio.gather(
        *(client.post(f"{API}/transactions", headers=h, json=body) for _ in range(6))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 200, 200, 200, 201], [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert await row_count() == 1
    async with get_database().sessionmaker() as s:
        n = (await s.execute(text("SELECT count(*) FROM transaction_tags"))).scalar_one()
        audits = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "transaction.created")
            )
        ).scalar_one()
    assert n == 1 and audits == 1


# --- ledger figures ---------------------------------------------------------------------


async def test_transfers_never_count_and_move_balances(client):
    """Critical scenario 6."""
    asha = await user(client, "asha")
    bank = await account(client, asha, "Bank", opening_balance="100")
    cash = await account(client, asha, "Cash", type="cash")
    await tx(client, asha, type="income", amount="1000", account_id=bank["id"])
    await tx(
        client, asha, type="transfer", amount="400", account_id=bank["id"], to_account_id=cash["id"]
    )
    await tx(client, asha, type="adjustment", amount="7", account_id=cash["id"], adjustment_sign=1)

    s = await summary(client, asha)
    assert s == {"income": "1000.00", "expense": "0.00", "net": "1000.00", "count": 3}
    s = await summary(client, asha, account_id=cash["id"])
    assert s == {"income": "0.00", "expense": "0.00", "net": "0.00", "count": 2}
    s = await summary(client, asha, types="transfer")
    assert s["count"] == 1 and s["income"] == "0.00" and s["expense"] == "0.00"
    balances = {
        a["name"]: a["current_balance"]
        for a in (await client.get(f"{API}/accounts", headers=asha.h)).json()
    }
    assert balances == {"Bank": "700.00", "Cash": "407.00"}


async def test_summary_and_balances_follow_edit_delete_restore(client):
    """Groundwork for critical scenario 7."""
    asha = await user(client, "asha")
    bank = await account(client, asha, "Bank", opening_balance="1000")
    e = await tx(client, asha, amount="100", account_id=bank["id"], category_id=EXPENSE_CAT)
    await tx(client, asha, type="refund", amount="30", account_id=bank["id"])
    pending = await tx(client, asha, amount="500", account_id=bank["id"], status="pending")

    async def state() -> tuple[dict[str, Any], str]:
        acc = (await client.get(f"{API}/accounts/{bank['id']}", headers=asha.h)).json()
        return await summary(client, asha), acc["current_balance"]

    s, bal = await state()
    assert (s["income"], s["expense"], s["net"], s["count"], bal) == (
        "0.00",
        "70.00",
        "-70.00",
        3,
        "930.00",
    )
    await client.patch(f"{API}/transactions/{e['id']}", headers=asha.h, json={"amount": "150"})
    s, bal = await state()
    assert (s["expense"], bal) == ("120.00", "880.00")
    await client.patch(f"{API}/transactions/{e['id']}", headers=asha.h, json={"type": "income"})
    s, bal = await state()
    assert (s["income"], s["expense"], s["net"], bal) == ("150.00", "-30.00", "180.00", "1180.00")
    await client.delete(f"{API}/transactions/{e['id']}", headers=asha.h)
    s, bal = await state()
    assert (s["income"], s["expense"], s["count"], bal) == ("0.00", "-30.00", 2, "1030.00")
    await client.post(f"{API}/transactions/{e['id']}/restore", headers=asha.h)
    s, bal = await state()
    assert (s["income"], s["count"], bal) == ("150.00", 3, "1180.00")
    # confirming the pending row makes it count
    await client.patch(
        f"{API}/transactions/{pending['id']}", headers=asha.h, json={"status": "completed"}
    )
    s, bal = await state()
    assert (s["expense"], bal) == ("470.00", "680.00")


# --- filters, sort, pagination ----------------------------------------------------------


async def test_filters_sort_pagination_and_search(client):
    asha = await user(client, "asha")
    bank = await account(client, asha, "Bank")
    cash = await account(client, asha, "Cash", type="cash")
    home = await add_category(name="Home", owner_user_id=asha.id)
    repairs = await add_category(name="Repairs", owner_user_id=asha.id, parent_id=home)
    ravi = await add_payee(name="Ravi Kirana", owner_user_id=asha.id)

    t1 = await tx(
        client,
        asha,
        amount="100",
        account_id=cash["id"],
        category_id=EXPENSE_CAT,
        payment_method="cash",
        tags=["lunch"],
        description="Pizza",
        occurred_at="2026-11-10T06:00:00Z",
    )
    t2 = await tx(
        client,
        asha,
        amount="250.50",
        account_id=bank["id"],
        category_id=repairs,
        payment_method="upi",
        merchant="100% Hardware_Store",
        occurred_at="2026-11-12T06:00:00Z",
    )
    t3 = await tx(
        client,
        asha,
        type="income",
        amount="5000",
        account_id=bank["id"],
        category_id=INCOME_CAT,
        description="Salary",
        occurred_at="2026-11-01T06:00:00Z",
    )
    t4 = await tx(
        client,
        asha,
        type="transfer",
        amount="300",
        account_id=bank["id"],
        to_account_id=cash["id"],
        occurred_at="2026-11-15T06:00:00Z",
    )
    t5 = await tx(
        client,
        asha,
        amount="40",
        status="pending",
        payee_id=ravi,
        occurred_at="2026-11-16T06:00:00Z",
    )
    t6 = await tx(
        client,
        asha,
        type="refund",
        amount="20",
        category_id=EXPENSE_CAT,
        notes="returned item",
        occurred_at="2026-11-17T06:00:00Z",
    )
    # IST boundary: 2026-11-19T20:00Z is 2026-11-20 01:30 in Asia/Kolkata
    late = await tx(client, asha, amount="1", occurred_at="2026-11-19T20:00:00Z")
    early = await tx(client, asha, amount="2", occurred_at="2026-11-19T18:00:00Z")

    async def ids(**params: Any) -> list[str]:
        return [t["id"] for t in (await listing(client, asha, **params))["items"]]

    assert await ids(date_from="2026-11-20", date_to="2026-11-20") == [late["id"]]
    assert await ids(date_from="2026-11-19", date_to="2026-11-19") == [early["id"]]
    await client.delete(f"{API}/transactions/{late['id']}", headers=asha.h)
    await client.delete(f"{API}/transactions/{early['id']}", headers=asha.h)

    assert await ids() == [t6["id"], t5["id"], t4["id"], t2["id"], t1["id"], t3["id"]]
    assert await ids(sort_by="amount", sort_dir="asc") == [
        t6["id"], t5["id"], t1["id"], t2["id"], t4["id"], t3["id"]
    ]  # fmt: skip
    assert await ids(sort_by="occurred_at", sort_dir="asc") == [
        t3["id"], t1["id"], t2["id"], t4["id"], t5["id"], t6["id"]
    ]  # fmt: skip
    assert await ids(types=["expense", "refund"]) == [t6["id"], t5["id"], t2["id"], t1["id"]]
    assert await ids(category_ids=[home]) == [t2["id"]]  # subcategories included
    assert await ids(category_ids=[repairs, INCOME_CAT]) == [t2["id"], t3["id"]]
    assert await ids(account_id=cash["id"]) == [t4["id"], t1["id"]]  # either side
    assert await ids(payment_method="upi") == [t2["id"]]
    assert await ids(status="pending") == [t5["id"]]
    assert await ids(q="ravi") == [t5["id"]]  # payee name
    assert await ids(q="PIZZA") == [t1["id"]]
    assert await ids(q="returned") == [t6["id"]]  # notes
    assert await ids(q="100%") == [t2["id"]]  # wildcards are literal
    assert await ids(q="_") == [t2["id"]]
    assert await ids(min_amount="100", max_amount="300") == [t4["id"], t2["id"], t1["id"]]
    assert await ids(tag="LUNCH") == [t1["id"]]
    assert await ids(date_from="2026-11-12", date_to="2026-11-15") == [t4["id"], t2["id"]]
    assert await ids(member_user_id=asha.id, types="income") == [t3["id"]]

    page = await listing(client, asha, limit=2, offset=2)
    assert [t["id"] for t in page["items"]] == [t4["id"], t2["id"]]
    assert (page["total"], page["limit"], page["offset"], page["has_more"]) == (6, 2, 2, True)
    last = await listing(client, asha, limit=2, offset=4)
    assert last["has_more"] is False and len(last["items"]) == 2

    s = await summary(client, asha)
    assert s == {"income": "5000.00", "expense": "330.50", "net": "4669.50", "count": 6}
    s = await summary(client, asha, types=["expense"], date_from="2026-11-11")
    assert s == {"income": "0.00", "expense": "250.50", "net": "-250.50", "count": 2}
    s = await summary(client, asha, q="ravi")
    assert s == {"income": "0.00", "expense": "0.00", "net": "0.00", "count": 1}

    for bad in (
        {"date_from": "2026-11-20", "date_to": "2026-11-01"},
        {"min_amount": "10", "max_amount": "5"},
        {"types": "bogus"},
        {"sort_by": "description"},
        {"sort_dir": "up"},
        {"date_from": "20-11-2026"},
        {"limit": "0"},
        {"limit": "201"},
        {"min_amount": "-1"},
    ):
        r = await client.get(f"{API}/transactions", headers=asha.h, params=bad)
        assert r.status_code == 422, bad
        r = await client.get(f"{API}/transactions/summary", headers=asha.h, params=bad)
        assert r.status_code == 422 or "sort" in str(bad) or "limit" in str(bad), bad


# --- CSV export -------------------------------------------------------------------------


async def test_csv_export_with_injection_escaping(client):
    asha = await user(client, "asha")
    bank = await account(client, asha, "=Bank")
    payee = await add_payee(name="@Evil payee", owner_user_id=asha.id)
    await tx(
        client,
        asha,
        amount="1234.5",
        account_id=bank["id"],
        category_id=EXPENSE_CAT,
        description='=HYPERLINK("http://evil","x")',
        merchant="+cmd",
        notes="-2+3",
        payee_id=payee,
        payment_method="upi",
        tags=["b", "a"],
        occurred_at="2026-11-19T20:00:00Z",
    )
    await tx(client, asha, type="income", amount="10", description="Salary, Nov")

    r = await client.get(f"{API}/transactions/export.csv", headers=asha.h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == [
        "id", "date", "time", "type", "status", "amount", "currency", "category", "account",
        "to_account", "payee", "merchant", "description", "payment_method", "tags", "notes",
        "scope", "created_by", "source",
    ]  # fmt: skip
    assert len(rows) == 3
    by_type = {row[3]: dict(zip(rows[0], row, strict=True)) for row in rows[1:]}
    e = by_type["expense"]
    assert e["amount"] == "1234.50" and e["currency"] == "INR"
    assert (e["date"], e["time"]) == ("2026-11-20", "01:30")  # Asia/Kolkata
    assert e["description"] == '\'=HYPERLINK("http://evil","x")'
    assert e["merchant"] == "'+cmd" and e["notes"] == "'-2+3"
    assert e["payee"] == "'@Evil payee" and e["account"] == "'=Bank"
    assert e["category"] == "Food & Dining" and e["tags"] == "a b"
    assert e["created_by"] == "Asha" and e["scope"] == "personal"
    assert by_type["income"]["description"] == "Salary, Nov"

    only_income = await client.get(
        f"{API}/transactions/export.csv", headers=asha.h, params={"types": "income"}
    )
    assert len(list(csv.reader(io.StringIO(only_income.text)))) == 2

    other = await user(client, "bob")
    r = await client.get(f"{API}/transactions/export.csv", headers=other.h)
    assert len(list(csv.reader(io.StringIO(r.text)))) == 1  # header only

    assert csv_safe("\tx") == "'\tx" and csv_safe("\rx") == "'\rx" and csv_safe("ok") == "ok"
    assert csv_safe("") == ""


# --- family permissions -----------------------------------------------------------------


async def test_family_permission_matrix(client):
    fid, p = await household(client)
    outsider = await user(client, "outsider")
    fam = {"scope": "family", "family_id": fid}

    r = await client.post(
        f"{API}/transactions", headers=p["viewer"].h, json={"type": "expense", "amount": "1", **fam}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_family_role"
    r = await client.post(
        f"{API}/transactions", headers=outsider.h, json={"type": "expense", "amount": "1", **fam}
    )
    assert r.status_code == 404

    by = {role: await tx(client, p[role], **fam) for role in ("owner", "admin", "member")}

    # everyone in the family reads every family row; personal lists stay empty
    for role in ("owner", "admin", "member", "viewer"):
        page = await listing(client, p[role], family_id=fid)
        assert page["total"] == 3
        flags = {t["created_by"]["full_name"].lower(): t["can_edit"] for t in page["items"]}
        if role in ("owner", "admin"):
            assert set(flags.values()) == {True}
        elif role == "member":
            assert flags == {"owner": False, "admin": False, "member": True}
        else:
            assert set(flags.values()) == {False}
        assert (await listing(client, p[role]))["total"] == 0
        for t in by.values():
            assert (
                await client.get(f"{API}/transactions/{t['id']}", headers=p[role].h)
            ).status_code == 200

    async def patch(who: str, t: dict[str, Any]) -> int:
        r = await client.patch(
            f"{API}/transactions/{t['id']}", headers=p[who].h, json={"description": who}
        )
        return int(r.status_code)

    # viewer: read-only
    for t in by.values():
        assert await patch("viewer", t) == 403
        r = await client.delete(f"{API}/transactions/{t['id']}", headers=p["viewer"].h)
        assert r.status_code == 403
    # member: own rows only
    assert await patch("member", by["member"]) == 200
    assert await patch("member", by["admin"]) == 403
    assert await patch("member", by["owner"]) == 403
    r = await client.delete(f"{API}/transactions/{by['owner']['id']}", headers=p["member"].h)
    assert r.status_code == 403
    # admin / owner: any row
    assert await patch("admin", by["member"]) == 200
    assert await patch("admin", by["owner"]) == 200
    assert await patch("owner", by["admin"]) == 200
    r = await client.delete(f"{API}/transactions/{by['member']['id']}", headers=p["admin"].h)
    assert r.status_code == 204
    # restore follows the same rules
    path = f"{API}/transactions/{by['member']['id']}/restore"
    assert (await client.post(path, headers=p["viewer"].h)).status_code == 403
    assert (await client.post(path, headers=outsider.h)).status_code == 404
    assert (await client.post(path, headers=p["member"].h)).status_code == 200
    r = await client.delete(f"{API}/transactions/{by['member']['id']}", headers=p["member"].h)
    assert r.status_code == 204
    assert (await client.post(path, headers=p["owner"].h)).status_code == 200

    # member_user_id filter within the family
    page = await listing(client, p["viewer"], family_id=fid, member_user_id=p["member"].id)
    assert [t["id"] for t in page["items"]] == [by["member"]["id"]]

    # outsiders get 404 for everything
    assert (
        await client.get(f"{API}/transactions", headers=outsider.h, params={"family_id": fid})
    ).status_code == 404
    for path in ("/transactions/summary", "/transactions/export.csv"):
        r = await client.get(f"{API}{path}", headers=outsider.h, params={"family_id": fid})
        assert r.status_code == 404, path
    for t in by.values():
        assert (
            await client.get(f"{API}/transactions/{t['id']}", headers=outsider.h)
        ).status_code == 404
        assert await patch_as(client, outsider, t) == 404
        r = await client.delete(f"{API}/transactions/{t['id']}", headers=outsider.h)
        assert r.status_code == 404

    # a member who leaves loses access
    r = await client.post(f"{API}/families/{fid}/leave", headers=p["member"].h)
    assert r.status_code == 204
    r = await client.get(f"{API}/transactions/{by['member']['id']}", headers=p["member"].h)
    assert r.status_code == 404
    assert await patch_as(client, p["member"], by["member"]) == 404

    async with get_database().sessionmaker() as s:
        actions = list(
            await s.scalars(
                select(AuditLog.action).where(
                    AuditLog.family_id == uuid.UUID(fid), AuditLog.entity_type == "transaction"
                )
            )
        )
    assert actions.count("transaction.created") == 3
    assert actions.count("transaction.deleted") == 2 and actions.count("transaction.restored") == 2


async def patch_as(client, who: Actor, t: dict[str, Any]) -> int:
    r = await client.patch(f"{API}/transactions/{t['id']}", headers=who.h, json={"notes": "x"})
    return int(r.status_code)


# --- critical scenarios 1 & 2 -----------------------------------------------------------


async def test_user_a_cannot_access_user_b_personal_transactions(client):
    """Critical scenario 1."""
    a = await user(client, "alice")
    b = await user(client, "bob")
    t = await tx(client, a, amount="500", description="Alice private")

    assert (await listing(client, b))["total"] == 0
    assert (await summary(client, b))["count"] == 0
    for method, path, body in (
        ("GET", f"/transactions/{t['id']}", None),
        ("PATCH", f"/transactions/{t['id']}", {"amount": "1"}),
        ("DELETE", f"/transactions/{t['id']}", None),
        ("POST", f"/transactions/{t['id']}/restore", None),
    ):
        r = await client.request(method, f"{API}{path}", headers=b.h, json=body)
        assert r.status_code == 404, (method, path)
    # B cannot even probe it via a deleted state
    await client.delete(f"{API}/transactions/{t['id']}", headers=a.h)
    r = await client.post(f"{API}/transactions/{t['id']}/restore", headers=b.h)
    assert r.status_code == 404
    r = await client.post(f"{API}/transactions/{t['id']}/restore", headers=a.h)
    assert r.status_code == 200 and r.json()["amount"] == "500.00"


async def test_family_a_cannot_access_family_b(client):
    """Critical scenario 2."""
    a_owner = await user(client, "aowner")
    b_owner = await user(client, "bowner")
    b_member = await user(client, "bmember")
    fa = await create_family(client, a_owner, "Family A")
    fb = await create_family(client, b_owner, "Family B")
    await join(client, b_owner, fb, b_member, "member")
    a_acc = await account(client, a_owner, "A joint", scope="family", family_id=fa)
    a_cat = await add_category(name="A only", family_id=fa)
    a_tx = await tx(client, a_owner, scope="family", family_id=fa, account_id=a_acc["id"])
    b_acc = await account(client, b_owner, "B joint", scope="family", family_id=fb)

    for who in (b_owner, b_member):
        for path in ("/transactions", "/transactions/summary", "/transactions/export.csv"):
            r = await client.get(f"{API}{path}", headers=who.h, params={"family_id": fa})
            assert r.status_code == 404, path
        assert (await listing(client, who, family_id=fb))["total"] == 0
        for method in ("GET", "PATCH", "DELETE"):
            r = await client.request(
                method,
                f"{API}/transactions/{a_tx['id']}",
                headers=who.h,
                json={"amount": "1"} if method == "PATCH" else None,
            )
            assert r.status_code == 404
        # can't reference family A's account / category from family B
        base = {"type": "expense", "amount": "5", "scope": "family", "family_id": fb}
        for extra in ({"account_id": a_acc["id"]}, {"category_id": a_cat}):
            r = await client.post(f"{API}/transactions", headers=who.h, json={**base, **extra})
            assert r.status_code == 404, extra
        # nor create into family A
        r = await client.post(f"{API}/transactions", headers=who.h, json={**base, "family_id": fa})
        assert r.status_code == 404
    # family A's owner can't use family B's account either
    r = await client.post(
        f"{API}/transactions",
        headers=a_owner.h,
        json={
            "type": "expense",
            "amount": "5",
            "scope": "family",
            "family_id": fa,
            "account_id": b_acc["id"],
        },
    )
    assert r.status_code == 404
    assert await row_count() == 1


# --- notifications ----------------------------------------------------------------------


async def test_family_shared_expense_notifies_other_members_only(client):
    fid, p = await household(client)
    fam = {"scope": "family", "family_id": fid}
    t = await tx(client, p["member"], amount="120", description="Groceries", **fam)
    got = await shared_expense_notifications()
    key = f"tx:{t['id']}:created"
    assert got == sorted((p[r].id, key) for r in ("owner", "admin", "viewer"))

    # not for: pending, income, personal rows, replays or edits
    await tx(client, p["member"], status="pending", **fam)
    await tx(client, p["member"], type="income", amount="5", **fam)
    await tx(client, p["member"])
    await client.patch(
        f"{API}/transactions/{t['id']}", headers=p["member"].h, json={"amount": "130"}
    )
    assert len(await shared_expense_notifications()) == 3

    async with get_database().sessionmaker() as s:
        n = await s.scalar(
            select(Notification)
            .where(Notification.user_id == uuid.UUID(p["owner"].id))
            .where(Notification.category == NotificationCategory.FAMILY_SHARED_EXPENSE)
        )
        assert n is not None and n.family_id == uuid.UUID(fid)
        assert "Member" in n.body and "120.00" in n.body and "Groceries" in n.body
        assert n.data["transaction_id"] == t["id"]


# --- internal API for UPI / recurring ---------------------------------------------------


async def test_create_internal_and_mark_status(client):
    fid, p = await household(client)
    async with get_database().sessionmaker() as s:
        member = await s.get(User, uuid.UUID(p["member"].id))
        viewer = await s.get(User, uuid.UUID(p["viewer"].id))
        assert member is not None and viewer is not None
        svc = TransactionService(s)
        data = TransactionCreate(
            type=TransactionType.EXPENSE,
            amount=Decimal("250"),
            scope="family",
            family_id=uuid.UUID(fid),
            category_id=uuid.UUID(EXPENSE_CAT),
            description="UPI to Ravi",
        )
        row, created = await svc.create_internal(
            member,
            data,
            source=TransactionSource.UPI_INTENT,
            status=TransactionStatus.PENDING,
            idempotency_key="upi:attempt-1",
        )
        row_id = row.id
        assert created and row.status is TransactionStatus.PENDING
        assert row.source is TransactionSource.UPI_INTENT
        assert row.verification is VerificationStatus.UNVERIFIED
        again, created_again = await svc.create_internal(
            member, data, idempotency_key="upi:attempt-1"
        )
        assert again.id == row_id and created_again is False
        await s.commit()
        # pending never counts; no notification yet
        totals = await ledger.totals(s, Transaction.family_id == uuid.UUID(fid))
        assert totals["expense"] == Decimal("0.00")
        assert await shared_expense_notifications() == []

        with pytest.raises(ForbiddenError):
            await svc.create_internal(viewer, data)
        with pytest.raises(InvalidTransactionError) as err:
            await svc.create_internal(
                member, data.model_copy(update={"category_id": uuid.UUID(INCOME_CAT)})
            )
        assert err.value.code == "category_kind_mismatch"

        done = await svc.mark_status(
            row_id,
            TransactionStatus.COMPLETED,
            actor=member,
            verification=VerificationStatus.USER_CONFIRMED,
            external_ref="UTR123456",
            reason="user confirmed in app",
        )
        assert done.status is TransactionStatus.COMPLETED and done.external_ref == "UTR123456"
        await s.commit()
        totals = await ledger.totals(s, Transaction.family_id == uuid.UUID(fid))
        assert totals["expense"] == Decimal("250.00")
        notified = await shared_expense_notifications()
        assert len(notified) == 3 and p["member"].id not in {u for u, _ in notified}
        # flipping again never notifies twice
        await svc.mark_status(row_id, TransactionStatus.PENDING, actor=member)
        await svc.mark_status(row_id, TransactionStatus.COMPLETED, actor=member)
        await s.commit()
        assert len(await shared_expense_notifications()) == 3
        with pytest.raises(NotFoundError):
            await svc.mark_status(uuid.uuid4(), TransactionStatus.FAILED, actor=member)

        status_audits = list(
            await s.scalars(
                select(AuditLog.details).where(
                    AuditLog.action == "transaction.status_changed", AuditLog.entity_id == row_id
                )
            )
        )
        assert len(status_audits) == 3
        assert any(
            d["changes"].get("verification") == {"before": "unverified", "after": "user_confirmed"}
            for d in status_audits
        )

    # the generic API refuses to change the status of a UPI row
    r = await client.patch(
        f"{API}/transactions/{row_id}", headers=p["member"].h, json={"status": "failed"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "status_change_not_allowed"
    r = await client.patch(
        f"{API}/transactions/{row_id}", headers=p["member"].h, json={"description": "Ravi"}
    )
    assert r.status_code == 200 and r.json()["status"] == "completed"


async def test_create_internal_recurring_period_is_unique(client):
    asha = await user(client, "asha")
    async with get_database().sessionmaker() as s:
        me = await s.get(User, uuid.UUID(asha.id))
        assert me is not None
        rule = RecurringTransaction(
            owner_user_id=me.id,
            name="Rent",
            type=TransactionType.EXPENSE,
            amount=Decimal("15000"),
            frequency=RecurrenceFrequency.MONTHLY,
            start_date=date(2026, 11, 1),
            next_due_date=date(2026, 12, 1),
        )
        s.add(rule)
        await s.commit()
        svc = TransactionService(s)
        data = TransactionCreate(type=TransactionType.EXPENSE, amount=Decimal("15000"))
        first, created = await svc.create_internal(
            me,
            data,
            source=TransactionSource.RECURRING,
            status=TransactionStatus.PENDING,
            recurring_id=rule.id,
            period_key="2026-11",
        )
        assert created and first.recurring_id == rule.id
        with pytest.raises(ConflictError) as err:
            await svc.create_internal(me, data, recurring_id=rule.id, period_key="2026-11")
        assert err.value.code == "recurring_period_exists"
        # the savepoint kept the outer transaction usable
        second, _ = await svc.create_internal(me, data, recurring_id=rule.id, period_key="2026-12")
        await s.commit()
        n = await s.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.recurring_id == rule.id)
        )
        assert n == 2 and second.period_key == "2026-12"
        with pytest.raises(ValueError, match="together"):
            await svc.create_internal(me, data, recurring_id=rule.id)
