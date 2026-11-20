"""Integration-pass fixes across modules (categories ↔ budgets, archived references, immutable
transaction fields, ``can_edit`` / ``can_delete`` flags, configurable limits)."""

from __future__ import annotations

import io
import uuid
from datetime import date
from typing import Any

from PIL import Image
from sqlalchemy import func, select, text

from app.db.session import get_database
from app.models import Transaction, UpiPaymentAttempt
from app.models.planning import BudgetCategory
from tests.helpers_transactions import (
    API,
    EXPENSE_CAT,
    Actor,
    account,
    create_family,
    household,
    tx,
    user,
)


def err(r: Any) -> str:
    return str(r.json()["error"]["code"])


async def category(client, who: Actor, name: str, **kw: Any) -> str:
    r = await client.post(
        f"{API}/categories", headers=who.h, json={"name": name, "kind": "expense", **kw}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def budget(client, who: Actor, limits: dict[str, str], **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Monthly",
        "period": "monthly",
        "start_date": date.today().replace(day=1).isoformat(),
        "amount": "1000.00",
        "categories": [{"category_id": c, "amount": a} for c, a in limits.items()],
        **kw,
    }
    r = await client.post(f"{API}/budgets", headers=who.h, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def get_budget(client, who: Actor, budget_id: str) -> dict[str, Any]:
    r = await client.get(f"{API}/budgets/{budget_id}", headers=who.h)
    assert r.status_code == 200, r.text
    return dict(r.json())


def limits_of(b: dict[str, Any]) -> dict[str, str]:
    return {c["category_id"]: c["amount"] for c in b["categories"]}


async def budget_rows(budget_id: str) -> int:
    async with get_database().sessionmaker() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(BudgetCategory)
                .where(BudgetCategory.budget_id == uuid.UUID(budget_id))
            )
            or 0
        )


# --- 1a: GET /categories/{id} children -----------------------------------------------------


async def test_get_category_returns_children_in_every_context(client):
    asha = await user(client, "asha")
    fid = await create_family(client, asha)
    for scope in ({}, {"scope": "family", "family_id": fid}):
        parent = await category(client, asha, "Pets", **scope)
        child = await category(client, asha, "Vet", parent_id=parent, **scope)
        archived = await category(client, asha, "Toys", parent_id=parent, **scope)
        r = await client.post(f"{API}/categories/{archived}/archive", headers=asha.h)
        assert r.status_code == 200
        for method, url in (
            ("GET", f"{API}/categories/{parent}"),
            ("PATCH", f"{API}/categories/{parent}"),
            ("POST", f"{API}/categories/{parent}/unarchive"),
        ):
            kwargs: dict[str, Any] = {"json": {"color": "#22C55E"}} if method == "PATCH" else {}
            r = await client.request(method, url, headers=asha.h, **kwargs)
            assert r.status_code == 200, r.text
            assert {c["id"] for c in r.json()["children"]} == {child, archived}, (scope, method)


async def test_get_builtin_category_children_for_a_family(client):
    fid, p = await household(client)
    outsider = await user(client, "outsider")
    fam_sub = await category(
        client, p["admin"], "Biryani", scope="family", family_id=fid, parent_id=EXPENSE_CAT
    )
    own_sub = await category(client, p["member"], "Chai", parent_id=EXPENSE_CAT)

    r = await client.get(f"{API}/categories/{EXPENSE_CAT}", headers=p["member"].h)
    assert [c["id"] for c in r.json()["children"]] == [own_sub]  # personal context
    r = await client.get(
        f"{API}/categories/{EXPENSE_CAT}", headers=p["viewer"].h, params={"family_id": fid}
    )
    assert [c["id"] for c in r.json()["children"]] == [fam_sub]
    r = await client.get(
        f"{API}/categories/{EXPENSE_CAT}", headers=outsider.h, params={"family_id": fid}
    )
    assert r.status_code == 404


# --- 1b: budgets vs deleted categories ------------------------------------------------------


async def test_category_delete_with_reassign_merges_or_repoints_budget_limits(client):
    asha = await user(client, "asha")
    pets = await category(client, asha, "Pets")
    vet = await category(client, asha, "Vet", parent_id=pets)
    home = await category(client, asha, "Home")
    both = await budget(client, asha, {pets: "100.00", vet: "20.00", home: "50.00"})
    only_pets = await budget(client, asha, {pets: "30.00"}, name="Pets only")
    other = await budget(client, asha, {home: "70.00"}, name="Home only")
    await tx(client, asha, amount="40.00", category_id=vet)

    r = await client.delete(
        f"{API}/categories/{pets}", headers=asha.h, params={"reassign_to": home}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out["deleted_ids"]) == {pets, vet}
    assert out["moved_transactions"] == 1
    assert out["moved_budget_limits"] == 3 and out["removed_budget_limits"] == 0

    merged = await get_budget(client, asha, both["id"])
    assert limits_of(merged) == {home: "170.00"}  # 100 + 20 merged into the existing 50
    assert merged["current"]["spent"] == "40.00"  # the moved expense now counts under Home
    assert [c["spent"] for c in merged["category_breakdown"]] == ["40.00"]
    repointed = await get_budget(client, asha, only_pets["id"])
    assert limits_of(repointed) == {home: "30.00"}
    assert limits_of(await get_budget(client, asha, other["id"])) == {home: "70.00"}
    assert await budget_rows(both["id"]) == 1 and await budget_rows(only_pets["id"]) == 1


async def test_category_delete_without_reassign_removes_unused_budget_limits(client):
    asha = await user(client, "asha")
    pets = await category(client, asha, "Pets")
    home = await category(client, asha, "Home")
    b = await budget(client, asha, {pets: "100.00", home: "50.00"})
    r = await client.delete(f"{API}/categories/{pets}", headers=asha.h)
    assert r.status_code == 200, r.text
    assert r.json()["removed_budget_limits"] == 1 and r.json()["moved_budget_limits"] == 0
    assert limits_of(await get_budget(client, asha, b["id"])) == {home: "50.00"}
    assert await budget_rows(b["id"]) == 1


async def test_budgets_ignore_limits_on_soft_deleted_categories(client):
    """Rows left behind by older data (category soft-deleted outside the service) are stale:
    not listed, not counted, and never break the budget endpoints."""
    asha = await user(client, "asha")
    pets = await category(client, asha, "Pets")
    home = await category(client, asha, "Home")
    b = await budget(client, asha, {pets: "100.00", home: "50.00"})
    await tx(client, asha, amount="25.00", category_id=pets)
    await tx(client, asha, amount="10.00", category_id=home)
    async with get_database().engine.begin() as conn:  # unavoidable: simulate legacy data
        await conn.execute(
            text("UPDATE transaction_categories SET deleted_at = now() WHERE id = :id"),
            {"id": uuid.UUID(pets)},
        )
    detail = await get_budget(client, asha, b["id"])
    assert limits_of(detail) == {home: "50.00"}
    assert [c["category_id"] for c in detail["category_breakdown"]] == [home]
    assert detail["current"]["spent"] == "10.00"
    r = await client.get(f"{API}/budgets", headers=asha.h)
    assert limits_of(r.json()[0]) == {home: "50.00"}
    r = await client.get(f"{API}/budgets/{b['id']}/history", headers=asha.h)
    assert r.status_code == 200 and r.json()["items"][-1]["spent"] == "10.00"
    assert (await client.get(f"{API}/budgets/overview", headers=asha.h)).status_code == 200


# --- 1c: archived categories / inactive accounts ---------------------------------------------


async def test_new_transactions_reject_archived_categories_and_inactive_accounts(client):
    asha = await user(client, "asha")
    pets = await category(client, asha, "Pets")
    vet = await category(client, asha, "Vet", parent_id=pets)
    spare = await category(client, asha, "Spare")
    bank = await account(client, asha)
    old = await tx(client, asha, category_id=vet, account_id=bank["id"])

    assert (
        await client.post(f"{API}/categories/{pets}/archive", headers=asha.h)
    ).status_code == 200
    assert (
        await client.post(f"{API}/categories/{spare}/archive", headers=asha.h)
    ).status_code == 200
    assert (
        await client.post(f"{API}/accounts/{bank['id']}/archive", headers=asha.h)
    ).status_code == 200

    r = await client.post(
        f"{API}/transactions",
        headers=asha.h,
        json={"type": "expense", "amount": "5.00", "category_id": spare},
    )
    assert r.status_code == 422 and err(r) == "category_archived"
    r = await client.post(
        f"{API}/transactions",
        headers=asha.h,  # child of an archived parent
        json={"type": "expense", "amount": "5.00", "category_id": vet},
    )
    assert r.status_code == 422 and err(r) == "category_archived"
    r = await client.post(
        f"{API}/transactions",
        headers=asha.h,
        json={"type": "expense", "amount": "5.00", "account_id": bank["id"]},
    )
    assert r.status_code == 422 and err(r) == "account_inactive"

    # the existing row stays editable without changing its archived references
    r = await client.patch(
        f"{API}/transactions/{old['id']}",
        headers=asha.h,
        json={"description": "Annual checkup", "amount": "120.00"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["category"]["id"] == vet and r.json()["account"]["id"] == bank["id"]
    # ...but can't be moved *to* another archived category
    r = await client.patch(
        f"{API}/transactions/{old['id']}", headers=asha.h, json={"category_id": spare}
    )
    assert r.status_code == 422 and err(r) == "category_archived"
    # unarchiving makes the category usable again
    await client.post(f"{API}/categories/{spare}/unarchive", headers=asha.h)
    await tx(client, asha, category_id=spare)


async def test_recurring_and_upi_reject_archived_references(client):
    asha = await user(client, "asha")
    spare = await category(client, asha, "Spare")
    live = await category(client, asha, "Live")
    bank = await account(client, asha)
    rule_body = {
        "name": "Gym",
        "type": "expense",
        "amount": "999.00",
        "frequency": "monthly",
        "start_date": "2027-01-01",
    }
    r = await client.post(
        f"{API}/recurring",
        headers=asha.h,
        json={**rule_body, "category_id": live, "account_id": bank["id"]},
    )
    assert r.status_code == 201, r.text
    rule = r.json()

    await client.post(f"{API}/categories/{spare}/archive", headers=asha.h)
    await client.post(f"{API}/categories/{live}/archive", headers=asha.h)
    await client.post(f"{API}/accounts/{bank['id']}/archive", headers=asha.h)
    r = await client.post(
        f"{API}/recurring", headers=asha.h, json={**rule_body, "category_id": spare}
    )
    assert r.status_code == 422, r.text
    r = await client.post(
        f"{API}/recurring", headers=asha.h, json={**rule_body, "account_id": bank["id"]}
    )
    assert r.status_code == 422, r.text
    # the existing rule (now pointing at an archived category/account) stays editable
    r = await client.patch(
        f"{API}/recurring/{rule['id']}", headers=asha.h, json={"amount": "1099.00"}
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        f"{API}/upi/payments",
        headers={**asha.h, "Idempotency-Key": "k-archived"},
        json={"payee_vpa": "ravi@okaxis", "amount": "10.00", "category_id": spare},
    )
    assert r.status_code == 422 and err(r) == "category_archived"
    async with get_database().sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(UpiPaymentAttempt)) == 0


# --- 1d: generic PATCH can't touch flow-owned fields -----------------------------------------


async def test_patch_cannot_change_flow_owned_transaction_fields(client):
    asha = await user(client, "asha")
    row = await tx(client, asha, idempotency_key="k-1")
    for field, value in (
        ("recurring_id", str(uuid.uuid4())),
        ("period_key", "2026-11-20"),
        ("upi_attempt_id", str(uuid.uuid4())),
        ("source", "upi_intent"),
        ("verification", "verified"),
        ("external_ref", "UTR1"),
        ("idempotency_key", "k-2"),
        ("owner_user_id", str(uuid.uuid4())),
        ("deleted_at", None),
    ):
        r = await client.patch(
            f"{API}/transactions/{row['id']}", headers=asha.h, json={field: value}
        )
        assert r.status_code == 422, (field, r.text)
    async with get_database().sessionmaker() as s:
        stored = await s.get(Transaction, uuid.UUID(row["id"]))
        assert stored is not None
        assert stored.recurring_id is None and stored.period_key is None
        assert stored.upi_attempt_id is None and stored.source.value == "manual"
        assert stored.verification.value == "unverified" and stored.external_ref is None
        assert stored.idempotency_key == "k-1"


# --- 1e: can_edit / can_delete / user_name ----------------------------------------------------


async def test_recurring_out_can_edit_matches_write_rules(client):
    fid, p = await household(client)
    body = {
        "name": "Netflix",
        "type": "expense",
        "amount": "649.00",
        "frequency": "monthly",
        "start_date": "2027-01-01",
        "scope": "family",
        "family_id": fid,
    }
    r = await client.post(f"{API}/recurring", headers=p["member"].h, json=body)
    assert r.status_code == 201 and r.json()["can_edit"] is True
    by_member = r.json()["id"]
    r = await client.post(f"{API}/recurring", headers=p["admin"].h, json=body)
    by_admin = r.json()["id"]
    personal = await client.post(
        f"{API}/recurring",
        headers=p["viewer"].h,
        json={k: v for k, v in body.items() if k not in ("scope", "family_id")},
    )
    assert personal.json()["can_edit"] is True

    expected = {
        "owner": {by_member: True, by_admin: True},
        "admin": {by_member: True, by_admin: True},
        "member": {by_member: True, by_admin: False},
        "viewer": {by_member: False, by_admin: False},
    }
    for role, flags in expected.items():
        r = await client.get(f"{API}/recurring", headers=p[role].h, params={"family_id": fid})
        assert {i["id"]: i["can_edit"] for i in r.json()["items"]} == flags, role
        for rule_id, flag in flags.items():
            one = await client.get(f"{API}/recurring/{rule_id}", headers=p[role].h)
            assert one.json()["can_edit"] is flag
            # the flag agrees with what the API actually allows
            r = await client.post(f"{API}/recurring/{rule_id}/pause", headers=p[role].h)
            assert r.status_code == (200 if flag else 403), (role, r.text)
            if flag:
                assert r.json()["can_edit"] is True
                await client.post(f"{API}/recurring/{rule_id}/resume", headers=p[role].h)


async def test_contribution_out_has_user_name_and_can_delete(client):
    fid, p = await household(client)
    r = await client.post(
        f"{API}/savings",
        headers=p["owner"].h,
        json={"name": "Trip", "target_amount": "10000.00", "scope": "family", "family_id": fid},
    )
    assert r.status_code == 201, r.text
    goal = r.json()["id"]

    async def contribute(who: Actor, amount: str) -> dict[str, Any]:
        r = await client.post(
            f"{API}/savings/{goal}/contributions",
            headers=who.h,
            json={"kind": "deposit", "amount": amount},
        )
        assert r.status_code == 201, r.text
        return dict(r.json()["contribution"])

    by_member = await contribute(p["member"], "100.00")
    assert by_member["user_name"] == "Member" and by_member["can_delete"] is True
    by_admin = await contribute(p["admin"], "200.00")
    expected = {
        "owner": {by_member["id"]: True, by_admin["id"]: True},
        "admin": {by_member["id"]: True, by_admin["id"]: True},
        "member": {by_member["id"]: True, by_admin["id"]: False},
        "viewer": {by_member["id"]: False, by_admin["id"]: False},
    }
    for role, flags in expected.items():
        r = await client.get(f"{API}/savings/{goal}/contributions", headers=p[role].h)
        items = r.json()["items"]
        assert {i["id"]: i["can_delete"] for i in items} == flags, role
        assert {i["id"]: i["user_name"] for i in items} == {
            by_member["id"]: "Member",
            by_admin["id"]: "Admin",
        }
    r = await client.delete(
        f"{API}/savings/{goal}/contributions/{by_admin['id']}", headers=p["member"].h
    )
    assert r.status_code == 403
    r = await client.delete(
        f"{API}/savings/{goal}/contributions/{by_member['id']}", headers=p["member"].h
    )
    assert r.status_code == 200


# --- 1f: limits come from Settings ------------------------------------------------------------


def _png(size: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (10, 200, 30)).save(buf, format="PNG")
    return buf.getvalue()


async def test_receipt_and_upi_limits_come_from_settings(client, settings):
    asha = await user(client, "asha")
    row = await tx(client, asha)
    data = _png(64)
    settings.max_receipt_bytes = len(data) - 1
    r = await client.post(
        f"{API}/transactions/{row['id']}/attachments",
        headers=asha.h,
        files={"file": ("r.png", data, "image/png")},
    )
    assert r.status_code == 413, r.text
    settings.max_receipt_bytes = 5 * 1024 * 1024
    settings.receipt_upload_limit = 1
    r = await client.post(
        f"{API}/transactions/{row['id']}/attachments",
        headers=asha.h,
        files={"file": ("r.png", data, "image/png")},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/transactions/{row['id']}/attachments",
        headers=asha.h,
        files={"file": ("r.png", data, "image/png")},
    )
    assert r.status_code == 429

    settings.upi_parse_rate_limit = 1
    body = {"payload": "upi://pay?pa=ravi@okaxis&pn=Ravi"}
    assert (await client.post(f"{API}/upi/parse", headers=asha.h, json=body)).status_code == 200
    assert (await client.post(f"{API}/upi/parse", headers=asha.h, json=body)).status_code == 429
