"""Deterministic Cognos → DAX translator.

Handles all transformations that have a mechanical, no-LLM mapping:
    A. Column reference stripping   [C].[M].[Table].[Col] → 'Table'[Col]
    B/C. CASE → SWITCH template     parsed branches → SWITCH(SELECTEDVALUE(...))
    E. Arithmetic variance          [A]-[B] passthrough, [A]/[B] → DIVIDE
    F. HEX color extraction         namedStyles simple-equality → color measures
    G. Row context / zebra striping mod(RowNumber(),2)=0 → DAX
    Conditional time intelligence   MTD/QTD/YTD ONLY if absent from CSV schema

Per BRIEF.md: "What stays deterministic (never sent to LLM)".
"""
import re
from typing import Any

from cognos.expression_parser import (
    COLUMN_REF_PATTERN,
    extract_case_branches,
    extract_param_references,
)


# ---------------------------------------------------------------------------
# A. Column reference stripping
# ---------------------------------------------------------------------------

def strip_column_reference(expression: str) -> str:
    """Strip a Cognos fully-qualified column ref to DAX.

    [C].[Module].[Table].[Column] → 'Table'[Column]

    Handles the dotted namespace path by taking the last two segments.
    Also strips trailing underscores from member-style names (e.g. [Value_]).
    """
    def _replace(match: re.Match) -> str:
        full = match.group(0)
        # Segments: [C] . [Module] . [Table] . [Column]
        segments = re.findall(r'\[([^\]]+)\]', full)
        if len(segments) >= 2:
            table = segments[-2]
            col = segments[-1].rstrip("_")
            return f"'{table}'[{col}]"
        return full

    return COLUMN_REF_PATTERN.sub(_replace, expression)


# ---------------------------------------------------------------------------
# B/C. CASE → SWITCH template
# ---------------------------------------------------------------------------

# Matches  ?param? contains 'X'   and   ?param? = 'X'
_CONTAINS_COND = re.compile(
    r"\?(\w+)\?\s*(?:contains|=)\s*'([^']*)'", re.IGNORECASE
)


def _branch_to_switch_pair(condition: str, value: str, param_name: str) -> tuple[str, str]:
    """Convert one CASE branch condition+value into a SWITCH (key, value) pair.

    Returns (switch_key, dax_value). For ELSE, switch_key is the default marker.
    """
    if condition.strip().upper() == "ELSE":
        return ("__DEFAULT__", _value_to_dax(value, param_name))

    # Extract the comparison value from ?param? contains/= 'X'
    m = _CONTAINS_COND.search(condition)
    if m:
        key = m.group(2)
        return (key, _value_to_dax(value, param_name))

    # Compound condition (e.g. ?p?='X' AND ?other? > date(...)) — can't template
    # deterministically; leave a marker so the LLM call picks it up.
    return ("__COMPOUND__", f"/* COMPOUND: {condition} */ {value}")


def _value_to_dax(value: str, param_name: str) -> str:
    """Normalize a branch value to DAX syntax (strip column refs, quotes)."""
    # Strip fully-qualified column refs
    value = strip_column_reference(value)
    # 'literal string' stays quoted; [Measure] stays bracketed
    return value


def case_to_switch(expr_item: dict, param_table_map: dict[str, str]) -> dict | None:
    """Convert a param_switch expression into a DAX SWITCH measure.

    Args:
        expr_item: classified expression with 'branches', 'parameters', 'name'
        param_table_map: {param_name: "ParamTableName"} for SELECTEDVALUE source

    Returns:
        {name, expression, type:"switch_measure", source_expression} or None
        if the branches contain compound conditions that require the LLM.
    """
    branches = expr_item.get("branches", [])
    if not branches:
        return None

    params = expr_item.get("parameters", [])
    if not params:
        return None
    param_name = params[0]
    table_ref = param_table_map.get(param_name, f"Param_{param_name}")
    col_ref = f"'{table_ref}'[{param_name}]"

    pairs: list[tuple[str, str]] = []
    default_val: str | None = None
    has_compound = False

    for branch in branches:
        key, dax_val = _branch_to_switch_pair(branch["condition"], branch["value"], param_name)
        if key == "__DEFAULT__":
            default_val = dax_val
        elif key == "__COMPOUND__":
            has_compound = True
        else:
            pairs.append((key, dax_val))

    # Compound conditions can't be templated deterministically → defer to LLM
    if has_compound:
        return None

    # Build SWITCH(SELECTEDVALUE(...), "key", val, ..., default)
    lines = [f"SWITCH("]
    lines.append(f"    SELECTEDVALUE({col_ref}, \"\"),")
    for key, val in pairs:
        lines.append(f'    "{key}", {val},')
    if default_val is not None:
        lines.append(f"    {default_val}")
    else:
        lines.append('    BLANK()')
    lines.append(")")
    dax = "\n".join(lines)

    return {
        "name": expr_item.get("name", "SwitchMeasure"),
        "expression": dax,
        "type": "switch_measure",
        "source_expression": expr_item.get("expression", ""),
    }


# ---------------------------------------------------------------------------
# E. Arithmetic variance
# ---------------------------------------------------------------------------

_DIVISION_PATTERN = re.compile(r'\[([\w\s]+)\]\s*/\s*\[([\w\s]+)\]')
_SUBTRACT_ADD_PATTERN = re.compile(r'\[([\w\s]+)\]\s*([-+])\s*\[([\w\s]+)\]')


def variance_to_dax(expr_item: dict) -> dict:
    """Convert an arithmetic variance expression to DAX.

    [A] / [B]    → DIVIDE([A], [B], 0)
    [A] - [B]    → [A] - [B]   (passthrough)
    [A] + [B]    → [A] + [B]   (passthrough)
    """
    expr = expr_item.get("expression", "")
    name = expr_item.get("name", "")

    # Division → DIVIDE for divide-by-zero safety
    def _div_repl(m: re.Match) -> str:
        return f"DIVIDE([{m.group(1)}], [{m.group(2)}], 0)"
    dax = _DIVISION_PATTERN.sub(_div_repl, expr)

    return {
        "name": name,
        "expression": dax,
        "type": "variance",
        "source_expression": expr,
    }


# ---------------------------------------------------------------------------
# F. HEX color extraction from namedStyles (simple equality)
# ---------------------------------------------------------------------------

# [FieldName] = 'Value'   →  simple equality condition
_SIMPLE_EQ_COND = re.compile(
    r"\[([\w\s]+)\]\s*=\s*'([^']*)'", re.IGNORECASE
)
_HEX_COLOR = re.compile(r'#([0-9A-Fa-f]{6})\b')


def extract_hex_colors(named_styles: dict) -> list[dict]:
    """Extract simple-equality conditional color measures from namedStyles.

    Only handles cases where the condition is a single equality
    ([Field] = 'Value') and the style has an explicit background-color HEX.
    Complex multi-condition cases are left for the LLM.

    Returns list of {name, expression, type:"color", target_field}.
    """
    color_measures: list[dict] = []

    for style_name, style_data in named_styles.items():
        if style_data.get("type") != "advanced":
            continue

        cases = style_data.get("cases", [])
        simple_pairs: list[tuple[str, str, str]] = []  # (field, value, hex)

        for case in cases:
            condition = case.get("condition", "")
            style = case.get("style", {})
            bg = style.get("backgroundColor", "")
            hex_match = _HEX_COLOR.search(bg)
            if not hex_match:
                continue
            hex_val = f"#{hex_match.group(1)}"

            eq = _SIMPLE_EQ_COND.search(condition)
            if eq and "and" not in condition.lower() and "or" not in condition.lower():
                field = eq.group(1)
                value = eq.group(2)
                simple_pairs.append((field, value, hex_val))

        if not simple_pairs:
            continue

        # Build a SWITCH measure: SWITCH([Field], "v1", "#hex1", "v2", "#hex2", "#default")
        field = simple_pairs[0][0]
        lines = [f"SWITCH("]
        lines.append(f"    VALUES('{field}'),")
        for _, value, hex_val in simple_pairs:
            lines.append(f'    "{value}", "{hex_val}",')
        # Default color from styleDefault or white
        default_bg = named_styles.get(style_name, {}).get("default", {}).get("backgroundColor", "#FFFFFF")
        default_hex = _HEX_COLOR.search(default_bg)
        default_color = f"#{default_hex.group(1)}" if default_hex else "#FFFFFF"
        lines.append(f'    "{default_color}"')
        lines.append(")")

        color_measures.append({
            "name": f"{style_name}_Color",
            "expression": "\n".join(lines),
            "type": "color",
            "target_field": field,
        })

    return color_measures


# ---------------------------------------------------------------------------
# G. Row context / zebra striping
# ---------------------------------------------------------------------------

def zebra_striping_measure() -> dict:
    """Generate the DAX measure for zebra row striping.

    Cognos: mod(RowNumber(),2)=0 → alternate row background.
    Power BI: IF(ISEVEN(...), color1, color2) bound to row visual.
    """
    return {
        "name": "ZebraStripeColor",
        "expression": 'IF(ISEVEN(ROW()), "#F5F5F5", "#FFFFFF")',
        "type": "color",
        "target_field": "__row__",
    }


# ---------------------------------------------------------------------------
# Conditional time intelligence (only if absent from CSV)
# ---------------------------------------------------------------------------

# Columns that indicate pre-aggregated time intelligence is already present
_TI_COLUMN_MARKERS = {"mtd", "qtd", "ytd", "prior_mtd", "prior_qtd", "prior_ytd"}


def _csv_has_ti_columns(csv_columns: list[str]) -> bool:
    """Check whether the CSV schema already contains MTD/QTD/YTD columns."""
    normalized = {c.lower().replace(" ", "_") for c in csv_columns}
    return bool(normalized & _TI_COLUMN_MARKERS)


def conditional_time_intel(
    csv_schema: dict,
    date_table: str = "dim_time",
    date_column: str = "Date",
    value_column: str = "Value",
) -> list[dict]:
    """Generate MTD/QTD/YTD template measures ONLY if absent from the CSV.

    Per BRIEF.md: "MTD/QTD/YTD template measures (only generated if columns
    absent from CSV schema)". If the CSV already has these aggregations as
    columns, return an empty list.

    Args:
        csv_schema: {table_name: [column_names]} from the CSV reader
        date_table/date_column/value_column: anchors for the DAX

    Returns:
        List of measure dicts, or [] if TI columns already exist.
    """
    all_columns: list[str] = []
    for cols in csv_schema.values():
        all_columns.extend(cols)

    if _csv_has_ti_columns(all_columns):
        return []

    date_ref = f"'{date_table}'[{date_column}]"
    base = f"SUM(fact_data[{value_column}])"

    return [
        {
            "name": "MTD",
            "expression": f"CALCULATE({base}, DATESMTD({date_ref}))",
            "type": "base_measure",
            "source_expression": "TEMPLATE (CSV lacks MTD column)",
        },
        {
            "name": "QTD",
            "expression": f"CALCULATE({base}, DATESQTD({date_ref}))",
            "type": "base_measure",
            "source_expression": "TEMPLATE (CSV lacks QTD column)",
        },
        {
            "name": "YTD",
            "expression": f"CALCULATE({base}, DATESYTD({date_ref}))",
            "type": "base_measure",
            "source_expression": "TEMPLATE (CSV lacks YTD column)",
        },
        {
            "name": "Prior_MTD",
            "expression": f"CALCULATE([MTD], SAMEPERIODLASTYEAR({date_ref}))",
            "type": "base_measure",
            "source_expression": "TEMPLATE",
        },
        {
            "name": "Prior_QTD",
            "expression": f"CALCULATE([QTD], SAMEPERIODLASTYEAR({date_ref}))",
            "type": "base_measure",
            "source_expression": "TEMPLATE",
        },
        {
            "name": "Prior_YTD",
            "expression": f"CALCULATE([YTD], SAMEPERIODLASTYEAR({date_ref}))",
            "type": "base_measure",
            "source_expression": "TEMPLATE",
        },
    ]


# ---------------------------------------------------------------------------
# Orchestrator: translate all deterministic groups in one pass
# ---------------------------------------------------------------------------

def translate_deterministic(
    classified: dict,
    named_styles: dict,
    csv_schema: dict,
    parameters: list[dict],
) -> dict:
    """Run every deterministic translation and return measures + deferred items.

    Args:
        classified: output of classify_expressions_from_queries()
        named_styles: xml_data['namedStyles']
        csv_schema: {table_name: [columns]} for TI column check
        parameters: xml_data['parameters'] for param table naming

    Returns:
        {
          "measures": [...],          # all deterministic DAX measures
          "deferred": [...],          # param_switches with compound conditions → LLM
          "parameter_tables": [...],  # disconnected table specs for the generator
        }
    """
    measures: list[dict] = []
    deferred: list[dict] = []

    # Build param_name → table name map for SWITCH SELECTEDVALUE refs
    param_table_map: dict[str, str] = {}
    parameter_tables: list[dict] = []
    for param in parameters:
        pname = param.get("name", "")
        if not pname:
            continue
        table_name = f"Param_{pname}"
        param_table_map[pname] = table_name
        parameter_tables.append({
            "name": table_name,
            "column": pname,
            "values": [opt.get("value", "") for opt in param.get("options", [])]
                      or [param.get("defaultValue", "All")],
        })

    # A. Column references → base measures (one per unique stripped ref)
    seen_refs: set[str] = set()
    for item in classified.get("column_ref", []):
        expr = item.get("expression", "")
        stripped = strip_column_reference(expr)
        if stripped in seen_refs:
            continue
        seen_refs.add(stripped)
        measures.append({
            "name": item.get("name", stripped),
            "expression": stripped,
            "type": "base_measure",
            "source_expression": expr,
        })

    # B/C. Param switches → SWITCH measures (defer compound to LLM)
    for item in classified.get("param_switches", []):
        result = case_to_switch(item, param_table_map)
        if result is not None:
            measures.append(result)
        else:
            deferred.append(item)

    # E. Variance → DAX
    for item in classified.get("variance", []):
        measures.append(variance_to_dax(item))

    # F. HEX colors from simple-equality namedStyles
    measures.extend(extract_hex_colors(named_styles))

    # G. Row context / zebra striping
    if classified.get("row_context"):
        measures.append(zebra_striping_measure())

    # Conditional time intelligence (only if CSV lacks the columns)
    measures.extend(conditional_time_intel(csv_schema))

    return {
        "measures": measures,
        "deferred": deferred,
        "parameter_tables": parameter_tables,
    }


# ---------------------------------------------------------------------------
# CSV schema reader (lightweight, for the Cognos path)
# ---------------------------------------------------------------------------

def read_csv_schema(input_dir) -> dict[str, list[str]]:
    """Read CSV headers from a directory → {table_name: [columns]}.

    Table name is derived from the filename without extension.
    """
    import csv
    import pathlib

    schema: dict[str, list[str]] = {}
    input_path = pathlib.Path(input_dir)

    for csv_file in input_path.glob("*.csv"):
        table_name = csv_file.stem
        try:
            with open(csv_file, encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, [])
            schema[table_name] = header
        except Exception:
            schema[table_name] = []

    return schema


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _test() -> None:
    # A. Column reference stripping
    assert strip_column_reference(
        "[C].[C_Data_Module_PA].[Income_Statement_withdatekey_csv].[Value_]"
    ) == "'Income_Statement_withdatekey_csv'[Value]"

    # E. Variance → DIVIDE
    item = {"name": "Var", "expression": "[Variance] / [Budget]"}
    res = variance_to_dax(item)
    assert "DIVIDE([Variance], [Budget], 0)" in res["expression"], res["expression"]

    # F. HEX extraction (simple equality)
    styles = {
        "AcctColor": {
            "type": "advanced",
            "cases": [
                {"condition": "[Account] = '6599'", "style": {"backgroundColor": "#25CAC8"}},
                {"condition": "[Account] = '6499'", "style": {"backgroundColor": "#DDB00E"}},
            ],
            "default": {"backgroundColor": "#FFFFFF"},
        }
    }
    colors = extract_hex_colors(styles)
    assert len(colors) == 1
    assert '"6599", "#25CAC8"' in colors[0]["expression"]

    # Conditional TI — CSV with MTD column → no template
    assert conditional_time_intel({"fact": ["Date", "Value", "MTD"]}) == []
    # Conditional TI — CSV without → templates generated
    ti = conditional_time_intel({"fact": ["Date", "Value"]})
    assert len(ti) == 6

    print("Deterministic translator tests passed!")


if __name__ == "__main__":
    _test()