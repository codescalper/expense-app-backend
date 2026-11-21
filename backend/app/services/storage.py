"""File storage for user uploads (avatars, family images, receipts).

``FileStorage`` is the small async interface the avatar, family-image and receipt code uses.
Two implementations, picked by ``STORAGE_BACKEND`` (:func:`get_storage`):

* ``local`` (default): files under ``UPLOAD_DIR`` (docker compose / self-hosted).
* ``s3``: a private bucket on an S3-compatible service such as Neon object storage
  (``app/services/s3_storage.py``), for serverless deployments whose disk doesn't survive
  an invocation.
* ``vercel_blob``: a *private* Vercel Blob store (``app/services/blob_storage.py``).

Keys are generated server-side (``<folder>/<random>.<ext>``) and validated, so callers can never
address arbitrary paths. Files are only ever served through our authenticated endpoints.
"""

from __future__ import annotations

import io
import re
import secrets
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.errors import AppError, BadRequestError, PayloadTooLargeError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

log = get_logger(__name__)

_KEY_RE = re.compile(r"^[a-z]+/[A-Za-z0-9_-]{16,64}\.(jpg|png|webp|pdf)$")
CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
}
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_IMAGE_PIXELS = 25_000_000  # Pillow's global decompression-bomb guard (all uploads)
MAX_AVATAR_PIXELS = 4096 * 4096  # ~16.7 MP: checked from the header, before decoding
AVATAR_SIZE = 512

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class StorageError(AppError):
    """The storage backend failed (network, quota...). Details go to the log, not the client."""

    status_code = 503
    code = "storage_unavailable"
    message = "File storage is temporarily unavailable. Please try again."


def validate_key(key: str) -> None:
    if not _KEY_RE.match(key):
        raise BadRequestError("Invalid file key", code="invalid_file_key")


def new_key(folder: str, ext: str) -> str:
    """A fresh, unguessable key (192 random bits)."""
    key = f"{folder}/{secrets.token_urlsafe(24)}.{ext}"
    validate_key(key)
    return key


@dataclass(frozen=True)
class StoredFile:
    """A stored file ready to be sent: a local ``path`` or a stream of ``chunks`` (+ ``close``,
    which must run once the response is done; see ``app.api.files.stored_file_response``)."""

    path: Path | None = None
    chunks: AsyncIterator[bytes] | None = None
    close: Callable[[], Awaitable[None]] | None = None


class FileStorage(Protocol):
    async def save(self, folder: str, data: bytes, ext: str) -> str:
        """Store ``data`` under a new key in ``folder``; returns the key."""
        ...

    async def open(self, key: str) -> StoredFile | None:
        """The stored file, or None if it doesn't exist."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the file (missing files are fine)."""
        ...


class LocalFileStorage:
    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir).resolve()

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.base / key).resolve()
        if self.base not in path.parents:
            raise BadRequestError("Invalid file key", code="invalid_file_key")
        return path

    def path_for(self, key: str) -> Path | None:
        path = self._path(key)
        return path if path.is_file() else None

    async def save(self, folder: str, data: bytes, ext: str) -> str:
        key = new_key(folder, ext)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    async def open(self, key: str) -> StoredFile | None:
        path = self.path_for(key)
        return StoredFile(path=path) if path is not None else None

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def get_storage(settings: Settings) -> FileStorage:
    if settings.storage_backend == "s3":
        from app.services.s3_storage import S3Storage

        # All present: enforced by Settings validation.
        if (
            settings.s3_endpoint_url is None
            or settings.s3_bucket is None
            or settings.s3_access_key_id is None
            or settings.s3_secret_access_key is None
        ):
            raise RuntimeError("STORAGE_BACKEND=s3 requires the S3_* settings")
        return S3Storage(
            endpoint_url=settings.s3_endpoint_url,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        )
    if settings.storage_backend == "vercel_blob":
        from app.services.blob_storage import VercelBlobStorage

        if settings.blob_read_write_token is None:  # also enforced by Settings validation
            raise RuntimeError("STORAGE_BACKEND=vercel_blob requires BLOB_READ_WRITE_TOKEN")
        return VercelBlobStorage(
            settings.blob_read_write_token.get_secret_value(), api_url=settings.blob_api_url
        )
    return LocalFileStorage(settings.upload_dir)


async def discard(storage: FileStorage, key: str) -> None:
    """Best-effort delete for files that are already unreferenced (after the DB commit, or
    cleaning up after a failed write): a storage hiccup must not fail the request."""
    try:
        await storage.delete(key)
    except Exception:  # noqa: BLE001 - an orphaned file is better than a failed request
        log.warning("storage_delete_failed", folder=key.split("/", 1)[0])


def process_avatar(raw: bytes, max_bytes: int) -> bytes:
    """Validate an uploaded image by content (not by filename/MIME) and re-encode it.

    Re-encoding strips EXIF/GPS metadata and any trailing payload. CPU-bound: call it from a
    worker thread (``UserService.set_avatar`` does).
    """
    if len(raw) > max_bytes:
        raise PayloadTooLargeError(f"Image must be at most {max_bytes / 1024 / 1024:g} MB")
    if not raw:
        raise BadRequestError("Empty file", code="invalid_image")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            width, height = probe.size
            # Image.open only parses the header; reject huge canvases before any decoding.
            if width * height > MAX_AVATAR_PIXELS:
                raise BadRequestError(
                    "Image dimensions are too large (max 4096 x 4096)", code="image_too_large"
                )
            probe.verify()
        if fmt not in ALLOWED_IMAGE_FORMATS:
            raise BadRequestError("Only JPEG, PNG or WebP images are allowed", code="invalid_image")
        with Image.open(io.BytesIO(raw)) as source:
            rgb = source.convert("RGB")
        # Center-crop to a square, then downscale.
        side = min(rgb.size)
        left, top = (rgb.width - side) // 2, (rgb.height - side) // 2
        square = rgb.crop((left, top, left + side, top + side))
        square.thumbnail((AVATAR_SIZE, AVATAR_SIZE))
        out = io.BytesIO()
        square.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise BadRequestError("File is not a valid image", code="invalid_image") from exc


# --- receipts (transaction attachments) ------------------------------------------------

MAX_RECEIPT_PIXELS = 4096 * 4096  # checked from the header, before decoding
RECEIPT_MAX_SIDE = 2000  # re-encoded images are downscaled to this long side
RECEIPT_JPEG_QUALITY = 85
# Budget for inflating PDF object streams while scanning them (zip-bomb guard).
_PDF_INFLATE_BUDGET = 20 * 1024 * 1024

_MAGIC_FORMATS: tuple[tuple[str, frozenset[str]], ...] = (
    ("jpeg", frozenset({"JPEG", "MPO"})),  # phone cameras often write MPO (multi-picture JPEG)
    ("png", frozenset({"PNG"})),
    ("webp", frozenset({"WEBP"})),
)


@dataclass(frozen=True)
class ProcessedFile:
    data: bytes
    ext: str
    content_type: str


def sniff_receipt_type(raw: bytes) -> str | None:
    """File type from magic bytes only (``jpeg`` / ``png`` / ``webp`` / ``pdf``), never from the
    client-supplied filename or MIME type."""
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw.startswith(b"%PDF-"):
        return "pdf"
    return None


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """RGB copy; transparent areas become white (receipts are documents)."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def _process_receipt_image(raw: bytes, kind: str) -> ProcessedFile:
    allowed = dict(_MAGIC_FORMATS)[kind]
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            width, height = probe.size
            # Image.open only parses the header; reject huge canvases before any decoding.
            if width * height > MAX_RECEIPT_PIXELS:
                raise BadRequestError(
                    "Image dimensions are too large (max 4096 x 4096 pixels)",
                    code="image_too_large",
                )
            probe.verify()
        if fmt not in allowed:  # e.g. PNG magic bytes but another format behind them
            raise BadRequestError("File content does not match its type", code="invalid_file")
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            upright = ImageOps.exif_transpose(source)  # honour camera rotation before stripping
            rgb = _flatten_to_rgb(upright)
        rgb.thumbnail((RECEIPT_MAX_SIDE, RECEIPT_MAX_SIDE))  # keeps aspect ratio, never upscales
        clean = Image.new("RGB", rgb.size)  # fresh image: no EXIF/GPS/ICC/comments carried over
        clean.paste(rgb)
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=RECEIPT_JPEG_QUALITY, optimize=True)
        return ProcessedFile(out.getvalue(), "jpg", "image/jpeg")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise BadRequestError("File is not a valid image", code="invalid_file") from exc


# PDF names (``/Name``) are delimited by whitespace or one of ``()<>[]{}/%``.
_PDF_NAME_RE = re.compile(rb"/([^\x00\t\n\x0c\r ()<>\[\]{}/%]+)")
_PDF_NAME_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
_PDF_STREAM_RE = re.compile(rb"(?<!end)stream(?:\r\n|\r|\n)(.*?)endstream", re.DOTALL)
# Active content / actions that can run code or open other files.
_PDF_ACTIVE_NAMES = frozenset(
    {b"JavaScript", b"JS", b"Launch", b"EmbeddedFile", b"EmbeddedFiles", b"RichMedia", b"XFA"}
)


def _pdf_names(chunk: bytes) -> set[bytes]:
    """Every name token in ``chunk`` with ``#xx`` escapes decoded (``/Java#53cript``)."""
    return {
        _PDF_NAME_ESCAPE_RE.sub(lambda m: bytes([int(m.group(1), 16)]), m.group(1))
        for m in _PDF_NAME_RE.finditer(chunk)
    }


def _check_pdf_names(names: set[bytes]) -> None:
    if b"Encrypt" in names:
        raise BadRequestError("Password-protected PDFs are not supported", code="pdf_encrypted")
    if names & _PDF_ACTIVE_NAMES:
        raise BadRequestError(
            "PDFs with scripts, launch actions or embedded files are not allowed",
            code="unsafe_pdf",
        )


def _process_receipt_pdf(raw: bytes) -> ProcessedFile:
    """Accept a PDF only if no dictionary declares active content.

    Dictionaries live outside stream bodies, except inside *object streams* (``/ObjStm``),
    which are inflated (within a budget) and scanned too. Other stream bodies (images, page
    content) are skipped so binary data can't produce false matches. Encrypted PDFs are
    rejected because their object streams can't be inspected.
    """
    if raw.rfind(b"%%EOF") == -1:
        raise BadRequestError("File is not a valid PDF", code="invalid_file")
    outside: list[bytes] = []
    budget = _PDF_INFLATE_BUDGET
    position = 0
    for match in _PDF_STREAM_RE.finditer(raw):
        header = raw[position : match.start()]
        outside.append(header)
        position = match.end()
        dictionary = header[header.rfind(b"obj") :] if b"obj" in header else header[-4096:]
        if b"/ObjStm" not in dictionary:
            continue
        try:
            inflated = zlib.decompressobj().decompress(match.group(1), budget + 1)
        except zlib.error as exc:
            raise BadRequestError("PDF could not be inspected", code="invalid_file") from exc
        budget -= len(inflated)
        if budget < 0:
            raise BadRequestError("PDF could not be inspected", code="invalid_file")
        _check_pdf_names(_pdf_names(inflated))
    outside.append(raw[position:])
    _check_pdf_names(_pdf_names(b"\n".join(outside)))
    return ProcessedFile(raw, "pdf", "application/pdf")


def process_receipt(raw: bytes, max_bytes: int) -> ProcessedFile:
    """Validate a receipt upload by its content and make it safe to serve.

    * JPEG / PNG / WebP → decoded (≤ 4096² px, checked before decoding), EXIF-rotated, flattened
      and re-encoded as JPEG (long side ≤ 2000 px). Re-encoding drops EXIF/GPS metadata and any
      trailing payload.
    * PDF → kept as is if it has a ``%PDF-`` header and declares no JavaScript, launch actions
      or embedded files (and isn't encrypted).

    CPU-bound: call it from a worker thread (``run_limited``).
    """
    if len(raw) > max_bytes:
        raise PayloadTooLargeError(f"File must be at most {max_bytes / 1024 / 1024:g} MB")
    if not raw:
        raise BadRequestError("Empty file", code="invalid_file")
    kind = sniff_receipt_type(raw)
    if kind is None:
        raise BadRequestError(
            "Only JPEG, PNG, WebP images or PDF documents are allowed",
            code="unsupported_file_type",
        )
    if kind == "pdf":
        return _process_receipt_pdf(raw)
    return _process_receipt_image(raw, kind)
