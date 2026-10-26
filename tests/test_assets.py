"""Asset store: handles, hashes, integrity."""

import hashlib

import pytest

from agenthand.assets import AssetStore


def test_put_and_read_roundtrip(tmp_path):
    store = AssetStore(tmp_path)
    payload = b"\x89PNG fake image bytes"
    handle = store.put_bytes(payload, kind="source_photo", suffix=".png")

    assert handle.startswith("asset://")
    assert store.read_bytes(handle) == payload
    assert store.resolve(handle).exists()


def test_sha256_recorded_at_ingest(tmp_path):
    store = AssetStore(tmp_path)
    payload = b"product photo bytes"
    handle = store.put_bytes(payload, kind="source_photo")
    assert store.sha256_of(handle) == hashlib.sha256(payload).hexdigest()


def test_verify_integrity_detects_tampering(tmp_path):
    store = AssetStore(tmp_path)
    handle = store.put_bytes(b"original", kind="foreground_png", suffix=".png")
    assert store.verify_integrity(handle) is True

    store.resolve(handle).write_bytes(b"regenerated - not the real product")
    assert store.verify_integrity(handle) is False


def test_derived_asset_records_its_source(tmp_path):
    store = AssetStore(tmp_path)
    source = store.put_bytes(b"photo", kind="source_photo")
    source_id = AssetStore.parse_handle(source)
    cut = store.put_bytes(
        b"cutout", kind="foreground_png", suffix=".png", source_asset_id=source_id
    )
    assert store.get_record(cut).source_asset_id == source_id


def test_bad_handles_raise(tmp_path):
    store = AssetStore(tmp_path)
    with pytest.raises(ValueError):
        store.get_record("https://example.com/not-a-handle")
    with pytest.raises(KeyError):
        store.get_record("asset://does-not-exist")
