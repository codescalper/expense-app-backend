# Budgets & savings API (`/api/v1`)

Money = 2-dp strings; percents = numbers with 1 decimal. Invisible → 404, role too low → 403, validation → 422.

## Budgets
- `GET /budgets?family_id=&is_active=` → `[BudgetOut]` (no family_id = personal budgets)
- `POST /budgets` → 201 BudgetDetailOut. Body:
  `{"name":"Groceries","scope":"family","family_id":"…","period":"monthly","start_date":"2026-11-01","end_date":null,"amount":"8000.00","currency":"INR","member_user_id":null,"carry_forward":true,"alert_thresholds":[50,75,90,100],"is_active":true,"categories":[{"category_id":"…","amount":"3000.00"}]}`
  - period monthly|weekly|custom (custom needs start_date + end_date); start_date defaults to today.
  - **If `categories` are given, the budget's overall `spent` counts only those categories (incl. subcategories).** No categories = all spending.
  - Category limits on a **deleted** category are ignored (not listed, not counted). Deleting a category with `reassign_to` moves its limits to the target (amounts are added to an existing limit on the target); deleting without `reassign_to` removes them (`DELETE /categories/{id}` reports `moved_budget_limits` / `removed_budget_limits`).
- `GET /budgets/{id}` → BudgetDetailOut · `PATCH /budgets/{id}` (partial; `categories` replaces the list) · `DELETE /budgets/{id}` → 204
- `GET /budgets/{id}/history?periods=6` → `{budget_id, period, items: [PeriodStatus + is_current]}` (oldest first)
- `GET /budgets/overview?family_id=` → dashboard summary:
  `{"scope","family_id","month":{"period_key":"2026-11","start_date","end_date","basis":"overall|categories|none","budget_count","total_budget","spent","remaining","percent","status"},"warnings":[{"budget_id","name","period","member_user_id","limit","spent","remaining","percent","status"}]}`

BudgetOut:
```json
{"id","scope","family_id","owner_user_id","member_user_id","name","period","start_date","end_date",
 "amount","currency","carry_forward","alert_thresholds","is_active",
 "categories":[{"category_id","name","amount"}],"can_edit","created_at","updated_at",
 "current":{"period_key":"2026-11","start_date":"2026-11-01","end_date":"2026-11-30","state":"current",
            "limit":"1300.00","carried":"300.00","spent":"1200.00","remaining":"100.00",
            "percent":92.3,"status":"ok|warning|exceeded","days_left":11}}
```
BudgetDetailOut adds `"category_breakdown":[{"category_id","name","limit","spent","remaining","percent","status"}]`.
Status: exceeded when spent > limit, warning at ≥75%, else ok. Spending only counts transactions in the budget's `currency`. Carry-forward: previous period's unspent (or overspend) adjusts this period's limit.

## Savings goals
- `GET /savings?family_id=&status=active|completed|archived` → `[GoalOut]`
- `POST /savings` → 201 GoalOut. Body `{"name":"Laptop","target_amount":"80000.00","scope":"personal","family_id":null,"target_date":"2027-05-20","currency":"INR","icon":"laptop","color":"#22C55E"}`
- `GET /savings/{id}` · `PATCH /savings/{id}` (`name, target_amount, target_date|null, icon|null, color|null, status: active|archived`) · `DELETE /savings/{id}` → 204
- `GET /savings/{id}/contributions?limit&offset` → `Page[ContributionOut]` newest first
- `POST /savings/{id}/contributions` → 201 `{"contribution": ContributionOut, "goal": GoalOut}`. Body `{"kind":"deposit|withdrawal","amount":"500.00","note":"…","contributed_at":"2026-11-20T10:00:00Z","transaction_id":null}`. Overdraw → 409 `insufficient_goal_balance`; archived → 409 `goal_archived`. `transaction_id` must be a transaction you can see (else 404) **in the goal's own scope** (personal goal → your personal transaction; family goal → that family's) and currency (422 `transaction_scope_mismatch` / `currency_mismatch`).
- `DELETE /savings/{id}/contributions/{cid}` → 200 GoalOut (409 if it would go negative)
- `GET /savings/{id}/history` → `{"goal_id","items":[{"month":"2026-08","deposits","withdrawals","net","balance"}]}`

GoalOut:
```json
{"id","scope","family_id","owner_user_id","name","target_amount","current_amount","remaining_amount",
 "progress_percent","target_date","currency","status":"active|completed|archived","icon","color","completed_at",
 "pacing":{"target_date","days_left","months_left","required_monthly","overdue"}|null,
 "can_edit","can_contribute","created_at","updated_at"}
```
ContributionOut: `{"id","goal_id","user_id","user_name","kind","amount","note","contributed_at","transaction_id","can_delete","created_at"}`
- `user_name`: who added it. `can_delete`: personal goal → its owner; family goal → owner/admin, or the member who added it (viewers never) — same rule as `DELETE …/contributions/{cid}` (403 otherwise).

Notifications: `budget_alert` (data `{budget_id, period_key, threshold, percent}`), `savings_milestone` (data `{goal_id, milestone}`).
