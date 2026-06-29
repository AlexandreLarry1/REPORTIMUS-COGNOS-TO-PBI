"""LLM layout improvement: rewrites positions + titles for each report page.

One LLM call per page so each result is small, focused, and traceable in Langfuse
without flooding the trace history. Produces:
  - A styled header textbox (primary-color band at y=0)
  - Rewritten visual titles in the source language
  - Improved grid positions (charts grouped, tables full-width, slicers on top)

Output written to intermediate/layout_pages.json.
"""
import datetime
import json
import pathlib
import re
import sys

from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import observability as obs
from utils import call_api_azure, strip_json_fences

CANVAS_W = 1280
HEADER_H = 70
MARGIN = 8

_SYSTEM_PROMPT = """\
You are an expert Power BI dashboard designer. For each page you receive, \
improve the layout and rewrite visual titles to be clear and professional.

## Input
- Page name, canvas width (always 1280px)
- Visuals: id, pbi_type, cognos_title, current width, current height

## Layout rules
- Canvas width: 1280px (fixed). Canvas height: you decide (min 720px).
- Reserve y=0..69 for the header band (managed externally — do NOT include it \
  in the visuals array).
- All visuals: y >= 80 (header + 10px gap).
- Margins: 8px minimum between visuals.
- Slicers: full width 1280px, height 70px, place right below header (y=80).
- Tables / matrix: full width 1280px, height 320-400px.
- Charts: 2-3 per row.
  - 2 charts: width 632px each  (= (1280-16)/2)
  - 3 charts: width 418px each  (= (1280-24)/3)
  - Height 280-320px.
- KPI cards: 4 per row, width 308px, height 140px.
- Group similar visual types on the same row.
- No visual overflows canvas width.

## Title rules
- Rewrite Cognos internal names (e.g. "Floating bar1") into clear business labels.
- Use the SAME language as the input titles.
- Concise: max 6 words.
- "header_text": page title rewritten as a short dashboard title.
- "header_subtitle": one short phrase describing page content, or null.

## Output — valid JSON only, no markdown fences:
{
  "page_name": "<page name>",
  "header_text": "<main header title>",
  "header_subtitle": "<subtitle or null>",
  "canvas_height": <integer>,
  "visuals": [
    {
      "visual_id": "<id from input>",
      "title": "<rewritten title>",
      "x": <integer>,
      "y": <integer>,
      "width": <integer>,
      "height": <integer>
    }
  ]
}
"""


def _build_page_prompt(sheet: dict) -> str:
    lines = [
        f"Page: {sheet.get('title', 'Page')}",
        f"Canvas width: {CANVAS_W}px",
        "",
        "Visuals (id | pbi_type | cognos_title | current_w | current_h):",
    ]
    for obj in sheet.get("objects", []):
        layout = obj.get("layout", {})
        w = layout.get("colspan", 4)
        h = layout.get("rowspan", 3)
        lines.append(
            f"  {obj['id']} | {obj.get('type', 'card')} | {obj.get('title', obj['id'])} "
            f"| w={w} | h={h}"
        )
    lines.append("")
    lines.append("Produce the JSON layout for this page.")
    return "\n".join(lines)


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w]", "_", name)[:30]


def _save_page_trace(
    output_dir: pathlib.Path,
    page_name: str,
    system: str,
    user: str,
    raw: str,
    parsed: dict,
) -> None:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_dir = output_dir / "llm_traces" / f"layout_{_sanitize(page_name)}_{ts}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "system_prompt.txt").write_text(system, encoding="utf-8")
    (trace_dir / "user_prompt.txt").write_text(user, encoding="utf-8")
    (trace_dir / "raw_response.txt").write_text(raw, encoding="utf-8")
    (trace_dir / "parsed_output.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"   Layout trace -> {trace_dir.name}")


def _call_page(system: str, user: str, span=None) -> str:
    return call_api_azure(system, user, span, "layout_translation_llm")


def run(
    visual_extraction: dict,
    output_dir: pathlib.Path,
    mode: str = "api",
    trace=None,
) -> list[dict]:
    """Run one LLM call per page, return list of page layout specs.

    Each page call gets its own Langfuse child span and its own trace dir
    so the history stays readable.

    Args:
        visual_extraction: dict from visual_extraction.json
        output_dir: intermediate/ dir (trace files written here)
        mode: "api" or "paste"
        trace: parent Langfuse trace/span

    Returns:
        List of page layout dicts (one per sheet)
    """
    sheets = visual_extraction.get("sheets", [])
    layout_pages: list[dict] = []

    parent_span = (trace or obs._Noop()).span(name="Layout_LLM")

    for sheet in sheets:
        page_name = sheet.get("title", "Page")
        objects = [o for o in sheet.get("objects", []) if "error" not in o]
        if not objects:
            continue

        page_span = parent_span.span(name=f"Layout_Page_{_sanitize(page_name)}")
        user = _build_page_prompt(sheet)

        if mode == "paste":
            prompt_path = output_dir / f"layout_prompt_{_sanitize(page_name)}.txt"
            prompt_path.write_text(
                f"SYSTEM:\n{_SYSTEM_PROMPT}\n\nUSER:\n{user}", encoding="utf-8"
            )
            out_path = output_dir / f"layout_response_{_sanitize(page_name)}.json"
            print(f"Layout prompt -> {prompt_path}")
            print(f"Paste response into: {out_path}")
            input(f"Press Enter once {out_path.name} is ready...")
            raw = out_path.read_text(encoding="utf-8")
        else:
            raw = _call_page(_SYSTEM_PROMPT, user, page_span)

        try:
            parsed = json.loads(strip_json_fences(raw))
        except json.JSONDecodeError as e:
            print(f"   [layout] JSON parse error on page '{page_name}': {e} — skipped")
            page_span.end()
            continue

        _save_page_trace(output_dir, page_name, _SYSTEM_PROMPT, user, raw, parsed)
        layout_pages.append(parsed)
        print(f"   Layout '{page_name}': {len(parsed.get('visuals', []))} visuals repositioned")
        page_span.end()

    parent_span.end()

    out_path = output_dir / "layout_pages.json"
    out_path.write_text(json.dumps(layout_pages, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   Layout pages -> {out_path} ({len(layout_pages)} pages)")
    return layout_pages
