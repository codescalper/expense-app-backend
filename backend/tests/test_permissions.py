"""Permission matrix, ScopeFilter tenant isolation on real rows, and schema constraints."""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.core.permissions import (
    FamilyPermission,
    ScopeFilter,
    can_manage_member,
    can_modify_record,
    list_user_family_ids,
    list_user_family_roles,
    require_family_access,
    require_record_write,
    role_at_least,
    role_has,
    visible_categories_clause,
)
from app.core.security import utcnow
from app.db.session import get_database
from app.models import (
    Family,
    FamilyMember,
    FamilyRole,
    RecordScope,
    Transaction,
    TransactionCategory,
    TransactionType,
    User,
)
from app.models.enums import CategoryKind
from app.utils.pagination import Page, PageParams, paginate
from tests.conftest import BUILTIN_CATEGORIES

OWN, A, M, V = FamilyRole.OWNER, FamilyRole.ADMIN, FamilyRole.MEMBER, FamilyRole.VIEWER
P = FamilyPermission


# --- pure matrix -----------------------------------------------------------------------


def test_permission_matrix():
    expected = {
        P.VIEW: {OWN, A, M, V},
        P.CREATE_RECORDS: {OWN, A, M},
        P.MANAGE_ALL_RECORDS: {OWN, A},
        P.MANAGE_BUDGETS: {OWN, A},
        P.MANAGE_SHARED_SETUP: {OWN, A},
        P.UPDATE_FAMILY: {OWN, A},
        P.INVITE_MEMBERS: {OWN, A},
        P.MANAGE_MEMBERS: {OWN, A},
        P.TRANSFER_OWNERSHIP: {OWN},
        P.DELETE_FAMILY: {OWN},
    }
    assert set(expected) == set(FamilyPermission)
    for perm, roles in expected.items():
        assert {r for r in FamilyRole if role_has(r, perm)} == roles, perm
    assert role_at_least(OWN, A) and role_at_least(A, A) and not role_at_least(M, A)


def test_can_manage_member():
    assert can_manage_member(OWN, A, M) and can_manage_member(OWN, V) and can_manage_member(A, A, V)
    assert can_manage_member(A, M, A)
    assert not can_manage_member(A, OWN)  # nobody manages the owner
    assert not can_manage_member(OWN, OWN, A)
    assert not can_manage_member(OWN, M, OWN)  # owner is never granted, only transferred
    assert not can_manage_member(M, V) and not can_manage_member(V, V)


# --- fixtures on real rows -------------------------------------------------------------


async def _user(s: AsyncSession, name: str) -> User:
    u = User(email=f"{name}@example.com", password_hash="!", full_name=name)
    s.add(u)
    await s.flush()
    return u


async def _family(s: AsyncSession, owner: User, name: str, **members: tuple[User, FamilyRole]):
    f = Family(name=name, created_by_user_id=owner.id)
    s.add(f)
    await s.flush()
    s.add(FamilyMember(family_id=f.id, user_id=owner.id, role=OWN, joined_at=utcnow()))
    for u, role in members.values():
        s.add(FamilyMember(family_id=f.id, user_id=u.id, role=role, joined_at=utcnow()))
    await s.flush()
    return f


def _tx(owner: User, family: Family | None = None, amount: str = "10.00", **kw: Any) -> Transaction:
    return Transaction(
        owner_user_id=owner.id,
        family_id=family.id if family else None,
        scope=RecordScope.FAMILY if family else RecordScope.PERSONAL,
        type=TransactionType.EXPENSE,
        amount=Decimal(amount),
        occurred_at=utcnow(),
        description=kw.pop("description", None),
        **kw,
    )


async def _visible(s: AsyncSession, scope: ScopeFilter) -> set[str]:
    rows = await s.scalars(scope.apply(select(Transaction), Transaction))
    return {t.description or "" for t in rows}


@pytest.fixture
async def world() -> Any:
    """alice: owner of FA (carol member, vic viewer); bob: owner of FB (dave admin)."""
    async with get_database().sessionmaker() as s:
        alice, bob, carol, dave, vic = [
            await _user(s, n) for n in ("alice", "bob", "carol", "dave", "vic")
        ]
        fa = await _family(s, alice, "FA", c=(carol, M), v=(vic, V))
        fb = await _family(s, bob, "FB", d=(dave, A))
        s.add_all(
            [
                _tx(alice, description="alice-personal"),
                _tx(bob, description="bob-personal"),
                _tx(carol, description="carol-personal"),
                _tx(alice, fa, description="fa-by-alice"),
                _tx(carol, fa, description="fa-by-carol"),
                _tx(bob, fb, description="fb-by-bob"),
                _tx(dave, fb, description="fb-by-dave"),
                _tx(alice, description="alice-deleted", deleted_at=utcnow()),
            ]
        )
        await s.commit()
    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "dave": dave,
        "vic": vic,
        "fa": fa,
        "fb": fb,
    }


async def test_scope_filter_isolates_users_and_families(world):
    w = world
    async with get_database().sessionmaker() as s:
        assert await _visible(s, ScopeFilter.for_user(w["alice"].id)) == {
            "alice-personal",
            "fa-by-alice",
            "fa-by-carol",
        }
        assert await _visible(s, ScopeFilter.for_user(w["carol"].id)) == {
            "carol-personal",
            "fa-by-alice",
            "fa-by-carol",
        }
        assert await _visible(s, ScopeFilter.for_user(w["bob"].id)) == {
            "bob-personal",
            "fb-by-bob",
            "fb-by-dave",
        }
        assert await _visible(s, ScopeFilter.for_user(w["vic"].id)) == {
            "fa-by-alice",
            "fa-by-carol",
        }
        # Explicit scopes.
        personal = ScopeFilter.for_user(w["alice"].id, scope=RecordScope.PERSONAL)
        assert await _visible(s, personal) == {"alice-personal"}
        fam = ScopeFilter.for_user(w["alice"].id, scope=RecordScope.FAMILY)
        assert await _visible(s, fam) == {"fa-by-alice", "fa-by-carol"}
        only_fa = ScopeFilter.for_user(w["alice"].id, family_id=w["fa"].id)
        assert await _visible(s, only_fa) == {"fa-by-alice", "fa-by-carol"}
        # Asking for another family's rows yields nothing, even with its real id.
        assert await _visible(s, ScopeFilter.for_user(w["alice"].id, family_id=w["fb"].id)) == set()
        # Soft-deleted rows only on request.
        rows = await s.scalars(
            ScopeFilter.for_user(w["alice"].id, scope=RecordScope.PERSONAL).apply(
                select(Transaction), Transaction, include_deleted=True
            )
        )
        assert {t.description for t in rows} == {"alice-personal", "alice-deleted"}
        with pytest.raises(ValueError):
            ScopeFilter.for_user(w["alice"].id, family_id=w["fa"].id, scope=RecordScope.PERSONAL)


async def test_scope_filter_follows_membership_changes(world):
    w = world
    async with get_database().sessionmaker() as s:
        member = await s.scalar(select(FamilyMember).where(FamilyMember.user_id == w["carol"].id))
        assert member is not None
        member.left_at = utcnow()
        await s.commit()
        assert await _visible(s, ScopeFilter.for_user(w["carol"].id)) == {"carol-personal"}
        assert await list_user_family_ids(s, w["carol"].id) == []

        fb = await s.get(Family, w["fb"].id)
        assert fb is not None
        fb.deleted_at = utcnow()
        await s.commit()
        assert await _visible(s, ScopeFilter.for_user(w["dave"].id)) == set()
        assert await _visible(s, ScopeFilter.for_user(w["bob"].id)) == {"bob-personal"}
        assert await list_user_family_roles(s, w["alice"].id) == {w["fa"].id: OWN}


async def test_require_family_access(world):
    w = world
    async with get_database().sessionmaker() as s:
        m = await require_family_access(s, w["vic"].id, w["fa"].id)
        assert m.role is V
        with pytest.raises(ForbiddenError):
            await require_family_access(s, w["vic"].id, w["fa"].id, P.CREATE_RECORDS)
        with pytest.raises(NotFoundError):
            await require_family_access(s, w["bob"].id, w["fa"].id)
        with pytest.raises(NotFoundError):
            await require_family_access(s, w["alice"].id, uuid.uuid4())
        alice = await s.get(User, w["alice"].id)
        assert alice is not None
        assert (await require_family_access(s, alice, w["fa"].id, P.DELETE_FAMILY)).role is OWN


async def test_record_write_rules(world):
    w = world
    async with get_database().sessionmaker() as s:
        by_desc = {t.description: t for t in await s.scalars(select(Transaction))}

        # Member edits own family row, not someone else's.
        assert await require_record_write(s, w["carol"].id, by_desc["fa-by-carol"]) is not None
        with pytest.raises(ForbiddenError):
            await require_record_write(s, w["carol"].id, by_desc["fa-by-alice"])
        # Owner/admin edit any family row of their family.
        await require_record_write(s, w["alice"].id, by_desc["fa-by-carol"])
        await require_record_write(s, w["dave"].id, by_desc["fb-by-bob"])
        # Viewer never.
        with pytest.raises(ForbiddenError):
            await require_record_write(s, w["vic"].id, by_desc["fa-by-alice"])
        # Invisible rows are 404: other people's personal rows and other families' rows.
        for uid, desc in [
            ("alice", "bob-personal"),
            ("alice", "fb-by-bob"),
            ("carol", "alice-personal"),
            ("bob", "fa-by-alice"),
            ("alice", "alice-deleted"),
        ]:
            with pytest.raises(NotFoundError):
                await require_record_write(s, w[uid].id, by_desc[desc])
        with pytest.raises(NotFoundError):
            await require_record_write(s, w["alice"].id, None)
        assert await require_record_write(s, w["alice"].id, by_desc["alice-personal"]) is None

        carol_m = await s.scalar(select(FamilyMember).where(FamilyMember.user_id == w["carol"].id))
        assert can_modify_record(w["carol"].id, by_desc["fa-by-carol"], carol_m)
        assert not can_modify_record(w["carol"].id, by_desc["fb-by-bob"], carol_m)  # wrong family
        assert not can_modify_record(w["carol"].id, by_desc["fa-by-carol"], None)


async def test_visible_categories(world):
    w = world
    async with get_database().sessionmaker() as s:
        s.add_all(
            [
                TransactionCategory(name="Builtin", kind=CategoryKind.EXPENSE),
                TransactionCategory(
                    name="Alice", kind=CategoryKind.EXPENSE, owner_user_id=w["alice"].id
                ),
                TransactionCategory(
                    name="Bob", kind=CategoryKind.EXPENSE, owner_user_id=w["bob"].id
                ),
                TransactionCategory(name="FA", kind=CategoryKind.EXPENSE, family_id=w["fa"].id),
                TransactionCategory(name="FB", kind=CategoryKind.EXPENSE, family_id=w["fb"].id),
            ]
        )
        await s.commit()

        # Migration-seeded built-ins are visible to everyone too (conftest restores them).
        seeded = {c["name"] for c in BUILTIN_CATEGORIES}

        async def names(clause: Any) -> set[str]:
            return set(await s.scalars(select(TransactionCategory.name).where(clause))) - seeded

        assert await names(visible_categories_clause(w["alice"].id)) == {"Builtin", "Alice", "FA"}
        assert await names(visible_categories_clause(w["carol"].id, family_id=w["fa"].id)) == {
            "Builtin",
            "FA",
        }
        assert await names(visible_categories_clause(w["alice"].id, family_id=w["fb"].id)) == {
            "Builtin"
        }
        all_visible = set(
            await s.scalars(
                select(TransactionCategory.name).where(visible_categories_clause(w["bob"].id))
            )
        )
        assert seeded <= all_visible


async def test_paginate(world):
    w = world
    async with get_database().sessionmaker() as s:
        stmt = ScopeFilter.for_user(w["alice"].id).apply(
            select(Transaction).order_by(Transaction.description), Transaction
        )
        params = PageParams(limit=2, offset=0)
        rows, total = await paginate(s, stmt, params)
        page = Page[str].build([r.description or "" for r in rows], total, params)
        assert page.total == 3 and page.items == ["alice-personal", "fa-by-alice"] and page.has_more
        rows, total = await paginate(s, stmt, PageParams(limit=2, offset=2))
        assert [r.description for r in rows] == ["fa-by-carol"] and total == 3


# --- schema constraints ----------------------------------------------------------------


async def _expect_integrity_error(s: AsyncSession, obj: Any, constraint: str) -> None:
    s.add(obj)
    with pytest.raises(IntegrityError) as exc:
        await s.flush()
    assert constraint in str(exc.value)
    await s.rollback()


async def test_schema_constraints(world):
    w = world
    async with get_database().sessionmaker() as s:
        bad_scope = _tx(w["alice"])
        bad_scope.scope = RecordScope.FAMILY  # family scope without family_id
        await _expect_integrity_error(s, bad_scope, "ck_transactions_scope_family")
        await _expect_integrity_error(
            s, _tx(w["alice"], amount="0"), "ck_transactions_amount_positive"
        )
        await _expect_integrity_error(
            s, _tx(w["alice"], to_account_id=uuid.uuid4()), "ck_transactions_transfer_accounts"
        )

        # Exactly one active owner / one active membership per user.
        await _expect_integrity_error(
            s,
            FamilyMember(family_id=w["fa"].id, user_id=w["bob"].id, role=OWN, joined_at=utcnow()),
            "uq_family_members_active_owner",
        )
        await _expect_integrity_error(
            s,
            FamilyMember(family_id=w["fa"].id, user_id=w["carol"].id, role=V, joined_at=utcnow()),
            "uq_family_members_active_user",
        )
        # Case-insensitive category names per owner (NULLS NOT DISTINCT: built-ins too).
        s.add(TransactionCategory(name="Pets", kind=CategoryKind.EXPENSE))
        await s.commit()
        await _expect_integrity_error(
            s,
            TransactionCategory(name="pets", kind=CategoryKind.EXPENSE),
            "uq_transaction_categories_owner_name",
        )
        s.add(
            TransactionCategory(name="pets", kind=CategoryKind.EXPENSE, owner_user_id=w["alice"].id)
        )
        await s.commit()

        # Idempotency key unique per owner.
        s.add(_tx(w["alice"], idempotency_key="k1"))
        await s.commit()
        await _expect_integrity_error(
            s,
            _tx(w["alice"], idempotency_key="k1"),
            "uq_transactions_owner_user_id_idempotency_key",
        )
        s.add(_tx(w["bob"], idempotency_key="k1"))
        await s.commit()


async def test_recurring_period_is_unique():
    from datetime import date

    from app.models import RecurrenceFrequency, RecurringTransaction

    async with get_database().sessionmaker() as s:
        u = await _user(s, "rec")
        rule = RecurringTransaction(
            owner_user_id=u.id,
            name="Rent",
            type=TransactionType.EXPENSE,
            amount=Decimal("15000"),
            frequency=RecurrenceFrequency.MONTHLY,
            start_date=date(2026, 1, 1),
            next_due_date=date(2026, 12, 1),
        )
        s.add(rule)
        await s.flush()
        s.add(_tx(u, recurring_id=rule.id, period_key="2026-11"))
        await s.commit()
        await _expect_integrity_error(
            s,
            _tx(u, recurring_id=rule.id, period_key="2026-11"),
            "uq_transactions_recurring_id_period_key",
        )


def test_default_categories_seed_matches_spec():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20261120_0002_finance_schema.py"
    spec = importlib.util.spec_from_file_location("mig_0002", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = module.DEFAULT_CATEGORIES
    expense = [r[2] for r in rows if r[1] == "expense"]
    assert expense == [
        "Food & Dining", "Groceries", "Rent", "Utilities", "Electricity", "Internet",
        "Mobile Recharge", "Transportation", "Fuel", "Shopping", "Healthcare", "Education",
        "Entertainment", "Travel", "Insurance", "Subscriptions", "EMI & Loans", "Personal Care",
        "Family Expenses", "Other",
    ]  # fmt: skip
    assert [r[2] for r in rows if r[1] == "income"] == [
        "Salary", "Business", "Interest", "Gifts", "Refunds", "Other Income",
    ]  # fmt: skip
    assert len({r[0] for r in rows}) == len(rows)
    assert all(r[4].startswith("#") and len(r[4]) == 7 for r in rows)
