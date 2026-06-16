#!/usr/bin/env python3
"""Render the latest custom outlook plots and build a local HTML proof page."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402

SPC_FIRE_BASE = "https://www.spc.noaa.gov/products/fire_wx"
SPC_FIRE_DAY38_BASE = "https://www.spc.noaa.gov/products/exper/fire_wx"
WPC_ERO_BASE = "https://www.wpc.ncep.noaa.gov/qpf"

FIRE_WEATHER_IMAGES = (
    (
        "day1_fire",
        "Day 1 Fire Weather",
        f"{SPC_FIRE_BASE}/fwdy1.html",
        f"{SPC_FIRE_BASE}/day1otlk_fire.png",
    ),
    (
        "day2_fire",
        "Day 2 Fire Weather",
        f"{SPC_FIRE_BASE}/fwdy2.html",
        f"{SPC_FIRE_BASE}/day2otlk_fire.png",
    ),
    (
        "day38_fire",
        "Day 3-8 Fire Weather Composite",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day38otlk_fire.gif",
    ),
    (
        "day3_fire",
        "Day 3 Fire Weather",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day3otlk_fire.gif",
    ),
    (
        "day4_fire",
        "Day 4 Fire Weather",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day4otlk_fire.gif",
    ),
    (
        "day5_fire",
        "Day 5 Fire Weather",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day5otlk_fire.gif",
    ),
    (
        "day6_fire",
        "Day 6 Fire Weather",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day6otlk_fire.gif",
    ),
    (
        "day7_fire",
        "Day 7 Fire Weather",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day7otlk_fire.gif",
    ),
    (
        "day8_fire",
        "Day 8 Fire Weather",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day8otlk_fire.gif",
    ),
    (
        "day3_fire_probability",
        "Day 3 Fire Probability",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day3fireprob.gif",
    ),
    (
        "day4_fire_probability",
        "Day 4 Fire Probability",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day4fireprob.gif",
    ),
    (
        "day5_fire_probability",
        "Day 5 Fire Probability",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day5fireprob.gif",
    ),
    (
        "day6_fire_probability",
        "Day 6 Fire Probability",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day6fireprob.gif",
    ),
    (
        "day7_fire_probability",
        "Day 7 Fire Probability",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day7fireprob.gif",
    ),
    (
        "day8_fire_probability",
        "Day 8 Fire Probability",
        f"{SPC_FIRE_DAY38_BASE}/",
        f"{SPC_FIRE_DAY38_BASE}/imgs/day8fireprob.gif",
    ),
)

EXCESSIVE_RAINFALL_IMAGES = (
    (
        "day1_ero",
        "Day 1 Excessive Rainfall",
        f"{WPC_ERO_BASE}/ero.php?day=1&opt=curr",
        f"{WPC_ERO_BASE}/94ewbg.gif",
    ),
    (
        "day2_ero",
        "Day 2 Excessive Rainfall",
        f"{WPC_ERO_BASE}/ero.php?day=2&opt=curr",
        f"{WPC_ERO_BASE}/98ewbg.gif",
    ),
    (
        "day3_ero",
        "Day 3 Excessive Rainfall",
        f"{WPC_ERO_BASE}/ero.php?day=3&opt=curr",
        f"{WPC_ERO_BASE}/99ewbg.gif",
    ),
    (
        "day4_ero",
        "Day 4 Excessive Rainfall",
        f"{WPC_ERO_BASE}/ero.php?day=4&opt=curr",
        f"{WPC_ERO_BASE}/ero_d45/images/d4wbg.gif",
    ),
    (
        "day5_ero",
        "Day 5 Excessive Rainfall",
        f"{WPC_ERO_BASE}/ero.php?day=5&opt=curr",
        f"{WPC_ERO_BASE}/ero_d45/images/d5wbg.gif",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "data" / "latest-plots"),
        help="Directory for index.html, PNG assets, and metadata.",
    )
    parser.add_argument(
        "--custom-source",
        choices=("geojson-first", "geojson-only", "pts-only"),
        default="pts-only",
        help="Geometry source used for the proof render.",
    )
    parser.add_argument(
        "--regional-maps",
        default=bot.DEFAULT_REGIONAL_MAPS,
        help="Comma-separated maps that get regional cut-ins; use none or all.",
    )
    parser.add_argument(
        "--regional-min-risk-level",
        choices=("tstm", "mrgl", "slgt", "enh", "mdt", "high"),
        default=bot.DEFAULT_REGIONAL_MIN_RISK_LEVEL,
        help="Minimum categorical risk used for regional centers.",
    )
    parser.add_argument(
        "--regional-max-areas",
        type=int,
        default=bot.DEFAULT_REGIONAL_MAX_AREAS,
        help="Maximum regional cut-ins per enabled map.",
    )
    parser.add_argument("--keep-existing", action="store_true", help="Do not clear output-dir before writing.")
    return parser.parse_args()


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def label_name(label: str) -> str:
    names = {
        "day4-8": "Day 4-8 Composite",
        "categorical": "Categorical",
        "probabilistic": "Probabilistic",
        "tornado": "Tornado",
        "wind": "Wind",
        "hail": "Hail",
    }
    if "_regional_" in label:
        base, index = label.split("_regional_", 1)
        return f"{label_name(base)} - Regional {index}"
    if label in names:
        return names[label]
    if label.startswith("day") and label[3:].isdigit():
        return f"Day {label[3:]}"
    return label.replace("_", " ").title()


def render_latest_snapshots(args: argparse.Namespace) -> list[bot.BundleSnapshot]:
    snapshots: list[bot.BundleSnapshot] = []
    for spec in bot.BUNDLES:
        product = bot.choose_custom_product(spec, None, args.custom_source)
        snapshots.append(
            bot.render_product_bundle(
                product,
                regional_maps=args.regional_maps,
                regional_min_risk_level=args.regional_min_risk_level,
                regional_max_areas=args.regional_max_areas,
            )
        )
    return snapshots


def write_assets(snapshots: list[bot.BundleSnapshot], output_dir: Path) -> list[dict[str, object]]:
    bundles: list[dict[str, object]] = []
    assets_dir = output_dir / "assets"
    for snapshot in snapshots:
        bundle_dir = assets_dir / snapshot.spec.key
        bundle_dir.mkdir(parents=True, exist_ok=True)
        images = []
        for image in snapshot.images:
            image_path = bundle_dir / image.filename
            image_path.write_bytes(image.data)
            images.append(
                {
                    "label": image.label,
                    "name": label_name(image.label),
                    "filename": image.filename,
                    "path": image_path.relative_to(output_dir).as_posix(),
                    "url": image.url,
                    "source_page": snapshot.page_url,
                    "source_name": "Official SPC product",
                    "sha256": image.sha256,
                    "bytes": len(image.data),
                }
            )
        metadata = {
            "key": snapshot.spec.key,
            "name": snapshot.spec.name,
            "title": snapshot.title,
            "updated": snapshot.updated,
            "product_id": snapshot.product_id,
            "page_url": snapshot.page_url,
            "issued": snapshot.issued,
            "valid": snapshot.valid,
            "risk_labels": list(snapshot.risk_labels),
            "images": images,
        }
        (bundle_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        bundles.append(metadata)
    return bundles


def fetch_page_metadata(page_url: str, fallback_title: str) -> dict[str, str]:
    try:
        text = bot.fetch_text_with_retries(
            page_url,
            attempts=2,
            delay=1.0,
            timeout=20,
            context=f"metadata fetch {page_url}",
        )
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, bot.BotError) as exc:
        return {"title": fallback_title, "updated": "", "warning": str(exc)}
    return {
        "title": bot.extract_title(text, fallback_title),
        "updated": bot.extract_updated(text),
        "warning": "",
    }


def write_external_bundle(
    *,
    output_dir: Path,
    key: str,
    name: str,
    product_id: str,
    page_url: str,
    risk_labels: tuple[str, ...],
    source_name: str,
    images: tuple[tuple[str, str, str, str], ...],
) -> dict[str, object]:
    bundle_dir = output_dir / "assets" / key
    bundle_dir.mkdir(parents=True, exist_ok=True)
    page_metadata: dict[str, dict[str, str]] = {}
    image_metadata: list[dict[str, object]] = []
    warnings: list[str] = []

    for label, image_name, source_page, image_url in images:
        page_metadata.setdefault(source_page, fetch_page_metadata(source_page, image_name))
        try:
            image = bot.download_image(key, label, image_url, timeout=30)
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, bot.BotError) as exc:
            warnings.append(f"{image_name}: {exc}")
            continue
        image_path = bundle_dir / image.filename
        image_path.write_bytes(image.data)
        image_metadata.append(
            {
                "label": label,
                "name": image_name,
                "filename": image.filename,
                "path": image_path.relative_to(output_dir).as_posix(),
                "url": image.url,
                "source_page": source_page,
                "source_name": source_name,
                "sha256": image.sha256,
                "bytes": len(image.data),
            }
        )

    if not image_metadata:
        raise bot.BotError(f"{name} did not produce any downloadable images")

    updated_values = []
    for metadata in page_metadata.values():
        if metadata.get("updated"):
            updated_values.append(metadata["updated"])
        if metadata.get("warning"):
            warnings.append(metadata["warning"])
    updated = " | ".join(dict.fromkeys(updated_values))
    title = " | ".join(dict.fromkeys(meta["title"] for meta in page_metadata.values() if meta.get("title"))) or name

    metadata = {
        "key": key,
        "name": name,
        "title": title,
        "updated": updated,
        "product_id": product_id,
        "page_url": page_url,
        "issued": updated,
        "valid": "",
        "risk_labels": list(risk_labels),
        "images": image_metadata,
        "warnings": warnings,
    }
    (bundle_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def write_adjacent_official_assets(output_dir: Path) -> list[dict[str, object]]:
    return [
        write_external_bundle(
            output_dir=output_dir,
            key="spc-fire-weather",
            name="SPC Fire Weather Outlooks",
            product_id="official-spc-fire-weather",
            page_url=f"{SPC_FIRE_BASE}/",
            risk_labels=("Elevated", "Critical", "Extreme", "Dry Thunderstorms", "Fire Probability"),
            source_name="Official SPC fire product",
            images=FIRE_WEATHER_IMAGES,
        ),
        write_external_bundle(
            output_dir=output_dir,
            key="wpc-excessive-rainfall",
            name="WPC Excessive Rainfall Outlooks",
            product_id="official-wpc-ero",
            page_url=f"{WPC_ERO_BASE}/excessive_rainfall_outlook_ero.php",
            risk_labels=("Marginal", "Slight", "Moderate", "High"),
            source_name="Official WPC ERO product",
            images=EXCESSIVE_RAINFALL_IMAGES,
        ),
    ]


def build_html(
    *,
    bundles: list[dict[str, object]],
    generated_at: str,
    args: argparse.Namespace,
) -> str:
    image_count = sum(len(bundle["images"]) for bundle in bundles)
    sections = []
    for index, bundle in enumerate(bundles, start=1):
        images_html = []
        for image in bundle["images"]:
            source_page = image.get("source_page") or bundle["page_url"]
            source_name = image.get("source_name") or "Official product"
            images_html.append(
                f"""
        <figure>
          <figcaption>
            <span class="label">{esc(image["name"])}</span>
            <span class="bytes">{int(image["bytes"]):,} bytes</span>
          </figcaption>
          <a href="{esc(image["path"])}" target="_blank" rel="noopener">
            <img src="{esc(image["path"])}" alt="{esc(bundle["name"])} {esc(image["name"])} latest outlook map" width="1630" height="1110">
          </a>
          <div class="source-link"><a href="{esc(source_page)}" target="_blank" rel="noopener">{esc(source_name)}</a></div>
          <details><summary>File proof</summary><code>{esc(image["filename"])} | sha256 {esc(image["sha256"])}</code></details>
        </figure>"""
            )
        risk_labels = ", ".join(bundle["risk_labels"]) if bundle["risk_labels"] else "none"
        sections.append(
            f"""
    <section class="bundle" id="{esc(bundle["key"])}">
      <div class="bundle-head">
        <div>
          <p class="eyebrow">Bundle {index}</p>
          <h2>{esc(bundle["name"])}</h2>
        </div>
        <div class="meta">
          <div>{esc(bundle["product_id"])}</div>
          <div>Issued: {esc(bundle["issued"] or "unknown")}</div>
          <div>Valid: {esc(bundle["valid"] or "unknown")}</div>
          <div>Risks: {esc(risk_labels)}</div>
          <a href="{esc(bundle["page_url"])}" target="_blank" rel="noopener">Official source</a>
        </div>
      </div>
      <div class="maps">
{''.join(images_html)}
      </div>
    </section>"""
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Latest Outlook Plots</title>
  <style>
    :root {{ color-scheme: light; --ink: #16202c; --muted: #5c6877; --line: #cfd6df; --panel: #f3f6f8; --surface: #ffffff; --accent: #0b5cad; --warn: #b00020; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #e7ecf2; color: var(--ink); font-family: Arial, Helvetica, sans-serif; line-height: 1.35; }}
    header {{ position: sticky; top: 0; z-index: 10; border-bottom: 1px solid var(--line); background: rgba(255, 255, 255, 0.96); backdrop-filter: blur(8px); }}
    .bar {{ max-width: 1580px; margin: 0 auto; padding: 14px 18px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    .status {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; color: var(--muted); font-size: 13px; }}
    .chip {{ border: 1px solid var(--line); background: var(--panel); padding: 4px 8px; border-radius: 4px; white-space: nowrap; }}
    main {{ max-width: 1580px; margin: 0 auto; padding: 18px 18px 48px; }}
    .notice {{ margin: 0 0 18px; padding: 12px 14px; border-left: 4px solid var(--warn); background: #fff; }}
    .notice strong {{ color: var(--warn); }}
    .bundle {{ margin: 0 0 28px; border: 1px solid var(--line); background: var(--surface); }}
    .bundle-head {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: var(--panel); display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }}
    .eyebrow {{ margin: 0 0 4px; color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.04em; }}
    h2 {{ margin: 0; font-size: 18px; }}
    .meta {{ color: var(--muted); font-size: 13px; text-align: right; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .maps {{ padding: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 610px), 1fr)); gap: 16px; }}
    figure {{ margin: 0; border: 1px solid var(--line); background: #fff; }}
    figcaption {{ min-height: 42px; border-bottom: 1px solid var(--line); padding: 9px 11px; display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; }}
    .label {{ font-weight: 700; text-transform: uppercase; }}
    .bytes {{ color: var(--muted); white-space: nowrap; }}
    img {{ display: block; width: 100%; height: auto; background: #f0f3f7; }}
    details {{ border-top: 1px solid var(--line); padding: 8px 11px 10px; color: var(--muted); font-size: 12px; }}
    .source-link {{ border-top: 1px solid var(--line); padding: 7px 11px; font-size: 12px; }}
    summary {{ cursor: pointer; color: var(--ink); font-weight: 700; }}
    code {{ font-family: Consolas, "Liberation Mono", monospace; overflow-wrap: anywhere; }}
    @media (max-width: 760px) {{ .bar, .bundle-head, figcaption {{ flex-direction: column; align-items: flex-start; }} .status, .meta {{ justify-content: flex-start; text-align: left; }} main {{ padding-left: 10px; padding-right: 10px; }} }}
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>Latest Outlook Plots</h1>
      <div class="status" aria-label="latest plot summary">
        <span class="chip">Generated {esc(generated_at)}</span>
        <span class="chip">{len(bundles)} bundles</span>
        <span class="chip">{image_count} maps</span>
        <span class="chip">Source: {esc(args.custom_source)}</span>
        <span class="chip">Regional: {esc(args.regional_maps)}</span>
      </div>
    </div>
  </header>
  <main>
    <p class="notice"><strong>Unofficial fast render.</strong> Convective sections are the latest plots this bot renders from official NOAA/NWS Storm Prediction Center geometry products. Fire-weather and excessive-rainfall sections are official SPC/WPC image products included for coverage until custom renderers are added. No NOAA/NWS/SPC logos or emblems are added by this project. Always verify with the linked official source.</p>
{''.join(sections)}
  </main>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    if output_dir.exists() and not args.keep_existing:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    snapshots = render_latest_snapshots(args)
    bundles = write_assets(snapshots, output_dir)
    bundles.extend(write_adjacent_official_assets(output_dir))
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "custom_source": args.custom_source,
                "regional_maps": args.regional_maps,
                "regional_min_risk_level": args.regional_min_risk_level,
                "regional_max_areas": args.regional_max_areas,
                "bundles": bundles,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "index.html").write_text(
        build_html(bundles=bundles, generated_at=generated_at, args=args),
        encoding="utf-8",
    )

    print(f"Wrote {output_dir / 'index.html'}")
    print(f"Rendered {sum(len(bundle['images']) for bundle in bundles)} map(s) across {len(bundles)} bundle(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
