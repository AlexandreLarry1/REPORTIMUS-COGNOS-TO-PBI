"""Unified LLM translation: a SINGLE call replacing the old 3-call architecture.

Per BRIEF.md: deterministic translations are done locally; ONLY the genuinely
unresolved expressions (types D & H — date functions and total() aggregations)
are sent to the LLM. Compound parameter switches that the deterministic
translator cannot template are also included here.

Input context (built by expression_parser.build_prompt_context):
    - unresolved[]      — expressions requiring LLM (types D & H)
    - deferred[]        — compound param_switches that need LLM
    - parameters[]      — for SELECTEDVALUE targets and table naming
    - namedStyles{}     — complex conditional formatting cases
    - csv_schema{}      — table/column names for DAX anchoring
    - deterministic_measures[] — already-generated names (avoid collisions)

Output contract (flat JSON, per brief):
    {
      "measures": [
        {"name": "...", "expression": "...", "type": "base_measure|switch_measure|variance|color"}
      ],
      "parameter_tables": [
        {"name": "Param_p_timeview", "column": "p_timeview", "values": ["MTD","QTD","YTD"]}
      ]
    }
"""
import json
import pathlib
import sys

from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import observability as obs
from utils import call_api_azure, strip_json_fences


_SYSTEM_PROMPT = """\
You are an expert Power BI data modeler translating Cognos report expressions to DAX.

You receive ONLY the expressions that cannot be translated deterministically:
  - Cognos date functions (_add_months, date(), String2date()) → DAX equivalents
  - total(CASE WHEN ...) aggregations → CALCULATE/SUMX patterns
  - Compound CASE expressions (multiple conditions joined by AND/OR)

The deterministic translations (column refs, simple CASE→SWITCH, arithmetic \
variance, HEX colors, zebra striping, MTD/QTD/YTD templates) have ALREADY been \
generated. Do NOT regenerate them. Only produce DAX for the unresolved expressions.

Return ONLY a valid JSON object (no markdown, no comments) with this structure:
{
  "measures": [
    {"name": "<measure_name>", "expression": "<DAX>", "type": "base_measure"}
  ],
  "parameter_tables": [
    {"name": "Param_<param>", "column": "<param>", "values": ["v1","v2"]}
  ]
}

DAX rules:
1. Use SELECTEDVALUE('Param_<name>'[<name>]) for ?param? references
2. _add_months(date, -N) → EDATE(date, -N)
3. date('YYYY-MM-DD') → DATE(YYYY, MM, DD)
4. String2date('YYYY-MM-DD') → DATE(YYYY, MM, DD)
5. total(CASE WHEN ?p? contains 'X' AND [Col] = 'Y' THEN [Val].[Period] END) →
   Use this VAR pattern (NEVER use SUMX/SWITCH — it is an anti-pattern that breaks context):
   VAR _p = SELECTEDVALUE('Param_<p>'[<p>], "Full")
   VAR _base = CALCULATE(SUM(fact_table[value_col]), fact_table[Col] = "csv_value_Y")
   RETURN SWITCH(_p,
       "MTD", CALCULATE(_base, DATESMTD('dim_time'[Date])),
       "QTD", CALCULATE(_base, DATESQTD('dim_time'[Date])),
       "YTD", CALCULATE(_base, DATESYTD('dim_time'[Date])),
       _base)
   IMPORTANT: The filter value "csv_value_Y" must come from the CSV sample values
   provided in the prompt, NOT from the Cognos expression literal. Cognos names like
   "Version 1" or "Actual PY" may differ from CSV values like "Budget" or "Actual".
6. Use DIVIDE(a, b, 0) for any division
7. For time-shifted measures (SAMEPERIODLASTYEAR / prior year), wrap the base measure:
   CALCULATE(_base, SAMEPERIODLASTYEAR('dim_time'[Date]))
"""


def build_user_prompt(
    unresolved: list[dict],
    deferred: list[dict],
    parameters: list[dict],
    named_styles: dict,
    csv_schema: dict,
    deterministic_measure_names: list[str],
    data_dictionary_text: str = "",
) -> str:
    """Build the user prompt with ONLY unresolved expressions + context."""
    lines: list[str] = []

    # Business data dictionary (table descriptions, relationships, assumptions)
    if data_dictionary_text:
        lines.append("## Business data dictionary (source of truth for table/column semantics):\n")
        lines.append(data_dictionary_text)
        lines.append("")

    # CSV schema + sample distinct values (for correct table/column refs and value mapping)
    lines.append("## CSV schema with sample values (use these exact names and values):\n")
    for table, cols in csv_schema.items():
        if isinstance(cols, dict):
            # enriched schema: {col: [sample_values]}
            for col, samples in cols.items():
                if samples:
                    lines.append(f"Table '{table}', column '{col}': sample values = {samples[:8]}")
                else:
                    lines.append(f"Table '{table}', column '{col}'")
        else:
            lines.append(f"Table '{table}': {', '.join(cols)}")
    lines.append("")

    # Parameters (for SELECTEDVALUE targets + disconnected tables)
    lines.append("## Parameters:\n")
    for param in parameters:
        pname = param.get("name", "")
        opts = [o.get("value", "") for o in param.get("options", [])]
        lines.append(f"?{pname}? options: {opts}")
    lines.append("")

    # Unresolved expressions (types D & H)
    lines.append("## Unresolved expressions to translate (types D & H):\n")
    for i, item in enumerate(unresolved, 1):
        lines.append(f"{i}. name={item.get('name', '?')}")
        lines.append(f"   expression: {item.get('expression', '')}")
        if item.get("branches"):
            lines.append(f"   branches: {item['branches']}")
        lines.append("")

    # Deferred compound switches (could not template deterministically)
    if deferred:
        lines.append("## Compound parameter switches (multi-condition, need LLM):\n")
        for i, item in enumerate(deferred, 1):
            lines.append(f"{i}. name={item.get('name', '?')}")
            lines.append(f"   expression: {item.get('expression', '')}")
            if item.get("branches"):
                lines.append(f"   branches: {item['branches']}")
            lines.append("")

    # Complex named styles (only if not simple-equality — those were handled)
    complex_styles = {
        k: v for k, v in named_styles.items()
        if _is_complex_style(v)
    }
    if complex_styles:
        lines.append("## Complex conditional formatting (multi-condition cases):\n")
        lines.append(json.dumps(complex_styles, indent=2, ensure_ascii=False))
        lines.append("")

    # Already-generated measures (avoid name collisions)
    if deterministic_measure_names:
        lines.append("## Already-generated measure names (do NOT regenerate):\n")
        lines.append(", ".join(deterministic_measure_names))
        lines.append("")

    lines.append("Translate the unresolved expressions to DAX and return the JSON object.")
    return "\n".join(lines)


def _is_complex_style(style_data: dict) -> bool:
    """Return True if a namedStyle has multi-condition cases (AND/OR)."""
    for case in style_data.get("cases", []):
        cond = case.get("condition", "")
        if "and" in cond.lower() or "or" in cond.lower():
            return True
    return False


def parse_response(raw: str) -> dict:
    """Parse the LLM response and extract the JSON object."""
    try:
        data = json.loads(strip_json_fences(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid LLM JSON response: {e}") from e

    if "measures" not in data:
        data["measures"] = []
    if "parameter_tables" not in data:
        data["parameter_tables"] = []

    return data


def call_api(system: str, user: str, trace=None) -> str:
    """Call Azure OpenAI to translate the unresolved expressions."""
    return call_api_azure(system, user, trace, "unified_translation_llm")


def _save_llm_trace(
    output_path: pathlib.Path,
    system: str,
    user: str,
    raw_response: str,
    parsed: dict,
    mode: str,
) -> pathlib.Path:
    """Save a versioned snapshot of the LLM call under intermediate/llm_traces/.

    Layout:
        intermediate/llm_traces/
            20260626_142301/
                system_prompt.txt
                user_prompt.txt
                raw_response.txt
                parsed_output.json
                meta.json          ← model, mode, counts, timestamp

    Returns the snapshot directory path.
    """
    import datetime

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_dir = output_path.parent / "llm_traces" / ts
    trace_dir.mkdir(parents=True, exist_ok=True)

    (trace_dir / "system_prompt.txt").write_text(system, encoding="utf-8")
    (trace_dir / "user_prompt.txt").write_text(user, encoding="utf-8")
    (trace_dir / "raw_response.txt").write_text(raw_response, encoding="utf-8")
    (trace_dir / "parsed_output.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    meta = {
        "timestamp": ts,
        "mode": mode,
        "model": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "unknown"),
        "n_measures": len(parsed.get("measures", [])),
        "n_parameter_tables": len(parsed.get("parameter_tables", [])),
        "prompt_chars": len(user),
        "response_chars": len(raw_response),
    }
    (trace_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"   LLM trace -> {trace_dir}")
    return trace_dir


def run(
    prompt_context: dict,
    deferred: list[dict],
    csv_schema: dict,
    deterministic_measure_names: list[str],
    output_path: pathlib.Path,
    mode: str = "api",
    trace=None,
    data_dictionary_text: str = "",
) -> dict:
    """Execute the SINGLE unified LLM call.

    Args:
        prompt_context: output of build_prompt_context() (unresolved, parameters, namedStyles)
        deferred: compound param_switches from deterministic_translator
        csv_schema: {table: [columns]} for correct DAX references
        deterministic_measure_names: names already generated (avoid collisions)
        output_path: where to write unified_translation.json
        mode: "api" or "paste"
        trace: observability trace

    Returns:
        Dict with {measures: [...], parameter_tables: [...]}
    """
    system = _SYSTEM_PROMPT
    user = build_user_prompt(
        unresolved=prompt_context.get("unresolved", []),
        deferred=deferred,
        parameters=prompt_context.get("parameters", []),
        named_styles=prompt_context.get("namedStyles", {}),
        csv_schema=csv_schema,
        deterministic_measure_names=deterministic_measure_names,
        data_dictionary_text=data_dictionary_text,
    )

    if mode == "paste":
        prompt_path = output_path.parent / "unified_translation_prompt.txt"
        prompt_path.write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
        print(f"Prompt written -> {prompt_path}")
        print("\nPaste into your LLM and put the JSON response in:")
        print(f"  {output_path}")
        input("Press Enter once the file is created...")

        if not output_path.exists():
            raise FileNotFoundError(f"File not found: {output_path}")
        raw_response = output_path.read_text(encoding="utf-8")
        data = json.loads(raw_response)
        _save_llm_trace(output_path, system, user, raw_response, data, mode)
        return data

    # API mode
    raw = call_api(system, user, trace)
    data = parse_response(raw)

    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_meas = len(data.get("measures", []))
    n_tabs = len(data.get("parameter_tables", []))
    print(f"Unified translation -> {output_path} ({n_meas} measures, {n_tabs} param tables)")
    _save_llm_trace(output_path, system, user, raw, data, mode)

    return data


# ---------------------------------------------------------------------------
# CLI (standalone testing)
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Unified LLM Translation (single call)")
    parser.add_argument("--context", required=True, help="Path to prompt_context.json")
    parser.add_argument("--deferred", required=True, help="Path to deferred.json")
    parser.add_argument("--csv-schema", required=True, help="Path to csv_schema.json")
    parser.add_argument("--output", required=True, help="Output path unified_translation.json")
    parser.add_argument("--mode", choices=["api", "paste"], default="api")
    args = parser.parse_args()

    ctx = json.loads(pathlib.Path(args.context).read_text(encoding="utf-8"))
    deferred = json.loads(pathlib.Path(args.deferred).read_text(encoding="utf-8"))
    schema = json.loads(pathlib.Path(args.csv_schema).read_text(encoding="utf-8"))

    with obs.trace("unified_translation_run") as trace:
        run(ctx, deferred, schema, [], pathlib.Path(args.output), mode=args.mode, trace=trace)


if __name__ == "__main__":
    main()