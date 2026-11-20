"""Regression tests for the integration-pass security review (IDOR / scope leaks, log
redaction, CSV injection, rate limits, recurring generation authority, currency mixing)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.middleware import loggable_path
from app.db.session import get_database
from app.models.system import BackgroundJob
from app.repositories.system import JobRepository
from app.services.transactions import csv_safe
from app.utils.csv_safety import neutralize_formula
from app.workers.jobs.recurring import run_recurring
from app.workers.worker import process_due_jobs
from tests.helpers_transactions import (
    API,
    EXPENSE_CAT,
    account,
    household,
    tx,
    user,
)


def err(r: Any) -> str:
    return str(r.json()["error"]["code"])


# --- idempotent replays never leak a row -----------------------------------------------------


async def test_idempotent_replay_checks_scope_and_current_membership(client):
    fid, p = await household(client)
    member = p["member"]
    fam = {"scope": "family", "family_id": fid}
    h = {**member.h, "Idempotency-Key": "fam-key"}
    body = {"type": "expense", "amount": "75.00", "merchant": "FamilySecret", **fam}
    r = await client.post(f"{API}/transactions", headers=h, json=body)
    assert r.status_code == 201
    # same key, personal body → not silently answered with the family row
    personal = {"type": "expense", "amount": "75.00"}
    r = await client.post(f"{API}/transactions", headers=h, json=personal)
    assert r.status_code == 409 and err(r) == "idempotency_key_reused"
    assert (await client.post(f"{API}/families/{fid}/leave", headers=member.h)).status_code == 204
    for payload in (personal, body):
        r = await client.post(f"{API}/transactions", headers=h, json=payload)
        assert r.status_code in (404, 409), r.text
        assert "FamilySecret" not in r.text


# --- savings contributions only link transactions of the goal's scope ------------------------


async def test_contribution_transaction_must_match_goal_scope_and_currency(client):
    fid, p = await household(client)
    member = p["member"]
    r = await client.post(
        f"{API}/savings",
        headers=p["owner"].h,
        json={"name": "Trip", "target_amount": "1000.00", "scope": "family", "family_id": fid},
    )
    goal = r.json()["id"]
    personal = await tx(client, member, amount="10.00")
    family_row = await tx(client, member, amount="10.00", scope="family", family_id=fid)
    usd = await account(
        client, p["admin"], name="USD", currency="USD", scope="family", family_id=fid
    )
    usd_row = await tx(
        client, p["admin"], amount="10.00", account_id=usd["id"], scope="family", family_id=fid
    )

    async def link(who, tx_id: str) -> Any:
        return await client.post(
            f"{API}/savings/{goal}/contributions",
            headers=who.h,
            json={"amount": "10.00", "transaction_id": tx_id},
        )

    r = await link(member, personal["id"])
    assert r.status_code == 422 and err(r) == "transaction_scope_mismatch"
    r = await link(p["admin"], usd_row["id"])
    assert r.status_code == 422 and err(r) == "currency_mismatch"
    r = await link(p["viewer"], family_row["id"])
    assert r.status_code == 403
    r = await link(member, family_row["id"])
    assert r.status_code == 201, r.text
    # a transaction the caller can't see at all stays a 404
    outsider_tx = await tx(client, await user(client, "zed"), amount="5.00")
    assert (await link(member, outsider_tx["id"])).status_code == 404


# --- logs and CSV ---------------------------------------------------------------------------


def test_push_tokens_are_redacted_from_logged_paths():
    token = "ExponentPushToken[abcdefghijklmnop]"
    assert loggable_path(f"/api/v1/notifications/devices/{token}") == (
        "/api/v1/notifications/devices/<redacted>"
    )
    assert loggable_path("/api/v1/transactions/123") == "/api/v1/transactions/123"


async def test_device_delete_request_log_has_no_token(client, caplog):
    asha = await user(client, "asha")
    token = "ExponentPushToken[secretsecretsecret00]"
    r = await client.post(
        f"{API}/notifications/devices", headers=asha.h, json={"token": token, "platform": "android"}
    )
    assert r.status_code == 200
    with caplog.at_level(logging.INFO):
        r = await client.delete(f"{API}/notifications/devices/{token}", headers=asha.h)
    assert r.status_code == 204
    assert "secretsecretsecret00" not in caplog.text
    assert "<redacted>" in caplog.text


def test_csv_formula_neutralisation_covers_hidden_and_full_width_prefixes():
    for risky in (
        "=1+1",
        "+1",
        "-1",
        "@SUM(A1)",
        "\tx",
        "\rx",
        "\nx",
        " =HYPERLINK()",
        "\uff1dcmd",
        "\uff0b1",
        "  \t=x",
    ):
        assert neutralize_formula(risky) == "'" + risky, repr(risky)
        assert csv_safe(risky).startswith("'")
    for fine in ("Dinner", "100.00", "a=b", "", "2026-11-20"):
        assert neutralize_formula(fine) == fine


async def test_transactions_csv_neutralises_whitespace_prefixed_formula(client):
    asha = await user(client, "asha")
    await tx(client, asha, description=' =HYPERLINK("http://evil")')
    r = await client.get(f"{API}/transactions/export.csv", headers=asha.h)
    assert r.status_code == 200
    # (the API strips the leading space; the cell is neutralised either way)
    assert "'=HYPERLINK" in r.text and '",=HYPERLINK' not in r.text


async def test_worker_errors_never_store_email_addresses(settings: Settings):
    async def boom(_p: dict[str, Any], _s: Settings) -> None:
        raise RuntimeError("550 recipient refused: <bob.secret@example.com>")

    async with get_database().sessionmaker() as s:
        await JobRepository(s).enqueue("demo", {}, max_attempts=1)
        await s.commit()
    await process_due_jobs(get_database(), settings, {"demo": boom})
    async with get_database().sessionmaker() as s:
        job = await s.scalar(select(BackgroundJob))
    assert job is not None and job.last_error is not None
    assert "bob.secret@example.com" not in job.last_error and "<email>" in job.last_error


# --- rate limits on expensive endpoints -------------------------------------------------------


async def test_exports_analytics_and_device_registration_are_rate_limited(client, settings):
    asha = await user(client, "asha")
    settings.export_rate_limit = 2
    assert (await client.get(f"{API}/transactions/export.csv", headers=asha.h)).status_code == 200
    r = await client.get(f"{API}/reports/export.csv", headers=asha.h, params={"report": "spending"})
    assert r.status_code == 200
    r = await client.get(f"{API}/transactions/export.csv", headers=asha.h)
    assert r.status_code == 429 and "retry-after" in r.headers

    bob = await user(client, "bob")
    settings.analytics_rate_limit = 3
    for path in ("/dashboard", "/reports/spending", "/reports/categories"):
        assert (await client.get(f"{API}{path}", headers=bob.h)).status_code == 200
    assert (await client.get(f"{API}/dashboard", headers=bob.h)).status_code == 429
    assert (await client.get(f"{API}/transactions", headers=bob.h)).status_code == 200

    settings.device_register_rate_limit = 1
    body = {"token": "ExponentPushToken[aaaaaaaaaaaaaaaaaaaa]", "platform": "android"}
    assert (
        await client.post(f"{API}/notifications/devices", headers=bob.h, json=body)
    ).status_code == 200
    r = await client.post(f"{API}/notifications/devices", headers=bob.h, json=body)
    assert r.status_code == 429


# --- recurring generation authority & currency ------------------------------------------------


async def test_family_rule_stops_generating_when_its_creator_leaves_or_is_demoted(client):
    fid, p = await household(client)
    start = (date.today() - timedelta(days=2)).isoformat()
    body = {
        "name": "Maid",
        "type": "expense",
        "amount": "100.00",
        "frequency": "daily",
        "start_date": start,
        "scope": "family",
        "family_id": fid,
    }
    left = (await client.post(f"{API}/recurring", headers=p["member"].h, json=body)).json()
    demoted = (await client.post(f"{API}/recurring", headers=p["admin"].h, json=body)).json()
    kept = (await client.post(f"{API}/recurring", headers=p["owner"].h, json=body)).json()
    assert (
        await client.post(f"{API}/families/{fid}/leave", headers=p["member"].h)
    ).status_code == 204
    r = await client.patch(
        f"{API}/families/{fid}/members/{p['admin'].id}",
        headers=p["owner"].h,
        json={"role": "viewer"},
    )
    assert r.status_code == 200, r.text
    await run_recurring(get_database(), get_settings())

    async def generated(rule_id: str) -> int:
        r = await client.get(f"{API}/recurring/{rule_id}/occurrences", headers=p["owner"].h)
        return int(r.json()["total"])

    assert await generated(left["id"]) == 0
    assert await generated(demoted["id"]) == 0
    assert await generated(kept["id"]) >= 2


async def test_recurring_rule_currency_must_match_its_accounts(client):
    asha = await user(client, "asha")
    usd = await account(client, asha, name="USD", currency="USD")
    inr = await account(client, asha, name="INR")
    base = {
        "name": "Netflix",
        "type": "expense",
        "amount": "10.00",
        "frequency": "monthly",
        "start_date": "2027-01-01",
    }
    r = await client.post(
        f"{API}/recurring",
        headers=asha.h,
        json={**base, "account_id": usd["id"], "currency": "INR"},
    )
    assert r.status_code == 422, r.text
    r = await client.post(
        f"{API}/recurring", headers=asha.h, json={**base, "account_id": usd["id"]}
    )
    assert r.status_code == 201 and r.json()["currency"] == "USD"  # defaults to the account's
    rule = r.json()
    r = await client.patch(
        f"{API}/recurring/{rule['id']}", headers=asha.h, json={"account_id": inr["id"]}
    )
    assert r.status_code == 422
    r = await client.patch(
        f"{API}/recurring/{rule['id']}", headers=asha.h, json={"currency": "INR"}
    )
    assert r.status_code == 422


async def test_budget_spending_ignores_other_currencies(client):
    asha = await user(client, "asha")
    usd = await account(client, asha, name="USD", currency="USD")
    r = await client.post(
        f"{API}/budgets",
        headers=asha.h,
        json={"name": "All", "period": "monthly", "amount": "1000.00"},
    )
    bid = r.json()["id"]
    await tx(client, asha, amount="100.00", category_id=EXPENSE_CAT)
    await tx(client, asha, amount="900.00", account_id=usd["id"])
    r = await client.get(f"{API}/budgets/{bid}", headers=asha.h)
    assert r.json()["currency"] == "INR" and r.json()["current"]["spent"] == "100.00"
