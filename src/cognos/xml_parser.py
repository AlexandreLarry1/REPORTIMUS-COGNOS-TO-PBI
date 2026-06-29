"""Parseur XML Cognos v17.5 vers JSON intermédiaire.

Extraction déterministe (0 LLM) des éléments du XML Cognos:
- Paramètres (prompts utilisateurs)
- Variables (booléens pour conditionalRender)
- Queries (expressions CASE WHEN avec ?param?)
- Crosstabs (structure rows/columns/intersections)
- Styles CSS

Namespace Cognos: http://developer.cognos.com/schemas/report/17.5/
"""
import json
import pathlib
import re
from typing import Any
from lxml import etree

# Namespace Cognos
NS = {"c": "http://developer.cognos.com/schemas/report/17.5/"}


def parse_parameters(xml_root: etree._Element) -> list[dict]:
    """Extrait les paramètres Cognos (prompts utilisateurs).

    Returns:
        Liste de {name, defaultValue, options}
    """
    parameters = []
    for param in xml_root.findall(".//c:parameters/c:parameter", NS):
        name = param.get("name", "")
        default_val = ""
        options = []

        for default in param.findall("c:defaultValues/c:defaultValue/c:dataSource", NS):
            static = default.find("c:staticValue", NS)
            if static is not None:
                default_val = static.text or ""
                break

        for opt in param.findall(".//c:selectOptions/c:selectOption", NS):
            use_val = opt.get("useValue", "")
            disp = opt.find("c:displayValue", NS)
            display_val = disp.text if disp is not None else use_val
            options.append({"value": use_val, "label": display_val})

        parameters.append({
            "name": name,
            "defaultValue": default_val,
            "options": options
        })
    return parameters


def parse_variables(xml_root: etree._Element) -> list[dict]:
    """Extrait les variables Cognos (reportVariables pour conditionalRender).

    Returns:
        Liste de {name, type, expression, values}
    """
    variables = []
    for var in xml_root.findall(".//c:reportVariables/c:reportVariable", NS):
        name = var.get("name", "")
        var_type = var.get("type", "boolean")

        expr_node = var.find("c:reportExpression", NS)
        expression = expr_node.text if expr_node is not None else ""

        values = []
        for val in var.findall("c:variableValues/c:variableValue", NS):
            values.append(val.get("value", ""))

        variables.append({
            "name": name,
            "type": var_type,
            "expression": expression,
            "values": values
        })
    return variables


def parse_queries(xml_root: etree._Element) -> dict:
    """Extrait les requêtes et expressions de données.

    Returns:
        Dict {query_name: {dataItems, expressions, filters}}
    """
    queries = {}
    for query in xml_root.findall(".//c:queries/c:query", NS):
        name = query.get("name", "")
        data_items = []
        expressions = []

        # DataItems (colonnes de données)
        for item in query.findall("./c:selection/c:dataItem", NS):
            item_name = item.get("name", "")
            item_label = item.get("label", item_name)
            agg = item.get("aggregate", "none")

            expr_node = item.find("c:expression", NS)
            expression = expr_node.text if expr_node is not None else ""

            data_items.append({
                "name": item_name,
                "label": item_label,
                "aggregate": agg,
                "expression": expression
            })
            if expression and "CASE" in expression.upper():
                expressions.append({
                    "name": item_name,
                    "type": "calculated_column",
                    "expression": expression
                })

        # Measures (calculs)
        for item in query.findall(".//c:dataItem", NS):
            expr_node = item.find("c:expression", NS)
            if expr_node is not None and expr_node.text:
                expr_text = expr_node.text
                if "CASE" in expr_text.upper() or "?" in expr_text:
                    expressions.append({
                        "name": item.get("name", ""),
                        "type": "measure",
                        "expression": expr_text
                    })

        queries[name] = {
            "dataItems": data_items,
            "expressions": expressions
        }
    return queries


def parse_crosstab_node(crosstab: etree._Element, path: str = "") -> dict:
    """Parse un crosstab Cognos récursivement.

    Returns:
        Dict structure du crosstab (rows, columns, intersections)
    """
    name = crosstab.get("name", "")
    ref_query = crosstab.get("refQuery", "")

    result = {
        "name": name,
        "refQuery": ref_query,
        "rows": [],
        "columns": [],
        "intersections": [],
        "styles": {},
        "conditionalRender": None
    }

    # ConditionalRender
    cond_render = crosstab.find("c:conditionalRender", NS)
    if cond_render is not None:
        result["conditionalRender"] = {
            "refVariable": cond_render.get("refVariable", ""),
            "renderFor": cond_render.get("renderFor", "")
        }

    # Rows
    for row_node in crosstab.findall(".//c:crosstabRows/c:crosstabNode", NS):
        row_data = _parse_crosstab_dimension(row_node, "row")
        if row_data:
            result["rows"].append(row_data)

    # Columns
    for col_node in crosstab.findall(".//c:crosstabColumns/c:crosstabNode", NS):
        col_data = _parse_crosstab_dimension(col_node, "column")
        if col_data:
            result["columns"].append(col_data)

    # Intersections (cell values)
    for inter in crosstab.findall(".//c:crosstabIntersections/c:crosstabIntersection", NS):
        col_ref = inter.get("column", "")
        row_ref = inter.get("row", "")
        style = inter.find("c:style", NS)

        inter_data = {
            "column": col_ref,
            "row": row_ref,
            "style": _parse_style_node(style) if style is not None else {}
        }

        # Conditional styles
        cond_refs = inter.findall("c:conditionalStyleRefs/c:conditionalStyleRef", NS)
        if cond_refs:
            inter_data["conditionalStyles"] = [cr.get("refConditionalStyle", "") for cr in cond_refs]

        result["intersections"].append(inter_data)

    # Corner style
    corner = crosstab.find("c:crosstabCorner", NS)
    if corner is not None:
        style = corner.find("c:style", NS)
        if style is not None:
            result["styles"]["corner"] = _parse_style_node(style)

    return result


def _parse_crosstab_dimension(node: etree._Element, dim_type: str) -> dict:
    """Parse une dimension de crosstab (row ou column)."""
    members = node.findall(".//c:crosstabNodeMembers/c:crosstabNodeMember", NS)
    if not members:
        return None

    result = {"type": dim_type, "members": []}

    for member in members:
        edge = member.get("edgeLocation", "")
        ref_item = member.get("refDataItem", "")

        # Style
        style_node = member.find("c:style", NS)
        style = _parse_style_node(style_node) if style_node is not None else {}

        # Conditional styles
        cond_refs = member.findall("c:conditionalStyleRefs/c:conditionalStyleRef", NS)
        cond_styles = [cr.get("refConditionalStyle", "") for cr in cond_refs]

        # Contents (textItem for headers)
        contents = []
        for content in member.findall("c:contents/c:textItem", NS):
            ds = content.find("c:dataSource", NS)
            if ds is not None:
                label_ref = ds.find("c:dataItemLabel", NS)
                static = ds.find("c:staticValue", NS)
                if label_ref is not None:
                    contents.append({"type": "label", "refDataItem": label_ref.get("refDataItem", "")})
                elif static is not None:
                    contents.append({"type": "static", "value": static.text or ""})

        member_data = {
            "edge": edge,
            "refDataItem": ref_item,
            "style": style,
            "conditionalStyles": cond_styles,
            "contents": contents
        }
        result["members"].append(member_data)

    return result


def parse_named_styles(xml_root: etree._Element) -> dict:
    """Extrait les styles nommés (conditionalStyles).

    Returns:
        Dict {style_name: {type, cases, default}}
    """
    styles = {}

    # Advanced conditional styles
    for style in xml_root.findall(".//c:namedConditionalStyles/c:advancedConditionalStyle", NS):
        name = style.get("name", "")
        cases = []
        default = {}

        for case in style.findall("c:styleCases/c:styleCase", NS):
            condition = case.find("c:reportCondition", NS)
            condition_text = condition.text if condition is not None else ""

            style_node = case.find("c:style", NS)
            style_dict = _parse_style_node(style_node) if style_node is not None else {}

            cases.append({
                "condition": condition_text,
                "style": style_dict
            })

        default_node = style.find("c:styleDefault", NS)
        if default_node is not None:
            default = _parse_style_node(default_node)

        styles[name] = {
            "type": "advanced",
            "cases": cases,
            "default": default
        }

    # Range conditional styles
    for style in xml_root.findall(".//c:namedConditionalStyles/c:rangeConditionalStyle", NS):
        name = style.get("name", "")
        style_type = style.get("type", "number")

        cond_data = style.find("c:conditionalDataItem", NS)
        ref_item = cond_data.get("refDataItem", "") if cond_data is not None else ""
        ref_query = cond_data.get("refQuery", "") if cond_data is not None else ""

        ranges = []
        for rng in style.findall("c:styleRanges/c:styleRange", NS):
            value = rng.get("value", "")
            inclusive = rng.get("inclusive", "true") == "true"
            style_node = rng.find("c:style", NS)
            style_dict = _parse_style_node(style_node) if style_node is not None else {}
            ranges.append({
                "value": value,
                "inclusive": inclusive,
                "style": style_dict
            })

        remaining = style.find("c:styleRangeRemaining", NS)
        remaining_style = _parse_style_node(remaining) if remaining is not None else {}

        styles[name] = {
            "type": "range",
            "dataType": style_type,
            "refDataItem": ref_item,
            "refQuery": ref_query,
            "ranges": ranges,
            "remaining": remaining_style
        }

    return styles


def _parse_style_node(style_node: etree._Element) -> dict:
    """Parse un noeud de style Cognos."""
    css_attr = style_node.find("c:CSS", NS)
    css_value = css_attr.get("value", "") if css_attr is not None else ""

    style_dict = {}
    if css_value:
        style_dict = _parse_css_string(css_value)

    return style_dict


def _parse_css_string(css: str) -> dict:
    """Parse une chaîne CSS simple vers dict.

    Ex: "border:1pt solid black;color:red" → {"border": {...}, "color": "red"}
    """
    result = {}
    if not css:
        return result

    # Split par ; mais attention aux parenthèses dans les expressions
    parts = re.split(r';(?![^()]*\))', css)

    for part in parts:
        part = part.strip()
        if not part or ':' not in part:
            continue

        prop, val = part.split(':', 1)
        prop = prop.strip()
        val = val.strip()

        # Mapping des propriétés communes
        prop_map = {
            "border": "border",
            "border-top": "borderTop",
            "border-bottom": "borderBottom",
            "border-left": "borderLeft",
            "border-right": "borderRight",
            "background-color": "backgroundColor",
            "color": "color",
            "font-size": "fontSize",
            "font-weight": "fontWeight",
            "text-align": "textAlign",
            "vertical-align": "verticalAlign",
            "padding": "padding",
            "padding-left": "paddingLeft",
            "padding-right": "paddingRight",
            "padding-top": "paddingTop",
            "padding-bottom": "paddingBottom",
            "width": "width",
            "height": "height",
            "display": "display"
        }

        mapped_prop = prop_map.get(prop, prop)
        result[mapped_prop] = val

    return result


def parse_layouts(xml_root: etree._Element) -> dict:
    """Extrait la structure de layout du rapport.

    Returns:
        Dict {pages: [{name, objects: [...]}]}
    """
    layouts = {"pages": []}

    for page in xml_root.findall(".//c:reportPages/c:page", NS):
        page_name = page.get("name", "Page1")
        page_data = {"name": page_name, "objects": []}

        # Conteneurs principaux
        for obj in page.findall(".//c:pageBody/c:contents/c:table", NS):
            page_data["objects"].append(_parse_table_object(obj))

        for obj in page.findall(".//c:pageBody/c:contents/c:crosstab", NS):
            crosstab_data = parse_crosstab_node(obj)
            crosstab_data["layoutType"] = "crosstab"
            page_data["objects"].append(crosstab_data)

        for obj in page.findall(".//c:contents/c:singleton", NS):
            singleton_data = {
                "type": "singleton",
                "name": obj.get("name", ""),
                "refQuery": obj.get("refQuery", "")
            }
            page_data["objects"].append(singleton_data)

        layouts["pages"].append(page_data)

    return layouts


def _parse_table_object(table: etree._Element) -> dict:
    """Parse un objet table Cognos."""
    return {
        "type": "table",
        "rows": len(table.findall(".//c:tableRows/c:tableRow", NS)) or 0
    }


def parse_data_stores(xml_root: etree._Element) -> dict:
    """Extrait les reportDataStore → {dataStoreN: {refQuery, fields}}.

    Chaîne de résolution utilisée par vizControl et listControl :
      vizControl.refDataStore → reportDataStore.name → dsV5ListQuery.refQuery + dsV5DataItem.refDataItem
    """
    stores: dict[str, dict] = {}
    for rds in xml_root.findall(".//c:reportDataStore", NS):
        name = rds.get("name", "")
        if not name:
            continue
        ref_query = ""
        q = rds.find(".//c:dsV5ListQuery", NS)
        if q is not None:
            ref_query = q.get("refQuery", "")
        fields = [
            item.get("refDataItem", "")
            for item in rds.findall(".//c:dsV5DataItem", NS)
            if item.get("refDataItem", "")
        ]
        stores[name] = {"refQuery": ref_query, "fields": fields}
    return stores


def parse_viz_controls(xml_root: etree._Element) -> list[dict]:
    """Extrait les vizControl IBM avec page d'appartenance et slots de données.

    Structure extraite par vizControl :
      name, ibm_type (ex: "floatingBar"), page, ref_data_store,
      slots: {idSlot: [refDsColumn, ...]}, height, width
    """
    result: list[dict] = []
    seen: set[str] = set()

    for page in xml_root.findall(".//c:reportPages/c:page", NS):
        page_name = page.get("name", "Page1")
        for vc in page.findall(".//c:vizControl", NS):
            name = vc.get("name", "")
            if name in seen:
                continue
            seen.add(name)

            ibm_type = vc.get("type", "").split(".")[-1]

            ds = vc.find(".//c:vcDataSet", NS)
            ref_data_store = ds.get("refDataStore", "") if ds is not None else ""

            slots: dict[str, list[str]] = {}
            for slot in vc.findall(".//c:vcSlotData", NS):
                slot_id = slot.get("idSlot", "")
                fields = [
                    col.get("refDsColumn", "")
                    for col in slot.findall("c:vcSlotDsColumns/c:vcSlotDsColumn", NS)
                    if col.get("refDsColumn", "")
                ]
                if fields:
                    slots[slot_id] = fields

            height = width = ""
            for prop in vc.findall(".//c:vizPropertyLengthValue", NS):
                pname = prop.get("name", "")
                if pname == "vcHeight":
                    height = prop.text or ""
                elif pname == "vcWidth":
                    width = prop.text or ""

            result.append({
                "name": name,
                "ibm_type": ibm_type,
                "page": page_name,
                "ref_data_store": ref_data_store,
                "slots": slots,
                "height": height,
                "width": width,
            })

    return result


def parse_select_values(xml_root: etree._Element) -> list[dict]:
    """Extrait les selectValue (slicers dropdown) avec leur page et query source.

    Contrairement aux <parameter> (qui ont des options statiques), les selectValue
    pointent vers une query Cognos pour leurs valeurs dynamiques.
    """
    result: list[dict] = []
    seen: set[str] = set()

    for page in xml_root.findall(".//c:reportPages/c:page", NS):
        page_name = page.get("name", "Page1")
        for sv in page.findall(".//c:selectValue", NS):
            name = sv.get("name", "")
            ref_query = sv.get("refQuery", "")
            param_ref = sv.find(".//c:parameterReference", NS)
            param_name = param_ref.get("name", "") if param_ref is not None else ""

            key = name or ref_query
            if key in seen:
                continue
            seen.add(key)

            result.append({
                "name": name or f"slicer_{ref_query}",
                "page": page_name,
                "ref_query": ref_query,
                "param_name": param_name,
            })

    return result


def parse_list_controls(xml_root: etree._Element) -> list[dict]:
    """Extrait les listControl (tableaux tabulaires Cognos).

    Les colonnes sont résolues via le reportDataStore associé
    (lcColumn.refDataItem est souvent absent — on utilise dsV5DataItem à la place).
    """
    result: list[dict] = []

    for page in xml_root.findall(".//c:reportPages/c:page", NS):
        page_name = page.get("name", "Page1")
        for lc in page.findall(".//c:listControl", NS):
            name = lc.get("name", "")
            ref_data_store = lc.get("refDataStore", "")

            # Try explicit column refs first; fallback to data store
            columns = [
                col.get("refDataItem", "")
                for col in lc.findall(".//c:lcColumn", NS)
                if col.get("refDataItem", "")
            ]

            result.append({
                "name": name,
                "page": page_name,
                "ref_data_store": ref_data_store,
                "columns": columns,
            })

    return result


def parse_cognos_xml(xml_path: pathlib.Path) -> dict:
    """Point d'entrée principal: parse un fichier XML Cognos complet.

    Args:
        xml_path: Chemin vers le fichier XML Cognos

    Returns:
        Dict complet avec toutes les extractions
    """
    tree = etree.parse(str(xml_path))
    root = tree.getroot()

    return {
        "parameters": parse_parameters(root),
        "variables": parse_variables(root),
        "queries": parse_queries(root),
        "crosstabs": [parse_crosstab_node(cb) for cb in root.findall(".//c:crosstab", NS)],
        "namedStyles": parse_named_styles(root),
        "layouts": parse_layouts(root),
        "data_stores": parse_data_stores(root),
        "viz_controls": parse_viz_controls(root),
        "list_controls": parse_list_controls(root),
        "select_values": parse_select_values(root),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Parseur XML Cognos vers JSON")
    parser.add_argument("xml_file", help="Chemin vers le fichier XML Cognos")
    parser.add_argument("-o", "--output", help="Fichier JSON de sortie")
    args = parser.parse_args()

    xml_path = pathlib.Path(args.xml_file)
    if not xml_path.exists():
        print(f"Erreur: fichier introuvable {xml_path}")
        sys.exit(1)

    result = parse_cognos_xml(xml_path)

    output_path = pathlib.Path(args.output) if args.output else xml_path.with_suffix(".json")
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Extraction Cognos → {output_path}")
    print(f"  {len(result['parameters'])} paramètres")
    print(f"  {len(result['variables'])} variables")
    print(f"  {len(result['queries'])} requêtes")
    print(f"  {len(result['crosstabs'])} crosstabs")
    print(f"  {len(result['namedStyles'])} styles nommés")


if __name__ == "__main__":
    main()
