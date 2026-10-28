"""ROOM RUN: manifest, de-dup, and failure paths — fully offline.

The chain (identify / cut_out / draw) is injected as a fake, so no
test here can reach the network or the real ledger: AGENTHAND_DATA_DIR
points at a tmp dir, the API key is removed, and the spend probe is
the fake's own counter. The fake's cut_out/draw create REAL assets in
the tmp store (foregrounds, composites, persisted receipts), so the
manifest's opaque-pixel counts and receipt links are exercised
against the same store code production uses.
"""

import os

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from agenthand import config, ingest, roomrun  # noqa: E402
from agenthand.assets import AssetStore  # noqa: E402


@pytest.fixture(autouse=True)
def scratch_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setattr(config, "_DOTENV_LOADED", True)
    return tmp_path


def _photo(tmp_path: Path, name: str, color=(120, 60, 20)) -> Path:
    path = tmp_path / name
    Image.new("RGB", (48, 36), color).save(path, format="JPEG")
    return path


def _plan(concept: str, title: str | None = None) -> dict:
    return {
        "description": f"a photo whose primary subject is {concept}",
        "concept": concept,
        "concept_rewritten_from": None,
        "alternates": [],
        "suggested_title": title or f"Listing: {concept}",
        "listing_bullets": [f"one {concept}"],
        "condition_notes": "good",
        "model": "fake",
    }


class FakeChain:
    """The three tools, faked at the roomrun adapter boundary.

    plans: identify results in source order (a dict with an "error"
    key makes that photo's plan fail). contents maps
    (photo_index, concept) -> object_id: a cut_out succeeds exactly
    for those pairs, and the foreground it returns is derived from
    the object_id alone — two names resolving to one object_id get
    pixel-identical foregrounds, like two names for one real object.
    """

    IDENTIFY_COST = 0.008
    CUT_COST = 0.0025
    DRAW_COST = 0.01

    def __init__(self, store, plans, contents, *, draw_fail_objects=(), opaque_by_object=None):
        self.store = store
        self.plans = plans
        self.contents = contents
        self.draw_fail_objects = set(draw_fail_objects)
        self.opaque_by_object = opaque_by_object or {}
        self.photo_handles: dict[str, int] = {}
        self.fg_object: dict[str, str] = {}
        self.calls = {"identify": 0, "cut_out": 0, "draw": 0}
        self.spend = 0.0

    def chain(self) -> dict:
        return {"identify": self.identify, "cut_out": self.cut_out, "draw": self.draw}

    def identify(self, image_ref: str) -> dict:
        idx = self.calls["identify"]
        self.calls["identify"] += 1
        self.spend += self.IDENTIFY_COST
        plan = self.plans[idx]
        if "error" in plan:
            return {"status": "error", "error": plan["error"]}
        handle = ingest.ingest(image_ref, self.store)
        self.photo_handles[handle] = idx
        return {"status": "ok", "image": handle, "cost_usd": self.IDENTIFY_COST, **plan}

    def _foreground_for(self, object_id: str) -> Image.Image:
        opaque = self.opaque_by_object.get(object_id, 100)
        side = int(opaque**0.5) or 1
        color = tuple((hash(object_id) >> shift) & 0xFF for shift in (0, 8, 16)) + (255,)
        img = Image.new("RGBA", (side, side), color)
        return img

    def cut_out(self, handle: str, concept: str) -> dict:
        self.calls["cut_out"] += 1
        self.spend += self.CUT_COST
        idx = self.photo_handles[handle]
        object_id = self.contents.get((idx, concept))
        if object_id is None:
            return {
                "status": "error",
                "error": {
                    "code": "NO_MATCH",
                    "message": f"no {concept!r} in photo {idx}",
                    "cost_counted_usd": self.CUT_COST,
                },
            }
        fg_handle = self.store.put_image(
            self._foreground_for(object_id), kind="foreground_png"
        )
        self.fg_object[fg_handle] = object_id
        return {
            "status": "ok",
            "output_asset": fg_handle,
            "mask_asset": None,
            "mask_overlay_asset": None,
            "mask_review": {"bbox": [4, 4, 30, 30], "warnings": []},
            "cost_usd": self.CUT_COST,
        }

    def draw(self, fg_handle: str, scene: str) -> dict:
        self.calls["draw"] += 1
        self.spend += self.DRAW_COST
        object_id = self.fg_object[fg_handle]
        if object_id in self.draw_fail_objects:
            return {
                "status": "error",
                "error": {
                    "code": "IMAGE_REFUSED",
                    "message": "the image model refused this plate",
                    "cost_counted_usd": self.DRAW_COST,
                },
            }
        fg_record = self.store.get_record(fg_handle)
        composite = self.store.put_image(
            Image.new("RGBA", (8, 8), (10, 10, 10, 255)),
            kind="composite",
            source_asset_id=fg_record.asset_id,
        )
        receipt = {
            "source_asset": "asset://source",
            "foreground_pixel_sha256": fg_record.pixel_sha256,
            "opaque_pixels": self.opaque_by_object.get(object_id, 100),
            "changed_opaque_pixels": 0,
            "color_profile_preserved": True,
        }
        self.store.save_receipt(composite, receipt)
        return {
            "status": "ok",
            "output_asset": composite,
            "fidelity_receipt": receipt,
            "cost_usd": self.DRAW_COST,
        }


def _run(tmp_path, photos, fake, **kwargs):
    kwargs.setdefault("budget_usd", 5.00)
    return roomrun.run_room(
        photos,
        [],
        tmp_path / "out",
        store=fake.store,
        chain=fake.chain(),
        spend_probe=lambda: fake.spend,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_concept_variants_order_and_stripping():
    assert roomrun.concept_variants("gray storage box") == [
        "gray storage box",
        "storage box",
        "box",
    ]
    assert roomrun.concept_variants("straw hat") == ["straw hat", "hat"]
    assert roomrun.concept_variants("orange suitcase") == [
        "orange suitcase",
        "suitcase",
    ]
    plan = _plan("straw sun hat")
    variants = roomrun.concept_variants("straw hat", plan)
    assert variants[0] == "straw hat"
    assert "straw sun hat" in variants  # the plan's own phrasing rides along


def test_token_similarity_and_bbox_iou():
    assert roomrun.token_similarity("orange suitcase", "orange suitcase") == 1.0
    assert roomrun.token_similarity("orange suitcase", "orange spinner suitcase") >= 0.6
    grey = roomrun.token_similarity("red leather wallet", "brown leather wallet")
    assert roomrun.FLAG_SIMILARITY <= grey < roomrun.MERGE_SIMILARITY
    assert roomrun.token_similarity("book", "bookcase") > 0  # prefix rule
    assert roomrun.bbox_iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert roomrun.bbox_iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert roomrun.bbox_iou(None, [0, 0, 1, 1]) == 0.0


def test_group_candidates_merge_and_grey_zone():
    def cand(name, idx):
        return roomrun.Candidate(name=name, photo_index=idx, plan={})

    groups = roomrun.group_candidates(
        [cand("orange suitcase", 0), cand("orange spinner suitcase", 1)]
    )
    assert len(groups) == 1
    assert groups[0]["photo_indices"] == [0, 1]

    groups = roomrun.group_candidates(
        [cand("red leather wallet", 0), cand("brown leather wallet", 1)]
    )
    assert len(groups) == 2  # grey zone: kept both...
    assert any("possible duplicate" in f for f in groups[0]["flags"])  # ...and flagged
    assert any("possible duplicate" in f for f in groups[1]["flags"])


def test_rank_photos_prefers_mention_then_stills():
    sources = [
        roomrun.PhotoSource(path=Path("a.jpg"), origin="still", plan=_plan("white bookcase")),
        roomrun.PhotoSource(path=Path("b.jpg"), origin="still", plan=_plan("orange suitcase")),
    ]
    ranked = roomrun._rank_photos("orange suitcase", sources)
    assert ranked[0] == 1
    # No mention anywhere: full coverage order, stills before frames —
    # a silent plan is weak evidence of absence, so the probe cap,
    # not the ranker, decides how far down the order a run goes.
    sources.append(
        roomrun.PhotoSource(
            path=Path("f.jpg"), origin="video_frame", plan=_plan("white bookcase")
        )
    )
    ranked = roomrun._rank_photos("unicycle", sources)
    assert ranked == [0, 1, 2]


# ---------------------------------------------------------------------------
# Full runs against the fake chain
# ---------------------------------------------------------------------------


def test_named_run_stages_and_manifests(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "shelf.jpg"), _photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("straw hat"), _plan("orange suitcase")],
        contents={(0, "straw hat"): "hat1", (1, "orange suitcase"): "case1"},
    )
    manifest = _run(tmp_path, photos, fake, item_names=["straw hat", "orange suitcase"])
    items = {i["name"]: i for i in manifest["items"]}
    assert items["straw hat"]["status"] == "staged"
    assert items["straw hat"]["primary_photo_index"] == 0
    assert items["straw hat"]["receipt"]["changed_opaque_pixels"] == 0
    assert items["straw hat"]["receipt_file"] and Path(items["straw hat"]["receipt_file"]).exists()
    assert items["straw hat"]["listing"]["source"] == "photo_plan"
    assert items["orange suitcase"]["status"] == "staged"
    assert manifest["total_cost_usd"] == pytest.approx(fake.spend)
    assert (tmp_path / "out" / "manifest.json").exists()
    assert "STAGED" in (tmp_path / "out" / "summary.txt").read_text()


def test_partial_failure_is_first_class(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    manifest = _run(
        tmp_path, photos, fake, item_names=["orange suitcase", "ghost item"]
    )
    items = {i["name"]: i for i in manifest["items"]}
    assert items["orange suitcase"]["status"] == "staged"
    ghost = items["ghost item"]
    assert ghost["status"] == "failed"
    assert ghost["failure"]["stage"] == "cut_out"
    assert ghost["failure"]["code"] == "NO_MATCH"
    assert ghost["sightings"]  # the failed probes are in the record
    # Failed probes still cost money, and the manifest says so:
    # 2 concept variants x the per-call counted cost.
    assert ghost["cost_usd"] == pytest.approx(2 * FakeChain.CUT_COST)
    assert ghost["sightings"][0]["cost_usd"] == pytest.approx(2 * FakeChain.CUT_COST)


def test_listing_copy_not_borrowed_from_another_subject(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "shelf.jpg")]
    fake = FakeChain(
        store,
        plans=[_plan("white bookcase")],  # the photo's primary subject
        contents={(0, "straw hat"): "hat1"},  # ...but the hat is in it too
    )
    manifest = _run(tmp_path, photos, fake, item_names=["straw hat"])
    item = manifest["items"][0]
    assert item["status"] == "staged"
    assert item["listing"]["source"] == "none"
    assert item["listing"]["title"] is None


def test_auto_mode_dedups_across_photos(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "a.jpg", (200, 90, 10)), _photo(tmp_path, "b.jpg", (210, 95, 12))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase"), _plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1", (1, "orange suitcase"): "case1"},
        opaque_by_object={"case1": 100},
    )
    # The second view is the same object drawn larger: primary must be
    # the photo whose cut has more opaque pixels. Give photo 1's cut a
    # bigger foreground by using a per-object size the fake keys on
    # photo too — emulate by making both succeed and checking the
    # grouping first: one item, two sightings.
    manifest = _run(tmp_path, photos, fake, auto=True)
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["status"] == "staged"
    ok_sightings = [s for s in item["sightings"] if s["status"] == "ok"]
    assert len(ok_sightings) == 2
    assert item["also_seen_in"]  # the non-primary sighting is recorded


def test_cross_item_same_object_flagged_not_merged(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "shelf.jpg")]
    fake = FakeChain(
        store,
        plans=[_plan("gray storage box")],
        contents={
            (0, "gray storage box"): "box1",
            (0, "storage box"): "box1",  # same physical object, second name
        },
    )
    manifest = _run(
        tmp_path, photos, fake, item_names=["gray storage box", "storage box"]
    )
    items = {i["name"]: i for i in manifest["items"]}
    assert len(items) == 2  # kept both
    assert "storage box" in items["gray storage box"]["possible_duplicate_of"]
    assert "gray storage box" in items["storage box"]["possible_duplicate_of"]


def test_draw_failure_keeps_cut_evidence(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
        draw_fail_objects={"case1"},
    )
    manifest = _run(tmp_path, photos, fake, item_names=["orange suitcase"])
    item = manifest["items"][0]
    assert item["status"] == "failed"
    assert item["failure"]["stage"] == "draw"
    assert item["failure"]["code"] == "IMAGE_REFUSED"
    assert item["sightings"][0]["foreground_asset"]  # the cut survives in the record
    assert item["cost_usd"] == pytest.approx(
        FakeChain.CUT_COST + FakeChain.DRAW_COST
    )


def test_budget_stop_marks_remaining_items(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "a.jpg"), _photo(tmp_path, "b.jpg")]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase"), _plan("straw hat")],
        contents={(0, "orange suitcase"): "case1", (1, "straw hat"): "hat1"},
    )

    def greedy_probe():
        return fake.spend * 1000  # spend appears to explode past any budget

    manifest = roomrun.run_room(
        photos,
        [],
        tmp_path / "out",
        item_names=["orange suitcase", "straw hat"],
        store=fake.store,
        chain=fake.chain(),
        spend_probe=greedy_probe,
        budget_usd=5.00,
    )
    # spend_before was read through the same probe (0 at start), then
    # the plan pass makes the probe report >> the budget, so every
    # item is stopped before its first probe.
    assert all(i["status"] == "failed" for i in manifest["items"])
    assert all(i["failure"]["code"] == "BUDGET_STOP" for i in manifest["items"])
    assert fake.calls["cut_out"] == 0


def test_projection_gate_refuses_before_any_call(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "a.jpg")]
    fake = FakeChain(store, plans=[_plan("orange suitcase")], contents={})
    with pytest.raises(RuntimeError, match="worst-case projection"):
        _run(tmp_path, photos, fake, item_names=["orange suitcase"], budget_usd=0.01)
    assert fake.calls == {"identify": 0, "cut_out": 0, "draw": 0}


# ---------------------------------------------------------------------------
# Video frame sampling
# ---------------------------------------------------------------------------


def test_sample_video_frames_without_ffmpeg(tmp_path):
    frames, note = roomrun.sample_video_frames(
        tmp_path / "nope.mp4", tmp_path / "frames", ffmpeg=""
    )
    assert frames == []
    assert note and "ffmpeg" in note


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_sample_video_frames_extracts(tmp_path):
    video = tmp_path / "walk.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=duration=3:size=64x64:rate=5",
            "-pix_fmt", "yuv420p", str(video), "-y",
        ],
        check=True,
    )
    frames, note = roomrun.sample_video_frames(
        video, tmp_path / "frames", every_seconds=1.0, max_frames=4
    )
    assert note is None
    assert [t for _, t in frames] == [0.0, 1.0, 2.0]
    assert all(p.exists() for p, _ in frames)
