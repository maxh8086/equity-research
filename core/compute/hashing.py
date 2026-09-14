import hashlib


def content_hash(data: bytes) -> str:
    """The single definition of `content_hash`: lowercase hex SHA-256 of the source bytes."""
    return hashlib.sha256(data).hexdigest()
