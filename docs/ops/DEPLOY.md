# Deploying the bot on a VDS

The bot can't be published to Telegram from the Claude Code web environment
(its egress allowlist blocks `api.telegram.org`). Run it on your own server,
where Telegram is reachable. Docker Compose runs the bot (one container).

## Prerequisites

- A VDS with Docker + Docker Compose.
- The three secrets: `BOT_TOKEN`, `GEMINI_KEY` (and optionally `GPT_KEY`).
- Your Telegram numeric `user_id` (for the whitelist/admin). Get it from
  [@userinfobot](https://t.me/userinfobot).
- If the VDS is in RU and can't reach Gemini directly, an outbound proxy URL.

## One-time setup

```bash
git clone https://github.com/hleserg/stickerz.git
cd stickerz                       # main has everything; no branch checkout needed

cp .env.example .env
```

Edit `.env` (git-ignored) and fill in:

```dotenv
BOT_TOKEN=...                 # from @BotFather
GEMINI_KEY=...               # Gemini 3 Pro Image key
# GPT_KEY=...                # optional (GPT path)
APP_MODEL_PROVIDER=gemini
APP_ADMIN_IDS=<your_numeric_user_id>
# APP_MODELS_PROXY_URL=http://user:pass@host:port   # only if RU/blocked
```

## Run

```bash
docker compose up -d --build
docker compose logs -f bot        # watch "Starting long-polling as @<bot>"
```

## First use

1. Open `https://t.me/<your_bot_username>` and send `/start`.
2. As admin you're auto-whitelisted; allow others with `/allow <user_id>`.
   The bot starts in **debug** mode (admins only); switch to **alpha** with
   `/mode` (first admin only) to open applications + free generations.
3. Send `/new` → photo → name → adult/child(+age) → style → pick captions →
   preview the transparent stickers → **«Опубликовать в Telegram?»**; on confirm
   you get a `t.me/addstickers/...` link (or download the pack as a zip).
4. `/mychars` — new pack about a saved character. `/mypacks` — publish/download a
   saved pack. `/addto` — extend a published pack.

## Seeding `Kate_<style>` characters (optional)

To pre-generate one canonical per style from a photo and save them to an owner's
character list (default: the first admin), run the seeder **inside the container**
(it reuses the bot's env + DB). Needs live Gemini credits:

```bash
docker compose cp kate.jpg bot:/app/data/kate.jpg
docker compose exec bot python -m sticker_service.maintenance.seed_kate \
    --photo /app/data/kate.jpg            # --subject child --age 14 for a minor
```

Idempotent: existing `Kate_<style>` characters are skipped, so re-running fills gaps.

## Notes

- `gemini-3-pro-image` occasionally returns `503 high demand`; retry.
- A `429 … prepayment credits are depleted` means the **Gemini account is out of
  credits** — top up billing; the bot fails fast and tells users to retry later.
- Generated data (sqlite, photos, sheets, stickers) lives in the named volume
  `sticker-data` (owned by the container's appuser). Inspect/back up with
  `docker compose cp bot:/app/data ./data-backup`.
- Manual update (if not using auto-deploy below): `git pull`, then
  `docker compose pull bot && docker compose up -d bot`.

## Automatic deploy (soft, on every green `main`)

CI builds the image and pushes it to GHCR; the `Deploy` workflow then redeploys
the VDS over SSH **after CI passes** (never a red commit). The redeploy is
**soft**: `SIGTERM` makes the bot stop fetching new updates and finish the flow
already running (within `stop_grace_period: 150s`) before the new container
starts. Telegram queues incoming updates during the few-second swap, and the
persistent SQLite FSM means any flow that still couldn't finish resumes — so a
deploy doesn't cut users off mid-pack.

> Note: a single long-polling bot can't be *zero-gap* (Telegram allows one
> `getUpdates` consumer per token, so the old instance must stop before the new
> one starts). "Soft" here = no lost state, queued updates, in-flight flow drains.
> True zero-gap needs webhook mode + a reverse proxy (blue-green) — a later step.

### One-time setup

1. **GitHub → repo → Settings → Secrets and variables → Actions → New secret** —
   add four:
   - `VDS_HOST` — server IP/hostname
   - `VDS_USER` — SSH user (e.g. `deploy` or `root`)
   - `VDS_SSH_KEY` — that user's **private** SSH key (the matching public key is
     in the server's `~/.ssh/authorized_keys`)
   - `VDS_PATH` — absolute path to the repo on the server, e.g. `/opt/stickerz`
2. **On the VDS**, do the normal one-time setup once (clone + `.env` as above) at
   `VDS_PATH`, then let the image come from GHCR:
   ```bash
   # if the GHCR package is private, log in once (PAT with read:packages):
   echo <GHCR_PAT> | docker login ghcr.io -u hleserg --password-stdin
   # or make the package public (GitHub → Packages → stickerz → Package settings
   # → Change visibility → Public) and skip the login entirely.
   docker compose pull bot && docker compose up -d bot
   ```

That's it. From then on, every merge to `main` that passes CI auto-redeploys the
VDS. Roll back by pulling a specific tag: each build is also tagged with its
commit SHA (`ghcr.io/hleserg/stickerz:<sha>`).
