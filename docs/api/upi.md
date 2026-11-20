# UPI payments API (`/api/v1/upi`)

Bearer auth; money = 2-dp strings; errors `{"error":{"code","message","details?"}}`. Any body field named like `pin, upi_pin, mpin, password, cvv, otp, card_number` → 422; unknown fields → 422. Invisible → 404; family role too low → 403.

## Flow
1. Scan QR (parse locally) + `POST /upi/parse` → prefill; lock amount when `amount_locked`.
2. `POST /upi/payments` with a fresh idempotency key (one per payment screen; reuse on retries) → `upi_uri`.
3. `POST /upi/payments/{id}/launched`, then open `upi_uri` (OS app chooser).
4. When the app returns to the foreground: `POST /upi/payments/{id}/return` (raw data if any) — informational only.
5. ALWAYS ask the user: succeeded / failed / still pending → `POST /upi/payments/{id}/confirm`.
6. Unresolved payments: `GET /upi/payments/pending`.

**Opening a UPI app never means success. Only the user's confirm changes money state. `verified` is never set by this API.**

## POST /upi/parse
Req `{"payload":"upi://pay?pa=Swiggy.Stores@AXB&pn=Swiggy&am=349&cu=INR&tn=Dinner&mc=5812","family_id":null}`
Resp `{"payee_vpa","payee_name","amount":"349.00"|null,"amount_locked":true,"currency":"INR","note","merchant_code","reference","mode","purpose","url","unknown":{},"warnings":[],"known_payee":PayeeOut|null,"suggested_category":{"category_id","source":"history|payee_default|keyword|none","confidence","category":{…}|null}}`
422 codes: `not_upi_uri, missing_vpa, invalid_vpa, invalid_amount, unsupported_currency, duplicate_param, payload_too_long, invalid_upi_uri`.

## POST /upi/payments (initiate)
Idempotency key REQUIRED: header `Idempotency-Key` (`[A-Za-z0-9._:-]{1,96}`) or body `idempotency_key`.
Req `{"payee_vpa":"ravi.kirana@okaxis","payee_name":"Ravi Kirana","amount":"250.00","note":"Groceries","category_id":"…","account_id":"…","payee_id":null,"merchant_code":"5411","scope":"personal","family_id":null,"from_qr":true,"qr_amount_locked":true}` (only payee_vpa + amount required).
→ 201 new / 200 replay: **UpiPaymentDetailOut**
```json
{"id","scope","family_id","status":"initiated","status_label":"Not paid yet",
 "verification":"unverified","verification_label":"Not verified","needs_confirmation":true,
 "payee_vpa","payee_name","payee_id","amount","currency","note","tr_ref","utr":null,
 "transaction_id","transaction_status":"pending","initiated_by":{"id","full_name"},
 "initiated_at","launched_at":null,"confirmed_at":null,"created_at","updated_at","can_edit":true,
 "upi_uri":"upi://pay?pa=…&pn=Ravi%20Kirana&am=250.00&cu=INR&tn=…&tr=FB…&mc=5411",
 "payee":{"id","name","upi_id"},"transaction":{…TransactionOut},"untrusted_hint":null}
```
`upi_uri` non-null only while status is initiated|launched. Errors: 422 `idempotency_key_required|idempotency_key_mismatch`, 409 `idempotency_key_reused` (same key, different payment), 422 transaction validation codes (e.g. `category_archived`, `account_inactive`), 404, 429 `rate_limited` (per user: 30 new payments / 10 min, 120 parses / 10 min — configurable, see docs/ENVIRONMENT.md).

## POST /upi/payments/{id}/launched
Empty body → status launched. No money change. 409 `upi_not_launchable` once answered/cancelled.

## POST /upi/payments/{id}/return
`{"raw":"txnId=…&responseCode=00&Status=SUCCESS&txnRef=…&ApprovalRefNo=…"}` (string, object or omitted) → detail with
`"untrusted_hint":{"trusted":false,"status":"success|failure|submitted|unknown","txn_id","response_code","approval_ref","txn_ref","reference_matches","suggested_utr","received_at","message"}`. No status/money change; prefill UTR from `suggested_utr`.

## POST /upi/payments/{id}/confirm
`{"outcome":"success|failure|pending","utr":"432112345678"}` (utr optional, 6–35 alphanumerics)
| outcome | attempt | transaction | verification |
|---|---|---|---|
| success | success_reported | completed (counts) | user_confirmed |
| failure | failure_reported | failed | user_confirmed |
| pending | pending_reported | pending | unverified |
Same answer again = no-op; changing the answer later is allowed (reconciliation, audited). 409 `transaction_deleted`.

## POST /upi/payments/{id}/cancel
→ attempt + transaction cancelled. 409 `upi_payment_succeeded`.

## Lists
- `GET /upi/payments?family_id=&status=&q=&date_from=&date_to=&limit&offset` → `Page[UpiPaymentOut]` newest first (q: payee name, VPA, note, UTR, tr_ref)
- `GET /upi/payments/pending?family_id=` → initiated | launched | pending_reported
- `GET /upi/payments/{id}` → UpiPaymentDetailOut

Status labels: initiated "Not paid yet", launched "Awaiting your confirmation", success_reported "Paid (confirmed by user)", failure_reported "Failed (reported by user)", pending_reported "Pending at bank", cancelled "Cancelled". Verification: unverified "Not verified", user_confirmed "Confirmed by user, not verified with the bank".

Reminders: `upi_pending` notifications at 30 min and 24 h (data `{upi_attempt_id, transaction_id, amount, payee_name}`) → deep link `familybudget://upi/{upi_attempt_id}`.
