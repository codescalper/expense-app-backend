"""Shared helpers for the category / payee / attachment tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.core.security import utcnow
from app.db.session import get_database
from app.models.enums import (
    FamilyRole,
    RecordScope,
    TransactionStatus,
    TransactionType,
)
from app.models.family import FamilyMember
from app.models.finance import Transaction, TransactionTag
from tests.conftest import BUILTIN_CATEGORIES, bearer, register_verified

API = "/api/v1"
BUILTIN = {c["name"]: c["id"] for c in BUILTIN_CATEGORIES}


class Actor:
    def __init__(self, auth: dict[str, Any]) -> None:
        self.id = auth["user"]["id"]
        self.email = auth["user"]["email"]
        self.h = bearer(auth["tokens"])


async def user(client: Any, name: str) -> Actor:
    return Actor(
        await register_verified(client, email=f"{name}@example.com", full_name=name.title())
    )


async def create_family(client: Any, owner: Actor, name: str = "Rao Family") -> str:
    r = await client.post(f"{API}/families", headers=owner.h, json={"name": name})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def add_member(family_id: str, who: Actor, role: FamilyRole) -> None:
    async with get_database().sessionmaker() as s:
        s.add(
            FamilyMember(
                family_id=uuid.UUID(family_id),
                user_id=uuid.UUID(who.id),
                role=role,
                joined_at=utcnow(),
            )
        )
        await s.commit()


async def household(client: Any) -> tuple[str, dict[str, Actor]]:
    """A family with one user per role (owner, admin, member, viewer)."""
    people = {n: await user(client, n) for n in ("owner", "admin", "member", "viewer")}
    fam = await create_family(client, people["owner"])
    await add_member(fam, people["admin"], FamilyRole.ADMIN)
    await add_member(fam, people["member"], FamilyRole.MEMBER)
    await add_member(fam, people["viewer"], FamilyRole.VIEWER)
    return fam, people


async def insert_tx(
    owner: Actor,
    *,
    family_id: str | None = None,
    category_id: str | None = None,
    payee_id: str | None = None,
    merchant: str | None = None,
    type_: TransactionType = TransactionType.EXPENSE,
    status: TransactionStatus = TransactionStatus.COMPLETED,
    amount: str = "100.00",
    deleted: bool = False,
    tags: tuple[str, ...] = (),
) -> str:
    """Insert a transaction directly (the transactions API is built separately)."""
    async with get_database().sessionmaker() as s:
        tx = Transaction(
            owner_user_id=uuid.UUID(owner.id),
            family_id=uuid.UUID(family_id) if family_id else None,
            scope=RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
            type=type_,
            status=status,
            amount=Decimal(amount),
            category_id=uuid.UUID(category_id) if category_id else None,
            payee_id=uuid.UUID(payee_id) if payee_id else None,
            merchant=merchant,
            occurred_at=utcnow(),
            deleted_at=utcnow() if deleted else None,
            adjustment_sign=1 if type_ is TransactionType.ADJUSTMENT else None,
        )
        s.add(tx)
        await s.flush()
        for tag in tags:
            s.add(TransactionTag(transaction_id=tx.id, name=tag))
        await s.commit()
        return str(tx.id)


async def tx_category(tx_id: str) -> str | None:
    async with get_database().sessionmaker() as s:
        tx = await s.get(Transaction, uuid.UUID(tx_id))
        assert tx is not None
        return str(tx.category_id) if tx.category_id else None
