"""Generate a Power BI Project (.pbip) from input/ CSVs + intermediate/visual_extraction.json."""
import argparse
import csv
import json
import os
import pathlib
import uuid
from dotenv import load_dotenv
load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent

REPORT_NAME = "MigrationQlikPBI"
CANVAS_W    = 1280.0
CANVAS_H    = 720.0
QLIK_COLS   = 30
QLIK_ROWS   = 15

VIZ_MAP = {
    "barchart":           "barChart",
    "linechart":          "lineChart",
    "combochart":         "lineClusteredColumnComboChart",
    "piechart":           "donutChart",
    "kpi":                "card",
    "table":              "tableEx",
    "sn-table":           "tableEx",
    "pivot-table":        "pivotTable",
    "listbox":            "slicer",
    "filterpane":         "slicer",
    "text-image":         "textbox",
    "sn-text":            "textbox",
    "scatterplot":        "scatterChart",
    "scatterChart":       "scatterChart",
    "histogram":          "columnChart",
    "gauge":              "gauge",
    "treemap":            "treemap",
    "map":                "map",
    "waterfall":          "waterfallChart",
    "sn-layout-container":"textbox",
    "action-button":      "textbox",
}


def _uid() -> str:
    return str(uuid.uuid4())

def _hex20() -> str:
    return uuid.uuid4().hex[:20]

def _read_csv_headers(path: pathlib.Path) -> list[str]:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                headers = next(csv.reader(f))
            if all(h.isprintable() for h in headers):
                return headers
        except Exception:
            continue
    return []


def _infer_col_types(path: pathlib.Path, sample: int = 200) -> dict[str, str]:
    """Return {col_name: pbi_dataType} by sampling up to `sample` rows."""
    import re as _re
    _DATE_RE = _re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                reader = csv.reader(f)
                headers = next(reader)
                rows = [r for _, r in zip(range(sample), reader)]
            break
        except Exception:
            continue
    else:
        return {}

    _STRIP = str.maketrans("", "", ",$%() ")
    _NULL_VALS = {"", "-", "n/a", "null", "none", "#n/a", "#value!", "#ref!"}

    def _clean(v: str) -> str:
        return v.strip().lstrip("$€£").translate(_STRIP).lstrip("-")

    def _ratio(vals: list, pred) -> float:
        hits = sum(1 for v in vals if pred(v))
        return hits / len(vals) if vals else 0

    result: dict[str, str] = {}
    for i, col in enumerate(headers):
        vals = [r[i].strip() for r in rows if i < len(r) and r[i].strip().lower() not in _NULL_VALS]
        if not vals:
            result[col] = "string"
            continue
        def _is_int(v):
            try: int(_clean(v)); return True
            except: return False
        if _ratio(vals, _is_int) >= 0.9:
            result[col] = "int64"
            continue
        def _is_float(v):
            try: float(_clean(v)); return True
            except: return False
        if _ratio(vals, _is_float) >= 0.9:
            result[col] = "double"
            continue
        if _ratio(vals[:50], lambda v: bool(_DATE_RE.match(v))) >= 0.9:
            result[col] = "dateTime"
            continue
        result[col] = "string"
    return result

def _write(path: pathlib.Path, data: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else data
    path.write_text(content, encoding="utf-8")

def _platform(item_type: str, display_name: str) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": item_type, "displayName": display_name},
        "config":   {"version": "2.0", "logicalId": _uid()},
    }

def _m_expression(csv_path: pathlib.Path, n_cols: int, col_types: dict[str, str] | None = None) -> list[str]:
    lines = [
        "let",
        f'    Source = Csv.Document(File.Contents("{csv_path.as_posix()}"),'
        f'[Delimiter=",", Columns={n_cols}, Encoding=65001, QuoteStyle=QuoteStyle.Csv]),',
        '    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true])',
    ]
    transforms = []
    if col_types:
        for col, dt in col_types.items():
            if dt in ("double", "int64"):
                transforms.append(f'{{"{col}", each try Number.From(_) otherwise null, type nullable number}}')
            elif dt == "dateTime":
                transforms.append(f'{{"{col}", each try DateTime.From(_) otherwise null, type nullable datetime}}')
    if transforms:
        t_list = ", ".join(transforms)
        lines[-1] += ","
        lines.append(f'    #"Changed Types" = Table.TransformColumns(#"Promoted Headers", {{{t_list}}})')
        lines.append("in")
        lines.append('    #"Changed Types"')
    else:
        lines.append("in")
        lines.append('    #"Promoted Headers"')
    return lines

_NUMERIC_SUMMARIZE = {"int64": "sum", "double": "sum"}

def _bim_column(name: str, data_type: str = "string") -> dict:
    summarize = _NUMERIC_SUMMARIZE.get(data_type, "none")
    col: dict = {"name": name, "lineageTag": _uid(), "dataType": data_type,
                 "sourceColumn": name, "summarizeBy": summarize}
    if data_type == "dateTime":
        col["formatString"] = "General Date"
    return col

def _bim_table(csv_path: pathlib.Path) -> dict | None:
    headers = _read_csv_headers(csv_path)
    if not headers:
        return None
    types = _infer_col_types(csv_path)
    cols  = [_bim_column(h, types.get(h, "string")) for h in headers]
    type_summary = {}
    for c in cols:
        type_summary.setdefault(c["dataType"], 0)
        type_summary[c["dataType"]] += 1
    print(f"    types: {type_summary}")
    return {
        "name":       csv_path.stem,
        "lineageTag": _uid(),
        "columns":    cols,
        "measures":   [],
        "partitions": [{"name": "Partition", "mode": "import", "source": {
            "type": "m", "expression": _m_expression(csv_path, len(headers), types)
        }}],
    }


import re as _re_rel


def _canonical_dim(col: str, dim_tbls: list[str]) -> str | None:
    """Return the ONE dim table whose entity name best matches the column name.

    ClientID   → dim_clients       (base 'client'   ⊂ 'clients')
    PortefeuilleID → dim_portefeuilles  (base 'portefeuille' ⊂ 'portefeuilles')
    If no match, return None → no relationship created for this column.
    """
    base = _re_rel.sub(r'(?i)(ID|Key|Ref|Code)$', '', col).lower().replace('_', '')
    if not base:
        return None
    scored = []
    for dim in dim_tbls:
        entity = dim.removeprefix('dim_').lower().replace('_', '').rstrip('s')
        common = 0
        for a, b in zip(base, entity):
            if a == b:
                common += 1
            else:
                break
        if common >= min(3, len(entity)):
            scored.append((common, dim))
    return max(scored, key=lambda x: x[0])[1] if scored else None


def _infer_relationships(tables: list) -> list:
    """Infer fact→dim relationships using canonical dim matching per FK column.

    Each FK column maps to exactly ONE dim table (by name similarity), preventing
    ambiguous multi-path errors in Power BI when multiple tables share a column.
    """
    col_index: dict[str, list[str]] = {}
    for t in tables:
        for c in t.get("columns", []):
            col_index.setdefault(c["name"], []).append(t["name"])

    relationships = []
    seen: set = set()

    for col, tbls in col_index.items():
        if len(tbls) < 2:
            continue
        if not _re_rel.search(r'(?i)(ID|Key|Ref|Code)$', col):
            continue
        dim_tbls  = [t for t in tbls if t.startswith("dim_")]
        fact_tbls = [t for t in tbls if t.startswith("fact_")]
        if not dim_tbls or not fact_tbls:
            continue
        canonical = _canonical_dim(col, dim_tbls)
        if not canonical:
            continue
        for fact in fact_tbls:
            key = (fact, canonical, col)
            if key in seen:
                continue
            seen.add(key)
            relationships.append({
                "name": f"{fact}_{canonical}_{col}",
                "fromTable": fact,
                "fromColumn": col,
                "toTable": canonical,
                "toColumn": col,
            })
            print(f"  ~ relation: {fact}[{col}] → {canonical}[{col}]")
    return relationships


def build_semantic_model(csv_dir: pathlib.Path, out_root: pathlib.Path) -> list[str]:
    sm_dir  = out_root / f"{REPORT_NAME}.SemanticModel"
    tables  = []
    for csv_path in sorted(csv_dir.glob("*.csv")):
        t = _bim_table(csv_path)
        if t:
            tables.append(t)
            print(f"  + table '{t['name']}' ({len(t['columns'])} cols) <- {csv_path.name}")

    relationships = _infer_relationships(tables)
    bim = {
        "name": "SemanticModel",
        "compatibilityLevel": 1550,
        "model": {
            "culture": "fr-FR",
            "dataAccessOptions": {"legacyRedirects": True, "returnErrorValuesAsNull": True},
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "fr-FR",
            "tables": tables,
            "relationships": relationships,
        },
    }
    _write(sm_dir / "model.bim", bim)
    _write(sm_dir / "definition.pbism", {"version": "1.0", "settings": {}})
    _write(sm_dir / ".platform", _platform("SemanticModel", REPORT_NAME))
    print(f"  -> {sm_dir / 'model.bim'}  ({len(relationships)} relations)")
    return [t["name"] for t in tables]


_REPORT_CONFIG = json.dumps({
    "version": "5.72",
    "themeCollection": {"baseTheme": {"name": "CY26SU04",
        "version": {"visual": "2.8.0", "report": "3.2.0", "page": "2.3.1"}, "type": 2}},
    "activeSectionIndex": 0,
    "defaultDrillFilterOtherVisuals": True,
    "linguisticSchemaSyncVersion": 2,
    "settings": {
        "useNewFilterPaneExperience": True, "allowChangeFilterTypes": True,
        "useStylableVisualContainerHeader": True, "queryLimitOption": 6,
        "useEnhancedTooltips": True, "exportDataMode": 1,
        "useDefaultAggregateDisplayName": True,
    },
    "objects": {"section": [{"properties": {"verticalAlignment": {"expr": {"Literal": {"Value": "'Top'"}}}}}]},
}, ensure_ascii=False, separators=(',', ':'))


def _qlik_to_px(col: int, row: int, colspan: int, rowspan: int) -> tuple:
    cw = CANVAS_W / QLIK_COLS
    rh = CANVAS_H / QLIK_ROWS
    return round(col * cw, 1), round(row * rh, 1), round(colspan * cw, 1), round(rowspan * rh, 1)


import math as _math

def _flatten_objects(objects: list) -> list:
    """Flatten containers into children, auto-layouting if child positions are missing."""
    result = []
    for obj in objects:
        children = obj.get("children", [])
        if obj.get("type") == "sn-layout-container" and children:
            result.append(obj)
            p = obj.get("layout", {})
            p_col, p_row = p.get("col", 0), p.get("row", 0)
            p_cs, p_rs   = p.get("colspan", 30), p.get("rowspan", 15)
            real = [c for c in children
                    if c.get("layout", {}).get("col", 0) or c.get("layout", {}).get("row", 0)
                    or c.get("layout", {}).get("colspan", 1) > 1 or c.get("layout", {}).get("rowspan", 1) > 1]
            if real:
                for child in children:
                    cl = child.get("layout", {})
                    result.append({**child, "layout": {
                        "col": p_col + cl.get("col", 0),
                        "row": p_row + cl.get("row", 0),
                        "colspan": max(cl.get("colspan", 1), 1),
                        "rowspan": max(cl.get("rowspan", 1), 1),
                    }})
            else:
                n    = len(children)
                ncols = max(1, _math.ceil(_math.sqrt(n)))
                nrows = max(1, _math.ceil(n / ncols))
                cw   = max(1, p_cs // ncols)
                rh   = max(1, p_rs // nrows)
                for i, child in enumerate(children):
                    result.append({**child, "layout": {
                        "col":     p_col + (i % ncols) * cw,
                        "row":     p_row + (i // ncols) * rh,
                        "colspan": cw,
                        "rowspan": rh,
                    }})
        else:
            result.append(obj)
    return result


def _visual_container(obj: dict, tab_order: int) -> dict:
    viz_type = VIZ_MAP.get(obj.get("type", ""), "card")
    layout   = obj.get("layout", {})
    x, y, w, h = _qlik_to_px(
        layout.get("col", 0), layout.get("row", 0),
        max(layout.get("colspan", 4), 3), max(layout.get("rowspan", 3), 2),
    )
    config = json.dumps({
        "name": obj.get("id", _uid()),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": tab_order,
                                            "width": w, "height": h, "tabOrder": tab_order}}],
        "singleVisual": {"visualType": viz_type, "drillFilterOtherVisuals": True, "objects": {}},
    }, ensure_ascii=False, separators=(',', ':'))

    return {"config": config, "filters": "[]", "height": h, "width": w, "x": x, "y": y, "z": tab_order}


def _section(sheet: dict, ordinal: int) -> dict:
    flat = _flatten_objects(sheet.get("objects", []))
    containers = [_visual_container(obj, (i + 1) * 1000)
                  for i, obj in enumerate(flat) if "error" not in obj]
    section = {
        "config": "{}", "displayName": sheet.get("title", f"Page {ordinal + 1}"),
        "displayOption": 1, "filters": "[]", "height": CANVAS_H,
        "name": _hex20(), "visualContainers": containers, "width": CANVAS_W,
    }
    if ordinal > 0:
        section["ordinal"] = ordinal
    return section


def build_report(intermediate: pathlib.Path, out_root: pathlib.Path) -> None:
    report_dir  = out_root / f"{REPORT_NAME}.Report"
    visual_json = intermediate / "visual_extraction.json"

    sheets = []
    if visual_json.exists():
        data   = json.loads(visual_json.read_text(encoding="utf-8"))
        sheets = data.get("sheets", [])
        print(f"  {len(sheets)} sheets found")
    else:
        print("  no visual_extraction.json — empty report")

    sections = [_section(s, i) for i, s in enumerate(sheets)]
    if not sections:
        sections = [{"config": "{}", "displayName": "Page 1", "displayOption": 1,
                     "filters": "[]", "height": CANVAS_H, "name": _hex20(),
                     "visualContainers": [], "width": CANVAS_W}]

    report = {
        "config": _REPORT_CONFIG,
        "layoutOptimization": 0,
        "resourcePackages": [{"resourcePackage": {
            "disabled": False,
            "items": [{"name": "CY26SU04", "path": "BaseThemes/CY26SU04.json", "type": 202}],
            "name": "SharedResources", "type": 2,
        }}],
        "sections": sections,
    }

    qlik_meta = {}
    for sheet in sheets:
        for obj in _flatten_objects(sheet.get("objects", [])):
            if "error" not in obj and obj.get("id"):
                qlik_meta[obj["id"]] = {
                    "sheet": sheet.get("title", ""), "type": obj.get("type", ""),
                    "title": obj.get("title", ""), "dimensions": obj.get("dimensions", []),
                    "measures": obj.get("measures", []),
                }
    _write(report_dir / "qlik_sidecar.json", qlik_meta)
    _write(report_dir / "report.json", report)
    _write(report_dir / "definition.pbir", {
        "version": "1.0",
        "datasetReference": {"byPath": {"path": f"../{REPORT_NAME}.SemanticModel"}},
    })
    _write(report_dir / ".platform", _platform("Report", REPORT_NAME))

    for s in sections:
        print(f"    section '{s['displayName']}' — {len(s['visualContainers'])} visuals")
    print(f"  -> {report_dir / 'report.json'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--example", default=os.getenv("EXAMPLE_NAME", ""))
    args = parser.parse_args()

    example      = args.example
    example_dir  = ROOT / "examples" / example
    intermediate = example_dir / "intermediate"
    out_dir      = example_dir / "pbip"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_dir}\n")

    print("=== Semantic model ===")
    build_semantic_model(example_dir / "input", out_dir)
    print("\n=== Report ===")
    build_report(intermediate, out_dir)
    print("\n=== Entry point ===")
    _write(out_dir / f"{REPORT_NAME}.pbip", {
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{REPORT_NAME}.Report"}}],
    })
    print(f"\nDone -> {out_dir / f'{REPORT_NAME}.pbip'}")


if __name__ == "__main__":
    main()
