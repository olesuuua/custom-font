"""Small FontForge-only cubic refit helpers used by Bold v3."""

import fontforge
import redraw_font as rf


def evenly_spaced_indices(points, count):
    count = max(3, min(count, len(points)))
    distances = [0.0]
    for index in range(1, len(points) + 1):
        left = points[index - 1]
        right = points[index % len(points)]
        distances.append(distances[-1] + rf.distance(left, right))
    total = distances[-1]
    result, cursor = [], 0
    for step in range(count):
        target = total * step / count
        while cursor + 1 < len(distances) and distances[cursor + 1] <= target:
            cursor += 1
        result.append(cursor % len(points))
    return sorted(set(result))


def curve_contour(points, anchor_count, tension=.30, corner_angle=42.0,
                  force_smooth=False):
    indices = evenly_spaced_indices(points, anchor_count)
    anchors = [points[index] for index in indices]
    count = len(anchors)
    incoming, outgoing, smooth = [], [], []
    for index, source_index in enumerate(indices):
        previous = anchors[(index - 1) % count]
        current = anchors[index]
        following = anchors[(index + 1) % count]
        before = rf.normalize(rf.subtract(current, previous))
        after = rf.normalize(rf.subtract(following, current))
        turn = rf.anchor_turn(
            points, source_index, min(4, max(1, len(points) // 30))
        )
        is_smooth = force_smooth or turn < corner_angle
        tangent = rf.normalize(rf.subtract(following, previous))
        incoming.append(tangent if is_smooth else before)
        outgoing.append(tangent if is_smooth else after)
        smooth.append(is_smooth)
    contour = fontforge.contour()
    contour.is_quadratic = False
    contour.moveTo(*anchors[0])
    for index in range(count):
        following = (index + 1) % count
        chord = rf.distance(anchors[index], anchors[following])
        control1 = rf.add(
            anchors[index], rf.multiply(outgoing[index], chord * tension)
        )
        control2 = rf.subtract(
            anchors[following], rf.multiply(incoming[following], chord * tension)
        )
        contour.cubicTo(control1, control2, anchors[following])
    contour.closed = True
    on_curves = [point for point in contour if point.on_curve]
    for point, is_smooth in zip(on_curves, smooth):
        point.type = fontforge.splineCurve if is_smooth else fontforge.splineCorner
    return contour


def curve_layer(point_sets, counts, tension=.30, corner_angle=42.0,
                force_smooth_indices=()):
    result = fontforge.layer()
    result.is_quadratic = False
    forced = set(force_smooth_indices)
    for index, (points, count) in enumerate(zip(point_sets, counts)):
        result += curve_contour(
            points, count, tension=tension, corner_angle=corner_angle,
            force_smooth=index in forced,
        )
    return result
