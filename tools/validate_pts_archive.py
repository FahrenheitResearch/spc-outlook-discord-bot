#!/usr/bin/env python3
"""Fetch archived SPC PTS products and render them through the PTS-only path."""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402


ARCHIVE_RANGE_URL = "https://www.spc.noaa.gov/cgi-bin-spc/getacrange-aws-py.pl"
SPC_ARCHIVE_BASE = "https://www.spc.noaa.gov/products/outlook/archive"
DAY48_ARCHIVE_BASE = "https://www.spc.noaa.gov/products/exper/day4-8/archive"
SPECS = {spec.key: spec for spec in bot.BUNDLES}
AWIPS_FOR_KEY = {
    "day1": "PTSDY1",
    "day2": "PTSDY2",
    "day3": "PTSDY3",
    "day4-8": "PTSD48",
}
PAGE_PATTERNS = {
    "day1": re.compile(r"/products/outlook/archive/(?P<year>\d{4})/day1otlk_(?P<issue>\d{8}_\d{4})\.html", re.I),
    "day2": re.compile(r"/products/outlook/archive/(?P<year>\d{4})/day2otlk_(?P<issue>\d{8}_\d{4})\.html", re.I),
    "day3": re.compile(r"/products/outlook/archive/(?P<year>\d{4})/day3otlk_(?P<issue>\d{8}_\d{4})\.html", re.I),
    "day4-8": re.compile(r"/products/exper/day4-8/archive/(?P<year>\d{4})/day4-8_(?P<issue>\d{8})\.html", re.I),
}


@dataclasses.dataclass(frozen=True)
class ArchiveIssue:
    key: str
    issue: str
    page_url: str


def parse_args() -> argparse.Namespace:
    default_end = datetime.now(timezone.utc).date()
    default_start = default_end - timedelta(days=31)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default=default_start.strftime("%Y%m%d"), help="First archive date, YYYYMMDD.")
    parser.add_argument("--end-date", default=default_end.strftime("%Y%m%d"), help="Last archive date, YYYYMMDD.")
    parser.add_argument(
        "--products",
        default="day1,day2,day3,day4-8",
        help="Comma-separated product keys: day1, day2, day3, day4-8.",
    )
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "pts-archive-validation"))
    parser.add_argument("--limit", type=int, default=0, help="Validate only the newest N discovered issues.")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent archive page/text fetches.")
    parser.add_argument("--render-images", action="store_true", help="Write rendered PNGs for every issue.")
    parser.add_argument("--refresh", action="store_true", help="Refetch cached SPC archive inputs.")
    return parser.parse_args()


def fetch_bytes(url: str, dest: Path, *, refresh: bool = False, timeout: int = 45) -> bytes:
    if dest.exists() and not refresh:
        return dest.read_bytes()
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
            return data
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_text(url: str, dest: Path, *, refresh: bool = False) -> str:
    return fetch_bytes(url, dest, refresh=refresh).decode("utf-8", errors="replace")


def archive_range_url(start_date: str, end_date: str) -> str:
    return f"{ARCHIVE_RANGE_URL}?date0={start_date}&date1={end_date}"


def cache_name(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    name = Path(parsed.path).name or "index.html"
    query = re.sub(r"[^A-Za-z0-9_.-]+", "_", parsed.query)
    if query:
        name = f"{Path(name).stem}_{query}{Path(name).suffix}"
    return name


def discover_issues(index_html: str, products: set[str]) -> list[ArchiveIssue]:
    issues: dict[tuple[str, str], ArchiveIssue] = {}
    for key in products:
        pattern = PAGE_PATTERNS[key]
        for match in pattern.finditer(index_html):
            issue = match.group("issue")
            path = match.group(0)
            issues[(key, issue)] = ArchiveIssue(
                key=key,
                issue=issue,
                page_url=urllib.parse.urljoin(bot.SPC_BASE, path),
            )
    return sorted(issues.values(), key=lambda item: (item.issue, item.key))


def archive_urljoin(page_url: str, href: str) -> str:
    normalized = html.unescape(href).strip()
    if normalized.startswith("archive/"):
        return urllib.parse.urljoin(f"{bot.SPC_BASE}/products/outlook/", normalized)
    return urllib.parse.urljoin(page_url, normalized)


def pts_url_for_issue(issue: ArchiveIssue, page_html: str) -> str:
    awips = AWIPS_FOR_KEY[issue.key]
    hrefs = re.findall(r"""href=["']([^"']+)["']""", page_html, flags=re.I)
    for href in hrefs:
        if awips in href and href.lower().endswith(".txt"):
            return archive_urljoin(issue.page_url, href)
    year = issue.issue[:4]
    compact_issue = issue.issue.replace("_", "")
    if issue.key == "day4-8":
        return f"{DAY48_ARCHIVE_BASE}/{year}/KWNS{awips}_{compact_issue}.txt"
    return f"{SPC_ARCHIVE_BASE}/{year}/KWNS{awips}_{compact_issue}.txt"


def geometry_summary(product: bot.PtsProduct) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for map_label, labels in product.maps.items():
        summary[map_label] = {
            label: len(polygons)
            for label, polygons in sorted(labels.items())
        }
    return summary


def validate_issue(issue: ArchiveIssue, output_dir: Path, *, refresh: bool, render_images: bool) -> dict[str, Any]:
    spec = SPECS[issue.key]
    page_html = fetch_text(
        issue.page_url,
        output_dir / "cache" / "pages" / issue.key / cache_name(issue.page_url),
        refresh=refresh,
    )
    pts_url = pts_url_for_issue(issue, page_html)
    pts_text = fetch_text(
        pts_url,
        output_dir / "cache" / "pts" / issue.key / Path(urllib.parse.urlsplit(pts_url).path).name,
        refresh=refresh,
    )
    product = bot.parse_pts_text(pts_text, spec)
    warnings: list[str] = []
    geometry = geometry_summary(product)
    if not geometry:
        warnings.append("no PTS geometry parsed")
    images: dict[str, str] = {}
    for map_label in spec.expected_order:
        labels = product.maps.get(map_label, {})
        if not labels:
            warnings.append(f"{map_label}: no polygons")
        try:
            data = bot.render_pts_map_png(product, map_label)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{map_label}: render failed: {exc}")
            continue
        if render_images:
            path = output_dir / "renders" / issue.key / f"{issue.issue}_{map_label}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            images[map_label] = str(path.relative_to(output_dir)).replace("\\", "/")
    return {
        "key": issue.key,
        "issue": issue.issue,
        "page_url": issue.page_url,
        "pts_url": pts_url,
        "product_id": product.product_id,
        "updated": product.updated,
        "risk_labels": list(bot.risk_labels_from_product(product)),
        "geometry": geometry,
        "images": images,
        "warnings": warnings,
        "status": "failed" if any("render failed" in item for item in warnings) or not geometry else "ok",
    }


def write_html(output_dir: Path, entries: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    rows = []
    for entry in entries:
        warning_text = "<br>".join(esc(item) for item in entry["warnings"]) or "none"
        links = " ".join(
            f"<a href='{esc(path)}'>{esc(label)}</a>"
            for label, path in entry["images"].items()
        ) or "not written"
        rows.append(
            "<tr>"
            f"<td>{esc(entry['status'])}</td>"
            f"<td>{esc(entry['key'])}</td>"
            f"<td>{esc(entry['issue'])}</td>"
            f"<td>{esc(', '.join(entry['risk_labels']))}</td>"
            f"<td>{warning_text}</td>"
            f"<td>{links}</td>"
            f"<td><a href='{esc(entry['pts_url'])}'>PTS</a></td>"
            "</tr>"
        )
    html_text = "\n".join(
        [
            "<!doctype html><meta charset='utf-8'>",
            "<title>PTS Archive Validation</title>",
            "<style>body{font:14px system-ui,sans-serif;margin:24px}table{border-collapse:collapse;width:100%}"
            "td,th{border:1px solid #ccc;padding:6px;vertical-align:top}th{background:#eee}.failed{color:#900}</style>",
            "<h1>PTS Archive Validation</h1>",
            f"<p>{esc(summary)}</p>",
            "<table><thead><tr><th>Status</th><th>Product</th><th>Issue</th><th>Risk labels</th><th>Warnings</th><th>Images</th><th>Source</th></tr></thead><tbody>",
            *rows,
            "</tbody></table>",
        ]
    )
    (output_dir / "index.html").write_text(html_text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    products = {item.strip() for item in args.products.split(",") if item.strip()}
    invalid = products - set(SPECS)
    if invalid:
        raise SystemExit(f"unknown product key(s): {', '.join(sorted(invalid))}")

    index_url = archive_range_url(args.start_date, args.end_date)
    print(f"Fetching archive range {args.start_date} through {args.end_date}")
    index_html = fetch_text(
        index_url,
        output_dir / "cache" / "archive_range" / cache_name(index_url),
        refresh=args.refresh,
    )
    issues = discover_issues(index_html, products)
    if args.limit:
        issues = issues[-args.limit:]
    print(f"Validating {len(issues)} archived PTS products")

    entries: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {
            pool.submit(validate_issue, issue, output_dir, refresh=args.refresh, render_images=args.render_images): issue
            for issue in issues
        }
        for index, future in enumerate(as_completed(futures), 1):
            issue = futures[future]
            try:
                entry = future.result()
            except Exception as exc:  # noqa: BLE001
                entry = {
                    "key": issue.key,
                    "issue": issue.issue,
                    "page_url": issue.page_url,
                    "pts_url": "",
                    "product_id": "",
                    "updated": "",
                    "risk_labels": [],
                    "geometry": {},
                    "images": {},
                    "warnings": [f"validation failed: {exc}"],
                    "status": "failed",
                }
            entries.append(entry)
            if index % 25 == 0 or index == len(futures):
                failed = sum(1 for item in entries if item["status"] != "ok")
                print(f"validated {index}/{len(futures)}; failures/warnings: {failed}")
    entries.sort(key=lambda item: (item["issue"], item["key"]))
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "products": sorted(products),
        "issue_count": len(entries),
        "failed_count": sum(1 for item in entries if item["status"] != "ok"),
        "warning_count": sum(1 for item in entries if item["warnings"]),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "entries.json").write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    write_html(output_dir, entries, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Wrote {output_dir / 'index.html'}")
    return 1 if summary["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
