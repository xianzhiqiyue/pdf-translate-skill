---
name: pdf-translate
version: 0.1.1
category: docs
tags:
  - docs
  - automation
  - integration
description: Translate selectable non-scanned PDFs while preserving label positions. Prefer caller AI (Codex/OpenClaw); configure an OpenAI-compatible API only as fallback.

---

# PDF Translate

## Overview

Translate non-scanned PDFs by extracting selectable text with coordinates, using the invoking AI assistant (for example Codex, OpenClaw, or another caller) for terminology-aware translation, then replacing each text line inside its original PDF bounding box. Default target language is English unless the user specifies another language.

Default AI policy:

- First choice: the caller that invoked this skill provides translation intelligence. Extract JSON, let the current assistant fill each segment's `translation`, then apply that reviewed JSON.
- Fallback only: if the caller is a non-AI automation environment or cannot produce translations, configure an OpenAI-compatible endpoint and let the script call it with `OPENAI_API_KEY`, `OPENAI_MODEL`, and optional `OPENAI_BASE_URL`.
- Do not ask users to configure an AI API when the active assistant can translate the extracted segments itself.

Use this skill for PDFs where text position matters (figure labels, image annotations, engineering/medical/legal/scientific captions). Do not use it for scanned PDFs unless an OCR step has already converted image text into selectable text.

## Workflow

1. Confirm the PDF has selectable text.
   - Run extraction first; if it returns no text, tell the user the PDF is likely scanned and needs OCR before this skill can preserve positions.
2. Choose the target language.
   - Default to `English` when the user does not specify one.
3. Capture domain constraints.
   - If the document is technical or professional, create a small glossary/instructions file and pass it with `--glossary` and/or `--instructions`.
4. Run the bundled script in extraction mode to produce a translation JSON template.
5. Use the current assistant's AI ability to fill `translation` values in that JSON, preserving segment ids, boxes, skip flags, and concise terminology.
6. Apply the reviewed JSON with the bundled script to overlay text in-place.
7. Inspect the output PDF visually and with extraction logs. Re-run with skip rules or glossary improvements if labels are mistranslated or too long for boxes.

## Quick start

From the skill folder, first confirm the PDF has selectable text and create a translation template:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf output.pdf \
  --target-language English \
  --extract-only-json /tmp/pdf-label-translations.json
```

Then, as the invoking AI assistant, edit `/tmp/pdf-label-translations.json`:

- Keep the top-level schema and `recommended_args`.
- For each non-skipped segment, replace `translation` with a concise target-language translation.
- For skipped segments, keep `translation` equal to the original `text`.
- Preserve `id`, `page`, `bbox`, `font_size`, `rotation`, `char_budget`, and skip metadata exactly.

Apply the reviewed translations without making any external AI API call:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json
```

Install the only runtime PDF dependency in the active environment if missing. Prefer the bundled installer because it uses longer pip network timeouts and China-friendly mirrors by default:

```bash
python3 scripts/install_deps.py
```

Equivalent manual command with a longer timeout and Tsinghua PyPI mirror:

```bash
python3 -m pip install --timeout 300 --retries 8 \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  --trusted-host pypi.tuna.tsinghua.edu.cn \
  pymupdf
```

If one mirror is slow or unavailable, the installer automatically tries common China mirrors in this order: Tsinghua, Aliyun, USTC, Douban. Override with `--mirror URL`, use `--timeout 600` on slow networks, use `--dry-run` to preview pip commands, or use `--no-mirror` to respect the environment's default pip index.

## Recommended two-pass execution

First confirm that the PDF has selectable text and inspect the extracted line boxes plus top-level `recommended_args` without calling any external LLM API:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf output.pdf \
  --extract-only-json /tmp/pdf-label-extraction.json
```

Copy the extraction JSON to a review file and translate it with the current assistant:

```bash
cp /tmp/pdf-label-extraction.json /tmp/pdf-label-translations.json
```

Assistant translation instructions:

1. Read `recommended_args` and honor `target_language`, `char_budget`, glossary, and user instructions.
2. Translate only non-skipped `segments[*].text`.
3. Write the translated text to the same segment's `translation` field.
4. Keep skipped, numeric-only, model names, units, symbols, figure numbers, standards, citations, and user-specified no-translate terms unchanged.
5. Keep translations concise enough to fit the original label/callout box.

Then apply the reviewed translations without another AI call:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json
```

Only when the invoking environment cannot provide translation AI, create a translated review JSON through an OpenAI-compatible endpoint:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf output.pdf \
  --target-language English \
  --model "$OPENAI_MODEL" \
  --dry-run-json /tmp/pdf-label-translations.json
```

This fallback endpoint mode uses:

- `OPENAI_API_KEY` (required unless using `--translations-json`)
- `OPENAI_MODEL` or `--model`
- `OPENAI_BASE_URL` (optional; defaults to `https://api.openai.com/v1`)

Review `/tmp/pdf-label-translations.json` for terminology, segment boundaries, and parameter hints. Then apply the reviewed translations without another endpoint call:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json
```

## AI parameter decision guide

Always run extraction before choosing final write-back parameters:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf output.pdf \
  --target-language English \
  --extract-only-json /tmp/pdf-label-extraction.json
```

Read the top-level `recommended_args` object in the extraction JSON, then choose parameters deliberately instead of using one fixed command for every PDF.

Decision rules:

| Signal from extraction or user request | Recommended action |
| --- | --- |
| User did not specify target language | Use `--target-language English`. |
| Target is English or another Latin-script language | Usually omit `--fontfile`; built-in Helvetica is portable. |
| Target is Chinese/Japanese/Korean or output contains non-Latin glyphs | Add `--fontfile auto`, or pass a known font such as Noto Sans CJK / Source Han Sans. |
| `recommended_args.min_font_size` is `3.5` | Use `--min-font-size 3.5`; many label boxes are tight. |
| `recommended_args.max_box_scale` is above `1.0` | Use that value if slight centered expansion is acceptable; keep `1.0` for strict no-expansion placement. |
| `recommended_args.skip_regex_suggestions` contains patterns | Consider adding those `--skip-regex` values for drawing grid letters, pure numbers, dates, or title-block fields that should stay unchanged. |
| Long requirement lines or professional terminology are present | Use `--extract-only-json`, let the invoking assistant produce/review concise translations, then apply with `--translations-json`. |
| Colored/gray backgrounds or mixed image areas are present | Keep default `--redact-fill auto`; override with a hex color only after visual inspection. |
| Rotated labels are reported | No parameter is needed for right-angle labels; the script reuses extracted rotation. Inspect arbitrary-angle text manually. |

For Chinese engineering drawings translated to English, a good starting point is usually:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --target-language English \
  --redact-fill auto \
  --min-font-size 3.5 \
  --max-box-scale 1.10 \
  --extract-only-json /tmp/pdf-label-translations.json
```

Then fill/review translations with the invoking assistant and apply:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json \
  --redact-fill auto \
  --min-font-size 3.5 \
  --max-box-scale 1.10
```

## Terminology and professional language

Prefer a short glossary for specialized documents:

```text
Finite element analysis -> finite element analysis
应变片 -> strain gauge
屈服强度 -> yield strength
Do not translate model names, units, symbols, figure numbers, standards, or citations.
Keep labels concise enough to fit inside the original box.
```

Run with:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --target-language English \
  --glossary glossary.txt \
  --instructions "Use concise engineering terminology; preserve units and reference codes." \
  --extract-only-json /tmp/pdf-label-translations.json
```

Then fill the `translation` fields using the invoking assistant's AI ability and apply:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json
```

## Position-preservation guidance

The script translates by text line, not by paragraph, because labels and callouts usually depend on exact local placement. It redacts the original text rectangle and inserts the translation into the same rectangle, shrinking font size when needed. The default redaction fill is `auto`, which samples the local background color instead of always painting white. The extractor records 0/90/180/270-degree line rotation and the writer reuses that rotation when inserting translations. Inserted text uses Helvetica by default, which is suitable for English and many Latin-script targets; pass `--fontfile` or `--fontfile auto` for CJK or other scripts that require a specific font.

Important options:

- `--extract-only-json /tmp/segments.json` verifies selectable text and line boxes without an external LLM/API call, writes top-level `recommended_args`, and creates the preferred caller-AI translation template.
- `--pages 1,3-5` limits translation to selected 1-based pages.
- `--skip-regex PATTERN` leaves matching text unchanged; repeat for multiple patterns.
- `--min-font-size 5` sets the smallest allowed inserted font.
- `--max-box-scale 1.3` permits centered expansion around the original bbox for translations that cannot fit after shrinking; keep at `1.0` for strict no-expansion placement.
- `--bbox-pad 0.75` expands each source box slightly before redaction/insertion.
- `--redact-fill auto` samples the surrounding background; pass a hex color such as `--redact-fill '#F3F3F3'` when auto sampling is wrong.
- `--fontfile /path/to/font.ttf` embeds a font that can render non-Latin target languages such as Chinese, Japanese, or Korean; `--fontfile auto` tries local common Unicode fonts and warns if it may not cover CJK.
- `--keep-original` overlays translations without removing original text; use only for debugging.

Read `references/pdf-positioning.md` before handling dense figures, colored backgrounds, vertical labels, or documents with strict publication-quality layout requirements.

## Fixing common layout issues

- Colored or image backgrounds: keep the default `--redact-fill auto` so each source bbox samples its local background. If the sampled color is visibly wrong on gradients or photographs, rerun the affected pages with a manual hex fill or use `--keep-original` for a diagnostic overlay. Exact image inpainting is outside this script; use manual post-editing for publication-critical photo backgrounds.
- Rotated/vertical labels: the script now detects line direction and preserves 0/90/180/270-degree rotation when inserting translations. Inspect arbitrary-angle text manually because PDF text boxes only support right-angle insertion.
- Long translations: each segment includes a per-label `char_budget` estimate. Use concise glossary entries, keep caller-generated translations short, edit translations manually, lower `--min-font-size` if fit matters more than readability, or allow bounded expansion with `--max-box-scale 1.2` to keep the label centered on the original position.
- Missing glyphs: pass `--fontfile /path/to/NotoSansCJK-Regular.ttc` or `--fontfile auto` for non-Latin targets. If warnings say the selected font may not cover CJK, install/use a CJK font and rerun from the reviewed JSON.
- Exact typography: the script preserves placement and color, not the source font. For final print layout, inspect and optionally post-edit high-value pages.

## Validation checklist

After producing the translated PDF:

1. Open the PDF and compare each page against the original.
2. Check that labels remain in the same regions and do not cover important image content.
3. Search/extract text from the output PDF to confirm translated text is selectable.
4. Inspect warnings from the script; long labels may be inserted at minimum font size or clipped by the original box.
5. For professional PDFs, verify terminology with the glossary and rerun only affected pages if needed.
