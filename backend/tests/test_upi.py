"""UPI intent payments API (/api/v1/upi) — critical scenarios 4 & 5 and the payment
integrity rules of spec §6."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.security import utcnow
from app.db.session import get_database
from app.models import (
    AuditLog,
    Notification,
    NotificationCategory,
    Payee,
    Transaction,
    UpiPaymentAttempt,
)
from app.services.upi import UpiService
from tests.helpers_transactions import (
    API,
    EXPENSE_CAT,
    Actor,
    account,
    household,
    user,
)

VPA = "ravi.kirana@okaxis"


# --- helpers ---------------------------------------------------------------------------


async def initiate(
    client, who: Actor, key: str | None = None, expect: int = 201, **kw: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "payee_vpa": VPA,
        "payee_name": "Ravi Kirana",
        "amount": "250.00",
        **kw,
    }
    h = {**who.h, "Idempotency-Key": key or uuid.uuid4().hex}
    r = await client.post(f"{API}/upi/payments", headers=h, json=body)
    assert r.status_code == expect, r.text
    return dict(r.json())


async def act(
    client, who: Actor, pid: str, action: str, expect: int = 200, **body: Any
) -> dict[str, Any]:
    r = await client.post(f"{API}/upi/payments/{pid}/{action}", headers=who.h, json=body)
    assert r.status_code == expect, r.text
    return dict(r.json())


async def summary(client, who: Actor, **params: Any) -> dict[str, Any]:
    r = await client.get(f"{API}/transactions/summary", headers=who.h, params=params)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def count(model: Any, *where: Any) -> int:
    async with get_database().sessionmaker() as s:
        return int(
            (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )


async def audit_actions(entity_id: str) -> list[str]:
    async with get_database().sessionmaker() as s:
        rows = await s.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_id == uuid.UUID(entity_id))
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        return list(rows)


async def audit_rows(action: str) -> list[AuditLog]:
    async with get_database().sessionmaker() as s:
        rows = await s.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
        )
        return list(rows)


async def tx_row(tx_id: str) -> Transaction:
    async with get_database().sessionmaker() as s:
        row = await s.get(Transaction, uuid.UUID(tx_id))
        assert row is not None
        return row


async def payee_row(vpa: str = VPA) -> Payee:
    async with get_database().sessionmaker() as s:
        row = await s.scalar(select(Payee).where(Payee.upi_id == vpa))
        assert row is not None
        return row


# --- parse -----------------------------------------------------------------------------


async def test_parse_qr_enriches_with_known_payee_and_category(client):
    asha = await user(client, "asha")
    payload = "upi://pay?pa=Swiggy.Stores@AXB&pn=Swiggy&am=349&cu=INR&tn=Dinner&mc=5812"
    r = await client.post(f"{API}/upi/parse", headers=asha.h, json={"payload": payload})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["payee_vpa"] == "swiggy.stores@axb" and p["payee_name"] == "Swiggy"
    assert p["amount"] == "349.00" and p["amount_locked"] is True and p["currency"] == "INR"
    assert p["note"] == "Dinner" and p["merchant_code"] == "5812"
    assert p["known_payee"] is None
    assert p["suggested_category"]["source"] == "keyword"
    assert p["suggested_category"]["category"]["name"] == "Food & Dining"

    # after one confirmed payment the payee is known and history drives the suggestion
    cat = p["suggested_category"]["category_id"]
    pay = await initiate(
        client, asha, payee_vpa="swiggy.stores@axb", payee_name="Swiggy", category_id=cat
    )
    await act(client, asha, pay["id"], "confirm", outcome="success")
    p2 = (await client.post(f"{API}/upi/parse", headers=asha.h, json={"payload": payload})).json()
    assert p2["known_payee"]["id"] == pay["payee_id"] and p2["known_payee"]["use_count"] == 1
    assert p2["suggested_category"]["source"] == "history"

    open_amount = await client.post(
        f"{API}/upi/parse", headers=asha.h, json={"payload": "upi://pay?pa=amma@ybl"}
    )
    assert open_amount.json()["amount"] is None and open_amount.json()["amount_locked"] is False


async def test_parse_rejects_bad_payloads(client):
    asha = await user(client, "asha")
    cases = {
        "javascript:alert(1)": "not_upi_uri",
        "https://evil.example/?pa=a@ybl": "not_upi_uri",
        "upi://pay?pn=NoVpa": "missing_vpa",
        "upi://pay?pa=bad": "invalid_vpa",
        "upi://pay?pa=ab@ybl&cu=USD": "unsupported_currency",
        "upi://pay?pa=ab@ybl&am=-1": "invalid_amount",
        "upi://pay?pa=ab@ybl&pa=cd@ybl": "duplicate_param",
        "upi://pay?pa=ab@ybl&tn=" + "x" * 3000: "payload_too_long",
    }
    for payload, code in cases.items():
        r = await client.post(f"{API}/upi/parse", headers=asha.h, json={"payload": payload})
        assert r.status_code == 422, payload
        assert r.json()["error"]["code"] == code, payload
    r = await client.post(
        f"{API}/upi/parse", headers=asha.h, json={"payload": "upi://pay?pa=ab@ybl", "pin": "1"}
    )
    assert r.status_code == 422
    r = await client.post(
        f"{API}/upi/parse",
        headers=asha.h,
        json={"payload": "upi://pay?pa=ab@ybl", "family_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404
    assert (await client.post(f"{API}/upi/parse", json={"payload": "x"})).status_code == 401


# --- initiate --------------------------------------------------------------------------


async def test_initiate_creates_pending_transaction_attempt_and_touches_payee(client):
    asha = await user(client, "asha")
    bank = await account(client, asha, "HDFC", type="upi")
    pay = await initiate(
        client,
        asha,
        key="pay-1",
        note="Groceries & milk",
        category_id=EXPENSE_CAT,
        account_id=bank["id"],
        merchant_code="5411",
        from_qr=True,
        qr_amount_locked=True,
    )
    assert pay["status"] == "initiated" and pay["needs_confirmation"] is True
    assert pay["verification"] == "unverified"
    assert pay["payee_vpa"] == VPA and pay["payee_name"] == "Ravi Kirana"
    assert pay["amount"] == "250.00" and pay["currency"] == "INR"
    assert pay["note"] == "Groceries & milk" and pay["utr"] is None
    assert pay["tr_ref"].startswith("FB") and len(pay["tr_ref"]) == 20
    assert pay["initiated_by"] == {"id": asha.id, "full_name": "Asha"}
    assert pay["can_edit"] is True and pay["untrusted_hint"] is None
    assert pay["upi_uri"] == (
        f"upi://pay?pa={VPA}&pn=Ravi%20Kirana&am=250.00&cu=INR&tn=Groceries%20%26%20milk"
        f"&tr={pay['tr_ref']}&mc=5411"
    )
    t = pay["transaction"]
    assert t["id"] == pay["transaction_id"] and pay["transaction_status"] == "pending"
    assert t["status"] == "pending" and t["verification"] == "unverified"
    assert t["source"] == "upi_intent" and t["payment_method"] == "upi"
    assert t["type"] == "expense" and t["amount"] == "250.00"
    assert t["upi_attempt_id"] == pay["id"] and t["idempotency_key"] == "upi:pay-1"
    assert t["category"]["id"] == EXPENSE_CAT and t["account"]["id"] == bank["id"]
    assert t["payee"]["id"] == pay["payee_id"] and t["merchant"] == "Ravi Kirana"
    assert t["description"] == "Groceries & milk"

    payee = await payee_row()
    assert payee.use_count == 1 and payee.last_used_at is not None
    assert payee.merchant_code == "5411" and str(payee.default_category_id) == EXPENSE_CAT
    assert await audit_actions(pay["id"]) == ["upi.initiated"]

    again = await initiate(client, asha, amount="10.00", payee_name=None)
    assert again["payee_id"] == pay["payee_id"] and again["payee_name"] == "Ravi Kirana"
    assert (await payee_row()).use_count == 2
    assert again["transaction"]["description"] == "UPI payment to Ravi Kirana"

    got = await client.get(f"{API}/upi/payments/{pay['id']}", headers=asha.h)
    assert got.status_code == 200 and got.json()["upi_uri"] == pay["upi_uri"]
    # pending UPI expenses never count
    assert (await summary(client, asha))["expense"] == "0.00"


async def test_initiate_validation(client):
    asha = await user(client, "asha")
    body = {"payee_vpa": VPA, "amount": "10"}
    r = await client.post(f"{API}/upi/payments", headers=asha.h, json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_required"
    r = await client.post(
        f"{API}/upi/payments",
        headers={**asha.h, "Idempotency-Key": "a"},
        json={**body, "idempotency_key": "b"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_mismatch"
    ok = await client.post(
        f"{API}/upi/payments", headers=asha.h, json={**body, "idempotency_key": "body-key"}
    )
    assert ok.status_code == 201

    bad = [
        {"payee_vpa": "not-a-vpa", "amount": "10"},
        {"payee_vpa": VPA, "amount": "0"},
        {"payee_vpa": VPA, "amount": "-5"},
        {"payee_vpa": VPA, "amount": "1.005"},
        {"payee_vpa": VPA, "amount": "10", "merchant_code": "12"},
        {"payee_vpa": VPA, "amount": "10", "scope": "family"},
        {"payee_vpa": VPA, "amount": "10", "unknown": 1},
        {"payee_vpa": VPA, "amount": "10", "verification": "verified"},
        {"payee_vpa": VPA, "amount": "10", "status": "completed"},
    ]
    for b in bad:
        r = await client.post(
            f"{API}/upi/payments", headers={**asha.h, "Idempotency-Key": uuid.uuid4().hex}, json=b
        )
        assert r.status_code == 422, b
    r = await client.post(
        f"{API}/upi/payments", headers={**asha.h, "Idempotency-Key": "x" * 97}, json=body
    )
    assert r.status_code == 422

    # references are validated like any transaction
    await initiate(client, asha, expect=404, category_id=str(uuid.uuid4()))
    await initiate(client, asha, expect=404, account_id=str(uuid.uuid4()))
    usd = await account(client, asha, "Wise", currency="USD")
    r = await client.post(
        f"{API}/upi/payments",
        headers={**asha.h, "Idempotency-Key": uuid.uuid4().hex},
        json={**body, "account_id": usd["id"]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "currency_mismatch"
    # failed initiations leave nothing behind (one transaction from "body-key" only)
    assert await count(UpiPaymentAttempt) == 1 and await count(Transaction) == 1
    assert (await payee_row()).use_count == 1


async def test_initiate_with_saved_payee(client):
    asha = await user(client, "asha")
    r = await client.post(
        f"{API}/payees", headers=asha.h, json={"name": "Amma", "upi_id": "amma@ybl"}
    )
    amma = r.json()
    pay = await initiate(client, asha, payee_vpa="AMMA@ybl", payee_id=amma["id"], payee_name=None)
    assert pay["payee_id"] == amma["id"] and pay["payee_name"] == "Amma"
    r = await client.post(
        f"{API}/upi/payments",
        headers={**asha.h, "Idempotency-Key": "mismatch"},
        json={"payee_vpa": VPA, "amount": "5", "payee_id": amma["id"]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "payee_vpa_mismatch"
    bob = await user(client, "bob")
    r = await client.post(
        f"{API}/upi/payments",
        headers={**bob.h, "Idempotency-Key": "steal"},
        json={"payee_vpa": "amma@ybl", "amount": "5", "payee_id": amma["id"]},
    )
    assert r.status_code == 404


async def test_payment_credentials_are_rejected(client):
    asha = await user(client, "asha")
    for field in ("pin", "upi_pin", "mpin", "mPin", "password", "cvv", "otp", "UPI-PIN"):
        r = await client.post(
            f"{API}/upi/payments",
            headers={**asha.h, "Idempotency-Key": uuid.uuid4().hex},
            json={"payee_vpa": VPA, "amount": "10", field: "1234"},
        )
        assert r.status_code == 422, field
        assert "1234" not in r.text
    assert await count(UpiPaymentAttempt) == 0
    pay = await initiate(client, asha)
    await act(client, asha, pay["id"], "confirm", expect=422, outcome="success", upi_pin="1234")
    await act(client, asha, pay["id"], "return", expect=422, raw="Status=SUCCESS", mpin="1")
    # credential-like keys inside untrusted return data are stripped, never stored
    out = await act(
        client,
        asha,
        pay["id"],
        "return",
        raw={"Status": "SUCCESS", "upiPin": "4321", "MPIN": "8765", "txnId": "T1"},
    )
    assert out["untrusted_hint"]["status"] == "success"
    async with get_database().sessionmaker() as s:
        row = await s.get(UpiPaymentAttempt, uuid.UUID(pay["id"]))
        assert row is not None and row.raw_return is not None
        assert "4321" not in str(row.raw_return) and "8765" not in str(row.raw_return)
        assert row.raw_return["fields"] == {"Status": "SUCCESS", "txnId": "T1"}


# --- critical scenario 4 -----------------------------------------------------------------


async def test_launch_and_success_return_never_mark_transaction_successful(client):
    """Critical scenario 4: launching a UPI app (and even a SUCCESS return intent) never
    completes the transaction; only the user's confirmation does."""
    asha = await user(client, "asha")
    before = await summary(client, asha)
    pay = await initiate(client, asha, amount="499.00")
    tx_id = pay["transaction_id"]

    launched = await act(client, asha, pay["id"], "launched")
    assert launched["status"] == "launched" and launched["launched_at"] is not None
    assert launched["upi_uri"] == pay["upi_uri"]  # can be relaunched
    assert launched["transaction"]["status"] == "pending"
    assert launched["transaction"]["verification"] == "unverified"

    raw = (
        f"txnId=YBL0123&responseCode=00&Status=SUCCESS&txnRef={pay['tr_ref']}"
        "&ApprovalRefNo=432112345678"
    )
    returned = await act(client, asha, pay["id"], "return", raw=raw)
    hint = returned["untrusted_hint"]
    assert hint["trusted"] is False and hint["status"] == "success"
    assert hint["txn_id"] == "YBL0123" and hint["response_code"] == "00"
    assert hint["reference_matches"] is True and hint["suggested_utr"] == "432112345678"
    assert hint["received_at"] is not None
    assert returned["status"] == "launched"  # the attempt status is not moved by the app
    assert returned["verification"] == "unverified"

    row = await tx_row(tx_id)
    assert row.status.value == "pending" and row.verification.value == "unverified"
    assert row.external_ref is None
    assert await summary(client, asha) == {**before, "count": before["count"] + 1}
    assert (await summary(client, asha))["expense"] == "0.00"
    # a pending UPI transaction can't be completed through the generic transactions API
    r = await client.patch(
        f"{API}/transactions/{tx_id}", headers=asha.h, json={"status": "completed"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "status_change_not_allowed"

    confirmed = await act(client, asha, pay["id"], "confirm", outcome="success", utr="432112345678")
    assert confirmed["status"] == "success_reported" and confirmed["needs_confirmation"] is False
    assert confirmed["verification"] == "user_confirmed"
    assert confirmed["confirmed_at"] is not None and confirmed["utr"] == "432112345678"
    assert confirmed["upi_uri"] is None  # never relaunch a paid payment
    t = confirmed["transaction"]
    assert t["status"] == "completed" and t["verification"] == "user_confirmed"
    assert t["external_ref"] == "432112345678"
    assert (await summary(client, asha))["expense"] == "499.00"
    assert await audit_actions(pay["id"]) == [
        "upi.initiated",
        "upi.launched",
        "upi.return_received",
        "upi.confirmed",
    ]
    # verified is never set by this API
    assert await count(Transaction, Transaction.verification == "verified") == 0
    assert await count(UpiPaymentAttempt, UpiPaymentAttempt.verification == "verified") == 0


async def test_launched_is_idempotent_and_only_from_initiated(client):
    asha = await user(client, "asha")
    pay = await initiate(client, asha)
    first = await act(client, asha, pay["id"], "launched")
    second = await act(client, asha, pay["id"], "launched")
    assert first["launched_at"] == second["launched_at"]
    assert await audit_actions(pay["id"]) == ["upi.initiated", "upi.launched"]
    await act(client, asha, pay["id"], "confirm", outcome="pending")
    r = await act(client, asha, pay["id"], "launched", expect=409)
    assert r["error"]["code"] == "upi_not_launchable"


async def test_failure_return_hint_also_changes_nothing(client):
    asha = await user(client, "asha")
    pay = await initiate(client, asha)
    out = await act(client, asha, pay["id"], "return", raw="Status=FAILURE&responseCode=ZM")
    assert out["untrusted_hint"]["status"] == "failure"
    assert out["status"] == "initiated" and out["transaction"]["status"] == "pending"
    # same data again is a no-op (no second audit row)
    await act(client, asha, pay["id"], "return", raw="Status=FAILURE&responseCode=ZM")
    assert (await audit_actions(pay["id"])).count("upi.return_received") == 1
    # no data at all is fine
    out = await act(client, asha, pay["id"], "return")
    assert out["untrusted_hint"]["status"] == "failure"


# --- critical scenario 5 -----------------------------------------------------------------


async def test_same_idempotency_key_returns_same_attempt(client):
    """Critical scenario 5: retries never create a second payment / transaction."""
    asha = await user(client, "asha")
    first = await initiate(client, asha, key="retry-1")
    second = await initiate(client, asha, key="retry-1", expect=200)
    assert second["id"] == first["id"] and second["transaction_id"] == first["transaction_id"]
    assert second["tr_ref"] == first["tr_ref"]
    assert await count(UpiPaymentAttempt) == 1 and await count(Transaction) == 1
    assert (await payee_row()).use_count == 1
    # same key, different payment → refused, not silently merged
    r = await initiate(client, asha, key="retry-1", expect=409, amount="999.00")
    assert r["error"]["code"] == "idempotency_key_reused"
    # keys are per user
    bob = await user(client, "bob")
    other = await initiate(client, bob, key="retry-1")
    assert other["id"] != first["id"]
    # the transaction key namespace is separate from manual transactions
    r = await client.post(
        f"{API}/transactions",
        headers={**asha.h, "Idempotency-Key": "upi:clash"},
        json={"type": "expense", "amount": "1"},
    )
    assert r.status_code == 201
    r = await initiate(client, asha, key="clash", expect=409)
    assert r["error"]["code"] == "idempotency_key_reused"
    assert (
        await count(UpiPaymentAttempt, UpiPaymentAttempt.owner_user_id == uuid.UUID(asha.id)) == 1
    )


async def test_concurrent_initiations_with_same_key_create_exactly_one(client):
    """Critical scenario 5: 10 concurrent initiations with one key."""
    asha = await user(client, "asha")
    h = {**asha.h, "Idempotency-Key": "race-key"}
    body = {"payee_vpa": VPA, "payee_name": "Ravi", "amount": "120.00"}
    responses = await asyncio.gather(
        *(client.post(f"{API}/upi/payments", headers=h, json=body) for _ in range(10))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 9 + [201], [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len({r.json()["transaction_id"] for r in responses}) == 1
    assert await count(UpiPaymentAttempt) == 1
    assert await count(Transaction) == 1
    assert (await payee_row()).use_count == 1
    assert len(await audit_rows("upi.initiated")) == 1
    assert len(await audit_rows("transaction.created")) == 1


async def test_repeated_confirmations_never_duplicate_or_double_count(client):
    """Critical scenario 5: duplicate confirm callbacks (sequential and concurrent)."""
    asha = await user(client, "asha")
    pay = await initiate(client, asha, amount="300.00")
    await act(client, asha, pay["id"], "launched")
    results = await asyncio.gather(
        *(
            client.post(
                f"{API}/upi/payments/{pay['id']}/confirm",
                headers=asha.h,
                json={"outcome": "success", "utr": "123456789012"},
            )
            for _ in range(8)
        )
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    for _ in range(3):
        await act(client, asha, pay["id"], "confirm", outcome="success")
        await act(client, asha, pay["id"], "confirm", outcome="success", utr="123456789012")
    assert await count(Transaction) == 1 and await count(UpiPaymentAttempt) == 1
    s = await summary(client, asha)
    assert s["expense"] == "300.00" and s["count"] == 1
    assert len(await audit_rows("upi.confirmed")) == 1
    assert len(await audit_rows("transaction.status_changed")) == 1
    assert len(await audit_rows("transaction.created")) == 1


# --- confirm transitions & reconciliation ----------------------------------------------


async def test_confirm_transitions_and_ledger_effects(client):
    asha = await user(client, "asha")
    pay = await initiate(client, asha, amount="200.00")

    pending = await act(client, asha, pay["id"], "confirm", outcome="pending")
    assert pending["status"] == "pending_reported" and pending["needs_confirmation"] is True
    assert pending["confirmed_at"] is None and pending["upi_uri"] is None
    assert pending["transaction"]["status"] == "pending"
    assert (await summary(client, asha))["expense"] == "0.00"

    failed = await act(client, asha, pay["id"], "confirm", outcome="failure")
    assert failed["status"] == "failure_reported" and failed["confirmed_at"] is not None
    assert failed["transaction"]["status"] == "failed"
    assert failed["transaction"]["verification"] == "user_confirmed"
    assert (await summary(client, asha))["expense"] == "0.00"

    # reconciliation: the bank statement shows it went through after all
    ok = await act(client, asha, pay["id"], "confirm", outcome="success", utr="ABC123456")
    assert ok["status"] == "success_reported" and ok["transaction"]["status"] == "completed"
    assert ok["utr"] == "ABC123456" and ok["transaction"]["external_ref"] == "ABC123456"
    assert (await summary(client, asha))["expense"] == "200.00"

    # adding/changing the UTR later is audited too
    ok2 = await act(client, asha, pay["id"], "confirm", outcome="success", utr="xyz999888")
    assert ok2["utr"] == "XYZ999888" and ok2["transaction"]["external_ref"] == "XYZ999888"

    # and back to pending (e.g. the user mis-tapped): out of the totals again
    back = await act(client, asha, pay["id"], "confirm", outcome="pending")
    assert back["transaction"]["status"] == "pending"
    assert back["transaction"]["verification"] == "unverified"
    assert back["confirmed_at"] is None
    assert (await summary(client, asha))["expense"] == "0.00"

    assert await audit_actions(pay["id"]) == [
        "upi.initiated",
        "upi.confirmed",  # → pending_reported
        "upi.confirmed",  # pending_reported → failure (first real answer)
        "upi.reconciled",  # failure → success
        "upi.utr_updated",
        "upi.reconciled",  # success → pending
    ]
    rec = (await audit_rows("upi.reconciled"))[0]
    assert rec.details["changes"]["status"] == {
        "before": "failure_reported",
        "after": "success_reported",
    }
    assert rec.details["changes"]["transaction_status"] == {
        "before": "failed",
        "after": "completed",
    }
    assert rec.family_id is None and rec.details["outcome"] == "success"

    for bad in (
        {"outcome": "verified"},
        {"outcome": "success", "utr": "12345"},
        {"outcome": "success", "utr": "has space1"},
        {"outcome": "success", "utr": "x" * 36},
        {},
    ):
        await act(client, asha, pay["id"], "confirm", expect=422, **bad)


async def test_confirm_after_transaction_deleted(client):
    asha = await user(client, "asha")
    pay = await initiate(client, asha)
    r = await client.delete(f"{API}/transactions/{pay['transaction_id']}", headers=asha.h)
    assert r.status_code == 204
    out = await act(client, asha, pay["id"], "confirm", expect=409, outcome="success")
    assert out["error"]["code"] == "transaction_deleted"
    detail = (await client.get(f"{API}/upi/payments/{pay['id']}", headers=asha.h)).json()
    assert detail["transaction"] is None and detail["transaction_status"] is None


# --- cancel ----------------------------------------------------------------------------


async def test_cancel(client):
    asha = await user(client, "asha")
    pay = await initiate(client, asha)
    await act(client, asha, pay["id"], "launched")
    out = await act(client, asha, pay["id"], "cancel")
    assert out["status"] == "cancelled" and out["needs_confirmation"] is False
    assert out["transaction"]["status"] == "cancelled" and out["upi_uri"] is None
    again = await act(client, asha, pay["id"], "cancel")
    assert again["status"] == "cancelled"
    assert (await audit_actions(pay["id"])).count("upi.cancelled") == 1
    await act(client, asha, pay["id"], "launched", expect=409)
    # reconciliation is still possible: money actually left the account
    ok = await act(client, asha, pay["id"], "confirm", outcome="success")
    assert ok["transaction"]["status"] == "completed"
    assert (await audit_actions(pay["id"]))[-1] == "upi.reconciled"
    # a reported success can't be cancelled
    r = await act(client, asha, pay["id"], "cancel", expect=409)
    assert r["error"]["code"] == "upi_payment_succeeded"
    assert (await summary(client, asha))["expense"] == "250.00"


# --- isolation -------------------------------------------------------------------------


async def test_personal_payments_are_private(client):
    """Critical scenario 1 for UPI: user B can't see or touch user A's payments."""
    asha = await user(client, "asha")
    bob = await user(client, "bob")
    pay = await initiate(client, asha)
    assert (await client.get(f"{API}/upi/payments/{pay['id']}", headers=bob.h)).status_code == 404
    for action, body in (
        ("launched", {}),
        ("return", {"raw": "Status=SUCCESS"}),
        ("confirm", {"outcome": "success"}),
        ("cancel", {}),
    ):
        await act(client, bob, pay["id"], action, expect=404, **body)
    listed = (await client.get(f"{API}/upi/payments", headers=bob.h)).json()
    assert listed["total"] == 0
    pending = (await client.get(f"{API}/upi/payments/pending", headers=bob.h)).json()
    assert pending["total"] == 0
    assert (await tx_row(pay["transaction_id"])).status.value == "pending"
    unknown = await client.get(f"{API}/upi/payments/{uuid.uuid4()}", headers=asha.h)
    assert unknown.status_code == 404


async def test_family_roles(client):
    fid, p = await household(client)
    outsider = await user(client, "outsider")
    fam = {"scope": "family", "family_id": fid}

    # viewer can't initiate (403); non-member gets 404
    r = await initiate(client, p["viewer"], expect=403, **fam)
    assert r["error"]["code"] == "insufficient_family_role"
    await initiate(client, outsider, expect=404, **fam)

    by_member = await initiate(client, p["member"], **fam)
    by_admin = await initiate(client, p["admin"], payee_vpa="shop@okicici", **fam)
    assert by_member["scope"] == "family" and by_member["family_id"] == fid
    assert by_member["transaction"]["scope"] == "family"
    # the payee is a family payee shared by everyone in the family
    third = await initiate(client, p["owner"], **fam)
    assert third["payee_id"] == by_member["payee_id"]
    assert (await payee_row()).use_count == 2

    # every member (incl. viewers) sees family payments; outsiders don't
    for who in p.values():
        r = await client.get(f"{API}/upi/payments/{by_member['id']}", headers=who.h)
        assert r.status_code == 200
        listed = (
            await client.get(f"{API}/upi/payments", headers=who.h, params={"family_id": fid})
        ).json()
        assert listed["total"] == 3
    assert (
        await client.get(f"{API}/upi/payments/{by_member['id']}", headers=outsider.h)
    ).status_code == 404
    r = await client.get(f"{API}/upi/payments", headers=outsider.h, params={"family_id": fid})
    assert r.status_code == 404
    # family payments don't show up in the personal list
    assert (await client.get(f"{API}/upi/payments", headers=p["member"].h)).json()["total"] == 0

    # writes: initiator or owner/admin
    viewer_view = (
        await client.get(f"{API}/upi/payments/{by_member['id']}", headers=p["viewer"].h)
    ).json()
    assert viewer_view["can_edit"] is False
    await act(client, p["viewer"], by_member["id"], "confirm", expect=403, outcome="success")
    await act(client, p["member"], by_admin["id"], "confirm", expect=403, outcome="success")
    await act(client, p["member"], by_admin["id"], "launched", expect=403)
    await act(client, outsider, by_member["id"], "confirm", expect=404, outcome="success")
    member_view = (
        await client.get(f"{API}/upi/payments/{by_admin['id']}", headers=p["member"].h)
    ).json()
    assert member_view["can_edit"] is False
    admin_ok = await act(client, p["admin"], by_member["id"], "confirm", outcome="success")
    assert admin_ok["transaction"]["status"] == "completed"
    assert (await summary(client, p["owner"], family_id=fid))["expense"] == "250.00"

    # audit rows carry the family id
    rows = await audit_rows("upi.confirmed")
    assert len(rows) == 1 and str(rows[0].family_id) == fid
    assert str(rows[0].actor_user_id) == p["admin"].id


# --- lists & search --------------------------------------------------------------------


async def test_history_search_and_pending(client):
    asha = await user(client, "asha")
    a = await initiate(client, asha, note="Milk")
    b = await initiate(client, asha, payee_vpa="amma@ybl", payee_name="Amma", amount="5000")
    c = await initiate(client, asha, payee_vpa="tata.power@hdfcbank", payee_name="Tata Power")
    await act(client, asha, b["id"], "confirm", outcome="success", utr="998877665544")
    await act(client, asha, c["id"], "cancel")

    async def ids(path: str = "/upi/payments", **params: Any) -> list[str]:
        r = await client.get(f"{API}{path}", headers=asha.h, params=params)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    assert await ids() == [c["id"], b["id"], a["id"]]  # newest first
    assert await ids(q="amma") == [b["id"]]
    assert await ids(q="TATA.POWER@") == [c["id"]]
    assert await ids(q="milk") == [a["id"]]
    assert await ids(q="88776655") == [b["id"]]
    assert await ids(q=a["tr_ref"]) == [a["id"]]
    assert await ids(q="%") == []
    assert await ids(status="success_reported") == [b["id"]]
    assert await ids(status="cancelled") == [c["id"]]
    assert await ids("/upi/payments/pending") == [a["id"]]
    today = utcnow().date()
    assert len(await ids(date_from=str(today - timedelta(days=1)))) == 3
    assert await ids(date_to=str(today - timedelta(days=2))) == []
    r = await client.get(
        f"{API}/upi/payments",
        headers=asha.h,
        params={"date_from": str(today), "date_to": str(today - timedelta(days=1))},
    )
    assert r.status_code == 422
    page = (
        await client.get(f"{API}/upi/payments", headers=asha.h, params={"limit": 2, "offset": 0})
    ).json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["has_more"] is True
    item = page["items"][1]
    assert item["status"] == "success_reported" and item["status_label"]
    assert item["verification_label"] == "Confirmed by user, not verified with the bank"
    assert item["transaction_status"] == "completed" and "upi_uri" not in item


# --- rate limit ------------------------------------------------------------------------


async def test_initiation_rate_limit(client, settings):
    settings.upi_initiate_rate_limit = 2
    asha = await user(client, "asha")
    await initiate(client, asha, key="k1")
    await initiate(client, asha, key="k2")
    await initiate(client, asha, key="k1", expect=200)  # replays don't count
    r = await initiate(client, asha, key="k3", expect=429)
    assert r["error"]["code"] == "rate_limited"
    assert await count(UpiPaymentAttempt) == 2 and await count(Transaction) == 2


# --- reminders -------------------------------------------------------------------------


async def run_reminders(now) -> int:
    async with get_database().sessionmaker() as s:
        return await UpiService.send_pending_reminders(s, now)


async def notifications() -> list[Notification]:
    async with get_database().sessionmaker() as s:
        rows = await s.scalars(
            select(Notification)
            .where(Notification.category == NotificationCategory.UPI_PENDING)
            .order_by(Notification.created_at)
        )
        return list(rows)


async def test_pending_reminders(client):
    fid, p = await household(client)
    asha = await user(client, "asha")
    open_pay = await initiate(client, asha, amount="75.50")
    done = await initiate(client, asha, payee_vpa="amma@ybl")
    await act(client, asha, done["id"], "confirm", outcome="success")
    cancelled = await initiate(client, asha, payee_vpa="bob@ybl")
    await act(client, asha, cancelled["id"], "cancel")
    fam_pay = await initiate(client, p["member"], scope="family", family_id=fid)
    await act(client, p["member"], fam_pay["id"], "confirm", outcome="pending")

    now = utcnow()
    assert await run_reminders(now + timedelta(minutes=10)) == 0
    assert await run_reminders(now + timedelta(minutes=31)) == 2
    assert await run_reminders(now + timedelta(minutes=45)) == 0  # deduped
    rows = await notifications()
    assert {(str(n.user_id), n.dedupe_key) for n in rows} == {
        (asha.id, f"upi:{open_pay['id']}:pending:1"),
        (p["member"].id, f"upi:{fam_pay['id']}:pending:1"),
    }
    mine = next(n for n in rows if str(n.user_id) == asha.id)
    assert mine.category.value == "upi_pending"
    assert mine.data == {
        "upi_attempt_id": open_pay["id"],
        "transaction_id": open_pay["transaction_id"],
        "amount": "75.50",
        "payee_name": "Ravi Kirana",
        "category": "upi_pending",
    }
    fam_note = next(n for n in rows if str(n.user_id) == p["member"].id)
    assert str(fam_note.family_id) == fid

    assert await run_reminders(now + timedelta(hours=25)) == 2
    assert await run_reminders(now + timedelta(hours=26)) == 0
    assert len(await notifications()) == 4
    # nothing was completed or cancelled automatically
    assert (await tx_row(open_pay["transaction_id"])).status.value == "pending"
    detail = (await client.get(f"{API}/upi/payments/{open_pay['id']}", headers=asha.h)).json()
    assert detail["status"] == "initiated"
    # confirmed later → no more reminders even for a fresh stage
    await act(client, asha, open_pay["id"], "confirm", outcome="failure")
    async with get_database().sessionmaker() as s:
        await s.execute(
            Notification.__table__.delete().where(Notification.user_id == uuid.UUID(asha.id))
        )
        await s.commit()
    assert await run_reminders(now + timedelta(hours=30)) == 0


async def test_first_reminder_skipped_for_day_old_attempts(client):
    asha = await user(client, "asha")
    pay = await initiate(client, asha)
    assert await run_reminders(utcnow() + timedelta(hours=25)) == 1
    assert [n.dedupe_key for n in await notifications()] == [f"upi:{pay['id']}:pending:2"]
    assert await run_reminders(utcnow() + timedelta(days=8)) == 0  # beyond the look-back


async def test_no_reminder_after_leaving_family(client):
    fid, p = await household(client)
    pay = await initiate(client, p["member"], scope="family", family_id=fid)
    r = await client.post(f"{API}/families/{fid}/leave", headers=p["member"].h)
    assert r.status_code in (200, 204), r.text
    assert await run_reminders(utcnow() + timedelta(hours=1)) == 0
    assert (
        await client.get(f"{API}/upi/payments/{pay['id']}", headers=p["member"].h)
    ).status_code == 404
