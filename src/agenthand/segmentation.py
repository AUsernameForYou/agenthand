"""Segmentation quality control and the bounded mask-recovery loop.

cut_out's model step, between the wire boundary (sam_masks) and the
asset store:

- ``assess_mask`` quality-checks one decoded mask. A usable product
  mask covers a sane fraction of the frame (not empty, not ~the whole
  frame) and is dominated by a single connected region. The thresholds
  are module constants, chosen for product photos: the product is one
  object occupying a real share of its photo.
- ``extract_foreground`` builds the transparent foreground: source
  pixels exactly where the mask is on, transparent black elsewhere.
  Nothing is regenerated — this is the Fidelity Lock's extraction step.
- ``run_cut_pipeline`` acquires a fixed phrase ensemble and band tiles.
  All records and provenance survive; source pixels alone become foreground.
- Legacy history: ``run_attempts`` was the recovery loop the tool contract promises:
  try the concept phrase; on a zero-match or a poor mask, retry ONCE
  with the first alternate phrase. Every attempt is traced (phrase,
  outcome, coverage, QA detail). Total failure raises
  CutOutResultError carrying the trace; the spend for every attempt
  stays counted because run_attempts is the fn of one billed_call
  whose usage reports one image per attempt made.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO

import cv2
import numpy as np
from PIL import Image

from . import meta_client
from .sam_masks import DecodedMask, SamOutputError, parse_sam_masks

# QA thresholds. Coverage is mask pixels / frame pixels.
MIN_COVERAGE = 0.001  # below this the mask is effectively empty
MAX_COVERAGE = 0.98  # at or above this the "mask" is ~the whole frame
MIN_DOMINANT_FRACTION = 0.75  # largest component / mask pixels

# The SAM working-resolution cap (decision 68). SAM is sent the stored
# photo as-is up to this many pixels; past it, only the SAM-bound copy
# is downscaled (prepare_sam_png) and the returned mask is mapped back
# onto the full frame (map_mask_to_frame) before anything is cut.
#
# The number is the pipeline's proven envelope, not a guessed provider
# limit: every still the room runs ever sent SAM was 1206x1608 =
# 1,939,248 px (dozens of accepted calls across the v1/v2, narrated,
# and acceptance runs in both builds, plus the M5 working-resolution
# pass), and every video frame was 720x1280, below it. The failure this
# cap answers is measured too: the 4284x5712 (24 MP) original of
# IMG_1340 drew HTTP 413 from SAM's /responses endpoint (M5 live
# proof). The cap math below maps that exact original back onto
# 1206x1608, the frame size already proven live.
SAM_MAX_PIXELS = 1_939_248  # 1206 * 1608

# Mask provenance values recorded on the mask asset and in draw's
# receipt. "none": SAM saw the source frame itself, the mask needed no
# mapping. "bilinear_threshold_128": SAM saw a downscaled copy and its
# mask was mapped back onto the source frame by a bilinear resize
# thresholded at 128 (Fable's masking detail from the M5 junction
# Gauntlet: bilinear on the soft mask + threshold, never nearest on
# binary, so the mapped edge is a measured blend, not a guess).
MASK_SCALE_NONE = "none"
MASK_SCALE_BILINEAR_THRESHOLD = "bilinear_threshold_128"


def sam_frame_size(
    width: int, height: int, max_pixels: int | None = None
) -> tuple[int, int]:
    """The frame SAM sees for a source of (width, height).

    Unchanged when the source fits the working cap; otherwise the
    largest same-aspect frame that fits it. The decrement loop after
    the float math keeps the result inside the cap even when rounding
    lands a pixel over, and makes the answer an integer fact both
    builds reproduce exactly. The cap default resolves at call time
    (not def time) so the module constant stays the single knob.
    """
    if max_pixels is None:
        max_pixels = SAM_MAX_PIXELS
    if width * height <= max_pixels:
        return width, height
    scale = math.sqrt(max_pixels / (width * height))
    w = max(1, int(width * scale))
    h = max(1, int(height * scale))
    while w * h > max_pixels:
        if w >= h:
            w -= 1
        else:
            h -= 1
    return w, h


def prepare_sam_png(
    source_image: Image.Image,
    stored_png_bytes: bytes,
    max_pixels: int | None = None,
) -> dict:
    """The SAM-bound payload for one source photo.

    Returns png_bytes (what segment_image sends), sam_width /
    sam_height (the frame the returned masks will be decoded against),
    and scale_method (the mask-provenance value for the receipt).
    Inside the cap the stored bytes pass through byte-identical, so
    the common path is exactly the pre-scaling pipeline; past it, a
    Lanczos downscale of the decoded source becomes the SAM copy. The
    stored photo itself is never resized — the downscale exists only
    in this payload.
    """
    width, height = source_image.size
    sam_width, sam_height = sam_frame_size(width, height, max_pixels)
    if (sam_width, sam_height) == (width, height):
        return {
            "png_bytes": stored_png_bytes,
            "sam_width": width,
            "sam_height": height,
            "scale_method": MASK_SCALE_NONE,
        }
    small = source_image.convert("RGBA").resize(
        (sam_width, sam_height), Image.LANCZOS
    )
    buffer = BytesIO()
    small.save(buffer, format="PNG")
    return {
        "png_bytes": buffer.getvalue(),
        "sam_width": sam_width,
        "sam_height": sam_height,
        "scale_method": MASK_SCALE_BILINEAR_THRESHOLD,
    }


def map_mask_to_frame(
    mask_image: Image.Image, frame_width: int, frame_height: int
) -> Image.Image:
    """Map a SAM-frame mask onto the full source frame.

    Identity when the sizes already match. Otherwise a bilinear
    upscale of the (binary) mask, thresholded at 128 back to binary:
    a full-frame pixel is on exactly when its bilinear sample of the
    SAM mask is at least half on. The foreground is then cut from the
    full-resolution source through this mask, so the receipt binds
    full-resolution pixels, not the working copy's.
    """
    if mask_image.size == (frame_width, frame_height):
        return mask_image
    soft = mask_image.convert("L").resize(
        (frame_width, frame_height), Image.BILINEAR
    )
    return soft.point(lambda v: 255 if v >= 128 else 0)


class CutOutResultError(RuntimeError):
    """Raised when no attempt produced a usable mask.

    Attributes:
        code: "NO_MATCH" when the final attempt returned no mask at
            all, "POOR_MASK" when the final attempt returned a mask
            that failed QA, "BAD_MODEL_RESPONSE" when SAM output
            could not be parsed/decoded.
        attempts: The full attempt trace (see run_attempts).
    """

    def __init__(self, message: str, *, code: str, attempts: list[dict], records: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.attempts = attempts
        self.records = records or []


@dataclass
class MaskAssessment:
    mask_pixels: int
    coverage: float  # mask pixels / frame pixels
    components: int  # connected components in the mask
    dominant_fraction: float  # largest component / mask pixels
    accepted: bool
    rejection: str | None  # "empty" | "full_frame" | "fragmented" | None


def _label_components(binary: np.ndarray, width: int, height: int) -> tuple[int, int]:
    """(component_count, largest_component_size) of a 0/1 raster.

    4-connectivity labelling via OpenCV — validated count-for-count
    against the flood fill it replaced, over randomized rasters.
    ``binary`` is a uint8 array of any shape whose product is
    width * height; nonzero counts as set.
    """
    raster = (binary.reshape(height, width) != 0).astype(np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        raster, connectivity=4
    )
    if count <= 1:
        return 0, 0
    return int(count - 1), int(stats[1:, cv2.CC_STAT_AREA].max())


def assess_mask(mask_image: Image.Image) -> MaskAssessment:
    """Quality-check one full-frame binary mask ("L", 255 = on)."""
    width, height = mask_image.size
    frame_pixels = width * height
    values = np.frombuffer(mask_image.tobytes(), dtype=np.uint8)
    mask_pixels = int(np.count_nonzero(values))
    coverage = mask_pixels / frame_pixels if frame_pixels else 0.0
    if mask_pixels == 0:
        return MaskAssessment(0, 0.0, 0, 0.0, False, "empty")
    components, largest = _label_components(values, width, height)
    dominant = largest / mask_pixels
    if coverage < MIN_COVERAGE:
        rejection: str | None = "empty"
    elif coverage >= MAX_COVERAGE:
        rejection = "full_frame"
    elif dominant < MIN_DOMINANT_FRACTION:
        rejection = "fragmented"
    else:
        rejection = None
    return MaskAssessment(
        mask_pixels=mask_pixels,
        coverage=coverage,
        components=components,
        dominant_fraction=dominant,
        accepted=rejection is None,
        rejection=rejection,
    )


# The mask outline colour on review overlays: the brand cyan, chosen
# to be visible against most product photos and impossible to mistake
# for a pixel the pipeline produced (the pipeline never paints).
OVERLAY_OUTLINE_RGBA = (0, 175, 225, 255)


def mask_overlay_image(source: Image.Image, mask_image: Image.Image) -> Image.Image:
    """The source photo with the mask's outline drawn over it.

    The review artifact for the lanes' top risk — a wrong mask under a
    perfect receipt. The outline is the set of mask-on pixels with a
    4-neighbour that is mask-off (or outside the frame), painted in
    OVERLAY_OUTLINE_RGBA; every other pixel is the source untouched,
    so a human or an agent can see exactly what the mask selected.
    """
    source_rgba = source.convert("RGBA")
    mask = mask_image.convert("L")
    if mask.size != source_rgba.size:
        raise ValueError(
            f"Mask {mask.size} does not cover the source frame {source_rgba.size}."
        )
    width, height = mask.size
    on = (
        np.frombuffer(mask.tobytes(), dtype=np.uint8).reshape(height, width) != 0
    )
    # Outline = on-pixels with a 4-neighbour off or outside the frame
    # (the zero padding stands in for "outside").
    padded = np.pad(on, 1, constant_values=False)
    surrounded = (
        padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    outline = on & ~surrounded
    outline_image = Image.fromarray(
        (outline * 255).astype(np.uint8), mode="L"
    )
    source_rgba.paste(OVERLAY_OUTLINE_RGBA, (0, 0), outline_image)
    return source_rgba


def review_mask(mask_image: Image.Image, source: Image.Image | None = None) -> dict:
    """Sanity signals for one accepted mask, for the cut_out result.

    A receipt can only prove the SELECTED pixels survived; whether the
    selection is the product is a judgement the pipeline must support,
    not silently make (the J2 Gauntlet's mask-correctness gate).
    identify emits no spatial extent to compare against, so the gate
    reports the mask's own geometry — bounding box, the box's fill
    ratio, coverage, which frame edges the mask touches — and flags
    the shapes that are wrong in practice: a mask touching all four
    frame edges is usually the background selected by mistake, and a
    bounding box that dwarfs its filled area is a scattered selection
    wearing one object's name. The overlay asset is the visual half of
    the same gate; these numbers ride beside it in the result.
    """
    mask = mask_image.convert("L")
    width, height = mask.size
    on = np.frombuffer(mask.tobytes(), dtype=np.uint8).reshape(height, width)
    mask_pixels = int(np.count_nonzero(on))
    warnings: list[str] = []
    bbox = None
    bbox_fill = None
    touches: list[str] = []
    if mask_pixels:
        ys, xs = np.nonzero(on)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        bbox = [x0, y0, x1 + 1, y1 + 1]
        bbox_fill = round(mask_pixels / ((x1 - x0 + 1) * (y1 - y0 + 1)), 6)
        if x0 == 0:
            touches.append("left")
        if x1 == width - 1:
            touches.append("right")
        if y0 == 0:
            touches.append("top")
        if y1 == height - 1:
            touches.append("bottom")
        if len(touches) == 4:
            warnings.append(
                "mask touches all four frame edges; this usually means "
                "the background was selected instead of the product"
            )
        if bbox_fill is not None and bbox_fill < 0.25:
            warnings.append(
                "mask fills less than a quarter of its bounding box; "
                "the selection is scattered or frame-like, review the "
                "overlay before listing"
            )
    from .cut_quality import grade_cut

    return {
        "cut_quality": grade_cut(source, mask),
        "mask_pixels": mask_pixels,
        "coverage": round(mask_pixels / (width * height), 6) if width * height else 0.0,
        "bbox": bbox,
        "bbox_fill": bbox_fill,
        "touches_frame_edges": touches,
        "warnings": warnings,
    }


def mask_iou(a: Image.Image, b: Image.Image) -> float:
    """Intersection over union of two masks' on-pixels.

    The golden-fixture gate's metric: a decoded mask is compared
    against the mask it should have produced, so a segmentation
    regression (or the wrong object winning the attempt loop) shows
    up as a number in tests instead of a wrong listing in production.
    """
    if a.size != b.size:
        raise ValueError(f"Mask sizes differ: {a.size} vs {b.size}.")
    a_on = np.frombuffer(a.convert("L").tobytes(), dtype=np.uint8) != 0
    b_on = np.frombuffer(b.convert("L").tobytes(), dtype=np.uint8) != 0
    intersection = int(np.count_nonzero(a_on & b_on))
    union = int(np.count_nonzero(a_on | b_on))
    return intersection / union if union else 1.0


def extract_foreground(source: Image.Image, mask_image: Image.Image) -> Image.Image:
    """The product layer: source pixels through the mask, nothing else.

    Where the mask is on, the source pixel is copied byte-for-byte;
    where it is off, the pixel becomes transparent black. The mask is
    binary (0/255), so the composite is an exact selection, not a blend.
    """
    from .assets import carry_color_profile

    source_rgba = source.convert("RGBA")
    transparent = Image.new("RGBA", source_rgba.size, (0, 0, 0, 0))
    # Image.composite builds a fresh image and drops the source's ICC
    # profile; re-attach it so the product layer keeps the photo's
    # colour space (see assets.carry_color_profile).
    return carry_color_profile(
        source_rgba, Image.composite(source_rgba, transparent, mask_image)
    )


PIPELINE_VERSION = 'agenthand.cut-pipeline/1'
MIN_TILE_FRAME_SIDE = 128  # sub-thumbnail controls contain no native thin detail


def _body_score(record: dict, records: list[dict]) -> tuple:
    """Agreement, compactness and centering; area alone never chooses a product."""
    from .mask_evidence import geometry
    image = record['image']
    metrics, _, _ = geometry(np.asarray(image))
    box = metrics['body_bbox']
    cx = (box[0]+box[2])/2/image.width
    cy = (box[1]+box[3])/2/image.height
    agreement = max((mask_iou(image,r['image']) for r in records if r['phrase'] != record['phrase']), default=0.)
    defects = (metrics['top_concavity_pixels']+metrics['top_island_pixels'])/max(1,metrics['mask_pixels'])
    score = agreement + metrics['bbox_fill'] - abs(cx-.5) - abs(cy-.5) - 2*defects
    return (score, metrics['dominant_fraction'])


def band_tiles(size: tuple[int,int], bbox: tuple[int,int,int,int], slots: int) -> list[dict]:
    """Fixed bottom/top coverage, overlapping horizontally; every request fits cap.

    Full native resolution whenever the band fits its allocation. Very large
    bands get minimally scaled within each allotted crop, explicitly recorded.
    """
    w,h=size; x0,y0,x1,y1=bbox; bw,bh=x1-x0,y1-y0
    if min(w,h)<MIN_TILE_FRAME_SIDE or not bw or not bh or slots<2: return []
    bands=[('support',max(0,y1-int(.1*bh)), min(h,y1+max(16,int(.5*bh)))),
           ('top',max(0,y0-int(.1*bh)), min(h,y0+max(16,int(.35*bh))))]
    result=[]
    for index,(kind,ya,yb) in enumerate(bands):
        allotted = slots//2 + (slots%2 if index==0 else 0)
        needed = max(1,math.ceil(bw*(yb-ya)/(.85*SAM_MAX_PIXELS)))
        count = min(needed,allotted)
        tile_w = bw if count==1 else min(bw,math.ceil(bw/(count-.12*(count-1))))
        for i in range(count):
            xa=x0 if count==1 else x0+round(i*(bw-tile_w)/(count-1))
            result.append({'kind':kind,'bbox':[xa,ya,xa+tile_w,yb]})
    return result


def _classify_component(binary: np.ndarray, body: np.ndarray, source_rgb: np.ndarray, bbox: tuple, kind: str) -> str:
    """Only narrow, observed members touching the body may expand foreground."""
    from .mask_evidence import _bbox_of
    x0,y0,x1,y1=bbox; bw,bh=x1-x0,y1-y0
    cx0,cy0,cx1,cy1=_bbox_of(binary)
    area=int(binary.sum()); overlap=int((binary&body).sum())
    if not area: return 'debris'
    if overlap/area>.5: return 'body'
    if cy1 <= y0+.35*bh: return 'occluder'
    height=cy1-cy0; mean_width=area/max(1,height)
    touching=cv2.dilate(body.astype(np.uint8),np.ones((3,3),np.uint8),iterations=3)>0
    in_zone=cy0>=y1-.15*bh and cy1<=y1+.6*bh and cx0>=x0-.05*bw and cx1<=x1+.05*bw
    luma=source_rgb.mean(axis=2)
    floor=luma[min(y1,body.shape[0]-1):,x0:x1]
    observed=float(luma[binary].mean()) < .65*float(np.percentile(floor,90)) if floor.size else False
    if kind=='support' and in_zone and (binary&touching).any() and height>=12 and height/max(1,mean_width)>=4 and mean_width<=.06*bw and cy1<body.shape[0] and observed:
        return 'support'
    return 'debris'


def run_cut_pipeline(source: Image.Image, stored_png_bytes: bytes, phrases: list[str], *, model: str, client=None) -> dict:
    """Acquire the fixed phrase ensemble and bounded top/support detail.

    Must be invoked inside billed_call. Every decoded record is returned,
    including rejected/debris records, with source coordinates and provenance.
    """
    from .mask_evidence import geometry, _components
    phrases=list(dict.fromkeys(phrases[:3]))
    if not phrases: raise ValueError('at least one fixed phrase is required')
    records=[]; attempts=[]
    payload=prepare_sam_png(source,stored_png_bytes)
    fullbox=[0,0,source.width,source.height]

    def acquire(phrase, tile, data):
        output,_=meta_client.segment_image(data['png_bytes'],phrase,model=model,client=client)
        trace={'phrase':phrase,'tile':tile,'records':0,'sam_width':data['sam_width'],'sam_height':data['sam_height'],'scale_method':data['scale_method']}
        try: masks=parse_sam_masks(output,data['sam_width'],data['sam_height'])
        except SamOutputError as exc:
            trace['outcome']='unparseable';attempts.append(trace)
            raise CutOutResultError(str(exc),code='BAD_MODEL_RESPONSE',attempts=attempts,records=records) from exc
        trace['records']=len(masks)
        trace['outcome']='no_match' if not masks else 'poor_mask'
        for mask in masks:
            box=tile['bbox']; local=map_mask_to_frame(mask.image,box[2]-box[0],box[3]-box[1])
            mapped=Image.new('L',source.size); mapped.paste(local,(box[0],box[1]))
            qa=assess_mask(mapped)
            records.append({'image':mapped,'object_id':mask.object_id,'phrase':phrase,'tile':tile,
                            'sam_width':data['sam_width'],'sam_height':data['sam_height'],
                            'scale_method':data['scale_method'],'assessment':qa,'role':'debris','components':[]})
            if qa.accepted:
                trace.update(object_id=mask.object_id,coverage=round(qa.coverage,6),mask_pixels=qa.mask_pixels,
                             components=qa.components,dominant_fraction=round(qa.dominant_fraction,6),outcome='accepted')
            else:
                trace.update(coverage=round(qa.coverage,6),mask_pixels=qa.mask_pixels,components=qa.components,dominant_fraction=round(qa.dominant_fraction,6))
                trace['rejection']=qa.rejection
        attempts.append(trace)

    for phrase in phrases: acquire(phrase,{'kind':'global','bbox':fullbox},payload)
    usable=[r for r in records if r['assessment'].accepted]
    if not usable:
        code='POOR_MASK' if records else 'NO_MATCH'
        raise CutOutResultError('No usable global body in fixed phrase ensemble.',code=code,attempts=attempts,records=records)
    selected=max(usable,key=lambda r:_body_score(r,usable))
    metrics,body,_=geometry(np.asarray(selected['image']))
    bbox=tuple(metrics['body_bbox'])
    for i,tile in enumerate(band_tiles(source.size,bbox,meta_client.SAM_MAX_ATTEMPTS-len(attempts))):
        crop=source.crop(tuple(tile['bbox'])); b=BytesIO();crop.save(b,format='PNG')
        data=prepare_sam_png(crop,b.getvalue())
        acquire(phrases[i%len(phrases)],tile,data)
    rgb=np.asarray(source.convert('RGB'))
    merged=body.copy()
    for r in records:
        roles=[]
        for box,local in _components(np.asarray(r['image'])>0):
            binary=np.zeros_like(body);binary[box[1]:box[3],box[0]:box[2]]=local
            role=_classify_component(binary,body,rgb,bbox,r['tile']['kind'])
            roles.append({'role':role,'bbox':list(box),'pixels':int(local.sum())})
            if role=='support': merged|=binary
            if r['tile']['kind']=='support' and role=='body':
                # SAM may return a leg attached to a body fragment; assess
                # the new pixels independently, rather than discarding them.
                for extra_box,extra_local in _components(binary & ~body):
                    extra=np.zeros_like(body)
                    extra[extra_box[1]:extra_box[3],extra_box[0]:extra_box[2]]=extra_local
                    extra_role=_classify_component(extra,body,rgb,bbox,'support')
                    roles.append({'role':extra_role,'bbox':list(extra_box),'pixels':int(extra_local.sum())})
                    if extra_role=='support': merged|=extra
        r['components']=roles
        r['role']='body' if r is selected else (roles[0]['role'] if roles and len({p['role'] for p in roles})==1 else 'mixed')
    mask=Image.fromarray((merged*255).astype(np.uint8))
    return {'mask_image':mask,'phrase':selected['phrase'],'attempts':attempts,'records':records,
            'pipeline_version':PIPELINE_VERSION,'usage':{'images':len(attempts)}}


def run_attempts(png_bytes: bytes, phrases: list[str], *, model: str, frame_width: int, frame_height: int, client=None) -> dict:
    """Compatibility entry point; uses the same ensemble policy as cut_out."""
    with Image.open(BytesIO(png_bytes)) as image: source=image.convert('RGBA')
    if source.size != (frame_width,frame_height): raise ValueError('SAM frame mismatch')
    return run_cut_pipeline(source,png_bytes,phrases,model=model,client=client)
