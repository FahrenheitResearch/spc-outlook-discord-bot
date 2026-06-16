# Fast Severe Outlook Discord Bot

Post fast severe-weather outlook map bundles to Discord as soon as new NOAA/NWS Storm Prediction Center geometry is available.

By default, this bot runs in `custom-only` mode with `geojson-only` geometry: it renders fast unofficial maps from official NOAA/NWS Storm Prediction Center GeoJSON polygons for Day 1-3, uses PTS geometry for Day 4-8 because SPC does not publish the same live GeoJSON source there, posts four bundled Discord messages, and does not use NOAA/NWS/SPC logos or emblems. If you want the exact finished SPC web graphics instead, switch to `official-only`. If you explicitly want the earliest raw-text geometry path for Day 1-3, switch to `geojson-first` or `pts-only`.

Proof bundle: [docs/proof](docs/proof/index.html)

The custom renderer also understands [SPC's 2026 Conditional Intensity Group (CIG) system](https://www.spc.noaa.gov/exper/conditional-intensity-information/). Hazard maps can show hatched CIG overlays from the official geometry products, with CIG1-CIG3 for tornado/wind-style products and CIG1-CIG2 for hail/Day 3 total severe where SPC defines only two intensity levels.

## What It Posts

Each full current-set run becomes four image-only Discord messages:

| Message | Maps attached |
| --- | --- |
| Day 1 | categorical, tornado, wind, hail |
| Day 2 | categorical, tornado, wind, hail |
| Day 3 | categorical, probabilistic |
| Day 4-8 | combined Day 4-8, Day 4, Day 5, Day 6, Day 7, Day 8 |

## Render Modes

| Mode | Behavior |
| --- | --- |
| `custom-only` | Default. Posts fast custom maps rendered from official NOAA/NWS SPC geometry products. Keeps the output to four bundled messages. |
| `custom-first` | Posts the fast PTS render immediately, then posts the official SPC image bundle when those files appear. |
| `official-only` | Posts only the exact official SPC PNG/GIF files from the SPC web pages. Slower, but no custom rendering. |
| `both` | Posts both products whenever a refresh runs. Mostly useful for testing. |

## Custom Geometry Source

| Source | Behavior |
| --- | --- |
| `geojson-only` | Default production mode. Uses official SPC GeoJSON for Day 1-3. Day 4-8 falls back to PTS because SPC does not publish the same live GeoJSON source for it. |
| `geojson-first` | Faster mode. Uses official SPC GeoJSON when it is current, but switches Day 1-3 to raw PTS when raw PTS has a newer issue time. This can beat GeoJSON publication while still preferring closed official GeoJSON when it is already caught up. |
| `pts-only` | Earliest raw-text geometry path. Uses the raw SPC Points Product and a hardened CONUS/marine-boundary polygonizer for open contours. Best for live testing or speed-first private servers. |

## Optional Risk Filtering

By default, every new bundle posts. For high-signal servers, set:

```text
SPC_MIN_RISK_LEVEL=enh
SPC_ALWAYS_POST_DAY48=1
```

That posts Day 1-3 custom bundles only when the categorical outlook reaches Enhanced or higher, while still posting any Day 4-8 outlook that contains a 15% or 30% area. Valid thresholds are `any`, `tstm`, `mrgl`, `slgt`, `enh`, `mdt`, and `high`.

## Why It Is Fast

Fastest path:

1. `nwws-rs` receives a `KWNS` outlook product such as `PTSDY1`.
2. The bot refreshes the matching SPC geometry product immediately.
3. For Day 1-3, default custom rendering waits for complete official SPC GeoJSON because it contains closed polygons. For Day 4-8, the bot uses PTS geometry.
4. The bot renders the map bundle locally and posts it to Discord.

That path does not wait for SPC's finished web PNG/GIF plot images. On the local proof run, all 16 current maps rendered in about 6-8 seconds total once the geometry files were reachable. A single triggered Day 1 or Day 2 bundle is smaller than that full proof run.

Official-image mode is bounded by SPC web image availability. In a June 2026 spot check, the official image files commonly appeared several minutes after the outlook text product, with the sampled average around 8.5 minutes. GeoJSON publication can still have a short SPC-side publish gap; `geojson-first` can reduce that gap by using newer raw PTS, and `pts-only` is the fastest raw-text path.

Fallback path:

- If NWWS is unavailable, the bot polls the SPC outlook pages directly.
- Default polling cadence is 20 seconds.
- Dedupe uses product id and issue/update time for custom preview maps so direct-vs-ZIP fetch paths and tiny PNG differences do not duplicate the same SPC outlook. Official-image mode also includes image hashes.

## Quick Start: Bare Python

Requires Python 3.11+.

```powershell
git clone https://github.com/FahrenheitResearch/spc-outlook-discord-bot.git
cd spc-outlook-discord-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Set:

```text
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_USERNAME=Fast Severe Outlook Bot
SPC_RENDER_MODE=custom-only
SPC_CUSTOM_SOURCE=geojson-only
```

Do a local proof run without posting:

```powershell
.\run_bot.ps1 -DryRun -Once -PostCurrent
```

Run continuously:

```powershell
.\run_bot.ps1
```

On Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
$EDITOR .env
./run_bot.sh
```

## Quick Start: Docker

```bash
cp .env.example .env
# edit DISCORD_WEBHOOK_URL in .env
docker compose up -d
```

The container uses polling unless `NWWS_SSE_URLS` points at an `nwws-rs` service reachable from inside the container.

## Production Linux Service

A hardened systemd unit template is in `deploy/spc-outlook-bot.service.example`. The expected layout is:

```text
/opt/spc-outlook-discord-bot
/etc/spc-outlook-bot.env
```

Recommended public-server settings:

```text
DISCORD_USERNAME=Yalllooks
SPC_RENDER_MODE=custom-only
SPC_CUSTOM_SOURCE=geojson-only
SPC_MIN_RISK_LEVEL=any
SPC_ALWAYS_POST_DAY48=1
SPC_PRIME_CURRENT_ON_START=1
SPC_POST_CURRENT_ON_START=0
```

Use `pts-only` for speed-first live testing. Use `geojson-only` for the most conservative public default. Keep the webhook URL in `/etc/spc-outlook-bot.env`, not in git.

## Fastest Mode With NWWS

Install `nwws-rs` and set your NWWS-OI credentials:

```powershell
cargo install nwws-rs --features serve
$env:NWWS_USERNAME = "your-nwws-username"
$env:NWWS_PASSWORD = "your-nwws-password"
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
.\run_bot.ps1 -AutostartNwws
```

The bot listens to:

```text
http://127.0.0.1:8080/v1/stream?office=KWNS&pil=PTS
http://127.0.0.1:8080/v1/stream?office=KWNS&pil=SWO
```

`PTS` is the fastest trigger because it announces the outlook geometry product. In the default `geojson-first` source, Day 1-3 posting uses matching SPC GeoJSON when it is current, but raw PTS wins if it has a newer issue time. In `link` message mode, the discussion card/button is populated from the matching raw `SWODY*` text feed, not by waiting for the SPC HTML page. Set `SPC_PREPOST_DISCUSSION=1` with `pts-only` to send that raw discussion card first, then send the rendered map bundle as a follow-up message.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook destination. Required unless dry-running. |
| `DISCORD_BOT_TOKEN` / `DISCORD_TOKEN` | unset | Alternative to a webhook: post through an existing Discord bot token. |
| `DISCORD_CHANNEL_ID` | unset | Discord channel for bot-token posting. |
| `DISCORD_USERNAME` | `Fast Severe Outlook Bot` | Display name used by the Discord webhook. Keep this unofficial unless you have agency permission. |
| `SPC_RENDER_MODE` | `custom-only` | `custom-only`, `custom-first`, `official-only`, or `both`. |
| `SPC_CUSTOM_SOURCE` | `geojson-first` | `geojson-first`, `geojson-only`, or `pts-only`. |
| `SPC_IMAGE_SAFE_SCALE` | `0.95` | Shrinks custom maps inside the PNG canvas so Discord attachment previews crop less. Use `1.0` to disable. |
| `SPC_MIN_RISK_LEVEL` | `any` | Optional Day 1-3 custom bundle filter. Use `enh` for Enhanced-or-higher posts only. |
| `SPC_ALWAYS_POST_DAY48` | `0` | With risk filtering enabled, still post any Day 4-8 outlook with a 15% or 30% area. |
| `SPC_REGIONAL_MAPS` | `categorical,day4-8` | Adds regional auto-zoom maps for selected custom maps. Use `none` to disable or `all` to zoom every rendered map. |
| `SPC_REGIONAL_MIN_RISK_LEVEL` | `enh` | Minimum categorical risk used to select Day 1-3 regional zoom centers. |
| `SPC_REGIONAL_MAX_AREAS` | `2` | Maximum regional zoom images per enabled map. If two ENH+ blobs are separated, the bot can post two regional maps. |
| `SPC_MESSAGE_CONTENT` | `none` | `none`, `link`, `short`, or `debug`. `link` adds a raw `SWODY*` discussion embed and `View Discussion` button above the images. |
| `SPC_PREPOST_DISCUSSION` | `0` | In `pts-only` + `link` mode, post the raw `SWODY*` discussion card before rendering maps, then post the image bundle as a follow-up. |
| `SPC_POLL_SECONDS` | `20` | Direct SPC fallback poll cadence. |
| `SPC_FETCH_ATTEMPTS` | `4` | Normal fetch retry count. |
| `SPC_TRIGGER_FETCH_ATTEMPTS` | `12` | Retry count after an NWWS trigger, mainly for official-image modes. |
| `SPC_FETCH_RETRY_SECONDS` | `5` | Delay between fetch retries. |
| `SPC_PRIME_CURRENT_ON_START` | `1` | Mark current outlooks seen on startup so old maps are not posted. Also warms renderer assets. |
| `SPC_POST_CURRENT_ON_START` | `0` | Post current bundles immediately on startup. |
| `SPC_STATE_FILE` | `data/state.json` | Dedupe state file. |
| `SPC_DRY_RUN_DIR` | `data/dry-run` | Dry-run image output directory. |
| `NWWS_AUTOSTART` | `0` | Start `nwws serve` from this process. |
| `NWWS_USERNAME` / `NWWS_PASSWORD` | unset | Credentials used by `nwws-rs` when autostarting. |
| `NWWS_SSE_URLS` | local `PTS` and `SWO` streams | Comma-separated `nwws-rs` SSE endpoints. |

## Operational Notes

- Keep `.env` private. It contains your Discord webhook.
- The bot writes runtime files under `data/`, which is ignored by git.
- Discord allows up to 10 file attachments per message. Larger custom bundles are split across multiple webhook messages automatically.
- The first custom render may download/cache Cartopy Natural Earth map files. The default startup prime helps warm that before a live post.
- If NWWS is down, direct SPC polling keeps running.
- If SPC changes its page or PTS structure, CI tests cover the parser contract, and runtime logs will say which bundle failed.

## Public-Safety Boundary

The fast maps are generated from official NOAA/NWS Storm Prediction Center geometry products, but they are not official NOAA/NWS/SPC graphics. They are labeled as unofficial fast renders and intentionally omit NOAA/NWS/SPC logos and emblems. The data source is attributed in text only. For life-safety decisions, check SPC/NWS directly.

Use `official-only` when your priority is exact SPC web graphics. Use `custom-only` when your priority is getting the official outlook geometry into Discord as quickly as practical.

## Development

```bash
python -m unittest discover -s tests
python -m py_compile spc_outlook_bot.py
```

The unit tests use static SPC-like fixtures and do not hit the network.

The raw PTS polygonizer is inspired by the Iowa Environmental Mesonet
[`pyIEM`](https://github.com/akrherz/pyIEM) SPC PTS parser. This repo vendors a
small CONUS/marine boundary file from pyIEM under `assets/` and keeps the
implementation dependency-light with Shapely.

### Local Archive Validation

To spot-check polygon rendering against official SPC archive graphics, build a local proof page:

```bash
python tools/validate_day1_archive.py --output-dir data/day1-polygon-smoke \
  --only-issue 20260305_1630 \
  --only-issue 20260310_1630 \
  --only-issue 20260425_1630 \
  --only-issue 20260613_1630
```

The validator filters Day 1 outlooks to ENH/MDT/HIGH products, fetches official archive images for local comparison, renders the bot's custom maps from archived SPC GeoJSON or shapefiles, and writes `index.html`, `summary.json`, and `manifest.json` under the output directory. Keep these outputs in `data/`; they are intentionally ignored by git.

### Latest Plot Page

To render every map the bot currently posts from the latest available SPC products:

```bash
python tools/build_latest_plots_page.py --output-dir data/latest-plots --custom-source pts-only
```

Open `data/latest-plots/index.html`, or serve that folder locally if your browser blocks `file://` images.
