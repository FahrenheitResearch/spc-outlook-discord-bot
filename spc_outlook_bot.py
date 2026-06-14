#!/usr/bin/env python3
"""
SPC outlook map poster.

Posts four image-only bundles:
  - Day 1: categorical, tornado, wind, hail
  - Day 2: categorical, tornado, wind, hail
  - Day 3: categorical, probabilistic
  - Day 4-8: combined Day 4-8 plus Day 4, 5, 6, 7, 8

Fast path: optional nwws-rs Server-Sent Events trigger.
Fallback: direct SPC page polling.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import email.utils
import hashlib
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator


SPC_BASE = "https://www.spc.noaa.gov"
USER_AGENT = "spc-outlook-bot/1.0 (+https://www.spc.noaa.gov/)"
WATER_COLOR = "#6f9fca"
LAND_COLOR = "#f8f3df"
DEFAULT_IMAGE_SAFE_SCALE = 0.95
MAP_EXTENT = (-125.0, -66.0, 24.0, 50.5)
DEFAULT_SSE_URLS = (
    "http://127.0.0.1:8080/v1/stream?office=KWNS&pil=PTS,"
    "http://127.0.0.1:8080/v1/stream?office=KWNS&pil=SWO"
)
RAW_PTS_URLS = {
    "day1": "https://tgftp.nws.noaa.gov/data/raw/wu/wuus01.kwns.pts.dy1.txt",
    "day2": "https://tgftp.nws.noaa.gov/data/raw/wu/wuus02.kwns.pts.dy2.txt",
    "day3": "https://tgftp.nws.noaa.gov/data/raw/wu/wuus03.kwns.pts.dy3.txt",
    "day4-8": "https://tgftp.nws.noaa.gov/data/raw/wu/wuus48.kwns.pts.d48.txt",
}


@dataclasses.dataclass(frozen=True)
class BundleSpec:
    key: str
    name: str
    page_url: str
    awips_ids: tuple[str, ...]
    expected_order: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class MapImage:
    label: str
    url: str
    filename: str
    content_type: str
    sha256: str
    data: bytes


@dataclasses.dataclass(frozen=True)
class BundleSnapshot:
    spec: BundleSpec
    title: str
    updated: str
    product_id: str
    page_url: str
    images: tuple[MapImage, ...]
    risk_labels: tuple[str, ...] = ()

    @property
    def post_key(self) -> str:
        image_hashes = ",".join(f"{image.label}:{image.sha256}" for image in self.images)
        raw = f"{self.spec.key}|{self.product_id}|{self.updated}|{image_hashes}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class PtsProduct:
    spec: BundleSpec
    product_id: str
    title: str
    issued: str
    valid: str
    updated: str
    source: str
    maps: dict[str, dict[str, tuple[tuple[tuple[float, float], ...], ...]]]


BUNDLES: tuple[BundleSpec, ...] = (
    BundleSpec(
        key="day1",
        name="Day 1 Convective Outlook",
        page_url=f"{SPC_BASE}/products/outlook/day1otlk.html",
        awips_ids=("PTSDY1", "SWODY1"),
        expected_order=("categorical", "tornado", "wind", "hail"),
    ),
    BundleSpec(
        key="day2",
        name="Day 2 Convective Outlook",
        page_url=f"{SPC_BASE}/products/outlook/day2otlk.html",
        awips_ids=("PTSDY2", "SWODY2"),
        expected_order=("categorical", "tornado", "wind", "hail"),
    ),
    BundleSpec(
        key="day3",
        name="Day 3 Convective Outlook",
        page_url=f"{SPC_BASE}/products/outlook/day3otlk.html",
        awips_ids=("PTSDY3", "SWODY3"),
        expected_order=("categorical", "probabilistic"),
    ),
    BundleSpec(
        key="day4-8",
        name="Day 4-8 Convective Outlook",
        page_url=f"{SPC_BASE}/products/exper/day4-8/",
        awips_ids=("PTSD48", "SWOD48"),
        expected_order=("day4-8", "day4", "day5", "day6", "day7", "day8"),
    ),
)


class BotError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def log(message: str) -> None:
    print(f"{utc_now_iso()} {message}", flush=True)


def cache_busted(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("_spc_bot_ts", str(int(time.time() * 1000))))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    cache_bust: bool = True,
) -> urllib.response.addinfourl:
    target = cache_busted(url) if method == "GET" and cache_bust else url
    req_headers = {
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(target, data=data, headers=req_headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_text(url: str, timeout: int = 20) -> str:
    with request(url, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "windows-1252"
    return raw.decode(charset, errors="replace")


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("&nbsp;", " ")).strip()


def html_unescape_light(value: str) -> str:
    return (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def extract_title(html: str, fallback: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    return normalize_spaces(html_unescape_light(match.group(1)))


def extract_updated(html: str) -> str:
    match = re.search(r"Updated:\s*(?:&nbsp;|\s)*(.*?UTC\s+\d{4})", html, re.IGNORECASE | re.DOTALL)
    if match:
        return normalize_spaces(html_unescape_light(re.sub(r"<.*?>", " ", match.group(1))))
    reviewed = re.search(r'DC\.date\.reviewed"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if reviewed:
        return reviewed.group(1).strip()
    return ""


def extract_product_id(html: str, spec: BundleSpec, title: str, updated: str) -> str:
    ids = []
    for match in re.finditer(r"KWNS(PTS(?:DY[123]|D48)|SWO(?:DY[123]|D48))[_-]?(\d{8,12})?", html):
        ids.append("".join(part for part in match.groups() if part))
    if ids:
        return sorted(set(ids))[-1]
    for awips in spec.awips_ids:
        if awips in html:
            return f"KWNS{awips}:{updated or title}"
    return f"{title}:{updated}"


def label_from_tab(tab: str) -> str:
    lowered = tab.lower()
    if lowered == "48":
        return "day4-8"
    if lowered in {"4", "5", "6", "7", "8"}:
        return f"day{lowered}"
    if "torn" in lowered:
        return "tornado"
    if "wind" in lowered:
        return "wind"
    if "hail" in lowered:
        return "hail"
    if "prob" in lowered:
        return "probabilistic"
    return "categorical"


def parse_image_urls(html: str, spec: BundleSpec) -> list[tuple[str, str]]:
    prefix_suffix = re.search(
        r'document\.getElementById\("main"\)\.src\s*=\s*"([^"]*)"\s*\+\s*nam\s*\+\s*"([^"]*)"',
        html,
    )
    if not prefix_suffix:
        raise BotError(f"could not find SPC map image pattern on {spec.page_url}")
    prefix, suffix = prefix_suffix.groups()
    tabs = list(dict.fromkeys(re.findall(r"show_tab\('([^']+)'\)", html)))
    if not tabs:
        raise BotError(f"could not find SPC map tabs on {spec.page_url}")

    base = spec.page_url
    pairs = []
    for tab in tabs:
        label = label_from_tab(tab)
        if label not in spec.expected_order:
            continue
        rel = f"{prefix}{tab}{suffix}"
        pairs.append((label, urllib.parse.urljoin(base, rel)))

    order = {label: index for index, label in enumerate(spec.expected_order)}
    deduped = list(dict.fromkeys(pairs))
    deduped.sort(key=lambda item: order.get(item[0], 999))
    return deduped


def extension_for(content_type: str, url: str) -> str:
    path_ext = Path(urllib.parse.urlparse(url).path).suffix
    if path_ext.lower() in {".png", ".gif", ".jpg", ".jpeg"}:
        return path_ext.lower()
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip().lower())
    if guessed in {".png", ".gif", ".jpg", ".jpeg"}:
        return guessed
    return ".img"


def download_image(bundle_key: str, label: str, url: str, timeout: int = 20) -> MapImage:
    with request(url, timeout=timeout) as response:
        data = response.read()
        content_type = response.headers.get("Content-Type", "application/octet-stream")
    if not content_type.lower().startswith("image/"):
        raise BotError(f"{url} returned {content_type}, not an image")
    if len(data) < 1024:
        raise BotError(f"{url} returned an unexpectedly small image ({len(data)} bytes)")
    digest = hashlib.sha256(data).hexdigest()
    filename = f"{bundle_key}_{label}{extension_for(content_type, url)}"
    return MapImage(
        label=label,
        url=url,
        filename=filename,
        content_type=content_type,
        sha256=digest,
        data=data,
    )


def fetch_bundle(spec: BundleSpec) -> BundleSnapshot:
    html = fetch_text(spec.page_url)
    title = extract_title(html, spec.name)
    updated = extract_updated(html)
    product_id = extract_product_id(html, spec, title, updated)
    url_pairs = parse_image_urls(html, spec)
    if not url_pairs:
        raise BotError(f"{spec.name} did not expose any expected map images")

    images: list[MapImage] = []
    with ThreadPoolExecutor(max_workers=min(8, len(url_pairs))) as pool:
        futures = {
            pool.submit(download_image, spec.key, label, url): (label, url) for label, url in url_pairs
        }
        for future in as_completed(futures):
            images.append(future.result())

    order = {label: index for index, label in enumerate(spec.expected_order)}
    images.sort(key=lambda image: order.get(image.label, 999))
    return BundleSnapshot(
        spec=spec,
        title=title,
        updated=updated,
        product_id=product_id,
        page_url=spec.page_url,
        images=tuple(images),
    )


RISK_ORDER = ("TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH")
RISK_RANK = {label: index for index, label in enumerate(RISK_ORDER)}
PROB_ORDER = ("0.02", "0.05", "0.10", "0.15", "0.30", "0.45", "0.60")
SEVERE_PROB_ORDER = ("0.05", "0.15", "0.30", "0.45", "0.60", "0.75", "0.90")
DAY48_ORDER = ("day4", "day5", "day6", "day7", "day8")
DAY48_PROB_ORDER = ("0.15", "0.30")

CATEGORICAL_STYLE = {
    "TSTM": ("Thunderstorms", "#c8efc2", "#45b84d"),
    "MRGL": ("1 Marginal", "#66a866", "#006100"),
    "SLGT": ("2 Slight", "#ffe066", "#d0a000"),
    "ENH": ("3 Enhanced", "#ff9f5f", "#ff6f00"),
    "MDT": ("4 Moderate", "#e75d5d", "#cc0000"),
    "HIGH": ("5 High", "#f06cff", "#d000d0"),
}

PROB_STYLE = {
    "0.02": ("2%", "#aee7b2", "#2da346"),
    "0.05": ("5%", "#8a4f2a", "#5b2d12"),
    "0.10": ("10%", "#ffd84d", "#c29b00"),
    "0.15": ("15%", "#ef5350", "#c62828"),
    "0.30": ("30%", "#d55cff", "#9c27b0"),
    "0.45": ("45%", "#ff4fb3", "#c2185b"),
    "0.60": ("60%", "#5ff0ff", "#00a5b8"),
}

SEVERE_PROB_STYLE = {
    "0.05": ("5%", "#8a4f2a", "#5b2d12"),
    "0.15": ("15%", "#ffe066", "#d0a000"),
    "0.30": ("30%", "#ef5350", "#c62828"),
    "0.45": ("45%", "#f04cff", "#c218c9"),
    "0.60": ("60%", "#c05cff", "#7b1fa2"),
    "0.75": ("75%", "#7282ff", "#2636b8"),
    "0.90": ("90%", "#5ff0ff", "#00a5b8"),
}

DAY48_STYLE = {
    "day4": ("D4", "#ff0000", "#a40000"),
    "day5": ("D5", "#902bee", "#5b1599"),
    "day6": ("D6", "#008a00", "#005c00"),
    "day7": ("D7", "#104e8a", "#0a3156"),
    "day8": ("D8", "#8a4e26", "#5a3017"),
}

DAY48_PROB_STYLE = {
    "0.15": ("15%", "#fff36a", "#ff9b00"),
    "0.30": ("30%", "#d7a74f", "#7a4a1f"),
}

CIG_ORDER = ("CIG1", "CIG2", "CIG3")
CIG_STYLE = {
    "CIG1": ("Intensity 1", "cig1", 1.25),
    "CIG2": ("Intensity 2", "cig2", 1.25),
    "CIG3": ("Intensity 3", "cig3", 1.35),
}

MAJOR_CITY_LABELS = (
    ("Seattle", -122.33, 47.61, 0.35, 0.20),
    ("Portland", -122.68, 45.52, 0.35, -0.22),
    ("San Francisco", -122.42, 37.77, 0.35, -0.28),
    ("Reno", -119.81, 39.53, 0.35, 0.18),
    ("Los Angeles", -118.24, 34.05, 0.35, -0.28),
    ("Boise", -116.20, 43.62, 0.35, 0.18),
    ("Las Vegas", -115.14, 36.17, 0.35, 0.18),
    ("Salt Lake City", -111.89, 40.76, 0.35, -0.24),
    ("Phoenix", -112.07, 33.45, 0.35, -0.24),
    ("Billings", -108.50, 45.78, 0.35, 0.18),
    ("Cheyenne", -104.82, 41.14, 0.35, 0.22),
    ("Denver", -104.99, 39.74, 0.35, -0.28),
    ("Albuquerque", -106.65, 35.08, 0.35, 0.18),
    ("Amarillo", -101.83, 35.22, 0.35, 0.18),
    ("Dallas", -96.80, 32.78, 0.35, -0.25),
    ("Houston", -95.37, 29.76, 0.35, -0.28),
    ("Oklahoma City", -97.52, 35.47, 0.35, 0.18),
    ("Omaha", -95.94, 41.26, -1.25, 0.32),
    ("Kansas City", -94.58, 39.10, 0.35, 0.18),
    ("Des Moines", -93.62, 41.59, 0.35, -0.24),
    ("Bismarck", -100.78, 46.81, 0.35, 0.18),
    ("Minneapolis", -93.27, 44.98, 0.35, 0.20),
    ("Chicago", -87.63, 41.88, 0.35, 0.18),
    ("Detroit", -83.05, 42.33, 0.35, 0.18),
    ("St Louis", -90.20, 38.63, 0.35, -0.28),
    ("Nashville", -86.78, 36.16, 0.35, 0.18),
    ("Memphis", -90.05, 35.15, 0.35, -0.26),
    ("New Orleans", -90.07, 29.95, 0.35, -0.20),
    ("Atlanta", -84.39, 33.75, 0.35, -0.25),
    ("Charlotte", -80.84, 35.23, 0.35, 0.18),
    ("Pittsburgh", -79.99, 40.44, 0.35, 0.18),
    ("Washington", -77.04, 38.91, 0.35, -0.22),
    ("Philadelphia", -75.17, 39.95, 0.35, -0.24),
    ("New York", -74.01, 40.71, 0.35, 0.18),
    ("Boston", -71.06, 42.36, 0.35, 0.18),
    ("Bangor", -68.78, 44.80, 0.35, 0.18),
    ("Cape Canaveral", -80.61, 28.39, -3.45, 0.14),
)


def pts_awips_for_spec(spec: BundleSpec) -> str:
    for awips in spec.awips_ids:
        if awips.startswith("PTS"):
            return awips
    return spec.awips_ids[0]


def find_pts_url(html: str, spec: BundleSpec) -> str | None:
    pts_awips = pts_awips_for_spec(spec)
    pattern = rf'href="([^"]*KWNS{re.escape(pts_awips)}[^"]*\.txt)"'
    match = re.search(pattern, html, re.IGNORECASE)
    if match:
        return urllib.parse.urljoin(spec.page_url, html_unescape_light(match.group(1)))
    return None


def fetch_pts_text_for_spec(spec: BundleSpec) -> str:
    html = fetch_text(spec.page_url)
    pts_url = find_pts_url(html, spec)
    if not pts_url:
        raise BotError(f"{spec.name}: could not find PTS text product link")
    return fetch_text(pts_url)


def fetch_raw_pts_text_for_spec(spec: BundleSpec) -> str:
    raw_url = RAW_PTS_URLS.get(spec.key)
    if not raw_url:
        raise BotError(f"{spec.name}: no raw PTS feed URL is configured")
    return fetch_text(raw_url)


def find_geojson_url(html: str, spec: BundleSpec) -> str | None:
    match = re.search(r'href="([^"]*geojson\.zip)"', html, re.IGNORECASE)
    if not match:
        return None
    return urllib.parse.urljoin(spec.page_url, html_unescape_light(match.group(1)))


def fetch_geojson_product_for_spec(spec: BundleSpec) -> PtsProduct:
    html = fetch_text(spec.page_url)
    geojson_url = find_geojson_url(html, spec)
    if not geojson_url:
        raise BotError(f"{spec.name}: could not find SPC GeoJSON ZIP link")
    with request(geojson_url, timeout=30, cache_bust=True) as response:
        data = response.read()
    return parse_geojson_zip(data, spec, geojson_url)


def geojson_slug_for_map(map_label: str) -> str | None:
    return {
        "categorical": "cat",
        "tornado": "torn",
        "wind": "wind",
        "hail": "hail",
        "probabilistic": "prob",
    }.get(map_label)


def spec_supports_geojson(spec: BundleSpec) -> bool:
    return any(geojson_slug_for_map(map_label) for map_label in spec.expected_order)


def choose_geojson_member(names: list[str], spec: BundleSpec, map_label: str) -> str | None:
    slug = geojson_slug_for_map(map_label)
    if not slug:
        return None
    prefix = spec.key.replace("-", "")
    candidates = [
        name
        for name in names
        if name.lower().endswith(f"_{slug}.lyr.geojson")
        and f"{prefix}otlk" in name.lower()
    ]
    dated = [name for name in candidates if re.search(r"_\d{8}_\d{4}_", name)]
    if dated:
        return sorted(dated)[-1]
    if candidates:
        return sorted(candidates)[-1]
    return None


def iso_compact(value: str) -> str:
    if not value:
        return ""
    return value.replace("-", "").replace(":", "").replace("+00:00", "Z")


def format_geojson_time(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H%MZ")


def geojson_time_range(properties: dict[str, Any]) -> tuple[str, str, str]:
    issue = str(properties.get("ISSUE_ISO") or properties.get("ISSUE") or "")
    valid = str(properties.get("VALID_ISO") or properties.get("VALID") or "")
    expire = str(properties.get("EXPIRE_ISO") or properties.get("EXPIRE") or "")
    compact_valid = iso_compact(valid)
    compact_expire = iso_compact(expire)
    formatted_valid = format_geojson_time(valid)
    formatted_expire = format_geojson_time(expire)
    if formatted_valid and formatted_expire:
        valid_range = f"{formatted_valid} - {formatted_expire}"
    else:
        valid_range = ""
    return format_geojson_time(issue) or issue, valid_range, compact_valid or str(properties.get("VALID") or "")


def product_issue_datetime(product: PtsProduct) -> datetime | None:
    value = product.updated or product.issued
    if not value:
        return None
    for pattern in ("%Y-%m-%d %H%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    match = re.search(
        r"\b(\d{1,2})(\d{2})\s+(AM|PM)\s+(CST|CDT)\s+[A-Z]{3}\s+([A-Z]{3})\s+(\d{1,2})\s+(\d{4})\b",
        value.upper(),
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3)
    zone = match.group(4)
    month = match.group(5).title()
    day = int(match.group(6))
    year = int(match.group(7))
    if meridiem == "AM" and hour == 12:
        hour = 0
    elif meridiem == "PM" and hour != 12:
        hour += 12
    offset_hours = -5 if zone == "CDT" else -6
    local_tz = timezone(timedelta(hours=offset_hours))
    try:
        parsed = datetime.strptime(f"{year} {month} {day} {hour:02d} {minute:02d}", "%Y %b %d %H %M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=local_tz).astimezone(timezone.utc)


def pts_product_from_text_or_feed(spec: BundleSpec, pts_text: str | None = None) -> PtsProduct:
    if pts_text is None:
        try:
            pts_text = fetch_raw_pts_text_for_spec(spec)
        except Exception:
            pts_text = fetch_pts_text_for_spec(spec)
    return parse_pts_text(pts_text, spec)


def choose_custom_product(spec: BundleSpec, pts_text: str | None, custom_source: str) -> PtsProduct:
    if custom_source == "pts-only":
        return pts_product_from_text_or_feed(spec, pts_text)
    if custom_source == "geojson-only":
        return fetch_geojson_product_for_spec(spec)

    geojson_product: PtsProduct | None = None
    pts_product: PtsProduct | None = None
    geojson_error: Exception | None = None
    pts_error: Exception | None = None

    if spec_supports_geojson(spec):
        try:
            geojson_product = fetch_geojson_product_for_spec(spec)
        except Exception as exc:  # noqa: BLE001
            geojson_error = exc
    try:
        pts_product = pts_product_from_text_or_feed(spec, pts_text)
    except Exception as exc:  # noqa: BLE001
        pts_error = exc

    if geojson_product and pts_product:
        geojson_time = product_issue_datetime(geojson_product)
        pts_time = product_issue_datetime(pts_product)
        if geojson_time and pts_time and pts_time > geojson_time:
            log(
                f"{spec.name}: raw PTS is newer than SPC GeoJSON "
                f"({pts_time.strftime('%Y-%m-%d %H%MZ')} > {geojson_time.strftime('%Y-%m-%d %H%MZ')})"
            )
            return pts_product
        return geojson_product
    if pts_product:
        return pts_product
    if geojson_product:
        return geojson_product
    if geojson_error:
        raise BotError(f"{spec.name}: GeoJSON and raw PTS unavailable; GeoJSON error: {geojson_error}") from geojson_error
    if pts_error:
        raise BotError(f"{spec.name}: raw PTS unavailable: {pts_error}") from pts_error
    raise BotError(f"{spec.name}: no custom geometry source is available")


def parse_geojson_zip(data: bytes, spec: BundleSpec, geojson_url: str) -> PtsProduct:
    try:
        from shapely.geometry import shape
    except Exception as exc:  # noqa: BLE001
        raise BotError("SPC GeoJSON rendering requires shapely; install requirements.txt or use Docker") from exc

    maps: dict[str, dict[str, list[Any]]] = {}
    first_properties: dict[str, Any] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        for map_label in spec.expected_order:
            member = choose_geojson_member(names, spec, map_label)
            if not member:
                continue
            collection = json.loads(archive.read(member).decode("utf-8"))
            label_geometries = maps.setdefault(map_label, {})
            for feature in collection.get("features", []):
                properties = feature.get("properties") or {}
                if not first_properties:
                    first_properties = dict(properties)
                label = str(properties.get("LABEL") or "").strip()
                geometry_dict = feature.get("geometry")
                if not label or not geometry_dict:
                    continue
                geometry = shape(geometry_dict)
                if geometry.is_empty:
                    continue
                label_geometries.setdefault(label, []).append(geometry)

    if not maps:
        raise BotError(f"{spec.name}: SPC GeoJSON ZIP did not contain expected outlook layers")

    issued, valid, valid_start = geojson_time_range(first_properties)
    stem = Path(urllib.parse.urlsplit(geojson_url).path).name.removesuffix("-geojson.zip")
    product_id = f"geojson:{stem}:{valid_start or hashlib.sha1(data).hexdigest()[:12]}"
    return PtsProduct(
        spec=spec,
        product_id=product_id,
        title=spec.name,
        issued=issued or str(first_properties.get("ISSUE") or ""),
        valid=valid,
        updated=issued or utc_now_iso(),
        source="geojson",
        maps={key: {label: tuple(geometries) for label, geometries in labels.items()} for key, labels in maps.items()},
    )


def is_pts_label(token: str) -> bool:
    return (
        token in RISK_ORDER
        or token in PROB_ORDER
        or token in DAY48_PROB_ORDER
        or token.startswith("CIG")
        or re.fullmatch(r"0\.\d{2}", token) is not None
    )


def parse_pts_coord(token: str) -> tuple[float, float] | None:
    if not re.fullmatch(r"\d{8}", token) or token == "99999999":
        return None
    lat = int(token[:4]) / 100.0
    lon_degrees = int(token[4:]) / 100.0
    if lon_degrees < 30.0:
        lon_degrees += 100.0
    lon = -lon_degrees
    return lon, lat


def collect_pts_polygons(
    lines: list[str],
) -> dict[str, tuple[tuple[tuple[float, float], ...], ...]]:
    collected: dict[str, list[tuple[tuple[float, float], ...]]] = {}
    current_label: str | None = None
    current_poly: list[tuple[float, float]] = []

    def commit_poly() -> None:
        nonlocal current_poly
        if current_label and len(current_poly) >= 3:
            collected.setdefault(current_label, []).append(tuple(current_poly))
        current_poly = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("..."):
            continue
        parts = line.split()
        if not parts:
            continue
        if is_pts_label(parts[0]):
            commit_poly()
            current_label = parts[0]
            tokens = parts[1:]
        elif current_label:
            tokens = parts
        else:
            continue

        for token in tokens:
            if token == "99999999":
                commit_poly()
                continue
            coord = parse_pts_coord(token)
            if coord is not None:
                current_poly.append(coord)
    commit_poly()
    return {label: tuple(polygons) for label, polygons in collected.items()}


def is_open_coordinate_sequence(value: Any) -> bool:
    if hasattr(value, "geom_type"):
        return False
    if not isinstance(value, tuple | list) or len(value) < 3:
        return False
    first = value[0]
    last = value[-1]
    return isinstance(first, tuple | list) and isinstance(last, tuple | list) and first != last


def boundary_parameter(point: tuple[float, float], extent: tuple[float, float, float, float]) -> float:
    min_lon, max_lon, min_lat, max_lat = extent
    lon, lat = point
    width = max_lon - min_lon
    height = max_lat - min_lat
    candidates = [
        (abs(lat - min_lat), lon - min_lon),
        (abs(lon - max_lon), width + lat - min_lat),
        (abs(lat - max_lat), width + height + max_lon - lon),
        (abs(lon - min_lon), width + height + width + max_lat - lat),
    ]
    return min(candidates, key=lambda item: item[0])[1]


def boundary_point(t: float, extent: tuple[float, float, float, float]) -> tuple[float, float]:
    min_lon, max_lon, min_lat, max_lat = extent
    width = max_lon - min_lon
    height = max_lat - min_lat
    perimeter = 2 * (width + height)
    t %= perimeter
    if t <= width:
        return min_lon + t, min_lat
    t -= width
    if t <= height:
        return max_lon, min_lat + t
    t -= height
    if t <= width:
        return max_lon - t, max_lat
    t -= width
    return min_lon, max_lat - t


def boundary_path(
    start: tuple[float, float],
    end: tuple[float, float],
    extent: tuple[float, float, float, float],
    *,
    clockwise: bool,
) -> list[tuple[float, float]]:
    min_lon, max_lon, min_lat, max_lat = extent
    width = max_lon - min_lon
    height = max_lat - min_lat
    perimeter = 2 * (width + height)
    start_t = boundary_parameter(start, extent)
    end_t = boundary_parameter(end, extent)
    if clockwise:
        if end_t > start_t:
            start_t += perimeter
        steps = [start_t]
        for corner_t in (width, width + height, width + height + width, perimeter):
            for wrapped in (corner_t, corner_t + perimeter):
                if end_t < wrapped < start_t:
                    steps.append(wrapped)
        steps.append(end_t)
        return [boundary_point(step, extent) for step in sorted(set(steps), reverse=True)]
    if end_t < start_t:
        end_t += perimeter
    steps = [start_t]
    for corner_t in (width, width + height, width + height + width, perimeter):
        for wrapped in (corner_t, corner_t + perimeter):
            if start_t < wrapped < end_t:
                steps.append(wrapped)
    steps.append(end_t)
    return [boundary_point(step, extent) for step in sorted(set(steps))]


def right_side_sample(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    longest: tuple[tuple[float, float], tuple[float, float]] | None = None
    longest_distance = 0.0
    for start, end in zip(points, points[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = dx * dx + dy * dy
        if distance > longest_distance:
            longest = (start, end)
            longest_distance = distance
    if not longest or longest_distance <= 0:
        return None
    start, end = longest
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = (dx * dx + dy * dy) ** 0.5
    return ((start[0] + end[0]) / 2 + (dy / length) * 0.35, (start[1] + end[1]) / 2 - (dx / length) * 0.35)


def close_open_pts_contour(points: list[tuple[float, float]]) -> Any | None:
    from shapely.geometry import Point, Polygon

    if len(points) < 2:
        return None
    sample = right_side_sample(points)
    candidates = []
    for clockwise in (False, True):
        closure = boundary_path(points[-1], points[0], MAP_EXTENT, clockwise=clockwise)
        polygon = Polygon([*points, *closure])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty:
            candidates.append(polygon)
    if not candidates:
        return None
    if sample:
        sample_point = Point(sample)
        for polygon in candidates:
            if polygon.contains(sample_point):
                return polygon
    return max(candidates, key=lambda polygon: polygon.area)


@lru_cache(maxsize=1)
def conus_land_mask() -> Any:
    import cartopy.io.shapereader as shapereader
    from shapely.geometry import box
    from shapely.ops import unary_union

    path = shapereader.natural_earth("50m", "cultural", "admin_0_countries")
    reader = shapereader.Reader(path)
    united_states = [
        record.geometry
        for record in reader.records()
        if record.attributes.get("ADMIN") == "United States of America"
    ]
    if not united_states:
        raise BotError("could not load United States boundary for open PTS contour clipping")
    min_lon, max_lon, min_lat, max_lat = MAP_EXTENT
    return unary_union(united_states).intersection(box(min_lon, min_lat, max_lon, max_lat))


def clip_open_pts_fill_to_conus(geometry: Any) -> Any:
    try:
        clipped = geometry.intersection(conus_land_mask())
    except Exception as exc:  # noqa: BLE001
        log(f"open PTS CONUS clipping failed, using unmasked fill: {exc}")
        return geometry
    return clipped if not clipped.is_empty else geometry


def parse_pts_text(text: str, spec: BundleSpec) -> PtsProduct:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    nonempty = [line.strip() for line in lines if line.strip()]
    title = nonempty[0] if nonempty else spec.name
    issued = ""
    valid = ""
    for index, line in enumerate(nonempty):
        if line.startswith("NWS STORM PREDICTION CENTER") and index + 1 < len(nonempty):
            issued = nonempty[index + 1]
        if line.startswith("VALID TIME"):
            valid = line.replace("VALID TIME", "").strip()
            break
    pts_awips = pts_awips_for_spec(spec)
    valid_start = re.search(r"(\d{6}Z)", valid)
    product_id = f"{pts_awips}:{valid_start.group(1) if valid_start else hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"

    maps: dict[str, dict[str, tuple[tuple[tuple[float, float], ...], ...]]] = {}
    active_mode: str | None = None
    active_map: str | None = None
    map_lines: dict[str, list[str]] = {}

    def finish_map() -> None:
        nonlocal active_map
        if active_map:
            active_map = None

    for raw_line in lines:
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("PROBABILISTIC OUTLOOK POINTS"):
            active_mode = "prob"
            active_map = None
            continue
        if upper.startswith("CATEGORICAL OUTLOOK POINTS"):
            active_mode = "cat"
            active_map = None
            continue
        day_match = re.match(r"SEVERE WEATHER OUTLOOK POINTS DAY\s+(\d)", upper)
        if day_match:
            active_mode = "day48"
            active_map = f"day{day_match.group(1)}"
            map_lines.setdefault(active_map, [])
            continue
        if upper == "&&":
            finish_map()
            continue
        section_match = re.fullmatch(r"\.\.\.\s*(.*?)\s*\.\.\.", line)
        if section_match:
            section = section_match.group(1).strip().upper()
            if active_mode == "prob":
                if section == "TORNADO":
                    active_map = "tornado"
                elif section == "HAIL":
                    active_map = "hail"
                elif section == "WIND":
                    active_map = "wind"
                elif section == "ANY SEVERE":
                    active_map = "probabilistic"
                else:
                    active_map = None
                if active_map:
                    map_lines.setdefault(active_map, [])
            elif active_mode == "cat" and section == "CATEGORICAL":
                active_map = "categorical"
                map_lines.setdefault(active_map, [])
            continue
        if active_map and line:
            map_lines.setdefault(active_map, []).append(line)

    for map_key, block_lines in map_lines.items():
        maps[map_key] = collect_pts_polygons(block_lines)

    if spec.key == "day4-8":
        combined: dict[str, list[tuple[tuple[float, float], ...]]] = {}
        for day_key in DAY48_ORDER:
            for label, polygons in maps.get(day_key, {}).items():
                combined.setdefault(label, []).extend(polygons)
        maps["day4-8"] = {label: tuple(polygons) for label, polygons in combined.items()}

    return PtsProduct(
        spec=spec,
        product_id=product_id,
        title=title,
        issued=issued,
        valid=valid,
        updated=issued or utc_now_iso(),
        source="pts",
        maps=maps,
    )


def preview_order_for_map(map_label: str) -> tuple[str, ...]:
    if map_label == "categorical":
        return RISK_ORDER
    if map_label == "day4-8" or map_label in DAY48_ORDER:
        return DAY48_PROB_ORDER
    if map_label in {"wind", "hail", "probabilistic"}:
        return SEVERE_PROB_ORDER + CIG_ORDER
    return PROB_ORDER + CIG_ORDER


def preview_style_for_label(map_label: str, label: str) -> tuple[str, str, str]:
    if map_label == "categorical":
        return CATEGORICAL_STYLE.get(label, (label, "#dddddd", "#555555"))
    if map_label == "day4-8" or map_label in DAY48_ORDER:
        return DAY48_PROB_STYLE.get(label, (label.upper(), "#dddddd", "#555555"))
    if label.startswith("CIG"):
        return ("Significant", "none", "#111111")
    if map_label in {"wind", "hail", "probabilistic"}:
        return SEVERE_PROB_STYLE.get(label, (label, "#dddddd", "#555555"))
    return PROB_STYLE.get(label, (label, "#dddddd", "#555555"))


def preview_title(spec: BundleSpec, map_label: str) -> str:
    if spec.key == "day4-8":
        if map_label == "day4-8":
            return spec.name
        if map_label.startswith("day"):
            return f"{spec.name} - Day {map_label.removeprefix('day')}"
    if map_label == "categorical":
        detail = "Categorical"
    elif map_label == "probabilistic":
        detail = "Probabilistic"
    else:
        detail = f"{map_label.title()} Probability"
    return f"{spec.name} - {detail}"


def is_day48_probability_map(map_label: str) -> bool:
    return map_label == "day4-8" or map_label in DAY48_ORDER


def risk_labels_from_product(product: PtsProduct) -> tuple[str, ...]:
    labels: set[str] = set()
    categorical = product.maps.get("categorical", {})
    for label in RISK_ORDER:
        if label in categorical:
            labels.add(label)
    if product.spec.key == "day4-8":
        for map_label in ("day4-8", *DAY48_ORDER):
            for label in DAY48_PROB_ORDER:
                if label in product.maps.get(map_label, {}):
                    labels.add(label)
        if any(label in labels for label in DAY48_PROB_ORDER):
            labels.add("DAY48_OUTLOOK")
    return tuple(sorted(labels, key=lambda label: RISK_RANK.get(label, 100 + list(DAY48_PROB_ORDER).index(label) if label in DAY48_PROB_ORDER else 999)))


def cig_labels_for_map(product: PtsProduct, map_label: str) -> tuple[str, ...]:
    labels = product.maps.get(map_label, {})
    return tuple(label for label in CIG_ORDER if label in labels)


def allowed_cig_labels_for_map(map_label: str) -> tuple[str, ...]:
    if map_label == "hail":
        return ("CIG1", "CIG2")
    if map_label in {"tornado", "wind", "probabilistic"}:
        return CIG_ORDER
    return ()


def draw_major_city_labels(ax: Any, transform: Any) -> None:
    import matplotlib.patheffects as path_effects

    for name, lon, lat, dx, dy in MAJOR_CITY_LABELS:
        ax.scatter(
            [lon],
            [lat],
            transform=transform,
            s=13,
            color="#1c1c1c",
            edgecolors="#ffffff",
            linewidths=0.8,
            zorder=58,
        )
        ax.text(
            lon + dx,
            lat + dy,
            name,
            transform=transform,
            fontsize=10.0,
            fontweight="bold",
            fontfamily="DejaVu Sans",
            color="#151515",
            ha="left",
            va="center",
            zorder=59,
            path_effects=[path_effects.withStroke(linewidth=2.2, foreground="#ffffff")],
        )


def line_parts(geometry: Any) -> Iterator[Any]:
    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "LineString":
        yield geometry
    elif geom_type in {"MultiLineString", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from line_parts(part)


def cig_hatch_specs(label: str, solid_override: bool = False) -> tuple[tuple[float, tuple[int, tuple[float, ...]] | str], ...]:
    solid = "solid"
    dashed: tuple[int, tuple[float, ...]] | str = solid if solid_override else (0, (7.5, 5.0))
    if label == "CIG1":
        return ((0.50, dashed),)
    if label == "CIG2":
        return ((-0.50, solid),)
    if label == "CIG3":
        return ((0.50, solid), (-0.50, solid))
    return ((0.50, dashed),)


def cig_hatch_lines(geometry: Any, slope: float) -> list[Any]:
    from shapely.geometry import LineString

    min_x, min_y, max_x, max_y = geometry.bounds
    span = max(max_x - min_x, max_y - min_y, 1.0)
    pad = max(2.0, span * 0.35)
    spacing = 0.72
    x0 = min_x - pad
    x1 = max_x + pad
    b_min = (min_y - pad) - slope * x1
    b_max = (max_y + pad) - slope * x0
    lines: list[Any] = []
    b = b_min
    while b <= b_max:
        raw_line = LineString(((x0, slope * x0 + b), (x1, slope * x1 + b)))
        clipped = raw_line.intersection(geometry)
        for part in line_parts(clipped):
            if part.length >= 0.08:
                lines.append(part)
        b += spacing
    return lines


def draw_cig_overlay(ax: Any, geometry: Any, label: str, transform: Any) -> None:
    _legend, _pattern, outline_width = CIG_STYLE.get(label, (label, "cig1", 1.25))
    base_zorder = 32 + (CIG_ORDER.index(label) if label in CIG_ORDER else 0)
    ax.add_geometries(
        [geometry],
        crs=transform,
        facecolor="none",
        edgecolor="#111111",
        linewidth=outline_width,
        zorder=base_zorder,
    )
    for slope, line_style in cig_hatch_specs(label):
        lines = cig_hatch_lines(geometry, slope)
        if not lines:
            continue
        ax.add_geometries(
            lines,
            crs=transform,
            facecolor="none",
            edgecolor="#111111",
            linewidth=1.05,
            linestyle=line_style,
            zorder=base_zorder + 0.2,
        )


def draw_cig_legend_symbol(ax: Any, rect: Any, pattern: str) -> None:
    x, y = rect.get_xy()
    width = rect.get_width()
    height = rect.get_height()
    specs = {
        "cig1": ((0.50, (0, (5.5, 3.5))),),
        "cig2": ((-0.50, "solid"),),
        "cig3": ((0.50, "solid"), (-0.50, "solid")),
    }.get(pattern, ((0.50, (0, (5.5, 3.5))),))
    for slope, line_style in specs:
        step = width * 0.28
        start = x - width
        end = x + width * 2.0
        cursor = start
        while cursor <= end:
            x0 = cursor
            x1 = cursor + width * 1.35
            y0 = y + (0.02 * height if slope > 0 else height * 0.98)
            y1 = y0 + slope * (x1 - x0)
            line = ax.plot(
                [x0, x1],
                [y0, y1],
                transform=ax.transAxes,
                color="#111111",
                linewidth=1.05,
                linestyle=line_style,
                solid_capstyle="butt",
                dash_capstyle="butt",
                zorder=4,
            )[0]
            line.set_clip_path(rect)
            cursor += step


def geometry_parts(geometry: Any) -> Iterator[Any]:
    geom_type = getattr(geometry, "geom_type", "")
    if geom_type == "Polygon":
        yield geometry
    elif geom_type in {"MultiPolygon", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from geometry_parts(part)


def draw_day48_probability_labels(ax: Any, map_polygons: dict[str, tuple[Any, ...]], transform: Any) -> None:
    import matplotlib.patheffects as path_effects
    from shapely.geometry import Polygon as ShapelyPolygon

    for label in DAY48_PROB_ORDER:
        legend, _face, edge = DAY48_PROB_STYLE[label]
        for polygon_or_geometry in map_polygons.get(label, ()):
            if is_open_coordinate_sequence(polygon_or_geometry):
                continue
            if hasattr(polygon_or_geometry, "geom_type"):
                geometry = polygon_or_geometry
            else:
                geometry = ShapelyPolygon(polygon_or_geometry)
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            if geometry.is_empty:
                continue
            for part in geometry_parts(geometry):
                if part.is_empty or part.area < 0.20:
                    continue
                point = part.representative_point()
                ax.text(
                    point.x,
                    point.y,
                    legend,
                    transform=transform,
                    ha="center",
                    va="center",
                    fontsize=10.5,
                    fontweight="bold",
                    fontfamily="DejaVu Sans",
                    color=edge,
                    zorder=57,
                    path_effects=[path_effects.withStroke(linewidth=1.4, foreground="#fff9d0")],
                )


def draw_day48_day_labels(ax: Any, product: PtsProduct, transform: Any) -> None:
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    for day_key in DAY48_ORDER:
        geometries = []
        for label in DAY48_PROB_ORDER:
            for polygon_or_geometry in product.maps.get(day_key, {}).get(label, ()):
                if is_open_coordinate_sequence(polygon_or_geometry):
                    continue
                if hasattr(polygon_or_geometry, "geom_type"):
                    geometry = polygon_or_geometry
                else:
                    geometry = ShapelyPolygon(polygon_or_geometry)
                if not geometry.is_valid:
                    geometry = geometry.buffer(0)
                if not geometry.is_empty:
                    geometries.append(geometry)
        if not geometries:
            continue

        merged = unary_union(geometries) if len(geometries) > 1 else geometries[0]
        for part in geometry_parts(merged):
            if part.is_empty or part.area < 0.20:
                continue
            point = part.representative_point()
            ax.text(
                point.x,
                point.y,
                f"Day {day_key.removeprefix('day')}",
                transform=transform,
                ha="center",
                va="center",
                fontsize=11.5,
                fontweight="bold",
                fontfamily="DejaVu Sans",
                color="#111111",
                zorder=60,
                bbox={
                    "facecolor": "#fffdf0",
                    "edgecolor": "#111111",
                    "linewidth": 0.8,
                    "boxstyle": "square,pad=0.18",
                    "alpha": 0.88,
                },
            )


def preview_source_footer(product: PtsProduct) -> str:
    return "UNOFFICIAL RENDER - Data source: NOAA/NWS SPC - not an official SPC/NWS graphic"


def preview_source_badge(product: PtsProduct) -> str:
    return "UNOFFICIAL FAST RENDER"


def render_pts_map_png(product: PtsProduct, map_label: str) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle

        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        from shapely.geometry import Polygon as ShapelyPolygon
    except Exception as exc:  # noqa: BLE001
        raise BotError(
            "custom preview rendering requires matplotlib and cartopy; "
            "install requirements.txt or use Docker"
        ) from exc

    width, height, dpi = 1630, 1110, 100
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor="#ffffff")
    projection = ccrs.LambertConformal(
        central_longitude=-96.0,
        central_latitude=35.0,
        standard_parallels=(33.0, 45.0),
    )
    ax = fig.add_axes([0.0, 0.13, 1.0, 0.87], projection=projection)
    ax.set_extent([-125.0, -66.0, 24.0, 50.5], crs=ccrs.PlateCarree())
    ax.set_facecolor(WATER_COLOR)

    land = cfeature.NaturalEarthFeature(
        "physical", "land", "50m", facecolor=LAND_COLOR, edgecolor="none"
    )
    states = cfeature.NaturalEarthFeature(
        "cultural",
        "admin_1_states_provinces_lines",
        "50m",
        facecolor="none",
        edgecolor="#2e2e2e",
    )
    borders = cfeature.NaturalEarthFeature(
        "cultural", "admin_0_boundary_lines_land", "50m", facecolor="none", edgecolor="#2e2e2e"
    )
    ax.add_feature(land, zorder=0)
    ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor=WATER_COLOR, zorder=0)
    ax.add_feature(cfeature.LAKES.with_scale("50m"), facecolor=WATER_COLOR, edgecolor="#2e2e2e", linewidth=0.6, zorder=1)

    map_polygons = product.maps.get(map_label, {})
    order = preview_order_for_map(map_label)
    transform = ccrs.PlateCarree()
    for label in order:
        polygons = map_polygons.get(label, ())
        for polygon_or_geometry in polygons:
            if product.source == "pts" and is_open_coordinate_sequence(polygon_or_geometry):
                _legend, face, edge = preview_style_for_label(map_label, label)
                points = list(polygon_or_geometry)
                repaired = close_open_pts_contour(points)
                if repaired is not None:
                    repaired = clip_open_pts_fill_to_conus(repaired)
                    repaired_parts = [part for part in geometry_parts(repaired) if not part.is_empty and part.area > 0.01]
                    if repaired_parts:
                        ax.add_geometries(
                            repaired_parts,
                            crs=transform,
                            facecolor=face,
                            edgecolor="none",
                            linewidth=0,
                            alpha=0.46,
                            zorder=9 + order.index(label) if label in order else 9,
                        )
                ax.plot(
                    [point[0] for point in points],
                    [point[1] for point in points],
                    transform=transform,
                    color=edge,
                    linewidth=2.35,
                    alpha=0.88,
                    solid_capstyle="round",
                    zorder=28 + order.index(label) if label in order else 28,
                )
                continue
            if hasattr(polygon_or_geometry, "geom_type"):
                geometry = polygon_or_geometry
            else:
                geometry = ShapelyPolygon(polygon_or_geometry)
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            if geometry.is_empty:
                continue
            if label.startswith("CIG"):
                draw_cig_overlay(ax, geometry, label, transform)
            else:
                _legend, face, edge = preview_style_for_label(map_label, label)
                ax.add_geometries(
                    [geometry],
                    crs=transform,
                    facecolor=face,
                    edgecolor=edge,
                    linewidth=2.2,
                    alpha=0.66,
                    zorder=10 + order.index(label) if label in order else 10,
                )

    ax.add_feature(states, linewidth=1.05, zorder=40)
    ax.add_feature(borders, linewidth=1.15, zorder=41)
    ax.coastlines(resolution="50m", linewidth=1.0, color="#2e2e2e", zorder=42)
    if is_day48_probability_map(map_label):
        draw_day48_probability_labels(ax, map_polygons, transform)
    draw_major_city_labels(ax, transform)
    if map_label == "day4-8":
        draw_day48_day_labels(ax, product, transform)

    fig.patches.append(
        Rectangle(
            (0.0, 0.0),
            0.70,
            0.13,
            transform=fig.transFigure,
            facecolor="#ffffff",
            edgecolor="#111111",
            linewidth=1.0,
        )
    )
    fig.text(0.015, 0.098, preview_title(product.spec, map_label), fontsize=23, ha="left", va="center")
    fig.text(
        0.015,
        0.066,
        f"Issued: {product.issued or 'unknown'}",
        fontsize=17,
        ha="left",
        va="center",
    )
    fig.text(
        0.015,
        0.040,
        f"Valid: {product.valid or 'unknown'}",
        fontsize=17,
        ha="left",
        va="center",
    )
    fig.text(
        0.015,
        0.017,
        preview_source_footer(product),
        fontsize=15,
        fontweight="bold",
        color="#b00020",
        ha="left",
        va="center",
    )
    fig.text(
        0.012,
        0.965,
        preview_source_badge(product),
        fontsize=15,
        fontweight="bold",
        color="#ffffff",
        bbox={"facecolor": "#111111", "edgecolor": "#111111", "boxstyle": "square,pad=0.28"},
        ha="left",
        va="center",
    )

    legend_entries: list[tuple[str, str, str, str]] = []
    if map_label == "categorical":
        for label in reversed(RISK_ORDER):
            legend, face, edge = CATEGORICAL_STYLE[label]
            legend_entries.append((legend, face, edge, ""))
        legend_title = "Risk Level"
    elif is_day48_probability_map(map_label):
        for label in reversed(DAY48_PROB_ORDER):
            legend, face, edge = DAY48_PROB_STYLE[label]
            legend_entries.append((legend, face, edge, ""))
        legend_title = "Day 4 - 8\nSevere\nWeather\nOutlook\nLegend"
    else:
        prob_order = SEVERE_PROB_ORDER if map_label in {"wind", "hail", "probabilistic"} else PROB_ORDER
        style = SEVERE_PROB_STYLE if map_label in {"wind", "hail", "probabilistic"} else PROB_STYLE
        for label in reversed(prob_order):
            legend, face, edge = style[label]
            legend_entries.append((legend, face, edge, ""))
        shown_cig_labels = cig_labels_for_map(product, map_label) or allowed_cig_labels_for_map(map_label)
        for label in shown_cig_labels:
            legend, pattern, _line_width = CIG_STYLE[label]
            legend_entries.append((legend, "white", "#111111", pattern))
        legend_title = "Probability"

    day48_legend = is_day48_probability_map(map_label)
    legend_height = 0.285 if day48_legend else min(0.395, 0.115 + 0.037 * len(legend_entries))
    legend_width = 0.165 if day48_legend else 0.215 if len(legend_entries) <= 7 else 0.225
    legend_ax = fig.add_axes([0.99 - legend_width, 0.018, legend_width, legend_height])
    legend_ax.set_axis_off()
    legend_ax.add_patch(
        Rectangle((0, 0), 1, 1, transform=legend_ax.transAxes, facecolor="#ffffff", edgecolor="#111111", linewidth=1.2)
    )
    legend_title_y = 0.76 if day48_legend else 0.925
    legend_ax.text(
        0.50,
        legend_title_y,
        legend_title,
        transform=legend_ax.transAxes,
        ha="center",
        va="center",
        fontsize=10.5 if day48_legend else 16,
        fontweight="bold",
        linespacing=1.08,
        zorder=6,
    )
    y = 0.37 if day48_legend else 0.815
    step = 0.21 if day48_legend else 0.078 if len(legend_entries) > 8 else 0.098 if len(legend_entries) > 6 else 0.116
    for legend, face, edge, pattern in legend_entries:
        x0 = 0.20 if day48_legend else 0.075
        y0 = y - (0.055 if day48_legend else 0.034)
        swatch_width = 0.52 if day48_legend else 0.205
        swatch_height = 0.11 if day48_legend else 0.068
        swatch = Rectangle(
            (x0, y0),
            swatch_width,
            swatch_height,
            transform=legend_ax.transAxes,
            facecolor=face,
            edgecolor=edge,
            linewidth=2.15,
        )
        legend_ax.add_patch(swatch)
        if pattern.startswith("cig"):
            draw_cig_legend_symbol(legend_ax, swatch, pattern)
        else:
            swatch.set_hatch(pattern or None)
        if day48_legend:
            legend_ax.text(
                x0 + swatch_width / 2,
                y,
                legend,
                transform=legend_ax.transAxes,
                ha="center",
                va="center",
                fontsize=11.2,
                fontweight="bold",
                zorder=6,
            )
            y -= step
            continue
        legend_ax.text(
            0.335,
            y,
            legend,
            transform=legend_ax.transAxes,
            ha="left",
            va="center",
            fontsize=13.4,
            zorder=6,
        )
        y -= step

    if not any(map_polygons.values()):
        fig.text(
            0.50,
            0.56,
            "NO OUTLOOK AREA",
            fontsize=36,
            fontweight="bold",
            color="#333333",
            ha="center",
            va="center",
            bbox={"facecolor": "#ffffff", "edgecolor": "#111111", "alpha": 0.82},
        )

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=dpi)
    plt.close(fig)
    return apply_image_safe_margin(buffer.getvalue(), scale=image_safe_scale())


def image_safe_scale() -> float:
    raw = os.getenv("SPC_IMAGE_SAFE_SCALE", str(DEFAULT_IMAGE_SAFE_SCALE)).strip()
    try:
        scale = float(raw)
    except ValueError as exc:
        raise BotError(f"SPC_IMAGE_SAFE_SCALE must be a number between 0.50 and 1.00, got {raw!r}") from exc
    if scale < 0.50 or scale > 1.00:
        raise BotError(f"SPC_IMAGE_SAFE_SCALE must be between 0.50 and 1.00, got {raw!r}")
    return scale


def apply_image_safe_margin(data: bytes, *, scale: float) -> bytes:
    if scale >= 0.999:
        return data
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise BotError("SPC_IMAGE_SAFE_SCALE requires Pillow; install requirements.txt or use Docker") from exc

    source = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = source.size
    inner_width = max(1, round(width * scale))
    inner_height = max(1, round(height * scale))
    resized = source.resize((inner_width, inner_height), Image.Resampling.LANCZOS)
    framed = Image.new("RGB", (width, height), "#ffffff")
    framed.paste(resized, ((width - inner_width) // 2, (height - inner_height) // 2))
    output = io.BytesIO()
    framed.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_preview_bundle(
    spec: BundleSpec,
    pts_text: str | None = None,
    *,
    custom_source: str = "geojson-first",
) -> BundleSnapshot:
    product = choose_custom_product(spec, pts_text, custom_source)
    images: list[MapImage] = []
    for map_label in spec.expected_order:
        data = render_pts_map_png(product, map_label)
        digest = hashlib.sha256(data).hexdigest()
        images.append(
            MapImage(
                label=map_label,
                url=f"{product.source}://{product.product_id}/{map_label}",
                filename=f"{spec.key}_fast_{map_label}.png",
                content_type="image/png",
                sha256=digest,
                data=data,
            )
        )
    return BundleSnapshot(
        spec=spec,
        title=f"{spec.name} Fast Custom Preview",
        updated=product.updated,
        product_id=f"preview:{product.product_id}",
        page_url=spec.page_url,
        images=tuple(images),
        risk_labels=risk_labels_from_product(product),
    )


def render_mode_posts_preview(mode: str) -> bool:
    return mode in {"custom-first", "custom-only", "both"}


def render_mode_posts_official(mode: str) -> bool:
    return mode in {"official-only", "custom-first", "both"}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"posted": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BotError(f"state file is not valid JSON: {path}: {exc}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def bundle_is_posted(
    state: dict[str, Any],
    snapshot: BundleSnapshot,
    *,
    state_key: str | None = None,
    post_key: str | None = None,
) -> bool:
    posted = state.setdefault("posted", {})
    key = state_key or snapshot.spec.key
    return posted.get(key, {}).get("post_key") == (post_key or snapshot.post_key)


def mark_posted(
    state: dict[str, Any],
    snapshot: BundleSnapshot,
    *,
    mode: str,
    reason: str,
    state_key: str | None = None,
    post_key: str | None = None,
) -> None:
    posted = state.setdefault("posted", {})
    key = state_key or snapshot.spec.key
    posted[key] = {
        "post_key": post_key or snapshot.post_key,
        "product_id": snapshot.product_id,
        "updated": snapshot.updated,
        "title": snapshot.title,
        "image_count": len(snapshot.images),
        "image_sha256": {image.label: image.sha256 for image in snapshot.images},
        "risk_labels": list(snapshot.risk_labels),
        "mode": mode,
        "reason": reason,
        "at": utc_now_iso(),
    }


def normalize_min_risk(value: str) -> str:
    lowered = value.strip().lower()
    aliases = {"none": "any", "all": "any", "enhanced": "enh", "moderate": "mdt", "high": "high"}
    normalized = aliases.get(lowered, lowered)
    valid = {"any", "tstm", "mrgl", "slgt", "enh", "mdt", "high"}
    if normalized not in valid:
        raise BotError(f"invalid risk level {value!r}; expected one of {', '.join(sorted(valid))}")
    return normalized


def max_categorical_risk(risk_labels: tuple[str, ...]) -> str | None:
    categorical = [label for label in risk_labels if label in RISK_RANK]
    if not categorical:
        return None
    return max(categorical, key=lambda label: RISK_RANK[label])


def snapshot_passes_risk_filter(
    snapshot: BundleSnapshot,
    *,
    min_risk_level: str,
    always_post_day48: bool,
) -> tuple[bool, str]:
    min_level = normalize_min_risk(min_risk_level)
    filter_active = min_level != "any" or always_post_day48
    if not filter_active:
        return True, "risk filter disabled"

    if snapshot.spec.key == "day4-8":
        has_day48_area = "DAY48_OUTLOOK" in snapshot.risk_labels
        if always_post_day48:
            if has_day48_area:
                return True, "Day 4-8 outlook area present"
            return False, "Day 4-8 has no outlook area"
        if min_level == "any":
            return True, "no Day 4-8 risk threshold"
        return False, f"Day 4-8 skipped by {min_level.upper()}+ categorical filter"

    if min_level == "any":
        return True, "no categorical threshold"
    max_risk = max_categorical_risk(snapshot.risk_labels)
    if not max_risk:
        return False, "no custom categorical risk metadata"
    threshold = RISK_RANK[min_level.upper()]
    if RISK_RANK[max_risk] >= threshold:
        return True, f"max risk {max_risk} meets {min_level.upper()}+"
    return False, f"max risk {max_risk} below {min_level.upper()}+"


def write_bundle_files(snapshot: BundleSnapshot, out_dir: Path) -> None:
    bundle_dir = out_dir / snapshot.spec.key
    bundle_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "bundle": snapshot.spec.key,
        "name": snapshot.spec.name,
        "title": snapshot.title,
        "updated": snapshot.updated,
        "product_id": snapshot.product_id,
        "page_url": snapshot.page_url,
        "post_key": snapshot.post_key,
        "risk_labels": list(snapshot.risk_labels),
        "images": [
            {
                "label": image.label,
                "url": image.url,
                "filename": image.filename,
                "content_type": image.content_type,
                "sha256": image.sha256,
                "bytes": len(image.data),
            }
            for image in snapshot.images
        ],
    }
    (bundle_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    for image in snapshot.images:
        (bundle_dir / image.filename).write_bytes(image.data)


def discord_boundary() -> str:
    return "----spc-outlook-bot-" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]


def multipart_body(payload: dict[str, Any], images: tuple[MapImage, ...]) -> tuple[bytes, str]:
    boundary = discord_boundary()
    chunks: list[bytes] = []

    def add_part(headers: dict[str, str], body: bytes) -> None:
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        for key, value in headers.items():
            chunks.append(f"{key}: {value}\r\n".encode("utf-8"))
        chunks.append(b"\r\n")
        chunks.append(body)
        chunks.append(b"\r\n")

    add_part(
        {
            "Content-Disposition": 'form-data; name="payload_json"',
            "Content-Type": "application/json; charset=utf-8",
        },
        json.dumps(payload).encode("utf-8"),
    )
    for index, image in enumerate(images):
        add_part(
            {
                "Content-Disposition": (
                    f'form-data; name="files[{index}]"; filename="{image.filename}"'
                ),
                "Content-Type": image.content_type.split(";")[0].strip() or "application/octet-stream",
            },
            image.data,
        )

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def discord_payload(snapshot: BundleSnapshot, *, content_mode: str, include_username: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if include_username:
        payload["username"] = os.getenv("DISCORD_USERNAME", "Fast Severe Outlook Bot")
    if content_mode == "short":
        payload["content"] = f"{snapshot.spec.name} - {snapshot.updated or snapshot.product_id}"
    elif content_mode == "debug":
        labels = ", ".join(image.label for image in snapshot.images)
        payload["content"] = (
            f"{snapshot.spec.name}\n"
            f"{snapshot.updated or snapshot.product_id}\n"
            f"{labels}\n"
            f"{snapshot.page_url}"
        )
    return payload


def post_to_discord_webhook(snapshot: BundleSnapshot, webhook_url: str, *, content_mode: str) -> None:
    payload = discord_payload(snapshot, content_mode=content_mode, include_username=True)
    body, content_type = multipart_body(payload, snapshot.images)
    headers = {"Content-Type": content_type}

    try:
        with request(
            webhook_url,
            method="POST",
            data=body,
            headers=headers,
            timeout=30,
            cache_bust=False,
        ) as response:
            response.read()
            status = getattr(response, "status", response.getcode())
            if status < 200 or status >= 300:
                raise BotError(f"Discord webhook returned HTTP {status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BotError(f"Discord webhook returned HTTP {exc.code}: {detail}") from exc


def post_to_discord_channel(
    snapshot: BundleSnapshot,
    *,
    bot_token: str,
    channel_id: str,
    content_mode: str,
) -> None:
    payload = discord_payload(snapshot, content_mode=content_mode, include_username=False)
    body, content_type = multipart_body(payload, snapshot.images)
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": content_type,
    }
    url = f"https://discord.com/api/v10/channels/{urllib.parse.quote(channel_id)}/messages"
    try:
        with request(
            url,
            method="POST",
            data=body,
            headers=headers,
            timeout=30,
            cache_bust=False,
        ) as response:
            response.read()
            status = getattr(response, "status", response.getcode())
            if status < 200 or status >= 300:
                raise BotError(f"Discord channel post returned HTTP {status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BotError(f"Discord channel post returned HTTP {exc.code}: {detail}") from exc


def post_bundle(
    snapshot: BundleSnapshot,
    *,
    webhook_url: str | None,
    bot_token: str | None,
    channel_id: str | None,
    dry_run: bool,
    dry_run_dir: Path,
    content_mode: str,
) -> str:
    if dry_run:
        write_bundle_files(snapshot, dry_run_dir)
        return f"dry-run wrote {len(snapshot.images)} image(s) to {dry_run_dir / snapshot.spec.key}"
    if webhook_url:
        post_to_discord_webhook(snapshot, webhook_url, content_mode=content_mode)
        return f"posted {len(snapshot.images)} image(s) to Discord webhook"
    if bot_token and channel_id:
        post_to_discord_channel(snapshot, bot_token=bot_token, channel_id=channel_id, content_mode=content_mode)
        return f"posted {len(snapshot.images)} image(s) to Discord channel"
    raise BotError("DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID is required unless --dry-run is used")


def snapshot_with_retries(spec: BundleSpec, attempts: int, delay: float) -> BundleSnapshot:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch_bundle(spec)
        except Exception as exc:  # noqa: BLE001 - logged and retried.
            last_error = exc
            log(f"{spec.name}: fetch attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(delay)
    raise BotError(f"{spec.name}: failed after {attempts} attempts: {last_error}") from last_error


class OutlookBot:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.state_path = Path(args.state_file)
        self.state = load_state(self.state_path)
        self.trigger_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.nwws_process: subprocess.Popen[str] | None = None

    def configured_post_key(self, snapshot: BundleSnapshot) -> str:
        min_level = normalize_min_risk(self.args.min_risk_level)
        filter_active = min_level != "any" or self.args.always_post_day48
        if not filter_active:
            return snapshot.post_key
        signature = f"risk:{min_level}|day48:{int(self.args.always_post_day48)}"
        return hashlib.sha256(f"{snapshot.post_key}|{signature}".encode("utf-8")).hexdigest()

    def start_nwws_if_requested(self) -> None:
        if not self.args.autostart_nwws:
            return
        if not os.getenv("NWWS_USERNAME") or not os.getenv("NWWS_PASSWORD"):
            log("NWWS autostart skipped: NWWS_USERNAME/NWWS_PASSWORD are not set")
            return
        exe = shutil.which("nwws")
        if not exe:
            log("NWWS autostart skipped: nwws executable was not found on PATH")
            return
        archive_dir = Path(self.args.nwws_archive)
        archive_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            exe,
            "serve",
            str(archive_dir),
            "--bind",
            self.args.nwws_bind,
        ]
        log(f"starting nwws-rs: {' '.join(cmd)}")
        self.nwws_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        time.sleep(1.5)

    def stop_nwws(self) -> None:
        process = self.nwws_process
        if not process or process.poll() is not None:
            return
        log("stopping nwws-rs")
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
            return
        process.kill()

    def handle_snapshot(
        self,
        snapshot: BundleSnapshot,
        reason: str,
        *,
        prime_only: bool = False,
        state_key: str | None = None,
    ) -> None:
        post_key = self.configured_post_key(snapshot)
        if bundle_is_posted(self.state, snapshot, state_key=state_key, post_key=post_key):
            log(f"{snapshot.spec.name}: unchanged ({snapshot.product_id})")
            return
        if prime_only:
            mark_posted(self.state, snapshot, mode="primed", reason=reason, state_key=state_key, post_key=post_key)
            save_state(self.state_path, self.state)
            log(f"{snapshot.spec.name}: primed current issue without posting ({snapshot.product_id})")
            return

        passes_filter, filter_reason = snapshot_passes_risk_filter(
            snapshot,
            min_risk_level=self.args.min_risk_level,
            always_post_day48=self.args.always_post_day48,
        )
        if not passes_filter:
            mark_posted(
                self.state,
                snapshot,
                mode="filtered",
                reason=f"{reason}: {filter_reason}",
                state_key=state_key,
                post_key=post_key,
            )
            save_state(self.state_path, self.state)
            log(f"{snapshot.spec.name}: skipped by risk filter; {filter_reason}; product={snapshot.product_id}")
            return

        result = post_bundle(
            snapshot,
            webhook_url=self.args.discord_webhook_url,
            bot_token=self.args.discord_bot_token,
            channel_id=self.args.discord_channel_id,
            dry_run=self.args.dry_run,
            dry_run_dir=Path(self.args.dry_run_dir),
            content_mode=self.args.message_content,
        )
        mode = "dry-run" if self.args.dry_run else "posted"
        mark_posted(self.state, snapshot, mode=mode, reason=f"{reason}: {filter_reason}", state_key=state_key, post_key=post_key)
        save_state(self.state_path, self.state)
        labels = ", ".join(image.label for image in snapshot.images)
        log(f"{snapshot.spec.name}: {result}; maps={labels}; product={snapshot.product_id}")

    def refresh_all(self, reason: str, *, prime_only: bool = False, changed_only: bool = False) -> None:
        for spec in BUNDLES:
            try:
                if render_mode_posts_preview(self.args.render_mode):
                    preview = render_preview_bundle(spec, custom_source=self.args.custom_source)
                    preview_key = f"{spec.key}:preview"
                    if not changed_only or not bundle_is_posted(self.state, preview, state_key=preview_key):
                        self.handle_snapshot(
                            preview,
                            f"{reason}:preview",
                            prime_only=prime_only,
                            state_key=preview_key,
                        )
                if render_mode_posts_official(self.args.render_mode):
                    snapshot = snapshot_with_retries(
                        spec,
                        attempts=self.args.fetch_attempts,
                        delay=self.args.fetch_retry_seconds,
                    )
                    if changed_only and bundle_is_posted(self.state, snapshot):
                        continue
                    self.handle_snapshot(snapshot, reason, prime_only=prime_only)
            except Exception as exc:  # noqa: BLE001 - keep the bot alive.
                log(f"{spec.name}: {exc}")

    def refresh_for_awips(self, awips_id: str, reason: str, *, raw_bulletin: str | None = None) -> None:
        matched = [spec for spec in BUNDLES if awips_id.upper() in spec.awips_ids]
        specs = matched or list(BUNDLES)
        for spec in specs:
            try:
                if render_mode_posts_preview(self.args.render_mode):
                    pts_text = (
                        raw_bulletin
                        if awips_id.upper().startswith("PTS") and raw_bulletin and raw_bulletin.strip()
                        else None
                    )
                    preview = render_preview_bundle(
                        spec,
                        pts_text=pts_text,
                        custom_source=self.args.custom_source,
                    )
                    self.handle_snapshot(preview, f"{reason}:preview", state_key=f"{spec.key}:preview")
                if render_mode_posts_official(self.args.render_mode):
                    snapshot = snapshot_with_retries(
                        spec,
                        attempts=max(self.args.fetch_attempts, self.args.trigger_fetch_attempts),
                        delay=self.args.fetch_retry_seconds,
                    )
                    self.handle_snapshot(snapshot, reason)
            except Exception as exc:  # noqa: BLE001 - keep the bot alive.
                log(f"{spec.name}: trigger refresh failed: {exc}")

    def start_sse_threads(self) -> list[threading.Thread]:
        if self.args.disable_nwws_sse:
            return []
        urls = [url.strip() for url in self.args.nwws_sse_urls.split(",") if url.strip()]
        threads = []
        for url in urls:
            thread = threading.Thread(target=self.sse_loop, args=(url,), daemon=True)
            thread.start()
            threads.append(thread)
        return threads

    def sse_loop(self, url: str) -> None:
        while not self.stop_event.is_set():
            try:
                log(f"connecting NWWS SSE: {url}")
                with request(url, timeout=90, cache_bust=False) as response:
                    event_data: list[str] = []
                    for raw_line in response:
                        if self.stop_event.is_set():
                            return
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            self.process_sse_event(event_data)
                            event_data = []
                            continue
                        if line.startswith("data:"):
                            event_data.append(line[5:].strip())
            except Exception as exc:  # noqa: BLE001 - reconnect forever.
                log(f"NWWS SSE disconnected: {exc}")
                time.sleep(self.args.sse_reconnect_seconds)

    def process_sse_event(self, event_data: list[str]) -> None:
        if not event_data:
            return
        text = "\n".join(event_data)
        if text == "ping":
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        awips_id = str(payload.get("awips_id", "")).upper()
        cccc = str(payload.get("cccc", "")).upper()
        if cccc != "KWNS":
            return
        watched = {awips for spec in BUNDLES for awips in spec.awips_ids}
        if awips_id not in watched:
            return
        log(f"NWWS trigger received: {cccc} {awips_id}")
        self.trigger_queue.put((awips_id, str(payload.get("raw_bulletin") or "")))

    def poll_due(self, next_poll: float) -> bool:
        return time.monotonic() >= next_poll

    def run_once(self) -> None:
        prime_only = self.args.prime_current and not self.args.post_current
        self.refresh_all("run-once", prime_only=prime_only)

    def run_forever(self) -> None:
        self.start_nwws_if_requested()
        self.start_sse_threads()

        if self.args.post_current:
            self.refresh_all("startup-post-current")
        elif self.args.prime_current:
            self.refresh_all("startup-prime-current", prime_only=True)

        next_poll = time.monotonic() + self.args.poll_seconds
        fast_path = "NWWS SSE" if not self.args.disable_nwws_sse else "raw PTS/SPC HTTP polling"
        log(
            f"running; fast path={fast_path}, fallback=poll "
            f"every {self.args.poll_seconds}s"
        )
        try:
            while not self.stop_event.is_set():
                try:
                    awips_id, raw_bulletin = self.trigger_queue.get(timeout=1.0)
                    self.refresh_for_awips(awips_id, f"nwws:{awips_id}", raw_bulletin=raw_bulletin)
                    next_poll = time.monotonic() + self.args.poll_seconds
                except queue.Empty:
                    pass

                if self.poll_due(next_poll):
                    self.refresh_all("poll", changed_only=True)
                    next_poll = time.monotonic() + self.args.poll_seconds
        finally:
            self.stop_nwws()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Post SPC outlook map bundles as soon as they update.")
    parser.add_argument("--once", action="store_true", help="check once, post/prime, then exit")
    parser.add_argument("--dry-run", action="store_true", default=env_bool("SPC_DRY_RUN", False))
    parser.add_argument(
        "--dry-run-dir",
        default=os.getenv("SPC_DRY_RUN_DIR", "data/dry-run"),
        help="where to write image bundles in dry-run mode",
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("SPC_STATE_FILE", "data/state.json"),
        help="dedupe state file",
    )
    parser.add_argument(
        "--discord-webhook-url",
        default=os.getenv("DISCORD_WEBHOOK_URL"),
        help="Discord webhook URL; required unless --dry-run is used",
    )
    parser.add_argument(
        "--discord-bot-token",
        default=os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN"),
        help="Discord bot token alternative to webhook posting",
    )
    parser.add_argument(
        "--discord-channel-id",
        default=os.getenv("DISCORD_CHANNEL_ID"),
        help="Discord channel ID for bot-token posting",
    )
    parser.add_argument(
        "--message-content",
        choices=("none", "short", "debug"),
        default=os.getenv("SPC_MESSAGE_CONTENT", "none"),
        help="Discord message text. 'none' posts image-only messages.",
    )
    parser.add_argument(
        "--render-mode",
        choices=("official-only", "custom-first", "custom-only", "both"),
        default=os.getenv("SPC_RENDER_MODE", "custom-only"),
        help=(
            "what to post: official SPC PNGs only, custom PTS previews before official plots, "
            "custom previews only, or both"
        ),
    )
    parser.add_argument(
        "--custom-source",
        choices=("geojson-first", "geojson-only", "pts-only"),
        default=os.getenv("SPC_CUSTOM_SOURCE", "geojson-first"),
        help="geometry source for custom maps: official SPC GeoJSON first, GeoJSON only, or raw PTS only",
    )
    parser.add_argument(
        "--min-risk-level",
        choices=("any", "tstm", "mrgl", "slgt", "enh", "mdt", "high"),
        default=normalize_min_risk(os.getenv("SPC_MIN_RISK_LEVEL", "any")),
        help="only post Day 1-3 custom bundles at or above this categorical risk level",
    )
    parser.add_argument(
        "--always-post-day48",
        action="store_true",
        default=env_bool("SPC_ALWAYS_POST_DAY48", False),
        help="when risk filtering is enabled, still post any Day 4-8 outlook with a 15% or 30% area",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=env_int("SPC_POLL_SECONDS", 20),
        help="SPC HTTP fallback poll cadence",
    )
    parser.add_argument(
        "--fetch-attempts",
        type=int,
        default=env_int("SPC_FETCH_ATTEMPTS", 4),
        help="fetch attempts per bundle",
    )
    parser.add_argument(
        "--trigger-fetch-attempts",
        type=int,
        default=env_int("SPC_TRIGGER_FETCH_ATTEMPTS", 12),
        help="fetch attempts after an NWWS trigger, to bridge image lag",
    )
    parser.add_argument(
        "--fetch-retry-seconds",
        type=float,
        default=float(os.getenv("SPC_FETCH_RETRY_SECONDS", "5")),
        help="delay between fetch retries",
    )
    parser.add_argument(
        "--post-current",
        action="store_true",
        default=env_bool("SPC_POST_CURRENT_ON_START", False),
        help="post the currently available four bundles on startup",
    )
    parser.add_argument(
        "--prime-current",
        action="store_true",
        default=env_bool("SPC_PRIME_CURRENT_ON_START", True),
        help="mark current bundles as seen on startup when not using --post-current",
    )
    parser.add_argument(
        "--disable-nwws-sse",
        action="store_true",
        default=env_bool("SPC_DISABLE_NWWS_SSE", False),
        help="disable nwws-rs SSE triggers and only poll SPC pages",
    )
    parser.add_argument(
        "--nwws-sse-urls",
        default=os.getenv("NWWS_SSE_URLS", DEFAULT_SSE_URLS),
        help="comma-separated nwws-rs SSE URLs",
    )
    parser.add_argument(
        "--sse-reconnect-seconds",
        type=int,
        default=env_int("NWWS_SSE_RECONNECT_SECONDS", 5),
    )
    parser.add_argument(
        "--autostart-nwws",
        action="store_true",
        default=env_bool("NWWS_AUTOSTART", False),
        help="start `nwws serve` if NWWS credentials and nwws executable are available",
    )
    parser.add_argument(
        "--nwws-bind",
        default=os.getenv("NWWS_BIND", "127.0.0.1:8080"),
    )
    parser.add_argument(
        "--nwws-archive",
        default=os.getenv("NWWS_ARCHIVE_DIR", "data/nwws-archive"),
    )
    return parser


def install_signal_handlers(bot: OutlookBot) -> None:
    def stop(_signum: int, _frame: Any) -> None:
        log("stop requested")
        bot.stop_event.set()

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)


def main(argv: list[str] | None = None) -> int:
    script_env = Path(__file__).resolve().with_name(".env")
    load_env_file(script_env)
    if Path.cwd() != script_env.parent:
        load_env_file(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    bot = OutlookBot(args)
    install_signal_handlers(bot)
    try:
        if args.once:
            bot.run_once()
        else:
            bot.run_forever()
    except KeyboardInterrupt:
        bot.stop_event.set()
        bot.stop_nwws()
    except Exception as exc:  # noqa: BLE001 - top-level user-facing error.
        log(f"fatal: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
