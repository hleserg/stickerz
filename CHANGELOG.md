# Changelog

Managed by Commitizen (`cz bump`).

## v0.3.0 (2026-06-07)

### Feat

- Sticker Service MVP — Telegram photo→sticker-pack bot (#2)
- Sticker Service MVP — Telegram photo→sticker-pack bot
- **styles**: add Disney and Pixar style plugins
- **styles**: fixate debugged pipeline + soft_anime style + gaze pre-check
- resilient generation (retry-any) + child-safety reformulation + resume
- **start**: nudge /new in the welcome message
- **models**: retry vision (gate/emoji) calls on transient overload
- **models**: retry + model fallback on Gemini overload (503)
- **bot**: set About + Description on startup
- live progress, server logs, error surfacing during generation
- **bot**: register the command menu via setMyCommands on startup
- **postprocess**: grid-cut fallback when chroma slicing under-performs
- **models**: live Gemini implementation (image gen + vision gate/emoji)
- **flow**: /mychars (reuse character) and /addto (extend pack) entries
- **flow**: FSM pack-building flow + full bot wiring
- **orchestrator**: end-to-end canonical->sheet->slice->emoji->publish
- **whatsapp**: optional WebP 512x512 <=100KB export
- **access**: whitelist middleware + admin allow/deny commands
- **publish**: Telegram set publication with hidden id + occupied retry
- **stickers**: emoji assignment via Vision with 🙂 fallback
- **stickers**: one-call sheet generation with chroma bg + refusal retries
- **postprocess**: chroma-key slicing to 512 stickers (§7, §B.4)
- **canonical**: data-driven pipeline engine + geometry gate (CORE)
- **models**: image-model interface with mock + Gemini/GPT adapters
- **styles**: data-driven style engine (schema + loader + watercolor)
- **db**: typed aiosqlite repository for characters/packs/orders
- **bot**: aiogram skeleton with /start, dispatcher factory, compose

### Fix

- **watercolor**: gaze-to-camera step 3 + identity-only sheet suffix
- **watercolor**: re-anchor identity to the photo to stop drift
- **docker**: install the 'models' extra so google-genai/openai ship
- **docker**: writable data dir via named volume owned by appuser
- **config**: resolve styles_dir from the package, data_dir from CWD
