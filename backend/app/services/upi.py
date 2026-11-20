"""UPI intent payments (spec §6) — the priority payment flow.

Flow: the client parses the QR (``POST /upi/parse`` gives the same result plus enrichment),
**initiates** (one DB transaction: authorise → touch the payee → create the attempt and a
*pending* ``upi_intent`` expense → commit), launches the ``upi://pay`` URI through the OS
chooser (``/launched``), stores whatever the UPI app hands back (``/return``) and finally asks
the user what happened (``/confirm``).

Integrity rules (spec §6, §17 scenarios 4 & 5):

* **Opening a UPI app is not success.** ``launched`` and ``return`` never touch the
  transaction; it stays ``pending`` / ``unverified`` and out of every total until the user
  confirms.
* **Return data is untrusted.** It is stored sanitised (no credential-like keys, capped) and
  only surfaced as an ``untrusted_hint``.
* **User-confirmed ≠ verified.** A user's "it succeeded" sets the transaction ``completed`` with
  ``verification=user_confirmed``. ``verified`` is never set here: it is reserved for a future
  authorised bank / PSP integration (or statement import).
* **No duplicates.** Initiation is keyed by ``(user, idempotency key)``: an advisory lock
  serialises concurrent retries, a replay returns the original attempt (200), and the unique
  constraints on attempts / transactions back this up. Confirmations are row-locked and
  idempotent; repeating one changes and creates nothing.
* **No credentials.** Request bodies with PIN/MPIN/password/CVV/OTP-like fields are rejected;
  nothing of the sort is stored.
* **Manual reconciliation.** A previous outcome may be changed later (e.g. "failed" → "success"
  after checking the bank statement); every change is audited with before/after values.

Visibility: personal attempts only to their initiator; family attempts to active members (404
otherwise). Writes (launched/return/confirm/cancel): the initiator, or the family's
owner/admins (members can't change someone else's attempt, viewers can't change any — 403).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.permissions import (
    FamilyPermission,
    ScopeFilter,
    list_user_family_roles,
    require_family_access,
    require_record_write,
    role_has,
)
from app.core.rate_limit import RateLimiter
from app.core.security import utcnow
from app.models.enums import (
    FamilyRole,
    NotificationCategory,
    PaymentMethod,
    RecordScope,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    UpiAttemptStatus,
    VerificationStatus,
)
from app.models.upi import UpiPaymentAttempt
from app.models.user import User
from app.repositories.payees import PayeeRepository
from app.repositories.system import AuditRepository
from app.repositories.upi import UpiRepository
from app.schemas.accounts import money_str
from app.schemas.payees import PayeeOut
from app.schemas.transactions import PayeeRef, TransactionCreate, UserRef
from app.schemas.upi import (
    OPEN_STATUSES,
    STATUS_LABELS,
    VERIFICATION_LABELS,
    ConfirmOutcome,
    UntrustedReturnHint,
    UpiConfirmRequest,
    UpiInitiateRequest,
    UpiParseOut,
    UpiParseRequest,
    UpiPaymentDetailOut,
    UpiPaymentOut,
    UpiReturnRequest,
)
from app.services.auth import ClientInfo
from app.services.notify import notify
from app.services.payees import PayeeService
from app.services.transactions import TransactionService, user_tz
from app.services.upi_uri import (
    MAX_NAME_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_URI_NOTE_LENGTH,
    UpiUriError,
    build_upi_uri,
    clean_text,
    new_tr_ref,
    parse_upi_uri,
    return_hint,
    sanitize_return,
)
from app.utils.pagination import Page, PageParams

REMINDER_FIRST_AFTER = timedelta(minutes=30)
REMINDER_SECOND_AFTER = timedelta(hours=24)
REMINDER_LOOKBACK = timedelta(days=7)
REMINDER_BATCH = 500

ATTEMPT_IDEMPOTENCY_CONSTRAINT = "uq_upi_payment_attempts_owner_user_id_idempotency_key"
TR_REF_CONSTRAINT = "uq_upi_payment_attempts_tr_ref"
TX_KEY_PREFIX = "upi:"

# outcome → (attempt status, transaction status, verification)
OUTCOMES: dict[str, tuple[UpiAttemptStatus, TransactionStatus, VerificationStatus]] = {
    "success": (
        UpiAttemptStatus.SUCCESS_REPORTED,
        TransactionStatus.COMPLETED,
        VerificationStatus.USER_CONFIRMED,
    ),
    "failure": (
        UpiAttemptStatus.FAILURE_REPORTED,
        TransactionStatus.FAILED,
        VerificationStatus.USER_CONFIRMED,
    ),
    "pending": (
        UpiAttemptStatus.PENDING_REPORTED,
        TransactionStatus.PENDING,
        VerificationStatus.UNVERIFIED,
    ),
}
LAUNCHABLE = (UpiAttemptStatus.INITIATED, UpiAttemptStatus.LAUNCHED)
# Statuses from which a confirmation is the *first* real answer (not a reconciliation).
UNANSWERED = (
    UpiAttemptStatus.INITIATED,
    UpiAttemptStatus.LAUNCHED,
    UpiAttemptStatus.PENDING_REPORTED,
)


def _constraint_name(exc: IntegrityError) -> str:
    orig: Any = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return str(orig)


def _can_edit(user_id: uuid.UUID, a: UpiPaymentAttempt, roles: dict[uuid.UUID, FamilyRole]) -> bool:
    if a.scope is RecordScope.PERSONAL:
        return a.owner_user_id == user_id
    role = roles.get(a.family_id) if a.family_id else None
    if role is None:
        return False
    if role_has(role, FamilyPermission.MANAGE_ALL_RECORDS):
        return True
    return role_has(role, FamilyPermission.CREATE_RECORDS) and a.owner_user_id == user_id


class UpiService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.repo = UpiRepository(session)
        self.payee_repo = PayeeRepository(session)
        self.payees = PayeeService(session)
        self.txs = TransactionService(session)
        self.audit = AuditRepository(session)
        # Shares the request's connection/transaction (no second pooled connection).
        self.limiter = RateLimiter(session)

    # ------------------------------------------------------------------ helpers

    async def _rate_limit(self, kind: str, user: User) -> None:
        """Per-user limit for ``kind`` (``initiate`` / ``parse``) from the settings
        (``UPI_<KIND>_RATE_LIMIT`` per ``UPI_<KIND>_RATE_WINDOW_SECONDS``)."""
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        limit, window = {
            "initiate": (
                settings.upi_initiate_rate_limit,
                settings.upi_initiate_rate_window_seconds,
            ),
            "parse": (settings.upi_parse_rate_limit, settings.upi_parse_rate_window_seconds),
        }[kind]
        await self.limiter.hit(f"upi:{kind}:{user.id}", limit, window)

    def _audit(
        self,
        action: str,
        user: User,
        attempt: UpiPaymentAttempt,
        client: ClientInfo | None,
        **details: Any,
    ) -> None:
        base = {
            "amount": money_str(attempt.amount),
            "payee_vpa": attempt.payee_vpa,
            "status": attempt.status.value,
            "tr_ref": attempt.tr_ref,
            "transaction_id": str(attempt.transaction_id) if attempt.transaction_id else None,
        }
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="upi_payment",
            entity_id=attempt.id,
            family_id=attempt.family_id,
            ip_address=client.ip if client else None,
            user_agent=client.user_agent if client else None,
            details={**base, **details},
        )

    async def _load_for_write(self, user: User, attempt_id: uuid.UUID) -> UpiPaymentAttempt:
        """Row-locked attempt the caller may change: 404 if invisible, 403 if the caller is a
        viewer / a member who didn't initiate it."""
        attempt = await self.repo.get_visible(user.id, attempt_id, for_update=True)
        if attempt is None:
            raise NotFoundError("UPI payment not found")
        await require_record_write(self.db, user.id, attempt)
        return attempt

    @staticmethod
    def _invalid(exc: UpiUriError) -> ValidationFailedError:
        return ValidationFailedError(exc.message, code=exc.code)

    # ------------------------------------------------------------------ parse

    async def parse(self, user: User, body: UpiParseRequest) -> UpiParseOut:
        """Validate a scanned QR payload and enrich it (known payee in the scope, suggested
        category). Purely informational: nothing is stored."""
        if body.family_id is not None:
            await require_family_access(self.db, user, body.family_id)
        await self._rate_limit("parse", user)
        try:
            parsed = parse_upi_uri(body.payload)
        except UpiUriError as exc:
            await self.db.commit()  # keep the rate-limit hit
            raise self._invalid(exc) from exc
        known = await self.payee_repo.find_by_vpa(user.id, body.family_id, parsed.pa)
        suggestion = await self.payees.suggest_category(
            user, body.family_id, upi_id=parsed.pa, merchant=parsed.pn, q=parsed.tn
        )
        await self.db.commit()
        return UpiParseOut(
            payee_vpa=parsed.pa,
            payee_name=parsed.pn,
            amount=parsed.am,
            amount_locked=parsed.am is not None,
            currency="INR",
            note=parsed.tn,
            merchant_code=parsed.mc,
            reference=parsed.tr,
            mode=parsed.mode,
            purpose=parsed.purpose,
            url=parsed.url,
            unknown=parsed.unknown,
            warnings=parsed.warnings,
            known_payee=PayeeOut.model_validate(known) if known else None,
            suggested_category=suggestion,
        )

    # ------------------------------------------------------------------ initiate

    async def initiate(
        self, user: User, body: UpiInitiateRequest, client: ClientInfo
    ) -> tuple[UpiPaymentDetailOut, bool]:
        """Create the attempt + its pending transaction, or return the original attempt for a
        repeated idempotency key (``created=False``)."""
        key = body.idempotency_key
        if key is None:
            raise ValidationFailedError(
                "An idempotency key is required (Idempotency-Key header or idempotency_key)",
                code="idempotency_key_required",
            )
        family_id = body.family_id if body.scope is RecordScope.FAMILY else None
        if family_id is not None:
            await require_family_access(self.db, user, family_id, FamilyPermission.CREATE_RECORDS)

        await self.repo.lock_idempotency_key(user.id, key)
        existing = await self.repo.get_by_idempotency_key(user.id, key)
        if existing is not None:
            return await self._replay(user, existing, body), False

        await self._rate_limit("initiate", user)
        vpa = body.payee_vpa
        if body.payee_id is not None:
            chosen = await self.payee_repo.get_visible(user.id, body.payee_id)
            if chosen is None:
                raise NotFoundError("Payee not found")
            if chosen.scope is not body.scope or chosen.family_id != family_id:
                raise ValidationFailedError(
                    "The payee belongs to a different scope", code="payee_scope_mismatch"
                )
            if chosen.upi_id != vpa:
                raise ValidationFailedError(
                    "payee_vpa doesn't match the chosen payee's UPI ID", code="payee_vpa_mismatch"
                )

        now = utcnow()
        payee = await self.payees.touch(
            user,
            body.scope,
            family_id,
            vpa,
            body.payee_name,
            merchant_code=body.merchant_code,
            default_category_id=body.category_id,
            used_at=now,
        )
        name = clean_text(body.payee_name, MAX_NAME_LENGTH) or payee.name
        note = clean_text(body.note, MAX_TEXT_LENGTH)

        attempt = await self._insert_attempt(
            user,
            key,
            UpiPaymentAttempt(
                owner_user_id=user.id,
                scope=body.scope,
                family_id=family_id,
                payee_id=payee.id,
                payee_vpa=vpa,
                payee_name=name,
                amount=body.amount,
                currency="INR",
                note=clean_text(note, MAX_URI_NOTE_LENGTH),
                idempotency_key=key,
                status=UpiAttemptStatus.INITIATED,
                verification=VerificationStatus.UNVERIFIED,
                initiated_at=now,
            ),
        )
        if attempt is None:  # lost a race the advisory lock should have prevented
            await self.db.rollback()
            existing = await self.repo.get_by_idempotency_key(user.id, key)
            if existing is None:  # pragma: no cover - the constraint said it exists
                raise ConflictError("Please retry", code="idempotency_conflict")
            return await self._replay(user, existing, body), False

        tx, created = await self.txs.create_internal(
            user,
            TransactionCreate(
                type=TransactionType.EXPENSE,
                amount=body.amount,
                currency="INR",
                scope=body.scope,
                family_id=family_id,
                account_id=body.account_id,
                category_id=body.category_id,
                payee_id=payee.id,
                merchant=name[:120],
                description=(note or f"UPI payment to {name}")[:255],
                notes=f"UPI to {vpa} · ref {attempt.tr_ref}",
                payment_method=PaymentMethod.UPI,
                status=TransactionStatus.PENDING,
            ),
            source=TransactionSource.UPI_INTENT,
            status=TransactionStatus.PENDING,
            verification=VerificationStatus.UNVERIFIED,
            idempotency_key=TX_KEY_PREFIX + key,
            upi_attempt_id=attempt.id,
            client=client,
        )
        if not created:
            # A transaction (not from this flow) already uses "upi:<key>" — refuse rather than
            # link someone else's row.
            raise ConflictError(
                "This idempotency key was already used for another transaction",
                code="idempotency_key_reused",
            )
        attempt.transaction_id = tx.id
        await self.db.flush()
        self._audit(
            "upi.initiated",
            user,
            attempt,
            client,
            from_qr=body.from_qr,
            qr_amount_locked=body.qr_amount_locked,
            payee_id=str(payee.id),
        )
        await self.db.commit()
        return await self._detail(user, attempt), True

    async def _insert_attempt(
        self, user: User, key: str, attempt: UpiPaymentAttempt
    ) -> UpiPaymentAttempt | None:
        """Insert in a savepoint; ``None`` if ``(user, key)`` already exists. A (practically
        impossible) ``tr_ref`` collision is retried with a fresh reference."""
        for _ in range(3):
            attempt.tr_ref = new_tr_ref()
            try:
                async with self.db.begin_nested():
                    self.repo.add(attempt)
                    await self.db.flush()
                return attempt
            except IntegrityError as exc:
                name = _constraint_name(exc)
                if ATTEMPT_IDEMPOTENCY_CONSTRAINT in name:
                    return None
                if TR_REF_CONSTRAINT not in name:
                    raise
        raise ConflictError("Please retry", code="reference_conflict")  # pragma: no cover

    async def _replay(
        self, user: User, existing: UpiPaymentAttempt, body: UpiInitiateRequest
    ) -> UpiPaymentDetailOut:
        same = (
            existing.payee_vpa == body.payee_vpa
            and existing.amount == body.amount
            and existing.scope is body.scope
            and existing.family_id == (body.family_id if body.scope is RecordScope.FAMILY else None)
        )
        if not same:
            raise ConflictError(
                "This idempotency key was already used for a different payment",
                code="idempotency_key_reused",
                details={"upi_payment_id": str(existing.id)},
            )
        visible = await self.repo.get_visible(user.id, existing.id)
        if visible is None:  # e.g. the user has since left the family
            raise NotFoundError("UPI payment not found")
        return await self._detail(user, visible)

    # ------------------------------------------------------------------ lifecycle

    async def mark_launched(
        self, user: User, attempt_id: uuid.UUID, client: ClientInfo
    ) -> UpiPaymentDetailOut:
        """The UPI app chooser was opened. Never changes the transaction."""
        attempt = await self._load_for_write(user, attempt_id)
        if attempt.status is UpiAttemptStatus.LAUNCHED:
            return await self._detail(user, attempt)
        if attempt.status is not UpiAttemptStatus.INITIATED:
            raise ConflictError(
                f"This payment is {attempt.status.value} and can't be launched again",
                code="upi_not_launchable",
            )
        attempt.status = UpiAttemptStatus.LAUNCHED
        attempt.launched_at = utcnow()
        await self.db.flush()
        self._audit("upi.launched", user, attempt, client)
        await self.db.commit()
        return await self._detail(user, attempt)

    async def record_return(
        self, user: User, attempt_id: uuid.UUID, body: UpiReturnRequest, client: ClientInfo
    ) -> UpiPaymentDetailOut:
        """Store the (untrusted) data the UPI app returned. Never changes money state or the
        attempt status — the client must ask the user and call ``/confirm``."""
        attempt = await self._load_for_write(user, attempt_id)
        sanitized = sanitize_return(body.raw)
        previous = dict(attempt.raw_return or {})
        previous.pop("received_at", None)
        if sanitized is None or sanitized == previous:
            return await self._detail(user, attempt)
        attempt.raw_return = {**sanitized, "received_at": utcnow().isoformat()}
        await self.db.flush()
        hint = return_hint(sanitized, attempt.tr_ref) or {}
        self._audit(
            "upi.return_received",
            user,
            attempt,
            client,
            claimed_status=hint.get("status"),
            reference_matches=hint.get("reference_matches"),
        )
        await self.db.commit()
        return await self._detail(user, attempt)

    async def confirm(
        self, user: User, attempt_id: uuid.UUID, body: UpiConfirmRequest, client: ClientInfo
    ) -> UpiPaymentDetailOut:
        """Record the outcome the user reports (success / failure / still pending).

        Idempotent: the same outcome again changes nothing. A different outcome later is a
        manual reconciliation (audited ``upi.reconciled`` with before/after). Never sets
        ``verification=verified``."""
        attempt = await self._load_for_write(user, attempt_id)
        tx = await self.repo.get_transaction(attempt.transaction_id, for_update=True)
        if tx is None or tx.deleted_at is not None:
            raise ConflictError(
                "The transaction for this payment was deleted; restore it first",
                code="transaction_deleted",
            )
        outcome: ConfirmOutcome = body.outcome
        new_status, tx_status, verification = OUTCOMES[outcome]
        utr = body.utr
        attempt_same = attempt.status is new_status and (utr is None or utr == attempt.utr)
        tx_same = (
            tx.status is tx_status
            and tx.verification is verification
            and (utr is None or tx.external_ref == utr)
        )
        if attempt_same and tx_same:
            return await self._detail(user, attempt)

        before = {
            "status": attempt.status.value,
            "verification": attempt.verification.value,
            "utr": attempt.utr,
            "transaction_status": tx.status.value,
        }
        previous_status = attempt.status
        if previous_status is not new_status:
            attempt.confirmed_at = None if outcome == "pending" else utcnow()
        attempt.status = new_status
        attempt.verification = verification
        if utr is not None:
            attempt.utr = utr
        await self.txs.mark_status(
            tx.id,
            tx_status,
            actor=user,
            verification=verification,
            external_ref=utr,
            client=client,
            reason=f"upi_{outcome}_reported",
        )
        await self.db.flush()
        after = {
            "status": attempt.status.value,
            "verification": attempt.verification.value,
            "utr": attempt.utr,
            "transaction_status": tx_status.value,
        }
        if previous_status is new_status:
            action = "upi.utr_updated"
        elif previous_status in UNANSWERED:
            action = "upi.confirmed"
        else:
            action = "upi.reconciled"
        self._audit(
            action,
            user,
            attempt,
            client,
            outcome=outcome,
            changes={
                k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]
            },
        )
        await self.db.commit()
        return await self._detail(user, attempt)

    async def cancel(
        self, user: User, attempt_id: uuid.UUID, client: ClientInfo
    ) -> UpiPaymentDetailOut:
        """Abandon the payment: attempt and transaction become ``cancelled``. Not allowed once
        the user reported success (report a failure first if that was wrong)."""
        attempt = await self._load_for_write(user, attempt_id)
        if attempt.status is UpiAttemptStatus.CANCELLED:
            return await self._detail(user, attempt)
        if attempt.status is UpiAttemptStatus.SUCCESS_REPORTED:
            raise ConflictError(
                "This payment was reported as successful and can't be cancelled",
                code="upi_payment_succeeded",
            )
        tx = await self.repo.get_transaction(attempt.transaction_id, for_update=True)
        previous = attempt.status.value
        attempt.status = UpiAttemptStatus.CANCELLED
        if (
            tx is not None
            and tx.deleted_at is None
            and tx.status is not TransactionStatus.CANCELLED
        ):
            await self.txs.mark_status(
                tx.id,
                TransactionStatus.CANCELLED,
                actor=user,
                client=client,
                reason="upi_cancelled",
            )
        await self.db.flush()
        self._audit(
            "upi.cancelled",
            user,
            attempt,
            client,
            changes={"status": {"before": previous, "after": attempt.status.value}},
        )
        await self.db.commit()
        return await self._detail(user, attempt)

    # ------------------------------------------------------------------ reads

    async def get(self, user: User, attempt_id: uuid.UUID) -> UpiPaymentDetailOut:
        attempt = await self.repo.get_visible(user.id, attempt_id)
        if attempt is None:
            raise NotFoundError("UPI payment not found")
        return await self._detail(user, attempt)

    async def list_payments(
        self,
        user: User,
        *,
        family_id: uuid.UUID | None,
        statuses: Sequence[UpiAttemptStatus],
        q: str | None,
        date_from: date | None,
        date_to: date | None,
        page: PageParams,
    ) -> Page[UpiPaymentOut]:
        """Without ``family_id``: my personal payments. With it: that family's payments (404 if
        I'm not a member). Newest first."""
        if family_id is not None:
            await require_family_access(self.db, user, family_id)
            scope = ScopeFilter.for_user(user.id, family_id=family_id)
        else:
            scope = ScopeFilter.for_user(user.id, scope=RecordScope.PERSONAL)
        if date_from and date_to and date_from > date_to:
            raise ValidationFailedError("date_from is after date_to", code="invalid_date_range")
        tz = user_tz(user)
        where = [
            scope.clause(UpiPaymentAttempt),
            *self.repo.filter_clauses(
                statuses=statuses,
                q=q,
                starts_at=datetime.combine(date_from, time.min, tzinfo=tz) if date_from else None,
                ends_before=(
                    datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz)
                    if date_to
                    else None
                ),
            ),
        ]
        rows, total = await self.repo.page(where, page)
        return Page[UpiPaymentOut].build(await self._outs(user, rows), total, page)

    # ------------------------------------------------------------------ output

    async def _outs(self, user: User, rows: Sequence[UpiPaymentAttempt]) -> list[UpiPaymentOut]:
        if not rows:
            return []
        names = await self.repo.user_names([a.owner_user_id for a in rows])
        tx_status = await self.repo.transaction_statuses(
            [a.transaction_id for a in rows if a.transaction_id]
        )
        roles = await list_user_family_roles(self.db, user.id)
        return [
            UpiPaymentOut(
                id=a.id,
                scope=a.scope,
                family_id=a.family_id,
                status=a.status,
                status_label=STATUS_LABELS[a.status],
                verification=a.verification,
                verification_label=VERIFICATION_LABELS[a.verification],
                needs_confirmation=a.status in OPEN_STATUSES,
                payee_vpa=a.payee_vpa,
                payee_name=a.payee_name,
                payee_id=a.payee_id,
                amount=a.amount,
                currency=a.currency,
                note=a.note,
                tr_ref=a.tr_ref,
                utr=a.utr,
                transaction_id=a.transaction_id,
                transaction_status=tx_status.get(a.transaction_id) if a.transaction_id else None,
                initiated_by=UserRef(id=a.owner_user_id, full_name=names.get(a.owner_user_id, "")),
                initiated_at=a.initiated_at,
                launched_at=a.launched_at,
                confirmed_at=a.confirmed_at,
                created_at=a.created_at,
                updated_at=a.updated_at,
                can_edit=_can_edit(user.id, a, roles),
            )
            for a in rows
        ]

    async def _detail(self, user: User, attempt: UpiPaymentAttempt) -> UpiPaymentDetailOut:
        await self.db.refresh(attempt)
        base = (await self._outs(user, [attempt]))[0]
        payee = await self.repo.get_payee(attempt.payee_id)
        transaction = None
        if attempt.transaction_id is not None:
            try:
                transaction = await self.txs.get(user, attempt.transaction_id)
            except NotFoundError:  # soft-deleted
                transaction = None
        upi_uri = None
        if attempt.status in LAUNCHABLE:
            upi_uri = build_upi_uri(
                pa=attempt.payee_vpa,
                pn=attempt.payee_name,
                am=attempt.amount,
                tr=attempt.tr_ref,
                tn=attempt.note,
                mc=payee.merchant_code if payee else None,
            )
        hint = None
        raw = attempt.raw_return
        if raw:
            parsed = return_hint(raw, attempt.tr_ref)
            if parsed is not None:
                received = raw.get("received_at")
                hint = UntrustedReturnHint(
                    **parsed,
                    received_at=datetime.fromisoformat(received)
                    if isinstance(received, str)
                    else None,
                )
        return UpiPaymentDetailOut(
            **base.model_dump(),
            upi_uri=upi_uri,
            payee=PayeeRef(id=payee.id, name=payee.name, upi_id=payee.upi_id) if payee else None,
            transaction=transaction,
            untrusted_hint=hint,
        )

    # ------------------------------------------------------------------ reminders (worker)

    @staticmethod
    async def send_pending_reminders(db: AsyncSession, now: datetime) -> int:
        """Remind initiators about payments they never confirmed: once 30 minutes after
        initiation (dedupe ``upi:<id>:pending:1``) and once after 24 hours (``…:2``; an
        attempt first seen when already a day old gets only this one). Never completes or
        cancels anything. Returns the number of notifications created."""
        repo = UpiRepository(db)
        candidates = await repo.reminder_candidates(
            initiated_after=now - REMINDER_LOOKBACK,
            initiated_before=now - REMINDER_FIRST_AFTER,
            limit=REMINDER_BATCH,
        )
        created = 0
        for a in candidates:
            initiated = a.initiated_at
            if initiated.tzinfo is None:  # pragma: no cover - timestamptz columns
                initiated = initiated.replace(tzinfo=UTC)
            stage = 2 if now - initiated >= REMINDER_SECOND_AFTER else 1
            who = a.payee_name or a.payee_vpa
            amount = f"₹{money_str(a.amount)}"
            title = "Did your UPI payment go through?"
            if stage == 1:
                body = f"{amount} to {who} is waiting for your confirmation."
            else:
                body = f"{amount} to {who} is still unconfirmed after a day. Did it succeed?"
            if await notify(
                db,
                user_id=a.owner_user_id,
                category=NotificationCategory.UPI_PENDING,
                title=title,
                body=body,
                dedupe_key=f"upi:{a.id}:pending:{stage}",
                data={
                    "upi_attempt_id": str(a.id),
                    "transaction_id": str(a.transaction_id) if a.transaction_id else None,
                    "amount": money_str(a.amount),
                    "payee_name": who,
                },
                family_id=a.family_id,
            ):
                created += 1
        await db.commit()
        return created
