"""Spec §17 critical scenarios 1-8, proven end-to-end through the HTTP API.

Every setup step and every assertion goes through ``/api/v1``. The only exception is scenario
8, where the recurring generator is a worker job (no HTTP trigger exists): it is invoked exactly
as the worker does (``app.workers.jobs.recurring.run_recurring``), and its results are then
checked through the API.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings
from app.db.session import get_database
from app.workers.jobs.recurring import run_recurring
from tests.conftest import BUILTIN_CATEGORIES
from tests.helpers_transactions import (
    API,
    EXPENSE_CAT,
    INCOME_CAT,
    Actor,
    account,
    create_family,
    household,
    join,
    tx,
    user,
)

GROCERIES = next(c["id"] for c in BUILTIN_CATEGORIES if c["name"] == "Groceries")
FOOD = next(c["id"] for c in BUILTIN_CATEGORIES if c["name"] == "Food & Dining")

# Report endpoints that take the common scope params (family_id / range).
SCOPED_REPORTS = (
    "spending",
    "income-vs-expense",
    "categories",
    "payment-methods",
    "savings-trend",
    "budget-performance",
    "recurring-summary",
    "upi",
)


def ist_today() -> Any:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()  # the default users.timezone


async def get(client: httpx.AsyncClient, who: Actor, path: str, **params: Any) -> httpx.Response:
    return await client.get(f"{API}{path}", headers=who.h, params=params)


async def ok(client: httpx.AsyncClient, who: Actor, path: str, **params: Any) -> Any:
    r = await get(client, who, path, **params)
    assert r.status_code == 200, (path, r.text)
    return r.json()


async def summary(client, who: Actor, **params: Any) -> dict[str, Any]:
    return dict(await ok(client, who, "/transactions/summary", **params))


async def dashboard(client, who: Actor, **params: Any) -> dict[str, Any]:
    return dict(await ok(client, who, "/dashboard", **params))


async def budget_spent(client, who: Actor, budget_id: str) -> tuple[str, list[str]]:
    b = await ok(client, who, f"/budgets/{budget_id}")
    return b["current"]["spent"], [c["spent"] for c in b["category_breakdown"]]


async def initiate_upi(client, who: Actor, key: str, **kw: Any) -> httpx.Response:
    body = {"payee_vpa": "ravi.kirana@okaxis", "payee_name": "Ravi Kirana", "amount": "250.00"}
    return await client.post(
        f"{API}/upi/payments", headers={**who.h, "Idempotency-Key": key}, json={**body, **kw}
    )


# --- 1 -----------------------------------------------------------------------------------


async def test_scenario_1_user_cannot_access_another_users_personal_transactions(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    bob_bank = await account(client, bob, name="Bob Bank", opening_balance="1000.00")
    secret = await tx(
        client,
        bob,
        amount="500.00",
        account_id=bob_bank["id"],
        category_id=FOOD,
        merchant="SecretMerchant",
        tags=["bobtag"],
    )
    bob_income = await tx(client, bob, type="income", amount="900.00", category_id=INCOME_CAT)
    sid = secret["id"]

    # direct access by id: indistinguishable from a missing row (404), never 403
    for method, path, body in (
        ("GET", f"/transactions/{sid}", None),
        ("PATCH", f"/transactions/{sid}", {"amount": "1.00"}),
        ("DELETE", f"/transactions/{sid}", None),
        ("POST", f"/transactions/{sid}/restore", None),
        ("GET", f"/transactions/{sid}/attachments", None),
        ("GET", f"/accounts/{bob_bank['id']}", None),
        ("PATCH", f"/accounts/{bob_bank['id']}", {"name": "Mine"}),
        ("DELETE", f"/accounts/{bob_bank['id']}", None),
    ):
        r = await client.request(method, f"{API}{path}", headers=alice.h, json=body)
        assert r.status_code == 404, (method, path, r.text)
    r = await client.post(
        f"{API}/transactions/{sid}/attachments",
        headers=alice.h,
        files={"file": ("r.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert r.status_code == 404
    # bob's account can't be used by alice either
    r = await client.post(
        f"{API}/transactions",
        headers=alice.h,
        json={"type": "expense", "amount": "1.00", "account_id": bob_bank["id"]},
    )
    assert r.status_code == 404

    # lists, totals, exports, reports and the dashboard never include bob's rows
    listed = await ok(client, alice, "/transactions")
    assert listed["total"] == 0 and listed["items"] == []
    assert (await ok(client, alice, "/transactions", member_user_id=bob.id))["total"] == 0
    assert await summary(client, alice) == {
        "income": "0.00",
        "expense": "0.00",
        "net": "0.00",
        "count": 0,
    }
    for path in ("/transactions/export.csv", "/reports/export.csv"):
        params = {"report": "categories"} if path.startswith("/reports") else {}
        r = await get(client, alice, path, **params)
        assert r.status_code == 200, r.text
        assert sid not in r.text and "SecretMerchant" not in r.text and "500.00" not in r.text
    assert (await ok(client, alice, "/reports/spending"))["total"] == "0.00"
    assert (await ok(client, alice, "/reports/categories"))["total"] == "0.00"
    assert (await ok(client, alice, "/reports/payment-methods"))["total"] == "0.00"
    ive = await ok(client, alice, "/reports/income-vs-expense")
    assert ive["income"] == "0.00" and ive["expense"] == "0.00"
    r = await get(client, alice, "/reports/account-balances", account_id=bob_bank["id"])
    assert r.status_code == 404
    dash = await dashboard(client, alice)
    assert dash["totals"]["income"] == "0.00" and dash["totals"]["expense"] == "0.00"
    assert dash["recent_transactions"] == [] and dash["balance"]["accounts"] == []
    assert await ok(client, alice, "/tags") == []
    assert await ok(client, alice, "/accounts") == []

    # ...and bob's data is untouched
    mine = await ok(client, bob, f"/transactions/{sid}")
    assert mine["amount"] == "500.00" and mine["deleted_at"] is None
    assert (await summary(client, bob))["expense"] == "500.00"
    assert (await ok(client, bob, f"/transactions/{bob_income['id']}"))["amount"] == "900.00"


# --- 2 -----------------------------------------------------------------------------------


async def test_scenario_2_family_cannot_access_another_familys_data(client):
    a_owner, a_member = await user(client, "aowner"), await user(client, "amember")
    b_owner = await user(client, "bowner")
    fam_a = await create_family(client, a_owner, "Family A")
    await join(client, a_owner, fam_a, a_member, "member")
    fam_b = await create_family(client, b_owner, "Family B")
    scope_b = {"scope": "family", "family_id": fam_b}
    b_bank = await account(client, b_owner, name="B Bank", **scope_b)
    b_cat = (
        await client.post(
            f"{API}/categories",
            headers=b_owner.h,
            json={"name": "B Only", "kind": "expense", **scope_b},
        )
    ).json()["id"]
    b_tx = await tx(
        client,
        b_owner,
        amount="777.00",
        account_id=b_bank["id"],
        category_id=b_cat,
        merchant="FamilyBSecret",
        **scope_b,
    )
    r = await client.post(
        f"{API}/budgets",
        headers=b_owner.h,
        json={"name": "B budget", "period": "monthly", "amount": "5000.00", **scope_b},
    )
    assert r.status_code == 201, r.text
    b_budget = r.json()["id"]
    r = await client.post(
        f"{API}/savings",
        headers=b_owner.h,
        json={"name": "B goal", "target_amount": "100.00", **scope_b},
    )
    b_goal = r.json()["id"]

    for outsider in (a_owner, a_member):
        # list/aggregate endpoints scoped to family B → 404 (not a member)
        for path in (
            "/transactions",
            "/transactions/summary",
            "/transactions/export.csv",
            "/accounts",
            "/budgets",
            "/budgets/overview",
            "/savings",
            "/recurring",
            "/recurring/upcoming",
            "/categories",
            "/payees",
            "/tags",
            "/upi/payments",
            "/dashboard",
            "/reports/members",
            "/reports/account-balances",
            *(f"/reports/{name}" for name in SCOPED_REPORTS),
        ):
            params: dict[str, Any] = {"family_id": fam_b}
            if path == "/reports/account-balances":
                params["account_id"] = b_bank["id"]
            r = await get(client, outsider, path, **params)
            assert r.status_code == 404, (path, r.status_code, r.text)
        r = await get(client, outsider, "/reports/export.csv", report="spending", family_id=fam_b)
        assert r.status_code == 404
        # direct access to family B rows by id → 404
        for method, path, body in (
            ("GET", f"/transactions/{b_tx['id']}", None),
            ("PATCH", f"/transactions/{b_tx['id']}", {"amount": "1.00"}),
            ("DELETE", f"/transactions/{b_tx['id']}", None),
            ("GET", f"/accounts/{b_bank['id']}", None),
            ("PATCH", f"/accounts/{b_bank['id']}", {"name": "x"}),
            ("GET", f"/budgets/{b_budget}", None),
            ("PATCH", f"/budgets/{b_budget}", {"amount": "1.00"}),
            ("DELETE", f"/budgets/{b_budget}", None),
            ("GET", f"/savings/{b_goal}", None),
            ("GET", f"/categories/{b_cat}", None),
            ("GET", f"/families/{fam_b}", None),
        ):
            r = await client.request(method, f"{API}{path}", headers=outsider.h, json=body)
            assert r.status_code == 404, (method, path, r.text)
        # family B's account / category can't be used from family A (or personally)
        for scope in ({"scope": "family", "family_id": fam_a}, {}):
            for ref in ({"account_id": b_bank["id"]}, {"category_id": b_cat}):
                r = await client.post(
                    f"{API}/transactions",
                    headers=outsider.h,
                    json={"type": "expense", "amount": "1.00", **scope, **ref},
                )
                assert r.status_code == 404, (scope, ref, r.text)
        # creating a row *in* family B is refused
        r = await client.post(
            f"{API}/transactions",
            headers=outsider.h,
            json={"type": "expense", "amount": "1.00", **scope_b},
        )
        assert r.status_code == 404
        # family A's own views contain nothing of family B
        fam_a_rows = await ok(client, outsider, "/transactions", family_id=fam_a)
        assert fam_a_rows["total"] == 0
        dash = await dashboard(client, outsider, family_id=fam_a)
        assert dash["totals"]["expense"] == "0.00" and dash["recent_transactions"] == []
        csv = await get(client, outsider, "/transactions/export.csv", family_id=fam_a)
        assert "FamilyBSecret" not in csv.text and b_tx["id"] not in csv.text

    assert (await summary(client, b_owner, family_id=fam_b))["expense"] == "777.00"
    assert (await ok(client, b_owner, f"/transactions/{b_tx['id']}"))["amount"] == "777.00"


# --- 3 -----------------------------------------------------------------------------------


async def test_scenario_3_member_and_viewer_cannot_perform_admin_actions(client):
    fid, p = await household(client)
    newcomer = await user(client, "newcomer")
    scope = {"scope": "family", "family_id": fid}
    admin_only: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", f"/families/{fid}/invitations", {"email": newcomer.email, "role": "member"}),
        ("GET", f"/families/{fid}/invitations", None),
        ("DELETE", f"/families/{fid}/members/{p['admin'].id}", None),
        ("PATCH", f"/families/{fid}/members/{p['viewer'].id}", {"role": "admin"}),
        ("PATCH", f"/families/{fid}", {"name": "Taken over"}),
        ("DELETE", f"/families/{fid}", None),
        ("POST", f"/families/{fid}/transfer-ownership", {"user_id": p["member"].id}),
        ("POST", "/budgets", {"name": "B", "period": "monthly", "amount": "10.00", **scope}),
        ("POST", "/accounts", {"name": "Joint", "type": "bank", **scope}),
        ("POST", "/categories", {"name": "Shared", "kind": "expense", **scope}),
    ]
    for role in ("member", "viewer"):
        for method, path, body in admin_only:
            r = await client.request(method, f"{API}{path}", headers=p[role].h, json=body)
            assert r.status_code == 403, (role, method, path, r.text)
            assert r.json()["error"]["code"] == "insufficient_family_role"
    # owner-only actions are refused to admins too
    for method, path, body in admin_only[5:7]:
        r = await client.request(method, f"{API}{path}", headers=p["admin"].h, json=body)
        assert r.status_code == 403, (method, path, r.text)

    # nothing changed
    fam = await ok(client, p["owner"], f"/families/{fid}")
    assert fam["name"] == "Rao Family"
    assert {m["user_id"]: m["role"] for m in fam["members"]} == {
        p["owner"].id: "owner",
        p["admin"].id: "admin",
        p["member"].id: "member",
        p["viewer"].id: "viewer",
    }
    assert await ok(client, p["owner"], f"/families/{fid}/invitations") == []
    assert await ok(client, p["owner"], "/budgets", family_id=fid) == []
    assert await ok(client, p["owner"], "/accounts", family_id=fid) == []
    tree = await ok(client, p["owner"], "/categories", family_id=fid)
    assert all(c["ownership"] == "builtin" for c in tree)
    # the same admin actions succeed for the admin (the 403s were about the role)
    r = await client.post(
        f"{API}/accounts", headers=p["admin"].h, json={"name": "Joint", "type": "bank", **scope}
    )
    assert r.status_code == 201
    # a viewer can't even add a family transaction; a member can
    body = {"type": "expense", "amount": "5.00", **scope}
    assert (
        await client.post(f"{API}/transactions", headers=p["viewer"].h, json=body)
    ).status_code == 403
    assert (
        await client.post(f"{API}/transactions", headers=p["member"].h, json=body)
    ).status_code == 201


# --- 4 -----------------------------------------------------------------------------------


async def test_scenario_4_upi_launch_and_return_never_complete_a_transaction(client):
    asha = await user(client, "asha")
    r = await client.post(
        f"{API}/budgets",
        headers=asha.h,
        json={"name": "All", "period": "monthly", "amount": "1000.00"},
    )
    budget_id = r.json()["id"]
    r = await initiate_upi(client, asha, "pay-1", category_id=FOOD)
    assert r.status_code == 201, r.text
    pay = r.json()
    assert pay["status"] == "initiated" and pay["upi_uri"].startswith("upi://pay?")

    async def assert_not_counted() -> None:
        t = await ok(client, asha, f"/transactions/{pay['transaction_id']}")
        assert t["status"] == "pending" and t["verification"] == "unverified"
        assert (await summary(client, asha))["expense"] == "0.00"
        assert (await dashboard(client, asha))["totals"]["expense"] == "0.00"
        assert (await ok(client, asha, "/reports/spending"))["total"] == "0.00"
        assert (await budget_spent(client, asha, budget_id))[0] == "0.00"

    await assert_not_counted()
    r = await client.post(f"{API}/upi/payments/{pay['id']}/launched", headers=asha.h, json={})
    assert r.status_code == 200 and r.json()["status"] == "launched"
    await assert_not_counted()
    # the UPI app claims success — untrusted, stored as a hint only
    raw = f"txnId=AXI123&responseCode=00&Status=SUCCESS&txnRef={pay['tr_ref']}&ApprovalRefNo=99"
    r = await client.post(
        f"{API}/upi/payments/{pay['id']}/return", headers=asha.h, json={"raw": raw}
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["status"] == "launched" and detail["needs_confirmation"] is True
    assert detail["untrusted_hint"]["status"] == "success"
    assert detail["transaction"]["status"] == "pending"
    await assert_not_counted()
    dash = await dashboard(client, asha)
    assert dash["pending_upi_count"] == 1
    # Home can open the pending payment's confirmation screen straight from the recent row
    (recent,) = dash["recent_transactions"]
    assert recent["id"] == pay["transaction_id"] and recent["upi_attempt_id"] == pay["id"]
    manual = await tx(client, asha, amount="5.00")
    recent = (await dashboard(client, asha))["recent_transactions"]
    assert {r["id"]: r["upi_attempt_id"] for r in recent} == {
        pay["transaction_id"]: pay["id"],
        manual["id"]: None,
    }
    assert (
        await client.delete(f"{API}/transactions/{manual['id']}", headers=asha.h)
    ).status_code == 204

    # only the user's confirmation completes it — and only as "user_confirmed"
    r = await client.post(
        f"{API}/upi/payments/{pay['id']}/confirm",
        headers=asha.h,
        json={"outcome": "success", "utr": "123456789012"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verification"] == "user_confirmed"
    t = await ok(client, asha, f"/transactions/{pay['transaction_id']}")
    assert t["status"] == "completed" and t["verification"] == "user_confirmed"
    assert (await summary(client, asha))["expense"] == "250.00"
    assert (await budget_spent(client, asha, budget_id))[0] == "250.00"


# --- 5 -----------------------------------------------------------------------------------


async def test_scenario_5_duplicate_initiate_and_confirm_never_duplicate(client):
    asha = await user(client, "asha")
    first = await initiate_upi(client, asha, "same-key")
    assert first.status_code == 201
    replays = [await initiate_upi(client, asha, "same-key") for _ in range(2)]
    replays += await asyncio.gather(*(initiate_upi(client, asha, "same-key") for _ in range(4)))
    assert {r.status_code for r in replays} == {200}
    assert {r.json()["id"] for r in replays} == {first.json()["id"]}
    # a brand-new key raced concurrently still yields exactly one payment
    raced = await asyncio.gather(*(initiate_upi(client, asha, "race-key") for _ in range(5)))
    assert sorted(r.status_code for r in raced) == [200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in raced}) == 1

    payments = await ok(client, asha, "/upi/payments")
    assert payments["total"] == 2
    listed = await ok(client, asha, "/transactions")
    assert listed["total"] == 2 and {t["source"] for t in listed["items"]} == {"upi_intent"}

    pid = first.json()["id"]
    raw = {"raw": "Status=SUCCESS&txnRef=x"}
    for _ in range(3):  # repeated return callbacks
        assert (
            await client.post(f"{API}/upi/payments/{pid}/return", headers=asha.h, json=raw)
        ).status_code == 200
    confirm = {"outcome": "success", "utr": "123456789012"}
    results = [await client.post(f"{API}/upi/payments/{pid}/confirm", headers=asha.h, json=confirm)]
    results += await asyncio.gather(
        *(
            client.post(f"{API}/upi/payments/{pid}/confirm", headers=asha.h, json=confirm)
            for _ in range(4)
        )
    )
    assert {r.status_code for r in results} == {200}
    assert (await summary(client, asha))["expense"] == "250.00"  # counted once
    assert (await ok(client, asha, "/transactions"))["total"] == 2
    assert (await ok(client, asha, "/reports/upi"))["confirmed_spend"] == "250.00"

    # the generic transactions API is idempotent too (header key, sequential + concurrent)
    body = {"type": "expense", "amount": "40.00", "category_id": EXPENSE_CAT}
    h = {**asha.h, "Idempotency-Key": "manual-1"}
    created = await client.post(f"{API}/transactions", headers=h, json=body)
    again = await asyncio.gather(
        *(client.post(f"{API}/transactions", headers=h, json=body) for _ in range(4))
    )
    assert created.status_code == 201 and {r.status_code for r in again} == {200}
    assert {r.json()["id"] for r in again} == {created.json()["id"]}
    assert (await summary(client, asha))["expense"] == "290.00"


# --- 6 -----------------------------------------------------------------------------------


async def test_scenario_6_transfers_never_inflate_income_or_expense(client):
    fid, p = await household(client)
    asha = p["owner"]
    for scope in ({}, {"scope": "family", "family_id": fid}):
        params = {"family_id": fid} if scope else {}
        bank = await account(client, asha, name="Bank", opening_balance="0.00", **scope)
        cash = await account(client, asha, name="Cash", opening_balance="0.00", **scope)
        r = await client.post(
            f"{API}/budgets",
            headers=asha.h,
            json={"name": "All", "period": "monthly", "amount": "1000.00", **scope},
        )
        budget_id = r.json()["id"]
        await tx(
            client,
            asha,
            type="income",
            amount="1000.00",
            category_id=INCOME_CAT,
            account_id=bank["id"],
            **scope,
        )
        await tx(
            client,
            asha,
            amount="200.00",
            category_id=FOOD,
            account_id=bank["id"],
            payment_method="card",
            **scope,
        )
        for _ in range(2):
            await tx(
                client,
                asha,
                type="transfer",
                amount="500.00",
                account_id=bank["id"],
                to_account_id=cash["id"],
                payment_method="bank_transfer",
                **scope,
            )

        s = await summary(client, asha, **params)
        assert (s["income"], s["expense"], s["net"]) == ("1000.00", "200.00", "800.00")
        dash = await dashboard(client, asha, **params)
        assert dash["totals"]["income"] == "1000.00" and dash["totals"]["expense"] == "200.00"
        assert dash["balance"]["available"] == "800.00"  # money moved, not created
        balances = {a["name"]: a["balance"] for a in dash["balance"]["accounts"]}
        assert balances == {"Bank": "-200.00", "Cash": "1000.00"}
        assert (await ok(client, asha, "/reports/spending", **params))["total"] == "200.00"
        assert (await ok(client, asha, "/reports/categories", **params))["total"] == "200.00"
        pm = await ok(client, asha, "/reports/payment-methods", **params)
        assert pm["total"] == "200.00"
        assert {i["payment_method"] for i in pm["items"]} == {"card"}
        ive = await ok(client, asha, "/reports/income-vs-expense", **params)
        assert (ive["income"], ive["expense"]) == ("1000.00", "200.00")
        assert (await budget_spent(client, asha, budget_id))[0] == "200.00"
        overview = await ok(client, asha, "/budgets/overview", **params)
        assert overview["month"]["spent"] == "200.00"
        if scope:
            members = await ok(client, asha, "/reports/members", **params)
            assert members["total"] == "200.00"


# --- 7 -----------------------------------------------------------------------------------


async def test_scenario_7_budget_spent_stays_correct_through_edits(client):
    asha = await user(client, "asha")
    r = await client.post(
        f"{API}/budgets",
        headers=asha.h,
        json={
            "name": "Food",
            "period": "monthly",
            "amount": "2000.00",
            "categories": [{"category_id": FOOD, "amount": "1000.00"}],
        },
    )
    assert r.status_code == 201, r.text
    bid = r.json()["id"]

    async def spent() -> tuple[str, list[str]]:
        return await budget_spent(client, asha, bid)

    row = await tx(client, asha, amount="100.00", category_id=FOOD)
    tid = row["id"]
    assert await spent() == ("100.00", ["100.00"])

    async def patch(**body: Any) -> None:
        r = await client.patch(f"{API}/transactions/{tid}", headers=asha.h, json=body)
        assert r.status_code == 200, r.text

    await patch(amount="250.00")
    assert await spent() == ("250.00", ["250.00"])
    await patch(category_id=GROCERIES)  # moved out of the budget's category
    assert await spent() == ("0.00", ["0.00"])
    await patch(category_id=FOOD)
    assert await spent() == ("250.00", ["250.00"])
    await patch(status="pending")  # not counted until completed
    assert await spent() == ("0.00", ["0.00"])
    await patch(status="completed")
    refund = await tx(client, asha, type="refund", amount="50.00", category_id=FOOD)
    assert await spent() == ("200.00", ["200.00"])
    await tx(
        client,
        asha,
        type="transfer",
        amount="999.00",
        account_id=(await account(client, asha, name="A"))["id"],
        to_account_id=(await account(client, asha, name="B"))["id"],
    )
    assert await spent() == ("200.00", ["200.00"])

    assert (await client.delete(f"{API}/transactions/{tid}", headers=asha.h)).status_code == 204
    assert await spent() == ("-50.00", ["-50.00"])  # only the refund is left
    r = await client.post(f"{API}/transactions/{tid}/restore", headers=asha.h)
    assert r.status_code == 200, r.text
    assert await spent() == ("200.00", ["200.00"])
    assert (
        await client.delete(f"{API}/transactions/{refund['id']}", headers=asha.h)
    ).status_code == 204
    assert await spent() == ("250.00", ["250.00"])
    # history, list, overview and the budget-performance report agree
    history = await ok(client, asha, f"/budgets/{bid}/history")
    assert history["items"][-1]["spent"] == "250.00"
    listed = await ok(client, asha, "/budgets")
    assert listed[0]["current"]["spent"] == "250.00"
    perf = await ok(client, asha, "/reports/budget-performance")
    assert perf["budgets"][0]["items"][-1]["spent"] == "250.00"


# --- 8 -----------------------------------------------------------------------------------


async def test_scenario_8_recurring_generation_never_duplicates(client):
    asha = await user(client, "asha")
    start = ist_today() - timedelta(days=5)
    r = await client.post(
        f"{API}/recurring",
        headers=asha.h,
        json={
            "name": "Milk",
            "type": "expense",
            "amount": "60.00",
            "category_id": GROCERIES,
            "frequency": "daily",
            "start_date": start.isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()

    db, settings = get_database(), get_settings()
    await run_recurring(db, settings)
    await run_recurring(db, settings)  # re-run
    await asyncio.gather(*(run_recurring(db, settings) for _ in range(4)))  # concurrent workers

    occ = await ok(client, asha, f"/recurring/{rule['id']}/occurrences", limit=50)
    assert occ["total"] == 6  # start .. today, once each
    keys = [o["period_key"] for o in occ["items"]]
    assert len(set(keys)) == 6
    assert sorted(keys) == [(start + timedelta(days=i)).isoformat() for i in range(6)]
    listed = await ok(client, asha, "/transactions", limit=50)
    assert listed["total"] == 6
    assert {t["status"] for t in listed["items"]} == {"pending"}  # never auto-paid
    assert (await summary(client, asha))["expense"] == "0.00"

    # paying an occurrence twice still counts it once; a schedule edit doesn't re-generate
    first = occ["items"][-1]["transaction_id"]
    for _ in range(2):
        r = await client.post(
            f"{API}/recurring/occurrences/{first}/mark-paid", headers=asha.h, json={}
        )
        assert r.status_code == 200, r.text
    r = await client.patch(
        f"{API}/recurring/{rule['id']}",
        headers=asha.h,
        json={"start_date": (start - timedelta(days=2)).isoformat()},
    )
    assert r.status_code == 200, r.text
    await asyncio.gather(*(run_recurring(db, settings) for _ in range(3)))
    occ = await ok(client, asha, f"/recurring/{rule['id']}/occurrences", limit=50)
    assert occ["total"] == 6 and len({o["period_key"] for o in occ["items"]}) == 6
    assert (await summary(client, asha))["expense"] == "60.00"
    # deleting a generated occurrence never lets the generator recreate that period
    r = await client.delete(f"{API}/transactions/{first}", headers=asha.h)
    assert r.status_code == 204
    await run_recurring(db, settings)
    occ = await ok(client, asha, f"/recurring/{rule['id']}/occurrences", limit=50)
    assert occ["total"] == 5 and first not in {o["transaction_id"] for o in occ["items"]}
    assert (await ok(client, asha, "/transactions", limit=50))["total"] == 5
