"""Categories (+ subcategories), the icon allowlist and tag autocomplete."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from app.models.enums import CategoryKind, RecordScope
from app.schemas.common import RequestModel, ResponseModel

# Lucide icon names (https://lucide.dev/icons). The first block is every icon the built-in
# categories use (migration 0002); the rest are common personal-finance icons.
CATEGORY_ICONS: tuple[str, ...] = (
    # built-in categories
    "utensils",
    "shopping-basket",
    "home",
    "plug",
    "zap",
    "wifi",
    "smartphone",
    "bus",
    "fuel",
    "shopping-bag",
    "heart-pulse",
    "graduation-cap",
    "film",
    "plane",
    "shield",
    "repeat",
    "landmark",
    "sparkles",
    "users",
    "circle-ellipsis",
    "briefcase",
    "store",
    "percent",
    "gift",
    "rotate-ccw",
    "circle-plus",
    # common finance icons
    "house",
    "wallet",
    "credit-card",
    "banknote",
    "piggy-bank",
    "coins",
    "hand-coins",
    "handshake",
    "receipt",
    "indian-rupee",
    "trending-up",
    "calculator",
    "tag",
    "car",
    "car-taxi-front",
    "bike",
    "train-front",
    "ship",
    "truck",
    "coffee",
    "pizza",
    "beer",
    "wine",
    "cake",
    "apple",
    "baby",
    "dog",
    "paw-print",
    "gamepad-2",
    "music",
    "tv",
    "book-open",
    "school",
    "laptop",
    "shirt",
    "scissors",
    "dumbbell",
    "stethoscope",
    "pill",
    "hospital",
    "building-2",
    "wrench",
    "droplet",
    "flame",
    "phone",
    "heart",
    "ticket",
    "umbrella",
    "sofa",
    "key",
    "package",
    "gem",
    "palette",
    "flower-2",
    "tree-palm",
)
_ICON_SET = frozenset(CATEGORY_ICONS)


def _check_icon(value: str) -> str:
    if value not in _ICON_SET:
        raise ValueError("Unknown icon (see GET /categories/icons)")
    return value


def _upper(value: str) -> str:
    return value.upper()


CategoryName = Annotated[str, StringConstraints(min_length=1, max_length=60)]
IconName = Annotated[str, StringConstraints(max_length=40), AfterValidator(_check_icon)]
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$"), AfterValidator(_upper)]
SortOrder = Annotated[int, Field(ge=0, le=32767)]


def check_scope_family(scope: RecordScope, family_id: uuid.UUID | None) -> None:
    """``family`` needs a ``family_id``; ``personal`` must not have one."""
    if scope is RecordScope.FAMILY and family_id is None:
        raise ValueError("family_id is required when scope is 'family'")
    if scope is RecordScope.PERSONAL and family_id is not None:
        raise ValueError("family_id must be omitted when scope is 'personal'")


# --- requests --------------------------------------------------------------------------


class CategoryCreate(RequestModel):
    scope: RecordScope = RecordScope.PERSONAL
    family_id: uuid.UUID | None = None
    name: CategoryName
    kind: CategoryKind = CategoryKind.EXPENSE
    parent_id: uuid.UUID | None = None
    icon: IconName | None = None
    color: HexColor | None = None
    sort_order: SortOrder = 0

    @model_validator(mode="after")
    def _scope(self) -> CategoryCreate:
        check_scope_family(self.scope, self.family_id)
        return self


class CategoryUpdate(RequestModel):
    """Only the fields sent are changed. ``icon``/``color``/``parent_id`` may be ``null``
    (clear / move to top level); ``name`` and ``sort_order`` may not."""

    name: CategoryName | None = None
    icon: IconName | None = None
    color: HexColor | None = None
    sort_order: SortOrder | None = None
    parent_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _not_null(self) -> CategoryUpdate:
        for field in ("name", "sort_order"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


# --- responses -------------------------------------------------------------------------


CategoryOwnership = Literal["builtin", "personal", "family"]


class CategoryBriefOut(ResponseModel):
    id: uuid.UUID
    name: str
    kind: CategoryKind
    icon: str | None
    color: str | None
    parent_id: uuid.UUID | None


class CategoryOut(ResponseModel):
    id: uuid.UUID
    name: str
    kind: CategoryKind
    ownership: CategoryOwnership
    family_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    icon: str | None
    color: str | None
    sort_order: int
    is_builtin: bool
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    children: list[CategoryOut] = Field(default_factory=list)


class CategoryDeleteOut(ResponseModel):
    """``moved_budget_limits``: budget category limits repointed to (or merged into an existing
    limit for) ``reassigned_to``; ``removed_budget_limits``: limits dropped because the category
    was deleted without a reassignment target."""

    deleted_ids: list[uuid.UUID]
    reassigned_to: uuid.UUID | None
    moved_transactions: int
    moved_recurring: int
    moved_budget_limits: int = 0
    removed_budget_limits: int = 0


class TagCountOut(ResponseModel):
    name: str
    count: int
