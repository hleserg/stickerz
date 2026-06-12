---
name: diagnose-generation
description: Diagnose why a sticker generation failed or produced defects (missing/duplicated captions, broken sheets). Use when the owner asks «почему не прошла генерация», «почему брак», or reports a failed/ugly pack.
---

# Diagnose a failed or defective generation

The single most reliable source is the production analytics DB; Sentry only
carries WARNING+ logs and may miss error events. Work newest-evidence-first.

## 1. Production timeline (authoritative)

```bash
ssh stickerz-vds "docker exec -i stickerz-bot-1 python - <<'PY'
import sqlite3
con = sqlite3.connect('/app/data/sticker_service.sqlite'); con.row_factory = sqlite3.Row
for r in con.execute(\"SELECT id, user_id, event, created_at, substr(detail,1,200) AS d \
FROM events WHERE event IN ('captions_selected','generation_done','generation_error', \
'caption_gate','scene_observer','slicing_fallback') ORDER BY id DESC LIMIT 20\"):
    print(r['id'], r['created_at'][5:19], r['user_id'], r['event'], r['d'])
PY"
```

- `generation_error.reason` is the truncated exception (`sheet slicing failed: …`,
  `caption check failed: …`, model errors).
- `captions_selected` holds the FULL ordered caption texts of the order —
  compare against what was actually drawn. The retry path does NOT re-log it;
  use the closest preceding row for the same user.
- `caption_gate` / `scene_observer` rows record what the vision checks saw.

## 2. The rejected sheet itself

Rejected sheets are dumped to `/app/data/rejected/{owner}_p{page}_{ts}.png`
(and also DM'd to the owner at failure time). Pull one:
`scp stickerz-vds:<path-in-volume> /tmp/` — the volume path is visible via
`docker inspect stickerz-bot-1 --format '{{json .Mounts}}'`.

## 3. Sentry (secondary)

Org `greyrock-labs`, project `yuki_stickers_bot`, region `https://de.sentry.io`.
Search the **logs** dataset for `message:"generation failed"` or
`sheet unusable` (warnings carry the gate reason 2 ms before the error).
Don't trust the errors dataset alone: oversized events were silently dropped
before 12.06 (fixed by the before_send trimmer), and INFO never reaches Sentry.

## 4. Failure-class cheat sheet

| reason starts with | class | code |
|---|---|---|
| `sheet slicing failed: count N != M` | model drew wrong tile count | `sheet_quality`, slice_stickers.py |
| `sheet slicing failed: scrap piece` | one tile is a fragment | same |
| `caption check failed: пропали/задублились` | text fidelity gate | caption_check.py |
| `503 / high demand / timeout` | model overload (ladder retries first) | models/gemini.py |
| refusal words | safety refusal (reformulations first) | models/base.py |

Stage labels of a healthy run: canonical → sheet → clean → slice → emoji → publish.
