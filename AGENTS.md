# Agent usage

The full agent-facing guide for this tool lives in **[SKILL.md](SKILL.md)** — it's the
canonical doc (registered as the `imagegen` skill). It covers when to invoke, the
`python3 imagegen.py` CLI contract, engine selection, every flag, batch mode + monitoring,
the anchor/consistency workflow, and provenance.

Quick reminder of the contract: call `python3 imagegen.py "<prompt>" [flags]` (or
`imagegen.py batch <file>`), read the **single JSON object on stdout**, use `images[*]`,
check `ok`. Progress streams to stderr. See SKILL.md for everything else.

## Appendix: optional async Batch API (not built)

This tool uses concurrent **client-side** batching (real-time monitorable, works for every
model). Both providers also offer a true async **Batch API** — JSONL upload, ~50% cheaper,
24-hour turnaround — useful for large overnight jobs. Notes if you ever wire it up:

- **OpenAI Batch** supports `/v1/images/generations` + `/v1/images/edits`, but the supported
  model list names `gpt-image-1.5` / `gpt-image-1` / `gpt-image-1-mini` / `chatgpt-image-latest`
  — **not** `gpt-image-2`. Reference images must be passed as `file_id`/`image_url`, not multipart.
- **Gemini Batch** supports `gemini-3-pro-image`; submit inline (≤20MB) or a JSONL file via the
  File API (≤2GB). ~50% cheaper, 24h target.
