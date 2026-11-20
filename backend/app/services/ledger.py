"""Financial aggregation rules — the ONE place that defines what counts as income, expense and
balance. Budgets, reports, dashboard and account balances must all use these helpers so numbers
agree everywhere.

Rules (spec §5, §7, §12, §17):

* Only rows that are **not soft-deleted** and have status **completed** count. Pending (e.g. a UPI
  payment the user hasn't confirmed), failed and cancelled rows never count.
* **Expense** = expenses - refunds (a refund reduces spending, in the refund's category).
* **Income** = income rows only.
* **Transfers never count** as income or expense; they only move money between accounts.
* **Adjustments** correct an account balance (``adjustment_sign`` ±1) and never count as
  income or expense.
* Account balance = opening balance
  + income + refunds - expenses ± adjustments (rows whose ``account_id`` is the account)
  - transfers out (``account_id``) + transfers in (``to_account_id``).

All money stays ``Decimal``. Callers apply visibility (``ScopeFilter``) themselves; these helpers
only express the accounting rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TransactionStatus, TransactionType
from app.models.finance import Account, Transaction

ZERO = Decimal("0.00")


def counted(model: type[Transaction] = Transaction) -> ColumnElement[bool]:
    """Rows that count towards any total: live and completed."""
    return and_(model.deleted_at.is_(None), model.status == TransactionStatus.COMPLETED)


def expense_amount(model: type[Transaction] = Transaction) -> ColumnElement[Any]:
    """Signed contribution to *spending*: expense +amount, refund -amount, everything else 0."""
    return case(
        (model.type == TransactionType.EXPENSE, model.amount),
        (model.type == TransactionType.REFUND, -model.amount),
        else_=literal(ZERO),
    )


def income_amount(model: type[Transaction] = Transaction) -> ColumnElement[Any]:
    """Contribution to *income*: income +amount, everything else 0."""
    return case((model.type == TransactionType.INCOME, model.amount), else_=literal(ZERO))


def is_spending(model: type[Transaction] = Transaction) -> ColumnElement[bool]:
    """Rows that affect spending totals (expenses and refunds)."""
    return model.type.in_([TransactionType.EXPENSE, TransactionType.REFUND])


def source_account_delta(model: type[Transaction] = Transaction) -> ColumnElement[Any]:
    """Effect on the balance of ``account_id``."""
    return case(
        (model.type == TransactionType.INCOME, model.amount),
        (model.type == TransactionType.REFUND, model.amount),
        (model.type == TransactionType.EXPENSE, -model.amount),
        (model.type == TransactionType.TRANSFER, -model.amount),
        (
            model.type == TransactionType.ADJUSTMENT,
            model.amount * func.coalesce(model.adjustment_sign, 1),
        ),
        else_=literal(ZERO),
    )


async def account_balances(
    session: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Current balance for each account id (opening balance + counted movements).

    Visibility is the caller's job: pass only accounts the user may see. Movements are summed
    regardless of who recorded them (a family account's balance includes every member's rows).
    """
    ids = list(dict.fromkeys(account_ids))
    if not ids:
        return {}
    outgoing = select(
        Transaction.account_id.label("account_id"), source_account_delta().label("delta")
    ).where(counted(), Transaction.account_id.in_(ids))
    incoming = select(
        Transaction.to_account_id.label("account_id"), Transaction.amount.label("delta")
    ).where(
        counted(),
        Transaction.type == TransactionType.TRANSFER,
        Transaction.to_account_id.in_(ids),
    )
    movements = union_all(outgoing, incoming).subquery()
    totals = (
        select(movements.c.account_id, func.coalesce(func.sum(movements.c.delta), 0))
        .group_by(movements.c.account_id)
        .subquery()
    )
    rows = await session.execute(
        select(Account.id, Account.opening_balance + func.coalesce(totals.c[1], 0))
        .outerjoin(totals, totals.c.account_id == Account.id)
        .where(Account.id.in_(ids))
    )
    return {row[0]: Decimal(row[1]).quantize(Decimal("0.01")) for row in rows}


async def totals(session: AsyncSession, *where: ColumnElement[bool]) -> dict[str, Decimal]:
    """Income / expense / net for counted rows matching ``where`` (caller adds visibility and
    date filters)."""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(income_amount()), 0),
                func.coalesce(func.sum(expense_amount()), 0),
            ).where(counted(), *where)
        )
    ).one()
    income = Decimal(row[0]).quantize(Decimal("0.01"))
    expense = Decimal(row[1]).quantize(Decimal("0.01"))
    return {"income": income, "expense": expense, "net": income - expense}
