"""The Fidelity Lock engine: deterministic compositing, no models.

The invariant, enforced here in code (not just promised in docs):

- The product layer is the stored foreground asset, loaded byte-for-byte
  and pixel-for-pixel from the asset store after an integrity check. It is
  NEVER scaled, re-encoded through a model, or regenerated.
- The background plate is the only generated element. When a real image
  model is wired in, it produces the plate; the plate function takes ONLY
  a prompt and dimensions, so product pixels cannot reach the generator.
- The plate is resized/cropped to the product's dimensions — never the
  reverse — and deterministic alpha compositing places the product layer
  over it.
- The receipt measures the result: pixels where the foreground alpha is
  fully opaque must be identical between the stored foreground and the
  composite (count reported, and it must be 0 for the claim to hold);
  edge-band pixels (0 < alpha < 255) blend by design and are counted
  separately.

Until the background model is wired, generate_background_plate returns a
deterministic local placeholder plate derived from the prompt, so the
whole pipeline runs and is testable offline with zero API calls.
"""

from __future__ import annotations

import hashlib
from io import BytesIO

from PIL import Image, ImageOps

from .assets import (
    HANDLE_PREFIX,
    KIND_COMPOSITE,
    KIND_FOREGROUND,
    KIND_MASK,
    KIND_PLATE,
    KIND_SOURCE,
    AssetRecord,
    AssetStore,
    pixel_hash_of_image,
)


class FidelityLockError(RuntimeError):
    """Raised when an asset cannot be composited under the Fidelity Lock."""


def generate_background_plate(prompt: str, width: int, height: int) -> Image.Image:
    """Produce a background plate from a prompt and dimensions ONLY.

    This signature is the Fidelity Lock contract for background
    generation: no product bytes, no product handle, no product pixels can
    be passed in, so the generator cannot touch the product layer. When
    muse-image-1.0 is wired, this function is where its output lands.

    Until then it returns a deterministic placeholder: a vertical
    gradient whose colors derive from the prompt hash. Same prompt, same
    plate, no API call.
    """
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    top = tuple(digest[0:3])
    bottom = tuple(digest[3:6])
    plate = Image.new("RGBA", (width, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        plate.paste(color, (0, y, width, y + 1))
    return plate


def _fit_plate(plate: Image.Image, size: tuple[int, int]) -> tuple[Image.Image, list[str]]:
    """Resize/crop the PLATE to the product's dimensions. Never the reverse."""
    if plate.size == size:
        return plate, []
    fitted = ImageOps.fit(plate, size)
    return fitted, [f"plate_resized_and_center_cropped_to_{size[0]}x{size[1]}"]


def _measure(foreground: Image.Image, composite: Image.Image) -> dict[str, int]:
    """Count opaque / edge-band / transparent pixels and verify the lock.

    A foreground pixel with alpha 255 must appear in the composite
    byte-identical; the count of violations is measured, not assumed.
    """
    fg_bytes = foreground.tobytes()
    out_bytes = composite.tobytes()
    alpha = foreground.getchannel("A").tobytes()
    opaque = edge_band = transparent = changed_opaque = 0
    for i, a in enumerate(alpha):
        if a == 255:
            opaque += 1
            j = i * 4
            if fg_bytes[j : j + 4] != out_bytes[j : j + 4]:
                changed_opaque += 1
        elif a > 0:
            edge_band += 1
        else:
            transparent += 1
    return {
        "opaque_pixels": opaque,
        "edge_band_pixels": edge_band,
        "transparent_pixels": transparent,
        "changed_opaque_pixels": changed_opaque,
    }


def composite_images(
    foreground: Image.Image, plate: Image.Image
) -> tuple[Image.Image, dict, list[str]]:
    """Composite a foreground over a plate; returns (image, counts, transforms)."""
    fg = foreground.convert("RGBA")
    fitted, transformations = _fit_plate(plate.convert("RGBA"), fg.size)
    out = Image.alpha_composite(fitted, fg)
    return out, _measure(fg, out), transformations


def validate_foreground(store: AssetStore, foreground_handle: str) -> list[AssetRecord]:
    """Check an asset may be composited; returns its lineage chain.

    Raises FidelityLockError (or KeyError for an unknown handle) unless
    the asset is a foreground_png whose lineage ends at a source_photo
    and the stored bytes of both match their recorded hashes.
    """
    record = store.get_record(foreground_handle)
    if record.kind != KIND_FOREGROUND:
        raise FidelityLockError(
            f"draw requires a {KIND_FOREGROUND} asset (the cut-out product "
            f"layer); {foreground_handle!r} is kind {record.kind!r}. "
            "Compositing anything else would break the Fidelity Lock."
        )
    chain = store.lineage(foreground_handle)
    source_record = chain[-1]
    if source_record.kind != KIND_SOURCE:
        raise FidelityLockError(
            f"Foreground {foreground_handle!r} traces back to kind "
            f"{source_record.kind!r}, not a {KIND_SOURCE}. Its provenance "
            "cannot be verified, so it cannot be composited."
        )
    # The stored bytes must be exactly what was recorded. Raises on mismatch.
    store.verify_integrity(foreground_handle)
    if source_record.asset_id != record.asset_id:
        store.verify_integrity(HANDLE_PREFIX + source_record.asset_id)
    return chain


def draw_product(
    store: AssetStore,
    foreground_handle: str,
    scene_prompt: str,
    *,
    plate_handle: str | None = None,
) -> tuple[str, dict]:
    """Composite a stored foreground into a scene; returns (handle, receipt).

    Enforces the Fidelity Lock before any pixels move: the asset must be
    a foreground_png, its lineage must end at a source_photo, and the
    stored bytes must match their recorded hashes.
    """
    chain = validate_foreground(store, foreground_handle)
    record = chain[0]
    source_record = chain[-1]

    foreground = store.read_image(foreground_handle)

    mask_record = next((r for r in chain if r.kind == KIND_MASK), None)
    if mask_record is not None:
        mask_asset = HANDLE_PREFIX + mask_record.asset_id
        mask_sha256 = mask_record.sha256
        mask_pixel_sha256 = mask_record.pixel_sha256
        mask_source = "asset"
    else:
        # No separate mask asset in the chain: the foreground's own alpha
        # channel is the mask. Hash the derived mask so the receipt still
        # pins exactly which mask the composite used.
        mask_image = foreground.getchannel("A")
        buffer = BytesIO()
        mask_image.save(buffer, format="PNG")
        mask_asset = None
        mask_sha256 = hashlib.sha256(buffer.getvalue()).hexdigest()
        mask_pixel_sha256 = pixel_hash_of_image(mask_image)
        mask_source = "foreground_alpha"

    if plate_handle is not None:
        plate_record = store.get_record(plate_handle)
        if plate_record.kind != KIND_PLATE:
            raise FidelityLockError(
                f"Background plate {plate_handle!r} is kind "
                f"{plate_record.kind!r}, not {KIND_PLATE}."
            )
        plate = store.read_image(plate_handle)
        plate_source = "asset"
    else:
        plate = generate_background_plate(
            scene_prompt, foreground.width, foreground.height
        )
        plate_source = "local_placeholder"

    composite, counts, transformations = composite_images(foreground, plate)
    composite_handle = store.put_image(
        composite, kind=KIND_COMPOSITE, source_asset_id=record.asset_id
    )

    receipt = {
        "source_asset": HANDLE_PREFIX + source_record.asset_id,
        "source_sha256": source_record.sha256,
        "source_pixel_sha256": source_record.pixel_sha256,
        "foreground_asset": foreground_handle,
        "foreground_sha256": record.sha256,
        "foreground_pixel_sha256": record.pixel_sha256,
        "mask_asset": mask_asset,
        "mask_sha256": mask_sha256,
        "mask_pixel_sha256": mask_pixel_sha256,
        "mask_source": mask_source,
        "changed_opaque_pixels": counts["changed_opaque_pixels"],
        "opaque_pixels": counts["opaque_pixels"],
        "edge_band_pixels": counts["edge_band_pixels"],
        "transparent_pixels": counts["transparent_pixels"],
        "transformations": transformations,
        "product_layer_scaled": False,
        "plate_source": plate_source,
        "claim": (
            "The stored extracted foreground reached the composite "
            "unchanged: every fully opaque foreground pixel in the "
            "composite is identical to the stored foreground layer "
            "(foreground hashes above pin that layer). Edge-band pixels "
            "blend with the plate by design and are counted separately."
        ),
    }
    return composite_handle, receipt
