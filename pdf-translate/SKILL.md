---
name: pdf-translate
version: 0.1.5
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
6. Check font readiness before applying translations.
   - If the target language or translated text uses Chinese/Japanese/Korean, Arabic, Devanagari/Hindi, Thai, Hebrew, Cyrillic, Greek, or another non-Latin script, install a supporting font and use `--fontfile auto` or pass a known `.ttf/.otf/.ttc` file.
7. Apply the reviewed JSON with the bundled script to overlay text in-place.
8. Inspect the output PDF visually and with extraction logs. Re-run with skip rules, glossary improvements, larger `--max-box-scale`, explicit `--redact-pad`, or a better font if labels are mistranslated, clipped, still contain source text, or rendered as missing glyph boxes.

## Simple intent shortcuts

When a user says “把这个 PDF 全部替换成英文” / “translate the whole PDF to English”, the invoking assistant should use the `to-english` preset and should not ask the user to choose layout parameters:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --to-english \
  --extract-only-json /tmp/pdf-label-translations.json
```

Then the assistant fills `translation` for every non-skipped segment and applies with the same preset:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --to-english \
  --translations-json /tmp/pdf-label-translations.json
```

For English-to-Chinese output, use:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --to-chinese \
  --extract-only-json /tmp/pdf-label-translations.json

python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --to-chinese \
  --translations-json /tmp/pdf-label-translations.json
```

Preset defaults:

| Preset | Target/source | Defaults applied |
| --- | --- | --- |
| `--to-english` / `--preset zh-to-en` | Chinese → English | `target_language=English`, `source_language=Chinese`, `--max-box-scale 1.2`, `--min-font-size 3.5`, `--fail-on-untranslated`, concise English instructions. |
| `--to-chinese` / `--preset en-to-zh` | English → Simplified Chinese | `target_language=Simplified Chinese`, `source_language=English`, `--max-box-scale 1.15`, `--min-font-size 4.0`, concise Chinese instructions, automatic Chinese font selection. |

The user-facing instruction can remain simple: “把文本全部替换成英文”. The assistant should run extraction, fill translations, apply the preset, inspect warnings, and only ask the user if OCR/manual cleanup is required.

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

Before applying, read `recommended_args.fontfile`, `recommended_args.font_scripts`, and `recommended_args.font_install_hint`. If the target output is non-Latin and the system lacks a suitable font, install one first or pass an explicit font path.

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
6. Treat `review_flags` seriously. For `spaced_cjk`, translate by meaning after removing spaces between CJK characters, e.g. `阶 段 标 记` should be handled as `阶段标记`.
7. Do not leave `(NEEDS_REVIEW)` or unchanged Chinese in `translation` for English output unless the user explicitly wants that text preserved.
8. If the translations contain non-Latin text, ensure a matching font is installed before applying.

Then apply the reviewed translations without another AI call. For Chinese-to-English engineering drawings, use QA and more forgiving layout defaults:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json \
  --redact-pad 2.0 \
  --max-box-scale 1.2 \
  --min-font-size 3.5 \
  --fail-on-untranslated
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
| Target is Chinese/Japanese/Korean or output contains CJK glyphs | Install a CJK-capable font if missing, then add `--fontfile auto`, or pass a known font such as Noto Sans CJK / Source Han Sans. |
| Target is Arabic/Persian/Urdu, Devanagari/Hindi, Thai, Hebrew, Cyrillic, Greek, or another non-Latin script | Install a matching Noto/DejaVu/Liberation-family font if missing, then use `--fontfile auto` or an explicit font path. Inspect complex scripts visually. |
| `recommended_args.font_scripts` is non-empty | Treat `recommended_args.font_install_hint` as a preflight checklist before applying translations. |
| `recommended_args.min_font_size` is `3.5` | Use `--min-font-size 3.5`; many label boxes are tight. |
| `recommended_args.max_box_scale` is above `1.0` | Use that value if slight centered expansion is acceptable; keep `1.0` for strict no-expansion placement. |
| `recommended_args.redact_pad` is `auto` or `review_flags` contains `large_source_font` | Keep auto redaction padding or pass `--redact-pad 2.0` / larger after visual inspection to prevent source glyph edges from leaking through. |
| `review_flags` contains `spaced_cjk` | Translate the phrase semantically after removing intra-CJK spaces; glossary matching must normalize `阶 段 标 记` as `阶段标记`. |
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
  --max-box-scale 1.20 \
  --extract-only-json /tmp/pdf-label-translations.json
```

Then fill/review translations with the invoking assistant and apply:

```bash
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json \
  --redact-fill auto \
  --redact-pad 2.0 \
  --min-font-size 3.5 \
  --max-box-scale 1.20 \
  --fail-on-untranslated
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


## Troubleshooting residual source text or missed translations

If the output still shows Chinese/source text after translation, distinguish three layers:

1. Translation JSON layer: inspect `/tmp/pdf-label-translations.json` first. If `translation` is unchanged, contains Chinese for an English target, or includes `(NEEDS_REVIEW)`, fix that segment before applying. The script now warns on likely untranslated segments and `--fail-on-untranslated` can stop the run.
2. Extraction/terminology layer: CJK text may be extracted with spaces between characters, such as `阶 段 标 记`. Use each segment's `review_flags`; when `spaced_cjk` appears, normalize mentally or in glossary matching by removing intra-CJK spaces.
3. Rendering/redaction layer: original glyph edges may remain visible if the PDF bbox is too tight. Use the default auto redaction padding, or pass a larger value such as `--redact-pad 2.0` or `--redact-pad 3.0`. This affects the erase rectangle only; insertion still uses `--bbox-pad`.

For labels where English becomes tiny, the script warns when inserted font size shrinks below `--warn-font-scale` of the original. Prefer shorter terminology, abbreviations, or a larger centered box with `--max-box-scale 1.2` to `1.4`.

The script processes every extracted, non-skipped segment. If one of several identical words remains, it usually means that instance was not extracted as selectable text, was marked skipped, had unchanged/NEEDS_REVIEW translation, or belongs to a PDF structure that redaction did not affect cleanly. Re-run extraction and search all matching `segments[*].text` values by `id` before applying.

## Bundled font support

A skill package can technically carry a Chinese font, but this package does **not** include a font binary by default because CJK fonts are usually large and the distributor must verify license/package-size requirements. Instead, the script supports bundled fonts when present:

- Place licensed `.ttf`, `.otf`, or `.ttc` files under `assets/fonts/` inside the skill.
- `--fontfile auto` and automatic non-Latin font selection search `assets/fonts/` before system font folders.
- If a packaged deployment needs offline Chinese output, bundle a licensed font such as Noto Sans CJK / Noto Sans SC / Source Han Sans in `assets/fonts/`, then pack the skill.

If no bundled or system font can render Chinese, the script fails before writing the PDF rather than producing `?` text.

## Font readiness for non-Latin output

PDF insertion only works well when the selected font contains glyphs for the translated language. Built-in Helvetica is fine for English and many Latin-script targets, but it does not cover Chinese/Japanese/Korean or many other scripts. Before applying translations to a non-Latin target:

1. Read `recommended_args.font_scripts` and `recommended_args.font_install_hint` in the extraction JSON.
2. Install an appropriate font if the machine does not already have one.
3. Apply normally; the script auto-selects a matching local font when translated text is non-Latin. You may still pass `--fontfile auto` or a known font path to be explicit.
4. If no matching font is found, the script now fails before writing the PDF instead of generating `?` / tofu-box output. Only use `--allow-missing-glyphs` for debugging.
5. Inspect the output PDF for tofu boxes (`□`), missing glyphs, incorrect shaping, or clipped text.

Examples:

```bash
# Ubuntu/Debian, Chinese/Japanese/Korean
sudo apt-get update && sudo apt-get install -y fonts-noto-cjk

# Ubuntu/Debian, broad non-Latin coverage such as Arabic/Hebrew/Cyrillic/Greek
sudo apt-get update && sudo apt-get install -y fonts-noto-core fonts-noto-extra

# Apply after installing fonts; --fontfile auto is optional because non-Latin text auto-selects a matching font
python3 scripts/pdf_translate_overlay.py input.pdf translated.pdf \
  --translations-json /tmp/pdf-label-translations.json
```

For manual font selection, use a real font file such as:

- CJK: `NotoSansCJK-Regular.ttc`, `NotoSansSC-Regular.otf`, Source Han Sans.
- Arabic/Persian/Urdu: Noto Sans Arabic, Noto Naskh Arabic, Amiri.
- Devanagari/Hindi: Noto Sans Devanagari.
- Thai: Noto Sans Thai.
- Hebrew: Noto Sans Hebrew or DejaVu Sans.
- Cyrillic/Greek: Noto Sans, DejaVu Sans, or Liberation Sans.

If the script cannot find a suitable font for non-Latin translated text, it refuses to write the PDF so it does not create `?` output. Install the hinted font package and rerun, pass an explicit `--fontfile`, or override only for debugging with `--allow-missing-glyphs`.

## Position-preservation guidance

The script translates by text line, not by paragraph, because labels and callouts usually depend on exact local placement. It redacts the original text rectangle and inserts the translation into the same rectangle, shrinking font size when needed. The default redaction fill is `auto`, which samples the local background color instead of always painting white. The extractor records 0/90/180/270-degree line rotation and the writer reuses that rotation when inserting translations. Inserted text uses Helvetica by default, which is suitable for English and many Latin-script targets; pass `--fontfile` or `--fontfile auto` for CJK or other scripts that require a specific font.

Important options:

- `--extract-only-json /tmp/segments.json` verifies selectable text and line boxes without an external LLM/API call, writes top-level `recommended_args`, and creates the preferred caller-AI translation template.
- `--pages 1,3-5` limits translation to selected 1-based pages.
- `--skip-regex PATTERN` leaves matching text unchanged; repeat for multiple patterns.
- `--min-font-size 5` sets the smallest allowed inserted font.
- `--max-box-scale 1.3` permits centered expansion around the original bbox for translations that cannot fit after shrinking; keep at `1.0` for strict no-expansion placement.
- `--bbox-pad 0.75` expands each source box slightly for insertion.
- `--redact-pad auto` expands each source box for redaction with `max(--bbox-pad, 18% of source font size)`; pass a number such as `--redact-pad 2.0` if source glyph edges remain visible.
- `--fail-on-untranslated` exits non-zero when QA detects unchanged CJK, CJK left in English output, or `(NEEDS_REVIEW)` markers.
- `--warn-font-scale 0.6` warns when inserted text shrinks below 60% of source font size.
- `--redact-fill auto` samples the surrounding background; pass a hex color such as `--redact-fill '#F3F3F3'` when auto sampling is wrong.
- `--preset {to-english,zh-to-en,to-chinese,en-to-zh}`, `--to-english`, and `--to-chinese` apply common replacement defaults so users do not need to choose layout parameters.
- `--fontfile /path/to/font.ttf` embeds a font that can render non-Latin target languages such as Chinese, Japanese, Korean, Arabic, Devanagari, Thai, Hebrew, Cyrillic, or Greek; if omitted, the script auto-selects a matching bundled/system font whenever translated text needs one. `--fontfile auto` makes this explicit.
- `--allow-missing-glyphs` bypasses the missing-font failure for debugging only; output may render as `?` or tofu boxes.
- `--keep-original` overlays translations without removing original text; use only for debugging.

Read `references/pdf-positioning.md` before handling dense figures, colored backgrounds, vertical labels, or documents with strict publication-quality layout requirements.

## Fixing common layout issues

- Colored or image backgrounds: keep the default `--redact-fill auto` so each source bbox samples its local background. If the sampled color is visibly wrong on gradients or photographs, rerun the affected pages with a manual hex fill or use `--keep-original` for a diagnostic overlay. Exact image inpainting is outside this script; use manual post-editing for publication-critical photo backgrounds.
- Rotated/vertical labels: the script now detects line direction and preserves 0/90/180/270-degree rotation when inserting translations. Inspect arbitrary-angle text manually because PDF text boxes only support right-angle insertion.
- Long translations: each segment includes a per-label `char_budget` estimate. Use concise glossary entries, keep caller-generated translations short, edit translations manually, lower `--min-font-size` if fit matters more than readability, or allow bounded expansion with `--max-box-scale 1.2` to `1.4` to keep the label centered on the original position.
- Residual source glyphs: increase `--redact-pad` or inspect `review_flags` for large fonts/tight boxes.
- Missing glyphs / question marks: the script should auto-select a matching font for non-Latin translations and fail if none is available. If you still see `?`, rerun with an explicit `--fontfile /path/to/NotoSansCJK-Regular.ttc` (or another matching font), and do not use `--allow-missing-glyphs` for production output.
- Exact typography: the script preserves placement and color, not the source font. For final print layout, inspect and optionally post-edit high-value pages.

## Validation checklist

After producing the translated PDF:

1. Open the PDF and compare each page against the original.
2. Check that labels remain in the same regions and do not cover important image content.
3. Search/extract text from the output PDF to confirm translated text is selectable.
4. Inspect warnings from the script; long labels may be inserted at minimum font size or clipped by the original box.
5. For professional PDFs, verify terminology with the glossary and rerun only affected pages if needed.
