"""Receipt attachments on transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from app.schemas.common import ResponseModel


class AttachmentOut(ResponseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    kind: Literal["image", "pdf"]
    content_type: str
    size_bytes: int
    uploaded_by_user_id: uuid.UUID
    created_at: datetime
    download_url: str  # API path (relative to the server base URL); needs the bearer token
