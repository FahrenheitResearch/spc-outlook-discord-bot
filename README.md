# SPC Outlook Discord Bot

Post SPC outlook map bundles to Discord as soon as new outlook geometry is available.

By default, this bot runs in `custom-only` mode: it renders fast SPC-styled maps from official SPC PTS text products, posts them as four bundled Discord messages, and does not use NOAA/NWS logos. If you want the exact finished SPC web graphics instead, switch to `official-only`. If you want fast previews first and official graphics later, use `custom-first`.

Proof bundle: [docs/proof](docs/proof/index.html)

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
| `custom-only` | Default. Posts fast custom maps rendered from official SPC PTS text. Keeps the output to four bundled messages. |
| `custom-first` | Posts the fast PTS render immediately, then posts the official SPC image bundle when those files appear. |
| `official-only` | Posts only the exact official SPC PNG/GIF files from the SPC web pages. Slower, but no custom rendering. |
| `both` | Posts both products whenever a refresh runs. Mostly useful for testing. |

## Why It Is Fast

Fastest path:

1. `nwws-rs` receives a `KWNS` PTS product such as `PTSDY1`.
2. This bot reads the raw bulletin from the local SSE event.
3. The PTS polygons are parsed immediately.
4. The bot renders the map bundle locally and posts it to Discord.

That path does not wait for SPC's finished web PNG/GIF files. On the local proof run, all 16 current maps rendered in about 8 seconds total after fetching the current PTS text. A single triggered Day 1 or Day 2 bundle is smaller than that full proof run.

Official-image mode is bounded by SPC web image availability. In a June 2026 spot check, the official image files commonly appeared several minutes after the outlook text product, with the sampled average around 8.5 minutes. The exact delay varies by outlook cycle and by SPC web publishing timing.

Fallback path:

- If NWWS is unavailable, the bot polls the SPC outlook pages directly.
- Default polling cadence is 20 seconds.
- Dedupe uses product id, issue/update time, and image hashes so restarts do not spam old outlooks.

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
SPC_RENDER_MODE=custom-only
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

`PTS` is the fastest trigger because it carries the outlook geometry. `SWO` discussion products can still trigger a refresh, but the bot may need to fetch the matching PTS text from SPC if the raw geometry was not in the event.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook destination. Required unless dry-running. |
| `SPC_RENDER_MODE` | `custom-only` | `custom-only`, `custom-first`, `official-only`, or `both`. |
| `SPC_MESSAGE_CONTENT` | `none` | `none`, `short`, or `debug`. `none` posts image-only messages. |
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
- Discord webhooks currently allow enough attachments for each bundle; the largest bundle here is six images.
- The first custom render may download/cache Cartopy Natural Earth map files. The default startup prime helps warm that before a live post.
- If NWWS is down, direct SPC polling keeps running.
- If SPC changes its page or PTS structure, CI tests cover the parser contract, and runtime logs will say which bundle failed.

## Public-Safety Boundary

The fast maps are generated from official SPC PTS text products, but they are not official NOAA/NWS/SPC graphics. They are labeled as unofficial fast renders and intentionally omit NOAA/NWS logos. For life-safety decisions, check SPC/NWS directly.

Use `official-only` when your priority is exact SPC web graphics. Use `custom-only` when your priority is getting the official outlook geometry into Discord as quickly as practical.

## Development

```bash
python -m unittest discover -s tests
python -m py_compile spc_outlook_bot.py
```

The unit tests use static SPC-like fixtures and do not hit the network.
