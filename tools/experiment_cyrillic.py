import sys
from pathlib import Path

import fontforge
import psMat

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simplify_font as sf

ROOT = Path(__file__).resolve().parents[1]
source = fontforge.open(str(ROOT / "qa/assets/original.ttf"))
simplified = fontforge.open(str(ROOT / "qa/assets/simplified.ttf"))

for name in ("uni0414", "uni0416"):
    target = source[name]
    original = target.foreground.dup()
    bbox = sf.bbox_of_sets(sf.layer_point_sets(original))
    original_mask = sf.rasterize(sf.layer_point_sets(original), bbox)
    original_masks = {size: sf.downsample(original_mask, sf.RASTER_SIZE, size) for size in sf.RASTER_SIZES}
    topology = sf.topology(original_mask)
    topologies = sf.topology_at_sizes(sf.layer_point_sets(original), bbox)
    work = fontforge.font()
    glyph = work.createChar(target.unicode, name)
    glyph.width = target.width
    print(name)
    for error in (0.5, 1, 2, 4, 8, 12, 16, 24, 32, 48, 64):
        glyph.foreground = original.dup()
        glyph.simplify(error, ("mergelines", "choosehv", "forcelines"))
        candidate = glyph.foreground.dup()
        points = sum(len(contour) for contour in candidate)
        metrics = sf.evaluate_candidate(candidate, sf.layer_point_sets(original), original_mask, original_masks, bbox, topology, topologies)
        print(error, points, round(metrics["mse"], 4), round(metrics["ink_iou"], 4), round(metrics["false_negative_ink"], 4), metrics["topology_status"])
    work.close()
simplified.close(); source.close()
