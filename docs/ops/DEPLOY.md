# Deploying the bot on a VDS

The bot can't be published to Telegram from the Claude Code web environment
(its egress allowlist blocks `api.telegram.org`). Run it on your own server,
where Telegram is reachable. Docker Compose brings up the bot + Redis.

## Prerequisites

- A VDS with Docker + Docker Compose.
- The three secrets: `BOT_TOKEN`, `GEMINI_KEY` (and optionally `GPT_KEY`).
- Your Telegram numeric `user_id` (for the whitelist/admin). Get it from
  [@userinfobot](https://t.me/userinfobot).
- If the VDS is in RU and can't reach Gemini directly, an outbound proxy URL.

## One-time setup

```bash
git clone https://github.com/hleserg/stickerz.git
cd stickerz
git checkout claude/ultracode-mode-setup-2VAa4   # until merged to main

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
3. Send `/new` → confirm consent → photo → name → adult/child(+age) → style →
   confirm the canonical → the pack is generated and published; you get a
   `t.me/addstickers/...` link.
4. `/mychars` — new pack about a saved character. `/addto` — extend a pack.

## Notes

- `gemini-3-pro-image` occasionally returns `503 high demand`; retry.
- Generated data (sqlite, photos, sheets, stickers) lives in `./data`
  (mounted volume). Back it up if you care about saved characters.
- To update: `git pull`, then `docker compose up -d --build`.
