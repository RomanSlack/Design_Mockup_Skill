#!/usr/bin/env python3
"""
imagegen — one CLI in front of two image engines, built for an AI to drive.

Backends (verified against 2026 docs):
  openai  -> gpt-image-2       (default), gpt-image-1.5 (used for transparent bg)
  gemini  -> gemini-3-pro-image  ("Nano Banana Pro", GA)

Two modes:
  single   imagegen "a ceramic cup, soft morning light"
  batch    imagegen batch prompts.txt            # one prompt per line
           imagegen batch prompts.jsonl          # one JSON spec per line (per-item overrides)

Every image is saved with provenance (prompt in EXIF + a sidecar .json). Batch
runs also write a run-manifest. stdout is ALWAYS a single JSON object; live
progress goes to stderr so the stdout stays clean for parsing.

Single output:
  {"ok": true, "provider": "...", "model": "...", "images": ["outputs/..."], "errors": []}
Batch output:
  {"ok": true, "count": N, "succeeded": n, "failed": m,
   "images": [...all...], "results": [ {per-item} ... ], "manifest": "outputs/batch_*.json"}
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image as PILImage, ExifTags

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

OUTPUT_DIR = ROOT / "outputs"

OPENAI_MODEL = "gpt-image-2"
OPENAI_TRANSPARENT_MODEL = "gpt-image-1.5"   # gpt-image-2 can't do transparent bg
GEMINI_MODEL = "gemini-3.1-flash-image"      # Nano Banana 2 — newest, default
GEMINI_PRO_MODEL = "gemini-3-pro-image"      # Nano Banana Pro — highest fidelity

# --model aliases (gemini); full ids pass through unchanged.
GEMINI_ALIASES = {
    "2": GEMINI_MODEL, "nb2": GEMINI_MODEL, "flash": GEMINI_MODEL,
    "nano-banana-2": GEMINI_MODEL,
    "pro": GEMINI_PRO_MODEL, "nbpro": GEMINI_PRO_MODEL, "nano-banana-pro": GEMINI_PRO_MODEL,
}

OPENAI_DEFAULT_SIZE = "1024x1024"
GEMINI_DEFAULT_SIZE = "2K"
GEMINI_DEFAULT_ASPECT = "16:9"

# Estimated USD per image (standard pricing, mid-2026 docs). These are ESTIMATES —
# the providers bill by token; figures marked (est) are derived, not list prices.
# Reference-image input cost (fractions of a cent) is not included. Batch API = ÷2.
PRICING = {
    "gpt-image-2": {        # 4K/non-1024 tiers are estimates (no flat list price)
        "1024x1024": {"low": 0.006, "medium": 0.053, "high": 0.211},
        "1536x1024": {"low": 0.005, "medium": 0.041, "high": 0.165},
        "1024x1536": {"low": 0.005, "medium": 0.041, "high": 0.165},
        "4k":        {"low": 0.120, "medium": 0.250, "high": 0.410},
    },
    "gpt-image-1.5": {
        "1024x1024": {"low": 0.009, "medium": 0.034, "high": 0.133},
        "1536x1024": {"low": 0.013, "medium": 0.050, "high": 0.199},
        "1024x1536": {"low": 0.013, "medium": 0.051, "high": 0.200},
    },
    "gemini-3.1-flash-image": {"0.5K": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151},
    "gemini-3-pro-image": {"1K": 0.134, "2K": 0.134, "4K": 0.240},
}

LEDGER_PATH = ROOT / "outputs" / "cost_ledger.jsonl"   # fixed location, ignores --out

_print_lock = threading.Lock()
_ledger_lock = threading.Lock()


def log(msg: str) -> None:
    """Progress to stderr — keeps stdout a single clean JSON object."""
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


# ── provenance ───────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:max_len] or "image"


def save_image(pil: PILImage.Image, slug: str, idx: int, meta: dict, ext: str) -> str:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if ext == "png" and pil.mode not in ("RGB", "RGBA"):
        pil = pil.convert("RGBA")
    elif ext == "webp" and pil.mode not in ("RGB", "RGBA"):
        pil = pil.convert("RGB")

    filename = f"{slug}_{idx}.{ext}"
    filepath = OUTPUT_DIR / filename

    exif = pil.getexif()
    exif[ExifTags.Base.ImageDescription] = meta["prompt"]
    exif[ExifTags.Base.UserComment] = meta["prompt"]
    exif[ExifTags.Base.Software] = f"imagegen / {meta['model']}"

    save_kwargs = {"exif": exif.tobytes()}
    if ext == "webp":
        save_kwargs.update(format="WEBP", quality=98, method=6)
    else:
        save_kwargs.update(format="PNG")
    pil.save(str(filepath), **save_kwargs)

    sidecar = {**meta, "filename": filename,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
    filepath.with_suffix(".json").write_text(json.dumps(sidecar, indent=2))
    return str(filepath.relative_to(ROOT))


# ── spec: a single normalized generation request ─────────────────────────────

DEFAULTS = dict(provider="auto", model=None, size=None, aspect=None, quality="high",
                transparent=False, refs=(), edit=(), n=1, name=None)


def make_spec(prompt, **over):
    spec = {**DEFAULTS, "prompt": prompt}
    spec.update({k: v for k, v in over.items() if v is not None})
    return spec


def resolve_provider(spec) -> str:
    if spec["provider"] != "auto":
        return spec["provider"]
    # transparent / pixel-size / edit -> openai ; otherwise gemini
    if spec["transparent"] or (spec["size"] and "x" in spec["size"]) or spec["edit"]:
        return "openai"
    return "gemini"


def resolve_model(spec, provider) -> str:
    if provider == "gemini":
        m = (spec.get("model") or "").lower()
        if m.startswith("gemini-"):
            return m
        return GEMINI_ALIASES.get(m, GEMINI_MODEL)
    return OPENAI_TRANSPARENT_MODEL if spec["transparent"] else OPENAI_MODEL


# ── cost estimation + tracking ───────────────────────────────────────────────

def _openai_tier(size) -> str:
    if not size or size == "auto":
        return "1024x1024"
    if "x" in size:
        try:
            w, h = (int(x) for x in size.lower().split("x"))
        except ValueError:
            return "1024x1024"
        if max(w, h) > 1536:
            return "4k"
        return "1536x1024" if w > h else "1024x1536" if h > w else "1024x1024"
    return "1024x1024"


def per_image_cost(provider, model, size, quality) -> float:
    """Estimated USD for one image. 0.0 if model unknown."""
    table = PRICING.get(model)
    if not table:
        return 0.0
    if provider == "gemini":
        return table.get((size or GEMINI_DEFAULT_SIZE).upper(), table["2K"])
    tier = _openai_tier(size)
    qcol = table.get(tier) or table.get("1536x1024") or table["1024x1024"]
    q = (quality or "high").lower()
    return qcol.get(q if q in qcol else "high")


def cost_tier(provider, model, size) -> str:
    return (size or GEMINI_DEFAULT_SIZE).upper() if provider == "gemini" else _openai_tier(size)


def estimate_spec(spec) -> dict:
    """Projected cost for a spec without generating. Counts produce n images each."""
    provider = resolve_provider(spec)
    model = resolve_model(spec, provider)
    per = per_image_cost(provider, model, spec["size"], spec["quality"])
    return {"provider": provider, "model": model, "n": spec["n"],
            "per_image_usd": round(per, 4), "cost_usd": round(per * spec["n"], 4)}


def append_ledger(entry: dict) -> None:
    with _ledger_lock:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")


# ── backends ─────────────────────────────────────────────────────────────────

def run_openai(spec):
    from openai import OpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI()
    size = spec["size"] or OPENAI_DEFAULT_SIZE
    model = OPENAI_TRANSPARENT_MODEL if spec["transparent"] else OPENAI_MODEL
    common = dict(model=model, prompt=spec["prompt"], n=spec["n"], size=size,
                  quality=spec["quality"], output_format="png")
    if spec["transparent"]:
        common["background"] = "transparent"

    sources = [Path(p) for p in tuple(spec["edit"]) + tuple(spec["refs"])]
    if sources:
        files = [open(p, "rb") for p in sources]
        try:
            resp = client.images.edit(image=files if len(files) > 1 else files[0], **common)
        finally:
            for f in files:
                f.close()
    else:
        resp = client.images.generate(**common)

    meta = {"prompt": spec["prompt"], "model": model, "provider": "openai",
            "size": size, "quality": spec["quality"], "transparent": spec["transparent"],
            "refs": [str(p) for p in sources]}
    slug = slugify(spec["name"] or spec["prompt"])
    images = [save_image(PILImage.open(io.BytesIO(base64.b64decode(d.b64_json))),
                         slug, i, meta, "png")
              for i, d in enumerate(resp.data)]
    return images, model


def run_gemini(spec):
    from google import genai
    from google.genai import types
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError("GOOGLE_API_KEY not set")
    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    model = resolve_model(spec, "gemini")
    size = spec["size"] or GEMINI_DEFAULT_SIZE
    aspect = spec["aspect"] or GEMINI_DEFAULT_ASPECT

    contents = [spec["prompt"]]
    sources = tuple(spec["edit"]) + tuple(spec["refs"])
    for p in sources:
        contents.append(PILImage.open(p))

    meta = {"prompt": spec["prompt"], "model": model, "provider": "gemini",
            "size": size, "aspect_ratio": aspect, "refs": [str(p) for p in sources]}
    slug = slugify(spec["name"] or spec["prompt"])
    saved = []
    for _ in range(spec["n"]):   # Gemini returns one image per call
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect, image_size=size),
            ),
        )
        imgs = [p for p in resp.parts if p.inline_data is not None]
        if not imgs:
            txt = " ".join(p.text for p in resp.parts if p.text) or "no image returned"
            raise RuntimeError(f"gemini: {txt[:200]}")
        for part in imgs:
            pil = PILImage.open(io.BytesIO(part.inline_data.data))
            saved.append(save_image(pil, slug, len(saved), meta, "webp"))
    return saved, model


def run_one(spec) -> dict:
    """Generate for one spec. Never raises — returns a result dict."""
    provider = resolve_provider(spec)
    try:
        images, model = (run_openai if provider == "openai" else run_gemini)(spec)
        # bill for images actually produced, not the requested n
        per = per_image_cost(provider, model, spec["size"], spec["quality"])
        cost = round(per * len(images), 4)
        append_ledger({"ts": datetime.now().isoformat(timespec="seconds"),
                       "provider": provider, "model": model,
                       "tier": cost_tier(provider, model, spec["size"]),
                       "quality": spec["quality"] if provider == "openai" else None,
                       "images": len(images), "cost_usd": cost,
                       "prompt": spec["prompt"][:80]})
        return {"ok": True, "provider": provider, "model": model,
                "prompt": spec["prompt"], "images": images,
                "cost_usd": cost, "error": None}
    except Exception as e:
        return {"ok": False, "provider": provider, "model": None,
                "prompt": spec["prompt"], "images": [], "cost_usd": 0.0, "error": str(e)}


# ── batch ────────────────────────────────────────────────────────────────────

def load_specs(path: Path, defaults: dict) -> list:
    """Read a .txt (one prompt/line) or .jsonl/.json (per-item spec) into specs."""
    text = path.read_text()
    specs = []
    if path.suffix == ".jsonl":
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    elif path.suffix == ".json":
        rows = json.loads(text)
    else:
        rows = [{"prompt": l.strip()} for l in text.splitlines() if l.strip()]
    for row in rows:
        if "prompt" not in row:
            raise ValueError(f"each item needs a 'prompt' key: {row}")
        merged = {**defaults, **row}     # per-item keys override CLI defaults
        specs.append(make_spec(**merged))
    return specs


def run_batch(specs: list, workers: int) -> dict:
    total = len(specs)
    log(f"batch: {total} prompts, {workers} workers -> {OUTPUT_DIR}")
    results = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(run_one, s): i for i, s in enumerate(specs)}
        for fut in as_completed(futs):
            i = futs[fut]
            res = fut.result()
            results[i] = res
            done += 1
            mark = "ok " if res["ok"] else "ERR"
            tail = res["images"][0] if res["images"] else (res["error"] or "")[:80]
            log(f"  [{done}/{total}] {mark} {res['provider']:<6} ${res['cost_usd']:.3f} "
                f"{res['prompt'][:38]!r} -> {tail}")

    images = [p for r in results for p in r["images"]]
    succeeded = sum(1 for r in results if r["ok"])
    total_cost = round(sum(r["cost_usd"] for r in results), 4)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = OUTPUT_DIR / f"batch_{stamp}.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                "count": total, "succeeded": succeeded, "failed": total - succeeded,
                "total_cost_usd": total_cost, "results": results}
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log(f"batch done: {succeeded}/{total} ok, est ${total_cost:.3f}, "
        f"manifest -> {manifest_path.relative_to(ROOT)}")

    return {"ok": succeeded == total, "count": total, "succeeded": succeeded,
            "failed": total - succeeded, "total_cost_usd": total_cost,
            "images": images, "results": results,
            "manifest": str(manifest_path.relative_to(ROOT)),
            "errors": [r["error"] for r in results if r["error"]]}


# ── cost report ──────────────────────────────────────────────────────────────

def cost_report() -> dict:
    """Aggregate the ledger for visibility: total, today, by model."""
    if not LEDGER_PATH.exists():
        return {"ok": True, "note": "no ledger yet", "total_usd": 0.0,
                "today_usd": 0.0, "images": 0, "by_model": {}}
    rows = [json.loads(l) for l in LEDGER_PATH.read_text().splitlines() if l.strip()]
    today = datetime.now().date().isoformat()
    by_model = {}
    for r in rows:
        m = by_model.setdefault(r["model"], {"images": 0, "cost_usd": 0.0})
        m["images"] += r["images"]
        m["cost_usd"] = round(m["cost_usd"] + r["cost_usd"], 4)
    return {
        "ok": True,
        "total_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "today_usd": round(sum(r["cost_usd"] for r in rows if r["ts"][:10] == today), 4),
        "images": sum(r["images"] for r in rows),
        "runs": len(rows),
        "by_model": by_model,
        "ledger": str(LEDGER_PATH.relative_to(ROOT)),
        "note": "estimates; see PRICING in imagegen.py",
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_common(ap):
    ap.add_argument("--provider", choices=["auto", "openai", "gemini"], default="auto")
    ap.add_argument("--model", help="gemini model: 2|flash (Nano Banana 2, default) | pro (Nano Banana Pro) | full id")
    ap.add_argument("--size", help="openai: 1024x1024|1536x1024|1024x1536|<WxH up to 4K>|auto ; gemini: 0.5K|1K|2K|4K")
    ap.add_argument("--aspect", help="gemini: 1:1 16:9 9:16 4:3 3:4 3:2 2:3 5:4 4:5 21:9 (default 16:9)")
    ap.add_argument("--quality", default="high", help="openai: low|medium|high|auto")
    ap.add_argument("--transparent", action="store_true", help="transparent bg (routes to gpt-image-1.5)")
    ap.add_argument("--refs", nargs="*", default=[], help="reference images (style/subject); openai<=16, gemini<=14")
    ap.add_argument("--edit", nargs="*", default=[], help="source image(s) to edit/recolor")
    ap.add_argument("-n", type=int, default=1, help="images per prompt (default 1)")
    ap.add_argument("--out", help="output dir (default ./outputs)")
    ap.add_argument("--estimate", action="store_true",
                    help="dry run: print projected cost as JSON, generate nothing")


def cli_defaults(args) -> dict:
    return dict(provider=args.provider, model=args.model, size=args.size, aspect=args.aspect,
                quality=args.quality, transparent=args.transparent,
                refs=args.refs, edit=args.edit, n=args.n)


def main():
    ap = argparse.ArgumentParser(description="Unified image generation CLI (OpenAI + Gemini).")
    sub = ap.add_subparsers(dest="cmd")

    ps = sub.add_parser("single", help="generate from one prompt (default if no subcommand)")
    ps.add_argument("prompt")
    add_common(ps)

    pb = sub.add_parser("batch", help="generate from a .txt / .jsonl / .json file of prompts")
    pb.add_argument("file")
    pb.add_argument("--workers", type=int, default=4, help="concurrent requests (default 4)")
    add_common(pb)

    sub.add_parser("cost", help="show spend totals from the ledger (today / all-time / by model)")

    # Allow bare `imagegen "prompt" ...` (no subcommand) -> single.
    argv = sys.argv[1:]
    if argv and argv[0] not in ("single", "batch", "cost", "-h", "--help"):
        argv = ["single"] + argv
    args = ap.parse_args(argv)

    global OUTPUT_DIR
    if getattr(args, "out", None):
        OUTPUT_DIR = Path(args.out).expanduser().resolve()

    if args.cmd == "cost":
        print(json.dumps(cost_report(), indent=2))
        return

    if args.cmd == "batch":
        defaults = cli_defaults(args)
        # CLI defaults shouldn't clobber per-item jsonl keys unless explicitly set;
        # keep only the ones the user actually changed from argparse defaults.
        specs = load_specs(Path(args.file), {k: v for k, v in defaults.items()
                                             if v not in (None, False, [], "high", "auto")})
        if args.estimate:
            items = [estimate_spec(s) for s in specs]
            print(json.dumps({"ok": True, "estimate": True, "count": len(items),
                              "cost_usd": round(sum(i["cost_usd"] for i in items), 4),
                              "items": items, "note": "estimate, nothing generated"}, indent=2))
            return
        out = run_batch(specs, args.workers)
        print(json.dumps(out))
        sys.exit(0 if out["ok"] else 1)

    elif args.cmd == "single":
        spec = make_spec(args.prompt, **cli_defaults(args))
        if args.estimate:
            est = estimate_spec(spec)
            print(json.dumps({"ok": True, "estimate": True, **est,
                              "note": "estimate, nothing generated"}, indent=2))
            return
        res = run_one(spec)
        log(f"{'ok ' if res['ok'] else 'ERR'} {res['provider']} ${res['cost_usd']:.3f} -> "
            f"{res['images'][0] if res['images'] else res['error']}")
        print(json.dumps({"ok": res["ok"], "provider": res["provider"],
                          "model": res["model"], "images": res["images"],
                          "cost_usd": res["cost_usd"],
                          "errors": [res["error"]] if res["error"] else []}))
        sys.exit(0 if res["ok"] else 1)

    else:
        ap.print_help()


if __name__ == "__main__":
    main()
