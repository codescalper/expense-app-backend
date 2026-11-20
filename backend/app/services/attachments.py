"""Receipt attachments on transactions.

* Reading (list / download) needs the transaction to be visible to the caller; uploading and
  deleting need write access to it (``require_record_write``: personal → owner; family →
  owner/admin, or the member who created it; viewers 403). Invisible → 404.
* Files are validated by content (magic bytes), never by the client's filename or MIME type:
  JPEG/PNG/WebP images are re-encoded to JPEG without metadata, PDFs are accepted only without
  active content (see ``app.services.storage.process_receipt``).
* At most ``MAX_ATTACHMENTS_PER_TRANSACTION`` live attachments per transaction; files are at
  most ``settings.max_receipt_bytes``; uploads are rate-limited per user
  (``settings.receipt_upload_limit`` per ``receipt_upload_window_seconds``).
* Deletion is soft (the row and file are kept so it can be restored).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.core.permissions import require_record_write
from app.core.rate_limit import RateLimiter
from app.core.security import utcnow
from app.core.threads import run_limited
from app.models.finance import Transaction, TransactionAttachment
from app.models.user import User
from app.repositories.attachments import AttachmentRepository
from app.repositories.system import AuditRepository
from app.schemas.attachments import AttachmentOut
from app.services.auth import ClientInfo
from app.services.storage import LocalFileStorage, process_receipt

MAX_ATTACHMENTS_PER_TRANSACTION = 10
RECEIPT_FOLDER = "receipts"
IMAGE_WORKERS = 2  # shares the "image" thread limiter with avatar processing


@dataclass(frozen=True)
class AttachmentFile:
    path: Path
    content_type: str
    filename: str


def attachment_out(a: TransactionAttachment) -> AttachmentOut:
    return AttachmentOut(
        id=a.id,
        transaction_id=a.transaction_id,
        kind="pdf" if a.content_type == "application/pdf" else "image",
        content_type=a.content_type,
        size_bytes=a.size_bytes,
        uploaded_by_user_id=a.uploaded_by_user_id,
        created_at=a.created_at,
        download_url=f"/api/v1/transactions/{a.transaction_id}/attachments/{a.id}/download",
    )


class AttachmentLimitError(ConflictError):
    code = "attachment_limit"
    message = f"A transaction can have at most {MAX_ATTACHMENTS_PER_TRANSACTION} attachments"


class AttachmentService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.db = session
        self.settings = settings
        self.attachments = AttachmentRepository(session)
        self.audit = AuditRepository(session)
        self.limiter = RateLimiter(session)
        self.storage = LocalFileStorage(settings.upload_dir)

    def _audit(
        self,
        action: str,
        user: User,
        client: ClientInfo,
        tx: Transaction,
        attachment: TransactionAttachment,
    ) -> None:
        self.audit.record(
            action,
            actor_user_id=user.id,
            entity_type="transaction",
            entity_id=tx.id,
            family_id=tx.family_id,
            ip_address=client.ip,
            user_agent=client.user_agent,
            details={"attachment_id": str(attachment.id), "content_type": attachment.content_type},
        )

    async def _visible_transaction(self, user: User, transaction_id: uuid.UUID) -> Transaction:
        tx = await self.attachments.get_visible_transaction(user.id, transaction_id)
        if tx is None:
            raise NotFoundError("Transaction not found")
        return tx

    async def _writable_transaction(
        self, user: User, transaction_id: uuid.UUID, *, for_update: bool = False
    ) -> Transaction:
        tx = await self.attachments.get_live_transaction(transaction_id, for_update=for_update)
        await require_record_write(self.db, user.id, tx)
        assert tx is not None  # noqa: S101 - require_record_write raised 404 otherwise
        return tx

    async def upload(
        self, user: User, transaction_id: uuid.UUID, raw: bytes, client: ClientInfo
    ) -> AttachmentOut:
        await self._writable_transaction(user, transaction_id)
        if self.settings.rate_limit_enabled:
            await self.limiter.hit(
                f"receipt-upload:{user.id}",
                self.settings.receipt_upload_limit,
                self.settings.receipt_upload_window_seconds,
            )
            # Persist the hit now: rejected files (which still cost decoding CPU) must count.
            await self.db.commit()
        if await self.attachments.count_live(transaction_id) >= MAX_ATTACHMENTS_PER_TRANSACTION:
            raise AttachmentLimitError()
        # Decoding runs in a bounded worker thread, never on the event loop.
        processed = await run_limited(
            "image", IMAGE_WORKERS, process_receipt, raw, self.settings.max_receipt_bytes
        )
        # Re-check under a row lock so concurrent uploads can't pass the limit together.
        tx = await self._writable_transaction(user, transaction_id, for_update=True)
        if await self.attachments.count_live(tx.id) >= MAX_ATTACHMENTS_PER_TRANSACTION:
            raise AttachmentLimitError()
        key = self.storage.save(RECEIPT_FOLDER, processed.data, processed.ext)
        try:
            attachment = TransactionAttachment(
                transaction_id=tx.id,
                storage_key=key,
                content_type=processed.content_type,
                size_bytes=len(processed.data),
                uploaded_by_user_id=user.id,
            )
            self.attachments.add(attachment)
            await self.db.flush()
            self._audit("transaction.attachment_added", user, client, tx, attachment)
            await self.db.commit()
        except BaseException:
            self.storage.delete(key)  # no orphaned files when the DB write fails
            raise
        await self.db.refresh(attachment)
        return attachment_out(attachment)

    async def list_for_transaction(
        self, user: User, transaction_id: uuid.UUID
    ) -> list[AttachmentOut]:
        tx = await self._visible_transaction(user, transaction_id)
        return [attachment_out(a) for a in await self.attachments.list_live(tx.id)]

    async def file(
        self, user: User, transaction_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> AttachmentFile:
        tx = await self._visible_transaction(user, transaction_id)
        attachment = await self.attachments.get_live(tx.id, attachment_id)
        path = self.storage.path_for(attachment.storage_key) if attachment else None
        if attachment is None or path is None:
            raise NotFoundError("Attachment not found")
        ext = "pdf" if attachment.content_type == "application/pdf" else "jpg"
        return AttachmentFile(
            path=path,
            content_type=attachment.content_type,
            filename=f"receipt-{str(attachment.id)[:8]}.{ext}",
        )

    async def delete(
        self,
        user: User,
        transaction_id: uuid.UUID,
        attachment_id: uuid.UUID,
        client: ClientInfo,
    ) -> None:
        tx = await self._writable_transaction(user, transaction_id)
        attachment = await self.attachments.get_live(tx.id, attachment_id)
        if attachment is None:
            raise NotFoundError("Attachment not found")
        attachment.deleted_at = utcnow()
        self._audit("transaction.attachment_deleted", user, client, tx, attachment)
        await self.db.commit()
