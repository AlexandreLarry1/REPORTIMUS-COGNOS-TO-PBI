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

def _resolve_to_csv(cognos_table: str, cognos_col: str, csv_schema: dict) -> tuple[str, str]:
    """Map a Cognos (table, column) to the nearest (csv_table, csv_column).

    Table matching: strip known prefixes (dim_, fact_) and trailing plural 's',
    then check if the entity name appears as a substring of any CSV table name
    or vice versa. Picks the longest overlap.

    Column matching: if Cognos column is an ATTR_S_Caption_Default_N pattern,
    map to the Nth text column of the matched table (Cognos caption attributes).
    Otherwise, find the CSV column with the highest normalized character overlap.

    Returns the original (cognos_table, cognos_col) unchanged if no confident match.
    """
    if not csv_schema:
        return cognos_table, cognos_col

    def _entity(s: str) -> str:
        s = re.sub(r'[_\s]', '', s).lower()
        for prefix in ('dim', 'fact', 'param'):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        return s.rstrip('s')

    ct_entity = _entity(cognos_table)

    # Find CSV table whose entity contains ct_entity or is contained by it
    best_table, best_score = None, 0
    for csv_table in csv_schema:
        csv_entity = _entity(csv_table)
        if ct_entity in csv_entity:
            score = len(ct_entity)
        elif csv_entity in ct_entity:
            score = len(csv_entity)
        else:
            score = 0
        if score > best_score:
            best_score, best_table = score, csv_table

    if not best_table or best_score < 2:
        # Fallback: find any CSV table that contains the exact column name.
        # Handles Cognos source tables with completely different names (e.g.
        # "Income_Statement_withdatekey_csv" → "fact_data" via column "Value").
        col_norm = re.sub(r'[_\s]', '', cognos_col).lower().rstrip('s')
        for csv_table, cols in csv_schema.items():
            for col in cols:
                if re.sub(r'[_\s]', '', col).lower().rstrip('s') == col_norm:
                    return csv_table, col
        return cognos_table, cognos_col.rstrip("_")

    # Column mapping
    # Pattern: ATTR_S_Caption_Default, ATTR_S_Caption_Default_1, _2, ...
    attr_match = re.match(r'ATTR_S_Caption_Default(?:_(\d+))?$', cognos_col, re.IGNORECASE)
    if attr_match:
        n = int(attr_match.group(1) or 0)
        # Keep only descriptive label columns: exclude sort/id/key/num columns
        # AND exclude the primary key column (entity name matches table entity)
        table_entity = _entity(best_table)
        text_cols = [
            c for c in csv_schema[best_table]
            if not re.search(r'(sort|order|id|key|num)$', c, re.IGNORECASE)
            and _entity(c) != table_entity  # exclude the key column itself
        ]
        if n < len(text_cols):
            return best_table, text_cols[n]
        return best_table, cognos_col.rstrip("_")

    # Generic column match: highest normalized overlap
    def _col_norm(s: str) -> str:
        return re.sub(r'[_\s]', '', s).lower().rstrip('s')

    cc_norm = _col_norm(cognos_col)
    best_col, best_col_score = None, 0
    for csv_col in csv_schema[best_table]:
        n = _col_norm(csv_col)
        if cc_norm in n or n in cc_norm:
            score = min(len(cc_norm), len(n))
        else:
            score = sum(1 for a, b in zip(cc_norm, n) if a == b)
        if score > best_col_score:
            best_col_score, best_col = score, csv_col

    if not best_col or best_col_score < 2:
        return best_table, cognos_col.rstrip("_")

    return best_table, best_col


# csv_schema injected at translate time via module-level variable
_current_csv_schema: dict = {}


def strip_column_reference(expression: str, csv_schema: dict | None = None) -> str:
    """Strip a Cognos fully-qualified column ref to DAX.

    4-level:  [C].[Module].[Table].[Column]    → 'CsvTable'[csv_column]
    5-level:  [C].[Module].[Table].[Col].[Mbr] → [Mbr]
              (the member name maps to a DAX measure of the same name)

    Uses csv_schema to map Cognos table/column names to actual CSV names.
    Falls back to raw Cognos names if no match found.
    """
    schema = csv_schema if csv_schema is not None else _current_csv_schema

    def _replace(match: re.Match) -> str:
        full = match.group(0)
        member = match.group(1)  # 5th-level member captured by optional group

        segments = re.findall(r'\[([^\]]+)\]', full)
        if member:
            return f"[{member}]"
        if len(segments) >= 2:
            cognos_table = segments[-2]
            cognos_col = segments[-1].rstrip("_")
            csv_table, csv_col = _resolve_to_csv(cognos_table, cognos_col, schema)
            return f"'{csv_table}'[{csv_col}]"
        return full

    return COLUMN_REF_PATTERN.sub(_replace, expression)


# ---------------------------------------------------------------------------
# B/C. CASE → SWITCH template
# ---------------------------------------------------------------------------

# Matches  ?param? contains 'X'   and   ?param? = 'X'
_CONTAINS_COND = re.compile(
    r"\?(\w+)\?\s*(?:contains|=)\s*'([^']*)'", re.IGNORECASE
)


def _branch_to_switch_pair(
    condition: str, value: str, param_name: str, aggregate_map: dict | None = None
) -> tuple[str, str]:
    """Convert one CASE branch condition+value into a SWITCH (key, value) pair.

    Returns (switch_key, dax_value). For ELSE, switch_key is the default marker.
    """
    if condition.strip().upper() == "ELSE":
        return ("__DEFAULT__", _value_to_dax(value, param_name, aggregate_map))

    # Extract the comparison value from ?param? contains/= 'X'
    m = _CONTAINS_COND.search(condition)
    if m:
        key = m.group(2)
        return (key, _value_to_dax(value, param_name, aggregate_map))

    # Compound condition (e.g. ?p?='X' AND ?other? > date(...)) — can't template
    # deterministically; leave a marker so the LLM call picks it up.
    return ("__COMPOUND__", f"/* COMPOUND: {condition} */ {value}")


_BARE_COL_RE = re.compile(r"^'[^']+'\[[^\]]+\]$")


def build_aggregate_map(xml_data: dict, csv_schema: dict) -> dict:
    """Build Cognos-aggregate lookups for the generator.

    Returns:
        {
          "by_column": {csv_table: {csv_column: aggregate}},   # raw-column wells
          "by_name":   {cognos_item_name: aggregate},          # DAX-measure wells
        }
    Only entries with aggregate != "none" are included, so an empty/missing
    lookup always falls back to each caller's existing default (Sum).
    """
    schema = csv_schema or {}
    by_column: dict[str, dict[str, str]] = {}
    by_name: dict[str, str] = {}

    for query in xml_data.get("queries", {}).values():
        for item in query.get("dataItems", []):
            agg = (item.get("aggregate") or "none").strip().lower()
            if agg in ("", "none"):
                continue

            name = item.get("name", "")
            if name:
                by_name[name] = agg

            expr = item.get("expression", "")
            if expr and COLUMN_REF_PATTERN.search(expr) and "case" not in expr.lower():
                resolved = strip_column_reference(expr, schema).strip()
                if _BARE_COL_RE.match(resolved):
                    table_part, col_part = resolved.split("[", 1)
                    csv_table = table_part.strip().strip("'")
                    csv_col = col_part.rstrip("]")
                    by_column.setdefault(csv_table, {})[csv_col] = agg

    return {"by_column": by_column, "by_name": by_name}


def _aggregate_for_column(aggregate_map: dict | None, resolved_ref: str) -> str:
    """Look up the Cognos aggregate for a resolved 'Table'[Col] DAX ref."""
    if not aggregate_map:
        return "none"
    m = _BARE_COL_RE.match(resolved_ref.strip())
    if not m:
        return "none"
    table_part, col_part = resolved_ref.strip().split("[", 1)
    csv_table = table_part.strip().strip("'")
    csv_col = col_part.rstrip("]")
    return aggregate_map.get("by_column", {}).get(csv_table, {}).get(csv_col, "none")


def _value_to_dax(value: str, param_name: str, aggregate_map: dict | None = None) -> str:
    """Normalize a branch value to DAX syntax (strip column refs, quotes)."""
    value = strip_column_reference(value)
    # A bare 'Table'[Col] in a SWITCH branch needs an aggregation to be a
    # valid measure value — Sum unless Cognos declared a different aggregate.
    if _BARE_COL_RE.match(value.strip()):
        from pbip.aggregation import dax_func
        agg = _aggregate_for_column(aggregate_map, value)
        value = f"{dax_func(agg, default='SUM')}({value})"
    return value


def case_to_switch(
    expr_item: dict, param_table_map: dict[str, str], aggregate_map: dict | None = None
) -> dict | None:
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
        key, dax_val = _branch_to_switch_pair(
            branch["condition"], branch["value"], param_name, aggregate_map
        )
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


def _find_table_for_field(field: str, csv_schema: dict | None) -> str:
    """Return the CSV table name that contains a column matching `field`.

    Normalises both sides (lowercase, spaces→underscores) before comparing.
    Returns "" if no match found — callers fall back to MAX([field]).
    """
    if not csv_schema:
        return ""
    field_norm = field.lower().replace(" ", "_")
    for table_name, cols in csv_schema.items():
        for col in cols:
            if col.lower().replace(" ", "_") == field_norm:
                return table_name
    return ""


def extract_hex_colors(named_styles: dict, csv_schema: dict | None = None) -> list[dict]:
    """Extract simple-equality conditional color measures from namedStyles.

    Only handles cases where the condition is a single equality
    ([Field] = 'Value') and the style has an explicit background-color HEX.
    Complex multi-condition cases are left for the LLM.

    Args:
        named_styles: xml_data['namedStyles']
        csv_schema: {table: [columns]} — used to anchor SELECTEDVALUE to the
            correct table. If not provided or column not found, falls back to
            MAX([field]) which works in matrix row context.

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

        field = simple_pairs[0][0]

        # Anchor to the correct table so SELECTEDVALUE is valid DAX.
        # Fall back to MAX([field]) if table unknown — works in row context.
        table_name = _find_table_for_field(field, csv_schema)
        if table_name:
            switch_selector = f"SELECTEDVALUE('{table_name}'[{field}])"
        else:
            switch_selector = f"MAX([{field}])"

        lines = ["SWITCH("]
        lines.append(f"    {switch_selector},")
        for _, value, hex_val in simple_pairs:
            lines.append(f'    "{value}", "{hex_val}",')
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
    aggregate_map: dict | None = None,
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

    # Resolve fact table and value column from csv_schema instead of hardcoding.
    # Priority: table starting with "fact_", then largest table, fallback to first.
    fact_table = next(
        (t for t in csv_schema if t.lower().startswith("fact_")),
        None,
    )
    if not fact_table:
        # Pick the table with the most columns as a proxy for the fact table
        fact_table = max(csv_schema, key=lambda t: len(csv_schema[t]), default=None)
    if not fact_table:
        return []

    # Resolve value column: first numeric-looking column (not a key/id/date column)
    _SKIP = re.compile(r'(id|key|date|month|year|quarter|order|num|code)$', re.IGNORECASE)
    resolved_value = value_column  # fallback
    for col in csv_schema[fact_table]:
        if not _SKIP.search(col):
            resolved_value = col
            break

    # Resolve date table: any table with a recognisable date column
    resolved_date_table: str | None = None
    resolved_date_col = date_column
    for tbl, cols in csv_schema.items():
        for col in cols:
            if col.lower() in ("date", "period_date", "transaction_date"):
                resolved_date_table = tbl
                resolved_date_col = col
                break
        if resolved_date_table:
            break

    # Guard: no real date dimension found → skip TI measures entirely
    if not resolved_date_table:
        print("  TI measures skipped — no date column found in CSV schema (no dim_time equivalent)")
        return []

    date_ref = f"'{resolved_date_table}'[{resolved_date_col}]"
    from pbip.aggregation import dax_func
    value_agg = (aggregate_map or {}).get("by_column", {}).get(fact_table, {}).get(resolved_value, "none")
    base = f"{dax_func(value_agg, default='SUM')}('{fact_table}'[{resolved_value}])"

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
    xml_data: dict | None = None,
) -> dict:
    """Run every deterministic translation and return measures + deferred items.

    Args:
        classified: output of classify_expressions_from_queries()
        named_styles: xml_data['namedStyles']
        csv_schema: {table_name: [columns]} for TI column check
        parameters: xml_data['parameters'] for param table naming
        xml_data: full extraction (for Cognos @aggregate lookup); optional —
            when omitted, aggregation defaults to Sum everywhere (prior behavior)

    Returns:
        {
          "measures": [...],          # all deterministic DAX measures
          "deferred": [...],          # param_switches with compound conditions → LLM
          "parameter_tables": [...],  # disconnected table specs for the generator
          "aggregate_map": {...},     # Cognos aggregate lookups, for downstream generator use
        }
    """
    # Inject csv_schema globally so strip_column_reference can resolve Cognos→CSV names
    global _current_csv_schema
    _current_csv_schema = csv_schema or {}

    aggregate_map = build_aggregate_map(xml_data or {}, csv_schema)

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
    # Bare 'Table'[Col] is invalid as a DAX measure — wrap in SELECTEDVALUE()
    # so it returns the single value in filter context (row in table/matrix).
    seen_refs: set[str] = set()
    for item in classified.get("column_ref", []):
        expr = item.get("expression", "")
        stripped = strip_column_reference(expr)
        if stripped in seen_refs:
            continue
        seen_refs.add(stripped)
        dax_expr = f"SELECTEDVALUE({stripped})" if _BARE_COL_RE.match(stripped.strip()) else stripped
        measures.append({
            "name": item.get("name", stripped),
            "expression": dax_expr,
            "type": "base_measure",
            "source_expression": expr,
        })

    # B/C. Param switches → SWITCH measures (defer compound to LLM)
    for item in classified.get("param_switches", []):
        result = case_to_switch(item, param_table_map, aggregate_map)
        if result is not None:
            measures.append(result)
        else:
            deferred.append(item)

    # E. Variance → DAX
    for item in classified.get("variance", []):
        measures.append(variance_to_dax(item))

    # F. HEX colors from simple-equality namedStyles
    measures.extend(extract_hex_colors(named_styles, csv_schema))

    # G. Row context / zebra striping
    if classified.get("row_context"):
        measures.append(zebra_striping_measure())

    # Conditional time intelligence (only if CSV lacks the columns)
    measures.extend(conditional_time_intel(csv_schema, aggregate_map=aggregate_map))

    return {
        "measures": measures,
        "aggregate_map": aggregate_map,
        "deferred": deferred,
        "parameter_tables": parameter_tables,
    }


# ---------------------------------------------------------------------------
# CSV schema reader (lightweight, for the Cognos path)
# ---------------------------------------------------------------------------

def read_csv_schema(input_dir) -> dict[str, list[str]]:
    """Read CSV headers from a directory → {table_name: [columns]}."""
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


def read_csv_schema_with_samples(
    input_dir, max_distinct: int = 20
) -> dict[str, dict[str, list[str]]]:
    """Read CSV schema + distinct values for low-cardinality columns.

    Returns {table: {col: [distinct_values]}} for string columns with
    <= max_distinct unique values. Used to give the LLM concrete value
    examples so it can map Cognos labels (e.g. "Version 1") to CSV
    values (e.g. "Budget").
    """
    import csv
    import pathlib

    schema: dict[str, dict[str, list[str]]] = {}
    input_path = pathlib.Path(input_dir)

    for csv_file in input_path.glob("*.csv"):
        table_name = csv_file.stem
        try:
            with open(csv_file, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                schema[table_name] = {}
                continue
            cols: dict[str, list[str]] = {}
            for col in rows[0].keys():
                vals = list(dict.fromkeys(
                    r[col] for r in rows if r.get(col) not in (None, "")
                ))
                # Only include if low-cardinality (useful for LLM value mapping)
                if len(vals) <= max_distinct:
                    cols[col] = vals
                else:
                    cols[col] = []  # high-cardinality: no sample values
            schema[table_name] = cols
        except Exception:
            schema[table_name] = {}

    return schema


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _test() -> None:
    # A. Column reference stripping — 4-level
    assert strip_column_reference(
        "[C].[C_Data_Module_PA].[Income_Statement_withdatekey_csv].[Value_]"
    ) == "'Income_Statement_withdatekey_csv'[Value]"

    # A. Column reference stripping — 5-level member ref maps to measure
    assert strip_column_reference(
        "[C].[C_Data_Module_PA].[Income_Statement_withdatekey_csv].[Value_].[MTD]"
    ) == "[MTD]", strip_column_reference(
        "[C].[C_Data_Module_PA].[Income_Statement_withdatekey_csv].[Value_].[MTD]"
    )

    # E. Variance → DIVIDE
    item = {"name": "Var", "expression": "[Variance] / [Budget]"}
    res = variance_to_dax(item)
    assert "DIVIDE([Variance], [Budget], 0)" in res["expression"], res["expression"]

    # F. HEX extraction (simple equality) — with csv_schema lookup
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
    schema = {"fact_data": ["Date", "Account", "Value"]}
    colors = extract_hex_colors(styles, schema)
    assert len(colors) == 1
    expr = colors[0]["expression"]
    assert '"6599", "#25CAC8"' in expr
    assert "SELECTEDVALUE('fact_data'[Account])" in expr, expr

    # Without csv_schema → falls back to MAX([field])
    colors_no_schema = extract_hex_colors(styles)
    assert "MAX([Account])" in colors_no_schema[0]["expression"]

    # Conditional TI — CSV with MTD column → no template
    assert conditional_time_intel({"fact": ["Date", "Value", "MTD"]}) == []
    # Conditional TI — CSV without → templates generated
    ti = conditional_time_intel({"fact": ["Date", "Value"]})
    assert len(ti) == 6

    print("Deterministic translator tests passed!")


if __name__ == "__main__":
    _test()