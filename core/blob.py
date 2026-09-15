"""Raw source file storage. Every backend goes through `BlobStore`.

Objects are keyed by `content_hash` and written once; the bucket has object
lock enabled so stored bytes cannot be deleted. `S3BlobStore` serves MinIO on
the laptop and S3-compatible cloud storage. Azure Blob is not S3-compatible
and gets its own implementation of the protocol when needed.
"""

import re
from typing import Protocol

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from core.compute.hashing import content_hash
from core.config import Settings

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BlobIntegrityError(Exception):
    """Stored bytes no longer hash to their key."""


class BlobStore(Protocol):
    def put(self, data: bytes) -> str:
        """Store `data` once and return its content_hash. Storing the same bytes again is a no-op."""
        ...

    def get(self, key: str) -> bytes:
        """The bytes stored under content_hash `key`, verified against it. KeyError if absent."""
        ...

    def exists(self, key: str) -> bool: ...


def object_key(key: str) -> str:
    if not _SHA256.match(key):
        raise ValueError(f"blob key must be a lowercase hex SHA-256, got {key!r}")
    return f"sha256/{key[:2]}/{key}"


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class S3BlobStore:
    def __init__(self, client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def put(self, data: bytes) -> str:
        key = content_hash(data)
        try:
            # IfNoneMatch: never replace an existing object, even with identical bytes.
            self.client.put_object(
                Bucket=self.bucket, Key=object_key(key), Body=data, IfNoneMatch="*"
            )
        except ClientError as exc:
            if _error_code(exc) not in {"PreconditionFailed", "412"}:
                raise
        return key

    def get(self, key: str) -> bytes:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=object_key(key))["Body"].read()
        except ClientError as exc:
            if _error_code(exc) in {"NoSuchKey", "404"}:
                raise KeyError(key) from exc
            raise
        if content_hash(body) != key:
            raise BlobIntegrityError(f"object {key} does not hash to its key")
        return body

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=object_key(key))
        except ClientError as exc:
            if _error_code(exc) in {"NoSuchKey", "NotFound", "404"}:
                return False
            raise
        return True


def s3_blob_store(settings: Settings, *, bucket: str | None = None) -> S3BlobStore:
    client = boto3.client(
        "s3",
        endpoint_url=settings.blob_endpoint_url,
        region_name=settings.blob_region,
        aws_access_key_id=settings.blob_access_key,
        aws_secret_access_key=settings.blob_secret_key.get_secret_value(),
        config=BotoConfig(
            s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}
        ),
    )
    return S3BlobStore(client, bucket or settings.blob_bucket)
