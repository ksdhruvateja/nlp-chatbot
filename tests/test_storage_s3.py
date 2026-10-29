import os

import pytest
from cryptography.fernet import Fernet

moto = pytest.importorskip("moto")


def test_s3_store_roundtrip_encrypted(monkeypatch):
    # Moto supplies fake credentials; never depend on the developer's AWS profile.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    from moto import mock_aws
    with mock_aws():
        import boto3
        from app.storage import S3EncryptedStore
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="fb-media")
        store = S3EncryptedStore("fb-media", "media", Fernet.generate_key().decode(), region="us-east-1")
        key, sha = store.put(b"\xff\xd8\xffjpegdata")
        raw = store.s3.get_object(Bucket="fb-media", Key=f"media/{key}")
        assert raw["ServerSideEncryption"] == "AES256"
        assert not raw["Body"].read().startswith(b"\xff\xd8")      # client-side encrypted too
        assert store.get(key, sha) == b"\xff\xd8\xffjpegdata"
        with pytest.raises(ValueError):
            store.get("../etc/passwd")
