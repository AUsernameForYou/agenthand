"""Standalone verification of exported files — no store, no models.

The J2 Gauntlet's demo fallback and must-fix 2: a small, independent
checker that works on the DELIVERED files on disk, so the fidelity
claim can be re-proven anywhere (on camera, on a judge's machine)
without the asset store or the code that did the compositing.

    agenthand-verify SOURCE FINAL MASK
    python -m agenthand.verify SOURCE FINAL MASK

SOURCE is the product photo (as exported from the store — already
EXIF-normalized by ingest), FINAL is the delivered composite, MASK is
the cut_out mask PNG. The product region is where the mask is fully on
(mask value 255): at those pixels the final must equal the source
EXACTLY — same canvas, integer offset zero, no tolerance, no colour
conversion. Semi-transparent mask pixels are the edge band: counted,
not compared. The check also compares the embedded ICC colour
profiles of source and final: identical pixel values under a changed
or stripped profile display as different colours, so a profile
mismatch fails verification even when every compared pixel matches.

Exit code 0 when verification passes, 1 when it fails; the JSON
report prints either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

from .assets import (
    color_profile_sha256_of_image,
    describe_color_profile,
    icc_profile_of_image,
)


def _load_rgba(path: str | Path) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        converted = img.convert("RGBA")
        icc = img.info.get("icc_profile")
        if icc:
            converted.info["icc_profile"] = bytes(icc)
        return converted


def verify_files(
    source_path: str | Path, final_path: str | Path, mask_path: str | Path
) -> dict:
    """Verify an exported composite against its source photo and mask.

    Returns the report dict (see the module docstring for the rules);
    ``ok`` is True only when sizes match, every fully-masked product
    pixel is byte-identical between source and final, and the colour
    profile is preserved. Undecodable or missing files raise — the
    report is for files that exist but fail the fidelity rules.
    """
    source = _load_rgba(source_path)
    final = _load_rgba(final_path)
    with Image.open(mask_path) as mask_img:
        mask_img.load()
        mask = mask_img.convert("L")

    problems: list[str] = []
    if final.size != source.size:
        problems.append(
            f"final is {final.size}, source is {source.size}; "
            "the product canvas must never be rescaled"
        )
    if mask.size != source.size:
        problems.append(
            f"mask is {mask.size}, source is {source.size}; "
            "the mask must cover the source frame exactly"
        )

    checked = changed = edge_band = outside = 0
    if not problems:
        source_bytes = source.tobytes()
        final_bytes = final.tobytes()
        mask_bytes = mask.tobytes()
        for i, m in enumerate(mask_bytes):
            if m == 255:
                checked += 1
                j = i * 4
                if source_bytes[j : j + 4] != final_bytes[j : j + 4]:
                    changed += 1
            elif m > 0:
                edge_band += 1
            else:
                outside += 1
        if changed:
            problems.append(
                f"{changed} of {checked} fully-masked product pixels "
                "differ between the source photo and the final image"
            )

    source_profile = color_profile_sha256_of_image(source)
    output_profile = color_profile_sha256_of_image(final)
    color_profile_preserved = source_profile == output_profile
    if not color_profile_preserved:
        problems.append(
            "colour profile changed between the source photo and the "
            "final image (source "
            f"{describe_color_profile(icc_profile_of_image(source))}, "
            f"final {describe_color_profile(icc_profile_of_image(final))}); "
            "identical pixel values would display as different colours"
        )

    return {
        "ok": not problems,
        "source": str(source_path),
        "final": str(final_path),
        "mask": str(mask_path),
        "checked_pixels": checked,
        "changed_pixels": changed,
        "edge_band_pixels": edge_band,
        "outside_mask_pixels": outside,
        "color_profile_source_sha256": source_profile,
        "color_profile_output_sha256": output_profile,
        "color_profile_preserved": color_profile_preserved,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agenthand-verify",
        description=(
            "Verify a delivered agenthand composite against its source "
            "photo and mask, from the files on disk. Exit 0 = the "
            "fidelity claim holds; 1 = it does not."
        ),
    )
    parser.add_argument("source", help="the source product photo (exported asset)")
    parser.add_argument("final", help="the delivered composite image")
    parser.add_argument("mask", help="the cut_out mask PNG")
    args = parser.parse_args(argv)

    report = verify_files(args.source, args.final, args.mask)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
