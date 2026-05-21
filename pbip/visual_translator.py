"""Translate Qlik visual objects to Power BI visual JSON + DAX measures."""
import argparse
import json
import os
import pathlib
import sys
import uuid
from dotenv import load_dotenv
load_dotenv()

ROOT       = pathlib.Path(__file__).parent.parent
_ALPHABET  = "abcdefghijklmnopqrstuvwxyz"
REPORT_NAME = "MigrationQlikPBI"


def _example_paths(example: str) -> dict:
    example_dir  = ROOT / "examples" / example
    intermediate = example_dir / "output" / "intermediate"
    pbip         = example_dir / "output" / "pbip"
    return {
        "visual_json":   intermediate / "visual_extraction.json",
        "script_json":   intermediate / "extraction.json",
        "bim":           pbip / f"{REPORT_NAME}.SemanticModel" / "model.bim",
        "report":        pbip / f"{REPORT_NAME}.Report" / "report.json",
        "prompt":        intermediate / "visual_prompt.txt",
        "response":      intermediate / "visual_response.json",
    }


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _tables_block(bim: dict) -> str:
    lines = ["Tables disponibles dans le modele semantique Power BI :"]
    for t in bim["model"]["tables"]:
        cols = ", ".join(c["name"] for c in t.get("columns", [])[:20])
        suffix = f" ... (+{len(t['columns'])-20} cols)" if len(t.get("columns", [])) > 20 else ""
        lines.append(f"- {t['name']} : {cols}{suffix}")
    return "\n".join(lines)


def _obj_lines(obj: dict, prefix: str = "  ") -> list[str]:
    if "error" in obj:
        return []
    dims    = [d.get("field") or d.get("label", "?") for d in obj.get("dimensions", [])]
    meas    = [m.get("expression") or m.get("label", "?") for m in obj.get("measures", [])]
    layout  = obj.get("layout", {})
    line = (
        f"{prefix}id={obj['id']} | type={obj['type']} | title=\"{obj.get('title','')}\" "
        f"| grid({layout.get('col',0)},{layout.get('row',0)},"
        f"{layout.get('colspan',1)}x{layout.get('rowspan',1)})"
        + (f" | dims=[{', '.join(dims)}]" if dims else "")
        + (f" | measures=[{', '.join(str(m)[:80] for m in meas)}]" if meas else "")
    )
    lines = [line]
    for child in obj.get("children", []):
        lines.extend(_obj_lines(child, prefix + "  "))
    return lines


def _visuals_block(sheets: list) -> str:
    lines = ["Objets visuels Qlik a traduire (par sheet) :"]
    for sheet in sheets:
        lines.append(f"\nSheet: \"{sheet['title']}\"")
        for obj in sheet.get("objects", []):
            lines.extend(_obj_lines(obj))
    return "\n".join(lines)


def _script_summary(script_data: dict | None) -> str:
    if not script_data:
        return ""
    raw = script_data.get("script", "")
    return f"\nExtrait du script Qlik (contexte metier) :\n{raw[:2000].strip()}\n[...tronque]"


_SYSTEM = (
    "Tu es un expert en migration Qlik -> Power BI.\n"
    "On te donne :\n"
    "1. La liste des tables/colonnes du modele semantique PBI\n"
    "2. Les objets visuels Qlik (id, type, titre, dimensions, mesures, position grille)\n"
    "   Les objets enfants d'un container sont indentes sous leur parent.\n"
    "3. Un extrait du script Qlik pour contexte metier\n\n"
    "Tu dois produire un JSON strict (sans markdown, sans explication) avec cette structure :\n"
    "{\n"
    '  "measures": [\n'
    '    {"name": "<nom lisible>", "table": "<table PBI>", "expression": "<DAX valide>"}\n'
    "  ],\n"
    '  "visuals": [\n'
    "    {\n"
    '      "qlik_id": "<id exact>",\n'
    '      "visual_type": "<barChart|columnChart|lineChart|donutChart|card|tableEx|pivotTable|slicer|textbox|scatterChart>",\n'
    '      "title": "<titre propose>",\n'
    '      "dimensions": [{"table": "<table PBI>", "column": "<colonne exacte>", "label": "<label>"}],\n'
    '      "measures": ["<nom mesure>"],\n'
    '      "slicer_field": "<table.colonne si slicer, sinon null>"\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Regles :\n"
    "- Chaque objet Qlik (y compris les enfants de containers) doit avoir une entree dans visuals\n"
    "- Les noms de mesures dans visuals[].measures doivent matcher measures[].name exactement\n"
    "- Pour filterpane/listbox : renseigne slicer_field, laisse dimensions et measures vides\n"
    "- Pour sn-text/text-image/action-button : visual_type=textbox, pas de dims/measures\n"
    "- Pour sn-layout-container : visual_type=textbox, pas de dims/measures\n"
    "- Retourne UNIQUEMENT le JSON"
)


def build_prompt(bim: dict, sheets: list, script_data: dict | None) -> tuple[str, str]:
    user = (
        f"{_tables_block(bim)}\n\n"
        f"{_visuals_block(sheets)}"
        f"{_script_summary(script_data)}\n\n"
        "Produis le JSON de traduction PBI."
    )
    return _SYSTEM, user


# ---------------------------------------------------------------------------
# Apply translation
# ---------------------------------------------------------------------------

def _alias(entity: str, tables_used: dict) -> str:
    if entity not in tables_used:
        tables_used[entity] = _ALPHABET[len(tables_used) % 26]
    return tables_used[entity]


_WELLS: dict[str, tuple[str | None, str | None, int, int]] = {
    "barChart":      ("Category", "Y",      -1, -1),
    "columnChart":   ("Category", "Y",      -1, -1),
    "lineChart":     ("Category", "Y",      -1, -1),
    "lineClusteredColumnComboChart": ("Category", "Y", -1, -1),
    "donutChart":    ("Category", "Y",        -1, 1),
    "scatterChart":  ("Details",  "Y",      -1, 2),
    "pivotTable":    ("Rows",     "Values", -1, -1),
    "treemap":       ("Category", "Values", -1, 1),
    "waterfallChart":("Category", "Y",      -1, 1),
    "gauge":         (None,       "Y",       0, 1),
    "map":           ("Location", "Size",   -1, 1),
    "card":          ("Values",   "Values",  0, 1),
    "tableEx":       ("Values",   "Values", -1, -1),
    "slicer":        ("Field",    None,       1, 0),
    "textbox":       (None,       None,      0, 0),
}


def _build_prototype_query(
    dims: list, measure_names: list, all_measures: list,
    slicer_field: str | None, visual_type: str = "card",
) -> tuple[dict, dict]:
    meas_by_name = {m["name"]: m for m in all_measures}
    tables_used: dict[str, str] = {}
    selects: list = []
    projections: dict[str, list] = {}

    dim_well, meas_well, max_dims, max_meas = _WELLS.get(visual_type, ("Category", "Y", -1, -1))

    def _add_col(tbl: str, col: str, well: str | None) -> None:
        a = _alias(tbl, tables_used)
        ref = f"{tbl}.{col}"
        if not any(s.get("Name") == ref for s in selects):
            selects.append({"Column": {"Expression": {"SourceRef": {"Source": a}}, "Property": col},
                            "Name": ref, "NativeReferenceName": col})
        if well:
            projections.setdefault(well, []).append({"queryRef": ref})

    def _add_meas(m: dict, well: str | None) -> None:
        tbl  = m["table"]
        safe = m.get("_safe_name", m["name"])
        a    = _alias(tbl, tables_used)
        ref  = f"{tbl}.{safe}"
        if not any(s.get("Name") == ref for s in selects):
            selects.append({"Measure": {"Expression": {"SourceRef": {"Source": a}}, "Property": safe},
                            "Name": ref, "NativeReferenceName": safe})
        if well:
            projections.setdefault(well, []).append({"queryRef": ref})

    if slicer_field and "." in slicer_field and dim_well:
        tbl, col = slicer_field.split(".", 1)
        _add_col(tbl, col, dim_well)

    if dim_well and max_dims != 0:
        for d in (dims[:max_dims] if max_dims > 0 else dims):
            tbl, col = d.get("table", ""), d.get("column", "")
            if tbl and col:
                _add_col(tbl, col, dim_well)

    if meas_well and max_meas != 0:
        names = measure_names[:max_meas] if max_meas > 0 else measure_names
        for mname in names:
            m = meas_by_name.get(mname)
            if m:
                _add_meas(m, meas_well)

    froms = [{"Name": a, "Entity": entity, "Type": 0} for entity, a in tables_used.items()]
    col_props: dict[str, dict] = {}
    for well, refs in projections.items():
        for item in refs:
            qr = item["queryRef"]
            col_props.setdefault(qr, {"roles": {}})["roles"][well] = {}
    return projections, {"Version": 2, "From": froms, "Select": selects}, col_props


def apply_translation(response: dict, paths: dict) -> None:
    all_measures = response.get("measures", [])
    visuals_map  = {v["qlik_id"]: v for v in response.get("visuals", [])}

    bim = json.loads(paths["bim"].read_text(encoding="utf-8"))
    rename: dict[str, str] = {}
    added = 0
    for m in all_measures:
        for t in bim["model"]["tables"]:
            if t["name"] == m.get("table", ""):
                col_names_lower = {c["name"].lower() for c in t.get("columns", [])}
                meas_name = m["name"]
                if meas_name.lower() in col_names_lower:
                    meas_name = meas_name + " M"
                    rename[m["name"]] = meas_name
                existing = [x["name"] for x in t.get("measures", [])]
                if meas_name not in existing:
                    t.setdefault("measures", []).append({
                        "name": meas_name,
                        "expression": m["expression"],
                        "lineageTag": str(uuid.uuid4()),
                    })
                    added += 1
                break
    for m in all_measures:
        if m["name"] in rename:
            m["_safe_name"] = rename[m["name"]]
        else:
            m["_safe_name"] = m["name"]
    paths["bim"].write_text(json.dumps(bim, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {added} measures added to model.bim ({len(rename)} renamed to avoid column conflicts)")

    report  = json.loads(paths["report"].read_text(encoding="utf-8"))
    patched = 0
    for section in report.get("sections", []):
        for vc in section.get("visualContainers", []):
            cfg  = json.loads(vc["config"])
            name = cfg.get("name", "")
            if name not in visuals_map:
                continue
            translated = visuals_map[name]
            vtype = translated.get("visual_type", "card")
            projections, proto, col_props = _build_prototype_query(
                translated.get("dimensions", []),
                translated.get("measures", []),
                all_measures,
                translated.get("slicer_field"),
                vtype,
            )
            sv = cfg.setdefault("singleVisual", {})
            sv["visualType"] = translated.get("visual_type", sv.get("visualType", "card"))
            if projections:
                sv["projections"] = projections
            if col_props:
                sv["columnProperties"] = col_props
            sv["prototypeQuery"] = proto
            title = translated.get("title", "")
            if title:
                sv.setdefault("vcObjects", {}).setdefault("title", [{"properties": {
                    "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
                    "show": {"expr": {"Literal": {"Value": "'True'"}}},
                }}])
            vc["config"] = json.dumps(cfg, ensure_ascii=False, separators=(',', ':'))
            patched += 1

    paths["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {patched} visualContainers patched in report.json")


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def _call_api(system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError:
        print("pip install anthropic")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    print("Appel Claude API...")
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=8192,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--example", default=os.getenv("EXAMPLE_NAME", ""))
    parser.add_argument("--mode", choices=["paste", "api", "apply"], default="paste")
    args = parser.parse_args()

    paths = _example_paths(args.example)

    for key in ("visual_json", "bim", "report"):
        if not paths[key].exists():
            print(f"Introuvable : {paths[key]}")
            sys.exit(1)

    visual_data = json.loads(paths["visual_json"].read_text(encoding="utf-8"))
    bim         = json.loads(paths["bim"].read_text(encoding="utf-8"))
    script_data = json.loads(paths["script_json"].read_text(encoding="utf-8")) if paths["script_json"].exists() else None
    sheets      = visual_data.get("sheets", [])

    if args.mode == "apply":
        if not paths["response"].exists():
            print(f"Introuvable : {paths['response']}")
            sys.exit(1)
        apply_translation(json.loads(paths["response"].read_text(encoding="utf-8")), paths)
        return

    system, user = build_prompt(bim, sheets, script_data)

    if args.mode == "paste":
        paths["prompt"].write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
        print(f"Prompt ecrit -> {paths['prompt']}")
        print("\nCopier dans LLM, coller reponse JSON dans :")
        print(f"  {paths['response']}")
        input("\nAppuyez sur Entree une fois le fichier cree...")
        if not paths["response"].exists():
            print("Fichier introuvable. Abandon.")
            sys.exit(1)

    elif args.mode == "api":
        raw     = _call_api(system, user)
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        paths["response"].write_text(cleaned, encoding="utf-8")
        print(f"Reponse LLM -> {paths['response']}")

    response = json.loads(paths["response"].read_text(encoding="utf-8"))
    print("\nApplication de la traduction...")
    apply_translation(response, paths)
    print(f"\nDone. Ouvrez {paths['bim'].parent.parent / REPORT_NAME}.pbip dans Power BI Desktop.")


if __name__ == "__main__":
    main()
