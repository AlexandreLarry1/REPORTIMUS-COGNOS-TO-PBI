# Cognos → Power BI Migration Pipeline — Project Brief

## What this project does

Automated Python pipeline that converts an IBM Cognos report (XML v17.5 spec) into a Power BI project (`.pbip` folder, April 2026 format), using flat CSV files as the data source.

**Inputs**
- `report.xml` — Cognos report spec (layout, CSS styles, filters, queries/measures)
- `*.csv` — raw flat data files (one row per date/account/entity)

**Output**
- `.pbip` folder — importable Power BI project containing:
  - `model.bim` — semantic model (tables, relationships, M expressions, DAX measures)
  - `report.json` — visual layout (matrices, slicers, conditional formatting, bookmarks)

---

## Architecture principle

Hybrid: deterministic where the transformation is mechanical, LLM only where Cognos logic has no direct DAX equivalent.

```
Phase 1 — Deterministic Parser   (0 LLM)  XML → structured JSON AST
Phase 2 — Targeted LLM           (N LLM)  Cognos logic → DAX measures
Phase 3 — Deterministic Generator (0 LLM) JSON AST + DAX → .pbip files
```

The pipeline must be **report-agnostic**: it should work on any Cognos XML, not just the finance sample used during development.

---

## Real expression taxonomy in Cognos XML

From analysis of a real Cognos finance report (28 expressions, 6 namedStyles):

| Type | Example | Correct treatment |
|---|---|---|
| A. Column reference | `[C].[Module].[Table].[Col]` | Deterministic — strip path, map to `'Table'[Col]` |
| B. Static SWITCH | `CASE WHEN ?p?='MTD' THEN 'MTD up to...'` | Deterministic — parse branches → `SWITCH(SELECTEDVALUE(...))` |
| C. Dynamic SWITCH | `CASE WHEN ?p_timeview? contains 'MTD' THEN [Val].[MTD]` | Deterministic — same branch mapping |
| D. Cognos date functions | `_add_months(?_as_of_date?, -1)`, `date('2022-01-31')` | **LLM required** — no direct DAX equivalent |
| E. Arithmetic variance | `[Actual]-[Budget]`, `[Variance]/[Budget]` | Deterministic — `[A]-[B]`, `DIVIDE([A],[B],0)` |
| F. Conditional formatting | `[AccountsLevel1] = 'Net Profit'` → HEX color | Deterministic for simple equality; LLM for complex multi-condition |
| G. Row context | `mod(RowNumber(),2)=0` → zebra striping | Deterministic |
| H. Aggregation context | `total(CASE WHEN ...)` | **LLM required** — maps to `CALCULATE(SUMX(...))` |

Types D and H are the only ones that genuinely require LLM. They represent ~15-25% of expressions in a typical finance report.

---

## Current implementation gaps

### Gap 1 — LLM task segregation is fine-tuned, not generalist

The 3-call architecture (`time_intelligence`, `parameter_logic`, `variance_formatting`) was designed around one specific report. Problems:

- `expression_parser.py` hardcodes `"?P_TIMEVIEW?"`, `"MTD"`, `"ACTUAL"`, `"BUDGET"` as detection keywords — any report with different parameter names or column names bypasses classification entirely.
- Call 1 generates MTD/QTD/YTD DAX measures assuming the CSV lacks aggregations. In the actual sample, those aggregations **are already CSV columns**. Call 1 generates duplicate logic.
- Call 2 only handles `CASE WHEN ?param? contains 'X'` — misses `?param? = 'X'` (strict equality) and compound conditions like `?p_timeview?='MTD' and ?_as_of_date? > date(...)`.
- Call 3 scans for `-` / `/` / `DIVIDE` in all expressions — catches any arithmetic, not just variance. HEX color values are already explicit in the XML `namedStyles` and don't need LLM at all.
- The `llm_context` object computed by `expression_parser.build_prompt_context()` is never passed to any LLM call — all classification work is unused.

### Gap 2 — Sequential chain creates consistency risk

Three separate calls forces the LLM to name measures without full context. Call 3 references measure names from Call 2 which references measure names from Call 1. Any naming inconsistency across calls produces broken DAX (measures referencing non-existent names). The chain is only justified if context exceeds the model window — which it doesn't (~28 expressions ≈ 8K tokens).

### Gap 3 — Deterministic paths are incomplete

- CSS → Power BI JSON style mapping exists in `pbip_builder.py` but conditional formatting injection (namedStyles → DAX color measures → visual `conditionalFormatting` JSON) is not wired end-to-end.
- `conditionalRender` (Cognos visibility logic via `refVariable`) is not converted to Power BI Bookmarks + Selection Panes (as specified in spec.md §6.2).
- Disconnected parameter tables (replacing `?param?` slicers) are not generated deterministically.

---

## Objective of this version: 1-prompt architecture

**Goal:** replace the 3-call chain with a single, context-complete LLM call that handles all non-deterministic expressions in one pass.

### What the single prompt receives

```
1. CSV schema          — actual table names + column names + inferred types
2. Classified expressions — output of expression_parser, grouped by type:
     - unresolved[]    — types D and H only (date functions, total() aggregations)
     - param_switches[] — all CASE WHEN ?param? expressions with parsed branches
     - variance[]      — arithmetic expressions between named measures
3. Named styles        — conditional formatting rules with explicit HEX values already extracted
4. Parameters list     — all ?param? names with their options/defaults
```

### What the single prompt returns

```json
{
  "measures": [
    {
      "name": "string",
      "expression": "DAX string",
      "type": "base_measure | switch_measure | variance | color",
      "source_expression": "original Cognos expression for traceability"
    }
  ],
  "parameter_tables": [
    {
      "name": "string",
      "column": "string",
      "values": ["string"]
    }
  ]
}
```

### What stays deterministic (never sent to LLM)

- Column reference stripping (`[C].[Module].[Table].[Col]` → `'Table'[Col]`)
- CASE branch parsing and SWITCH template fill-in
- `[A] - [B]` → `[A] - [B]`, `[A] / [B]` → `DIVIDE([A], [B], 0)`
- HEX color extraction from `namedStyles` with simple equality conditions
- Zebra striping row context
- MTD/QTD/YTD template measures (only generated if columns absent from CSV schema)

### Prompt design constraints

- Inject only the expressions the LLM actually needs to translate (types D and H) — not the full `xml_data` dump
- Include the CSV schema so the LLM anchors all table/column references to real names
- Include already-generated deterministic measure names so the LLM can reference them correctly in complex expressions
- Output is a flat list — no nested dependencies between items, so any order of generation is valid
- All Cognos-specific functions that need translation must be listed in the system prompt with their DAX equivalents as few-shot examples

### Success criteria for this version

1. The single prompt produces valid, importable DAX for the finance sample report
2. Changing the parameter name from `p_timeview` to anything else does not break classification
3. A report with no MTD/QTD/YTD in CSV columns gets template measures; one that has them does not
4. Zero hardcoded column names or parameter names in `expression_parser.py`
