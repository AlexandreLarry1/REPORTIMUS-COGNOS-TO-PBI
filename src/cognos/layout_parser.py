"""Convertisseur layout Cognos vers visual_extraction.json compatible.

Convertit les éléments de layout Cognos (crosstab, selectValue, textItem, etc.)
vers le format JSON attendu par pbip_builder.py et visual_translator.py.

Mappings:
- <crosstab> → {type: "pivotTable"}
- <selectValue> → {type: "slicer"}
- <selectDate> → {type: "slicer", fieldType: "date"}
- <textItem><staticValue> → {type: "textbox"}
- <singleton> → {type: "card"}
"""
import json
import uuid
from typing import Any

from cognos.css_parser import parse_css_string, css_to_pbi_format
from cognos.xml_parser import NS
from lxml import etree


# Mapping des types Cognos vers Power BI
TYPE_MAP = {
    "crosstab": "pivotTable",
    "table": "tableEx",
    "selectValue": "slicer",
    "selectDate": "slicer",
    "textItem": "textbox",
    "singleton": "card",
    "promptButton": "button"
}


def parse_cognos_layout(xml_data: dict) -> dict:
    """Convertit les données XML Cognos vers visual_extraction.json.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()

    Returns:
        Dict compatible avec le pipeline existant:
        {
            "metadata": {...},
            "sheets": [{title, objects: [...]}]
        }
    """
    sheets = []

    # Extraire les pages depuis xml_data.layouts
    for page in xml_data.get("layouts", {}).get("pages", []):
        sheet = {
            "id": str(uuid.uuid4()),
            "title": page.get("name", "Page1"),
            "objects": []
        }

        # Traiter les objets de la page
        for obj in page.get("objects", []):
            parsed_obj = _parse_object(obj, xml_data)
            if parsed_obj:
                sheet["objects"].append(parsed_obj)

        sheets.append(sheet)

    # Fallback: si pas de pages, créer une page par défaut avec les crosstabs
    if not sheets and xml_data.get("crosstabs"):
        sheet = {
            "id": str(uuid.uuid4()),
            "title": "Page1",
            "objects": []
        }
        for ct in xml_data["crosstabs"]:
            parsed = _parse_crosstab(ct, xml_data)
            if parsed:
                sheet["objects"].append(parsed)
        sheets.append(sheet)

    return {
        "metadata": {
            "report_name": "Cognos Report",
            "source": "cognos_xml"
        },
        "sheets": sheets
    }


def _parse_object(obj: dict, xml_data: dict) -> dict | None:
    """Parse un objet de layout."""
    obj_type = obj.get("type", "")
    obj_name = obj.get("name", "")

    if obj_type == "crosstab":
        # Trouver le crosstab correspondant dans xml_data
        for ct in xml_data.get("crosstabs", []):
            if ct.get("name") == obj_name:
                return _parse_crosstab(ct, xml_data)

    elif obj_type == "singleton":
        return _parse_singleton(obj, xml_data)

    elif obj_type == "table":
        return _parse_table(obj)

    return None


def _parse_crosstab(crosstab_data: dict, xml_data: dict) -> dict:
    """Parse un crosstab Cognos vers objet visuel PBI."""
    name = crosstab_data.get("name", "")
    query_name = crosstab_data.get("refQuery", "")

    # Extraire dimensions depuis rows/columns
    dimensions = []
    measures = []

    # Rows → dimensions
    for row in crosstab_data.get("rows", []):
        for member in row.get("members", []):
            ref_item = member.get("refDataItem", "")
            if ref_item:
                dimensions.append({
                    "field": ref_item,
                    "label": ref_item
                })

    # Columns → dimensions (catégories)
    for col in crosstab_data.get("columns", []):
        for member in col.get("members", []):
            ref_item = member.get("refDataItem", "")
            if ref_item and ref_item not in [d["field"] for d in dimensions]:
                dimensions.append({
                    "field": ref_item,
                    "label": ref_item
                })

    # Extraire les mesures depuis les query dataItems
    query = xml_data.get("queries", {}).get(query_name, {})
    for item in query.get("dataItems", []):
        item_name = item.get("name", "")
        item_label = item.get("label", item_name)
        agg = item.get("aggregate", "")

        if item_name and agg != "none":
            # C'est une mesure
            measures.append({
                "expression": f"SUM([{item_name}])",
                "label": item_label
            })
        elif item.get("expression"):
            # Colonne calculée
            measures.append({
                "expression": item["expression"],
                "label": item_label
            })

    # Style et formatage
    style = {}
    corner_style = crosstab_data.get("styles", {}).get("corner", {})
    if corner_style:
        style.update(css_to_pbi_format(corner_style))

    # Conditional styles
    conditional_styles = []
    for inter in crosstab_data.get("intersections", []):
        for cs_ref in inter.get("conditionalStyles", []):
            if cs_ref:
                style_data = xml_data.get("namedStyles", {}).get(cs_ref, {})
                if style_data and style_data.get("type") == "range":
                    conditional_styles.append({
                        "refDataItem": style_data.get("refDataItem", ""),
                        "type": "range",
                        "ranges": style_data.get("ranges", [])
                    })

    # Layout grid (estimation simple)
    layout = {
        "col": 0,
        "row": 0,
        "colspan": 12,
        "rowspan": 8
    }

    return {
        "id": name,
        "type": "pivotTable",
        "title": name.replace("_", " ").title(),
        "layout": layout,
        "dimensions": dimensions,
        "measures": measures,
        "style": style,
        "conditional_styles": conditional_styles,
        "conditionalRender": crosstab_data.get("conditionalRender")
    }


def _parse_singleton(singleton_data: dict, xml_data: dict) -> dict:
    """Parse un singleton Cognos vers card PBI."""
    name = singleton_data.get("name", "")
    query_name = singleton_data.get("refQuery", "")

    # Extraire les dataItems affichés
    query = xml_data.get("queries", {}).get(query_name, {})
    measures = []

    for item in query.get("dataItems", []):
        item_name = item.get("name", "")
        item_label = item.get("label", item_name)
        if item_name:
            measures.append({
                "expression": f"FIRST([{item_name}])",
                "label": item_label
            })

    return {
        "id": name,
        "type": "card",
        "title": name.replace("_", " ").title(),
        "layout": {"col": 0, "row": 0, "colspan": 4, "rowspan": 2},
        "dimensions": [],
        "measures": measures
    }


def _parse_table(table_data: dict) -> dict:
    """Parse une table Cognos vers tableEx PBI."""
    return {
        "id": f"table_{uuid.uuid4().hex[:8]}",
        "type": "tableEx",
        "title": "Table",
        "layout": {"col": 0, "row": 0, "colspan": 8, "rowspan": 4},
        "dimensions": [],
        "measures": []
    }


def parse_parameters_as_slicers(xml_data: dict) -> list:
    """Crée des slicers PBI à partir des paramètres Cognos.

    Returns:
        Liste d'objets slicer pour le rapport
    """
    slicers = []
    col_offset = 0

    for param in xml_data.get("parameters", []):
        param_name = param.get("name", "")
        options = param.get("options", [])

        if not param_name:
            continue

        # Déterminer le type basé sur le nom
        if "date" in param_name.lower():
            slicer_type = "date"
        else:
            slicer_type = "string"

        slicer = {
            "id": f"slicer_{param_name}",
            "type": "slicer",
            "title": param_name.replace("_", " ").title(),
            "layout": {
                "col": col_offset,
                "row": 0,
                "colspan": 3,
                "rowspan": 1
            },
            "slicer_field": f"Parameters[{param_name}]",
            "slicer_type": slicer_type,
            "options": [opt.get("value", "") for opt in options]
        }

        slicers.append(slicer)
        col_offset += 3

    return slicers


def add_slicers_to_sheets(visual_data: dict, xml_data: dict) -> dict:
    """Ajoute les slicers de paramètres à la première page du rapport.

    Args:
        visual_data: Dict produit par parse_cognos_layout()
        xml_data: Dict produit par xml_parser.parse_cognos_xml()

    Returns:
        visual_data avec slicers ajoutés
    """
    slicers = parse_parameters_as_slicers(xml_data)

    if slicers and visual_data.get("sheets"):
        # Ajouter à la première page
        first_sheet = visual_data["sheets"][0]
        row_offset = max(
            [obj.get("layout", {}).get("row", 0) + obj.get("layout", {}).get("rowspan", 1)
             for obj in first_sheet.get("objects", [])],
            default=0
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
