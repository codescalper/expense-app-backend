"""Accounts (/api/v1/accounts): CRUD, computed balances, family permissions and isolation."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.session import get_database
from app.models.system import AuditLog
from tests.helpers_transactions import (
    API,
    account,
    household,
    tx,
    user,
)


async def test_personal_account_crud_archive_and_delete(client):
    asha = await user(client, "asha")
    acc = await account(client, asha, "HDFC Savings", institution="HDFC", opening_balance="1500.5")
    assert acc["type"] == "bank" and acc["institution"] == "HDFC" and acc["currency"] == "INR"
    assert acc["opening_balance"] == "1500.50" and acc["current_balance"] == "1500.50"
    assert acc["scope"] == "personal" and acc["family_id"] is None
    assert acc["owner_user_id"] == asha.id and acc["is_active"] is True
    assert acc["can_manage"] is True

    cash = await account(client, asha, "Cash", type="cash")
    listed = (await client.get(f"{API}/accounts", headers=asha.h)).json()
    assert [a["name"] for a in listed] == ["Cash", "HDFC Savings"]

    got = await client.get(f"{API}/accounts/{acc['id']}", headers=asha.h)
    assert got.status_code == 200 and got.json()["name"] == "HDFC Savings"

    r = await client.patch(
        f"{API}/accounts/{acc['id']}",
        headers=asha.h,
        json={"name": "HDFC", "type": "debit_card", "institution": None, "opening_balance": "-20"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "HDFC" and body["type"] == "debit_card"
    assert body["institution"] is None and body["opening_balance"] == "-20.00"
    assert body["current_balance"] == "-20.00"

    # Archive hides it from the default list but keeps it retrievable.
    r = await client.post(f"{API}/accounts/{cash['id']}/archive", headers=asha.h)
    assert r.status_code == 200 and r.json()["is_active"] is False
    names = [a["name"] for a in (await client.get(f"{API}/accounts", headers=asha.h)).json()]
    assert names == ["HDFC"]
    every = (
        await client.get(f"{API}/accounts", headers=asha.h, params={"include_inactive": "true"})
    ).json()
    assert [a["name"] for a in every] == ["HDFC", "Cash"]
    r = await client.post(f"{API}/accounts/{cash['id']}/unarchive", headers=asha.h)
    assert r.json()["is_active"] is True
    r = await client.patch(
        f"{API}/accounts/{cash['id']}", headers=asha.h, json={"is_active": False}
    )
    assert r.json()["is_active"] is False

    # An account with live transactions can't be deleted; after deleting them it can.
    t = await tx(client, asha, account_id=acc["id"])
    r = await client.delete(f"{API}/accounts/{acc['id']}", headers=asha.h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "account_in_use"
    assert (await client.delete(f"{API}/transactions/{t['id']}", headers=asha.h)).status_code == 204
    assert (await client.delete(f"{API}/accounts/{acc['id']}", headers=asha.h)).status_code == 204
    assert (await client.get(f"{API}/accounts/{acc['id']}", headers=asha.h)).status_code == 404
    assert (await client.delete(f"{API}/accounts/{acc['id']}", headers=asha.h)).status_code == 404

    async with get_database().sessionmaker() as s:
        actions = list(
            await s.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "account")
                .order_by(AuditLog.created_at)
            )
        )
    assert actions.count("account.created") == 2
    assert {"account.updated", "account.archived", "account.unarchived", "account.deleted"} <= set(
        actions
    )


async def test_account_validation_and_no_credentials(client):
    asha = await user(client, "asha")
    bad_bodies = [
        {"name": "", "type": "bank"},
        {"name": "x" * 81, "type": "bank"},
        {"name": "A", "type": "piggy"},
        {"name": "A", "type": "bank", "opening_balance": "1.234"},
        {"name": "A", "type": "bank", "currency": "inr"},
        {"name": "A", "type": "bank", "scope": "family"},
        {"name": "A", "type": "bank", "family_id": str(uuid.uuid4())},
        # credentials are never accepted
        {"name": "A", "type": "bank", "password": "hunter2"},
        {"name": "A", "type": "credit_card", "card_number": "4111111111111111"},
        {"name": "A", "type": "credit_card", "cvv": "123"},
        {"name": "A", "type": "upi", "upi_pin": "1234"},
    ]
    for body in bad_bodies:
        r = await client.post(f"{API}/accounts", headers=asha.h, json=body)
        assert r.status_code == 422, body
    acc = await account(client, asha)
    for body in ({"name": None}, {"is_active": None}, {"login": "x"}, {"currency": "USD"}):
        r = await client.patch(f"{API}/accounts/{acc['id']}", headers=asha.h, json=body)
        assert r.status_code == 422, body
    assert (await client.get(f"{API}/accounts")).status_code == 401


async def test_family_accounts_permissions(client):
    fid, p = await household(client)
    outsider = await user(client, "outsider")

    for role in ("member", "viewer"):
        r = await client.post(
            f"{API}/accounts",
            headers=p[role].h,
            json={"name": "Joint", "type": "bank", "scope": "family", "family_id": fid},
        )
        assert r.status_code == 403, role
        assert r.json()["error"]["code"] == "insufficient_family_role"
    r = await client.post(
        f"{API}/accounts",
        headers=outsider.h,
        json={"name": "Joint", "type": "bank", "scope": "family", "family_id": fid},
    )
    assert r.status_code == 404

    joint = await account(client, p["admin"], "Joint", scope="family", family_id=fid)
    assert joint["scope"] == "family" and joint["family_id"] == fid

    # Every member sees it (only with family_id); only owner/admin may manage it.
    for role, can_manage in (
        ("owner", True),
        ("admin", True),
        ("member", False),
        ("viewer", False),
    ):
        listed = (
            await client.get(f"{API}/accounts", headers=p[role].h, params={"family_id": fid})
        ).json()
        assert [(a["id"], a["can_manage"]) for a in listed] == [(joint["id"], can_manage)]
        personal = (await client.get(f"{API}/accounts", headers=p[role].h)).json()
        assert personal == []
        got = await client.get(f"{API}/accounts/{joint['id']}", headers=p[role].h)
        assert got.status_code == 200

    for role in ("member", "viewer"):
        h = p[role].h
        assert (
            await client.patch(f"{API}/accounts/{joint['id']}", headers=h, json={"name": "X"})
        ).status_code == 403
        assert (
            await client.post(f"{API}/accounts/{joint['id']}/archive", headers=h)
        ).status_code == 403
        assert (await client.delete(f"{API}/accounts/{joint['id']}", headers=h)).status_code == 403

    r = await client.patch(
        f"{API}/accounts/{joint['id']}", headers=p["owner"].h, json={"name": "Household"}
    )
    assert r.status_code == 200 and r.json()["name"] == "Household"

    # Outsiders: 404 everywhere.
    assert (
        await client.get(f"{API}/accounts", headers=outsider.h, params={"family_id": fid})
    ).status_code == 404
    for method, path in (
        ("GET", f"/accounts/{joint['id']}"),
        ("PATCH", f"/accounts/{joint['id']}"),
        ("POST", f"/accounts/{joint['id']}/archive"),
        ("DELETE", f"/accounts/{joint['id']}"),
    ):
        r = await client.request(
            method,
            f"{API}{path}",
            headers=outsider.h,
            json={"name": "x"} if method == "PATCH" else None,
        )
        assert r.status_code == 404, (method, path)

    async with get_database().sessionmaker() as s:
        audited = list(
            await s.scalars(select(AuditLog.action).where(AuditLog.family_id == uuid.UUID(fid)))
        )
    assert "account.created" in audited and "account.updated" in audited


async def test_user_cannot_touch_another_users_account(client):
    """Critical scenario 1 for accounts: B gets 404 for everything of A's."""
    a = await user(client, "alice")
    b = await user(client, "bob")
    acc = await account(client, a, "Alice Bank", opening_balance="100")
    assert (await client.get(f"{API}/accounts", headers=b.h)).json() == []
    for method, path in (
        ("GET", f"/accounts/{acc['id']}"),
        ("PATCH", f"/accounts/{acc['id']}"),
        ("POST", f"/accounts/{acc['id']}/archive"),
        ("POST", f"/accounts/{acc['id']}/unarchive"),
        ("DELETE", f"/accounts/{acc['id']}"),
    ):
        r = await client.request(
            method, f"{API}{path}", headers=b.h, json={"name": "x"} if method == "PATCH" else None
        )
        assert r.status_code == 404, (method, path)
    # nor use it in a transaction
    r = await client.post(
        f"{API}/transactions",
        headers=b.h,
        json={"type": "expense", "amount": "1", "account_id": acc["id"]},
    )
    assert r.status_code == 404
    assert (await client.get(f"{API}/accounts/{acc['id']}", headers=a.h)).json()["name"] == (
        "Alice Bank"
    )


async def test_balances_follow_transactions(client):
    asha = await user(client, "asha")
    bank = await account(client, asha, "Bank", opening_balance="1000")
    cash = await account(client, asha, "Cash", type="cash")
    await tx(client, asha, type="income", amount="500", account_id=bank["id"])
    spent = await tx(client, asha, amount="200.25", account_id=bank["id"])
    await tx(client, asha, type="refund", amount="50", account_id=bank["id"])
    await tx(
        client, asha, type="transfer", amount="300", account_id=bank["id"], to_account_id=cash["id"]
    )
    await tx(client, asha, amount="40", account_id=cash["id"])
    await tx(
        client, asha, type="adjustment", amount="10", account_id=cash["id"], adjustment_sign=-1
    )
    await tx(client, asha, amount="999", account_id=bank["id"], status="pending")  # not counted

    async def balances() -> dict[str, str]:
        rows = (await client.get(f"{API}/accounts", headers=asha.h)).json()
        return {a["name"]: a["current_balance"] for a in rows}

    assert await balances() == {"Bank": "1049.75", "Cash": "250.00"}
    r = await client.patch(
        f"{API}/transactions/{spent['id']}", headers=asha.h, json={"amount": "100.25"}
    )
    assert r.status_code == 200
    assert await balances() == {"Bank": "1149.75", "Cash": "250.00"}
    await client.delete(f"{API}/transactions/{spent['id']}", headers=asha.h)
    assert await balances() == {"Bank": "1250.00", "Cash": "250.00"}
    await client.post(f"{API}/transactions/{spent['id']}/restore", headers=asha.h)
    assert await balances() == {"Bank": "1149.75", "Cash": "250.00"}
    got = (await client.get(f"{API}/accounts/{cash['id']}", headers=asha.h)).json()
    assert got["current_balance"] == "250.00"
