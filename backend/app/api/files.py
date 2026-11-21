"""Turn a :class:`app.services.storage.StoredFile` into an HTTP response.

Local files are sent with ``FileResponse``; remote (Vercel Blob) files are streamed through
this process, so the client only ever talks to our authenticated endpoint and never sees a
storage URL. Streaming also keeps large receipts clear of Vercel's 4.5 MB limit on buffered
function responses.
"""

from __future__ import annotations

from typing import Literal

from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.services.storage import StoredFile


def stored_file_response(
    stored: StoredFile,
    *,
    media_type: str,
    headers: dict[str, str],
    filename: str | None = None,
    disposition: Literal["attachment", "inline"] = "attachment",
) -> Response:
    if stored.path is not None:
        return FileResponse(
            stored.path,
            media_type=media_type,
            filename=filename,
            content_disposition_type=disposition,
            headers=headers,
        )
    if stored.chunks is None:  # pragma: no cover - StoredFile always has one of the two
        raise RuntimeError("StoredFile has neither a path nor a stream")
    out_headers = dict(headers)
    if filename is not None:
        # Server-generated ASCII names (receipt-<id>.jpg): no quoting/encoding needed.
        out_headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return StreamingResponse(
        stored.chunks,
        media_type=media_type,
        headers=out_headers,
        background=BackgroundTask(stored.close) if stored.close is not None else None,
    )
