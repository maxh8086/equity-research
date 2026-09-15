import pytest
from botocore.exceptions import ClientError

from core.blob import object_key
from core.compute.hashing import content_hash

pytestmark = pytest.mark.blob


def test_round_trip_keyed_by_content_hash(s3_store):
    data = b"nifty 50 constituents, 2024-03-28"
    key = s3_store.put(data)
    assert key == content_hash(data)
    assert s3_store.exists(key)
    assert s3_store.get(key) == data


def test_putting_same_bytes_again_is_a_no_op(s3_store):
    data = b"same bytes twice"
    assert s3_store.put(data) == s3_store.put(data)


def test_missing_key(s3_store):
    key = content_hash(b"never stored \x00\x01")
    assert not s3_store.exists(key)
    with pytest.raises(KeyError):
        s3_store.get(key)


def test_existing_object_cannot_be_replaced(s3_store):
    key = s3_store.put(b"original")
    with pytest.raises(ClientError) as exc:
        s3_store.client.put_object(
            Bucket=s3_store.bucket, Key=object_key(key), Body=b"tampered", IfNoneMatch="*"
        )
    assert exc.value.response["Error"]["Code"] in {"PreconditionFailed", "412"}
    assert s3_store.get(key) == b"original"


def test_bucket_has_object_lock_with_default_retention(s3_store):
    config = s3_store.client.get_object_lock_configuration(Bucket=s3_store.bucket)
    lock = config["ObjectLockConfiguration"]
    assert lock["ObjectLockEnabled"] == "Enabled"
    assert lock["Rule"]["DefaultRetention"]["Mode"] == "GOVERNANCE"


def test_malformed_key_rejected(s3_store):
    with pytest.raises(ValueError):
        s3_store.get("../../etc/passwd")
