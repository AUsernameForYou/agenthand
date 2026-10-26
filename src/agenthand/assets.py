"""The asset store: images live on disk, tools pass handles.

Every image agenthand touches is stored as a file under the data dir and
referenced everywhere else as ``asset://<id>``. Each asset has a sidecar
JSON record (``<id>.json``, written via temp file + os.replace so a crash
mid-write cannot corrupt the store) next to its data file. A legacy
``assets.json`` index from the first scaffold is still READ as a fallback
so old data dirs keep working; new writes are sidecars only.

Two hashes are recorded per asset:

- ``sha256``: the hash of the stored file bytes.
- ``pixel_sha256``: the hash of the decoded RGBA pixel bytes, recorded
  when the asset decodes as an image. File bytes can change without the
  pixels changing (re-encoding) and pixels can change under the same file
  hash's blind spots (metadata-driven decode differences), so the
  Fidelity Lock checks both.

Lineage: each record names the asset it was derived from
(``source_asset_id``), forming a chain back to a source photo. The draw
tool refuses a foreground whose chain does not end at a source photo.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

HANDLE_PREFIX = "asset://"

# Asset kinds. draw only accepts KIND_FOREGROUND, chained to KIND_SOURCE.
KIND_SOURCE = "source_photo"
KIND_FOREGROUND = "foreground_png"
KIND_MASK = "mask_png"
KIND_PLATE = "background_plate"
KIND_COMPOSITE = "composite"


class AssetIntegrityError(RuntimeError):
    """Raised when stored bytes no longer match their recorded hashes."""


@dataclass
class AssetRecord:
    asset_id: str
    sha256: str
    kind: str  # one of the KIND_* constants
    filename: str
    byte_size: int
    created_at: str
    source_asset_id: str | None = None  # the asset this one was derived from
    pixel_sha256: str | None = None  # hash of decoded RGBA bytes, if an image
    width: int | None = None
    height: int | None = None


def pixel_hash_of_image(image) -> str:
    """Hash of an image's decoded RGBA pixel bytes (PIL Image input)."""
    return hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()


def _image_facts(data: bytes) -> tuple[str | None, int | None, int | None]:
    """(pixel_sha256, width, height) if data decodes as an image, else Nones."""
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            img.load()
            return pixel_hash_of_image(img), img.width, img.height
    except Exception:
        return None, None, None


class AssetStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.assets_dir = self.data_dir / "assets"
        self.index_path = self.data_dir / "assets.json"  # legacy, read-only

    # -- internal record handling -------------------------------------------

    def _sidecar_path(self, asset_id: str) -> Path:
        return self.assets_dir / f"{asset_id}.json"

    def _load_legacy_index(self) -> dict[str, dict]:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _load_record(self, asset_id: str) -> AssetRecord:
        sidecar = self._sidecar_path(asset_id)
        if sidecar.exists():
            return AssetRecord(**json.loads(sidecar.read_text(encoding="utf-8")))
        legacy = self._load_legacy_index()
        if asset_id in legacy:
            return AssetRecord(**legacy[asset_id])
        raise KeyError(f"Unknown asset id: {asset_id!r}")

    def _save_record(self, record: AssetRecord) -> None:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        sidecar = self._sidecar_path(record.asset_id)
        tmp = sidecar.with_suffix(f".json.tmp-{uuid.uuid4().hex}")
        tmp.write_text(
            json.dumps(asdict(record), indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, sidecar)

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
        tmp = self.assets_dir / f"{filename}.tmp-{uuid.uuid4().hex}"
        tmp.write_bytes(data)
        os.replace(tmp, self.assets_dir / filename)

        pixel_sha256, width, height = _image_facts(data)
        record = AssetRecord(
            asset_id=asset_id,
            sha256=hashlib.sha256(data).hexdigest(),
            kind=kind,
            filename=filename,
            byte_size=len(data),
            created_at=datetime.now(timezone.utc).isoformat(),
            source_asset_id=source_asset_id,
            pixel_sha256=pixel_sha256,
            width=width,
            height=height,
        )
        self._save_record(record)
        return HANDLE_PREFIX + asset_id

    def put_image(
        self,
        image,
        *,
        kind: str,
        source_asset_id: str | None = None,
    ) -> str:
        """Store a PIL Image as a PNG asset; returns its asset:// handle."""
        buffer = BytesIO()
        image.convert("RGBA").save(buffer, format="PNG")
        return self.put_bytes(
            buffer.getvalue(), kind=kind, suffix=".png", source_asset_id=source_asset_id
        )

    def get_record(self, handle: str) -> AssetRecord:
        return self._load_record(self.parse_handle(handle))

    def resolve(self, handle: str) -> Path:
        record = self.get_record(handle)
        return self.assets_dir / record.filename

    def read_bytes(self, handle: str) -> bytes:
        return self.resolve(handle).read_bytes()

    def read_image(self, handle: str):
        """Decode the asset as a PIL Image in RGBA mode."""
        from PIL import Image

        with Image.open(BytesIO(self.read_bytes(handle))) as img:
            return img.convert("RGBA")

    def sha256_of(self, handle: str) -> str:
        return self.get_record(handle).sha256

    def lineage(self, handle: str) -> list[AssetRecord]:
        """The derivation chain for a handle: [record, parent, ..., root].

        Raises KeyError if any link in the chain is missing; a chain that
        cannot be walked to its root must not be trusted by draw.
        """
        chain: list[AssetRecord] = []
        seen: set[str] = set()
        record = self.get_record(handle)
        while True:
            if record.asset_id in seen:
                raise AssetIntegrityError(
                    f"Cycle in asset lineage at {record.asset_id!r}."
                )
            seen.add(record.asset_id)
            chain.append(record)
            if record.source_asset_id is None:
                return chain
            record = self._load_record(record.source_asset_id)

    def verify_integrity(self, handle: str) -> None:
        """Re-hash the stored asset and RAISE on any mismatch.

        Checks the file-byte hash and, when one was recorded, the pixel
        hash. This is the check the Fidelity Lock receipt relies on: if
        the stored product layer no longer matches the hashes recorded
        when segmentation produced it, it must never be presented as the
        untouched product. Returns None on success; raises
        AssetIntegrityError (or KeyError for an unknown handle).
        """
        record = self.get_record(handle)
        data = self.read_bytes(handle)
        actual_file = hashlib.sha256(data).hexdigest()
        if actual_file != record.sha256:
            raise AssetIntegrityError(
                f"Asset {handle!r} file hash mismatch: recorded "
                f"{record.sha256}, actual {actual_file}. The stored bytes "
                "were modified after ingest."
            )
        if record.pixel_sha256 is not None:
            pixel_sha256, _, _ = _image_facts(data)
            if pixel_sha256 != record.pixel_sha256:
                raise AssetIntegrityError(
                    f"Asset {handle!r} pixel hash mismatch: recorded "
                    f"{record.pixel_sha256}, actual {pixel_sha256}. The "
                    "decoded pixels differ from what was stored."
                )
