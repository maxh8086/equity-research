from core.compute.hashing import content_hash


class MemoryBlobStore:
    """In-memory BlobStore for tests that exercise adapters, not storage."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        key = content_hash(data)
        self.objects.setdefault(key, data)
        return key

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class FailingBlobStore(MemoryBlobStore):
    def put(self, data: bytes) -> str:
        raise ConnectionError("blob store down")
