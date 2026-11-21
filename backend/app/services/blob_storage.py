"""Vercel Blob implementation of :class:`app.services.storage.FileStorage` (private stores only).

Talks to the Blob HTTP API with httpx (no SDK), the same way the official SDKs do:

* upload: ``PUT {BLOB_API_URL}?pathname=<key>`` with ``x-vercel-blob-access: private``,
  ``x-content-type``, ``x-add-random-suffix: 0`` and ``x-allow-overwrite: 0``;
* download: ``GET https://<store-id>.private.blob.vercel-storage.com/<key>`` with the
  read-write token as a bearer token (private blobs are never readable without it);
* delete: ``POST {BLOB_API_URL}/delete`` with ``{"urls": [...]}``.

Keys are the same server-generated, unguessable ``<folder>/<random>.<ext>`` names the local
backend uses, so the database never stores a URL. Downloads always go through our own
authenticated endpoints, which stream the blob back; no blob URL is ever handed to a client.
Error messages never include the token.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from app.core.logging import get_logger
from app.services.storage import CONTENT_TYPES, StorageError, StoredFile, new_key, validate_key

log = get_logger(__name__)

DEFAULT_BLOB_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "13"  # what @vercel/blob 2.9 sends
DEFAULT_TIMEOUT_SECONDS = 20.0


def store_id_from_token(token: str) -> str:
    """``vercel_blob_rw_<storeId>_<secret>`` → ``<storeId>`` (how the SDKs derive it)."""
    parts = token.split("_")
    if len(parts) < 5 or parts[:3] != ["vercel", "blob", "rw"] or not parts[3].isalnum():
        raise ValueError("BLOB_READ_WRITE_TOKEN is not a Vercel Blob read-write token")
    return parts[3]


class VercelBlobStorage:
    """Private Vercel Blob store. One short-lived HTTP client per operation, so nothing is tied
    to an event loop that a serverless runtime may replace between invocations."""

    # Tests swap in an httpx.MockTransport here; production always uses the real network.
    default_transport: ClassVar[httpx.AsyncBaseTransport | None] = None

    def __init__(
        self,
        token: str,
        *,
        api_url: str = DEFAULT_BLOB_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self.store_id = store_id_from_token(token)
        self.api_url = api_url.rstrip("/")
        self._transport = transport if transport is not None else self.default_transport
        self._timeout = timeout

    # ------------------------------------------------------------------ helpers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        )

    def _api_headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "x-api-version": BLOB_API_VERSION,
            "x-vercel-blob-store-id": self.store_id,
        }

    def blob_url(self, key: str) -> str:
        """Private delivery URL. Only ever used server-side, with the token."""
        validate_key(key)  # keys are [a-z]+/[A-Za-z0-9_-]+.ext: nothing to percent-encode
        return f"https://{self.store_id}.private.blob.vercel-storage.com/{key}"

    @staticmethod
    def _fail(operation: str, response: httpx.Response | None = None) -> StorageError:
        status = response.status_code if response is not None else None
        log.error("blob_request_failed", operation=operation, status=status)
        return StorageError(f"Vercel Blob {operation} failed (HTTP {status})")

    # ------------------------------------------------------------------ FileStorage

    async def save(self, folder: str, data: bytes, ext: str) -> str:
        key = new_key(folder, ext)
        headers = {
            **self._api_headers(),
            "x-vercel-blob-access": "private",
            "x-content-type": CONTENT_TYPES[ext],
            "x-add-random-suffix": "0",  # the key is already random; keep it exact
            "x-allow-overwrite": "0",
            "x-content-length": str(len(data)),
            "content-type": "application/octet-stream",
        }
        try:
            async with self._client() as client:
                response = await client.put(
                    self.api_url, params={"pathname": key}, content=data, headers=headers
                )
        except httpx.HTTPError as exc:
            raise self._fail("upload") from exc
        if response.status_code != 200:
            raise self._fail("upload", response)
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise self._fail("upload", response) from exc
        if body.get("pathname") != key:
            # Never store a key that doesn't name the blob we just wrote.
            raise self._fail("upload", response)
        return key

    async def open(self, key: str) -> StoredFile | None:
        url = self.blob_url(key)
        client = self._client()
        try:
            request = client.build_request(
                "GET",
                url,
                headers={"authorization": f"Bearer {self._token}", "accept-encoding": "identity"},
            )
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise self._fail("download") from exc

        async def close() -> None:
            await response.aclose()
            await client.aclose()

        if response.status_code == 404:
            await close()
            return None
        if response.status_code != 200:
            await close()
            raise self._fail("download", response)
        return StoredFile(chunks=response.aiter_bytes(), close=close)

    async def delete(self, key: str) -> None:
        url = self.blob_url(key)
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.api_url}/delete", json={"urls": [url]}, headers=self._api_headers()
                )
        except httpx.HTTPError as exc:
            raise self._fail("delete") from exc
        if response.status_code != 200:  # deleting a missing blob succeeds (200)
            raise self._fail("delete", response)
