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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SPC_BASE = "https://www.spc.noaa.gov"
USER_AGENT = "spc-outlook-bot/1.0 (+https://www.spc.noaa.gov/)"
DEFAULT_SSE_URLS = (
    "http://127.0.0.1:8080/v1/stream?office=KWNS&pil=PTS,"
    "http://127.0.0.1:8080/v1/stream?office=KWNS&pil=SWO"
)


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

    @property
    def post_key(self) -> str:
        image_hashes = ",".join(f"{image.label}:{image.sha256}" for image in self.images)
        raw = f"{self.spec.key}|{self.product_id}|{self.updated}|{image_hashes}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


BUNDLES: tuple[BundleSpec, ...] = (
    BundleSpec(
        key="day1",
        name="SPC Day 1 Outlook",
        page_url=f"{SPC_BASE}/products/outlook/day1otlk.html",
        awips_ids=("PTSDY1", "SWODY1"),
        expected_order=("categorical", "tornado", "wind", "hail"),
    ),
    BundleSpec(
        key="day2",
        name="SPC Day 2 Outlook",
        page_url=f"{SPC_BASE}/products/outlook/day2otlk.html",
        awips_ids=("PTSDY2", "SWODY2"),
        expected_order=("categorical", "tornado", "wind", "hail"),
    ),
    BundleSpec(
        key="day3",
        name="SPC Day 3 Outlook",
        page_url=f"{SPC_BASE}/products/outlook/day3otlk.html",
        awips_ids=("PTSDY3", "SWODY3"),
        expected_order=("categorical", "probabilistic"),
    ),
    BundleSpec(
        key="day4-8",
        name="SPC Day 4-8 Outlook",
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


def bundle_is_posted(state: dict[str, Any], snapshot: BundleSnapshot) -> bool:
    posted = state.setdefault("posted", {})
    return posted.get(snapshot.spec.key, {}).get("post_key") == snapshot.post_key


def mark_posted(
    state: dict[str, Any],
    snapshot: BundleSnapshot,
    *,
    mode: str,
    reason: str,
) -> None:
    posted = state.setdefault("posted", {})
    posted[snapshot.spec.key] = {
        "post_key": snapshot.post_key,
        "product_id": snapshot.product_id,
        "updated": snapshot.updated,
        "title": snapshot.title,
        "image_count": len(snapshot.images),
        "image_sha256": {image.label: image.sha256 for image in snapshot.images},
        "mode": mode,
        "reason": reason,
        "at": utc_now_iso(),
    }


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


def post_to_discord(snapshot: BundleSnapshot, webhook_url: str, *, content_mode: str) -> None:
    payload: dict[str, Any] = {
        "username": os.getenv("DISCORD_USERNAME", "SPC Outlook Bot"),
        "allowed_mentions": {"parse": []},
    }
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


def post_bundle(
    snapshot: BundleSnapshot,
    *,
    webhook_url: str | None,
    dry_run: bool,
    dry_run_dir: Path,
    content_mode: str,
) -> str:
    if dry_run:
        write_bundle_files(snapshot, dry_run_dir)
        return f"dry-run wrote {len(snapshot.images)} image(s) to {dry_run_dir / snapshot.spec.key}"
    if not webhook_url:
        raise BotError("DISCORD_WEBHOOK_URL is required unless --dry-run is used")
    post_to_discord(snapshot, webhook_url, content_mode=content_mode)
    return f"posted {len(snapshot.images)} image(s) to Discord"


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
        self.trigger_queue: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self.nwws_process: subprocess.Popen[str] | None = None

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

    def handle_snapshot(self, snapshot: BundleSnapshot, reason: str, *, prime_only: bool = False) -> None:
        if bundle_is_posted(self.state, snapshot):
            log(f"{snapshot.spec.name}: unchanged ({snapshot.product_id})")
            return
        if prime_only:
            mark_posted(self.state, snapshot, mode="primed", reason=reason)
            save_state(self.state_path, self.state)
            log(f"{snapshot.spec.name}: primed current issue without posting ({snapshot.product_id})")
            return

        result = post_bundle(
            snapshot,
            webhook_url=self.args.discord_webhook_url,
            dry_run=self.args.dry_run,
            dry_run_dir=Path(self.args.dry_run_dir),
            content_mode=self.args.message_content,
        )
        mode = "dry-run" if self.args.dry_run else "posted"
        mark_posted(self.state, snapshot, mode=mode, reason=reason)
        save_state(self.state_path, self.state)
        labels = ", ".join(image.label for image in snapshot.images)
        log(f"{snapshot.spec.name}: {result}; maps={labels}; product={snapshot.product_id}")

    def refresh_all(self, reason: str, *, prime_only: bool = False, changed_only: bool = False) -> None:
        for spec in BUNDLES:
            try:
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

    def refresh_for_awips(self, awips_id: str, reason: str) -> None:
        matched = [spec for spec in BUNDLES if awips_id.upper() in spec.awips_ids]
        specs = matched or list(BUNDLES)
        for spec in specs:
            try:
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
        self.trigger_queue.put(awips_id)

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
        log(
            "running; fast path=NWWS SSE, fallback=poll "
            f"every {self.args.poll_seconds}s"
        )
        try:
            while not self.stop_event.is_set():
                try:
                    awips_id = self.trigger_queue.get(timeout=1.0)
                    self.refresh_for_awips(awips_id, f"nwws:{awips_id}")
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
        "--message-content",
        choices=("none", "short", "debug"),
        default=os.getenv("SPC_MESSAGE_CONTENT", "none"),
        help="Discord message text. 'none' posts image-only messages.",
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
