"""S3-compatible implementation of :class:`app.services.storage.FileStorage`.

Built for Neon's S3-compatible object storage (works with any S3 API: AWS, R2, MinIO...).
Requests are signed with AWS Signature Version 4 by hand and sent with httpx, so everything is
async and downloads stream (no boto3 thread hop, nothing tied to an event loop between
serverless invocations). Path-style addressing: ``{S3_ENDPOINT_URL}/{bucket}/{key}``.

Objects are private: they are written without any ACL (bucket default = private), no URL or
presigned link ever leaves the server, and downloads stream through our authenticated API
endpoints. Error messages never contain credentials.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote, urlsplit

import httpx

from app.core.logging import get_logger
from app.services.storage import CONTENT_TYPES, StorageError, StoredFile, new_key, validate_key

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    url: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    payload_sha256: str,
    headers: dict[str, str] | None = None,
    now: datetime | None = None,
    service: str = "s3",
) -> dict[str, str]:
    """Headers (incl. ``authorization``) that sign ``method url`` with AWS SigV4.

    ``url`` must already be percent-encoded the way it will be sent (path segments encoded
    with ``quote(segment, safe="")``-style rules; we only use [A-Za-z0-9_.-/] in keys).
    """
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    parts = urlsplit(url)
    host = parts.netloc
    canonical_uri = parts.path or "/"
    query_pairs = []
    if parts.query:
        for item in parts.query.split("&"):
            name, _, value = item.partition("=")
            query_pairs.append((name, value))
    canonical_query = "&".join(f"{n}={v}" for n, v in sorted(query_pairs))

    signed: dict[str, str] = {
        **{k.lower(): v.strip() for k, v in (headers or {}).items()},
        "host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    names = sorted(signed)
    canonical_headers = "".join(f"{n}:{signed[n]}\n" for n in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = _hmac(f"AWS4{secret_access_key}".encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in signed.items() if k != "host"}  # httpx sets Host itself
    out["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


class S3Storage:
    """Private bucket on an S3-compatible service, path-style, SigV4-signed."""

    # Tests swap in an httpx.MockTransport here; production always uses the real network.
    default_transport: ClassVar[httpx.AsyncBaseTransport | None] = None

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not endpoint_url.startswith("https://") and not endpoint_url.startswith("http://"):
            raise ValueError("S3_ENDPOINT_URL must be an http(s) URL")
        self.endpoint = endpoint_url.rstrip("/")
        self.bucket = bucket
        self.region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._transport = transport if transport is not None else self.default_transport
        self._timeout = timeout

    # ------------------------------------------------------------------ helpers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        )

    def object_url(self, key: str) -> str:
        validate_key(key)
        return f"{self.endpoint}/{quote(self.bucket, safe='')}/{quote(key, safe='/')}"

    def _signed(
        self,
        method: str,
        url: str,
        *,
        payload_sha256: str = EMPTY_SHA256,
        headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        return sigv4_headers(
            method=method,
            url=url,
            region=self.region,
            access_key_id=self._access_key_id,
            secret_access_key=self._secret_access_key,
            payload_sha256=payload_sha256,
            headers=headers,
        )

    @staticmethod
    def _fail(operation: str, response: httpx.Response | None = None) -> StorageError:
        status = response.status_code if response is not None else None
        log.error("s3_request_failed", operation=operation, status=status)
        return StorageError(f"Object storage {operation} failed (HTTP {status})")

    # ------------------------------------------------------------------ FileStorage

    async def save(self, folder: str, data: bytes, ext: str) -> str:
        key = new_key(folder, ext)
        url = self.object_url(key)
        extra = {"content-type": CONTENT_TYPES[ext]}
        headers = self._signed(
            "PUT", url, payload_sha256=hashlib.sha256(data).hexdigest(), headers=extra
        )
        try:
            async with self._client() as client:
                response = await client.put(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise self._fail("upload") from exc
        if response.status_code != 200:
            raise self._fail("upload", response)
        return key

    async def open(self, key: str) -> StoredFile | None:
        url = self.object_url(key)
        client = self._client()
        try:
            # Unsigned accept-encoding: stream the stored bytes as they are.
            headers = {**self._signed("GET", url), "accept-encoding": "identity"}
            request = client.build_request("GET", url, headers=headers)
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
        url = self.object_url(key)
        try:
            async with self._client() as client:
                response = await client.delete(url, headers=self._signed("DELETE", url))
        except httpx.HTTPError as exc:
            raise self._fail("delete") from exc
        if response.status_code not in (200, 204, 404):  # S3 answers 204 even when missing
            raise self._fail("delete", response)
