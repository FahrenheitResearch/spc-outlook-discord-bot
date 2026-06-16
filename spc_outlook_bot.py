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
OUTLOOK_FILL_ALPHA = 0.72
DEFAULT_IMAGE_SAFE_SCALE = 0.95
MAP_EXTENT = (-125.0, -66.0, 24.0, 50.5)
DISCORD_MAX_FILES_PER_MESSAGE = 10
DEFAULT_REGIONAL_MAPS = "categorical,day4-8"
DEFAULT_REGIONAL_MIN_RISK_LEVEL = "enh"
DEFAULT_REGIONAL_MAX_AREAS = 2
CONUS_MARINE_BOUNDARY_FILE = Path(__file__).resolve().with_name("assets") / "conus_marine_bnds.txt"
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
RAW_DISCUSSION_URLS = {
    "day1": "https://tgftp.nws.noaa.gov/data/raw/ac/acus01.kwns.swo.dy1.txt",
    "day2": "https://tgftp.nws.noaa.gov/data/raw/ac/acus02.kwns.swo.dy2.txt",
    "day3": "https://tgftp.nws.noaa.gov/data/raw/ac/acus03.kwns.swo.dy3.txt",
    "day4-8": "https://tgftp.nws.noaa.gov/data/raw/ac/acus48.kwns.swo.d48.txt",
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
    issued: str = ""
    valid: str = ""
    discussion: str = ""
    discussion_url: str = ""

    @property
    def post_key(self) -> str:
        image_hashes = ",".join(f"{image.label}:{image.sha256}" for image in self.images)
        raw = f"{self.spec.key}|{self.product_id}|{self.updated}|{image_hashes}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def product_post_key(self) -> str:
        raw = f"{self.spec.key}|{self.product_id}|{self.updated}"
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
    discussion: str = ""
    discussion_url: str = ""


@dataclasses.dataclass(frozen=True)
class MapRenderView:
    map_label: str
    suffix: str = ""
    title_suffix: str = ""
    extent: tuple[float, float, float, float] = MAP_EXTENT


@dataclasses.dataclass(frozen=True)
class DiscussionText:
    title: str
    issued: str
    valid: str
    body: str
    url: str


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


STATE_RECENT_POST_LIMIT = 16
TRANSIENT_HTTP_CODES = {404, 408, 409, 425, 429, 500, 502, 503, 504}


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


def retry_after_seconds(exc: urllib.error.HTTPError, default: float) -> float:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value:
        with contextlib.suppress(ValueError):
            return max(default, float(value))
    return default


def fetch_text_with_retries(
    url: str,
    *,
    attempts: int = 3,
    delay: float = 1.5,
    timeout: int = 20,
    context: str = "text fetch",
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch_text(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            should_retry = exc.code in TRANSIENT_HTTP_CODES and attempt < attempts
            if not should_retry:
                raise
            sleep_for = retry_after_seconds(exc, delay)
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            sleep_for = delay
        log(f"{context}: attempt {attempt}/{attempts} failed: {last_error}; retrying")
        time.sleep(sleep_for)
    raise BotError(f"{context}: failed after {attempts} attempts: {last_error}") from last_error


def fetch_json_with_retries(
    url: str,
    *,
    attempts: int = 3,
    delay: float = 1.5,
    timeout: int = 20,
    context: str = "JSON fetch",
) -> tuple[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            text = fetch_text_with_retries(
                url,
                attempts=1,
                delay=delay,
                timeout=timeout,
                context=context,
            )
            return text, json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt >= attempts:
                break
        except (urllib.error.HTTPError, TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in TRANSIENT_HTTP_CODES:
                raise
            if attempt >= attempts:
                break
        log(f"{context}: attempt {attempt}/{attempts} returned incomplete data: {last_error}; retrying")
        time.sleep(delay)
    raise BotError(f"{context}: failed after {attempts} attempts: {last_error}") from last_error


def fetch_bytes_with_retries(
    url: str,
    *,
    attempts: int = 3,
    delay: float = 1.5,
    timeout: int = 30,
    context: str = "binary fetch",
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with request(url, timeout=timeout, cache_bust=True) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= attempts:
                raise
            sleep_for = retry_after_seconds(exc, delay)
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            sleep_for = delay
        log(f"{context}: attempt {attempt}/{attempts} failed: {last_error}; retrying")
        time.sleep(sleep_for)
    raise BotError(f"{context}: failed after {attempts} attempts: {last_error}") from last_error


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


def raw_discussion_url_for_spec(spec: BundleSpec) -> str:
    raw_url = RAW_DISCUSSION_URLS.get(spec.key)
    if not raw_url:
        raise BotError(f"{spec.name}: no raw SWO discussion URL is configured")
    return raw_url


def fetch_raw_discussion_text_for_spec(spec: BundleSpec) -> DiscussionText:
    raw_url = raw_discussion_url_for_spec(spec)
    return parse_raw_discussion_text(fetch_text(raw_url, timeout=8), raw_url)


def parse_raw_discussion_text(text: str, url: str = "") -> DiscussionText:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    start_index = 0
    for index, line in enumerate(lines):
        if "Convective Outlook" in line:
            start_index = index
            break
    body_lines = lines[start_index:]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    for index, line in enumerate(body_lines):
        if line.strip() == "$$":
            body_lines = body_lines[:index]
            break

    nonempty = [line.strip() for line in body_lines if line.strip()]
    title = nonempty[0] if nonempty else ""
    issued = ""
    valid = ""
    for index, line in enumerate(nonempty):
        if title and line == title and index + 2 < len(nonempty):
            issued = nonempty[index + 2]
        if line.upper().startswith("VALID "):
            valid = line[6:].strip()
            break
    return DiscussionText(
        title=title,
        issued=issued,
        valid=valid,
        body="\n".join(body_lines).strip(),
        url=url,
    )


def find_geojson_url(html: str, spec: BundleSpec) -> str | None:
    match = re.search(r'href="([^"]*geojson\.zip)"', html, re.IGNORECASE)
    if not match:
        return None
    return urllib.parse.urljoin(spec.page_url, html_unescape_light(match.group(1)))


def fetch_geojson_product_for_spec(spec: BundleSpec) -> PtsProduct:
    try:
        return fetch_direct_geojson_product_for_spec(spec)
    except Exception as direct_error:  # noqa: BLE001
        log(f"{spec.name}: direct SPC GeoJSON unavailable, falling back to ZIP: {direct_error}")
    html = fetch_text(spec.page_url)
    geojson_url = find_geojson_url(html, spec)
    if not geojson_url:
        raise BotError(f"{spec.name}: could not find SPC GeoJSON ZIP link")
    data = fetch_bytes_with_retries(
        geojson_url,
        attempts=3,
        delay=2,
        timeout=30,
        context=f"{spec.name} GeoJSON ZIP",
    )
    return parse_geojson_zip(data, spec, geojson_url)


def geojson_slug_for_map(map_label: str) -> str | None:
    return {
        "categorical": "cat",
        "tornado": "torn",
        "wind": "wind",
        "hail": "hail",
        "probabilistic": "prob",
    }.get(map_label)


def direct_geojson_url(spec: BundleSpec, map_label: str) -> str | None:
    slug = geojson_slug_for_map(map_label)
    if not slug or spec.key not in {"day1", "day2", "day3"}:
        return None
    day = spec.key.removeprefix("day")
    return f"{SPC_BASE}/products/outlook/day{day}otlk_{slug}.lyr.geojson"


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


def parse_geojson_feature_collection(
    collection: dict[str, Any],
    maps: dict[str, dict[str, list[Any]]],
    map_label: str,
    first_properties: dict[str, Any],
) -> dict[str, Any]:
    from shapely.geometry import shape

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
    return first_properties


def geojson_product_from_maps(
    spec: BundleSpec,
    maps: dict[str, dict[str, list[Any]]],
    first_properties: dict[str, Any],
    _source_id: str,
    fallback_hash: str,
) -> PtsProduct:
    if not maps:
        raise BotError(f"{spec.name}: SPC GeoJSON did not contain expected outlook layers")

    issued, valid, valid_start = geojson_time_range(first_properties)
    issue_id = str(first_properties.get("ISSUE") or "").strip()
    product_id = f"geojson:{spec.key}:{issue_id or valid_start or fallback_hash}"
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


def fetch_direct_geojson_product_for_spec(spec: BundleSpec) -> PtsProduct:
    try:
        import shapely  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise BotError("SPC GeoJSON rendering requires shapely; install requirements.txt or use Docker") from exc

    maps: dict[str, dict[str, list[Any]]] = {}
    first_properties: dict[str, Any] = {}
    hash_source = hashlib.sha1()
    fetched_any = False
    for map_label in spec.expected_order:
        url = direct_geojson_url(spec, map_label)
        if not url:
            continue
        try:
            text, collection = fetch_json_with_retries(
                url,
                attempts=3,
                delay=1.5,
                timeout=20,
                context=f"{spec.name} {map_label} direct GeoJSON",
            )
        except Exception as exc:  # noqa: BLE001
            raise BotError(f"{map_label} direct layer fetch failed: {exc}") from exc
        fetched_any = True
        hash_source.update(text.encode("utf-8"))
        first_properties = parse_geojson_feature_collection(collection, maps, map_label, first_properties)

    if not fetched_any:
        raise BotError(f"{spec.name}: no direct live GeoJSON layers are configured")
    source_id = f"{spec.key}otlk_direct"
    return geojson_product_from_maps(spec, maps, first_properties, source_id, hash_source.hexdigest()[:12])


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
        if not spec_supports_geojson(spec):
            return pts_product_from_text_or_feed(spec, pts_text)
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
        import shapely  # noqa: F401
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
            first_properties = parse_geojson_feature_collection(collection, maps, map_label, first_properties)

    stem = Path(urllib.parse.urlsplit(geojson_url).path).name.removesuffix("-geojson.zip")
    return geojson_product_from_maps(spec, maps, first_properties, stem, hashlib.sha1(data).hexdigest()[:12])


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


@lru_cache(maxsize=1)
def conus_marine_boundary() -> Any:
    from shapely.geometry import Polygon

    # Boundary and winding approach are adapted from pyIEM's mature SPC PTS parser.
    points: list[tuple[float, float]] = []
    try:
        with CONUS_MARINE_BOUNDARY_FILE.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                lon, lat = line.split(",", 1)
                points.append((float(lon), float(lat)))
    except OSError as exc:
        raise BotError(f"could not load CONUS/marine PTS boundary: {CONUS_MARINE_BOUNDARY_FILE}") from exc
    if len(points) < 3:
        raise BotError(f"CONUS/marine PTS boundary is too small: {CONUS_MARINE_BOUNDARY_FILE}")
    if points[0] != points[-1]:
        points.append(points[0])
    polygon = Polygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        raise BotError(f"CONUS/marine PTS boundary is empty: {CONUS_MARINE_BOUNDARY_FILE}")
    return polygon


def point_outside_conus_marine(point: Any) -> bool:
    boundary = conus_marine_boundary()
    return not point.within(boundary) and point.distance(boundary) > 0.001


def conus_marine_boundary_point(point: Any) -> Any:
    boundary = conus_marine_boundary()
    return boundary.exterior.interpolate(boundary.exterior.project(point))


def ensure_pts_segment_endpoints_outside(points: list[tuple[float, float]]) -> Any:
    from shapely.affinity import translate
    from shapely.geometry import LineString, Point

    line = LineString(points)
    for index in (0, -1):
        point = Point(line.coords[index])
        if point_outside_conus_marine(point):
            continue
        point = conus_marine_boundary_point(point)
        if point.within(conus_marine_boundary()) or point.distance(conus_marine_boundary()) < 0.001:
            done = False
            for multiplier in (0.01, 0.1, 1.0):
                if done:
                    break
                for xoff, yoff in (
                    (-0.01 * multiplier, -0.01 * multiplier),
                    (-0.01 * multiplier, 0.0),
                    (-0.01 * multiplier, 0.01 * multiplier),
                    (0.0, -0.01 * multiplier),
                    (0.0, 0.01 * multiplier),
                    (0.01 * multiplier, -0.01 * multiplier),
                    (0.01 * multiplier, 0.0),
                    (0.01 * multiplier, 0.01 * multiplier),
                ):
                    nudged = translate(point, xoff=xoff, yoff=yoff)
                    if not nudged.within(conus_marine_boundary()):
                        point = nudged
                        done = True
                        break
        coords = list(line.coords)
        coords[index] = (point.x, point.y)
        line = LineString(coords)
    return line


def condition_pts_segment(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    from shapely.geometry import LineString, Point

    if len(points) < 2:
        return []
    if points[0] == points[-1]:
        return [points] if len(points) > 2 else []

    start = Point(points[0])
    end = Point(points[-1])
    if not point_outside_conus_marine(start) and not point_outside_conus_marine(end):
        start_boundary = conus_marine_boundary_point(start)
        end_boundary = conus_marine_boundary_point(end)
        if start.distance(end) < 0.5 * min(start.distance(start_boundary), end.distance(end_boundary)):
            return [[*points, points[0]]]

    line = ensure_pts_segment_endpoints_outside(points)
    intersection = line.intersection(conus_marine_boundary())
    if isinstance(intersection, LineString):
        return [[tuple(coord) for coord in line.coords]]
    parts = [
        part
        for part in getattr(intersection, "geoms", ())
        if getattr(part, "geom_type", "") == "LineString" and part.length > 0.2
    ]
    if len(parts) == 1:
        return [[tuple(coord) for coord in ensure_pts_segment_endpoints_outside(list(parts[0].coords)).coords]]
    return [[tuple(coord) for coord in ensure_pts_segment_endpoints_outside(list(part.coords)).coords] for part in parts]


def rhs_split_polygon(polygon: Any, splitter: Any) -> Any | None:
    from shapely.geometry import GeometryCollection, MultiLineString, MultiPolygon, Point
    from shapely.ops import split

    split_intersection = splitter.intersection(polygon)
    if isinstance(split_intersection, (MultiLineString, GeometryCollection)):
        lines = [
            part
            for part in split_intersection.geoms
            if getattr(part, "geom_type", "") == "LineString" and len(part.coords) >= 2
        ]
        if not lines:
            return None
        split_intersection = max(lines, key=lambda part: part.length)
    if getattr(split_intersection, "geom_type", "") != "LineString" or len(split_intersection.coords) < 2:
        return None

    result = split(polygon, splitter)
    polygons = [part for part in result.geoms if getattr(part, "geom_type", "") == "Polygon"]
    if len(polygons) > 2:
        polygons = [part for part in polygons if part.area > 0.1]
    if len(polygons) == 1:
        return polygons[0]
    if len(polygons) != 2:
        return None

    first, second = polygons
    start = Point(split_intersection.coords[0])
    end = Point(split_intersection.coords[1])
    start_distance = first.exterior.project(start)
    end_distance = first.exterior.project(end)
    return first if end_distance > start_distance else second


def wind_open_pts_lines(linestrings: list[Any]) -> list[Any]:
    rows = []
    boundary = conus_marine_boundary()
    for index, line in enumerate(linestrings):
        start = boundary.exterior.project(line.interpolate(0.0))
        end = boundary.exterior.project(line.interpolate(line.length))
        rows.append({"index": index, "start": round(start, 2), "end": round(end, 2)})
    rows.sort(key=lambda row: row["start"])

    used: set[int] = set()
    polygons = []
    for row in rows:
        index = row["index"]
        if index in used:
            continue
        used.add(index)
        started_at = row["start"]
        polygon = rhs_split_polygon(boundary, linestrings[index])
        if polygon is None:
            continue
        ended_at = row["end"]
        for _ in range(100):
            if ended_at < started_at:
                candidates = [
                    candidate
                    for candidate in rows
                    if candidate["index"] not in used and ended_at <= candidate["start"] < started_at
                ]
            else:
                candidates = [
                    candidate
                    for candidate in rows
                    if candidate["index"] not in used
                    and (candidate["start"] >= ended_at or candidate["start"] < started_at)
                ]
            if not candidates:
                if all(not polygon.equals(existing) for existing in polygons):
                    polygons.append(polygon)
                break
            candidate = candidates[0]
            used.add(candidate["index"])
            ended_at = candidate["end"]
            next_polygon = rhs_split_polygon(polygon, linestrings[candidate["index"]])
            if next_polygon is None:
                break
            polygon = next_polygon
    return polygons


def pts_sequences_to_geometry(sequences: tuple[Any, ...] | list[Any]) -> Any | None:
    from shapely.geometry import LinearRing, LineString, MultiPolygon, Polygon

    segments: list[list[tuple[float, float]]] = []
    direct_geometries = []
    for sequence in sequences:
        if hasattr(sequence, "geom_type"):
            direct_geometries.append(sequence)
            continue
        points = [tuple(point) for point in sequence if isinstance(point, tuple | list) and len(point) >= 2]
        for conditioned in condition_pts_segment(points):
            if len(conditioned) >= 2:
                segments.append(conditioned)

    polygons = []
    interiors = []
    linestrings = []
    for segment in segments:
        line = LineString(segment)
        if segment[0] == segment[-1]:
            ring = LinearRing(line)
            if ring.is_ccw:
                interiors.append(ring)
            else:
                polygons.append(Polygon(segment))
        else:
            linestrings.append(line)

    if not polygons and not linestrings and len(interiors) == 1:
        ring = interiors.pop()
        polygons.append(Polygon(ring.coords[::-1]))
    if linestrings:
        polygons.extend(wind_open_pts_lines(linestrings))
    for interior in interiors:
        for index, polygon in enumerate(polygons):
            if not polygon.intersection(interior).is_empty:
                polygons[index] = Polygon(polygon.exterior, [*polygon.interiors, interior])
                break

    boundary = conus_marine_boundary()
    cleaned = []
    for geometry in (*direct_geometries, *polygons):
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        clipped = geometry.intersection(boundary)
        if clipped.is_empty:
            continue
        for part in geometry_parts(clipped):
            if not part.is_empty and part.area > 0.01:
                cleaned.append(part)
    if not cleaned:
        return None
    return MultiPolygon(cleaned)


def repaired_open_pts_geometry(points: list[tuple[float, float]]) -> Any | None:
    from shapely.geometry import Polygon

    if len(points) < 2:
        return None

    candidates = []
    for clockwise in (False, True):
        closure = boundary_path(points[-1], points[0], MAP_EXTENT, clockwise=clockwise)
        polygon = Polygon([*points, *closure])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            continue
        try:
            clipped = polygon.intersection(conus_land_mask())
        except Exception as exc:  # noqa: BLE001
            log(f"open PTS CONUS clipping failed, using unclipped closure candidate: {exc}")
            clipped = polygon
        if not clipped.is_empty and getattr(clipped, "area", 0.0) > 0.01:
            candidates.append(clipped)

    if not candidates:
        return close_open_pts_contour(points)
    return min(candidates, key=lambda geometry: geometry.area)


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


def draw_major_city_labels(
    ax: Any,
    transform: Any,
    extent: tuple[float, float, float, float] | None = None,
) -> None:
    import matplotlib.patheffects as path_effects

    for name, lon, lat, dx, dy in MAJOR_CITY_LABELS:
        if extent is not None:
            lon0, lon1, lat0, lat1 = extent
            lon_margin = max(1.15, (lon1 - lon0) * 0.075)
            lat_margin = max(0.55, (lat1 - lat0) * 0.055)
            label_lon = lon + dx
            label_lat = lat + dy
            if not (
                lon0 + lon_margin <= lon <= lon1 - lon_margin
                and lat0 + lat_margin <= lat <= lat1 - lat_margin
                and lon0 + lon_margin <= label_lon <= lon1 - lon_margin
                and lat0 + lat_margin <= label_lat <= lat1 - lat_margin
            ):
                continue
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
    spacing = 0.42
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
        step = width * 0.20
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


def outlook_geometry_for_label(
    product: PtsProduct,
    map_label: str,
    label: str,
    polygons: tuple[Any, ...],
) -> Any | None:
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    if not polygons:
        return None
    if product.source == "pts":
        try:
            return pts_sequences_to_geometry(polygons)
        except Exception as exc:  # noqa: BLE001
            log(f"{product.spec.name} {map_label} {label}: mature PTS polygonization failed: {exc}")
            return None

    geometries = []
    for polygon_or_geometry in polygons:
        if hasattr(polygon_or_geometry, "geom_type"):
            geometry = polygon_or_geometry
        else:
            geometry = ShapelyPolygon(polygon_or_geometry)
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if not geometry.is_empty:
            geometries.append(geometry)
    if not geometries:
        return None
    return unary_union(geometries) if len(geometries) > 1 else geometries[0]


def raw_geometries_for_map(product: PtsProduct, map_label: str) -> dict[str, Any]:
    map_polygons = product.maps.get(map_label, {})
    geometries: dict[str, Any] = {}
    for label in preview_order_for_map(map_label):
        polygons = map_polygons.get(label, ())
        geometry = outlook_geometry_for_label(product, map_label, label, polygons)
        if geometry is not None and not geometry.is_empty:
            geometries[label] = geometry
    return geometries


def repaired_outlook_geometry(geometry: Any) -> Any:
    if geometry is None or geometry.is_empty:
        return geometry
    try:
        if geometry.is_valid:
            return geometry
    except Exception:  # noqa: BLE001
        pass
    try:
        from shapely.validation import make_valid

        repaired = make_valid(geometry)
    except Exception:  # noqa: BLE001
        repaired = geometry.buffer(0)
    if not repaired.is_valid:
        repaired = repaired.buffer(0)
    return repaired


def non_overlapping_outlook_fills(
    raw_geometries: dict[str, Any],
    order: tuple[str, ...],
) -> dict[str, Any]:
    from shapely.ops import unary_union

    visible: dict[str, Any] = {}
    higher_union = None
    fill_labels = [label for label in order if label in raw_geometries and not label.startswith("CIG")]
    for label in reversed(fill_labels):
        geometry = repaired_outlook_geometry(raw_geometries[label])
        if geometry.is_empty:
            continue
        fill_geometry = geometry
        if higher_union is not None and not higher_union.is_empty:
            try:
                fill_geometry = fill_geometry.difference(higher_union)
            except Exception:  # noqa: BLE001
                fill_geometry = repaired_outlook_geometry(fill_geometry).difference(
                    repaired_outlook_geometry(higher_union)
                )
        fill_geometry = repaired_outlook_geometry(fill_geometry)
        if not fill_geometry.is_empty:
            visible[label] = fill_geometry
        if higher_union is None:
            higher_union = geometry
            continue
        try:
            higher_union = unary_union((higher_union, geometry))
        except Exception:  # noqa: BLE001
            higher_union = unary_union((repaired_outlook_geometry(higher_union), geometry))
        higher_union = repaired_outlook_geometry(higher_union)
    return visible


def visible_outlook_fills_for_map(
    map_label: str,
    raw_geometries: dict[str, Any],
    order: tuple[str, ...],
) -> dict[str, Any]:
    if map_label == "categorical":
        return {
            label: repaired_outlook_geometry(raw_geometries[label])
            for label in order
            if label in raw_geometries and not label.startswith("CIG")
        }
    return non_overlapping_outlook_fills(raw_geometries, order)


def parse_regional_map_config(value: str | None, expected_order: tuple[str, ...]) -> set[str]:
    if value is None:
        value = DEFAULT_REGIONAL_MAPS
    aliases = {
        "cat": "categorical",
        "category": "categorical",
        "prob": "probabilistic",
        "day48": "day4-8",
        "d48": "day4-8",
    }
    tokens = {
        aliases.get(token.strip().lower(), token.strip().lower())
        for token in value.split(",")
        if token.strip()
    }
    if not tokens or tokens & {"none", "off", "false", "0"}:
        return set()
    if "all" in tokens:
        return set(expected_order)
    return {token for token in tokens if token in expected_order}


def regional_focus_geometries_for_map(
    map_label: str,
    raw_geometries: dict[str, Any],
    min_risk_level: str,
) -> list[Any]:
    if map_label == "categorical":
        threshold = RISK_RANK.get(min_risk_level.upper(), RISK_RANK["ENH"])
        return [
            raw_geometries[label]
            for label in RISK_ORDER
            if RISK_RANK[label] >= threshold and label in raw_geometries
        ]

    if is_day48_probability_map(map_label):
        for label in reversed(DAY48_PROB_ORDER):
            if label in raw_geometries:
                return [raw_geometries[label]]
        return []

    for label in reversed(CIG_ORDER):
        if label in raw_geometries:
            return [raw_geometries[label]]

    prob_order = SEVERE_PROB_ORDER if map_label in {"wind", "hail", "probabilistic"} else PROB_ORDER
    for label in reversed(prob_order):
        if label in raw_geometries:
            return [raw_geometries[label]]
    return []


def clamp_extent(extent: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    min_lon, max_lon, min_lat, max_lat = MAP_EXTENT
    lon0, lon1, lat0, lat1 = extent
    width = min(max(lon1 - lon0, 1.0), max_lon - min_lon)
    height = min(max(lat1 - lat0, 1.0), max_lat - min_lat)
    center_lon = min(max((lon0 + lon1) / 2, min_lon + width / 2), max_lon - width / 2)
    center_lat = min(max((lat0 + lat1) / 2, min_lat + height / 2), max_lat - height / 2)
    return (
        center_lon - width / 2,
        center_lon + width / 2,
        center_lat - height / 2,
        center_lat + height / 2,
    )


def regional_extent_for_geometry(geometry: Any) -> tuple[float, float, float, float]:
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    span_lon = max_lon - min_lon
    span_lat = max_lat - min_lat
    width = max(14.0, span_lon * 1.70)
    height = max(9.5, span_lat * 1.85)
    aspect = 1.68
    if width / height < aspect:
        width = height * aspect
    else:
        height = width / aspect
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2
    return clamp_extent(
        (
            center_lon - width / 2,
            center_lon + width / 2,
            center_lat - height / 2,
            center_lat + height / 2,
        )
    )


def extent_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    lon0 = max(first[0], second[0])
    lon1 = min(first[1], second[1])
    lat0 = max(first[2], second[2])
    lat1 = min(first[3], second[3])
    if lon1 <= lon0 or lat1 <= lat0:
        return 0.0
    overlap = (lon1 - lon0) * (lat1 - lat0)
    first_area = (first[1] - first[0]) * (first[3] - first[2])
    second_area = (second[1] - second[0]) * (second[3] - second[2])
    return overlap / max(1e-6, min(first_area, second_area))


def regional_render_views(
    product: PtsProduct,
    map_label: str,
    *,
    regional_maps: str,
    regional_min_risk_level: str,
    regional_max_areas: int,
) -> list[MapRenderView]:
    if regional_max_areas <= 0:
        return []
    enabled = parse_regional_map_config(regional_maps, product.spec.expected_order)
    if map_label not in enabled:
        return []

    from shapely.ops import unary_union

    focus_geometries = [
        repaired_outlook_geometry(geometry)
        for geometry in regional_focus_geometries_for_map(
            map_label,
            raw_geometries_for_map(product, map_label),
            regional_min_risk_level,
        )
    ]
    focus_geometries = [geometry for geometry in focus_geometries if geometry is not None and not geometry.is_empty]
    if not focus_geometries:
        return []

    merged = unary_union(focus_geometries) if len(focus_geometries) > 1 else focus_geometries[0]
    parts = [
        repaired_outlook_geometry(part)
        for part in geometry_parts(merged)
        if not part.is_empty and part.area >= 0.04
    ]
    parts.sort(key=lambda part: part.area, reverse=True)

    views: list[MapRenderView] = []
    extents: list[tuple[float, float, float, float]] = []
    for part in parts:
        extent = regional_extent_for_geometry(part)
        if any(extent_overlap_ratio(extent, existing) > 0.82 for existing in extents):
            continue
        index = len(views) + 1
        views.append(
            MapRenderView(
                map_label=map_label,
                suffix=f"regional_{index}",
                title_suffix=f"Regional {index}",
                extent=extent,
            )
        )
        extents.append(extent)
        if len(views) >= regional_max_areas:
            break
    return views


def render_views_for_product(
    product: PtsProduct,
    *,
    regional_maps: str,
    regional_min_risk_level: str,
    regional_max_areas: int,
) -> list[MapRenderView]:
    views: list[MapRenderView] = []
    for map_label in product.spec.expected_order:
        views.append(MapRenderView(map_label=map_label))
        views.extend(
            regional_render_views(
                product,
                map_label,
                regional_maps=regional_maps,
                regional_min_risk_level=regional_min_risk_level,
                regional_max_areas=regional_max_areas,
            )
        )
    return views


def draw_day48_probability_labels(ax: Any, map_polygons: dict[str, tuple[Any, ...]], transform: Any) -> None:
    import matplotlib.patheffects as path_effects
    from shapely.geometry import Polygon as ShapelyPolygon

    for label in DAY48_PROB_ORDER:
        legend, _face, edge = DAY48_PROB_STYLE[label]
        polygons = map_polygons.get(label, ())
        geometries = []
        if any(is_open_coordinate_sequence(polygon_or_geometry) for polygon_or_geometry in polygons):
            geometry = pts_sequences_to_geometry(polygons)
            if geometry is not None:
                geometries.append(geometry)
        else:
            for polygon_or_geometry in polygons:
                if hasattr(polygon_or_geometry, "geom_type"):
                    geometry = polygon_or_geometry
                else:
                    geometry = ShapelyPolygon(polygon_or_geometry)
                geometries.append(geometry)
        for geometry in geometries:
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
            polygons = product.maps.get(day_key, {}).get(label, ())
            if any(is_open_coordinate_sequence(polygon_or_geometry) for polygon_or_geometry in polygons):
                geometry = pts_sequences_to_geometry(polygons)
                if geometry is not None and not geometry.is_empty:
                    geometries.append(geometry)
                continue
            for polygon_or_geometry in polygons:
                geometry = (
                    polygon_or_geometry
                    if hasattr(polygon_or_geometry, "geom_type")
                    else ShapelyPolygon(polygon_or_geometry)
                )
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


def render_pts_map_png(product: PtsProduct, map_label: str, view: MapRenderView | None = None) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import to_rgba
        from matplotlib.patches import Rectangle

        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
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
    view = view or MapRenderView(map_label=map_label)
    ax = fig.add_axes([0.0, 0.13, 1.0, 0.87], projection=projection)
    ax.set_extent(list(view.extent), crs=ccrs.PlateCarree())
    if view.suffix:
        ax.set_aspect("auto")
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
    raw_geometries = raw_geometries_for_map(product, map_label)

    visible_fills = visible_outlook_fills_for_map(map_label, raw_geometries, order)
    for label in order:
        if label.startswith("CIG"):
            continue
        geometry = visible_fills.get(label)
        if geometry is None or geometry.is_empty:
            continue
        _legend, face, _edge = preview_style_for_label(map_label, label)
        parts = [part for part in geometry_parts(geometry) if not part.is_empty and part.area > 0.01]
        if parts:
            ax.add_geometries(
                parts,
                crs=transform,
                facecolor=face,
                edgecolor="none",
                linewidth=0.0,
                alpha=OUTLOOK_FILL_ALPHA,
                zorder=9 + order.index(label) if label in order else 9,
            )

    for label in order:
        if label.startswith("CIG"):
            continue
        geometry = raw_geometries.get(label)
        if geometry is None or geometry.is_empty:
            continue
        _legend, _face, edge = preview_style_for_label(map_label, label)
        parts = [part for part in geometry_parts(geometry) if not part.is_empty and part.area > 0.01]
        if parts:
            ax.add_geometries(
                parts,
                crs=transform,
                facecolor="none",
                edgecolor=edge,
                linewidth=2.2,
                alpha=0.94,
                zorder=24 + order.index(label) if label in order else 24,
            )

    for label in order:
        if not label.startswith("CIG"):
            continue
        geometry = raw_geometries.get(label)
        if geometry is None or geometry.is_empty:
            continue
        for part in geometry_parts(geometry):
            if not part.is_empty and part.area > 0.01:
                draw_cig_overlay(ax, part, label, transform)

    ax.add_feature(states, linewidth=1.05, zorder=40)
    ax.add_feature(borders, linewidth=1.15, zorder=41)
    ax.coastlines(resolution="50m", linewidth=1.0, color="#2e2e2e", zorder=42)
    if is_day48_probability_map(map_label):
        draw_day48_probability_labels(ax, map_polygons, transform)
    draw_major_city_labels(ax, transform, extent=view.extent if view.suffix else None)
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
    title = preview_title(product.spec, map_label)
    if view.title_suffix:
        title = f"{title} - {view.title_suffix}"
    fig.text(0.015, 0.098, title, fontsize=23, ha="left", va="center")
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
            facecolor=to_rgba(face, OUTLOOK_FILL_ALPHA) if not pattern else face,
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
    regional_maps: str = DEFAULT_REGIONAL_MAPS,
    regional_min_risk_level: str = DEFAULT_REGIONAL_MIN_RISK_LEVEL,
    regional_max_areas: int = DEFAULT_REGIONAL_MAX_AREAS,
) -> BundleSnapshot:
    product = choose_custom_product(spec, pts_text, custom_source)
    return render_product_bundle(
        product,
        regional_maps=regional_maps,
        regional_min_risk_level=regional_min_risk_level,
        regional_max_areas=regional_max_areas,
    )


def preview_snapshot_from_product(product: PtsProduct, images: tuple[MapImage, ...] = ()) -> BundleSnapshot:
    return BundleSnapshot(
        spec=product.spec,
        title=f"{product.spec.name} Fast Custom Preview",
        updated=product.updated,
        product_id=f"preview:{product.product_id}",
        page_url=product.spec.page_url,
        images=images,
        risk_labels=risk_labels_from_product(product),
        issued=product.issued,
        valid=product.valid,
        discussion=product.discussion,
        discussion_url=product.discussion_url,
    )


def render_product_bundle(
    product: PtsProduct,
    *,
    regional_maps: str = DEFAULT_REGIONAL_MAPS,
    regional_min_risk_level: str = DEFAULT_REGIONAL_MIN_RISK_LEVEL,
    regional_max_areas: int = DEFAULT_REGIONAL_MAX_AREAS,
) -> BundleSnapshot:
    images: list[MapImage] = []
    for view in render_views_for_product(
        product,
        regional_maps=regional_maps,
        regional_min_risk_level=regional_min_risk_level,
        regional_max_areas=regional_max_areas,
    ):
        data = render_pts_map_png(product, view.map_label, view=view)
        digest = hashlib.sha256(data).hexdigest()
        label = view.map_label if not view.suffix else f"{view.map_label}_{view.suffix}"
        images.append(
            MapImage(
                label=label,
                url=f"{product.source}://{product.product_id}/{label}",
                filename=f"{product.spec.key}_fast_{label}.png",
                content_type="image/png",
                sha256=digest,
                data=data,
            )
        )
    return preview_snapshot_from_product(product, tuple(images))


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
    target = post_key or snapshot.post_key
    entry = posted.get(key, {})
    recent = entry.get("recent_post_keys") or []
    return entry.get("post_key") == target or target in recent


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
    target = post_key or snapshot.post_key
    previous = posted.get(key, {})
    recent_candidates = [
        target,
        previous.get("post_key"),
        *(previous.get("recent_post_keys") or []),
    ]
    recent_post_keys: list[str] = []
    for candidate in recent_candidates:
        if isinstance(candidate, str) and candidate and candidate not in recent_post_keys:
            recent_post_keys.append(candidate)
        if len(recent_post_keys) >= STATE_RECENT_POST_LIMIT:
            break
    posted[key] = {
        "post_key": target,
        "recent_post_keys": recent_post_keys,
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


def discord_image_chunks(images: tuple[MapImage, ...]) -> list[tuple[MapImage, ...]]:
    if not images:
        return [()]
    return [
        tuple(images[index : index + DISCORD_MAX_FILES_PER_MESSAGE])
        for index in range(0, len(images), DISCORD_MAX_FILES_PER_MESSAGE)
    ]


def add_query_param(url: str, key: str, value: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(item_key, item_value) for item_key, item_value in query if item_key != key]
    query.append((key, value))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def truncate_discord_text(value: str, limit: int) -> str:
    clean = value.strip()
    if len(clean) <= limit:
        return clean
    clipped = clean[: max(0, limit - 4)].rstrip()
    if "\n" in clipped:
        clipped = clipped.rsplit("\n", 1)[0].rstrip()
    return f"{clipped}\n..."


def normalized_time_range(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().upper())


def snapshot_with_raw_discussion(snapshot: BundleSnapshot) -> BundleSnapshot:
    if snapshot.discussion:
        return snapshot
    try:
        discussion = fetch_raw_discussion_text_for_spec(snapshot.spec)
    except Exception as exc:  # noqa: BLE001
        log(f"{snapshot.spec.name}: raw SWO discussion fetch failed: {exc}")
        return snapshot
    if snapshot.valid and discussion.valid and normalized_time_range(snapshot.valid) != normalized_time_range(discussion.valid):
        log(
            f"{snapshot.spec.name}: raw SWO discussion valid time {discussion.valid!r} "
            f"does not match PTS valid time {snapshot.valid!r}; skipping discussion embed"
        )
        return snapshot
    return dataclasses.replace(
        snapshot,
        issued=discussion.issued or snapshot.issued,
        valid=discussion.valid or snapshot.valid,
        discussion=discussion.body,
        discussion_url=discussion.url,
    )


def discord_discussion_embed(snapshot: BundleSnapshot) -> dict[str, Any] | None:
    if not (snapshot.discussion or snapshot.issued or snapshot.valid):
        return None
    description = truncate_discord_text(snapshot.discussion, 2400) if snapshot.discussion else ""
    fields = []
    if snapshot.valid:
        fields.append({"name": "Valid", "value": snapshot.valid, "inline": True})
    if snapshot.issued:
        fields.append({"name": "Issued", "value": snapshot.issued, "inline": True})
    fields.append({"name": "Source", "value": "NOAA/NWS Storm Prediction Center raw text", "inline": False})
    official_images = bool(snapshot.images) and all(image.url.startswith(SPC_BASE) for image in snapshot.images)
    footer_text = (
        "Official SPC image files - notification by Outlook Notification"
        if official_images
        else "Unofficial Yalllooks render - not an official SPC/NWS graphic"
    )
    embed: dict[str, Any] = {
        "title": f"{snapshot.spec.name} - Discussion",
        "description": description or None,
        "color": 0xF4D03F,
        "fields": fields,
        "footer": {"text": footer_text},
    }
    return {key: value for key, value in embed.items() if value is not None}


def discord_link_button(label: str, url: str) -> dict[str, Any]:
    return {
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 5,
                "label": label,
                "url": url,
            }
        ],
    }


def discord_payload(snapshot: BundleSnapshot, *, content_mode: str, include_username: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if include_username:
        payload["username"] = os.getenv("DISCORD_USERNAME", "Fast Severe Outlook Bot")
    if content_mode == "link":
        content_lines = [snapshot.spec.name, f"Updated: {snapshot.updated or snapshot.product_id}"]
        if snapshot.images and snapshot.page_url:
            content_lines.append(f"Official SPC discussion/product: <{snapshot.page_url}>")
        payload["content"] = "\n".join(content_lines)
        embed = discord_discussion_embed(snapshot)
        if embed:
            payload["embeds"] = [embed]
        if snapshot.discussion_url:
            payload["components"] = [discord_link_button("View Discussion", snapshot.discussion_url)]
    elif content_mode == "short":
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


def post_to_discord_webhook(snapshot: BundleSnapshot, webhook_url: str, *, content_mode: str) -> int:
    chunks = discord_image_chunks(snapshot.images)
    for index, images in enumerate(chunks):
        chunk_snapshot = dataclasses.replace(snapshot, images=images)
        payload = discord_payload(
            chunk_snapshot,
            content_mode=content_mode if index == 0 else "none",
            include_username=True,
        )
        body, content_type = multipart_body(payload, images)
        headers = {"Content-Type": content_type}
        target_url = add_query_param(webhook_url, "with_components", "true") if payload.get("components") else webhook_url

        try:
            with request(
                target_url,
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
    return len(chunks)


def post_to_discord_channel(
    snapshot: BundleSnapshot,
    *,
    bot_token: str,
    channel_id: str,
    content_mode: str,
) -> int:
    url = f"https://discord.com/api/v10/channels/{urllib.parse.quote(channel_id)}/messages"
    chunks = discord_image_chunks(snapshot.images)
    for index, images in enumerate(chunks):
        chunk_snapshot = dataclasses.replace(snapshot, images=images)
        payload = discord_payload(
            chunk_snapshot,
            content_mode=content_mode if index == 0 else "none",
            include_username=False,
        )
        body, content_type = multipart_body(payload, images)
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": content_type,
        }
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
    return len(chunks)


def post_bundle(
    snapshot: BundleSnapshot,
    *,
    webhook_url: str | None,
    bot_token: str | None,
    channel_id: str | None,
    dry_run: bool,
    dry_run_dir: Path,
    content_mode: str,
    include_discussion: bool = True,
) -> str:
    if content_mode == "link" and include_discussion:
        snapshot = snapshot_with_raw_discussion(snapshot)
    if dry_run:
        write_bundle_files(snapshot, dry_run_dir)
        return f"dry-run wrote {len(snapshot.images)} image(s) to {dry_run_dir / snapshot.spec.key}"
    if webhook_url:
        message_count = post_to_discord_webhook(snapshot, webhook_url, content_mode=content_mode)
        return f"posted {len(snapshot.images)} image(s) to Discord webhook in {message_count} message(s)"
    if bot_token and channel_id:
        message_count = post_to_discord_channel(snapshot, bot_token=bot_token, channel_id=channel_id, content_mode=content_mode)
        return f"posted {len(snapshot.images)} image(s) to Discord channel in {message_count} message(s)"
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


def merge_official_snapshot_metadata(
    official: BundleSnapshot,
    metadata: BundleSnapshot | None,
) -> BundleSnapshot:
    if metadata is None:
        return official
    return dataclasses.replace(
        official,
        risk_labels=metadata.risk_labels or official.risk_labels,
        issued=metadata.issued or official.issued,
        valid=metadata.valid or official.valid,
        discussion=metadata.discussion or official.discussion,
        discussion_url=metadata.discussion_url or official.discussion_url,
    )


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
        base_key = snapshot.product_post_key if snapshot.product_id.startswith("preview:") else snapshot.post_key
        if not filter_active:
            return base_key
        signature = f"risk:{min_level}|day48:{int(self.args.always_post_day48)}"
        return hashlib.sha256(f"{base_key}|{signature}".encode("utf-8")).hexdigest()

    def discussion_post_key(self, snapshot: BundleSnapshot) -> str:
        raw = f"discussion|{snapshot.spec.key}|{snapshot.product_id}|{snapshot.updated}|{snapshot.valid}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def should_prepost_discussion(self) -> bool:
        return (
            self.args.prepost_discussion
            and self.args.message_content == "link"
            and self.args.custom_source == "pts-only"
            and render_mode_posts_preview(self.args.render_mode)
        )

    def official_risk_metadata(self, spec: BundleSpec, pts_text: str | None = None) -> BundleSnapshot | None:
        try:
            product = choose_custom_product(spec, pts_text, self.args.custom_source)
        except Exception as exc:  # noqa: BLE001 - official images can still be posted without the filter metadata.
            log(f"{spec.name}: could not load official risk metadata: {exc}")
            return None
        return preview_snapshot_from_product(product)

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

    def prepost_discussion(
        self,
        snapshot: BundleSnapshot,
        reason: str,
        *,
        prime_only: bool = False,
    ) -> bool:
        state_key = f"{snapshot.spec.key}:discussion"
        post_key = self.discussion_post_key(snapshot)
        if bundle_is_posted(self.state, snapshot, state_key=state_key, post_key=post_key):
            return True
        if prime_only:
            mark_posted(
                self.state,
                snapshot,
                mode="primed",
                reason=f"{reason}:discussion",
                state_key=state_key,
                post_key=post_key,
            )
            save_state(self.state_path, self.state)
            return True

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
                reason=f"{reason}:discussion: {filter_reason}",
                state_key=state_key,
                post_key=post_key,
            )
            save_state(self.state_path, self.state)
            return False

        discussion_snapshot = snapshot_with_raw_discussion(dataclasses.replace(snapshot, images=()))
        if not discussion_snapshot.discussion:
            return False
        try:
            result = post_bundle(
                discussion_snapshot,
                webhook_url=self.args.discord_webhook_url,
                bot_token=self.args.discord_bot_token,
                channel_id=self.args.discord_channel_id,
                dry_run=self.args.dry_run,
                dry_run_dir=Path(self.args.dry_run_dir),
                content_mode="link",
                include_discussion=False,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"{snapshot.spec.name}: fast raw discussion prepost failed: {exc}")
            return False
        mode = "dry-run" if self.args.dry_run else "posted"
        mark_posted(
            self.state,
            discussion_snapshot,
            mode=mode,
            reason=f"{reason}:discussion: {filter_reason}",
            state_key=state_key,
            post_key=post_key,
        )
        save_state(self.state_path, self.state)
        log(f"{snapshot.spec.name}: {result}; fast raw discussion prepost; product={snapshot.product_id}")
        return True

    def handle_snapshot(
        self,
        snapshot: BundleSnapshot,
        reason: str,
        *,
        prime_only: bool = False,
        state_key: str | None = None,
        include_discussion: bool = True,
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
            include_discussion=include_discussion,
        )
        mode = "dry-run" if self.args.dry_run else "posted"
        mark_posted(self.state, snapshot, mode=mode, reason=f"{reason}: {filter_reason}", state_key=state_key, post_key=post_key)
        save_state(self.state_path, self.state)
        labels = ", ".join(image.label for image in snapshot.images)
        log(f"{snapshot.spec.name}: {result}; maps={labels}; product={snapshot.product_id}")

    def refresh_all(self, reason: str, *, prime_only: bool = False, changed_only: bool = False) -> None:
        for spec in BUNDLES:
            try:
                official_metadata: BundleSnapshot | None = None
                if render_mode_posts_preview(self.args.render_mode):
                    preview_key = f"{spec.key}:preview"
                    preposted = False
                    if self.should_prepost_discussion():
                        pts_text = fetch_raw_pts_text_for_spec(spec)
                        product = choose_custom_product(spec, pts_text, self.args.custom_source)
                        metadata = preview_snapshot_from_product(product)
                        official_metadata = metadata
                        post_key = self.configured_post_key(metadata)
                        if changed_only and bundle_is_posted(self.state, metadata, state_key=preview_key, post_key=post_key):
                            log(f"{metadata.spec.name}: unchanged ({metadata.product_id})")
                            if not render_mode_posts_official(self.args.render_mode):
                                continue
                            preview = metadata
                            preposted = True
                        else:
                            preposted = self.prepost_discussion(
                                metadata,
                                f"{reason}:preview",
                                prime_only=prime_only,
                            )
                            preview = render_product_bundle(
                                product,
                                regional_maps=self.args.regional_maps,
                                regional_min_risk_level=self.args.regional_min_risk_level,
                                regional_max_areas=self.args.regional_max_areas,
                            )
                    else:
                        preview = render_preview_bundle(
                            spec,
                            custom_source=self.args.custom_source,
                            regional_maps=self.args.regional_maps,
                            regional_min_risk_level=self.args.regional_min_risk_level,
                            regional_max_areas=self.args.regional_max_areas,
                        )
                        official_metadata = dataclasses.replace(preview, images=())
                    if not changed_only or not bundle_is_posted(self.state, preview, state_key=preview_key):
                        self.handle_snapshot(
                            preview,
                            f"{reason}:preview",
                            prime_only=prime_only,
                            state_key=preview_key,
                            include_discussion=not preposted,
                        )
                if render_mode_posts_official(self.args.render_mode):
                    if official_metadata is None:
                        official_metadata = self.official_risk_metadata(spec)
                    snapshot = snapshot_with_retries(
                        spec,
                        attempts=self.args.fetch_attempts,
                        delay=self.args.fetch_retry_seconds,
                    )
                    snapshot = merge_official_snapshot_metadata(snapshot, official_metadata)
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
                official_metadata: BundleSnapshot | None = None
                if render_mode_posts_preview(self.args.render_mode):
                    pts_text = (
                        raw_bulletin
                        if awips_id.upper().startswith("PTS") and raw_bulletin and raw_bulletin.strip()
                        else None
                    )
                    preposted = False
                    if self.should_prepost_discussion():
                        if pts_text is None:
                            pts_text = fetch_raw_pts_text_for_spec(spec)
                        product = choose_custom_product(spec, pts_text, self.args.custom_source)
                        metadata = preview_snapshot_from_product(product)
                        official_metadata = metadata
                        preposted = self.prepost_discussion(metadata, f"{reason}:preview")
                        preview = render_product_bundle(
                            product,
                            regional_maps=self.args.regional_maps,
                            regional_min_risk_level=self.args.regional_min_risk_level,
                            regional_max_areas=self.args.regional_max_areas,
                        )
                    else:
                        preview = render_preview_bundle(
                            spec,
                            pts_text=pts_text,
                            custom_source=self.args.custom_source,
                            regional_maps=self.args.regional_maps,
                            regional_min_risk_level=self.args.regional_min_risk_level,
                            regional_max_areas=self.args.regional_max_areas,
                        )
                        official_metadata = dataclasses.replace(preview, images=())
                    self.handle_snapshot(
                        preview,
                        f"{reason}:preview",
                        state_key=f"{spec.key}:preview",
                        include_discussion=not preposted,
                    )
                if render_mode_posts_official(self.args.render_mode):
                    if official_metadata is None:
                        pts_text = (
                            raw_bulletin
                            if awips_id.upper().startswith("PTS") and raw_bulletin and raw_bulletin.strip()
                            else None
                        )
                        official_metadata = self.official_risk_metadata(spec, pts_text)
                    snapshot = snapshot_with_retries(
                        spec,
                        attempts=max(self.args.fetch_attempts, self.args.trigger_fetch_attempts),
                        delay=self.args.fetch_retry_seconds,
                    )
                    snapshot = merge_official_snapshot_metadata(snapshot, official_metadata)
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
        choices=("none", "link", "short", "debug"),
        default=os.getenv("SPC_MESSAGE_CONTENT", "none"),
        help="Discord message text. 'link' adds raw SWODY discussion metadata.",
    )
    parser.add_argument(
        "--prepost-discussion",
        action="store_true",
        default=env_bool("SPC_PREPOST_DISCUSSION", False),
        help="in pts-only link mode, post the raw SWODY discussion card before rendering image maps",
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
        default=os.getenv("SPC_CUSTOM_SOURCE", "geojson-only"),
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
        "--regional-maps",
        default=os.getenv("SPC_REGIONAL_MAPS", DEFAULT_REGIONAL_MAPS),
        help=(
            "comma-separated custom maps that should get auto-zoom regional images; "
            "use 'none' or 'all'"
        ),
    )
    parser.add_argument(
        "--regional-min-risk-level",
        choices=("tstm", "mrgl", "slgt", "enh", "mdt", "high"),
        default=normalize_min_risk(
            os.getenv("SPC_REGIONAL_MIN_RISK_LEVEL", DEFAULT_REGIONAL_MIN_RISK_LEVEL)
        ),
        help="minimum categorical risk used to choose Day 1-3 regional zoom centers",
    )
    parser.add_argument(
        "--regional-max-areas",
        type=int,
        default=env_int("SPC_REGIONAL_MAX_AREAS", DEFAULT_REGIONAL_MAX_AREAS),
        help="maximum auto-zoom regional images per enabled map",
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
