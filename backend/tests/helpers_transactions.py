"""Shared helpers for the accounts / transactions tests."""

from __future__ import annotations

import uuid
from typing import Any

from app.db.session import get_database
from app.models import Payee, RecordScope, TransactionCategory
from app.models.enums import CategoryKind
from tests.conftest import BUILTIN_CATEGORIES, bearer, register_verified

API = "/api/v1"

EXPENSE_CAT = next(c["id"] for c in BUILTIN_CATEGORIES if c["kind"] == "expense")
INCOME_CAT = next(c["id"] for c in BUILTIN_CATEGORIES if c["kind"] == "income")


class Actor:
    def __init__(self, auth: dict[str, Any]) -> None:
        self.id = auth["user"]["id"]
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
    """A family with one user per role."""
    people = {n: await user(client, n) for n in ("owner", "admin", "member", "viewer")}
    fid = await create_family(client, people["owner"])
    for role in ("admin", "member", "viewer"):
        await join(client, people["owner"], fid, people[role], role)
    return fid, people


async def account(client, who: Actor, name: str = "Bank", **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "type": "bank", **kw}
    r = await client.post(f"{API}/accounts", headers=who.h, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def tx(client, who: Actor, expect: int = 201, **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "expense", "amount": "100.00", **kw}
    r = await client.post(f"{API}/transactions", headers=who.h, json=body)
    assert r.status_code == expect, r.text
    return dict(r.json())


async def add_category(
    *,
    name: str,
    kind: str = "expense",
    owner_user_id: str | None = None,
    family_id: str | None = None,
    parent_id: str | None = None,
) -> str:
    async with get_database().sessionmaker() as s:
        c = TransactionCategory(
            name=name,
            kind=CategoryKind(kind),
            owner_user_id=uuid.UUID(owner_user_id) if owner_user_id else None,
            family_id=uuid.UUID(family_id) if family_id else None,
            parent_id=uuid.UUID(parent_id) if parent_id else None,
        )
        s.add(c)
        await s.commit()
        return str(c.id)


async def add_payee(*, name: str, owner_user_id: str, family_id: str | None = None) -> str:
    async with get_database().sessionmaker() as s:
        p = Payee(
            name=name,
            owner_user_id=uuid.UUID(owner_user_id),
            family_id=uuid.UUID(family_id) if family_id else None,
            scope=RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
        )
        s.add(p)
        await s.commit()
        return str(p.id)
