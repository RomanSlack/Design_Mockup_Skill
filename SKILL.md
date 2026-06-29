---
name: imagegen
description: Generate or edit images by calling a local CLI that fronts OpenAI (gpt-image-2 / gpt-image-1.5) and Google Gemini (gemini-3-pro-image / "Nano Banana Pro"). Use when you need to actually PRODUCE image files — generate, create, render, or batch-produce mockups, UI screens, icons, product shots, hero/marketing images, illustrations; edit/recolor an existing image; make a transparent-background icon; or generate many on-brand images at once with monitoring. This is the tool to call when the user wants images made (not just prompts authored — for prompt-writing use image-prompts).
---

# imagegen

A single CLI that produces image files from prompts, fronting two engines. You call it from the shell, read **one JSON object from stdout**, and use the returned file paths. It auto-saves every prompt (EXIF + sidecar JSON) so nothing is ever lost.

**Engine:** `python3 /home/roman/Design_Mockup_Skill/imagegen.py`
(It loads its API keys from its own `.env`, so it works from any working directory.)

## When to invoke

- "Generate / make / create / render an image of …"
- "Make me icons / mockups / product shots / a hero image / illustrations."
- "Edit / recolor / restyle this image" (you have a source file).
- "I need a transparent-background icon."
- "Generate a batch of images" / many prompts at once.

## When NOT to invoke

- The user only wants prompt **text** authored for an external generator → use `image-prompts`.
- Pure local photo editing with no model generation (crop/resize) → use PIL/ImageMagick directly.

## How to call it

```
python3 /home/roman/Design_Mockup_Skill/imagegen.py "<prompt>" [flags]        # single
python3 /home/roman/Design_Mockup_Skill/imagegen.py batch <file> [flags]      # many, concurrent
```

stdout is ALWAYS one JSON object — parse it. Live progress goes to **stderr** (safe to show the user or ignore). Exit code is 0 on full success, 1 if anything failed.

Single result:
```json
{"ok": true, "provider": "gemini", "model": "gemini-3-pro-image", "images": ["outputs/..._0.webp"], "cost_usd": 0.134, "errors": []}
```

## Picking the engine (`--provider`, default `auto`)

| Want | Use | Why |
|------|-----|-----|
| photoreal scenes, hero/marketing shots, 4K, exact aspect ratios, multi-image consistency | **gemini** | `gemini-3-pro-image`, up to 14 reference images, native aspect ratios, `.webp` |
| fixed pixel dimensions, edits/recolors | **openai** | `gpt-image-2`, exact `WxH` sizes, `.png` |
| **transparent background** | **openai + `--transparent`** | auto-routes to `gpt-image-1.5` (gpt-image-2 can't do transparency) |

`auto` routes to **openai** when you pass `--transparent`, a pixel `--size` (e.g. `1024x1024`), or `--edit`; otherwise **gemini**. When unsure, omit `--provider` and let auto decide, or pick gemini for "make it look good" scenes and openai for icons/edits.

## Flags

| flag | engine | values |
|------|--------|--------|
| `--provider` | both | `auto` (default) / `openai` / `gemini` |
| `--size` | both | openai: `1024x1024`, `1536x1024`, `1024x1536`, any `WxH` up to 4K, `auto` · gemini: `1K`, `2K`, `4K` |
| `--aspect` | gemini | `1:1 16:9 9:16 4:3 3:4 3:2 2:3 5:4 4:5 21:9` (default `16:9`) |
| `--quality` | openai | `low` `medium` `high` (default) `auto` |
| `--transparent` | openai | transparent background (→ gpt-image-1.5) |
| `--refs` | both | reference image paths for style/subject consistency |
| `--edit` | both | source image(s) to edit/recolor in place |
| `-n` | both | images per prompt (default 1) |
| `--out` | both | output dir (default `<skill>/outputs`) |
| `--estimate` | single/batch | dry run: print projected cost as JSON, generate nothing |
| `--workers` | batch | concurrent requests (default 4) |

## Recipes

**One good image (let auto pick gemini):**
```
python3 /home/roman/Design_Mockup_Skill/imagegen.py "a ceramic coffee cup by a window, soft morning light, photoreal"
```

**4K vertical hero with an anchor image for consistency:**
```
python3 /home/roman/Design_Mockup_Skill/imagegen.py "product hero on rippling water" \
  --provider gemini --size 4K --aspect 9:16 --refs anchor.webp
```

**Transparent app icon:**
```
python3 /home/roman/Design_Mockup_Skill/imagegen.py "white 3D clay rocket icon, isometric, matte" \
  --transparent --size 1024x1024
```

**Edit / recolor an existing file (auto → openai):**
```
python3 /home/roman/Design_Mockup_Skill/imagegen.py "recolor entirely purple, keep shape, white bg" \
  --edit outputs/rocket_0.png
```

## Batch mode (use this whenever there is more than one prompt)

Input file format is chosen by extension:
- **`.txt`** — one prompt per line; all share the CLI flags.
- **`.jsonl`** — one JSON object per line; each key overrides the CLI defaults per item.
- **`.json`** — a JSON array of those objects.

Per-item keys: `prompt` (required), `provider`, `size`, `aspect`, `quality`, `transparent`, `refs` (list), `edit` (list), `n`, `name`.

`prompts.jsonl`:
```json
{"prompt": "a river stone on white sand", "provider": "gemini", "size": "2K", "aspect": "1:1", "name": "stone"}
{"prompt": "a clay coffee mug on black bg", "provider": "openai", "size": "1024x1024", "name": "mug"}
```
```
python3 /home/roman/Design_Mockup_Skill/imagegen.py batch prompts.jsonl --workers 4
```

Monitoring streams to stderr as each finishes:
```
batch: 2 prompts, 4 workers -> .../outputs
  [1/2] ok  openai 'a clay coffee mug on black bg' -> outputs/mug_0.png
  [2/2] ok  gemini 'a river stone on white sand'   -> outputs/stone_0.webp
batch done: 2/2 ok, manifest -> outputs/batch_20260629_135021.json
```

Batch stdout (one object):
```json
{"ok": true, "count": 2, "succeeded": 2, "failed": 0, "total_cost_usd": 0.173,
 "images": ["outputs/stone_0.webp", "outputs/mug_0.png"],
 "results": [{"ok": true, "provider": "...", "prompt": "...", "images": [...], "cost_usd": 0.134, "error": null}, ...],
 "manifest": "outputs/batch_20260629_135021.json", "errors": []}
```
A failed item never aborts the run — it appears in `results` with `ok:false` + its `error`; run-level `ok` is true only if every item succeeded. To retry failures, build a new file from the `results` where `ok` is false.

## Cost: estimate before, track after

Image generation costs real money per image, so the tool makes spend visible at every step.

**Estimate before spending** — `--estimate` is a dry run that generates nothing and prints the projected cost. Always do this before a large or high-quality batch, and surface the number to the user:
```bash
python3 /home/roman/Design_Mockup_Skill/imagegen.py "..." --provider gemini --size 4K -n 8 --estimate
# -> {"ok": true, "estimate": true, "model": "gemini-3-pro-image", "n": 8, "per_image_usd": 0.24, "cost_usd": 1.92, ...}
python3 /home/roman/Design_Mockup_Skill/imagegen.py batch prompts.jsonl --estimate
# -> {"ok": true, "estimate": true, "count": 12, "cost_usd": 1.61, "items": [...]}
```

**Cost is reported on every real run** — single results carry `cost_usd`; batch results carry per-item `cost_usd` plus a `total_cost_usd`. The batch monitor also prints `$x.xxx` per item on stderr.

**Spend is tracked** — every generation appends to `outputs/cost_ledger.jsonl`. Check running totals any time:
```bash
python3 /home/roman/Design_Mockup_Skill/imagegen.py cost
# -> {"total_usd": ..., "today_usd": ..., "images": ..., "runs": ..., "by_model": {...}}
```

Rough per-image guide (standard pricing; estimates): gemini 1K/2K ≈ **$0.13**, gemini 4K ≈ **$0.24**; gpt-image-2 1024² ≈ **$0.006 / $0.05 / $0.21** at low/medium/high; gpt-image-1.5 1024² high (transparent) ≈ **$0.13**. The exact table lives in `PRICING` in `imagegen.py`. These are estimates (providers bill by token); reference-image input cost is excluded.

## Consistency across a set (the anchor workflow)

For a cohesive series: generate ONE anchor image first, pick the best, then pass it via `--refs` (or per-item `"refs"`) to every other prompt so palette/lighting/style carry over. This pairs with the `image-prompts` skill, which authors the anchor + follow-on prompt text.

## Provenance — prompts are auto-saved, always

Every image embeds its prompt in **EXIF** (`ImageDescription`/`UserComment`) and writes a sidecar
`<name>.json` (prompt, model, provider, size/aspect, refs, timestamp). Each batch also writes
`outputs/batch_<timestamp>.json` with every item's full spec + result. To recover how any image was
made, read its `.json` or EXIF.

## Operational notes for the agent

- **Always parse stdout as JSON**; use `images[*]` paths. Don't scrape stderr.
- **Check `ok`.** On failure, the human-readable reason is in `errors[*]` / per-item `error`.
- **More than one image → use `batch`** (concurrent + monitored) rather than looping single calls.
- **Costs real money/quota** per image. Don't silently generate large `-n` or huge batches — run `--estimate` first, show the user the projected `cost_usd`, and prefer `--quality low` / `--size 1K` while iterating. Use the `cost` subcommand to report spend so far.
- Setup, model-ID audit details, and the optional async Batch API path live in `AGENTS.md` / `README.md` in the skill directory.
