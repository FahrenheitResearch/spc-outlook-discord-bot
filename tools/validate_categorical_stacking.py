#!/usr/bin/env python3
"""Fail if categorical visible fills stop stacking lower risks under higher risks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402


RISK_AREA_TOLERANCE = 1.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--products",
        default="day1,day2,day3",
        help="Comma-separated product keys to validate. Defaults to day1,day2,day3.",
    )
    parser.add_argument(
        "--min-overlap-area",
        type=float,
        default=0.05,
        help="Minimum raw overlap area before a lower/higher pair is considered nested.",
    )
    parser.add_argument(
        "--min-visible-ratio",
        type=float,
        default=0.98,
        help="Required ratio of raw nested overlap still visible in the lower category fill.",
    )
    return parser.parse_args()


def safe_intersection_area(left: object, right: object) -> float:
    left = bot.repaired_outlook_geometry(left)
    right = bot.repaired_outlook_geometry(right)
    if left is None or right is None or left.is_empty or right.is_empty:
        return 0.0
    return bot.repaired_outlook_geometry(left.intersection(right)).area


def closed_sequence_reference_area(sequences: tuple[object, ...]) -> float | None:
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    geometries = []
    for sequence in sequences:
        if hasattr(sequence, "geom_type"):
            return None
        points = [tuple(point) for point in sequence if isinstance(point, tuple | list) and len(point) >= 2]
        if len(points) < 4 or points[0] != points[-1]:
            return None
        polygon = Polygon(points)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty:
            geometries.append(polygon)
    if not geometries:
        return None
    geometry = unary_union(geometries) if len(geometries) > 1 else geometries[0]
    return abs(geometry.area)


def validate_spec(spec: bot.BundleSpec, *, min_overlap_area: float, min_visible_ratio: float) -> list[str]:
    product = bot.parse_pts_text(bot.fetch_raw_pts_text_for_spec(spec), spec)
    raw_polygons = product.maps.get("categorical", {})
    raw_geometries = {}
    for label in bot.RISK_ORDER:
        geometry = bot.outlook_geometry_for_label(product, "categorical", label, raw_polygons.get(label, ()))
        if geometry is not None and not geometry.is_empty:
            raw_geometries[label] = geometry

    visible = bot.visible_outlook_fills_for_map("categorical", raw_geometries, bot.RISK_ORDER)
    failures: list[str] = []
    comparisons = 0
    for label, visible_geometry in visible.items():
        reference_area = closed_sequence_reference_area(raw_polygons.get(label, ()))
        if reference_area is None or reference_area < min_overlap_area:
            continue
        visible_area = visible_geometry.area
        allowed_area = max(reference_area * RISK_AREA_TOLERANCE, reference_area + min_overlap_area)
        if visible_area > allowed_area:
            failures.append(
                f"{spec.key} {product.product_id}: {label} visible fill area {visible_area:.2f} "
                f"exceeds closed-ring source area {reference_area:.2f}"
            )
    for lower_index, lower_label in enumerate(bot.RISK_ORDER[:-1]):
        lower = raw_geometries.get(lower_label)
        visible_lower = visible.get(lower_label)
        if lower is None or visible_lower is None or visible_lower.is_empty:
            continue
        for higher_label in bot.RISK_ORDER[lower_index + 1 :]:
            higher = raw_geometries.get(higher_label)
            if higher is None:
                continue
            raw_overlap = safe_intersection_area(lower, higher)
            if raw_overlap < min_overlap_area:
                continue
            visible_overlap = safe_intersection_area(visible_lower, higher)
            ratio = visible_overlap / raw_overlap if raw_overlap else 1.0
            comparisons += 1
            if ratio < min_visible_ratio:
                failures.append(
                    f"{spec.key} {product.product_id}: {lower_label} visible under {higher_label} "
                    f"fell to {ratio:.3f} of raw nested overlap"
                )
    print(f"{spec.key} {product.product_id}: checked {comparisons} nested categorical overlap(s)")
    return failures


def main() -> int:
    args = parse_args()
    wanted = {item.strip() for item in args.products.split(",") if item.strip()}
    specs = [spec for spec in bot.BUNDLES if spec.key in wanted]
    failures: list[str] = []
    for spec in specs:
        failures.extend(
            validate_spec(
                spec,
                min_overlap_area=args.min_overlap_area,
                min_visible_ratio=args.min_visible_ratio,
            )
        )
    if failures:
        print("Categorical stacking validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
