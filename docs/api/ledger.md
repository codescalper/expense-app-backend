# Accounts & transactions API (`/api/v1`)

Money = 2-dp strings. Errors `{"error":{"code","message","details?"}}`. Invisible → 404; visible family row you can't edit → 403 `insufficient_family_role`.

## Accounts
| Method | Path | Notes |
|---|---|---|
| POST | `/accounts` | 201. Family accounts need owner/admin |
| GET | `/accounts?family_id=&include_inactive=false` | `[AccountOut]`, active first then by name |
| GET / PATCH | `/accounts/{id}` | PATCH `name, type, institution (null clears), opening_balance, is_active` |
| POST | `/accounts/{id}/archive` · `/unarchive` | AccountOut |
| DELETE | `/accounts/{id}` | 204, or 409 `account_in_use` (archive instead) |

Create: `{"name":"HDFC Savings","type":"bank|cash|upi|credit_card|debit_card|wallet|other","institution":"HDFC","opening_balance":"1500.50","currency":"INR","scope":"personal|family","family_id":null}` (opening balance may be negative; credential-like fields → 422).

AccountOut: `{"id","name","type","institution","currency","opening_balance","current_balance","scope","family_id","owner_user_id","is_active","can_manage","created_at","updated_at"}`

## Transactions
| Method | Path | Notes |
|---|---|---|
| POST | `/transactions` | 201 created / 200 idempotent replay. Header `Idempotency-Key` (`[A-Za-z0-9._:-]{1,100}`) or body `idempotency_key`. A replay returns the original row only for the same scope/family (else 409 `idempotency_key_reused`) and only while you can still see it (404 after leaving the family) |
| GET | `/transactions` | filters → `Page[TransactionOut]` |
| GET | `/transactions/summary` | same filters → `{"income","expense","net","count"}` |
| GET | `/transactions/export.csv` | same filters, CSV attachment (formula-injection-safe cells; per-user limit 20 / 10 min shared with `/reports/export.csv` → 429) |
| GET / PATCH / DELETE | `/transactions/{id}` | PATCH partial; DELETE soft |
| POST | `/transactions/{id}/restore` | undo delete (409 `restore_conflict`) |

Create body:
```json
{"type":"expense","amount":"250.50","currency":"INR","scope":"personal","family_id":null,
 "account_id":"…","to_account_id":null,"category_id":"…","payee_id":null,
 "merchant":"Spice Hut","description":"Dinner","notes":"…","payment_method":"card",
 "occurred_at":"2026-11-18T19:30:00+05:30","adjustment_sign":null,
 "tags":["food","party"],"status":"completed","idempotency_key":"client-uuid-1"}
```
- type: expense|income|transfer|refund|adjustment; status: completed (default) | pending; payment_method: cash|upi|card|bank_transfer|wallet|other.
- `occurred_at` defaults to now; without offset → user's timezone. Tags lowercased, ≤10, ≤30 chars.
- Transfer: `account_id` + `to_account_id` (different), no category. Adjustment: `account_id` + `adjustment_sign` ±1, no category.
- expense/refund need an expense category; income an income category. Currency defaults to the account's.
- 422 codes: `transfer_accounts, transfer_same_account, to_account_not_allowed, adjustment_account_required, adjustment_sign_required, adjustment_sign_not_allowed, category_not_allowed, category_kind_mismatch, category_archived, currency_mismatch, account_inactive, account_scope_mismatch, payee_scope_mismatch, idempotency_key_mismatch, scope_change_not_allowed, status_change_not_allowed`.
- `account_inactive` / `category_archived` apply only to a *newly attached* account/category (create, or PATCH changing it): an existing row whose account/category was archived later stays editable.
- PATCH: any subset of the create fields except `idempotency_key`; `null` clears optional fields; `tags` replaces the set; status only on `source=manual` rows. Flow-owned fields (`source`, `verification`, `external_ref`, `recurring_id`, `period_key`, `upi_attempt_id`, `idempotency_key`, `owner`) are not accepted (422 — unknown fields are rejected).

List query: `family_id`, `date_from`, `date_to` (YYYY-MM-DD inclusive, user tz), repeatable `types`, `category_ids` (incl. subcategories), `account_id` (either side of transfers), `payment_method`, `member_user_id`, `status`, `tag`, `q` (description/merchant/notes/payee name), `min_amount`, `max_amount`, `sort_by` occurred_at|amount, `sort_dir` desc|asc, `limit` (≤200), `offset`.

TransactionOut:
```json
{"id","type","status","source":"manual|upi_intent|import|recurring","verification":"unverified|user_confirmed|verified",
 "amount","currency","scope","family_id","created_by":{"id","full_name"},
 "account":{"id","name","type","is_active"},"to_account":null,
 "category":{"id","name","kind","icon","color","parent_id"},"payee":{"id","name","upi_id"},
 "merchant","description","notes","payment_method","occurred_at","adjustment_sign","tags":[],
 "attachments_count":0,"recurring_id":null,"period_key":null,"upi_attempt_id":null,"external_ref":null,
 "idempotency_key","can_edit":true,"created_at","updated_at","deleted_at":null}
```
Summary follows ledger rules (completed, non-deleted; transfers/adjustments excluded; refunds reduce expense); `count` = all matching non-deleted rows.

## Server-side helpers (for UPI / recurring code)
```python
tx, created = await TransactionService(session).create_internal(
    actor, data: TransactionCreate, *, source=..., status=..., verification=...,
    idempotency_key=None, upi_attempt_id=None, external_ref=None,
    recurring_id=None, period_key=None, client=None)   # no commit; savepoint; dup idempotency → (existing, False)
tx = await TransactionService(session).mark_status(tx_id, status, *, actor, verification=None,
    external_ref=None, client=None, reason=None)        # no commit, no permission check, audited
```
