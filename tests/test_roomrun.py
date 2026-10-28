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

from agenthand import composite, config, ingest, ledger, roomrun  # noqa: E402
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


def _verify_ok(store, handle):
    """A passing verifier report, shaped like the real one's."""
    return {
        "ok": True,
        "checks": {"zero_changed_pixels": True},
        "problems": [],
        "recomputed": {
            "changed_opaque_pixels": 0,
            "opaque_pixels": 100,
            "color_profile_preserved": True,
        },
    }


def _run(tmp_path, photos, fake, **kwargs):
    kwargs.setdefault("budget_usd", 5.00)
    kwargs.setdefault("verify_fn", _verify_ok)
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


def test_rank_photos_head_noun_beats_modifier_match():
    """The first real room run's failure shape: for "black hat", the
    plans naming "black" (a suitcase's trim, a railing) outranked the
    plans naming "hat" (the bookcase views that held it), and the
    probe cap was spent before any hat-bearing photo was probed. The
    item's head noun is the strong signal; a shared colour is not."""

    def plan_with(text):
        return {
            "description": text,
            "concept": "",
            "suggested_title": "",
            "listing_bullets": [],
            "condition_notes": "",
        }

    sources = [
        roomrun.PhotoSource(
            path=Path("case.jpg"),
            origin="still",
            plan=plan_with("orange suitcase with black trim and spinner wheels"),
        ),
        roomrun.PhotoSource(
            path=Path("floor.jpg"),
            origin="video_frame",
            plan=plan_with("wood plank flooring with a black railing"),
        ),
        roomrun.PhotoSource(
            path=Path("shelf.jpg"),
            origin="video_frame",
            plan=plan_with("white bookshelf holding boxes, a straw hat and hats"),
        ),
    ]
    ranked = roomrun._rank_photos("black hat", sources)
    assert ranked[0] == 2  # the hat-bearing photo probes first


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
# Verify loop (demo-ready Gauntlet fix #1): draw's receipt is never
# believed on its own — the independent verifier's verdict is a
# distinct manifest field, and a failed verdict un-stages the item.
# ---------------------------------------------------------------------------


def _verify_fail(store, handle):
    return {
        "ok": False,
        "checks": {"zero_changed_pixels": False},
        "problems": ["2 of 100 fully-masked product pixels differ"],
        "recomputed": {
            "changed_opaque_pixels": 2,
            "opaque_pixels": 100,
            "color_profile_preserved": True,
        },
    }


def _one_item_fake(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    return photos, fake


def test_verify_failure_means_not_staged(tmp_path):
    photos, fake = _one_item_fake(tmp_path)
    manifest = _run(
        tmp_path, photos, fake,
        item_names=["orange suitcase"], verify_fn=_verify_fail,
    )
    item = manifest["items"][0]
    assert item["status"] == "verify_failed"  # NOT staged
    assert item["failure"]["stage"] == "verify"
    assert item["failure"]["code"] == "VERIFY_FAILED"
    # The verifier's own numbers are the record, distinct from the
    # receipt draw issued (which still claims 0 changed, untouched).
    assert item["verification"]["ok"] is False
    assert item["verification"]["changed_opaque_pixels"] == 2
    assert item["receipt"]["changed_opaque_pixels"] == 0
    summary = (tmp_path / "out" / "summary.txt").read_text()
    assert "VERIFY FAILED" in summary
    assert "0 of 1 items staged" in summary


def test_verification_numbers_come_from_the_verifier(tmp_path):
    def verify_recount(store, handle):
        report = _verify_ok(store, handle)
        report["recomputed"] = {
            "changed_opaque_pixels": 0,
            "opaque_pixels": 4242,  # the recount's count, not draw's
            "color_profile_preserved": True,
        }
        return report

    photos, fake = _one_item_fake(tmp_path)
    manifest = _run(
        tmp_path, photos, fake,
        item_names=["orange suitcase"], verify_fn=verify_recount,
    )
    item = manifest["items"][0]
    assert item["status"] == "staged"
    assert item["verification"]["ok"] is True
    assert item["verification"]["opaque_pixels"] == 4242
    assert item["receipt"]["opaque_pixels"] == 100  # the fake draw's claim
    summary = (tmp_path / "out" / "summary.txt").read_text()
    assert "4242 opaque px recounted from disk, 0 changed" in summary


def test_verifier_that_cannot_run_fails_the_item_not_the_room(tmp_path):
    def verify_broken(store, handle):
        raise ValueError("no receipt persisted beside the composite")

    photos, fake = _one_item_fake(tmp_path)
    manifest = _run(
        tmp_path, photos, fake,
        item_names=["orange suitcase"], verify_fn=verify_broken,
    )
    item = manifest["items"][0]
    assert item["status"] == "verify_failed"
    assert item["verification"]["ok"] is False
    assert "could not run" in item["verification"]["problems"][0]


class RealDrawChain:
    """identify/cut_out faked, draw REAL (composite.draw_product).

    The foreground is lineage-correct (parented to the ingested
    source photo), so draw runs the production compositor and
    persists a production receipt — and run_room's DEFAULT verifier
    (composite.verify_receipt_from_disk) grades it from disk.
    """

    def __init__(self, store):
        self.store = store

    def identify(self, image_ref: str) -> dict:
        handle = ingest.ingest(image_ref, self.store)
        return {"status": "ok", "image": handle, "cost_usd": 0.0, **_plan("straw hat")}

    def cut_out(self, handle: str, concept: str) -> dict:
        if concept != "straw hat":
            return {
                "status": "error",
                "error": {"code": "NO_MATCH", "message": "not here", "cost_counted_usd": 0.0},
            }
        fg_img = Image.new("RGBA", (48, 36), (0, 0, 0, 0))
        px = fg_img.load()
        for y in range(36):
            for x in range(20):
                px[x, y] = (200, 30, 30, 255)
            px[20, y] = (0, 200, 30, 128)
        fg = self.store.put_image(
            fg_img,
            kind="foreground_png",
            source_asset_id=AssetStore.parse_handle(handle),
        )
        return {
            "status": "ok",
            "output_asset": fg,
            "mask_asset": None,
            "mask_overlay_asset": None,
            "mask_review": {"bbox": [0, 0, 21, 36], "warnings": []},
            "cost_usd": 0.0,
        }

    def draw(self, fg_handle: str, scene: str) -> dict:
        handle, receipt = composite.draw_product(self.store, fg_handle, scene)
        return {
            "status": "ok",
            "output_asset": handle,
            "fidelity_receipt": receipt,
            "cost_usd": 0.0,
        }


def test_default_verifier_reproves_a_real_draw(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "shelf.jpg")]
    chain = RealDrawChain(store)
    manifest = roomrun.run_room(
        photos,
        [],
        tmp_path / "out",
        item_names=["straw hat"],
        store=store,
        chain={"identify": chain.identify, "cut_out": chain.cut_out, "draw": chain.draw},
        spend_probe=lambda: 0.0,
        budget_usd=5.00,
        # no verify_fn: the production default must be what runs
    )
    item = manifest["items"][0]
    assert item["status"] == "staged"
    verification = item["verification"]
    assert verification["ok"] is True
    assert verification["changed_opaque_pixels"] == 0
    assert verification["opaque_pixels"] == 20 * 36  # the recount, from disk
    assert verification["opaque_pixels"] == item["receipt"]["opaque_pixels"]
    assert verification["checks_failed"] == []


# ---------------------------------------------------------------------------
# Ledger isolation: a room run's receipts land in its own ledger
# (default <out>/ledger.jsonl); the configured (cumulative) ledger
# is never touched, and the manifest total is the room ledger's.
# ---------------------------------------------------------------------------


def test_room_run_spends_against_its_own_ledger(tmp_path, monkeypatch):
    submission_ledger = tmp_path / "submission-ledger.jsonl"
    monkeypatch.setenv("AGENTHAND_LEDGER_PATH", str(submission_ledger))
    photos, fake = _one_item_fake(tmp_path)

    def wrap(fn, tool, cost):
        def inner(*args):
            result = fn(*args)
            # billed_call resolves the ledger path at call time; so
            # does this simulation of it.
            ledger.append_receipt(
                config.get_ledger_path(),
                ledger.Receipt(tool=tool, model="fake", usd_estimate=cost),
            )
            return result

        return inner

    base = fake.chain()
    chain = {
        "identify": wrap(base["identify"], "identify", FakeChain.IDENTIFY_COST),
        "cut_out": wrap(base["cut_out"], "cut_out", FakeChain.CUT_COST),
        "draw": wrap(base["draw"], "draw", FakeChain.DRAW_COST),
    }
    out_dir = tmp_path / "out"
    manifest = roomrun.run_room(
        photos,
        [],
        out_dir,
        item_names=["orange suitcase"],
        store=fake.store,
        chain=chain,
        verify_fn=_verify_ok,
        budget_usd=5.00,
        # no spend_probe, no ledger_path: both defaults are the point
    )
    room_ledger = out_dir / "ledger.jsonl"
    assert manifest["ledger_path"] == str(room_ledger)
    assert ledger.ledger_total_usd(room_ledger) == pytest.approx(fake.spend)
    assert manifest["total_cost_usd"] == pytest.approx(fake.spend)
    assert not submission_ledger.exists()  # the cumulative ledger untouched
    # The caller's environment is restored after the run.
    assert os.environ["AGENTHAND_LEDGER_PATH"] == str(submission_ledger)


# ---------------------------------------------------------------------------
# Video frame sampling
# ---------------------------------------------------------------------------


def test_sample_video_frames_without_ffmpeg(tmp_path):
    frames, note = roomrun.sample_video_frames(
        tmp_path / "nope.mp4", tmp_path / "frames", ffmpeg=""
    )
    assert frames == []
    assert note and "ffmpeg" in note


# ---------------------------------------------------------------------------
# Narration: the seller's voice names the items (Baseten / Spark Open).
# The narration call is injected as a fake, so no test here can reach
# Baseten or spend from either pool. The audio files are real WAVs
# written with the stdlib, so extraction, probing, and the silence
# gate all run their production code.
# ---------------------------------------------------------------------------


def _wav_file(path: Path, *, seconds: float = 1.0, rate: int = 8000, peak: float = 0.5) -> Path:
    import math
    import struct
    import wave as wave_mod

    with wave_mod.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(peak * 32767 * math.sin(2 * math.pi * 440 * i / rate))
                )
                for i in range(int(seconds * rate))
            )
        )
    return path


class FakeNarration:
    """The Baseten narration call, faked at the roomrun boundary."""

    def __init__(self, reply: str = "", *, error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[Path] = []

    def __call__(self, audio_path: Path) -> dict:
        self.calls.append(Path(audio_path))
        if self.error is not None:
            raise self.error
        return {
            "reply": self.reply,
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
            "latency_ms": 123,
            "model": roomrun.NARRATION_MODEL,
        }


_NARRATION_REPLY = (
    "TRANSCRIPT\n"
    "Alright, sell this filter. Also some suitcases and other junk here.\n\n"
    "SELLING\n"
    "filter\n\n"
    "MENTIONED\n"
    "suitcase\n"
)


def _run_narrated(tmp_path, photos, fake, narration_fn, **kwargs):
    kwargs.setdefault("budget_usd", 5.00)
    kwargs.setdefault("verify_fn", _verify_ok)
    return roomrun.run_room(
        photos,
        [],
        tmp_path / "out",
        store=fake.store,
        chain=fake.chain(),
        spend_probe=lambda: fake.spend,
        narration_fn=narration_fn,
        **kwargs,
    )


def test_parse_narration_reply_splits_named_and_mentioned():
    parsed = roomrun.parse_narration_reply(_NARRATION_REPLY)
    assert parsed["transcript"].startswith("Alright, sell this filter.")
    assert parsed["selling"] == ["filter"]
    assert parsed["mentioned"] == ["suitcase"]


def test_parse_narration_reply_cleans_bullets_and_none():
    parsed = roomrun.parse_narration_reply(
        "TRANSCRIPT:\nhello\nSELLING:\n- red bike\n2. blue chair\nMENTIONED:\nNONE\n"
    )
    assert parsed["selling"] == ["red bike", "blue chair"]
    assert parsed["mentioned"] == []


def test_parse_narration_reply_without_headings_is_all_transcript():
    parsed = roomrun.parse_narration_reply("just some words the model said")
    assert parsed["transcript"] == "just some words the model said"
    assert parsed["selling"] == [] and parsed["mentioned"] == []


def test_probe_wav_and_cost_projection():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        loud = _wav_file(Path(td) / "loud.wav", seconds=2.0, peak=0.5)
        duration, peak = roomrun.probe_wav(loud)
        assert duration == pytest.approx(2.0, abs=0.01)
        assert peak == pytest.approx(0.5, abs=0.02)
        silent = _wav_file(Path(td) / "silent.wav", seconds=1.0, peak=0.0)
        _, silent_peak = roomrun.probe_wav(silent)
        assert silent_peak == 0.0
    # Published rates: $1.25/M in, $4.25/M out; audio priced at
    # 150 tokens/s plus the fixed prompt allowance, output at the cap.
    projected = roomrun.project_narration_cost_usd(10.0)
    assert projected == pytest.approx(
        (10.0 * 150 + 512) * 1.25e-6 + 4096 * 4.25e-6
    )
    assert roomrun.narration_cost_usd(
        {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    ) == pytest.approx(1.25 + 4.25)
    assert roomrun.narration_cost_usd({}) is None


def test_narrated_run_stages_named_and_confirms_mentioned(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "purifier.jpg"), _photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("air purifier"), _plan("orange suitcase")],
        contents={(0, "filter"): "filter1", (1, "suitcase"): "case1"},
    )
    narration = FakeNarration(_NARRATION_REPLY)
    audio = _wav_file(tmp_path / "walk.wav")
    manifest = _run_narrated(
        tmp_path, photos, fake, narration, narrated_audios=[audio]
    )
    assert manifest["mode"] == "narrated"
    assert len(narration.calls) == 1

    section = manifest["narration"]
    record = section["files"][0]
    assert record["error"] is None
    assert record["transcript"].startswith("Alright, sell this filter.")
    assert record["named_items"] == ["filter"]
    assert record["mentioned_items"] == ["suitcase"]
    assert section["usage"]["total_tokens"] == 1500
    # Baseten spend is priced at the published rates and kept OUT of
    # the Meta room total.
    assert section["cost_usd_estimate"] == pytest.approx(1000 * 1.25e-6 + 500 * 4.25e-6)
    assert manifest["baseten_cost_usd_estimate"] == section["cost_usd_estimate"]
    assert manifest["total_cost_usd"] == pytest.approx(fake.spend)
    transcript_path = Path(section["transcript_file"])
    assert transcript_path.exists()
    assert "sell this filter" in transcript_path.read_text()

    items = {i["name"]: i for i in manifest["items"]}
    assert items["filter"]["status"] == "staged"
    assert items["filter"]["narration"] == {"kind": "named", "source": str(audio)}
    assert items["filter"]["verification"]["ok"] is True
    suitcase = items["suitcase"]
    assert suitcase["status"] == "staged"  # mentioned, then visually confirmed
    assert suitcase["narration"]["kind"] == "mentioned"
    assert any("mentioned in passing" in f for f in suitcase["flags"])
    summary = (tmp_path / "out" / "summary.txt").read_text()
    assert "NARRATION" in summary and "sell this filter" in summary


def test_narrated_mentioned_item_refuted_when_not_visible(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "purifier.jpg")]
    fake = FakeChain(
        store,
        plans=[_plan("air purifier")],
        contents={(0, "filter"): "filter1"},  # no unicycle anywhere
    )
    narration = FakeNarration(
        "TRANSCRIPT\nsell this filter, and there is a unicycle somewhere\n"
        "SELLING\nfilter\nMENTIONED\nunicycle\n"
    )
    manifest = _run_narrated(
        tmp_path, photos, fake, narration,
        narrated_audios=[_wav_file(tmp_path / "walk.wav")],
    )
    items = {i["name"]: i for i in manifest["items"]}
    assert items["filter"]["status"] == "staged"
    unicycle = items["unicycle"]
    assert unicycle["status"] == "failed"  # refuted by the visual pipeline
    assert unicycle["failure"]["stage"] == "cut_out"
    assert unicycle["narration"]["kind"] == "mentioned"
    assert unicycle["sightings"]  # the refutation evidence is recorded


def test_narration_failure_is_a_row_and_the_stills_run_continues(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    narration = FakeNarration(error=RuntimeError("baseten is down"))
    manifest = _run_narrated(
        tmp_path, photos, fake, narration,
        item_names=["orange suitcase"],
        narrated_audios=[_wav_file(tmp_path / "walk.wav")],
    )
    record = manifest["narration"]["files"][0]
    assert record["error"]["code"] == "NARRATION_FAILED"
    assert "baseten is down" in record["error"]["message"]
    assert manifest["mode"] == "named"
    assert manifest["items"][0]["status"] == "staged"  # stills-only mode intact
    assert any("NARRATION_FAILED" in n for n in manifest["notes"])


def test_silent_track_never_reaches_the_model(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    narration = FakeNarration(_NARRATION_REPLY)
    manifest = _run_narrated(
        tmp_path, photos, fake, narration,
        item_names=["orange suitcase"],
        narrated_audios=[_wav_file(tmp_path / "silent.wav", peak=0.0)],
    )
    record = manifest["narration"]["files"][0]
    assert record["error"]["code"] == "SILENT_TRACK"
    assert narration.calls == []  # no call was made, no pool was touched
    assert manifest["items"][0]["status"] == "staged"


def test_narration_cost_cap_refuses_before_the_call(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    narration = FakeNarration(_NARRATION_REPLY)
    manifest = _run_narrated(
        tmp_path, photos, fake, narration,
        item_names=["orange suitcase"],
        narrated_audios=[_wav_file(tmp_path / "walk.wav")],
        narration_max_projected_usd=0.0001,
    )
    record = manifest["narration"]["files"][0]
    assert record["error"]["code"] == "NARRATION_COST_REFUSED"
    assert record["projected_cost_usd"] > 0.0001
    assert narration.calls == []
    assert manifest["items"][0]["status"] == "staged"


def test_narration_annotates_a_typed_item_instead_of_duplicating(tmp_path):
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    narration = FakeNarration(
        "TRANSCRIPT\nthe orange suitcase is for sale\n"
        "SELLING\norange suitcase\nMENTIONED\nNONE\n"
    )
    manifest = _run_narrated(
        tmp_path, photos, fake, narration,
        item_names=["orange suitcase"],
        narrated_audios=[_wav_file(tmp_path / "walk.wav")],
    )
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["status"] == "staged"
    assert item["narration"]["kind"] == "named"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_narrated_video_without_audio_track_is_a_row(tmp_path):
    video = tmp_path / "silent-walk.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=duration=1:size=64x64:rate=5",
            "-pix_fmt", "yuv420p", str(video), "-y",
        ],
        check=True,
    )
    store = AssetStore(tmp_path)
    photos = [_photo(tmp_path, "case.jpg", (200, 90, 10))]
    fake = FakeChain(
        store,
        plans=[_plan("orange suitcase")],
        contents={(0, "orange suitcase"): "case1"},
    )
    narration = FakeNarration(_NARRATION_REPLY)
    manifest = _run_narrated(
        tmp_path, photos, fake, narration,
        item_names=["orange suitcase"],
        narrated_videos=[video],
    )
    record = manifest["narration"]["files"][0]
    assert record["error"]["code"] == "NO_AUDIO_TRACK"
    assert narration.calls == []
    assert manifest["items"][0]["status"] == "staged"


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
