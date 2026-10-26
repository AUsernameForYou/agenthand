"""The asset store: images live on disk, tools pass handles.

Every image agenthand touches is stored as a file under the data dir and
referenced everywhere else as ``asset://<id>``. The store records the
sha256 of each asset's bytes at ingest. Those hashes are the backbone of the
Fidelity Lock: the draw tool can prove the product layer it composited is
byte-identical to the foreground the segmentation step extracted, because
both are checked against the hashes recorded here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

HANDLE_PREFIX = "asset://"


@dataclass
class AssetRecord:
    asset_id: str
    sha256: str
    kind: str  # e.g. "source_photo", "foreground_png", "mask", "composite"
    filename: str
    byte_size: int
    created_at: str
    source_asset_id: str | None = None  # the asset this one was derived from


class AssetStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.assets_dir = self.data_dir / "assets"
        self.index_path = self.data_dir / "assets.json"

    # -- internal index handling -------------------------------------------

    def _load_index(self) -> dict[str, dict]:
        if not self.index_path.exists():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, index: dict[str, dict]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
        )

    # -- public API ---------------------------------------------------------

    @staticmethod
    def parse_handle(handle: str) -> str:
        if not isinstance(handle, str) or not handle.startswith(HANDLE_PREFIX):
            raise ValueError(f"Not an asset handle: {handle!r}")
        asset_id = handle[len(HANDLE_PREFIX):].strip()
        if not asset_id:
            raise ValueError(f"Empty asset id in handle: {handle!r}")
        return asset_id

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: str,
        suffix: str = ".bin",
        source_asset_id: str | None = None,
    ) -> str:
        """Store bytes as a new asset; returns its asset:// handle."""
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        asset_id = uuid.uuid4().hex
        filename = f"{asset_id}{suffix}"
        (self.assets_dir / filename).write_bytes(data)

        record = AssetRecord(
            asset_id=asset_id,
            sha256=hashlib.sha256(data).hexdigest(),
            kind=kind,
            filename=filename,
            byte_size=len(data),
            created_at=datetime.now(timezone.utc).isoformat(),
            source_asset_id=source_asset_id,
        )
        index = self._load_index()
        index[asset_id] = asdict(record)
        self._save_index(index)
        return HANDLE_PREFIX + asset_id

    def get_record(self, handle: str) -> AssetRecord:
        asset_id = self.parse_handle(handle)
        index = self._load_index()
        if asset_id not in index:
            raise KeyError(f"Unknown asset handle: {handle!r}")
        return AssetRecord(**index[asset_id])

    def resolve(self, handle: str) -> Path:
        record = self.get_record(handle)
        return self.assets_dir / record.filename

    def read_bytes(self, handle: str) -> bytes:
        return self.resolve(handle).read_bytes()

    def sha256_of(self, handle: str) -> str:
        return self.get_record(handle).sha256

    def verify_integrity(self, handle: str) -> bool:
        """Re-hash the stored file and compare against the recorded hash.

        This is the check the Fidelity Lock receipt relies on: if the bytes
        on disk no longer match the hash recorded at ingest, the asset was
        modified and must not be presented as the untouched product layer.
        """
        record = self.get_record(handle)
        actual = hashlib.sha256(self.read_bytes(handle)).hexdigest()
        return actual == record.sha256
