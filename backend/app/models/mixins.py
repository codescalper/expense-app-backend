"""Ownership columns shared by every personal-or-family record (Phases 4-7).

A *scoped* row has an explicit creator/owner (``owner_user_id``) and a scope:

* ``scope = 'personal'`` → ``family_id IS NULL``; only ``owner_user_id`` may see it.
* ``scope = 'family'``   → ``family_id IS NOT NULL``; every active member of that family may see
  it, and editing follows the role rules in :mod:`app.core.permissions`.

``scope_family_check()`` adds the CHECK constraint that keeps the two columns consistent. Build
read queries with :class:`app.core.permissions.ScopeFilter` — never filter by hand.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums import RECORD_SCOPE, RecordScope

SCOPE_FAMILY_SQL = (
    "(scope = 'personal' AND family_id IS NULL) OR (scope = 'family' AND family_id IS NOT NULL)"
)


def scope_family_check() -> CheckConstraint:
    """``ck_<table>_scope_family``: personal rows have no family, family rows have one."""
    return CheckConstraint(SCOPE_FAMILY_SQL, name="scope_family")


class ScopedOwnershipMixin:
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    family_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id"))
    scope: Mapped[RecordScope] = mapped_column(
        RECORD_SCOPE, default=RecordScope.PERSONAL, server_default=RecordScope.PERSONAL.value
    )
