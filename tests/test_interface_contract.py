"""Interface contract tests (Differential Conformance Suite).

Loads the single written contract (spec/interface.schema.json) and the
shared differential fixture (tests/fixtures/interface/differential.json,
byte-identical in the Rust tree) and holds THIS build to both:

- the rounding semantics the contract declares (round6 half-even),
  exercised through the real functions that publish rounded values;
- the exact chessboard/chamfer distance maps, through the real
  segmentation implementation;
- the reconstruction-fraction formula and its error wording, through
  the real receipt validator;
- the scene-guard refusal wording, through the real guard;
- the tool signature surface the schema declares;
- cross-verification: this build's validator must accept the other
  build's issued receipts under their own version policies.

The fixture's expected values were computed independently of both
builds (exact rational arithmetic, brute-force distance). A failure
here is a finding about the build, not about the fixture.
"""

import inspect
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from agenthand import composite, ledger, provenance, sceneguard, segmentation, server

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "interface" / "differential.json").read_text()
)
SCHEMA = json.loads((ROOT / "spec" / "interface.schema.json").read_text())
RECEIPTS = ROOT / "tests" / "fixtures" / "interface" / "receipts"


def _receipt(name):
    return json.loads((RECEIPTS / name).read_text())


# ------------------------------------------------------------------ round6
@pytest.mark.parametrize(
    "case", FIXTURE["round6"]["cases"], ids=[c["input_written"] for c in FIXTURE["round6"]["cases"]]
)
def test_provenance_round6_matches_contract_half_even(case):
    """provenance.round6 publishes GPS degrees at 6dp in the receipt's
    provenance block. The contract value is half-even (the semantics
    Rust's round6 and this build's composite rounding both declare)."""
    assert provenance.round6(case["input_f64"]) == case["expected"]


@pytest.mark.parametrize(
    "case", FIXTURE["round6"]["cases"], ids=[c["input_written"] for c in FIXTURE["round6"]["cases"]]
)
def test_ledger_round6_half_away_matches_its_declared_rule(case):
    """The ledger's money rounding declares half-away-from-zero in its
    own name; it is pinned against that declared rule, not the
    contract's half-even."""
    assert ledger._round6_half_away(case["input_f64"]) == case["expected_half_away"]


def test_review_mask_coverage_uses_half_even():
    """128 opaque px in a 128x128 frame is exactly 0.0078125 — a true
    tie. The contract value is the even side, 0.007812."""
    mask = Image.new("L", (128, 128), 0)
    px = mask.load()
    for i in range(128):
        px[i % 128, i // 128] = 255
    review = segmentation.review_mask(mask)
    assert review["coverage"] == 0.007812


# ------------------------------------------------------------------ chamfer
@pytest.mark.parametrize(
    "case", FIXTURE["chamfer"]["cases"], ids=[c["name"] for c in FIXTURE["chamfer"]["cases"]]
)
def test_chessboard_distance_matches_fixture(case):
    target = np.zeros((case["h"], case["w"]), dtype=bool)
    for x, y in case["targets"]:
        target[y, x] = True
    got = segmentation._chessboard_distance(target, case["border_is_target"])
    assert got.tolist() == case["expected"]


def test_chamfer_cap_matches_contract():
    assert segmentation.EDGE_DIST_CAP == FIXTURE["chamfer"]["cap"]


# ------------------------------------------------------- reconstruction math
@pytest.mark.parametrize(
    "case",
    [c for c in FIXTURE["reconstruction_fraction"]["cases"] if c["reconstruction_pixels"] > 0],
    ids=[f"{c['reconstruction_pixels']}+{c['opaque_pixels']}" for c in FIXTURE["reconstruction_fraction"]["cases"] if c["reconstruction_pixels"] > 0],
)
def test_receipt_validator_reconstruction_formula(case):
    """A v4 receipt whose stated fraction equals the fixture's
    independently computed value draws no complaint from the real
    validator; a fraction 0.01 higher draws the exact contract string."""
    receipt = _receipt("py-v4.json")
    receipt["opaque_pixels"] = case["opaque_pixels"]
    # A fully policy-consistent applied block (attempted, renderer,
    # layer + region-map assets and hashes), so the validator's shape
    # and policy gates pass and only the fraction arithmetic is judged.
    handle = "asset://" + "0" * 32
    block = receipt["reconstruction"]
    block.update(
        {
            "status": "applied",
            "attempted": True,
            "renderer": "agenthand.paperfill/2",
            "layer_asset": handle,
            "layer_sha256": "0" * 64,
            "layer_pixel_sha256": "0" * 64,
            "region_map_asset": handle,
            "region_map_sha256": "0" * 64,
            "region_map_pixel_sha256": "0" * 64,
            "reconstruction_pixels": case["reconstruction_pixels"],
            "reconstruction_fraction": case["expected_fraction"],
        }
    )
    shape, _terms = composite._validate_receipt(receipt)
    want = FIXTURE["error_strings"]["reconstruction_fraction_disagrees"]
    assert want not in shape
    block["reconstruction_fraction"] = case["expected_fraction"] + 0.01
    shape, _terms = composite._validate_receipt(receipt)
    assert want in shape


# ------------------------------------------------------------- error strings
def test_scene_guard_refusal_wording_matches_contract():
    guard = sceneguard.check_scene(
        "blue BMX bike", "a bright showroom with a bike on display"
    )
    probe = FIXTURE["error_strings"]["scene_guard_probe"]
    expected = FIXTURE["error_strings"]["scene_guard_refusal_template"].format(**probe)
    assert guard.status == "refused"
    assert guard.refusal_message() == expected


def test_scene_guard_rule_and_skipped_reason_match_contract():
    guard = sceneguard.check_scene("blue BMX bike", "an empty studio")
    record = guard.record()
    assert record["rule"] == SCHEMA["$defs"]["scene_guard_record"]["properties"]["rule"]["const"]
    skipped = sceneguard.check_scene(None, "an empty studio")
    assert skipped.record()["reason"] == (
        "no concept recorded in the foreground's mask; nothing to check "
        "the scene text against"
    )


# ------------------------------------------------------------ tool surface
def test_tool_signatures_match_contract():
    canon = SCHEMA["tools"]
    for name, fn in (
        ("identify", server.identify),
        ("cut_out", server.cut_out),
        ("draw", server.draw),
    ):
        params = inspect.signature(fn).parameters
        declared = canon[name]["inputSchema"]
        assert list(params) == list(declared["properties"]) or set(params) == set(
            declared["properties"]
        ), f"{name}: params {list(params)} vs contract {list(declared['properties'])}"
        required = [p for p, v in params.items() if v.default is inspect.Parameter.empty]
        assert sorted(required) == sorted(declared["required"])
    # The recorded divergence, pinned: draw has NO plate parameter in
    # this build (the contract records plate as Rust-only).
    assert "plate" not in inspect.signature(server.draw).parameters
    concept = inspect.signature(server.cut_out).parameters["concept"]
    assert concept.default is None


# ------------------------------------------------------- cross-verification
def test_own_v4_receipt_validates_clean():
    shape, terms = composite._validate_receipt(_receipt("py-v4.json"))
    assert shape == [] and terms == []


def test_rust_v2_receipt_validates_under_its_own_policy():
    """The kill-shot, this direction: a receipt the Rust build
    issued (schema 2, with its enrichment keys) must verify under this
    build's validator, which keeps the v2 policy."""
    shape, terms = composite._validate_receipt(_receipt("rust-v2.json"))
    assert shape == [] and terms == []


def test_v4_receipt_missing_quality_blocks_fails_closed():
    """The other half of the recorded strictness gap: THIS build's
    validator requires the v4 quality blocks (the Rust verifier does
    not — see the pinned test in the Rust tree)."""
    for strip in ("cut_quality", "plate_quality"):
        receipt = _receipt("py-v4.json")
        del receipt[strip]
        shape, terms = composite._validate_receipt(receipt)
        assert f"missing field: {strip}" in shape


def test_legacy_v3_receipt_validates_under_its_own_policy():
    shape, terms = composite._validate_receipt(_receipt("py-v3-legacy.json"))
    assert shape == [] and terms == []
