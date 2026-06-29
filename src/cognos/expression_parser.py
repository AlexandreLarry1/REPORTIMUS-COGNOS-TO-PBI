"""Report-agnostic Cognos expression classifier.

Classifies Cognos expressions by STRUCTURE (not by hardcoded names) so the
pipeline works on any report. Produces three groups consumed downstream:

- unresolved[]     → expressions requiring LLM (date funcs, total() aggregations)
- param_switches[] → CASE WHEN ?param? expressions (deterministic SWITCH)
- variance[]       → arithmetic between named measures (deterministic)

Classification taxonomy (see BRIEF.md §"Real expression taxonomy"):
    A. Column reference    [C].[Module].[Table].[Col]   → deterministic strip
    B. Static SWITCH       CASE WHEN ?p?='X' THEN ...   → deterministic SWITCH
    C. Dynamic SWITCH      CASE WHEN ?p? contains 'X'   → deterministic SWITCH
    D. Cognos date funcs   _add_months(), date(), ...   → LLM required
    E. Arithmetic variance [Actual]-[Budget]            → deterministic
    F. Conditional format  [Col]='X' → HEX              → deterministic (simple)
    G. Row context         mod(RowNumber(),2)=0          → deterministic
    H. Aggregation context total(CASE WHEN ...)          → LLM required
"""
import re
from typing import Any

# ---------------------------------------------------------------------------
# Patterns — structural, not name-based
# ---------------------------------------------------------------------------

# ?param_name? (any parameter name)
PARAM_PATTERN = re.compile(r'\?(\w+)\?')

# CASE ... END (any case/when block)
CASE_PATTERN = re.compile(r'\bcase\b.+?\bend\b', re.IGNORECASE | re.DOTALL)

# Arithmetic between bracketed measures: [A] - [B], [A] / [B], [A] + [B]
ARITH_PATTERN = re.compile(r'\[[\w\s]+\]\s*[-+*/]\s*\[[\w\s]+\]')

# Cognos-specific functions with no direct DAX equivalent (Type D)
# These are the genuine triggers for LLM translation.
COGNOS_DATE_FUNCS = re.compile(
    r'_add_months|_add_days|_add_years|_last_of_month|_first_of_month|'
    r'_days_between|_months_between|_years_between|'
    r'\bdate\s*\(|\bString2date\s*\(|\b_to_date\s*\(',
    re.IGNORECASE,
)

# Aggregation wrappers requiring CALCULATE/SUMX translation (Type H)
AGG_FUNCS = re.compile(
    r'\btotal\s*\(|\baggregate\s*\(|\b_sum\s*\(',
    re.IGNORECASE,
)

# Row context (Type G) — zebra striping
ROW_CONTEXT_PATTERN = re.compile(r'mod\s*\(\s*RowNumber\s*\(\s*\)\s*,', re.IGNORECASE)

# Matches 4-level Cognos column refs: [C].[Module].[Table].[Col]
# Optionally followed by a 5th-level member ref .[Member] — e.g. [Value_].[MTD].
COLUMN_REF_PATTERN = re.compile(
    r'\[C\]\.\[[^\]]+\]\.\[[^\]]+\]\.\[[^\]]+\]'
    r'(?:\.\[([^\]]+)\])?'
)


# ---------------------------------------------------------------------------
# Extractors (reused by deterministic_translator)
# ---------------------------------------------------------------------------

def extract_param_references(expression: str) -> list[str]:
    """Extract ?param? names referenced in an expression."""
    if not expression:
        return []
    return list(dict.fromkeys(PARAM_PATTERN.findall(expression)))


def extract_case_branches(case_expr: str) -> list[dict]:
    """Parse the WHEN/THEN/ELSE branches of a CASE expression.

    Returns a list of {condition, value} dicts. Handles 'contains', '=',
    and compound conditions (AND/OR).
    """
    if not case_expr or "case" not in case_expr.lower():
        return []

    branches: list[dict] = []

    # WHEN <condition> THEN <value>  (value runs until next WHEN / ELSE / END)
    when_pattern = re.compile(
        r'when\s+(.+?)\s+then\s+(.+?)(?=\s+when\s+|\s+else\s+|\s+end\s*$)',
        re.IGNORECASE | re.DOTALL,
    )
    for match in when_pattern.finditer(case_expr):
        branches.append({
            "condition": match.group(1).strip(),
            "value": match.group(2).strip(),
        })

    # ELSE <value>
    else_match = re.search(r'else\s+(.+?)\s*end\s*$', case_expr, re.IGNORECASE | re.DOTALL)
    if else_match:
        branches.append({
            "condition": "ELSE",
            "value": else_match.group(1).strip(),
        })

    return branches


def extract_measure_names(expression: str) -> list[str]:
    """Extract bracketed measure references [MeasureName] from an expression.

    Excludes fully-qualified Cognos column refs ([C].[M].[T].[Col]).
    """
    if not expression:
        return []

    measures: list[str] = []
    # Bracketed names not part of a [C].[M]... path
    for match in re.finditer(r'(?<!\.)\[([\w\s]+)\]', expression):
        name = match.group(1)
        if name not in measures:
            measures.append(name)
    return measures


# ---------------------------------------------------------------------------
# Structural classifier (report-agnostic — no hardcoded keywords)
# ---------------------------------------------------------------------------

def classify_expression_type(expression: str) -> str:
    """Classify an expression by its STRUCTURE, not by hardcoded names.

    Returns one of:
        "unresolved"     — requires LLM (date functions, total() aggregations)
        "param_switch"   — CASE WHEN ?param? (deterministic SWITCH)
        "variance"       — arithmetic between measures (deterministic)
        "column_ref"     — pure column reference (deterministic strip)
        "row_context"    — zebra striping (deterministic)
        "simple"         — trivial / unclassified
    """
    if not expression or not expression.strip():
        return "simple"

    # Type D / H — LLM required (check FIRST: these win over sub-patterns,
    # e.g. a total(CASE WHEN...) contains a CASE but needs LLM).
    if COGNOS_DATE_FUNCS.search(expression):
        return "unresolved"
    if AGG_FUNCS.search(expression):
        return "unresolved"

    # Type G — row context / zebra striping
    if ROW_CONTEXT_PATTERN.search(expression):
        return "row_context"

    # Type B / C — parameter switch (CASE WHEN with ?param?)
    if re.search(r'\bcase\b', expression, re.IGNORECASE) and "?" in expression:
        if PARAM_PATTERN.search(expression):
            return "param_switch"

    # Type E — arithmetic variance between measures
    if ARITH_PATTERN.search(expression):
        return "variance"

    # Type A — pure column reference
    if COLUMN_REF_PATTERN.search(expression) and "case" not in expression.lower():
        return "column_ref"

    return "simple"


def classify_expressions_from_queries(xml_data: dict) -> dict:
    """Classify every expression in xml_data['queries'].

    Returns three buckets per BRIEF.md contract:
        {
          "unresolved":     [...],   # types D & H → sent to LLM
          "param_switches": [...],   # types B & C → deterministic SWITCH
          "variance":       [...],   # type E      → deterministic
          "column_ref":     [...],   # type A      → deterministic strip
          "row_context":    [...],   # type G      → deterministic
          "simple":         [...],   # trivial
        }
    Each item: {name, query, type, expression, branches?, parameters, measures}
    """
    result: dict[str, list[dict]] = {
        "unresolved": [],
        "param_switches": [],
        "variance": [],
        "column_ref": [],
        "row_context": [],
        "simple": [],
    }

    bucket_for_type = {
        "unresolved": "unresolved",
        "param_switch": "param_switches",
        "variance": "variance",
        "column_ref": "column_ref",
        "row_context": "row_context",
        "simple": "simple",
    }

    for query_name, query in xml_data.get("queries", {}).items():
        # dataItems and expressions both carry <expression> text
        seen_exprs: set[str] = set()
        items = list(query.get("expressions", [])) + list(query.get("dataItems", []))

        for item in items:
            expr_text = item.get("expression", "")
            if not expr_text or expr_text in seen_exprs:
                continue
            seen_exprs.add(expr_text)

            expr_type = classify_expression_type(expr_text)
            bucket = bucket_for_type[expr_type]

            entry: dict[str, Any] = {
                "name": item.get("name", ""),
                "query": query_name,
                "type": expr_type,
                "expression": expr_text,
                "parameters": extract_param_references(expr_text),
                "measures": extract_measure_names(expr_text),
            }
            # Attach parsed branches for CASE expressions (used by deterministic translator)
            if expr_type in ("param_switch", "unresolved") and re.search(r'\bcase\b', expr_text, re.IGNORECASE):
                entry["branches"] = extract_case_branches(expr_text)

            result[bucket].append(entry)

    return result


def build_prompt_context(classified: dict, xml_data: dict) -> dict:
    """Build the single context object for the unified LLM call.

    This is the ONLY context builder now. It groups exactly what the brief
    specifies: unresolved expressions + parameters + named styles.
    Deterministic groups are translated locally and never sent to the LLM.
    """
    return {
        # Only types D & H are sent to the LLM
        "unresolved": classified.get("unresolved", []),
        # Parameters list (for disconnected tables + SWITCH defaults)
        "parameters": xml_data.get("parameters", []),
        # Named styles with explicit HEX (simple-equality cases are extracted
        # deterministically; only complex multi-condition cases reach here)
        "namedStyles": xml_data.get("namedStyles", {}),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _test() -> None:
    """Report-agnostic classification tests (no hardcoded keywords)."""
    # Type D — date functions → unresolved
    assert classify_expression_type("_add_months(?_as_of_date?, -1)") == "unresolved"
    assert classify_expression_type("String2date('2024-01-31')") == "unresolved"

    # Type H — total() aggregation → unresolved (even though it contains CASE)
    assert classify_expression_type(
        "total (CASE WHEN ?any_param? contains 'X' Then [Value] end)"
    ) == "unresolved"

    # Type C — dynamic SWITCH → param_switch
    assert classify_expression_type(
        "CASE WHEN ?p_timeview? contains 'MTD' Then [MTD] ELSE [Value] END"
    ) == "param_switch"

    # Type B — static SWITCH with strict equality → param_switch
    assert classify_expression_type(
        "case when ?foo?='MTD' then 'bar' else null end"
    ) == "param_switch"

    # Renamed parameter must NOT break classification (brief success criterion #2)
    assert classify_expression_type(
        "CASE WHEN ?completely_different_param? = 'XYZ' THEN [A] END"
    ) == "param_switch"

    # Type E — variance arithmetic
    assert classify_expression_type("[Actual] - [Budget]") == "variance"
    assert classify_expression_type("[Variance] / [Budget]") == "variance"

    # Type A — pure column reference
    assert classify_expression_type(
        "[C].[Module].[Table].[Column]"
    ) == "column_ref"

    # Type G — row context
    assert classify_expression_type("mod(RowNumber(),2)=0") == "row_context"

    print("Expression parser (report-agnostic) tests passed!")


if __name__ == "__main__":
    _test()