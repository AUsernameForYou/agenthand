"""The site's proof fixture, pinned to the real files.

The badass Gauntlet found the browser verifier grading hard-coded
constants while the page claimed nothing was retyped. The honesty
pass (verifier-v4.js) grades the run's published receipt instead —
and these tests pin the whole arrangement down:

- the 580,613 / 0 figure is what _measure actually finds on the
  real proof PNGs served by the site;
- the published receipt's hashes are the served files' hashes;
- the printed receipt on the page agrees with the receipt file;
- the verifier JS contains no run constants to grade against;
- the counting code displayed on the page is the code the
  verifier file actually ships.
"""

import hashlib
import html as html_module
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from agenthand import composite
from agenthand.assets import pixel_hash_of_image

DOCS = Path(__file__).resolve().parents[1] / "docs"
RECEIPT = json.loads((DOCS / "proof-receipt-v1.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_measure_on_the_real_proof_pngs():
    # THE pin: 580,613 checked, 0 changed is measured here, from
    # the byte-exact files the site serves — not asserted from the
    # page's own text.
    layer = Image.open(DOCS / "proof-layer-v1.png")
    output = Image.open(DOCS / "proof-output-v1.png")
    counts = composite._measure(layer, output)
    assert counts["opaque_pixels"] == 580_613
    assert counts["changed_opaque_pixels"] == 0
    assert counts["opaque_pixels"] == RECEIPT["opaque_pixels"]
    assert counts["changed_opaque_pixels"] == RECEIPT["changed_opaque_pixels"]


def test_published_receipt_matches_the_served_files():
    assert RECEIPT["source_sha256"] == _sha256(DOCS / "proof-source-v1.png")
    assert RECEIPT["foreground_sha256"] == _sha256(DOCS / "proof-layer-v1.png")
    assert RECEIPT["output_sha256"] == _sha256(DOCS / "proof-output-v1.png")
    layer = Image.open(DOCS / "proof-layer-v1.png")
    output = Image.open(DOCS / "proof-output-v1.png")
    assert RECEIPT["foreground_pixel_sha256"] == pixel_hash_of_image(layer)
    assert RECEIPT["output_pixel_sha256"] == pixel_hash_of_image(output)


def test_page_figures_agree_with_the_receipt():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    assert f"{RECEIPT['opaque_pixels']:,}" in page  # the hero figure
    # The printed receipt block carries the receipt file's hashes.
    assert RECEIPT["source_sha256"] in page
    assert RECEIPT["foreground_pixel_sha256"] in page


def test_verifier_grades_the_receipt_not_constants():
    js = (DOCS / "verifier-v4.js").read_text(encoding="utf-8")
    assert "proof-receipt-v1.json" in js
    for field in (
        "source_sha256",
        "foreground_sha256",
        "foreground_pixel_sha256",
        "output_sha256",
        "output_pixel_sha256",
    ):
        assert RECEIPT[field] not in js, f"hard-coded {field} in verifier"
    assert "580613" not in js
    assert "580,613" not in js
    # Zero changed is the law in code, not an expectation from data.
    assert "counts.changed === 0" in js
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    assert 'src="verifier-v4.js"' in page


def test_displayed_counting_code_matches_the_verifier_file():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    start = page.index('<div class="opencode">')
    pre = page[page.index('<pre class="code">', start) : page.index("</pre>", start)]
    displayed = html_module.unescape(re.sub(r"<[^>]+>", "", pre)).strip()

    js = (DOCS / "verifier-v4.js").read_text(encoding="utf-8")
    match = re.search(
        r"/\* Replicates composite\.py.*?\n\}\n", js, flags=re.DOTALL
    )
    assert match, "countChanged not found in verifier-v4.js"
    shipped = match.group(0).strip()
    assert displayed == shipped


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_verifier_helpers_under_node(tmp_path):
    # The pure helpers, exercised headlessly: expectations come
    # from the real receipt, the verdict law refuses a doctored
    # receipt, and the ICC scan reads real chunk headers.
    script = tmp_path / "check.js"
    script.write_text(
        """
const fs = require("fs");
const v = require(process.argv[2]);
const receipt = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const exp = v.expectationsFromReceipt(receipt);
if (exp.expectedChecked !== receipt.opaque_pixels) throw new Error("checked");
if (exp.layerFileSha256 !== receipt.foreground_sha256) throw new Error("hash");
let verdict = v.verdictFor(exp, { checked: exp.expectedChecked, changed: 0 });
if (!verdict.pass) throw new Error("clean run must pass");
// Grok's case: a fixture whose data expects changed = 5 must NOT
// pass — zero is law in code, and the receipt must report zero.
const doctored = Object.assign({}, exp, { receiptChanged: 5 });
verdict = v.verdictFor(doctored, { checked: exp.expectedChecked, changed: 5 });
if (verdict.pass) throw new Error("doctored receipt passed");
verdict = v.verdictFor(exp, { checked: exp.expectedChecked, changed: 1 });
if (verdict.pass) throw new Error("one changed pixel passed");
verdict = v.verdictFor(exp, { checked: 0, changed: 0 });
if (verdict.pass) throw new Error("zero coverage passed");
// ICC scan: the real fixture has no iCCP; a synthetic one does.
const layer = fs.readFileSync(process.argv[4]);
if (v.pngHasICC(layer.buffer.slice(layer.byteOffset, layer.byteOffset + layer.byteLength)))
  throw new Error("fixture PNG has iCCP?");
const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const len = Buffer.alloc(4); len.writeUInt32BE(3);
const chunk = Buffer.concat([len, Buffer.from("iCCP"), Buffer.from([1, 2, 3]), Buffer.alloc(4)]);
const withIcc = Buffer.concat([sig, chunk]);
if (!v.pngHasICC(withIcc.buffer.slice(withIcc.byteOffset, withIcc.byteOffset + withIcc.byteLength)))
  throw new Error("synthetic iCCP not detected");
console.log("node helpers ok");
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "node",
            str(script),
            str(DOCS / "verifier-v4.js"),
            str(DOCS / "proof-receipt-v1.json"),
            str(DOCS / "proof-layer-v1.png"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "node helpers ok" in result.stdout
