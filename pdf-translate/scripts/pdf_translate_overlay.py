#!/usr/bin/env python3
"""Translate selectable PDF text and place translations in original boxes.

The script intentionally has only one PDF dependency: PyMuPDF (`pymupdf`).
Preferred workflow is caller-AI translation: extract JSON, let the invoking
assistant (Codex/OpenClaw/etc.) fill translation fields, then apply the reviewed
JSON. As a fallback for non-AI callers, the script can call an OpenAI-compatible
/chat/completions HTTP endpoint via Python's standard library, so no OpenAI SDK
is required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

try:
    import fitz  # type: ignore
except ImportError:  # pragma: no cover - exercised in environments without PyMuPDF
    fitz = None  # type: ignore


@dataclass
class Segment:
    id: str
    page: int  # zero-based
    text: str
    bbox: tuple[float, float, float, float]
    font_size: float
    color: tuple[float, float, float]
    translation: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    rotation: int = 0
    char_budget: int | None = None
    review_flags: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Translate selectable PDF text and insert translations in original positions. "
            "Prefer --extract-only-json plus caller-provided --translations-json; "
            "OpenAI-compatible endpoint calls are only a fallback."
        )
    )
    parser.add_argument("input_pdf", help="Source non-scanned PDF with selectable text")
    parser.add_argument("output_pdf", help="Path for translated output PDF")
    parser.add_argument("--target-language", default="English", help="Target language (default: English)")
    parser.add_argument("--source-language", default="auto", help="Source language hint (default: auto)")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"), help="Fallback OpenAI-compatible model name")
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        help="Fallback OpenAI-compatible API base URL",
    )
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"), help="Fallback API key for the LLM endpoint")
    parser.add_argument("--glossary", help="Plain-text glossary or terminology instructions file")
    parser.add_argument("--instructions", default="", help="Additional translation instructions")
    parser.add_argument("--pages", help="1-based page selection, e.g. '1,3-5'. Default: all pages")
    parser.add_argument(
        "--skip-regex",
        action="append",
        default=[],
        help="Regex for text that should stay unchanged. Can be repeated.",
    )
    parser.add_argument("--batch-size", type=int, default=40, help="Segments per fallback LLM request")
    parser.add_argument("--extract-only-json", help="Write extracted segments as caller-AI translation JSON without calling an LLM or writing a PDF")
    parser.add_argument("--dry-run-json", help="Write extracted + endpoint-translated segments as JSON and do not write PDF")
    parser.add_argument("--translations-json", help="Use reviewed/caller-translated JSON instead of calling an LLM")
    parser.add_argument("--bbox-pad", type=float, default=0.75, help="Points added around each text bbox for insertion")
    parser.add_argument(
        "--redact-pad",
        type=float,
        help="Points added around each text bbox for redaction. Default: auto, max(--bbox-pad, 18%% of source font size).",
    )
    parser.add_argument("--max-box-scale", type=float, default=1.0, help="Allow centered bbox expansion for long translations (default: 1.0, no expansion)")
    parser.add_argument("--min-font-size", type=float, default=5.0, help="Smallest font size used to fit translations")
    parser.add_argument(
        "--warn-font-scale",
        type=float,
        default=0.6,
        help="Warn when inserted font size is below this ratio of source size (default: 0.6)",
    )
    parser.add_argument(
        "--fail-on-untranslated",
        action="store_true",
        help="Exit non-zero if QA detects likely untranslated/NEEDS_REVIEW target text after applying translations",
    )
    parser.add_argument(
        "--allow-missing-glyphs",
        action="store_true",
        help="Allow writing non-Latin translations without a detected supporting font. Not recommended: output may render as ? or tofu boxes.",
    )
    parser.add_argument("--redact-fill", default="auto", help="Hex fill or auto to sample local background (default: auto)")
    parser.add_argument("--fontname", default="helv", help="PDF font resource name for inserted text (default: helv)")
    parser.add_argument(
        "--fontfile",
        help=(
            "Optional TrueType/OpenType font file, or auto to find a local font suitable for the target/translated script. "
            "Use auto or an explicit font for Chinese/Japanese/Korean, Arabic, Devanagari, Thai, Hebrew, Cyrillic, Greek, etc."
        ),
    )
    parser.add_argument("--keep-original", action="store_true", help="Overlay translations without redacting source text")
    parser.add_argument("--temperature", type=float, default=0.1, help="LLM temperature")
    parser.add_argument("--timeout", type=int, default=120, help="LLM HTTP timeout seconds")
    return parser.parse_args()


def require_fitz() -> None:
    if fitz is None:
        raise SystemExit(
            "PyMuPDF is required. From the skill folder, run: python3 scripts/install_deps.py "
            "or manually: python3 -m pip install --timeout 300 --retries 8 "
            "-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn pymupdf"
        )


def parse_pages(spec: str | None, page_count: int) -> set[int]:
    if not spec:
        return set(range(page_count))
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start
            selected.update(range(start - 1, end))
        else:
            selected.add(int(part) - 1)
    invalid = [p + 1 for p in selected if p < 0 or p >= page_count]
    if invalid:
        raise SystemExit(f"Page selection outside document range: {invalid}")
    return selected


def int_color_to_rgb(color: int | None) -> tuple[float, float, float]:
    if color is None:
        return (0.0, 0.0, 0.0)
    return (((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0)


def line_rotation(line: dict[str, Any]) -> int:
    """Return nearest PyMuPDF textbox rotation for a text line direction."""
    direction = line.get("dir") or (1.0, 0.0)
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except (TypeError, ValueError, IndexError):
        return 0
    candidates = {
        0: (1.0, 0.0),
        90: (0.0, -1.0),
        180: (-1.0, 0.0),
        270: (0.0, 1.0),
    }
    return min(candidates, key=lambda angle: (dx - candidates[angle][0]) ** 2 + (dy - candidates[angle][1]) ** 2)


def estimate_char_budget(bbox: tuple[float, float, float, float], font_size: float, rotation: int) -> int:
    """Approximate a concise translation budget for one PDF label box."""
    x0, y0, x1, y1 = bbox
    width = abs(x1 - x0)
    height = abs(y1 - y0)
    inline_extent = height if rotation in {90, 270} else width
    avg_latin_char_width = max(font_size * 0.48, 1.0)
    return max(4, int(inline_extent / avg_latin_char_width))


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
    if len(value) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        raise SystemExit("--redact-fill must be a 6-digit hex color such as '#FFFFFF'")
    return tuple(int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def remove_intra_cjk_spaces(text: str) -> str:
    return re.sub(r"(?<=[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af])\s+(?=[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af])", "", text)


def has_spaced_cjk(text: str) -> bool:
    return remove_intra_cjk_spaces(text) != text


def build_review_flags(text: str, font_size: float, char_budget: int | None) -> list[str]:
    flags: list[str] = []
    if has_spaced_cjk(text):
        flags.append("spaced_cjk: translate by meaning after removing spaces between CJK characters")
    if char_budget is not None and char_budget <= 12:
        flags.append("tight_box: keep translation very concise or allow box expansion")
    if len(text) >= 40:
        flags.append("long_line: review terminology and fit before applying")
    if font_size >= 18:
        flags.append("large_source_font: use larger redaction padding and inspect coverage")
    return flags


def compile_skip_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise SystemExit(f"Invalid --skip-regex {pattern!r}: {exc}") from exc
    return compiled


def should_skip(text: str, patterns: list[re.Pattern[str]]) -> tuple[bool, str | None]:
    if not text:
        return True, "empty"
    if not re.search(r"[A-Za-z\u0080-\uffff]", text):
        return True, "no letters"
    for pattern in patterns:
        if pattern.search(text):
            return True, f"matched skip regex: {pattern.pattern}"
    return False, None


def extract_segments(pdf_path: str, page_selection: str | None, skip_patterns: list[re.Pattern[str]]) -> list[Segment]:
    require_fitz()
    doc = fitz.open(pdf_path)
    pages = parse_pages(page_selection, doc.page_count)
    segments: list[Segment] = []
    for page_index in sorted(pages):
        page = doc[page_index]
        data = page.get_text("dict")
        line_no = 0
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [span for span in line.get("spans", []) if normalize_ws(span.get("text", ""))]
                if not spans:
                    continue
                text = normalize_ws("".join(span.get("text", "") for span in spans))
                bbox_values = line.get("bbox") or spans[0].get("bbox")
                bbox = tuple(float(v) for v in bbox_values)
                font_size = float(max((span.get("size", 9.0) for span in spans), default=9.0))
                color = int_color_to_rgb(spans[0].get("color"))
                rotation = line_rotation(line)
                char_budget = estimate_char_budget(bbox, font_size, rotation)
                skipped, reason = should_skip(text, skip_patterns)
                segments.append(
                    Segment(
                        id=f"p{page_index + 1:04d}-l{line_no:04d}",
                        page=page_index,
                        text=text,
                        bbox=bbox,  # type: ignore[arg-type]
                        font_size=font_size,
                        color=color,
                        skipped=skipped,
                        skip_reason=reason,
                        rotation=rotation,
                        char_budget=char_budget,
                        review_flags=build_review_flags(text, font_size, char_budget),
                    )
                )
                line_no += 1
    doc.close()
    return segments


def load_glossary(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def batches(items: list[Segment], batch_size: int) -> Iterable[list[Segment]]:
    if batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def extract_json_object(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def call_chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout: int,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature, "response_format": {"type": "json_object"}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"LLM request failed after retries: {last_error}")


def translate_segments(segments: list[Segment], args: argparse.Namespace) -> None:
    to_translate = [segment for segment in segments if not segment.skipped]
    for segment in segments:
        if segment.skipped:
            segment.translation = segment.text
    if not to_translate:
        return
    if not args.api_key:
        raise SystemExit(
            "No caller-provided translations JSON was supplied. Prefer: run --extract-only-json, "
            "fill each segment.translation with the invoking assistant's AI ability, then rerun with "
            "--translations-json. If the caller cannot provide AI translations, set OPENAI_API_KEY or pass --api-key."
        )
    if not args.model:
        raise SystemExit(
            "No fallback model was supplied. Prefer caller-translated --translations-json; otherwise set OPENAI_MODEL "
            "or pass --model for the OpenAI-compatible endpoint."
        )

    glossary = load_glossary(args.glossary)
    system = (
        "You are a precise professional PDF label translator. Translate only the provided segment text. "
        "Preserve numbers, units, equations, standards, citations, figure/table identifiers, and product/model names unless translation is clearly required. "
        "Keep translations concise enough to fit in the original label box and respect each segment max_chars when provided. Return strict JSON only."
    )
    context = {
        "target_language": args.target_language,
        "source_language": args.source_language,
        "additional_instructions": args.instructions,
        "glossary": glossary,
    }
    for batch in batches(to_translate, args.batch_size):
        payload = [
            {"id": segment.id, "text": segment.text, "max_chars": segment.char_budget, "rotation": segment.rotation}
            for segment in batch
        ]
        user = (
            "Translate these PDF text segments. Return JSON in exactly this shape: "
            '{"translations":[{"id":"...","translation":"..."}]}.\n\n'
            f"Context:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
            f"Segments:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        content = call_chat_completions(
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=args.temperature,
            timeout=args.timeout,
        )
        parsed = extract_json_object(content)
        translations = parsed.get("translations") if isinstance(parsed, dict) else None
        if not isinstance(translations, list):
            raise RuntimeError("LLM response did not contain a translations list")
        by_id = {item.get("id"): item.get("translation") for item in translations if isinstance(item, dict)}
        missing: list[str] = []
        for segment in batch:
            value = by_id.get(segment.id)
            if not isinstance(value, str) or not value.strip():
                missing.append(segment.id)
                continue
            segment.translation = normalize_ws(value)
        if missing:
            raise RuntimeError(f"LLM response omitted translations for: {missing}")


def cjk_ratio(segments: list[Segment]) -> float:
    candidate_segments = [segment for segment in segments if not segment.skipped]
    if not candidate_segments:
        return 0.0
    return sum(1 for segment in candidate_segments if contains_cjk(segment.text)) / len(candidate_segments)


def has_rotated_segments(segments: list[Segment]) -> bool:
    return any(segment.rotation in {90, 180, 270} for segment in segments if not segment.skipped)


def has_tight_boxes(segments: list[Segment]) -> bool:
    candidates = [segment.char_budget or 0 for segment in segments if not segment.skipped]
    if not candidates:
        return False
    tight = sum(1 for budget in candidates if budget and budget <= 12)
    return tight / len(candidates) >= 0.2


def has_long_lines(segments: list[Segment]) -> bool:
    return any(len(segment.text) >= 40 for segment in segments if not segment.skipped)


def has_spaced_cjk_segments(segments: list[Segment]) -> bool:
    return any(has_spaced_cjk(segment.text) for segment in segments if not segment.skipped)


def has_large_source_fonts(segments: list[Segment]) -> bool:
    return any(segment.font_size >= 18 for segment in segments if not segment.skipped)


def build_skip_regex_suggestions(segments: list[Segment]) -> list[str]:
    texts = [segment.text for segment in segments]
    suggestions: list[str] = []
    if any(re.fullmatch(r"[A-H]", text) for text in texts):
        suggestions.append(r"^[A-H]$")
    if any(re.fullmatch(r"\d{1,2}", text) for text in texts):
        suggestions.append(r"^\d{1,2}$")
    if any(re.fullmatch(r"\d+(?:\.\d+)?", text) for text in texts):
        suggestions.append(r"^\d+(?:\.\d+)?$")
    if any(re.search(r"\d{4}/\d{1,2}/\d{1,2}", text) for text in texts):
        suggestions.append(r"^\d{4}/\d{1,2}/\d{1,2}$")
    return suggestions


def recommend_args(segments: list[Segment], target_language: str) -> dict[str, Any]:
    reasons: list[str] = []
    tight = has_tight_boxes(segments)
    long_lines = has_long_lines(segments)
    rotated = has_rotated_segments(segments)
    spaced_cjk = has_spaced_cjk_segments(segments)
    large_fonts = has_large_source_fonts(segments)
    source_cjk_ratio = cjk_ratio(segments)
    target_scripts = script_requirements_for_language(target_language)
    target_needs_external_font = bool(target_scripts)

    min_font_size = 3.5 if tight else 5.0
    if tight:
        reasons.append("Many extracted labels have small char_budget; allow smaller font for fit.")
    if long_lines:
        reasons.append("Long technical requirement lines detected; review translations before applying.")
    if rotated:
        reasons.append("Right-angle rotated labels detected; extracted rotation will be reused automatically.")
    if spaced_cjk:
        reasons.append("Some CJK text contains spaces between characters; translate by meaning after removing intra-CJK spaces.")
    if large_fonts:
        reasons.append("Large source fonts detected; use auto redaction padding and inspect coverage for residual source glyphs.")
    if source_cjk_ratio >= 0.3 and re.search(r"english|英语", target_language, re.I):
        reasons.append("CJK source to English target usually expands text length; use concise terminology and slight box expansion.")

    max_box_scale = 1.2 if (tight or source_cjk_ratio >= 0.3 or large_fonts) else 1.0
    if max_box_scale > 1.0:
        reasons.append("Use slight centered box expansion to reduce clipping while keeping label position.")

    recommended: dict[str, Any] = {
        "target_language": target_language,
        "redact_fill": "auto",
        "min_font_size": min_font_size,
        "max_box_scale": max_box_scale,
        "bbox_pad": 0.75,
        "redact_pad": "auto",
        "fontfile": "auto" if target_needs_external_font else None,
        "font_scripts": sorted(target_scripts),
        "font_install_hint": font_install_hint(target_scripts),
        "review_translations_json": True,
        "skip_regex_suggestions": build_skip_regex_suggestions(segments),
        "reason": reasons or ["Default position-preserving settings are suitable."],
    }
    if target_needs_external_font:
        recommended["reason"].append(
            "Target language may require fonts beyond built-in Helvetica; install a supporting font and use --fontfile auto or a known font file."
        )
    return recommended


def write_segments_json(segments: list[Segment], path: str, target_language: str = "English") -> None:
    data = {
        "schema": "pdf-translate.segments.v1",
        "recommended_args": recommend_args(segments, target_language),
        "segments": [asdict(segment) for segment in segments],
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")




def target_is_english(target_language: str) -> bool:
    return bool(re.search(r"english|英语|英文", target_language, re.I))


def normalized_for_qa(text: str) -> str:
    return remove_intra_cjk_spaces(normalize_ws(text)).lower()


def likely_untranslated_reason(segment: Segment, target_language: str) -> str | None:
    if segment.skipped:
        return None
    translation = normalize_ws(segment.translation or "")
    source = normalize_ws(segment.text)
    if not translation:
        return "missing translation"
    if "NEEDS_REVIEW" in translation.upper():
        return "translation still contains NEEDS_REVIEW marker"
    if target_is_english(target_language) and contains_cjk(source) and contains_cjk(translation):
        return "target is English but translation still contains CJK characters"
    if contains_cjk(source) and normalized_for_qa(source) == normalized_for_qa(translation):
        return "translation is unchanged from CJK source text"
    return None

def load_segments_json(path: str) -> list[Segment]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("segments") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SystemExit("translations JSON must contain a segments list")
    segments: list[Segment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("translation"):
            item["translation"] = item.get("text", "")
        item["bbox"] = tuple(item["bbox"])
        item["color"] = tuple(item.get("color", (0.0, 0.0, 0.0)))
        item.setdefault("rotation", 0)
        item.setdefault("char_budget", None)
        item.setdefault("review_flags", build_review_flags(str(item.get("text", "")), float(item.get("font_size", 9.0)), item.get("char_budget")))
        segments.append(Segment(**item))
    return segments




def redact_pad_for_segment(segment: Segment, args: argparse.Namespace) -> float:
    if args.redact_pad is not None:
        if args.redact_pad < 0:
            raise SystemExit("--redact-pad must be >= 0")
        return args.redact_pad
    return max(args.bbox_pad, segment.font_size * 0.18)

def padded_rect(bbox: tuple[float, float, float, float], pad: float) -> Any:
    require_fitz()
    x0, y0, x1, y1 = bbox
    return fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def expand_rect(rect: Any, scale: float, page_rect: Any) -> Any:
    require_fitz()
    if scale <= 1.0:
        return rect & page_rect
    center = rect.tl + (rect.br - rect.tl) * 0.5
    width = rect.width * scale
    height = rect.height * scale
    expanded = fitz.Rect(center.x - width / 2, center.y - height / 2, center.x + width / 2, center.y + height / 2)
    return expanded & page_rect


def scale_steps(max_scale: float) -> list[float]:
    if max_scale < 1.0:
        raise SystemExit("--max-box-scale must be >= 1.0")
    steps = [1.0]
    scale = 1.1
    while scale <= max_scale + 1e-9:
        steps.append(round(scale, 2))
        scale += 0.1
    if max_scale not in steps:
        steps.append(max_scale)
    return sorted(set(steps))


def median_byte(values: list[int]) -> int:
    if not values:
        return 255
    values = sorted(values)
    return values[len(values) // 2]


def sample_background_color(page: Any, rect: Any, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    """Approximate the local background color from a border around a text box."""
    require_fitz()
    clip = rect & page.rect
    if clip.is_empty or clip.width < 1 or clip.height < 1:
        return fallback
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False, colorspace=fitz.csRGB)
    except Exception:
        return fallback
    if pix.width < 2 or pix.height < 2 or pix.n < 3:
        return fallback
    border = max(1, min(pix.width, pix.height) // 5)
    stride = max(1, int(math.sqrt((pix.width * pix.height) / 5000)))
    rs: list[int] = []
    gs: list[int] = []
    bs: list[int] = []
    data = pix.samples
    for y in range(0, pix.height, stride):
        for x in range(0, pix.width, stride):
            if not (x < border or y < border or x >= pix.width - border or y >= pix.height - border):
                continue
            offset = (y * pix.width + x) * pix.n
            rs.append(data[offset])
            gs.append(data[offset + 1])
            bs.append(data[offset + 2])
    if not rs:
        return fallback
    return (median_byte(rs) / 255.0, median_byte(gs) / 255.0, median_byte(bs) / 255.0)


def redact_fill_for(page: Any, rect: Any, value: str) -> tuple[float, float, float]:
    if value.strip().lower() == "auto":
        return sample_background_color(page, rect, fallback=(1.0, 1.0, 1.0))
    return hex_to_rgb(value)


def contains_non_latin(text: str) -> bool:
    return any(ord(char) > 0x024F for char in text)


def script_requirements_for_language(target_language: str) -> set[str]:
    value = target_language.lower()
    checks = {
        "cjk": r"chinese|mandarin|cantonese|中文|汉语|漢語|日文|日语|japanese|korean|韩文|韩语|한국|조선|cjk",
        "arabic": r"arabic|عربي|persian|farsi|فارسی|urdu|اردو",
        "devanagari": r"hindi|हिन्दी|devanagari|sanskrit|marathi|nepali",
        "thai": r"thai|ภาษาไทย|泰文|泰语",
        "hebrew": r"hebrew|עברית|希伯来",
        "cyrillic": r"russian|русский|ukrainian|україн|bulgarian|serbian|cyrillic|俄文|俄语",
        "greek": r"greek|ελλην|希腊",
    }
    return {script for script, pattern in checks.items() if re.search(pattern, value, re.I)}


def script_requirements_for_text(text: str) -> set[str]:
    scripts: set[str] = set()
    for char in text:
        code = ord(char)
        if (
            0x3400 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            scripts.add("cjk")
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
            scripts.add("arabic")
        elif 0x0900 <= code <= 0x097F:
            scripts.add("devanagari")
        elif 0x0E00 <= code <= 0x0E7F:
            scripts.add("thai")
        elif 0x0590 <= code <= 0x05FF:
            scripts.add("hebrew")
        elif 0x0400 <= code <= 0x052F:
            scripts.add("cyrillic")
        elif 0x0370 <= code <= 0x03FF:
            scripts.add("greek")
    return scripts


def script_display_name(script: str) -> str:
    return {
        "cjk": "Chinese/Japanese/Korean",
        "arabic": "Arabic/Persian/Urdu",
        "devanagari": "Devanagari/Hindi",
        "thai": "Thai",
        "hebrew": "Hebrew",
        "cyrillic": "Cyrillic",
        "greek": "Greek",
    }.get(script, script)


def required_scripts_for_segments(segments: list[Segment], target_language: str = "") -> set[str]:
    scripts = script_requirements_for_language(target_language)
    for segment in segments:
        if segment.skipped:
            continue
        scripts.update(script_requirements_for_text(segment.translation or ""))
    return scripts


def contains_cjk(text: str) -> bool:
    return any(
        "\u3400" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\u3040" <= char <= "\u30ff"
        or "\uac00" <= char <= "\ud7af"
        for char in text
    )


def font_name_looks_cjk(path: str | None) -> bool:
    if not path:
        return False
    return bool(re.search(r"CJK|SourceHan|NotoSans[STJKC]|NotoSerif[STJKC]|WenQuan|wqy|DroidSansFallback", path, re.I))


def font_name_looks_suitable(path: str | None, script: str) -> bool:
    if not path:
        return False
    patterns = {
        "cjk": r"CJK|SourceHan|NotoSans[STJKC]|NotoSerif[STJKC]|WenQuan|wqy|DroidSansFallback|PingFang|Hiragino|YuGoth",
        "arabic": r"Arabic|Naskh|Kufi|Amiri|Scheherazade|DejaVuSans|NotoSans-Regular",
        "devanagari": r"Devanagari|Lohit-Devanagari|Mangal|Kokila",
        "thai": r"Thai|Garuda|Laksaman|Norasi|Tlwg",
        "hebrew": r"Hebrew|David|Miriam|DejaVuSans|NotoSans-Regular",
        "cyrillic": r"Cyrillic|DejaVuSans|NotoSans-Regular|LiberationSans",
        "greek": r"Greek|DejaVuSans|NotoSans-Regular|LiberationSans",
    }
    return bool(re.search(patterns.get(script, r"Noto|DejaVu|Liberation"), path, re.I))


def font_install_hint(required_scripts: set[str]) -> str:
    scripts = sorted(required_scripts)
    if not scripts:
        return "No extra font package is usually required for English/Latin output with built-in Helvetica."
    apt_packages: set[str] = set()
    dnf_packages: set[str] = set()
    brew_packages: set[str] = set()
    manual_names: set[str] = set()
    for script in scripts:
        if script == "cjk":
            apt_packages.add("fonts-noto-cjk")
            dnf_packages.add("google-noto-sans-cjk-fonts")
            brew_packages.add("font-noto-sans-cjk")
            manual_names.add("Noto Sans CJK / Source Han Sans")
        elif script == "arabic":
            apt_packages.update({"fonts-noto-core", "fonts-noto-extra"})
            dnf_packages.add("google-noto-sans-arabic-fonts")
            brew_packages.add("font-noto-sans-arabic")
            manual_names.add("Noto Sans Arabic / Amiri")
        elif script == "devanagari":
            apt_packages.update({"fonts-noto-core", "fonts-deva-extra"})
            dnf_packages.add("google-noto-sans-devanagari-fonts")
            brew_packages.add("font-noto-sans-devanagari")
            manual_names.add("Noto Sans Devanagari")
        elif script == "thai":
            apt_packages.update({"fonts-noto-core", "fonts-thai-tlwg"})
            dnf_packages.add("google-noto-sans-thai-fonts")
            brew_packages.add("font-noto-sans-thai")
            manual_names.add("Noto Sans Thai")
        elif script == "hebrew":
            apt_packages.add("fonts-noto-core")
            dnf_packages.add("google-noto-sans-hebrew-fonts")
            brew_packages.add("font-noto-sans-hebrew")
            manual_names.add("Noto Sans Hebrew")
        elif script in {"cyrillic", "greek"}:
            apt_packages.add("fonts-noto-core")
            dnf_packages.add("google-noto-sans-fonts")
            brew_packages.add("font-noto-sans")
            manual_names.add("Noto Sans / DejaVu Sans")
    return (
        f"Required scripts: {', '.join(script_display_name(script) for script in scripts)}. "
        f"Install a supporting font before applying translations, e.g. Ubuntu/Debian: "
        f"sudo apt-get update && sudo apt-get install -y {' '.join(sorted(apt_packages))}; "
        f"Fedora/RHEL: sudo dnf install -y {' '.join(sorted(dnf_packages))}; "
        f"macOS Homebrew: brew install --cask {' '.join(sorted(brew_packages))}; "
        f"Windows/manual: install {', '.join(sorted(manual_names))}, then pass the .ttf/.otf/.ttc path with --fontfile."
    )


def font_candidates_for_script(script: str) -> tuple[list[str], list[str]]:
    candidates_by_script = {
        "cjk": [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
            "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ],
        "arabic": [
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
            "/usr/share/fonts/truetype/amiri/amiri-regular.ttf",
        ],
        "devanagari": [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
        ],
        "thai": [
            "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
            "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
        ],
        "hebrew": [
            "/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
        "cyrillic": [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ],
        "greek": [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ],
    }
    patterns_by_script = {
        "cjk": ["*CJK*", "*SourceHan*", "*NotoSansSC*", "*NotoSansJP*", "*NotoSansKR*", "*WenQuan*"],
        "arabic": ["*Arabic*", "*Naskh*", "*Amiri*", "*Kufi*"],
        "devanagari": ["*Devanagari*", "*Lohit-Devanagari*", "*Mangal*"],
        "thai": ["*Thai*", "*Garuda*", "*Laksaman*", "*Tlwg*"],
        "hebrew": ["*Hebrew*", "*David*", "*Miriam*", "*DejaVuSans.ttf"],
        "cyrillic": ["*NotoSans-Regular.ttf", "*DejaVuSans.ttf", "*LiberationSans-Regular.ttf"],
        "greek": ["*NotoSans-Regular.ttf", "*DejaVuSans.ttf", "*LiberationSans-Regular.ttf"],
    }
    return candidates_by_script.get(script, []), patterns_by_script.get(script, [])


def find_auto_fontfile(required_scripts: set[str] | None = None) -> str | None:
    required_scripts = required_scripts or set()
    script_priority = ["cjk", "arabic", "devanagari", "thai", "hebrew", "cyrillic", "greek"]
    candidate_scripts = [script for script in script_priority if script in required_scripts] or ["cjk", "arabic", "devanagari", "thai", "hebrew", "cyrillic", "greek"]
    candidates: list[str] = []
    patterns: list[str] = []
    for script in candidate_scripts:
        script_candidates, script_patterns = font_candidates_for_script(script)
        candidates.extend(script_candidates)
        patterns.extend(script_patterns)
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists() and (not required_scripts or any(font_name_looks_suitable(candidate, script) for script in required_scripts)):
            return candidate
    for root in ("/usr/share/fonts", str(Path.home() / ".local/share/fonts"), str(Path.home() / ".fonts")):
        root_path = Path(root)
        if not root_path.exists():
            continue
        for pattern in patterns or ("*CJK*", "*SourceHan*", "*NotoSansSC*", "*WenQuan*", "*DejaVuSans.ttf", "*NotoSans-Regular.ttf"):
            for candidate in root_path.rglob(pattern):
                candidate_s = str(candidate)
                if candidate.suffix.lower() in {".ttf", ".otf", ".ttc"} and (
                    not required_scripts or any(font_name_looks_suitable(candidate_s, script) for script in required_scripts)
                ):
                    return str(candidate)
    return None


def resolve_font_args(
    fontname: str,
    fontfile: str | None,
    required_scripts: set[str],
    allow_missing_glyphs: bool = False,
) -> tuple[str, str | None, list[str]]:
    warnings: list[str] = []
    requested_auto = bool(fontfile and fontfile.lower() == "auto")
    should_auto_detect = requested_auto or (required_scripts and not fontfile)
    resolved_file = fontfile
    if should_auto_detect:
        resolved_file = find_auto_fontfile(required_scripts)
        script_note = ", ".join(script_display_name(script) for script in sorted(required_scripts)) or "broad Unicode output"
        if resolved_file:
            prefix = "Using auto-detected" if requested_auto else "Auto-selected"
            warnings.append(f"{prefix} fontfile for {script_note}: {resolved_file}")
        elif requested_auto or required_scripts:
            message = (
                "No local font was found for non-Latin translated text. Refusing to write a PDF that would likely "
                f"render as '?' or tofu boxes. {font_install_hint(required_scripts)} "
                "Install a supporting font and rerun, or pass --allow-missing-glyphs to override."
            )
            if allow_missing_glyphs:
                warnings.append(message)
                resolved_file = None
            else:
                raise SystemExit(message)
    if resolved_file and not Path(resolved_file).exists():
        raise SystemExit(f"Font file does not exist: {resolved_file}")
    if resolved_file:
        for script in sorted(required_scripts):
            if not font_name_looks_suitable(resolved_file, script):
                warnings.append(
                    f"Font file may not support {script_display_name(script)} glyphs: {resolved_file}. "
                    f"{font_install_hint({script})}"
                )
    complex_scripts = required_scripts & {"arabic", "devanagari", "thai", "hebrew"}
    if complex_scripts:
        warnings.append(
            "Complex-script output may require shaping/ligature support beyond basic glyph coverage; "
            f"inspect rendered {', '.join(script_display_name(script) for script in sorted(complex_scripts))} text carefully."
        )
    resolved_name = fontname
    if resolved_file and fontname == "helv":
        # Built-in names ignore fontfile in PyMuPDF; use a custom resource name.
        resolved_name = "pdftranslatefont"
    return resolved_name, resolved_file, warnings


def insert_fitted_text(
    page: Any,
    rect: Any,
    text: str,
    base_size: float,
    min_size: float,
    color: tuple[float, float, float],
    fontname: str,
    fontfile: str | None,
    rotation: int,
    max_box_scale: float,
) -> tuple[bool, float, float]:
    for scale in scale_steps(max_box_scale):
        candidate = expand_rect(rect, scale, page.rect)
        size = max(base_size, min_size)
        while size >= min_size:
            spare = page.insert_textbox(
                candidate,
                text,
                fontsize=size,
                fontname=fontname,
                fontfile=fontfile,
                color=color,
                align=1,
                rotate=rotation,
            )
            if spare >= 0:
                return True, size, scale
            # PyMuPDF does not draw on negative spare.
            size -= 0.5
    final_rect = expand_rect(rect, max_box_scale, page.rect)
    page.insert_textbox(
        final_rect, text, fontsize=min_size, fontname=fontname, fontfile=fontfile, color=color, align=1, rotate=rotation
    )
    return False, min_size, max_box_scale


def apply_translations(input_pdf: str, output_pdf: str, segments: list[Segment], args: argparse.Namespace) -> list[str]:
    require_fitz()
    doc = fitz.open(input_pdf)
    required_scripts = required_scripts_for_segments(segments, args.target_language)
    fontname, fontfile, warnings = resolve_font_args(
        args.fontname, args.fontfile, required_scripts, allow_missing_glyphs=args.allow_missing_glyphs
    )
    if required_scripts and not fontfile and args.allow_missing_glyphs:
        warnings.append(
            "Translations require non-Latin font support but no usable fontfile was selected; output may render as '?' or tofu boxes. "
            f"Use --fontfile auto after installing fonts, or pass a known font file. {font_install_hint(required_scripts)}"
        )

    by_page: dict[int, list[Segment]] = {}
    for segment in segments:
        by_page.setdefault(segment.page, []).append(segment)

    if not args.keep_original:
        for page_index, page_segments in by_page.items():
            page = doc[page_index]
            for segment in page_segments:
                if segment.skipped:
                    continue
                rect = padded_rect(segment.bbox, redact_pad_for_segment(segment, args))
                page.add_redact_annot(rect, fill=redact_fill_for(page, rect, args.redact_fill))
            page.apply_redactions()

    for page_index, page_segments in by_page.items():
        page = doc[page_index]
        for segment in page_segments:
            if segment.skipped:
                continue
            translation = segment.translation or segment.text
            untranslated_reason = likely_untranslated_reason(segment, args.target_language)
            if untranslated_reason:
                warnings.append(f"{segment.id}: QA possible untranslated text: {untranslated_reason}; source={segment.text!r}; translation={translation!r}")
            rect = padded_rect(segment.bbox, args.bbox_pad)
            if contains_non_latin(translation) and not fontfile:
                warnings.append(
                    f"{segment.id}: translation contains non-Latin characters; pass --fontfile /path/to/font.ttf or --fontfile auto if glyphs render incorrectly"
                )
            if contains_cjk(translation) and fontfile and not font_name_looks_cjk(fontfile):
                warnings.append(
                    f"{segment.id}: translation contains CJK text but fontfile may not cover CJK glyphs: {fontfile}"
                )
            ok, used_size, used_scale = insert_fitted_text(
                page,
                rect,
                translation,
                segment.font_size,
                args.min_font_size,
                segment.color,
                fontname,
                fontfile,
                segment.rotation,
                args.max_box_scale,
            )
            if used_scale > 1.0:
                warnings.append(f"{segment.id}: expanded box around original center by {used_scale:.2f}x to fit translation")
            if segment.font_size > 0 and used_size / segment.font_size < args.warn_font_scale:
                warnings.append(
                    f"{segment.id}: inserted font shrank to {used_size:.1f}pt from source {segment.font_size:.1f}pt; "
                    "shorten the translation or increase --max-box-scale to avoid tiny overlapping-looking labels"
                )
            if not ok:
                warnings.append(
                    f"{segment.id}: translation may not fit box at min font size {used_size}: {translation!r}"
                )

    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    if args.fail_on_untranslated:
        failures = [warning for warning in warnings if "QA possible untranslated text" in warning]
        if failures:
            for warning in warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
            raise SystemExit(f"QA detected {len(failures)} likely untranslated segments; fix translations JSON and rerun")
    return warnings


def main() -> int:
    args = parse_args()
    if args.translations_json:
        segments = load_segments_json(args.translations_json)
    else:
        skip_patterns = compile_skip_patterns(args.skip_regex)
        segments = extract_segments(args.input_pdf, args.pages, skip_patterns)
        if not segments:
            raise SystemExit("No selectable text found. This PDF may be scanned and require OCR first.")
        if args.extract_only_json:
            for segment in segments:
                segment.translation = segment.text
            write_segments_json(segments, args.extract_only_json, args.target_language)
            print(f"Wrote extraction JSON: {args.extract_only_json}", file=sys.stderr)
            return 0
        translate_segments(segments, args)

    if args.dry_run_json:
        write_segments_json(segments, args.dry_run_json, args.target_language)
        print(f"Wrote translation review JSON: {args.dry_run_json}", file=sys.stderr)
        return 0

    warnings = apply_translations(args.input_pdf, args.output_pdf, segments, args)
    print(f"Wrote translated PDF: {args.output_pdf}", file=sys.stderr)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
