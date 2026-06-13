#!/usr/bin/env python3
"""Build a local official-vs-custom Day 1 ENH+ archive proof page."""

from __future__ import annotations

import argparse
import dataclasses
import html
import io
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402


SPC_ARCHIVE_BASE = "https://www.spc.noaa.gov/products/outlook/archive"
ARCHIVE_RANGE_URL = "https://www.spc.noaa.gov/cgi-bin-spc/getacrange-aws-py.pl"
CIG_START_ISSUE = "20260303_1630"
DAY1_SPEC = bot.BUNDLES[0]
MAP_LABELS = ("categorical", "tornado", "wind", "hail")
RISK_WORDS = (
    ("HIGH", re.compile(r"\bHIGH\s+RISK\b", re.IGNORECASE)),
    ("MDT", re.compile(r"\bMODERATE\s+RISK\b", re.IGNORECASE)),
    ("ENH", re.compile(r"\bENHANCED\s+RISK\b", re.IGNORECASE)),
)
RISK_SORT = {"ENH": 0, "MDT": 1, "HIGH": 2}
ROUGH_BOUNDS = (-140.0, 10.0, -45.0, 65.0)


@dataclasses.dataclass(frozen=True)
class ArchiveIssue:
    issue_id: str
    page_url: str
    max_risk: str


@dataclasses.dataclass(frozen=True)
class FetchResult:
    data: bytes
    url: str
    from_cache: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch every SPC Day 1 ENH/MDT/HIGH archive issue from the CIG start onward, "
            "render the bot's custom maps, and create a local side-by-side proof page."
        )
    )
    parser.add_argument("--start-issue", default=CIG_START_ISSUE, help="First issue id to include, YYYYMMDD_HHMM.")
    parser.add_argument(
        "--end-date",
        default=datetime.now(timezone.utc).strftime("%Y%m%d"),
        help="Last archive date to query, YYYYMMDD.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "data" / "day1-enh-validation"),
        help="Directory for cached SPC files and generated proof output.",
    )
    parser.add_argument("--concurrency", type=int, default=8, help="Concurrent SPC HTML/image fetches.")
    parser.add_argument("--limit", type=int, default=0, help="Only validate the first N matching issues.")
    parser.add_argument(
        "--only-issue",
        action="append",
        default=[],
        help="Validate only a specific issue id. Can be supplied more than once.",
    )
    parser.add_argument("--refresh", action="store_true", help="Refetch cached SPC archive inputs.")
    return parser.parse_args()


def cache_path_for_url(cache_root: Path, url: str, suffix: str | None = None) -> Path:
    parsed = urllib.parse.urlsplit(url)
    name = Path(parsed.path).name or "index.html"
    if suffix:
        name = f"{Path(name).stem}{suffix}"
    if not name:
        name = "download.bin"
    safe_query = re.sub(r"[^A-Za-z0-9_.-]+", "_", parsed.query)
    if safe_query:
        name = f"{Path(name).stem}_{safe_query}{Path(name).suffix}"
    return cache_root / name


def fetch_url(url: str, dest: Path, *, refresh: bool = False, timeout: int = 60) -> FetchResult:
    if dest.exists() and not refresh:
        return FetchResult(dest.read_bytes(), url, True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        request = urllib.request.Request(url, headers={"User-Agent": bot.USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return FetchResult(data, url, False)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * attempt)

    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_text(url: str, dest: Path, *, refresh: bool = False) -> str:
    result = fetch_url(url, dest, refresh=refresh)
    return result.data.decode("utf-8", errors="replace")


def archive_range_url(start_issue: str, end_date: str) -> str:
    start_date = start_issue.split("_", 1)[0]
    return f"{ARCHIVE_RANGE_URL}?date0={start_date}&date1={end_date}"


def extract_day1_links(index_html: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"(?P<path>/products/outlook/archive/(?P<year>\d{4})/day1otlk_(?P<issue>\d{8}_\d{4})\.html)",
        re.IGNORECASE,
    )
    links: dict[str, str] = {}
    for match in pattern.finditer(index_html):
        issue_id = match.group("issue")
        links[issue_id] = urllib.parse.urljoin("https://www.spc.noaa.gov", match.group("path").strip())
    return sorted(links.items())


def extract_product_text(page_html: str) -> str:
    pre_blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if pre_blocks:
        text = "\n".join(pre_blocks)
    else:
        text = re.sub(r"<[^>]+>", " ", page_html)
    return html.unescape(text)


def classify_max_risk(page_html: str) -> str | None:
    text = extract_product_text(page_html)
    for risk, pattern in RISK_WORDS:
        if pattern.search(text):
            return risk
    return None


def discover_matching_issues(
    output_dir: Path,
    *,
    start_issue: str,
    end_date: str,
    concurrency: int,
    refresh: bool,
    only_issues: set[str],
) -> list[ArchiveIssue]:
    if only_issues:
        links = [
            (
                issue_id,
                f"{SPC_ARCHIVE_BASE}/{issue_id[:4]}/day1otlk_{issue_id}.html",
            )
            for issue_id in sorted(only_issues)
            if issue_id >= start_issue
        ]
    else:
        index_url = archive_range_url(start_issue, end_date)
        index_html = fetch_text(
            index_url,
            output_dir / "cache" / "archive_range" / f"day1_{start_issue}_{end_date}.html",
            refresh=refresh,
        )
        links = [
            (issue_id, page_url)
            for issue_id, page_url in extract_day1_links(index_html)
            if issue_id >= start_issue
        ]

    def fetch_and_classify(issue_id: str, page_url: str) -> ArchiveIssue | None:
        page_html = fetch_text(
            page_url,
            output_dir / "cache" / "html" / f"day1otlk_{issue_id}.html",
            refresh=refresh,
        )
        max_risk = classify_max_risk(page_html)
        if max_risk is None:
            return None
        return ArchiveIssue(issue_id=issue_id, page_url=page_url, max_risk=max_risk)

    issues: list[ArchiveIssue] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(fetch_and_classify, issue_id, page_url) for issue_id, page_url in links]
        for index, future in enumerate(as_completed(futures), 1):
            issue = future.result()
            if issue is not None:
                issues.append(issue)
            if index % 50 == 0 or index == len(futures):
                print(f"classified {index}/{len(futures)} Day 1 archive pages; matches so far: {len(issues)}")

    issues.sort(key=lambda issue: (issue.issue_id, RISK_SORT.get(issue.max_risk, 99)))
    return issues


def extract_archive_links(page_url: str, page_html: str, issue_id: str) -> tuple[str | None, str | None]:
    hrefs = re.findall(r"""href=["']([^"']+)["']""", page_html, flags=re.IGNORECASE)
    geojson_url: str | None = None
    shapefile_url: str | None = None
    for href in hrefs:
        normalized = href.strip()
        if f"day1otlk_{issue_id}" not in normalized:
            continue
        if normalized.endswith("-geojson.zip"):
            geojson_url = archive_urljoin(page_url, normalized)
        elif normalized.endswith("-shp.zip"):
            shapefile_url = archive_urljoin(page_url, normalized)

    year = issue_id[:4]
    if shapefile_url is None:
        shapefile_url = f"{SPC_ARCHIVE_BASE}/{year}/day1otlk_{issue_id}-shp.zip"
    return geojson_url, shapefile_url


def archive_urljoin(page_url: str, href: str) -> str:
    normalized = href.strip()
    if normalized.startswith("archive/"):
        return urllib.parse.urljoin(f"{bot.SPC_BASE}/products/outlook/", normalized)
    return urllib.parse.urljoin(page_url, normalized)


def official_image_urls(page_url: str, page_html: str, issue_id: str) -> dict[str, str]:
    year = issue_id[:4]
    urls = {
        "categorical": f"{SPC_ARCHIVE_BASE}/{year}/day1otlk_{issue_id}_prt.png",
        "tornado": f"{SPC_ARCHIVE_BASE}/{year}/day1probotlk_{issue_id}_torn_prt.png",
        "wind": f"{SPC_ARCHIVE_BASE}/{year}/day1probotlk_{issue_id}_wind_prt.png",
        "hail": f"{SPC_ARCHIVE_BASE}/{year}/day1probotlk_{issue_id}_hail_prt.png",
    }
    candidates = re.findall(r"""(?:src|href)=["']([^"']+_prt\.png)["']""", page_html, flags=re.IGNORECASE)
    for raw in candidates:
        src = archive_urljoin(page_url, raw)
        name = Path(urllib.parse.urlsplit(src).path).name.lower()
        if f"_{issue_id.lower()}_" not in name:
            continue
        if name.startswith("day1otlk_"):
            urls["categorical"] = src
        elif "_torn_prt.png" in name:
            urls["tornado"] = src
        elif "_wind_prt.png" in name:
            urls["wind"] = src
        elif "_hail_prt.png" in name:
            urls["hail"] = src
    return urls


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def parse_shape_zip(data: bytes, issue: ArchiveIssue, shapefile_url: str) -> tuple[bot.PtsProduct, dict[str, Any]]:
    try:
        import shapefile
        from shapely.geometry import shape as shapely_shape
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("archive shapefile parsing requires pyshp and shapely") from exc

    layer_map = {
        "day1otlk_cat.lyr.shp": "categorical",
        "day1otlk_torn.lyr.shp": "tornado",
        "day1otlk_wind.lyr.shp": "wind",
        "day1otlk_hail.lyr.shp": "hail",
        "day1otlk_cigtorn.lyr.shp": "tornado",
        "day1otlk_cigwind.lyr.shp": "wind",
        "day1otlk_cighail.lyr.shp": "hail",
    }
    maps: dict[str, dict[str, list[Any]]] = {label: {} for label in MAP_LABELS}
    first_properties: dict[str, Any] = {}
    stats: dict[str, Any] = {
        "source": "shapefile",
        "layers": {},
        "null_shapes": 0,
        "invalid_shapes": 0,
        "empty_shapes": 0,
    }

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        names_by_lower = {name.lower(): name for name in names}
        for shp_member in names:
            base = Path(shp_member).name.lower()
            map_label = layer_map.get(base)
            if map_label is None:
                continue
            stem = shp_member[:-4]
            lower_stem = stem.lower()
            shx_member = names_by_lower.get(f"{lower_stem}.shx")
            dbf_member = names_by_lower.get(f"{lower_stem}.dbf")
            if shx_member is None or dbf_member is None:
                stats["layers"][base] = {"error": "missing shx/dbf sidecar"}
                continue

            with shapefile.Reader(
                shp=io.BytesIO(archive.read(shp_member)),
                shx=io.BytesIO(archive.read(shx_member)),
                dbf=io.BytesIO(archive.read(dbf_member)),
            ) as reader:
                fields = [field[0] for field in reader.fields[1:]]
                count = 0
                labels: set[str] = set()
                for shape_record in reader.iterShapeRecords():
                    if shape_record.shape.shapeType == shapefile.NULL:
                        stats["null_shapes"] += 1
                        continue
                    properties = dict(zip(fields, shape_record.record))
                    if not first_properties:
                        first_properties = dict(properties)
                    label = str(properties.get("LABEL") or "").strip()
                    if not label:
                        continue
                    geometry = shapely_shape(shape_record.shape.__geo_interface__)
                    if not geometry.is_valid:
                        stats["invalid_shapes"] += 1
                        geometry = geometry.buffer(0)
                    if geometry.is_empty:
                        stats["empty_shapes"] += 1
                        continue
                    maps.setdefault(map_label, {}).setdefault(label, []).append(geometry)
                    labels.add(label)
                    count += 1
                stats["layers"][base] = {"map": map_label, "features": count, "labels": sorted(labels)}

    issued, valid, valid_start = bot.geojson_time_range(first_properties)
    product = bot.PtsProduct(
        spec=DAY1_SPEC,
        product_id=f"archive-shp:{issue.issue_id}:{valid_start or issue.issue_id}",
        title=DAY1_SPEC.name,
        issued=issued or str(first_properties.get("ISSUE") or issue.issue_id),
        valid=valid,
        updated=issued or issue.issue_id,
        source="archive-shp",
        maps={key: {label: tuple(geometries) for label, geometries in labels.items()} for key, labels in maps.items()},
    )
    stats["url"] = shapefile_url
    return product, stats


def fetch_product_for_issue(
    issue: ArchiveIssue,
    page_html: str,
    output_dir: Path,
    *,
    refresh: bool,
) -> tuple[bot.PtsProduct, dict[str, Any]]:
    geojson_url, shapefile_url = extract_archive_links(issue.page_url, page_html, issue.issue_id)
    if geojson_url:
        try:
            data = fetch_url(
                geojson_url,
                cache_path_for_url(output_dir / "cache" / "geojson", geojson_url),
                refresh=refresh,
            ).data
            product = bot.parse_geojson_zip(data, DAY1_SPEC, geojson_url)
            return product, {"source": "geojson", "url": geojson_url}
        except Exception as exc:  # noqa: BLE001
            print(f"{issue.issue_id}: GeoJSON failed ({exc}); falling back to shapefile")

    if not shapefile_url:
        raise RuntimeError(f"{issue.issue_id}: no archived GeoJSON or shapefile link found")
    data = fetch_url(
        shapefile_url,
        cache_path_for_url(output_dir / "cache" / "shapefile", shapefile_url),
        refresh=refresh,
    ).data
    return parse_shape_zip(data, issue, shapefile_url)


def expected_labels_for_map(map_label: str) -> set[str]:
    if map_label == "categorical":
        return set(bot.RISK_ORDER)
    if map_label == "tornado":
        return {*bot.PROB_ORDER, *bot.CIG_ORDER}
    if map_label in {"wind", "hail"}:
        allowed_cig = bot.allowed_cig_labels_for_map(map_label)
        return {*bot.SEVERE_PROB_ORDER, *allowed_cig}
    return set()


def geometry_qc(product: bot.PtsProduct) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    summary: dict[str, Any] = {}
    for map_label in MAP_LABELS:
        label_map = product.maps.get(map_label, {})
        expected = expected_labels_for_map(map_label)
        unknown = sorted(label for label in label_map if label not in expected)
        if unknown:
            warnings.append(f"{map_label}: unknown labels {', '.join(unknown)}")
        map_summary: dict[str, Any] = {"labels": {}, "unknown_labels": unknown}
        for label, geometries in label_map.items():
            bounds: list[tuple[float, float, float, float]] = []
            invalid = 0
            out_of_bounds = 0
            for geometry in geometries:
                geom = geometry
                if not geom.is_valid:
                    invalid += 1
                if geom.is_empty:
                    continue
                minx, miny, maxx, maxy = geom.bounds
                bounds.append((float(minx), float(miny), float(maxx), float(maxy)))
                if minx < ROUGH_BOUNDS[0] or miny < ROUGH_BOUNDS[1] or maxx > ROUGH_BOUNDS[2] or maxy > ROUGH_BOUNDS[3]:
                    out_of_bounds += 1
            if invalid:
                warnings.append(f"{map_label}/{label}: {invalid} invalid geometries before render repair")
            if out_of_bounds:
                warnings.append(f"{map_label}/{label}: {out_of_bounds} rough CONUS bounds warnings")
            map_summary["labels"][label] = {
                "count": len(geometries),
                "invalid": invalid,
                "out_of_bounds": out_of_bounds,
                "bounds": bounds,
            }
        summary[map_label] = map_summary

    risk_labels = bot.risk_labels_from_product(product)
    if not any(label in {"ENH", "MDT", "HIGH"} for label in risk_labels):
        warnings.append("render source categorical labels do not contain ENH/MDT/HIGH")
    return summary, warnings


def write_official_images(
    issue: ArchiveIssue,
    page_html: str,
    output_dir: Path,
    *,
    refresh: bool,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    images: dict[str, Any] = {}
    urls = official_image_urls(issue.page_url, page_html, issue.issue_id)
    issue_dir = output_dir / "official" / issue.issue_id
    for map_label in MAP_LABELS:
        url = urls[map_label]
        dest = issue_dir / f"{map_label}.png"
        try:
            result = fetch_url(url, dest, refresh=refresh)
            dims = png_dimensions(result.data)
            if dims is None:
                warnings.append(f"official {map_label}: not a PNG")
            elif dims[0] < 700 or dims[1] < 450:
                warnings.append(f"official {map_label}: suspicious dimensions {dims[0]}x{dims[1]}")
            if len(result.data) < 10_000:
                warnings.append(f"official {map_label}: suspiciously small file ({len(result.data)} bytes)")
            images[map_label] = {
                "url": url,
                "path": str(dest.relative_to(output_dir)).replace("\\", "/"),
                "bytes": len(result.data),
                "dimensions": dims,
                "from_cache": result.from_cache,
            }
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"official {map_label}: fetch failed: {exc}")
            images[map_label] = {"url": url, "error": str(exc)}
    return images, warnings


def write_custom_images(
    issue: ArchiveIssue,
    product: bot.PtsProduct,
    output_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    images: dict[str, Any] = {}
    issue_dir = output_dir / "custom" / issue.issue_id
    issue_dir.mkdir(parents=True, exist_ok=True)
    for map_label in MAP_LABELS:
        dest = issue_dir / f"{map_label}.png"
        try:
            data = bot.render_pts_map_png(product, map_label)
            dest.write_bytes(data)
            dims = png_dimensions(data)
            if dims != (1630, 1110):
                warnings.append(f"custom {map_label}: unexpected dimensions {dims}")
            if len(data) < 50_000:
                warnings.append(f"custom {map_label}: suspiciously small file ({len(data)} bytes)")
            images[map_label] = {
                "path": str(dest.relative_to(output_dir)).replace("\\", "/"),
                "bytes": len(data),
                "dimensions": dims,
            }
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"custom {map_label}: render failed: {exc}")
            images[map_label] = {"error": str(exc)}
    return images, warnings


def validate_issue(issue: ArchiveIssue, output_dir: Path, *, refresh: bool) -> dict[str, Any]:
    page_html = fetch_text(
        issue.page_url,
        output_dir / "cache" / "html" / f"day1otlk_{issue.issue_id}.html",
        refresh=refresh,
    )
    warnings: list[str] = []
    official_images, official_warnings = write_official_images(issue, page_html, output_dir, refresh=refresh)
    warnings.extend(official_warnings)

    product, source_info = fetch_product_for_issue(issue, page_html, output_dir, refresh=refresh)
    geometry_summary, geometry_warnings = geometry_qc(product)
    warnings.extend(geometry_warnings)

    custom_images, custom_warnings = write_custom_images(issue, product, output_dir)
    warnings.extend(custom_warnings)

    return {
        "issue_id": issue.issue_id,
        "page_url": issue.page_url,
        "max_risk": issue.max_risk,
        "product_id": product.product_id,
        "issued": product.issued,
        "valid": product.valid,
        "risk_labels": bot.risk_labels_from_product(product),
        "source": source_info,
        "official_images": official_images,
        "custom_images": custom_images,
        "geometry": geometry_summary,
        "warnings": warnings,
        "status": "needs-review" if warnings else "ok",
    }


def html_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def html_text(value: Any) -> str:
    return html.escape(str(value))


def write_html(output_dir: Path, manifest: dict[str, Any]) -> None:
    entries = manifest["issues"]
    ok_count = sum(1 for entry in entries if not entry["warnings"])
    warning_count = len(entries) - ok_count
    generated = manifest["generated_at"]
    styles = """
body{margin:0;background:#f3f1e8;color:#191919;font-family:Arial,Helvetica,sans-serif}
header{position:sticky;top:0;z-index:10;background:#fff;border-bottom:1px solid #1f1f1f;padding:14px 18px}
h1{font-size:24px;margin:0 0 6px}p{margin:4px 0}.meta{font-size:14px;color:#444}
main{padding:18px;max-width:1500px;margin:0 auto}.issue{background:#fff;border:1px solid #2b2b2b;margin:0 0 22px}
.issue-head{display:flex;gap:12px;align-items:center;justify-content:space-between;border-bottom:1px solid #2b2b2b;padding:10px 12px}
h2{font-size:20px;margin:0}.badge{font-weight:700;border:1px solid #111;padding:3px 7px;background:#eef6ee}
.warn .badge{background:#ffe0df}.details{font-size:13px;color:#333;margin-top:3px}
.warnings{background:#fff3cd;border-bottom:1px solid #a98b00;padding:8px 12px;font-size:14px}
.pairs{padding:12px}.map-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
figure{margin:0;border:1px solid #b8b8b8;background:#f8f8f8}figcaption{font-weight:700;font-size:14px;padding:6px 8px;background:#ececec;border-bottom:1px solid #b8b8b8}
img{display:block;width:100%;height:auto;background:#ddd}.missing{padding:70px 10px;text-align:center;color:#8b0000;font-weight:700}
a{color:#0645ad}@media(max-width:900px){.map-row{grid-template-columns:1fr}.issue-head{display:block}}
"""
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>SPC Day 1 ENH+ Archive Validation</title>",
        f"<style>{styles}</style>",
        "<header>",
        "<h1>SPC Day 1 ENH+ Archive Validation</h1>",
        (
            f"<p class='meta'>Generated {html_text(generated)}. "
            f"CIG start: {html_text(manifest['start_issue'])}. "
            f"Validated issues: {len(entries)}. Clean automated checks: {ok_count}. Needs visual/flag review: {warning_count}.</p>"
        ),
        "<p class='meta'>Official SPC archive images are shown only for local comparison. Custom images are unofficial fast renders from archived SPC geometry.</p>",
        "</header><main>",
    ]

    for entry in entries:
        issue_class = "issue warn" if entry["warnings"] else "issue"
        badge = "Needs review" if entry["warnings"] else "Automated checks clean"
        parts.append(f"<section class='{issue_class}' id='issue-{html_attr(entry['issue_id'])}'>")
        parts.append("<div class='issue-head'>")
        parts.append(
            "<div>"
            f"<h2>{html_text(entry['issue_id'])} - {html_text(entry['max_risk'])}</h2>"
            f"<div class='details'>Issued {html_text(entry.get('issued') or 'unknown')} | Valid {html_text(entry.get('valid') or 'unknown')} | "
            f"Source {html_text(entry['source'].get('source'))} | "
            f"<a href='{html_attr(entry['page_url'])}'>SPC archive page</a></div>"
            "</div>"
        )
        parts.append(f"<span class='badge'>{html_text(badge)}</span>")
        parts.append("</div>")
        if entry["warnings"]:
            parts.append("<div class='warnings'><strong>Flags:</strong> " + html_text("; ".join(entry["warnings"])) + "</div>")
        parts.append("<div class='pairs'>")
        for map_label in MAP_LABELS:
            official = entry["official_images"].get(map_label, {})
            custom = entry["custom_images"].get(map_label, {})
            parts.append("<div class='map-row'>")
            for side, image in (("Official SPC", official), ("Our render", custom)):
                parts.append("<figure>")
                parts.append(f"<figcaption>{html_text(side)} - {html_text(map_label.title())}</figcaption>")
                if "path" in image:
                    parts.append(f"<img loading='lazy' src='{html_attr(image['path'])}' alt='{html_attr(side)} {html_attr(map_label)} {html_attr(entry['issue_id'])}'>")
                else:
                    parts.append(f"<div class='missing'>{html_text(image.get('error', 'missing image'))}</div>")
                parts.append("</figure>")
            parts.append("</div>")
        parts.append("</div></section>")
    parts.append("</main>")
    (output_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    only_issues = set(args.only_issue)

    print(f"Fetching Day 1 archive index from {args.start_issue} through {args.end_date}")
    issues = discover_matching_issues(
        output_dir,
        start_issue=args.start_issue,
        end_date=args.end_date,
        concurrency=args.concurrency,
        refresh=args.refresh,
        only_issues=only_issues,
    )
    if args.limit:
        issues = issues[: args.limit]
    print(f"Validating {len(issues)} Day 1 ENH+ archive issues")

    entries: list[dict[str, Any]] = []
    for index, issue in enumerate(issues, 1):
        print(f"[{index}/{len(issues)}] {issue.issue_id} {issue.max_risk}")
        try:
            entries.append(validate_issue(issue, output_dir, refresh=args.refresh))
        except Exception as exc:  # noqa: BLE001
            entries.append(
                {
                    "issue_id": issue.issue_id,
                    "page_url": issue.page_url,
                    "max_risk": issue.max_risk,
                    "warnings": [f"validation failed: {exc}"],
                    "status": "failed",
                    "source": {},
                    "official_images": {},
                    "custom_images": {},
                    "geometry": {},
                }
            )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_issue": args.start_issue,
        "end_date": args.end_date,
        "issue_count": len(entries),
        "clean_count": sum(1 for entry in entries if not entry["warnings"]),
        "warning_count": sum(1 for entry in entries if entry["warnings"]),
        "risk_counts": {
            risk: sum(1 for entry in entries if entry["max_risk"] == risk)
            for risk in ("ENH", "MDT", "HIGH")
        },
        "source_counts": {},
    }
    for entry in entries:
        source = entry.get("source", {}).get("source", "unknown")
        summary["source_counts"][source] = summary["source_counts"].get(source, 0) + 1

    manifest = {**summary, "issues": entries}
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_html(output_dir, manifest)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Proof page: {output_dir / 'index.html'}")


if __name__ == "__main__":
    main()
