# PDF positioning notes

Use these notes when translated PDF labels must remain visually close to the source layout.

## What the script preserves

- Page count and page dimensions.
- Per-line bounding boxes from the source PDF.
- Original images and vector artwork outside redacted text boxes.
- Selectable output text inserted inside the original line rectangles.

## Layout issue playbook

- Colored or image backgrounds: default `--redact-fill auto` samples the border of each text box and fills with the median local color. This reduces white patches on flat colored figures. On gradients/photos, use a manual hex `--redact-fill`, `--keep-original` for diagnosis, or manual post-editing because the script does not inpaint imagery.
- Translations longer than the source: extraction JSON includes `char_budget`; the invoking assistant should treat it as the concise length budget (fallback endpoint mode sends it as `max_chars`). If a label still overflows, edit the reviewed JSON to a concise abbreviation or rerun with `--max-box-scale 1.2` to expand the box around the original center.
- Rotated or vertical text: extraction records nearest right-angle `rotation` from the PDF line direction; insertion reuses it. Right-angle labels are supported. Arbitrary-angle labels still need manual inspection.
- Fonts and exact typography: built-in Helvetica is portable for English/Latin output. Use `--fontfile` or `--fontfile auto` for non-Latin output; CJK should use a real CJK font such as Noto Sans CJK or Source Han Sans. The script preserves position and color, not exact original font identity.
- Multi-span labels split into several PDF drawing commands: the script groups by text line. If this creates awkward translations, use `--extract-only-json`, let the invoking assistant edit segment translations, then re-apply with `--translations-json`.

## Practical quality loop

1. Run `--extract-only-json` to inspect extracted text, bbox, `rotation`, and `char_budget`; then fill translations with the invoking assistant's AI ability. Use `--dry-run-json` only as a fallback when the caller cannot provide AI translations and an OpenAI-compatible endpoint is configured.
2. Add `--skip-regex` for page numbers, units-only strings, formulas, citations, or labels that should remain unchanged.
3. Add/adjust glossary entries for professional terms.
4. Apply translations and visually inspect pages at 100% and 200% zoom.
5. Re-run only problematic pages with `--pages` to reduce cost and iteration time.
