"""LLM viz wiring: maps Cognos visual_extraction → PBI visual_wiring.json.

Receives visual_extraction.json (IDs, types, Cognos field names) + BIM
measure/column inventory and produces explicit well assignments for each
visual so pbip_generator.wire_from_spec() can build prototypeQuery.

Output contract (visual_wiring.json):
[
  {
    "visual_id": "Crosstab1",
    "pbi_type": "matrix",
    "wells": {
      "Rows":    ["dim_accounts[Account_Level1]", "dim_accounts[Account_Level2]"],
      "Columns": ["dim_versions[Version_Label]"],
      "Values":  ["fact_data[Actual]", "fact_data[Budget]", "fact_data[Variance]"]
    }
  },
  {
    "visual_id": "slicer_p_timeview",
    "pbi_type": "slicer",
    "wells": { "Field": ["Param_p_timeview[p_timeview]"] }
  }
]

Field syntax:
  - Measure  → "table[Measure Name]"   (table = table where measure lives)
  - Column   → "table[column_name]"
  - Slicer fields already resolved in visual_extraction → passed through
"""
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


# ---------------------------------------------------------------------------
# PBI well constraints
# ---------------------------------------------------------------------------

_WELL_RULES = {
    # PBI internal projection key names — used verbatim in report.json
    "matrix":                        "Rows, Columns, Values",
    "tableEx":                        "Values",
    "card":                           "Values (1 measure max)",
    "multiRowCard":                   "Values",
    "slicer":                         "Field",
    "lineChart":                      "Category, Y, Series (opt)",
    "areaChart":                      "Category, Y, Series (opt)",
    "clusteredBarChart":              "Category, Y, Series (opt)",
    "clusteredColumnChart":           "Category, Y, Series (opt)",
    "stackedBarChart":                "Category, Y, Series (opt)",
    "stackedColumnChart":             "Category, Y, Series (opt)",
    "pieChart":                       "Category, Y",
    "donutChart":                     "Category, Y",
    "waterfallChart":                 "Category, Y, Breakdown (opt)",
    "treemap":                        "Group, Values",
    "scatterChart":                   "X, Y, Details, Size (opt)",
    "lineClusteredColumnComboChart":           "Category, Y, Y2, Series (opt)",
    "stackedAreaChart":                        "Category, Y, Series (opt)",
    "hundredPercentStackedColumnChart":        "Category, Y, Series (opt)",
    "hundredPercentStackedBarChart":           "Category, Y, Series (opt)",
    "gauge":                          "Value, TargetValue (opt), MinValue (opt), MaxValue (opt)",
    "map":                            "Location, Size (opt), Color (opt)",
}

# IBM slot → PBI projection key (exact internal names)
_SLOT_TO_WELL = {
    "categories": "Category",
    "values":     "Y",
    "color":      "Series",
    "size":       "Size",
    "yStart":     "Y",       # range start collapsed into Y for approximated charts
    "x":          "X",
    "y":          "Y",
    "latitude":   "Location",
    "longitude":  "Location",
    "group":      "Series",
    "series":     "Series",
    "target":     "TargetValue",
}

_SYSTEM_PROMPT = """\
You are an expert Power BI report builder. Your task is to wire Cognos visual \
objects to Power BI well assignments.

You receive:
1. A list of Cognos visuals — each with id, pbi_type, ibm_type (original Cognos \
   chart type), slots (IBM axis roles → field names), and available_fields
2. The full BIM inventory: measures and columns available in the semantic model
3. PBI projection key names per visual type (these are EXACT internal PBI keys — \
   use them verbatim in your output wells)

Your job: for each visual, produce explicit well assignments using only fields \
that exist in the BIM inventory.

## CRITICAL: PBI projection key names (use EXACTLY these strings as well names)
- matrix                       → Rows, Columns, Values
- tableEx                      → Values
- card / multiRowCard          → Values
- slicer                       → Field
- lineChart, areaChart,
  clusteredBarChart/ColumnChart,
  stackedBarChart/ColumnChart,
  pieChart, donutChart,
  waterfallChart                → Category (x-axis/legend), Y (measures)
                                   Series (optional groupBy column)
- waterfallChart               → Category, Y, Breakdown (opt)
- scatterChart                 → X (measure), Y (measure), Details (category col), \
                                  Size (measure, opt)
- lineClusteredColumnComboChart → Category, Y (column measures), Y2 (line measures), \
                                   Series (opt)
- treemap                      → Group (category cols), Values (measure)
- gauge                        → Value (measure), TargetValue, MinValue, MaxValue (opt)
- map                          → Location (geo col), Size (measure, opt), Color (opt)
- stackedAreaChart             → Category (time col), Y (measures), Series (opt groupby)
- hundredPercentStackedColumnChart → Category, Y, Series (opt)
- matrix (heatmap approx)      → Rows (row dim), Columns (col dim), Values (measure)

## IBM slot → PBI well mapping
- categories → Category  (or Group for treemap, Location for map, Details for scatter)
- values     → Y         (or Values for treemap/tableEx)
- color      → Series    (NEVER Details — color is always a color/legend grouping)
- size       → Size
- yStart     → Y         (collapse into Y for approximated charts)
- x / y      → X / Y
- series     → Series
- target     → TargetValue

## Field name syntax
  Measure : "table_name[Measure Name]"
  Column  : "table_name[column_name]"
  Use EXACT table/column names from the BIM inventory.

## Rules
1. Match IBM available_fields/slot field names to BIM fields by label similarity.
2. Only use fields that exist in the BIM inventory. Drop unknowns silently.
3. Slicers: slicer_field is already resolved — copy it unchanged into Field well.
4. If a visual has a migration_note, copy it unchanged into your output AND still wire \
   all wells using available slots — migration_note is informational only, never skip wiring.
5. tableEx fallback: put all available_fields that exist in BIM into Values.
6. For measures: check if a "_Measures" table exists in the BIM inventory — \
   calculated measures live there, not in data tables.
7. CRITICAL — categorical wells (Category, Details, Group, Rows, Columns, Series, \
   Location, Field, Breakdown) MUST use table[column] from a real data table, NEVER \
   _Measures[...]. _Measures contains only numeric DAX measures — using them as \
   categories produces empty visuals. For example: Category → "customer_analysis[Month]", \
   Details → "customer_analysis[Employment Status]".
8. heatmap → matrix slot mapping: categories → Rows, series → Columns, \
   color → Values (color is the scalar measurement in a heatmap, NOT a grouping/Series).

Return ONLY a valid JSON array (no markdown, no comments):
[
  {
    "visual_id": "<id from input>",
    "pbi_type": "<pbi_type from input>",
    "wells": { "<ExactPBIKeyName>": ["table[field]", ...] },
    "migration_note": "<note or null>"
  }
]
"""


def _build_bim_inventory(bim_path: pathlib.Path) -> dict:
    """Extract measures and columns from model.bim → compact inventory for LLM."""
    bim = json.loads(bim_path.read_text(encoding="utf-8"))
    inventory: dict[str, list[str]] = {}  # {table: [fields]}

    for table in bim["model"]["tables"]:
        tname = table["name"]
        fields: list[str] = []
        for col in table.get("columns", []):
            fields.append(f"[col] {col['name']}")
        for meas in table.get("measures", []):
            fields.append(f"[meas] {meas['name']}")
        if fields:
            inventory[tname] = fields

    return inventory


def _build_slicer_filter_map(visual_extraction: dict, xml_data: dict) -> dict[str, list[str]]:
    """Map visual_id → list of slicer_ids that filter it, derived from query detailFilters.

    A visual is filtered by a slicer when its query has a detailFilter referencing
    the slicer's param_name via ?param?.
    """
    # param_name → slicer visual_id
    param_to_slicer: dict[str, str] = {}
    for sheet in visual_extraction.get("sheets", []):
        for obj in sheet.get("objects", []):
            if obj.get("type") == "slicer" and obj.get("param_name"):
                param_to_slicer[obj["param_name"]] = obj["id"]

    if not param_to_slicer:
        return {}

    _PARAM_RE = re.compile(r'\?(\w+)\?')
    queries = xml_data.get("queries", {})
    visual_filter_map: dict[str, list[str]] = {}

    for sheet in visual_extraction.get("sheets", []):
        for obj in sheet.get("objects", []):
            if obj.get("type") == "slicer":
                continue
            q_name = obj.get("query", "")
            if not q_name:
                continue
            for flt in queries.get(q_name, {}).get("filters", []):
                params_in_filter = _PARAM_RE.findall(flt.get("expression", ""))
                for p in params_in_filter:
                    if p in param_to_slicer:
                        visual_filter_map.setdefault(obj["id"], []).append(
                            param_to_slicer[p]
                        )

    return visual_filter_map


def build_user_prompt(
    visual_extraction: dict,
    bim_inventory: dict,
    xml_data: dict | None = None,
) -> str:
    lines: list[str] = []

    lines.append("## BIM inventory (tables → columns [col] and measures [meas]):\n")
    for table, fields in bim_inventory.items():
        lines.append(f"Table '{table}':")
        for f in fields:
            lines.append(f"  {f}")
    lines.append("")

    # Slicer → visual filter connections (from detailFilters on queries)
    if xml_data:
        filter_map = _build_slicer_filter_map(visual_extraction, xml_data)
        if filter_map:
            lines.append("## Slicer → visual filter connections:\n")
            lines.append(
                "These visuals are filtered by the listed slicer(s) via Cognos detailFilter. "
                "In PBI the slicer field MUST come from the same table as the visual's data "
                "so that cross-filter context is applied automatically. "
                "Resolve the slicer Field well to the matching BIM column in the data table.\n"
            )
            for visual_id, slicer_ids in filter_map.items():
                lines.append(f"  Visual '{visual_id}' filtered by slicer(s): {slicer_ids}")
            lines.append("")

    lines.append("## Cognos visuals to wire:\n")
    for sheet in visual_extraction.get("sheets", []):
        sheet_title = sheet.get("title", "")
        for obj in sheet.get("objects", []):
            pbi_type = obj["type"]
            ibm_type = obj.get("ibm_type", "")
            lines.append(f"Visual id={obj['id']}  pbi_type={pbi_type}  ibm_type={ibm_type}  page={sheet_title}")

            if pbi_type == "slicer":
                lines.append(f"  slicer_field (Cognos ref, resolve to BIM column): {obj.get('slicer_field', '')}")
                if obj.get("param_name"):
                    lines.append(f"  param_name: {obj['param_name']} — find matching column in BIM for Field well")
                if obj.get("default_value"):
                    lines.append(f"  default_value: {obj['default_value']}")

            elif pbi_type == "matrix":
                lines.append(f"  row_dimensions: {obj.get('row_dimensions', [])}")
                lines.append(f"  col_dimensions: {obj.get('col_dimensions', [])}")
                lines.append(f"  measures: {[m['name'] for m in obj.get('measures', [])]}")

            elif pbi_type in ("card", "multiRowCard"):
                meas_labels = [m.get("label", "") for m in obj.get("measures", [])]
                lines.append(f"  available fields: {meas_labels}")

            else:
                # Chart or tableEx — pass slots and available fields
                slots = obj.get("slots", {})
                if slots:
                    lines.append(f"  IBM slots (role → fields): {slots}")
                available = obj.get("available_fields") or obj.get("columns", [])
                if available:
                    lines.append(f"  available_fields: {available}")

            # Reference lines / baselines
            if obj.get("baselines"):
                for bl in obj["baselines"]:
                    label = (obj.get("text_labels") or [""])[0]
                    lines.append(
                        f"  baseline: color={bl['line_color']} style={bl['line_style']} "
                        f"ref_query={bl['ref_query']}"
                        + (f" label={label}" if label else "")
                    )

            if obj.get("migration_note"):
                lines.append(f"  migration_note: {obj['migration_note']}")

            lines.append("")

    lines.append("Wire each visual. Return the JSON array.")
    return "\n".join(lines)


def call_api(system: str, user: str, trace=None) -> str:
    return call_api_azure(system, user, trace, "viz_translation_llm")


def parse_response(raw: str) -> list[dict]:
    return json.loads(strip_json_fences(raw))


def _write_migration_report(report_path: pathlib.Path, wiring: list[dict]) -> None:
    """Agrège les migration_note de tous les visuels → migration_report.json."""
    notes = [
        {"visual_id": v["visual_id"], "pbi_type": v["pbi_type"], "note": v["migration_note"]}
        for v in wiring
        if v.get("migration_note")
    ]
    report = {"total_visuals": len(wiring), "migration_warnings": len(notes), "visuals": notes}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if notes:
        print(f"   Migration report -> {report_path} ({len(notes)} warnings)")


def _save_trace(
    output_path: pathlib.Path,
    system: str,
    user: str,
    raw: str,
    parsed: list,
    mode: str,
) -> None:
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_dir = output_path.parent / "llm_traces" / f"viz_{ts}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "system_prompt.txt").write_text(system, encoding="utf-8")
    (trace_dir / "user_prompt.txt").write_text(user, encoding="utf-8")
    (trace_dir / "raw_response.txt").write_text(raw, encoding="utf-8")
    (trace_dir / "parsed_output.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"   VIZ LLM trace -> {trace_dir}")


def run(
    visual_extraction: dict,
    bim_path: pathlib.Path,
    output_path: pathlib.Path,
    mode: str = "api",
    trace=None,
    xml_data: dict | None = None,
) -> list[dict]:
    """Run the viz LLM call.

    Args:
        visual_extraction: dict from visual_extraction.json
        bim_path: path to model.bim (for BIM inventory)
        output_path: where to write visual_wiring.json
        mode: "api" or "paste"
        trace: observability trace
        xml_data: full Cognos extraction (for filter connections)

    Returns:
        List of visual wiring specs
    """
    bim_inventory = _build_bim_inventory(bim_path)
    system = _SYSTEM_PROMPT
    user = build_user_prompt(visual_extraction, bim_inventory, xml_data=xml_data)

    if mode == "paste":
        prompt_path = output_path.parent / "viz_wiring_prompt.txt"
        prompt_path.write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
        print(f"VIZ prompt -> {prompt_path}")
        print(f"Paste response into: {output_path}")
        input("Press Enter once the file is created...")
        raw = output_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        _save_trace(output_path, system, user, raw, parsed, mode)
        return parsed

    raw = call_api(system, user, trace)
    parsed = parse_response(raw)
    output_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"VIZ wiring -> {output_path} ({len(parsed)} visuals)")
    _save_trace(output_path, system, user, raw, parsed, mode)
    _write_migration_report(output_path.parent / "migration_report.json", parsed)
    return parsed
