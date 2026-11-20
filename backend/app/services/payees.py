"""Saved payees (personal / family), recent & frequent lists, usage tracking for UPI payments
and category suggestions.

* Personal payees are visible to their owner only; family payees to active members. Creating,
  editing or deleting a *family* payee needs ``MANAGE_SHARED_SETUP`` (owner/admin) — 403 for
  members/viewers, 404 for non-members.
* A VPA (UPI ID) is unique among live payees of one scope (the owner's personal payees, or one
  family's payees) → 409 ``payee_exists``.
* :meth:`PayeeService.touch` is the hook for UPI payments: it upserts the payee by VPA and bumps
  its usage counters without committing (the caller owns the transaction and has already
  authorised the payment).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError, ValidationFailedError
from app.core.permissions import FamilyPermission, require_family_access
from app.core.security import utcnow
from app.models.enums import RecordScope
from app.models.finance import Payee, TransactionCategory
from app.models.user import User
from app.repositories.categories import CategoryRepository
from app.repositories.payees import PayeeRepository
from app.repositories.system import AuditRepository
from app.schemas.categories import CategoryBriefOut
from app.schemas.payees import (
    CategorySuggestionOut,
    PayeeCreate,
    PayeeOut,
    PayeeUpdate,
    normalize_mobile,
    normalize_vpa,
)
from app.services.auth import ClientInfo
from app.utils.pagination import Page, PageParams, paginate

# Built-in category ids (stable uuid5 values seeded by migration 0002).
FOOD = uuid.UUID("97a3995f-d00d-5f96-8d0a-4fc98fe09b24")
GROCERIES = uuid.UUID("70170c58-f9af-52dc-9dca-00e5ed7bfdbd")
ELECTRICITY = uuid.UUID("f7dc6fe8-c106-5a49-9424-5d13a45c95ab")
INTERNET = uuid.UUID("192541f1-812b-5e85-90da-ca687e027a11")
MOBILE_RECHARGE = uuid.UUID("fe3b7b5a-a171-548e-a978-d287c3d5870e")
TRANSPORTATION = uuid.UUID("fe4fe30c-ce49-5932-9de4-ff2c81bf4639")
FUEL = uuid.UUID("edcd3651-4859-5fc2-89f2-6d5a14ccbf20")
SHOPPING = uuid.UUID("dd347d22-3597-5276-a59d-798506c8b8ea")
HEALTHCARE = uuid.UUID("4f84fcb3-9e1c-5b6c-993d-a3818ff466e9")
EDUCATION = uuid.UUID("ce1efef2-64f3-5128-971d-ca41c668082f")
ENTERTAINMENT = uuid.UUID("bdd3ae6f-b1a3-5709-b0ab-ecba28cdd2f4")
TRAVEL = uuid.UUID("2d6c7cdb-1061-5d03-b26d-7af43e606c89")
INSURANCE = uuid.UUID("939489a9-9c3f-51fc-9300-cfb4c9af995e")
SUBSCRIPTIONS = uuid.UUID("faf36ccb-0819-5603-b6d8-f695e7d70c2c")
EMI_LOANS = uuid.UUID("cd734d78-5a67-5669-8dc0-5975dac11514")
PERSONAL_CARE = uuid.UUID("069b4dd4-e6bc-580e-822e-d28200b103cd")

# keyword → built-in category. Matching runs on lower-cased text where every run of
# non-alphanumerics is one space. Keywords in SHORT_KEYWORDS (and any of ≤ 3 characters) must
# be whole words ("vi" must not match "vivek", "jio" not "ajio"); longer ones also match inside
# words ("swiggyinstamart@…"). When several match, the longest keyword wins.
KEYWORD_CATEGORIES: dict[str, uuid.UUID] = {
    **dict.fromkeys(
        ["swiggy", "zomato", "restaurant", "cafe", "dominos", "mcdonalds", "kfc", "dhaba"], FOOD
    ),
    **dict.fromkeys(
        ["bigbasket", "blinkit", "zepto", "dmart", "grocery", "groceries", "jiomart", "kirana"],
        GROCERIES,
    ),
    **dict.fromkeys(
        ["uber", "ola", "rapido", "metro", "irctc", "redbus", "fastag", "parking"], TRANSPORTATION
    ),
    **dict.fromkeys(["petrol", "fuel", "hpcl", "iocl", "bpcl", "indianoil", "diesel"], FUEL),
    **dict.fromkeys(["jio", "airtel", "vi", "recharge", "vodafone", "bsnl"], MOBILE_RECHARGE),
    **dict.fromkeys(
        [
            "bescom",
            "tata power",
            "tatapower",
            "adani electricity",
            "adanielectricity",
            "msedcl",
            "electricity",
            "tneb",
            "bses",
        ],
        ELECTRICITY,
    ),
    **dict.fromkeys(["act fibernet", "actfibernet", "fibernet", "broadband", "hathway"], INTERNET),
    **dict.fromkeys(
        [
            "netflix",
            "spotify",
            "hotstar",
            "prime",
            "amazon prime",
            "amazonprime",
            "primevideo",
            "youtube premium",
        ],
        SUBSCRIPTIONS,
    ),
    **dict.fromkeys(["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa"], SHOPPING),
    **dict.fromkeys(
        ["apollo", "pharmacy", "hospital", "1mg", "medplus", "pharmeasy", "clinic"], HEALTHCARE
    ),
    **dict.fromkeys(["lic", "insurance", "policybazaar"], INSURANCE),
    **dict.fromkeys(["school", "college", "udemy", "coursera", "byjus", "tuition"], EDUCATION),
    **dict.fromkeys(["pvr", "inox", "bookmyshow", "cinema"], ENTERTAINMENT),
    **dict.fromkeys(
        ["makemytrip", "goibibo", "hotel", "airline", "airlines", "indigo", "cleartrip"], TRAVEL
    ),
    **dict.fromkeys(["emi", "loan", "bajaj finserv", "bajajfinserv"], EMI_LOANS),
    **dict.fromkeys(["salon", "spa", "parlour", "barber"], PERSONAL_CARE),
}
SHORT_KEYWORDS = frozenset({"ola", "uber", "jio", "vi", "lic", "emi", "spa", "pvr", "act", "kfc"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    if keyword in SHORT_KEYWORDS or len(keyword) <= 3:
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return re.compile(escaped)


_KEYWORD_PATTERNS = sorted(
    ((kw, _keyword_pattern(kw), cid) for kw, cid in KEYWORD_CATEGORIES.items()),
    key=lambda item: -len(item[0]),
)

PAYEE_DEFAULT_CONFIDENCE = 0.6
KEYWORD_CONFIDENCE = 0.35


def keyword_category(*texts: str | None) -> uuid.UUID | None:
    """Built-in category for the first (longest) keyword found in any of ``texts``."""
    normalized = " ".join(_NON_ALNUM.sub(" ", t.lower()).strip() for t in texts if t)
    if not normalized:
        return None
    for _, pattern, category_id in _KEYWORD_PATTERNS:
        if pattern.search(normalized):
            return category_id
    return None


def _brief(category: TransactionCategory) -> CategoryBriefOut:
    return CategoryBriefOut.model_validate(category)


class PayeeExistsError(ConflictError):
    code = "payee_exists"
    message = "A payee with this UPI ID already exists here"


class PayeeService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.payees = PayeeRepository(session)
        self.categories = CategoryRepository(session)
        self.audit = AuditRepository(session)

    # ------------------------------------------------------------------ helpers

    def _audit(self, action: str, user: User, client: ClientInfo, payee: Payee) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="payee",
            entity_id=payee.id,
            family_id=payee.family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
        )

    async def _context(self, user: User, family_id: uuid.UUID | None) -> None:
        if family_id is not None:
            await require_family_access(self.db, user, family_id)

    async def _check_category(
        self, user: User, family_id: uuid.UUID | None, category_id: uuid.UUID
    ) -> None:
        if await self.categories.get_in_context(user.id, family_id, category_id) is None:
            raise ValidationFailedError(
                "default_category_id must be an active category usable in this scope",
                code="invalid_category",
            )

    async def _ensure_vpa_free(
        self,
        owner_id: uuid.UUID,
        family_id: uuid.UUID | None,
        vpa: str,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        await self.payees.lock_vpa(family_id, owner_id, vpa)
        existing = await self.payees.find_by_vpa(owner_id, family_id, vpa, exclude_id=exclude_id)
        if existing is not None:
            raise PayeeExistsError(details={"payee_id": str(existing.id)})

    async def _load_for_write(self, user: User, payee_id: uuid.UUID) -> Payee:
        payee = await self.payees.get_visible(user.id, payee_id, for_update=True)
        if payee is None:
            raise NotFoundError("Payee not found")
        if payee.family_id is not None:
            await require_family_access(
                self.db, user, payee.family_id, FamilyPermission.MANAGE_SHARED_SETUP
            )
        return payee

    async def _commit(self) -> None:
        try:
            await self.db.commit()
        except IntegrityError as exc:  # pragma: no cover - guarded by the advisory lock
            await self.db.rollback()
            raise PayeeExistsError() from exc

    # ------------------------------------------------------------------ reads

    async def list_payees(
        self, user: User, family_id: uuid.UUID | None, q: str | None, page: PageParams
    ) -> Page[PayeeOut]:
        await self._context(user, family_id)
        stmt = self.payees.search_stmt(user.id, family_id, (q or "").strip() or None)
        rows, total = await paginate(self.db, stmt, page)
        return Page.build([PayeeOut.model_validate(r) for r in rows], total, page)

    async def recent(self, user: User, family_id: uuid.UUID | None, limit: int) -> list[PayeeOut]:
        await self._context(user, family_id)
        return [
            PayeeOut.model_validate(p) for p in await self.payees.recent(user.id, family_id, limit)
        ]

    async def frequent(self, user: User, family_id: uuid.UUID | None, limit: int) -> list[PayeeOut]:
        await self._context(user, family_id)
        rows = await self.payees.frequent(user.id, family_id, limit)
        return [PayeeOut.model_validate(p) for p in rows]

    async def get(self, user: User, payee_id: uuid.UUID) -> PayeeOut:
        payee = await self.payees.get_visible(user.id, payee_id)
        if payee is None:
            raise NotFoundError("Payee not found")
        return PayeeOut.model_validate(payee)

    # ------------------------------------------------------------------ writes

    async def create(self, user: User, data: PayeeCreate, client: ClientInfo) -> PayeeOut:
        family_id = data.family_id if data.scope is RecordScope.FAMILY else None
        if family_id is not None:
            await require_family_access(
                self.db, user, family_id, FamilyPermission.MANAGE_SHARED_SETUP
            )
        if data.default_category_id is not None:
            await self._check_category(user, family_id, data.default_category_id)
        if data.upi_id is not None:
            await self._ensure_vpa_free(user.id, family_id, data.upi_id)
        payee = Payee(
            owner_user_id=user.id,
            family_id=family_id,
            scope=data.scope,
            name=data.name,
            upi_id=data.upi_id,
            mobile=data.mobile,
            merchant_code=data.merchant_code,
            default_category_id=data.default_category_id,
        )
        self.payees.add(payee)
        await self.db.flush()
        self._audit("payee.created", user, client, payee)
        await self._commit()
        await self.db.refresh(payee)
        return PayeeOut.model_validate(payee)

    async def update(
        self, user: User, payee_id: uuid.UUID, data: PayeeUpdate, client: ClientInfo
    ) -> PayeeOut:
        payee = await self._load_for_write(user, payee_id)
        fields = data.model_fields_set
        if "default_category_id" in fields and data.default_category_id is not None:
            await self._check_category(user, payee.family_id, data.default_category_id)
        if "upi_id" in fields and data.upi_id is not None and data.upi_id != payee.upi_id:
            await self._ensure_vpa_free(
                payee.owner_user_id, payee.family_id, data.upi_id, exclude_id=payee.id
            )
        for field in ("name", "upi_id", "mobile", "merchant_code", "default_category_id"):
            if field in fields:
                setattr(payee, field, getattr(data, field))
        if fields:
            self._audit("payee.updated", user, client, payee)
            await self._commit()
            await self.db.refresh(payee)
        return PayeeOut.model_validate(payee)

    async def delete(self, user: User, payee_id: uuid.UUID, client: ClientInfo) -> None:
        payee = await self._load_for_write(user, payee_id)
        payee.deleted_at = utcnow()
        self._audit("payee.deleted", user, client, payee)
        await self.db.commit()

    async def touch(
        self,
        owner: User | uuid.UUID,
        scope: RecordScope,
        family_id: uuid.UUID | None,
        upi_id: str,
        name: str | None = None,
        *,
        mobile: str | None = None,
        merchant_code: str | None = None,
        default_category_id: uuid.UUID | None = None,
        used_at: datetime | None = None,
    ) -> Payee:
        """Upsert the payee for ``upi_id`` in a scope and record one use. **Does not commit.**

        For the UPI flow, after the caller has authorised the payment (e.g.
        ``require_family_access(..., CREATE_RECORDS)`` for a family payment). Lookup is by VPA
        within the scope (the owner's personal payees, or the family's payees whoever created
        them). An existing payee gets ``use_count + 1`` and ``last_used_at``; blanks
        (mobile/merchant code/default category) are filled in but the user's chosen name is
        kept. A new payee is created with ``use_count = 1`` (``name`` defaults to the VPA's
        handle). Invalid optional values (mobile, merchant code, a category not usable in the
        scope) are ignored; an invalid VPA raises 400 ``invalid_upi_id``.
        """
        owner_id = owner if isinstance(owner, uuid.UUID) else owner.id
        if (scope is RecordScope.FAMILY) != (family_id is not None):
            raise ValueError("family_id must be set exactly when scope is family")
        try:
            vpa = normalize_vpa(upi_id)
        except ValueError as exc:
            raise BadRequestError(str(exc), code="invalid_upi_id") from exc
        try:
            mobile = normalize_mobile(mobile) if mobile else None
        except ValueError:
            mobile = None
        if merchant_code is not None and not re.fullmatch(r"\d{4}", merchant_code):
            merchant_code = None
        if default_category_id is not None and (
            await self.categories.get_in_context(owner_id, family_id, default_category_id) is None
        ):
            default_category_id = None
        now = used_at or utcnow()
        clean_name = (name or "").strip()[:120] or vpa.split("@", 1)[0][:120]

        await self.payees.lock_vpa(family_id, owner_id, vpa)
        payee = await self.payees.find_by_vpa(owner_id, family_id, vpa, for_update=True)
        if payee is None:
            payee = Payee(
                owner_user_id=owner_id,
                family_id=family_id,
                scope=scope,
                name=clean_name,
                upi_id=vpa,
                mobile=mobile,
                merchant_code=merchant_code,
                default_category_id=default_category_id,
                use_count=1,
                last_used_at=now,
            )
            self.payees.add(payee)
        else:
            payee.use_count += 1
            if payee.last_used_at is None or now > payee.last_used_at:
                payee.last_used_at = now
            payee.mobile = payee.mobile or mobile
            payee.merchant_code = payee.merchant_code or merchant_code
            payee.default_category_id = payee.default_category_id or default_category_id
        await self.db.flush()
        return payee

    # ------------------------------------------------------------------ suggestions

    async def suggest_category(
        self,
        user: User,
        family_id: uuid.UUID | None,
        *,
        upi_id: str | None = None,
        payee_id: uuid.UUID | None = None,
        merchant: str | None = None,
        q: str | None = None,
    ) -> CategorySuggestionOut:
        """Best expense category for a payment, usable in the personal context or in
        ``family_id``:

        1. ``history`` — most used category on the caller's visible completed expenses with this
           payee / VPA / merchant (confidence = uses / (matches + 1));
        2. ``payee_default`` — the payee's ``default_category_id`` (0.6);
        3. ``keyword`` — a built-in category from keywords in the payee name / VPA / merchant /
           ``q`` (0.35);
        4. ``none``.
        """
        await self._context(user, family_id)
        vpa: str | None = None
        if upi_id:
            try:
                vpa = normalize_vpa(upi_id)
            except ValueError as exc:
                raise ValidationFailedError(str(exc), code="invalid_upi_id") from exc
        merchant = (merchant or "").strip() or None
        payees: list[Payee] = []
        if payee_id is not None:
            payee = await self.payees.get_visible(user.id, payee_id)
            if payee is None:
                raise NotFoundError("Payee not found")
            payees.append(payee)
            vpa = vpa or payee.upi_id
        if vpa:
            in_context = await self.payees.find_by_vpa(user.id, family_id, vpa)
            if in_context is not None and all(p.id != in_context.id for p in payees):
                payees.append(in_context)

        counts = await self.payees.history_category_counts(
            user.id, family_id, payee_ids=[p.id for p in payees], vpa=vpa, merchant=merchant
        )
        if counts:
            category_id, uses = counts[0]
            total = sum(n for _, n in counts)
            top = await self.categories.get_live(category_id)
            if top is not None:  # matched through a join on live categories
                return CategorySuggestionOut(
                    category_id=top.id,
                    source="history",
                    confidence=round(uses / (total + 1), 2),
                    category=_brief(top),
                )

        for payee in payees:
            if payee.default_category_id is None:
                continue
            category = await self.categories.get_in_context(
                user.id, family_id, payee.default_category_id
            )
            if category is not None:
                return CategorySuggestionOut(
                    category_id=category.id,
                    source="payee_default",
                    confidence=PAYEE_DEFAULT_CONFIDENCE,
                    category=_brief(category),
                )

        keyword_id = keyword_category(*(p.name for p in payees), vpa, merchant, q)
        if keyword_id is not None:
            category = await self.categories.get_in_context(user.id, family_id, keyword_id)
            if category is not None:
                return CategorySuggestionOut(
                    category_id=category.id,
                    source="keyword",
                    confidence=KEYWORD_CONFIDENCE,
                    category=_brief(category),
                )
        return CategorySuggestionOut(category_id=None, source="none", confidence=0.0, category=None)
