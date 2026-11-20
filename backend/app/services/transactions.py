"""Transactions (spec §5): create, update, soft delete/restore, list, summary and CSV export.

Rules enforced here (on top of the DB constraints):

* **Scope.** A transaction is personal (only its owner sees it) or family (active members see
  it). Creating a family row needs ``CREATE_RECORDS`` (viewers 403, non-members 404). Editing
  follows :func:`app.core.permissions.require_record_write` (members: own rows only;
  owner/admin: any row; viewers: none). A row can never move between scopes.
* **References** must be visible to the caller *and* belong to the transaction's scope: a
  personal transaction uses the owner's personal accounts/payees, a family one that family's.
  Invisible → 404; visible but wrong scope → 422.
* **Shape by type.** transfer: two different accounts, no category; adjustment: an account and
  ``adjustment_sign`` ±1, no category; expense/refund: optional *expense* category;
  income: optional *income* category.
* **Money** comes from :mod:`app.services.ledger` — never recomputed here.
* **Idempotency.** ``(owner_user_id, idempotency_key)`` is unique; a repeat returns the
  original row (``created=False``), including under a concurrent race (savepoint + re-read).
* Every create/update/delete/restore/status change is audited with ``family_id`` set.

Wave-3 integration points (UPI, recurring) — both run inside the *caller's* DB transaction and
never commit:

* :meth:`TransactionService.create_internal` — same validations as the API, plus
  ``source`` / ``status`` / ``verification`` / ``idempotency_key`` / ``upi_attempt_id`` /
  ``external_ref`` / ``recurring_id`` + ``period_key``. Returns ``(transaction, created)``.
* :meth:`TransactionService.mark_status` — move a row to completed/failed/cancelled/pending
  with an optional verification level and external reference.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.permissions import (
    FamilyPermission,
    ScopeFilter,
    can_modify_record,
    get_active_membership,
    list_user_family_roles,
    require_family_access,
    require_record_write,
    role_has,
)
from app.core.security import utcnow
from app.models.enums import (
    CategoryKind,
    FamilyRole,
    NotificationCategory,
    RecordScope,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    VerificationStatus,
)
from app.models.finance import Account, Transaction
from app.models.user import User
from app.repositories.accounts import AccountRepository
from app.repositories.system import AuditRepository
from app.repositories.transactions import TransactionRepository
from app.schemas.accounts import money_str
from app.schemas.transactions import (
    AccountRef,
    CategoryRef,
    PayeeRef,
    TransactionCreate,
    TransactionFilters,
    TransactionOut,
    TransactionSummaryOut,
    TransactionUpdate,
    UserRef,
)
from app.services import ledger
from app.services.auth import ClientInfo
from app.services.notify import notify_family_members
from app.utils.csv_safety import neutralize_formula
from app.utils.pagination import Page, PageParams

IDEMPOTENCY_CONSTRAINT = "uq_transactions_owner_user_id_idempotency_key"
RECURRING_PERIOD_CONSTRAINT = "uq_transactions_recurring_id_period_key"
UPI_ATTEMPT_CONSTRAINT = "uq_transactions_upi_attempt_id"

# Expected category kind per type (None = a category is not allowed).
CATEGORY_KIND_FOR: dict[TransactionType, CategoryKind | None] = {
    TransactionType.EXPENSE: CategoryKind.EXPENSE,
    TransactionType.REFUND: CategoryKind.EXPENSE,
    TransactionType.INCOME: CategoryKind.INCOME,
    TransactionType.TRANSFER: None,
    TransactionType.ADJUSTMENT: None,
}

CSV_COLUMNS = (
    "id",
    "date",
    "time",
    "type",
    "status",
    "amount",
    "currency",
    "category",
    "account",
    "to_account",
    "payee",
    "merchant",
    "description",
    "payment_method",
    "tags",
    "notes",
    "scope",
    "created_by",
    "source",
)

AUDITED_FIELDS = (
    "type",
    "status",
    "amount",
    "currency",
    "account_id",
    "to_account_id",
    "category_id",
    "payee_id",
    "merchant",
    "description",
    "notes",
    "payment_method",
    "occurred_at",
    "adjustment_sign",
    "verification",
    "external_ref",
)


class InvalidTransactionError(ValidationFailedError):
    """422 with a specific ``code`` (e.g. ``category_kind_mismatch``)."""


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection (see :mod:`app.utils.csv_safety`)."""
    return neutralize_formula(value)


def user_tz(user: User) -> tzinfo:
    try:
        return ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return money_str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # enums
        return value.value
    return value


def _constraint_name(exc: IntegrityError) -> str:
    orig: Any = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return str(orig)


@dataclass(frozen=True)
class _Shape:
    """The fields that drive validation (merged state for updates)."""

    scope: RecordScope
    family_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    type: TransactionType
    currency: str | None
    account_id: uuid.UUID | None
    to_account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    adjustment_sign: int | None


class TransactionService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.txs = TransactionRepository(session)
        self.accounts = AccountRepository(session)
        self.audit = AuditRepository(session)

    # ================================================================== validation

    async def _validate(
        self,
        actor: User,
        shape: _Shape,
        *,
        new_account_ids: set[uuid.UUID],
        new_category: bool,
    ) -> str:
        """Check type shape and references; returns the effective currency.

        ``new_account_ids``: accounts being newly attached (must be active).
        ``new_category``: the category is being newly attached (must not be archived, nor a
        subcategory of an archived parent). Rows that already use an account / category that
        was archived later stay editable without having to change it."""
        t = shape.type
        if t is TransactionType.TRANSFER:
            if shape.account_id is None or shape.to_account_id is None:
                raise InvalidTransactionError(
                    "A transfer needs account_id and to_account_id", code="transfer_accounts"
                )
            if shape.account_id == shape.to_account_id:
                raise InvalidTransactionError(
                    "Transfer accounts must be different", code="transfer_same_account"
                )
        elif shape.to_account_id is not None:
            raise InvalidTransactionError(
                "to_account_id is only allowed for transfers", code="to_account_not_allowed"
            )
        if t is TransactionType.ADJUSTMENT:
            if shape.account_id is None:
                raise InvalidTransactionError(
                    "An adjustment needs account_id", code="adjustment_account_required"
                )
            if shape.adjustment_sign not in (-1, 1):
                raise InvalidTransactionError(
                    "An adjustment needs adjustment_sign of 1 or -1",
                    code="adjustment_sign_required",
                )
        elif shape.adjustment_sign is not None:
            raise InvalidTransactionError(
                "adjustment_sign is only allowed for adjustments",
                code="adjustment_sign_not_allowed",
            )

        currency = shape.currency
        for account_id in (shape.account_id, shape.to_account_id):
            if account_id is None:
                continue
            account = await self._account_for(actor, shape, account_id)
            if account_id in new_account_ids and not account.is_active:
                raise InvalidTransactionError("This account is archived", code="account_inactive")
            if currency is None:
                currency = account.currency
            elif account.currency != currency:
                raise InvalidTransactionError(
                    "Currency must match the account's currency", code="currency_mismatch"
                )

        if shape.category_id is not None:
            expected = CATEGORY_KIND_FOR[t]
            if expected is None:
                raise InvalidTransactionError(
                    f"A {t.value} has no category", code="category_not_allowed"
                )
            category = await self.txs.get_usable_category(
                actor.id,
                shape.category_id,
                scope=shape.scope,
                family_id=shape.family_id,
                owner_user_id=shape.owner_user_id,
            )
            if category is None:
                raise NotFoundError("Category not found")
            if category.kind is not expected:
                raise InvalidTransactionError(
                    f"A {t.value} needs an {expected.value} category",
                    code="category_kind_mismatch",
                )
            if new_category and await self.txs.category_archived(category):
                raise InvalidTransactionError("This category is archived", code="category_archived")

        if shape.payee_id is not None:
            payee = await self.txs.get_visible_payee(actor.id, shape.payee_id)
            if payee is None:
                raise NotFoundError("Payee not found")
            if not self._same_scope(payee.scope, payee.family_id, payee.owner_user_id, shape):
                raise InvalidTransactionError(
                    "The payee belongs to a different scope", code="payee_scope_mismatch"
                )
        return currency or "INR"

    @staticmethod
    def _same_scope(
        scope: RecordScope, family_id: uuid.UUID | None, owner_id: uuid.UUID, shape: _Shape
    ) -> bool:
        if scope is not shape.scope:
            return False
        if scope is RecordScope.FAMILY:
            return family_id == shape.family_id
        return owner_id == shape.owner_user_id

    async def _account_for(self, actor: User, shape: _Shape, account_id: uuid.UUID) -> Account:
        account = await self.accounts.get_visible(actor.id, account_id)
        if account is None:
            raise NotFoundError("Account not found")
        if not self._same_scope(account.scope, account.family_id, account.owner_user_id, shape):
            raise InvalidTransactionError(
                "The account belongs to a different scope than the transaction",
                code="account_scope_mismatch",
            )
        return account

    @staticmethod
    def _as_aware(value: datetime | None, tz: tzinfo) -> datetime:
        if value is None:
            return utcnow()
        return value if value.tzinfo is not None else value.replace(tzinfo=tz)

    # ================================================================== audit & notify

    def _audit(
        self, action: str, actor: User, tx: Transaction, client: ClientInfo | None, **details: Any
    ) -> None:
        base = {
            "type": tx.type.value,
            "amount": money_str(tx.amount),
            "currency": tx.currency,
            "category_id": str(tx.category_id) if tx.category_id else None,
            "status": tx.status.value,
            "scope": tx.scope.value,
        }
        self.audit.record(
            action,
            actor_user_id=actor.id,
            entity_type="transaction",
            entity_id=tx.id,
            family_id=tx.family_id,
            ip_address=client.ip if client else None,
            user_agent=client.user_agent if client else None,
            details={**base, **details},
        )

    async def _notify_shared_expense(self, actor: User, tx: Transaction) -> None:
        """Tell the other family members about a completed family expense (deduped)."""
        if not (
            tx.scope is RecordScope.FAMILY
            and tx.family_id is not None
            and tx.type is TransactionType.EXPENSE
            and tx.status is TransactionStatus.COMPLETED
        ):
            return
        what = tx.description or tx.merchant
        body = f"{actor.full_name} added an expense of {tx.currency} {money_str(tx.amount)}"
        if what:
            body += f": {what}"
        await notify_family_members(
            self.db,
            family_id=tx.family_id,
            category=NotificationCategory.FAMILY_SHARED_EXPENSE,
            title="New family expense",
            body=body,
            dedupe_key=f"tx:{tx.id}:created",
            exclude_user_id=tx.owner_user_id,
            data={"transaction_id": str(tx.id), "family_id": str(tx.family_id)},
        )

    # ================================================================== create

    async def create_internal(
        self,
        actor: User,
        data: TransactionCreate,
        *,
        source: TransactionSource = TransactionSource.MANUAL,
        status: TransactionStatus | None = None,
        verification: VerificationStatus = VerificationStatus.UNVERIFIED,
        idempotency_key: str | None = None,
        upi_attempt_id: uuid.UUID | None = None,
        external_ref: str | None = None,
        recurring_id: uuid.UUID | None = None,
        period_key: str | None = None,
        client: ClientInfo | None = None,
    ) -> tuple[Transaction, bool]:
        """Validate and insert a transaction **without committing** (caller owns the commit).

        * ``actor`` becomes ``owner_user_id`` (the creator); family scope needs
          ``CREATE_RECORDS`` in ``data.family_id`` (404 non-member, 403 viewer).
        * ``status`` overrides ``data.status`` (e.g. ``PENDING`` for a UPI hand-off or a
          generated recurring bill); ``verification``/``source`` are stored as given.
        * ``idempotency_key`` (falls back to ``data.idempotency_key``): if the actor already
          has a row with that key — even one created concurrently — that row is returned with
          ``created=False`` and nothing else happens.
        * ``recurring_id`` + ``period_key`` must be given together; a duplicate period raises
          ``ConflictError(code="recurring_period_exists")``; a second row for the same
          ``upi_attempt_id`` raises ``ConflictError(code="upi_attempt_already_linked")``.
          The failed insert is rolled back to a savepoint, so the caller's transaction stays
          usable.
        * On creation: tags stored, ``transaction.created`` audited, and other family members
          notified of a *completed* family expense.

        Returns ``(transaction, created)``.
        """
        if (recurring_id is None) != (period_key is None):
            raise ValueError("recurring_id and period_key must be given together")
        key = idempotency_key or data.idempotency_key
        if data.scope is RecordScope.FAMILY and data.family_id is not None:
            await require_family_access(
                self.db, actor, data.family_id, FamilyPermission.CREATE_RECORDS
            )
        if key is not None:
            existing = await self.txs.get_by_idempotency_key(actor.id, key)
            if existing is not None:
                return await self._replay(actor, existing, data), False

        shape = _Shape(
            scope=data.scope,
            family_id=data.family_id,
            owner_user_id=actor.id,
            type=data.type,
            currency=data.currency,
            account_id=data.account_id,
            to_account_id=data.to_account_id,
            category_id=data.category_id,
            payee_id=data.payee_id,
            adjustment_sign=data.adjustment_sign,
        )
        new_accounts = {a for a in (data.account_id, data.to_account_id) if a is not None}
        currency = await self._validate(
            actor, shape, new_account_ids=new_accounts, new_category=data.category_id is not None
        )

        tx = Transaction(
            owner_user_id=actor.id,
            scope=data.scope,
            family_id=data.family_id,
            type=data.type,
            status=status or TransactionStatus(data.status),
            source=source,
            verification=verification,
            amount=data.amount,
            currency=currency,
            account_id=data.account_id,
            to_account_id=data.to_account_id,
            category_id=data.category_id,
            payee_id=data.payee_id,
            merchant=data.merchant,
            description=data.description,
            notes=data.notes,
            payment_method=data.payment_method,
            occurred_at=self._as_aware(data.occurred_at, user_tz(actor)),
            adjustment_sign=data.adjustment_sign,
            idempotency_key=key,
            upi_attempt_id=upi_attempt_id,
            external_ref=external_ref,
            recurring_id=recurring_id,
            period_key=period_key,
        )
        try:
            async with self.db.begin_nested():
                self.txs.add(tx)
                await self.db.flush()
        except IntegrityError as exc:
            name = _constraint_name(exc)
            if key is not None and IDEMPOTENCY_CONSTRAINT in name:
                existing = await self.txs.get_by_idempotency_key(actor.id, key)
                if existing is not None:
                    return await self._replay(actor, existing, data), False
            if RECURRING_PERIOD_CONSTRAINT in name:
                raise ConflictError(
                    "A transaction for this recurring period already exists",
                    code="recurring_period_exists",
                ) from exc
            if UPI_ATTEMPT_CONSTRAINT in name:
                raise ConflictError(
                    "This UPI payment already has a transaction",
                    code="upi_attempt_already_linked",
                ) from exc
            raise

        await self.txs.replace_tags(tx.id, data.tags)
        self._audit("transaction.created", actor, tx, client, source=source.value)
        await self._notify_shared_expense(actor, tx)
        return tx, True

    async def _replay(
        self, actor: User, existing: Transaction, data: TransactionCreate
    ) -> Transaction:
        """A repeated idempotency key returns the original row — but only for the same scope
        (else 409 ``idempotency_key_reused``) and only while the caller can still see it (a
        family row after leaving the family → 404), so a stale key never leaks a row."""
        family_id = data.family_id if data.scope is RecordScope.FAMILY else None
        if existing.scope is not data.scope or existing.family_id != family_id:
            raise ConflictError(
                "This idempotency key was already used for a different transaction",
                code="idempotency_key_reused",
            )
        if existing.scope is RecordScope.FAMILY and (
            existing.family_id is None
            or await get_active_membership(self.db, actor, existing.family_id) is None
        ):
            raise NotFoundError("Transaction not found")
        return existing

    async def create(
        self, actor: User, data: TransactionCreate, client: ClientInfo
    ) -> tuple[TransactionOut, bool]:
        """API create: ``(detail, created)``; ``created=False`` means an idempotent replay."""
        tx, created = await self.create_internal(actor, data, client=client)
        if created:
            await self.db.commit()
            await self.db.refresh(tx)
        return await self._one_out(actor, tx), created

    # ================================================================== status (internal)

    async def mark_status(
        self,
        tx_id: uuid.UUID,
        status: TransactionStatus,
        *,
        actor: User,
        verification: VerificationStatus | None = None,
        external_ref: str | None = None,
        client: ClientInfo | None = None,
        reason: str | None = None,
    ) -> Transaction:
        """Set ``status`` (and optionally ``verification`` / ``external_ref``) on a live row,
        **without committing** and **without permission checks** — the caller (UPI
        confirmation, recurring "mark paid") must already have authorised ``actor`` for it.

        Row-locked. Audited as ``transaction.status_changed`` with before/after. A family
        expense that becomes completed notifies the other members (deduped per transaction,
        so it never fires twice). Raises ``NotFoundError`` for unknown/deleted ids."""
        tx = await self.txs.get(tx_id, for_update=True)
        if tx is None or tx.deleted_at is not None:
            raise NotFoundError("Transaction not found")
        before = {"status": tx.status, "verification": tx.verification, "ref": tx.external_ref}
        tx.status = status
        if verification is not None:
            tx.verification = verification
        if external_ref is not None:
            tx.external_ref = external_ref
        after = {"status": tx.status, "verification": tx.verification, "ref": tx.external_ref}
        if before != after:
            await self.db.flush()
            self._audit(
                "transaction.status_changed",
                actor,
                tx,
                client,
                changes={
                    k: {"before": _jsonable(before[k]), "after": _jsonable(after[k])}
                    for k in before
                    if before[k] != after[k]
                },
                reason=reason,
            )
            if before["status"] is not TransactionStatus.COMPLETED:
                owner = actor if actor.id == tx.owner_user_id else None
                await self._notify_shared_expense(owner or await self._owner(tx), tx)
        return tx

    async def _owner(self, tx: Transaction) -> User:
        owner = await self.db.get(User, tx.owner_user_id)
        if owner is None:  # pragma: no cover - FK guarantees it
            raise NotFoundError()
        return owner

    # ================================================================== read

    async def get(self, actor: User, tx_id: uuid.UUID) -> TransactionOut:
        tx = await self.txs.get_visible(actor.id, tx_id)
        if tx is None:
            raise NotFoundError("Transaction not found")
        return await self._one_out(actor, tx)

    async def _where(self, actor: User, f: TransactionFilters) -> list[ColumnElement[bool]]:
        """Visibility + soft-delete + user filters, all as clauses on ``Transaction``."""
        if f.family_id is not None:
            await require_family_access(self.db, actor, f.family_id)
            scope = ScopeFilter.for_user(actor.id, family_id=f.family_id)
        else:
            scope = ScopeFilter.for_user(actor.id, scope=RecordScope.PERSONAL)
        if f.date_from and f.date_to and f.date_from > f.date_to:
            raise InvalidTransactionError("date_from is after date_to", code="invalid_date_range")
        if f.min_amount is not None and f.max_amount is not None and f.min_amount > f.max_amount:
            raise InvalidTransactionError(
                "min_amount is greater than max_amount", code="invalid_amount_range"
            )
        tz = user_tz(actor)
        return [
            scope.clause(Transaction),
            Transaction.deleted_at.is_(None),
            *self.txs.filter_clauses(
                starts_at=_day_start(f.date_from, tz) if f.date_from else None,
                ends_before=_day_start(f.date_to + timedelta(days=1), tz) if f.date_to else None,
                types=f.types,
                category_ids=f.category_ids,
                account_id=f.account_id,
                payment_method=f.payment_method,
                member_user_id=f.member_user_id,
                status=f.status,
                q=f.q,
                min_amount=f.min_amount,
                max_amount=f.max_amount,
                tag=f.tag,
            ),
        ]

    async def list_transactions(
        self,
        actor: User,
        filters: TransactionFilters,
        *,
        sort_by: str,
        sort_dir: str,
        page: PageParams,
    ) -> Page[TransactionOut]:
        where = await self._where(actor, filters)
        rows, total = await self.txs.page(where, sort_by=sort_by, sort_dir=sort_dir, params=page)
        return Page[TransactionOut].build(await self._out(actor, rows), total, page)

    async def summary(self, actor: User, filters: TransactionFilters) -> TransactionSummaryOut:
        where = await self._where(actor, filters)
        totals = await ledger.totals(self.db, *where)
        return TransactionSummaryOut(
            income=totals["income"],
            expense=totals["expense"],
            net=totals["net"],
            count=await self.txs.count(where),
        )

    async def export_csv(self, actor: User, filters: TransactionFilters) -> AsyncIterator[str]:
        """Authorise and build the query now; the returned iterator streams CSV lines."""
        where = await self._where(actor, filters)
        tz = user_tz(actor)
        return self._csv_lines(where, tz)

    async def _csv_lines(
        self, where: Sequence[ColumnElement[bool]], tz: tzinfo
    ) -> AsyncIterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")

        def line(cells: Sequence[str]) -> str:
            buf.seek(0)
            buf.truncate()
            writer.writerow([csv_safe(c) for c in cells])
            return buf.getvalue()

        yield line(CSV_COLUMNS)
        async for row in self.txs.stream_export(where):
            tx, category, account, to_account, payee, created_by, tags = row
            local = tx.occurred_at.astimezone(tz)
            yield line(
                [
                    str(tx.id),
                    local.strftime("%Y-%m-%d"),
                    local.strftime("%H:%M"),
                    tx.type.value,
                    tx.status.value,
                    money_str(tx.amount),
                    tx.currency,
                    category or "",
                    account or "",
                    to_account or "",
                    payee or "",
                    tx.merchant or "",
                    tx.description or "",
                    tx.payment_method.value if tx.payment_method else "",
                    tags or "",
                    tx.notes or "",
                    tx.scope.value,
                    created_by or "",
                    tx.source.value,
                ]
            )

    # ================================================================== update / delete

    async def update(
        self, actor: User, tx_id: uuid.UUID, data: TransactionUpdate, client: ClientInfo
    ) -> TransactionOut:
        tx = await self.txs.get(tx_id, for_update=True)
        await require_record_write(self.db, actor.id, tx)
        assert tx is not None  # noqa: S101 - require_record_write raised otherwise
        changes = data.model_dump(exclude_unset=True)
        if "scope" in changes and changes.pop("scope") is not tx.scope:
            raise InvalidTransactionError(
                "A transaction can't move between personal and family",
                code="scope_change_not_allowed",
            )
        if "family_id" in changes and changes.pop("family_id") != tx.family_id:
            raise InvalidTransactionError(
                "A transaction can't move to another family", code="scope_change_not_allowed"
            )
        if (
            "status" in changes
            and changes["status"] is not tx.status
            and tx.source is not TransactionSource.MANUAL
        ):
            raise InvalidTransactionError(
                f"The status of a {tx.source.value} transaction is changed through its own flow",
                code="status_change_not_allowed",
            )
        tags = changes.pop("tags", None)

        new_type: TransactionType = changes.get("type", tx.type)
        if new_type is not tx.type:
            # Drop fields that don't fit the new type unless the client set them explicitly.
            if new_type is not TransactionType.TRANSFER:
                changes.setdefault("to_account_id", None)
            if new_type is not TransactionType.ADJUSTMENT:
                changes.setdefault("adjustment_sign", None)
            if CATEGORY_KIND_FOR[new_type] is not CATEGORY_KIND_FOR[tx.type]:
                changes.setdefault("category_id", None)
        if "occurred_at" in changes:
            changes["occurred_at"] = self._as_aware(changes["occurred_at"], user_tz(actor))

        def merged(name: str) -> Any:
            return changes[name] if name in changes else getattr(tx, name)

        shape = _Shape(
            scope=tx.scope,
            family_id=tx.family_id,
            owner_user_id=tx.owner_user_id,
            type=new_type,
            currency=merged("currency"),
            account_id=merged("account_id"),
            to_account_id=merged("to_account_id"),
            category_id=merged("category_id"),
            payee_id=merged("payee_id"),
            adjustment_sign=merged("adjustment_sign"),
        )
        new_accounts = {
            changes[k]
            for k in ("account_id", "to_account_id")
            if changes.get(k) is not None and changes[k] != getattr(tx, k)
        }
        new_category = changes.get("category_id") is not None and (
            changes["category_id"] != tx.category_id
        )
        changes["currency"] = await self._validate(
            actor, shape, new_account_ids=new_accounts, new_category=new_category
        )

        diff: dict[str, dict[str, Any]] = {}
        for name, value in changes.items():
            before = getattr(tx, name)
            if before != value:
                diff[name] = {"before": _jsonable(before), "after": _jsonable(value)}
                setattr(tx, name, value)
        if tags is not None:
            old_tags = (await self.txs.tags_for([tx.id]))[tx.id]
            if sorted(old_tags) != sorted(tags):
                diff["tags"] = {"before": sorted(old_tags), "after": sorted(tags)}
                await self.txs.replace_tags(tx.id, tags)
        if diff:
            await self.db.flush()
            self._audit("transaction.updated", actor, tx, client, changes=diff)
            await self.db.commit()
            await self.db.refresh(tx)
        return await self._one_out(actor, tx)

    async def delete(self, actor: User, tx_id: uuid.UUID, client: ClientInfo) -> None:
        """Soft delete (reversible with :meth:`restore`)."""
        tx = await self.txs.get(tx_id, for_update=True)
        await require_record_write(self.db, actor.id, tx)
        assert tx is not None  # noqa: S101
        tx.deleted_at = utcnow()
        self._audit("transaction.deleted", actor, tx, client)
        await self.db.commit()

    async def restore(self, actor: User, tx_id: uuid.UUID, client: ClientInfo) -> TransactionOut:
        """Undo a soft delete. Same write rules as update/delete; restoring a live row is a
        no-op. 409 ``restore_conflict`` if an account/category/payee it used is gone."""
        tx = await self.txs.get(tx_id, for_update=True)
        if tx is None:
            raise NotFoundError("Transaction not found")
        if tx.deleted_at is None:
            await require_record_write(self.db, actor.id, tx)
            return await self._one_out(actor, tx)
        await self._require_write_deleted(actor.id, tx)
        accounts = await self.accounts.get_many(
            a for a in (tx.account_id, tx.to_account_id) if a is not None
        )
        categories = await self.txs.categories_many([tx.category_id] if tx.category_id else [])
        payees = await self.txs.payees_many([tx.payee_id] if tx.payee_id else [])
        gone = [
            *(f"account {a.name}" for a in accounts.values() if a.deleted_at is not None),
            *(f"category {c.name}" for c in categories.values() if c.deleted_at is not None),
            *(f"payee {p.name}" for p in payees.values() if p.deleted_at is not None),
        ]
        if gone:
            raise ConflictError(
                "Can't restore: its " + ", ".join(gone) + " was deleted",
                code="restore_conflict",
            )
        tx.deleted_at = None
        self._audit("transaction.restored", actor, tx, client)
        await self.db.commit()
        await self.db.refresh(tx)
        return await self._one_out(actor, tx)

    async def _require_write_deleted(self, user_id: uuid.UUID, tx: Transaction) -> None:
        """``require_record_write`` rules for a soft-deleted row (which it would 404)."""
        if tx.scope is RecordScope.PERSONAL:
            if tx.owner_user_id != user_id:
                raise NotFoundError("Transaction not found")
            return
        member = (
            await get_active_membership(self.db, user_id, tx.family_id) if tx.family_id else None
        )
        if member is None:
            raise NotFoundError("Transaction not found")
        if not can_modify_record(user_id, tx, member):
            raise ForbiddenError(
                "You can only change family records you created", code="insufficient_family_role"
            )

    # ================================================================== output

    async def _one_out(self, actor: User, tx: Transaction) -> TransactionOut:
        return (await self._out(actor, [tx]))[0]

    async def _out(self, actor: User, rows: Sequence[Transaction]) -> list[TransactionOut]:
        if not rows:
            return []
        ids = [t.id for t in rows]
        accounts = await self.accounts.get_many(
            a for t in rows for a in (t.account_id, t.to_account_id) if a is not None
        )
        categories = await self.txs.categories_many(t.category_id for t in rows if t.category_id)
        payees = await self.txs.payees_many(t.payee_id for t in rows if t.payee_id)
        names = await self.txs.user_names(t.owner_user_id for t in rows)
        tags = await self.txs.tags_for(ids)
        attachments = await self.txs.attachment_counts(ids)
        roles = await list_user_family_roles(self.db, actor.id)

        def account_ref(account_id: uuid.UUID | None) -> AccountRef | None:
            a = accounts.get(account_id) if account_id else None
            return (
                AccountRef(id=a.id, name=a.name, type=a.type, is_active=a.is_active) if a else None
            )

        out: list[TransactionOut] = []
        for t in rows:
            c = categories.get(t.category_id) if t.category_id else None
            p = payees.get(t.payee_id) if t.payee_id else None
            out.append(
                TransactionOut(
                    id=t.id,
                    type=t.type,
                    status=t.status,
                    source=t.source,
                    verification=t.verification,
                    amount=t.amount,
                    currency=t.currency,
                    scope=t.scope,
                    family_id=t.family_id,
                    created_by=UserRef(
                        id=t.owner_user_id, full_name=names.get(t.owner_user_id, "")
                    ),
                    account=account_ref(t.account_id),
                    to_account=account_ref(t.to_account_id),
                    category=CategoryRef(
                        id=c.id,
                        name=c.name,
                        kind=c.kind,
                        icon=c.icon,
                        color=c.color,
                        parent_id=c.parent_id,
                    )
                    if c
                    else None,
                    payee=PayeeRef(id=p.id, name=p.name, upi_id=p.upi_id) if p else None,
                    merchant=t.merchant,
                    description=t.description,
                    notes=t.notes,
                    payment_method=t.payment_method,
                    occurred_at=t.occurred_at,
                    adjustment_sign=t.adjustment_sign,
                    tags=tags.get(t.id, []),
                    attachments_count=attachments.get(t.id, 0),
                    recurring_id=t.recurring_id,
                    period_key=t.period_key,
                    upi_attempt_id=t.upi_attempt_id,
                    external_ref=t.external_ref,
                    idempotency_key=t.idempotency_key,
                    can_edit=_can_edit(actor.id, t, roles),
                    created_at=t.created_at,
                    updated_at=t.updated_at,
                    deleted_at=t.deleted_at,
                )
            )
        return out


def _day_start(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def _can_edit(user_id: uuid.UUID, tx: Transaction, roles: dict[uuid.UUID, FamilyRole]) -> bool:
    if tx.scope is RecordScope.PERSONAL:
        return tx.owner_user_id == user_id
    role = roles.get(tx.family_id) if tx.family_id else None
    if role is None:
        return False
    if role_has(role, FamilyPermission.MANAGE_ALL_RECORDS):
        return True
    return role_has(role, FamilyPermission.CREATE_RECORDS) and tx.owner_user_id == user_id
