# Families API (`/api/v1/families`)

- FamilyDetail = `{id, name, currency, has_image, my_role, created_by_user_id, created_at, updated_at, members: [{user_id, full_name, email, role, has_avatar, joined_at}]}`
- FamilySummary = `{id, name, currency, has_image, my_role, member_count, created_at}`
- Invitation = `{id, family_id, email, role, status, invited_by_user_id, expires_at, created_at}`

| Method & path | Body | Response |
|---|---|---|
| POST `` | `{name, currency?}` | 201 FamilyDetail |
| GET `` | – | `[FamilySummary]` |
| GET / PATCH / DELETE `/{id}` | PATCH `{name?, currency?}` | FamilyDetail / 204 |
| PUT / GET / DELETE `/{id}/image` | multipart `file` | FamilyDetail / image/jpeg / 204 |
| POST `/{id}/invitations` | `{email, role}` | 201 Invitation + `{token, deep_link}` |
| GET `/{id}/invitations` · DELETE `/{id}/invitations/{inv}` | – | `[Invitation]` / 204 |
| GET `/invitations/incoming` | – | `[{id, family_id, family_name, role, invited_by_name, expires_at, created_at}]` |
| POST `/invitations/preview` | `{token}` | `{id, family_id, family_name, role, invited_by_name, status, expires_at, already_member}` |
| POST `/invitations/accept` · `/invitations/reject` | `{token}` | FamilySummary / 204 |
| POST `/invitations/{inv}/accept` · `/invitations/{inv}/reject` | – | FamilySummary / 204 |
| PATCH / DELETE `/{id}/members/{user_id}` | `{role}` | FamilyDetail / 204 |
| POST `/{id}/transfer-ownership` | `{user_id}` | FamilyDetail |
| POST `/{id}/leave` | – | 204 |

Roles: owner > admin > member > viewer. Viewer is read-only; member creates records and edits their own; admin manages members (not the owner), budgets, shared setup and all records; owner can also transfer ownership and delete the family.
