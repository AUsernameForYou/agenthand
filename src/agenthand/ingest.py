"""Image ingest: how a seller's photo becomes an asset handle.

Tools accept an image argument in any of three forms:

- an existing asset handle (``asset://<id>``) — validated and passed
  through unchanged;
- an http(s) URL — fetched (bounded by the byte cap and a timeout);
- a base64 data URI (``data:image/png;base64,...``).

Ingest enforces byte and pixel caps, then NORMALIZES: EXIF orientation is
applied, the image is converted to 8-bit RGBA (sRGB-family) and re-encoded
as PNG. Normalizing at the boundary means every downstream step — hashing,
segmentation, compositing — sees one canonical pixel layout, and the
pixel hash recorded at ingest describes the pixels tools will actually
work with.
"""

from __future__ import annotations

import base64
import binascii
import os
import urllib.request
from io import BytesIO

from .assets import HANDLE_PREFIX, KIND_SOURCE, AssetStore

# Caps, overridable by environment for tests and tight deployments.
DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB of encoded input
DEFAULT_MAX_PIXELS = 40_000_000  # 40 megapixels decoded
_FETCH_TIMEOUT_SECONDS = 15


class IngestError(ValueError):
    """Raised when an image source cannot be accepted."""


def _max_image_bytes() -> int:
    raw = os.environ.get("AGENTHAND_MAX_IMAGE_BYTES", "").strip()
    return int(raw) if raw else DEFAULT_MAX_IMAGE_BYTES


def _max_pixels() -> int:
    raw = os.environ.get("AGENTHAND_MAX_PIXELS", "").strip()
    return int(raw) if raw else DEFAULT_MAX_PIXELS


def normalize_image_bytes(data: bytes) -> bytes:
    """Decode, cap-check, normalize, and re-encode image bytes as PNG.

    Raises IngestError on undecodable data or a pixel count over the cap.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
            if img.width * img.height > _max_pixels():
                raise IngestError(
                    f"Image is {img.width}x{img.height} "
                    f"({img.width * img.height} pixels), over the "
                    f"{_max_pixels()}-pixel ingest cap."
                )
            normalized = ImageOps.exif_transpose(img).convert("RGBA")
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"Input does not decode as an image: {exc}") from exc
    buffer = BytesIO()
    normalized.save(buffer, format="PNG")
    return buffer.getvalue()


def _decode_data_uri(source: str) -> bytes:
    header, sep, payload = source.partition(",")
    if not sep or ";base64" not in header or not header.startswith("data:image/"):
        raise IngestError(
            "Only base64 image data URIs are accepted "
            "(data:image/<type>;base64,...)."
        )
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IngestError(f"Data URI payload is not valid base64: {exc}") from exc


def _fetch_url(source: str) -> bytes:
    request = urllib.request.Request(
        source, headers={"User-Agent": "agenthand-ingest/0.1"}
    )
    limit = _max_image_bytes()
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise IngestError(f"Fetched image exceeds the {limit}-byte ingest cap.")
    return data


def ingest(source: str, store: AssetStore, *, kind: str = KIND_SOURCE) -> str:
    """Bring an image into the store; returns its asset:// handle.

    ``source`` is an asset handle, an http(s) URL, or a base64 data URI.
    Oversize or undecodable input raises IngestError; nothing is stored
    for a rejected image.
    """
    if not isinstance(source, str) or not source.strip():
        raise IngestError(f"Image source must be a non-empty string, got {source!r}")

    if source.startswith(HANDLE_PREFIX):
        store.get_record(source)  # raises KeyError for an unknown handle
        return source

    if source.startswith("data:"):
        data = _decode_data_uri(source)
    elif source.startswith(("http://", "https://")):
        data = _fetch_url(source)
    else:
        raise IngestError(
            "Image source must be an asset:// handle, an http(s) URL, or a "
            f"base64 data URI; got {source[:40]!r}..."
        )

    if len(data) > _max_image_bytes():
        raise IngestError(
            f"Image is {len(data)} bytes, over the {_max_image_bytes()}-byte "
            "ingest cap."
        )
    return store.put_bytes(normalize_image_bytes(data), kind=kind, suffix=".png")
