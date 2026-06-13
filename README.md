# SPC Outlook Discord Bot

Post official Storm Prediction Center outlook maps to Discord as soon as new outlooks appear.

This bot is intentionally conservative: it does not redraw polygons, render custom overlays, or transform SPC GIS data into new maps. It downloads the finished official SPC outlook plot images and posts those images as bundled Discord webhook messages.

## What It Posts

Each new issue becomes four image-only Discord messages:

| Message | Maps attached |
| --- | --- |
| Day 1 | categorical, tornado, wind, hail |
| Day 2 | categorical, tornado, wind, hail |
| Day 3 | categorical, probabilistic |
| Day 4-8 | combined Day 4-8, Day 4, Day 5, Day 6, Day 7, Day 8 |

Proof bundle: [docs/proof](docs/proof/index.html)

## How It Gets Updates Fast

Fast path:

- Listen to a local `nwws-rs` stream for `KWNS` outlook products: `PTS*` and `SWO*`.
- When NWWS fires, immediately fetch the live SPC page and retry for the official images in case the text product arrives a few seconds before the web images.

Fallback path:

- Poll the SPC outlook pages directly, defaulting to every 20 seconds.
- Dedupe by product id, issue/update time, and image hashes so restarts do not spam old outlooks.

## Speed Expectations

With NWWS enabled, speed should be comparable to other serious NWWS/NOAAPORT-triggered weather bots:

1. SPC issues the outlook text product.
2. `nwws-rs` receives the `KWNS` `PTS*` or `SWO*` product and sends an SSE event locally.
3. This bot immediately fetches the matching SPC outlook page.
4. If the official image files are not live yet, the bot retries every `SPC_FETCH_RETRY_SECONDS` seconds, up to `SPC_TRIGGER_FETCH_ATTEMPTS` attempts.
5. As soon as the official SPC images are available, the bot posts the bundle.

Default trigger-mode timing is usually bounded by official SPC image availability plus a retry interval, not by the 20-second fallback poll. With no NWWS stream, detection is bounded by `SPC_POLL_SECONDS`.

The bot intentionally does not try to beat the official SPC plot images by rendering its own polygons. A custom polygon renderer may post earlier, but that is a different product. This bot optimizes for getting the official plots out quickly.

## Quick Start: Bare Python

Requires Python 3.11+.

```powershell
git clone https://github.com/FahrenheitResearch/spc-outlook-discord-bot.git
cd spc-outlook-discord-bot
copy .env.example .env
notepad .env
```

Set:

```text
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
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

The container uses polling unless you point `NWWS_SSE_URLS` at an `nwws-rs` service reachable from the container.

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

`PTS` catches outline/map geometry products such as `PTSDY1`, `PTSDY2`, `PTSDY3`, and `PTSD48`. `SWO` catches the discussion products. Either can trigger a map refresh.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook destination. Required unless dry-running. |
| `SPC_MESSAGE_CONTENT` | `none` | `none`, `short`, or `debug`. `none` posts image-only messages. |
| `SPC_POLL_SECONDS` | `20` | Direct SPC fallback poll cadence. |
| `SPC_FETCH_ATTEMPTS` | `4` | Normal fetch retry count. |
| `SPC_TRIGGER_FETCH_ATTEMPTS` | `12` | Retry count after an NWWS trigger, to bridge image lag. |
| `SPC_FETCH_RETRY_SECONDS` | `5` | Delay between fetch retries. |
| `SPC_PRIME_CURRENT_ON_START` | `1` | Mark current outlooks seen on startup so old maps are not posted. |
| `SPC_POST_CURRENT_ON_START` | `0` | Post current bundles immediately on startup. |
| `SPC_STATE_FILE` | `data/state.json` | Dedupe state file. |
| `SPC_DRY_RUN_DIR` | `data/dry-run` | Dry-run image output directory. |
| `NWWS_AUTOSTART` | `0` | Start `nwws serve` from this process. |
| `NWWS_USERNAME` / `NWWS_PASSWORD` | unset | Credentials used by `nwws-rs` when autostarting. |
| `NWWS_SSE_URLS` | local `PTS` and `SWO` streams | Comma-separated `nwws-rs` SSE endpoints. |

## Operational Notes

- Keep `.env` private. It contains your Discord webhook.
- The bot writes runtime files under `data/`, which is ignored by git.
- Discord webhooks currently allow enough attachments for each bundle; the largest bundle here is six images.
- If NWWS is down, the direct SPC polling fallback keeps running.
- If SPC changes its page structure, CI tests should catch the parser contract, and runtime logs will say which bundle failed.

## Why Official Plots Only

Forwarded weather bot posts often contain custom-rendered maps, simplified polygons, or private styling that can subtly change what people think the official outlook says. This project is built to reduce that ambiguity: the posted attachment is the SPC map image itself, fetched from the SPC outlook page.

That does not replace checking SPC/NWS directly for life-safety decisions. Treat this as fast redistribution of official graphics, not an alerting authority.

## Development

```bash
python -m unittest discover -s tests
python -m py_compile spc_outlook_bot.py
```

The test suite uses static SPC-like HTML fixtures and does not hit the network.
