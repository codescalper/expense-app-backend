# Product spec — Family Budget & Expense Management App

Source of truth for features. Condensed from the original build brief; requirements are kept
verbatim where they matter. **Project decision overriding the brief: no Redis — PostgreSQL is the
only stateful dependency** (rate limits and background jobs live in Postgres).

## 1. Overview
Mobile-first app for individuals and families (initially 5–10 members) to: track income/expenses;
manage personal and shared family finances; record UPI payments quickly; monthly budgets;
recurring bills/subscriptions; savings goals; reports & analytics; reminders/notifications;
multiple bank/cash/digital accounts; invite family members and control access.
Each user has private personal transactions plus access to shared family transactions by permission.

Stack: Expo React Native (TypeScript, Expo Router, TanStack Query, Zustand, RHF, Zod) · FastAPI,
Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL (Neon in prod) · Docker Compose · GitHub Actions.

## 2. Authentication — DONE (Phase 2)
Register, login, logout, forgot/reset password (time-limited), email verification, refresh-token
rotation, session management, logout all devices, biometric app lock, profile + picture, change
password, delete account. Argon2id, short-lived access tokens, SecureStore on mobile, throttling.

## 3. Family management (Phase 3)
- Create family, family name, family profile image; invite members by email or invitation link;
  accept/reject invitations; remove members; transfer ownership; leave family; manage permissions.
- Roles: **Owner** (full control) · **Admin** (manage members, budgets, transactions) ·
  **Member** (add and view permitted shared transactions) · **Viewer** (read-only).
- Two transaction scopes: **personal** (visible only to owner) and **family** (visible to authorised
  members). Every transaction has an explicit owner and scope. Never expose one family's data to
  another. Enforce membership/authorisation in the backend, not just the UI.

## 4. Dashboard (Phase 4/7)
Total available balance, monthly income, monthly expenses, net savings, family spending, personal
spending, remaining monthly budget, recent transactions, upcoming bills, savings progress, spending
by category, monthly trend, budget warnings. Switch personal ↔ family dashboard. Date filters: today,
this week, this month, last month, custom range. Clear charts/cards, mobile-optimised.

## 5. Transactions (Phase 4)
Types: expense, income, transfer, refund, adjustment.
Fields: id, amount, currency (default INR), type, category, subcategory, description, merchant/payee,
payment method, account, transaction date, transaction time, created timestamp, created by user,
family id where applicable, personal/family scope, notes, attachments/receipt images, tags,
recurring reference, status, source (manual, upi_intent, import, recurring…).
Default expense categories: Food & Dining, Groceries, Rent, Utilities, Electricity, Internet, Mobile
Recharge, Transportation, Fuel, Shopping, Healthcare, Education, Entertainment, Travel, Insurance,
Subscriptions, EMI & Loans, Personal Care, Family Expenses, Other.
Categories: create/edit/delete, icons & colours, subcategories, monthly category budgets.
Management: add, edit, delete with confirmation, search, filter by date/category/payment
method/user/family member, sort by amount/date, CSV export, history. Audit records for financial
modifications. Soft/reversible deletion.

## 6. UPI payments — PRIORITY (Phase 5)
Flow: (1) initiate — scan UPI QR, enter UPI ID, mobile number where supported, pick previous payee,
amount, note, category, personal/family. (2) parse QR locally: payee VPA (pa), name (pn), merchant
info (mc etc.), amount (am), note (tn), currency (cu); if no amount, user enters it. (3) create a
**pending** transaction in the backend before launching (amount, payee, VPA, category, user, family,
date, time, initiation timestamp, unique transaction reference). (4) launch via UPI intent
`upi://pay?pa=…&pn=…&am=…&cu=INR&tn=…` using Linking → OS app chooser (GPay, PhonePe, Paytm, BHIM…);
never hardcode installed apps. (5) on return: handle app lifecycle, restore the pending transaction,
confirmation screen asking succeeded / failed / still pending, optional UTR/reference, save the
user-reported status.
**Integrity rules:** opening a UPI app ≠ success; never mark success because the app opened; treat
return-intent data as untrusted; distinguish *user-confirmed* from *independently verified*; never
collect/store UPI PINs, bank passwords or payment credentials; never claim bank-level verification;
duplicate prevention via idempotency keys and transaction references; manual reconciliation of
pending payments.
Extras: recent payees, frequent payees, payment history, UPI analytics, category suggestion from
history, search UPI transactions, pending-payment reminders, receipt attachment, status labels.
Minimum taps between entering an amount and launching the payment app.

## 7. Accounts & payment methods (Phase 4)
Types: bank, cash, UPI, credit card, debit card, wallet, other. Fields: name, type, institution,
opening balance, current calculated balance, currency, owner, personal/family scope, active flag.
Transfers between accounts. **Transfers are never income or expense in spending calculations.**
Never store bank logins, CVVs or UPI PINs.

## 8. Budgets (Phase 6)
Monthly, weekly, custom-period, category-specific, family, personal. Set monthly limit, category
limits, individual member budgets where permitted, actual spending, remaining, % consumed,
carry-forward settings, history. Alerts at 50/75/90/100 % and exceeded; thresholds configurable.

## 9. Recurring expenses & bills (Phase 6)
Rent, electricity, internet, recharge, subscriptions, insurance, EMIs, salary. Daily/weekly/monthly/
yearly + custom intervals, start/end date, next due date, reminder settings, auto-generation of
**pending** transactions, pause/resume, payment status. Never auto-mark as paid without user
confirmation or a trusted source. A recurring transaction must never be generated twice for the same
period.

## 10. Savings goals (Phase 6)
Emergency fund, vacation, laptop, vehicle, home, general. Name, target amount, current amount, target
date, contributions, progress %, personal/family ownership, history, visual progress.

## 11. Notifications (Phase 7)
Expo push. Budget alerts, upcoming bills, recurring reminders, payment confirmation reminders, pending
UPI reminders, family invitations, new member, shared family expense, savings milestones, unusual
spending. Architecture: notification records in Postgres, device push tokens stored securely,
preferences, categories, no duplicates, handle invalid/expired tokens, scheduled notifications, mark
read, history, respect time zones and preferences. Background worker (Postgres job queue).

## 12. Reports (Phase 7)
Daily/weekly/monthly/yearly spending, income vs expenses, category-wise, member-wise, personal vs
family, payment method breakdown, account balance history, savings trend, budget performance,
recurring summary. Charts: donut (categories), bar (monthly income/expense), line (trends), budget
progress, savings progress. CSV export and date filtering. All calculations from DB with correct
aggregation rules (transfers excluded from income/expense; refunds reduce expense; only
non-deleted, non-failed transactions count; pending UPI not counted as spent until confirmed).

## 13. Database
Entities: users, user_sessions, families, family_members, family_invitations, accounts, transactions,
transaction_categories, transaction_tags, transaction_attachments, budgets, budget_categories,
recurring_transactions, savings_goals, savings_contributions, payees, upi_payment_attempts,
notifications, notification_preferences, device_tokens, audit_logs.
UUID PKs, FKs, indexes, unique constraints, created/updated timestamps, soft deletion, NUMERIC for
money (never float), DB transactions for multi-step operations, tenant isolation, no orphaned
financial records, migrations for all changes.

## 16. Security (all phases)
HTTPS in prod, RBAC, family-level authorisation on **every** query, input validation, ORM-only SQL,
rate limiting, CORS allowlist, secure headers, secrets via env, secure file uploads with type/size
validation, audit logging, log redaction, backups, account deletion & data export, dependency
scanning. Test explicitly for IDOR and cross-family leakage.

## 17. Critical test scenarios
1. User A cannot access User B's private expenses. 2. Family A cannot access Family B's transactions.
3. A member cannot perform admin-only actions. 4. Launching a UPI app never marks a transaction
successful. 5. Duplicate payment callbacks never create duplicate transactions. 6. Transfers never
inflate income/expense reports. 7. Budget calculations stay correct when transactions are edited or
deleted. 8. A recurring transaction is never generated twice for the same period.

## 19. UI/UX
Polished modern fintech (Revolut/Monzo/Linear feel): clean, minimal, strong typography, consistent
spacing, rounded cards, subtle shadows, smooth transitions, clear charts, accessible colours,
light & dark. Bottom tabs: Home, Transactions, Budgets, Reports, Profile. Prominent quick-add for
Expense, Income, UPI payment, Transfer. Must feel native on varied Android screen sizes.
**Project addition:** backend URL configurable from the login screen (done).
