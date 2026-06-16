#!/usr/bin/env python3
"""Render the latest custom outlook plots and build a local HTML proof page."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402


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
            images_html.append(
                f"""
        <figure>
          <figcaption>
            <span class="label">{esc(image["name"])}</span>
            <span class="bytes">{int(image["bytes"]):,} bytes</span>
          </figcaption>
          <a href="{esc(image["path"])}" target="_blank" rel="noopener">
            <img src="{esc(image["path"])}" alt="{esc(bundle["name"])} {esc(image["name"])} latest unofficial map" width="1630" height="1110">
          </a>
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
          <a href="{esc(bundle["page_url"])}" target="_blank" rel="noopener">Official SPC product</a>
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
    <p class="notice"><strong>Unofficial fast render.</strong> These are the latest plots this bot currently renders from official NOAA/NWS Storm Prediction Center geometry products. No NOAA/NWS/SPC logos or emblems are used. Always verify with the linked official SPC product.</p>
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
