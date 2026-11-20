# Categories, payees, attachments, tags (`/api/v1`)

Errors `{"error":{"code","message","details?"}}`.

## Categories
`CategoryOut = {id, name, kind: "expense"|"income", ownership: "builtin"|"personal"|"family", family_id|null, parent_id|null, icon|null (Lucide name), color|null ("#RRGGBB"), sort_order, is_builtin, is_archived, created_at, updated_at, children: [CategoryOut]}`

- `GET /categories?family_id=&kind=expense|income&include_archived=false` → `[CategoryOut]` tree (top level + one level of children). No family_id = built-ins + my personal; with family_id = built-ins + that family's.
- `GET /categories/icons` → `[string]` (Lucide names) · `GET /categories/{id}?family_id=` → CategoryOut with `children` (archived ones included; for a built-in parent, `family_id` lists that family's subcategories instead of your personal ones — membership required, else 404)
- `POST /categories` → 201. Body `{scope?: personal|family, family_id?, name (1–60), kind? (default expense), parent_id?, icon?, color?, sort_order?}`. Errors 422 `invalid_parent`, 409 `category_name_taken`, 403 `insufficient_family_role`.
- `PATCH /categories/{id}` `{name?, icon?|null, color?|null, sort_order?, parent_id?|null}` (built-ins → 403 `builtin_category`)
- `POST /categories/{id}/archive` · `/unarchive` → CategoryOut
- `DELETE /categories/{id}?reassign_to=<uuid>` → `{deleted_ids, reassigned_to, moved_transactions, moved_recurring, moved_budget_limits, removed_budget_limits}`; in use without reassign → 409 `category_in_use` (`details {transactions, recurring}`). Budget category limits move with `reassign_to` (merged into an existing limit on the target) or are removed without it — all in one DB transaction.
- Archived categories (and subcategories of an archived parent) can't be put on new transactions / UPI payments / recurring rules (422 `category_archived`); rows already using them stay editable.

## Payees
`PayeeOut = {id, scope, family_id|null, owner_user_id, name, upi_id|null, mobile|null, merchant_code|null, default_category_id|null, use_count, last_used_at|null, created_at, updated_at}`

- `GET /payees?family_id=&q=&limit=50&offset=0` → `Page[PayeeOut]` (q matches name / UPI ID / mobile)
- `GET /payees/recent?family_id=&limit=10` · `GET /payees/frequent?family_id=&limit=10` → `[PayeeOut]`
- `GET /payees/suggest-category?family_id=&upi_id=&payee_id=&merchant=&q=` → `{category_id|null, source: "history"|"payee_default"|"keyword"|"none", confidence, category: {id,name,kind,icon,color,parent_id}|null}`
- `GET /payees/{id}` · `POST /payees` (201; `{scope?, family_id?, name (1–120), upi_id?, mobile?, merchant_code? (4 digits), default_category_id?}`; 409 `payee_exists` with `details.payee_id`) · `PATCH /payees/{id}` · `DELETE /payees/{id}` → 204
- Server-side `PayeeService(db).touch(owner, scope, family_id, upi_id, name=None, *, mobile=None, merchant_code=None, default_category_id=None, used_at=None)` upserts by VPA and bumps usage (no commit; caller authorises first).

## Receipt attachments
`AttachmentOut = {id, transaction_id, kind: "image"|"pdf", content_type, size_bytes, uploaded_by_user_id, created_at, download_url}`

- `POST /transactions/{tid}/attachments` multipart `file` (JPEG/PNG/WebP/PDF ≤5 MB; images re-encoded to JPEG, EXIF stripped) → 201. Errors 413, 400 `unsupported_file_type|invalid_file|image_too_large|unsafe_pdf|pdf_encrypted`, 409 `attachment_limit` (10), 429, 403 viewer.
- `GET /transactions/{tid}/attachments` → `[AttachmentOut]` · `GET …/{aid}/download` (send bearer header) · `DELETE …/{aid}` → 204
- Mobile picker must send JPEG (no HEIC).

## Tags
- `GET /tags?family_id=&q=&limit=20` → `[{name, count}]` most used first.
