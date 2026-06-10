# Capacity & concurrency readiness — 100–150 users/day

Status: **ready** (with the postprocess gate shipped alongside this report).
Measured on commit `19349dd`, June 2026, 4-vCPU container; VDS numbers below
are normalized to slower cores (×1.3–1.5).

## TL;DR — minimum VDS

| Profile | vCPU | RAM | Disk | Verdict |
|---|---|---|---|---|
| Absolute minimum | 2 | 3 GB (+2 GB swap) | 30 GB | works, no headroom |
| **Recommended** | **2** | **4 GB** | **40 GB NVMe** | comfortable for 100–150 DAU |
| Not enough | 1 | 2 GB | — | OOM risk during parallel packs |

Network: any (traffic is tiny); only the Gemini proxy matters for latency.

## Load model

150 users/day with a diurnal peak of ~3–4× the average rate gives a peak hour
of ~25–30 active users. A session is one generation (fresh packs 2–5 min —
3-step canonical + 4K sheet; reuse/extend 1–3 min — dominated by Gemini
network wait) plus browsing. Expected simultaneous generations at peak:
**3–6, bursts to ~10**. Telegram long-polling, FSM and SQLite traffic at this
scale are negligible (« 100 QPS).

## Measured numbers (this codebase, real pipeline)

| Operation | Time | Memory |
|---|---|---|
| App stack import (aiogram+numpy+PIL+charts) | 5.3 s once | RSS 134 MB |
| `process_sheet` on a 4K sheet (15 stickers) | 6.8 s CPU | py-peak 624 MB, RSS→863 MB |
| Watermark ×15 | 0.78 s (content-dependent, up to ~3 s on textured sets) | — |
| `compose_preview` | 0.13 s | — |
| `bundle_zip` | 0.01 s | — |
| **Total postprocess per pack** | **~7.8 s** | one gate slot |
| 3 × `process_sheet` in parallel (ungated) | wall 6.3 s | **RSS 2.2 GB** |

`chroma_key` already computes in float32; the ~0.6–0.9 GB per-sheet peak is the
honest cost of full-frame keying at 4096² (RGBA f32 256 MB + distance + labels).
On a typical VDS core multiply CPU times by ~1.3–1.5 (≈ 9–12 s per pack).

## The one real bottleneck — and its fix

Generation latency is network-bound (Gemini, 60–150 s) and costs ~zero CPU.
The only resource hazard is **memory stacking** when several users' sheets
finish at once: each keying holds ~0.7 GB, and `asyncio.to_thread`'s default
executor would happily run 8+ of them simultaneously → OOM on a small box.

**Fix (shipped):** a global `asyncio.Semaphore` in the orchestrator —
`APP_POSTPROCESS_CONCURRENCY` (default **2**) — gates `process_sheet` and the
watermark pass. Worst case at the gate: the 6th simultaneous pack waits
~2 × 7 s extra — invisible next to the 1–2 min generation itself. Covered by
`test_postprocess_gate_bounds_concurrent_sheet_keying`.

### RAM budget (gate = 2)

| Component | RSS |
|---|---|
| Bot process baseline (after first /stats chart) | ~250 MB |
| 2 × gated postprocess peaks | ~1.4–1.8 GB |
| In-flight photos/sheets/FSM for ~10 users | ~100 MB |
| OS + Docker | ~400 MB |
| **Total worst-case** | **~2.5 GB** → 3 GB floor, 4 GB comfort |

## Concurrency readiness checklist (verified in code)

- ✅ Parallel generations are independent: per-user FSM, per-pack file dirs,
  atomic SQLite credit ops (single conditional UPDATE), WAL + busy_timeout.
- ✅ Per-user single-flight guard (`_begin_action`) — no double-spend/dup packs.
- ✅ Charging strictly after success; failures are free (no refund path needed).
- ✅ Heavy CPU/file work off the event loop (`asyncio.to_thread`): keying,
  watermark, preview, zip, sticker IO, /stats chart. (The FSM json/base64
  codec runs on-loop; with byte payloads out of the FSM it is sub-millisecond.)
- ✅ Global postprocess gate (this change) bounds RAM/CPU stacking.
- ✅ Watchdogs: photo check 60 s, generation 600 s; model ladders + retries.
- ✅ Growth bounded: a daily maintenance loop (draft GC, events retention,
  FSM sweep, disk alert at `APP_DISK_ALERT_THRESHOLD_PCT`), docker log rotation.
- ✅ Refusal/quota/budget alerts reach admins.

## Disk forecast

Image ×2 (current+previous) ≈ 3 GB. Volume: every *generated* (not just
published) pack hits disk via the draft-save path; published packs are kept
forever (needed for /addto and downloads). A pack is **~2.2–4.5 MB typical**
(measured through `encode_sticker`; hard cap 512 KB × 15 ≈ 7.7 MB):

- **Full alpha burn** (150 DAU × 3 packs ≈ 450 packs/day): **1.2–2.2 GB/day**
  → 40 GB lasts only **2–4 weeks**. Plan 80 GB if sustained full-burn traffic
  materializes.
- **Realistic steady state** (30–60 packs/day once grants are spent):
  0.1–0.25 GB/day → 40 GB ≈ 4–10 months.

The daily maintenance loop (draft GC + events prune + FSM sweep) and the
disk alert at 80% keep growth visible and bounded; the alert is the signal
to either prune published packs or grow the volume.

## When to scale further

At >400–500 DAU or >10 sustained parallel generations: move postprocessing to
a worker process (the gate already isolates the call sites), raise the gate on
bigger RAM, and consider webhook mode instead of long polling. Nothing else
in the current design blocks horizontal growth.
