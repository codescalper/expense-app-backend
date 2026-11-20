"""Receipt attachment endpoints (/api/v1/transactions/{transaction_id}/attachments)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse

from app.api.deps import AppSettings, Client, CurrentPrincipal, DbSession
from app.core.errors import PayloadTooLargeError
from app.schemas.attachments import AttachmentOut
from app.services.attachments import AttachmentService

router = APIRouter(prefix="/transactions", tags=["attachments"])


def get_attachment_service(db: DbSession, settings: AppSettings) -> AttachmentService:
    return AttachmentService(db, settings)


AttachmentSvc = Annotated[AttachmentService, Depends(get_attachment_service)]


@router.post("/{transaction_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    transaction_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: AttachmentSvc,
    client: Client,
    settings: AppSettings,
    file: UploadFile = File(
        ..., description="JPEG, PNG, WebP or PDF, max MAX_RECEIPT_BYTES (default 5 MB)"
    ),
) -> AttachmentOut:
    # Read at most limit+1 bytes so an oversized upload can't exhaust memory.
    limit = settings.max_receipt_bytes
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        mib = 1024 * 1024
        size = f"{limit // mib} MB" if limit % mib == 0 else f"{limit} bytes"
        raise PayloadTooLargeError(f"File must be at most {size}")
    return await svc.upload(principal.user, transaction_id, raw, client)


@router.get("/{transaction_id}/attachments")
async def list_attachments(
    transaction_id: uuid.UUID, principal: CurrentPrincipal, svc: AttachmentSvc
) -> list[AttachmentOut]:
    return await svc.list_for_transaction(principal.user, transaction_id)


@router.get("/{transaction_id}/attachments/{attachment_id}/download", response_class=FileResponse)
async def download_attachment(
    transaction_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: AttachmentSvc,
) -> FileResponse:
    f = await svc.file(principal.user, transaction_id, attachment_id)
    return FileResponse(
        f.path,
        media_type=f.content_type,
        filename=f.filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.delete(
    "/{transaction_id}/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_attachment(
    transaction_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: CurrentPrincipal,
    svc: AttachmentSvc,
    client: Client,
) -> None:
    await svc.delete(principal.user, transaction_id, attachment_id, client)
