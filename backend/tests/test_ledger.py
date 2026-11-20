"""Accounting rules (app/services/ledger.py) and the notify helper (app/services/notify.py)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.session import get_database
from app.models import (
    Account,
    AccountType,
    Notification,
    NotificationCategory,
    NotificationPreference,
    PushStatus,
    RecordScope,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from app.services import ledger
from app.services.notify import notify
from tests.conftest import register_verified

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _user_id(client) -> uuid.UUID:
    auth = await register_verified(client)
    return uuid.UUID(auth["user"]["id"])


def _tx(owner: uuid.UUID, type_: TransactionType, amount: str, **kw: Any) -> Transaction:
    return Transaction(
        owner_user_id=owner,
        scope=RecordScope.PERSONAL,
        type=type_,
        amount=Decimal(amount),
        occurred_at=NOW,
        **kw,
    )


async def test_balances_and_totals_follow_accounting_rules(client):
    uid = await _user_id(client)
    async with get_database().sessionmaker() as s:
        bank = Account(
            owner_user_id=uid,
            scope=RecordScope.PERSONAL,
            name="Bank",
            type=AccountType.BANK,
            opening_balance=Decimal("1000.00"),
        )
        cash = Account(
            owner_user_id=uid, scope=RecordScope.PERSONAL, name="Cash", type=AccountType.CASH
        )
        s.add_all([bank, cash])
        await s.flush()
        s.add_all(
            [
                _tx(uid, TransactionType.INCOME, "500.00", account_id=bank.id),
                _tx(uid, TransactionType.EXPENSE, "200.25", account_id=bank.id),
                _tx(uid, TransactionType.REFUND, "50.00", account_id=bank.id),
                _tx(
                    uid,
                    TransactionType.TRANSFER,
                    "300.00",
                    account_id=bank.id,
                    to_account_id=cash.id,
                ),
                _tx(uid, TransactionType.EXPENSE, "40.00", account_id=cash.id),
                _tx(
                    uid, TransactionType.ADJUSTMENT, "10.00", account_id=cash.id, adjustment_sign=-1
                ),
                _tx(uid, TransactionType.ADJUSTMENT, "5.00", account_id=bank.id, adjustment_sign=1),
                # never counted:
                _tx(
                    uid,
                    TransactionType.EXPENSE,
                    "999.00",
                    account_id=bank.id,
                    status=TransactionStatus.PENDING,
                ),
                _tx(
                    uid,
                    TransactionType.EXPENSE,
                    "888.00",
                    account_id=bank.id,
                    status=TransactionStatus.FAILED,
                ),
                _tx(uid, TransactionType.EXPENSE, "777.00", account_id=bank.id, deleted_at=NOW),
            ]
        )
        await s.commit()

        balances = await ledger.account_balances(s, [bank.id, cash.id])
        # bank: 1000 + 500 - 200.25 + 50 - 300 + 5
        assert balances[bank.id] == Decimal("1054.75")
        # cash: 0 + 300 - 40 - 10
        assert balances[cash.id] == Decimal("250.00")

        t = await ledger.totals(s, Transaction.owner_user_id == uid)
        assert t["income"] == Decimal("500.00")
        # 200.25 + 40 - refund 50; transfers/adjustments/pending/failed/deleted excluded
        assert t["expense"] == Decimal("190.25")
        assert t["net"] == Decimal("309.75")


async def test_account_with_no_movements_shows_opening_balance(client):
    uid = await _user_id(client)
    async with get_database().sessionmaker() as s:
        acct = Account(
            owner_user_id=uid,
            scope=RecordScope.PERSONAL,
            name="Wallet",
            type=AccountType.WALLET,
            opening_balance=Decimal("12.30"),
        )
        s.add(acct)
        await s.commit()
        assert await ledger.account_balances(s, [acct.id]) == {acct.id: Decimal("12.30")}
        assert await ledger.account_balances(s, []) == {}


async def test_adjustment_requires_sign(client):
    uid = await _user_id(client)
    async with get_database().sessionmaker() as s:
        s.add(_tx(uid, TransactionType.ADJUSTMENT, "1.00"))
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()
        s.add(_tx(uid, TransactionType.EXPENSE, "1.00", adjustment_sign=1))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_notify_dedupes_and_respects_preferences(client):
    uid = await _user_id(client)
    async with get_database().sessionmaker() as s:
        kw: dict[str, Any] = {
            "user_id": uid,
            "category": NotificationCategory.BUDGET_ALERT,
            "title": "Budget 75% used",
            "body": "Groceries is at 75%",
            "dedupe_key": "budget:x:2026-11:75",
        }
        assert await notify(s, **kw) is True
        assert await notify(s, **kw) is False  # same event again → no duplicate
        await s.commit()

        s.add(
            NotificationPreference(
                user_id=uid, category=NotificationCategory.BILL_UPCOMING, push_enabled=False
            )
        )
        s.add(
            NotificationPreference(
                user_id=uid,
                category=NotificationCategory.UNUSUAL_SPENDING,
                push_enabled=False,
                in_app_enabled=False,
            )
        )
        await s.commit()
        assert await notify(
            s,
            user_id=uid,
            category=NotificationCategory.BILL_UPCOMING,
            title="Rent due",
            body="Rent is due tomorrow",
            dedupe_key="bill:1",
        )
        assert not await notify(
            s,
            user_id=uid,
            category=NotificationCategory.UNUSUAL_SPENDING,
            title="x",
            body="y",
            dedupe_key="unusual:1",
        )
        await s.commit()

        rows = {
            n.dedupe_key: n
            for n in await s.scalars(select(Notification).where(Notification.user_id == uid))
        }
        assert set(rows) == {"budget:x:2026-11:75", "bill:1"}
        assert rows["budget:x:2026-11:75"].push_status == PushStatus.PENDING
        assert rows["bill:1"].push_status == PushStatus.SKIPPED
        assert rows["bill:1"].data["category"] == "bill_upcoming"
        count = await s.scalar(select(func.count()).select_from(Notification))
        assert count == 2
