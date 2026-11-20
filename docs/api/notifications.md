# Notifications API (`/api/v1/notifications`)

- `GET /notifications?limit=50&offset=0&unread_only=false&category=<cat>` → `Page[NotificationOut]`, newest first.
  `NotificationOut = {id, category, title, body, data: {…ids, category}, url, family_id|null, read, read_at|null, created_at, scheduled_for, push_status: pending|sent|failed|skipped}`
- `GET /notifications/unread-count` → `{"count": int}`
- `POST /notifications/read-all[?category=]` → `{"updated": int}` · `POST /notifications/{id}/read` → NotificationOut
- `DELETE /notifications/{id}` → 204 (dismiss)
- `GET /notifications/preferences` → `[{category, push_enabled, in_app_enabled}]` (all 11 categories)
- `PUT /notifications/preferences` body `[{category, push_enabled, in_app_enabled}]` → full list
- `POST /notifications/devices` `{token: "ExponentPushToken[...]", platform: "ios"|"android"}` → `{id, platform, token_hint, created_at, last_seen_at}`. Call after every sign-in / token change. Rate-limited per user (default 20/hour → 429 `rate_limited` with `Retry-After`). A token already registered by another account is re-bound to this session.
- `DELETE /notifications/devices/{token}` (URL-encode the brackets) → 204; 404 if not one of my tokens. (The token is redacted from server logs.)

Categories: `budget_alert, bill_upcoming, recurring_reminder, payment_confirmation, upi_pending, family_invitation, family_member_joined, family_shared_expense, savings_milestone, unusual_spending, system`.

Push message: `{to, title, body, sound: "default", channelId: "default", priority: "high", data: {…, notification_id, category, url, family_id?}}` — **the app must create an Android notification channel with id `default`.**

## Deep links (`data.url` / `url`) → mobile routes
| Category | URL |
|---|---|
| budget_alert | `familybudget://budget/{budget_id}` (fallback `familybudget://budgets`) |
| bill_upcoming, recurring_reminder | `familybudget://recurring/{recurring_id}` (fallback `familybudget://recurring`) |
| payment_confirmation, upi_pending | `familybudget://upi/{upi_attempt_id}`, else `familybudget://transaction/{transaction_id}` (fallback `familybudget://transactions`) |
| family_invitation | `familybudget://family/invitations/{invitation_id}` (fallback `familybudget://family/invitations`) |
| family_member_joined | `familybudget://family/{family_id}` (fallback `familybudget://family`) |
| family_shared_expense | `familybudget://transaction/{transaction_id}`, else `familybudget://family/{family_id}` |
| savings_milestone | `familybudget://savings/{goal_id}` (fallback `familybudget://savings`) |
| unusual_spending | `familybudget://transactions?date=YYYY-MM-DD` |
| other | `familybudget://notifications` |
