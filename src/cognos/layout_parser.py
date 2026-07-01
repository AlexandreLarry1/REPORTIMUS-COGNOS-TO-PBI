"""Convertisseur layout Cognos vers visual_extraction.json compatible.

Gère trois familles de visuels Cognos :
  - <vizControl>   → graphiques IBM (floatingBar, line, pie, etc.) → chart PBI
  - <listControl>  → tableau tabulaire → tableEx
  - <crosstab>     → tableau croisé → matrix (pour rapports sans vizControl)
  - <selectValue>  → slicer paramètre

Mappings IBM → PBI :
  Native   : clusteredBar, line, pie, donut, waterfall, treemap, scatter, combo, map, gauge
  Fallback : heatmap, wordcloud, radar, network, river, marimekko, packedBubble, boxplot → tableEx
"""
import json
import uuid
from typing import Any

from cognos.css_parser import parse_css_string, css_to_pbi_format
from cognos.xml_parser import NS
from lxml import etree


# ---------------------------------------------------------------------------
# IBM vizControl type → (PBI type, is_native)
# ---------------------------------------------------------------------------

_IBM_TO_PBI: dict[str, tuple[str, bool]] = {
    # Direct PBI equivalents
    "clusteredBar":              ("clusteredBarChart", True),
    "clusteredColumn":           ("clusteredColumnChart", True),
    "stackedBar":                ("stackedBarChart", True),
    "stackedColumn":             ("stackedColumnChart", True),
    "line":                      ("lineChart", True),
    "smoothLine":                ("lineChart", True),
    "stepLine":                  ("lineChart", True),
    "area":                      ("areaChart", True),
    "smoothArea":                ("areaChart", True),
    "stepArea":                  ("areaChart", True),
    "pie":                       ("pieChart", True),
    "donut":                     ("donutChart", True),
    "waterfall":                 ("waterfallChart", True),
    "treemap":                   ("treemap", True),
    "scatter":                   ("scatterChart", True),
    "bubble":                    ("scatterChart", True),
    "point":                     ("scatterChart", True),
    "quadrant":                  ("scatterChart", True),
    "stackedCombination":        ("lineClusteredColumnComboChart", True),
    "clusteredCombination":      ("lineClusteredColumnComboChart", True),
    "dial":                      ("gauge", True),
    "map":                       ("map", True),
    "tiledmap":                  ("map", True),
    # Approximations (native PBI type exists but semantics differ)
    "floatingBar":               ("clusteredBarChart", True),
    "floatingColumn":            ("clusteredColumnChart", True),
    "targetBar":                 ("clusteredBarChart", True),
    "targetColumn":              ("clusteredColumnChart", True),
    "bullet":                    ("clusteredBarChart", True),
    # Approximations to native PBI types (semantics differ, noted in migration_note)
    "heatmap":                   ("matrix", True),
    "wordcloud":                 ("treemap", True),
    "radar":                     ("lineChart", True),
    "river":                     ("stackedAreaChart", True),
    "marimekko":                 ("hundredPercentStackedColumnChart", True),
    "packedBubble":              ("scatterChart", True),
    "hierarchicalPackedBubble":  ("treemap", True),
    # No native PBI equivalent → tableEx fallback
    "boxplot":                   ("tableEx", False),
    "network":                   ("tableEx", False),
}

_APPROXIMATED = {
    "floatingBar", "floatingColumn", "targetBar", "targetColumn", "bullet",
    "heatmap", "wordcloud", "radar", "river", "marimekko", "packedBubble", "hierarchicalPackedBubble",
}

_APPROXIMATION_NOTES = {
    "heatmap":                  "heatmap → matrix (add conditional formatting manually to recreate color scale)",
    "wordcloud":                "wordcloud → treemap (word size ≈ tile area; no font rendering)",
    "radar":                    "radar → lineChart (same axes/values, not circular)",
    "river":                    "river/streamgraph → stackedAreaChart (flow over time preserved)",
    "marimekko":                "marimekko → 100% stacked column (proportions preserved, variable width lost)",
    "packedBubble":             "packedBubble → scatterChart (bubbles with size; no circular packing)",
    "hierarchicalPackedBubble": "hierarchicalPackedBubble → treemap (hierarchy + size preserved)",
}


def parse_cognos_layout(xml_data: dict) -> dict:
    """Convertit xml_data Cognos vers visual_extraction.json.

    Priorité :
      1. vizControl + listControl (rapports avec graphiques IBM)
      2. crosstab (rapports tabulaires classiques)
    """
    data_stores = xml_data.get("data_stores", {})
    viz_controls = xml_data.get("viz_controls", [])
    list_controls = xml_data.get("list_controls", [])
    crosstabs = xml_data.get("crosstabs", [])

    # --- Route par type de rapport ---
    select_values = xml_data.get("select_values", [])
    queries = xml_data.get("queries", {})

    if viz_controls or list_controls:
        sheets = _build_sheets_from_viz(viz_controls, list_controls, select_values, data_stores, queries)
    else:
        sheets = _build_sheets_from_crosstabs(xml_data, crosstabs)

    # Fallback : un sheet vide nommé Page1
    if not sheets:
        sheets = [{"id": str(uuid.uuid4()), "title": "Page1", "objects": []}]

    return {
        "metadata": {"report_name": "Cognos Report", "source": "cognos_xml"},
        "sheets": sheets,
    }


# ---------------------------------------------------------------------------
# Route A : vizControl + listControl
# ---------------------------------------------------------------------------

def _build_sheets_from_viz(
    viz_controls: list[dict],
    list_controls: list[dict],
    select_values: list[dict],
    data_stores: dict,
    queries: dict | None = None,
) -> list[dict]:
    """Construit les sheets en groupant les visuels par page d'origine."""
    pages: dict[str, list[dict]] = {}

    for vc in viz_controls:
        page = vc.get("page", "Page1")
        obj = _parse_viz_control(vc, data_stores)
        if obj:
            pages.setdefault(page, []).append(obj)

    for lc in list_controls:
        page = lc.get("page", "Page1")
        obj = _parse_list_control(lc, data_stores)
        if obj:
            pages.setdefault(page, []).append(obj)

    for sv in select_values:
        page = sv.get("page", "Page1")
        obj = _parse_select_value(sv, data_stores, queries or {})
        if obj:
            pages.setdefault(page, []).append(obj)

    return [
        {"id": str(uuid.uuid4()), "title": page_name, "objects": objects}
        for page_name, objects in pages.items()
    ]


def _parse_viz_control(vc: dict, data_stores: dict) -> dict | None:
    """Parse un vizControl IBM → objet visuel PBI."""
    name = vc["name"]
    ibm_type = vc["ibm_type"]

    pbi_type, is_native = _IBM_TO_PBI.get(ibm_type, ("tableEx", False))

    # Resolve dataStore → query + available fields
    store = data_stores.get(vc["ref_data_store"], {})
    ref_query = store.get("refQuery", "")
    available_fields = store.get("fields", [])

    migration_note: str | None = None
    if not is_native:
        migration_note = f"No native Power BI equivalent for '{ibm_type}'. Degraded to tableEx."
    elif ibm_type in _APPROXIMATION_NOTES:
        migration_note = _APPROXIMATION_NOTES[ibm_type]
    elif ibm_type in _APPROXIMATED:
        migration_note = (
            f"'{ibm_type}' approximated as {pbi_type} — some semantics lost "
            f"(range/target values may not render correctly)."
        )

    obj: dict = {
        "id": name,
        "type": pbi_type,
        "ibm_type": ibm_type,
        "title": vc.get("title", name),
        "layout": {"col": 0, "row": 0, "colspan": 6, "rowspan": 8},
        "query": ref_query,
        "slots": vc["slots"],
        "available_fields": available_fields,
        "baselines": vc.get("baselines", []),
        "text_labels": vc.get("text_labels", []),
        "properties": vc.get("properties", {}),
    }
    if migration_note:
        obj["migration_note"] = migration_note

    return obj


def _parse_select_value(sv: dict, data_stores: dict, queries: dict | None = None) -> dict | None:
    """Parse un selectValue Cognos → slicer PBI."""
    name = sv["name"]
    ref_query = sv["ref_query"]
    param_name = sv.get("param_name", "")

    # Resolve field: use direct field_ref first (P1 fix), then dataStore fallback
    field_ref = sv.get("field_ref", "")
    if field_ref:
        slicer_field = f"{ref_query}[{field_ref}]"
    else:
        slicer_field = ""
        for store in data_stores.values():
            if store.get("refQuery") == ref_query and store.get("fields"):
                slicer_field = f"{ref_query}[{store['fields'][0]}]"
                break
        if not slicer_field and queries and ref_query in queries:
            items = queries[ref_query].get("dataItems", [])
            if items:
                first_field = items[0].get("name", "")
                if first_field:
                    slicer_field = f"{ref_query}[{first_field}]"

    header_text = sv.get("header_text", "") or name.replace("_", " ").title()
    default_value = sv.get("default_value", "")

    return {
        "id": name,
        "type": "slicer",
        "title": header_text,
        "layout": {"col": 0, "row": 0, "colspan": 3, "rowspan": 1},
        "slicer_field": slicer_field,
        "slicer_type": "string",
        "query": ref_query,
        "param_name": param_name,
        "default_value": default_value,
    }


def _parse_list_control(lc: dict, data_stores: dict) -> dict | None:
    """Parse un listControl Cognos → tableEx PBI."""
    name = lc["name"] or "ListControl"
    store = data_stores.get(lc["ref_data_store"], {})
    ref_query = store.get("refQuery", "")

    # Colonnes : depuis lcColumn.refDataItem si disponible, sinon depuis le dataStore
    columns = lc.get("columns") or store.get("fields", [])

    return {
        "id": name,
        "type": "tableEx",
        "ibm_type": "listControl",
        "title": name.replace("_", " ").title(),
        "layout": {"col": 0, "row": 0, "colspan": 12, "rowspan": 8},
        "query": ref_query,
        "columns": columns,
    }


# ---------------------------------------------------------------------------
# Route B : crosstab (rapports sans vizControl)
# ---------------------------------------------------------------------------

def _build_sheets_from_crosstabs(xml_data: dict, crosstabs: list) -> list[dict]:
    """Construit les sheets depuis les crosstabs (comportement original)."""
    sheets = []
    matched_ct_names: set[str] = set()

    for page in xml_data.get("layouts", {}).get("pages", []):
        sheet = {
            "id": str(uuid.uuid4()),
            "title": page.get("name", "Page1"),
            "objects": [],
        }

        for obj in page.get("objects", []):
            obj_type = obj.get("type", "")

            if obj_type == "crosstab":
                obj_name = obj.get("name", "")
                for ct in crosstabs:
                    if ct.get("name") == obj_name:
                        matched_ct_names.add(obj_name)
                        parsed = _parse_crosstab(ct, xml_data)
                        if parsed:
                            sheet["objects"].append(parsed)
                        break

            elif obj_type == "table":
                for ct in crosstabs:
                    if ct.get("name") not in matched_ct_names:
                        matched_ct_names.add(ct.get("name", ""))
                        parsed = _parse_crosstab(ct, xml_data)
                        if parsed:
                            sheet["objects"].append(parsed)

            elif obj_type == "singleton":
                parsed = _parse_singleton(obj, xml_data)
                if parsed:
                    sheet["objects"].append(parsed)

        sheets.append(sheet)

    if not sheets and crosstabs:
        sheet = {
            "id": str(uuid.uuid4()),
            "title": "Page1",
            "objects": [p for ct in crosstabs if (p := _parse_crosstab(ct, xml_data))],
        }
        sheets.append(sheet)

    return sheets


def _parse_crosstab(crosstab_data: dict, xml_data: dict) -> dict | None:
    name = crosstab_data.get("name", "")
    query_name = crosstab_data.get("refQuery", "")
    query = xml_data.get("queries", {}).get(query_name, {})
    query_items = {item.get("name", ""): item for item in query.get("dataItems", [])}

    row_dims: list[str] = []
    seen_row: set[str] = set()
    for row in crosstab_data.get("rows", []):
        for member in row.get("members", []):
            ref = member.get("refDataItem", "")
            if ref and "(" not in ref and ref not in seen_row:
                seen_row.add(ref)
                row_dims.append(ref)

    col_dims: list[str] = []
    seen_col: set[str] = set()
    for col in crosstab_data.get("columns", []):
        for member in col.get("members", []):
            ref = member.get("refDataItem", "")
            if ref and "(" not in ref and ref not in seen_col and ref not in seen_row:
                seen_col.add(ref)
                col_dims.append(ref)

    dim_names = seen_row | seen_col
    measures: list[dict] = [
        {"name": item_name, "label": item.get("label", item_name)}
        for item_name, item in query_items.items()
        if item_name and item_name not in dim_names
    ]

    conditional_styles: list[dict] = []
    for inter in crosstab_data.get("intersections", []):
        for cs_ref in inter.get("conditionalStyles", []):
            if cs_ref:
                style_data = xml_data.get("namedStyles", {}).get(cs_ref, {})
                if style_data and style_data.get("type") == "range":
                    conditional_styles.append({
                        "refDataItem": style_data.get("refDataItem", ""),
                        "type": "range",
                        "ranges": style_data.get("ranges", []),
                    })

    return {
        "id": name,
        "type": "matrix",
        "title": name.replace("_", " ").title(),
        "layout": {"col": 0, "row": 0, "colspan": 12, "rowspan": 8},
        "row_dimensions": row_dims,
        "col_dimensions": col_dims,
        "measures": measures,
        "conditional_styles": conditional_styles,
        "query": query_name,
    }


def _parse_singleton(singleton_data: dict, xml_data: dict) -> dict | None:
    name = singleton_data.get("name", "")
    query_name = singleton_data.get("refQuery", "")
    query = xml_data.get("queries", {}).get(query_name, {})
    measures = [
        {"expression": f"FIRST([{item.get('name','')}])", "label": item.get("label", item.get("name",""))}
        for item in query.get("dataItems", [])
        if item.get("name")
    ]
    return {
        "id": name,
        "type": "card",
        "title": name.replace("_", " ").title(),
        "layout": {"col": 0, "row": 0, "colspan": 4, "rowspan": 2},
        "dimensions": [],
        "measures": measures,
    }


# ---------------------------------------------------------------------------
# Slicers depuis paramètres (commune aux deux routes)
# ---------------------------------------------------------------------------

def parse_parameters_as_slicers(xml_data: dict) -> list:
    slicers = []
    col_offset = 0
    for param in xml_data.get("parameters", []):
        param_name = param.get("name", "")
        if not param_name:
            continue
        slicer_type = "date" if "date" in param_name.lower() else "string"
        slicers.append({
            "id": f"slicer_{param_name}",
            "type": "slicer",
            "title": param_name.replace("_", " ").title(),
            "layout": {"col": col_offset, "row": 0, "colspan": 3, "rowspan": 1},
            "slicer_field": f"Param_{param_name}[{param_name}]",
            "slicer_type": slicer_type,
            "options": [opt.get("value", "") for opt in param.get("options", [])],
        })
        col_offset += 3
    return slicers


def add_slicers_to_sheets(visual_data: dict, xml_data: dict) -> dict:
    slicers = parse_parameters_as_slicers(xml_data)
    if slicers and visual_data.get("sheets"):
        first_sheet = visual_data["sheets"][0]
        row_offset = max(
            (obj.get("layout", {}).get("row", 0) + obj.get("layout", {}).get("rowspan", 1)
             for obj in first_sheet.get("objects", [])),
            default=0,
        )
        for slicer in slicers:
            slicer["layout"]["row"] = row_offset
            first_sheet.setdefault("objects", []).append(slicer)
            row_offset += 1
    return visual_data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import pathlib

    parser = argparse.ArgumentParser(description="Parseur layout Cognos vers visual_extraction.json")
    parser.add_argument("json_file", help="Fichier JSON produit par xml_parser.py")
    parser.add_argument("-o", "--output", help="Fichier JSON de sortie")
    args = parser.parse_args()

    input_path = pathlib.Path(args.json_file)
    if not input_path.exists():
        print(f"Erreur: fichier introuvable {input_path}")
        return

    xml_data = json.loads(input_path.read_text(encoding="utf-8"))
    visual_data = parse_cognos_layout(xml_data)
    visual_data = add_slicers_to_sheets(visual_data, xml_data)

    output_path = pathlib.Path(args.output) if args.output else input_path.parent / "visual_extraction.json"
    output_path.write_text(json.dumps(visual_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Layout Cognos → {output_path}")
    print(f"  {len(visual_data['sheets'])} pages")
    for sheet in visual_data["sheets"]:
        print(f"    '{sheet['title']}': {len(sheet['objects'])} objects")


if __name__ == "__main__":
    main()
