# Dashboard & reports API (`/api/v1`)

Money = 2-dp strings; percent = 1-dp numbers. Errors `{"error":{"code","message","details"}}`; not a member / invisible → 404; bad params → 422.

**Common query params:** `family_id` (absent = personal), `preset=today|this_week|this_month|last_month|custom` (default this_month; weeks Mon–Sun), `from`/`to` (YYYY-MM-DD inclusive, caller's tz; implies custom), `currency` (optional).
422 codes: `range_conflict, range_required, range_invalid, range_too_large, invalid_currency`.
Rate limits (per user): `/dashboard` + `/reports/*` share 300 requests / 5 min; `/reports/export.csv` also counts towards the CSV-export limit (20 / 10 min, shared with `/transactions/export.csv`). Beyond them → 429 `rate_limited` with `Retry-After`.
Every report has `meta: {scope, family_id, currency, currencies[], timezone, range: {preset, start_date, end_date}}` — totals never mix currencies; offer a switch when `currencies` has >1.

## GET /dashboard
```json
{"meta":{…},
 "balance":{"available":"56950.00","liabilities_total":"-2000.00","net_worth":"54950.00",
   "accounts":[{"id","name","type","balance"}],"liabilities":[{"id","name","type":"credit_card","balance":"-2000.00"}]},
 "totals":{"income","expense","net_savings","savings_rate":88.9},
 "spending_split":{"view":"personal|family","personal","family","members":[MemberSpending]},
 "budget":{…same as GET /budgets/overview…},
 "recent_transactions":[{"id","type","status","source","amount","currency","occurred_at","description","merchant","payee_name","account_name","category":{"id","name","icon","color"}|null,"created_by","created_by_name","upi_attempt_id":"uuid|null"}],
 "upcoming_bills":[UpcomingOut (docs/api/recurring.md)],
 "savings":{"active_count","target_total","saved_total","goals":[GoalOut ×≤3]},
 "spending_by_category":[{"category_id","name","icon","color","amount","count","percent","has_children","is_other"}],
 "monthly_trend":[{"month":"2026-06","start_date","end_date","income","expense","net"} ×6],
 "pending_upi_count":2}
```
Balance: active accounts; credit cards only under liabilities; net_worth = available + liabilities_total. Recent = last 8 completed/pending; `upi_attempt_id` is set on rows created by the UPI flow (open `/upi/payments/{upi_attempt_id}` to confirm a pending payment), null otherwise. Upcoming = next 7 days incl. overdue. Category slices = top 6 + "Other". Trend = last 6 months.

## Reports (`/reports/…`)
- `spending?granularity=day|week|month|year` → `{meta, granularity, total, average, items:[{period, start_date, end_date, amount}]}` (zero-filled, ≤1000 buckets)
- `income-vs-expense?months=12` → `{meta, income, expense, net, savings_rate, items:[{month, start_date, end_date, income, expense, net}]}`
- `categories?kind=expense|income&parent_id=` → `{meta, kind, parent, total, items:[CategorySlice]}` (subcategories roll up; `category_id:null` = Uncategorized; drill down with parent_id; skip negative slices in donuts)
- `members` (family only; else 422 `family_scope_required`) → `{meta, total, items:[{user_id, full_name, is_active_member, expense, income, count, percent}]}`
- `personal-vs-family?months=6` (personal only; else 422 `personal_scope_only`) → `{meta, personal, family, families:[{family_id, name, amount}], items:[{month, start_date, end_date, personal, family}]}`
- `payment-methods` → `{meta, total, items:[{payment_method, amount, count, percent}]}`
- `account-balances?account_id=&days=90` → `{meta, account_id, name, type, is_active, opening_balance, current_balance, items:[{date, balance, change}]}`
- `savings-trend?months=12` → `{meta, goal_count, target_total, current_total, items:[{month, start_date, end_date, deposits, withdrawals, net, balance}]}`
- `budget-performance?periods=6` → `{meta, budgets:[{budget_id, name, period, currency, member_user_id, average_percent, exceeded_count, items:[PeriodStatus + is_current]}]}`
- `recurring-summary` → `{meta, active_rules, monthly_expense, monthly_income, monthly_transfer, by_category:[{category_id, name, icon, color, monthly_amount, rule_count, percent}], by_frequency:[{frequency, monthly_amount, rule_count}], rules:[{recurring_id, name, type, frequency, interval_count, amount, monthly_amount, category_id, next_due_date}]}`
- `upi` → `{meta, attempts, attempted_amount, success_reported, success_reported_amount, success_reported_rate, open_attempts, pending_confirmation, confirmed_spend, by_status:[{status, count, amount}], top_payees:[{payee_vpa, payee_name, count, amount}], by_category:[CategorySlice]}`
- `export.csv?report=<name>&…` → CSV attachment (text cells are protected against spreadsheet formula injection) (`<report>-<start>-<end>.csv`; extra params granularity, months, kind, parent_id, account_id, days, periods)
