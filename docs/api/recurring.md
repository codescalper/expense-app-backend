# Recurring bills API (`/api/v1/recurring`)

Money values are strings. Errors: `{"error":{"code","message","details"}}`; invisible → 404, role too low → 403, validation → 422.

**RecurringOut**
```json
{"id":"uuid","scope":"personal","family_id":null,"owner_user_id":"uuid","name":"Rent",
 "type":"expense","amount":"15000.00","currency":"INR","account_id":"uuid|null","to_account_id":null,
 "category_id":"uuid|null","payee_id":"uuid|null","description":"Flat 4B|null","payment_method":"upi|null",
 "frequency":"monthly","interval_count":1,"start_date":"2026-11-20","end_date":null,
 "next_due_date":"2026-12-20","reminder_days_before":1,"auto_generate":true,"is_paused":false,
 "status":"active","last_generated_at":"2026-11-20T06:00:00Z",
 "latest_occurrence":{"transaction_id":"uuid","period_key":"2026-11-20","due_date":"2026-11-20","status":"pending","amount":"15000.00"},
 "can_edit":true,"created_at":"…","updated_at":"…"}
```
`status`: active | paused | ended (`next_due_date` null when ended); `latest_occurrence` null until generated.
`can_edit`: whether the caller may PATCH / DELETE / pause / resume the rule and mark-paid / skip / unmark its occurrences — personal rule: its owner; family rule: owner/admin, or the member who created it; viewers never. Use it to show/hide edit controls (the API enforces the same rule: 403 otherwise).
`period_key` (also on generated transactions) is the occurrence's scheduled date in ISO format for every frequency.

**OccurrenceOut** (a generated transaction)
```json
{"transaction_id":"uuid","recurring_id":"uuid","period_key":"2026-11-20","due_date":"2026-11-20",
 "status":"pending|completed|cancelled|failed","verification":"unverified|user_confirmed|verified",
 "type":"expense","amount":"15000.00","currency":"INR","account_id":"uuid|null","to_account_id":null,
 "category_id":null,"payee_id":null,"occurred_at":"2026-11-20T03:30:00Z","external_ref":null,
 "created_at":"…","updated_at":"…"}
```

**UpcomingOut**
```json
{"recurring_id":"uuid","name":"Rent","scope":"personal","family_id":null,"type":"expense",
 "amount":"15000.00","currency":"INR","account_id":null,"category_id":null,"payee_id":null,
 "due_date":"2026-11-20","period_key":"2026-11-20","days_until_due":0,"is_overdue":false,
 "status":"scheduled|pending|completed|cancelled|failed","transaction_id":"uuid|null"}
```
`scheduled` = not generated yet.

## Endpoints
- `POST /recurring` → 201 RecurringOut (family rules: any member with create rights — owner/admin/member). Body: `{scope?, family_id?, name (1–80), type: expense|income|transfer, amount, currency?, account_id?, to_account_id? (transfer only; required for transfer; ≠ account_id), category_id? (not on transfers; kind must match), payee_id?, description? (≤255), payment_method?: cash|upi|card|bank_transfer|wallet|other, frequency: daily|weekly|monthly|yearly, interval_count? (1–365), start_date: YYYY-MM-DD, end_date?, reminder_days_before? (0–60, default 1), auto_generate? (default true)}`.
  **UI: default `start_date` to the next real due date** — a past start date back-fills pending occurrences.
  Newly referenced accounts must be active and categories not archived (422 with a field error); a rule whose account/category was archived *later* keeps working and stays editable.
  `currency` defaults to the account's currency (else the family's, else INR) and must match the rule's accounts (422). `mark-paid` with `account_id` also needs an active account in the bill's currency.
  A family rule only generates/reminds while its creator is still an active owner/admin/member of the family (it stops if they leave or become a viewer).
- `GET /recurring?family_id=&status=active|paused|ended&limit&offset` → `Page[RecurringOut]` = `{items,total,limit,offset,has_more}`.
- `GET /recurring/{id}` · `PATCH /recurring/{id}` (partial; `null` clears optional fields; members edit only their own, admins any) · `DELETE /recurring/{id}` → 204.
- `POST /recurring/{id}/pause` · `POST /recurring/{id}/resume` → RecurringOut.
- `GET /recurring/{id}/occurrences?limit&offset` → `Page[OccurrenceOut]` (latest first).
- `GET /recurring/upcoming?days=30&family_id=&include_overdue=true` → `[UpcomingOut]` sorted by due_date — dashboard "Upcoming bills".
- `POST /recurring/occurrences/{transaction_id}/mark-paid` body `{amount?, occurred_at?, account_id?, external_ref?}` → completed + `user_confirmed`.
- `POST /recurring/occurrences/{transaction_id}/skip` → cancelled (completed → 409 `invalid_status`).
- `POST /recurring/occurrences/{transaction_id}/unmark` → back to pending.

Notifications: `bill_upcoming` (within reminder window), `recurring_reminder` (still pending the day after due). Deep link `familybudget://recurring/{recurring_id}`.
