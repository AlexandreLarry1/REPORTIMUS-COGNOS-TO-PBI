"""Translate Qlik visual objects to Power BI visual JSON + DAX measures."""
import argparse
import json
import os
import pathlib
import re
import sys
import unicodedata
import uuid
from dotenv import load_dotenv
load_dotenv()

ROOT       = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))  # src/
import observability as obs
_ALPHABET  = "abcdefghijklmnopqrstuvwxyz"
REPORT_NAME = "MigrationQlikPBI"


def _example_paths(example: str) -> dict:
    example_dir  = ROOT / "examples" / example
    intermediate = example_dir / "intermediate"
    pbip         = example_dir / "pbip"
    return {
        "visual_json":   intermediate / "visual_extraction.json",
        "script_json":   intermediate / "extraction.json",
        "etl_context":   intermediate / "sql" / "etl_context.json",
        "bim":           pbip / f"{REPORT_NAME}.SemanticModel" / "model.bim",
        "report":        pbip / f"{REPORT_NAME}.Report" / "report.json",
        "prompt":        intermediate / "visual_prompt.txt",
        "response":      intermediate / "visual_response.json",
    }


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _tables_block(bim: dict) -> str:
    lines = [
        "Tables disponibles dans le modele semantique Power BI :",
        "IMPORTANT : utilise EXACTEMENT ces noms de tables et de colonnes dans toutes les expressions DAX et dimensions.",
        "Ne renomme pas, ne traduis pas, ne rajoute pas d'espaces ou d'accents.",
    ]
    for t in bim["model"]["tables"]:
        cols = ", ".join(c["name"] for c in t.get("columns", []))
        lines.append(f"- {t['name']} : {cols}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DAX auto-fix helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Normalize string for fuzzy matching: strip accents, lowercase, keep only alphanum."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _fix_dax_refs(expression: str, bim_idx: dict) -> tuple[str, list[str]]:
    """Auto-correct Table[Column] references in a DAX expression against bim_idx.

    Returns (fixed_expression, list_of_substitutions_made).
    """
    fixes: list[str] = []

    def _resolve_table(raw: str) -> tuple[str, dict[str, str]] | None:
        entry = bim_idx.get(raw.lower())
        if entry:
            return entry
        norm_raw = _norm(raw)
        # prefix match: Fact_Cours → fact_cours_historiques
        candidates = [(k, v) for k, v in bim_idx.items() if _norm(v[0]).startswith(norm_raw) or norm_raw.startswith(_norm(v[0]))]
        if len(candidates) == 1:
            return candidates[0][1]
        # best overlap
        best = max(bim_idx.items(), key=lambda kv: len(set(_norm(kv[0])) & set(norm_raw)), default=None)
        return best[1] if best else None

    def _resolve_col(raw: str, cols: dict[str, str]) -> str | None:
        exact = cols.get(raw.lower())
        if exact:
            return exact
        norm_raw = _norm(raw)
        for col_lower, col_actual in cols.items():
            if _norm(col_actual) == norm_raw:
                return col_actual
        return None

    def _replace(match: re.Match) -> str:
        raw_tbl = match.group(1).strip()
        raw_col = match.group(2).strip()
        entry = _resolve_table(raw_tbl)
        if not entry:
            return match.group(0)
        actual_tbl, cols = entry
        actual_col = _resolve_col(raw_col, cols)
        if not actual_col:
            return match.group(0)
        result = f"{actual_tbl}[{actual_col}]"
        if result != match.group(0):
            fixes.append(f"{match.group(0)} → {result}")
        return result

    fixed = re.sub(r"([\w][\w\s]*?)\[([^\]]+)\]", _replace, expression)
    return fixed, fixes


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


def _etl_context_block(etl_context: dict | None) -> str:
    if not etl_context:
        return ""
    lines = ["\nTables ETL disponibles (post-transformation, prets pour DAX) :"]
    for t in etl_context.get("tables", []):
        cols = ", ".join(f"{c['name']} ({c['type']})" for c in t["columns"])
        sample_vals = []
        for row in t.get("sample", [])[:2]:
            sample_vals.append("{" + ", ".join(f"{k}: {v}" for k, v in list(row.items())[:4]) + "}")
        sample_str = " | ".join(sample_vals)
        lines.append(f"- {t['name']} ({t['row_count']:,} lignes) : {cols}")
        if sample_str:
            lines.append(f"  ex: {sample_str}")
    errors = etl_context.get("errors", [])
    if errors:
        lines.append(f"\nAttention : {len(errors)} erreur(s) ETL — ces tables peuvent etre absentes ou incompletes :")
        for e in errors:
            lines.append(f"  - {e.get('error', '')[:120]}")
    return "\n".join(lines)


_SYSTEM = (
    "Tu es un expert en migration Qlik -> Power BI.\n"
    "On te donne :\n"
    "1. La liste des tables/colonnes du modele semantique PBI (noms EXACTS a utiliser)\n"
    "2. Les donnees ETL avec types et exemples\n"
    "3. Les objets visuels Qlik (id, type, titre, dimensions, mesures, position grille)\n"
    "   Les objets enfants d'un container sont indentes sous leur parent.\n"
    "4. Un extrait du script Qlik pour contexte metier\n\n"
    "Tu dois produire un JSON strict (sans markdown, sans explication) avec cette structure :\n"
    "{\n"
    '  "calculated_columns": [\n'
    '    {"table": "<table PBI>", "name": "<nom colonne>", "expression": "<DAX colonne calculee>", "data_type": "string|double|int64|boolean"}\n'
    "  ],\n"
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
    "Regles CRITIQUES :\n"
    "- Utilise UNIQUEMENT les noms de tables et colonnes tels que listes dans le bloc 'Tables disponibles'\n"
    "- Dans les expressions DAX, reference les colonnes avec Table[ColonneExacte] (casse identique)\n"
    "- Si une dimension Qlik est une expression calculee (ex: If(PnL>0,'Pos','Neg')), ajoute-la dans calculated_columns avec l'expression DAX equivalente. Ne la mets pas dans dimensions si elle n'existe pas comme colonne physique.\n"
    "- Chaque objet Qlik (y compris les enfants de containers) doit avoir une entree dans visuals\n"
    "- Les noms de mesures dans visuals[].measures doivent matcher measures[].name exactement\n"
    "- Pour filterpane/listbox : renseigne slicer_field, laisse dimensions et measures vides\n"
    "- Pour sn-text/text-image/action-button : visual_type=textbox, pas de dims/measures\n"
    "- Retourne UNIQUEMENT le JSON"
)


def build_prompt(bim: dict, sheets: list, script_data: dict | None, etl_context: dict | None = None) -> tuple[str, str]:
    user = (
        f"{_tables_block(bim)}\n\n"
        f"{_etl_context_block(etl_context)}\n\n"
        f"{_visuals_block(sheets)}"
        f"{_script_summary(script_data)}\n\n"
        "Produis le JSON de traduction PBI."
    )
    return _SYSTEM, user


# ---------------------------------------------------------------------------
# Apply translation
# ---------------------------------------------------------------------------

def _stable_alias(entity: str) -> str:
    """Derive a stable alias from the table name: initials of underscore-separated segments.

    dim_clients → dc, fact_positions → fp, MasterCalendar → mc.
    """
    parts = re.split(r"[_\s]+", entity.lower())
    return "".join(p[0] for p in parts if p)


def _alias(entity: str, tables_used: dict) -> str:
    if entity not in tables_used:
        base = _stable_alias(entity)
        alias = base
        n = 2
        while alias in tables_used.values():
            alias = f"{base}{n}"
            n += 1
        tables_used[entity] = alias
    return tables_used[entity]


_WELLS: dict[str, tuple[str | None, str | None, int, int]] = {
    "barChart":      ("Category", "Y",      -1, -1),
    "columnChart":   ("Category", "Y",      -1, -1),
    "lineChart":     ("Category", "Y",      -1, -1),
    "lineClusteredColumnComboChart": ("Category", "Y", -1, -1),
    "donutChart":    ("Category", "Y",        -1, 1),
    "scatterChart":  ("Details",  None,      -1, 0),  # scatter handled separately
    "pivotTable":    ("Rows",     "Values", -1, -1),
    "treemap":       ("Category", "Values", -1, 1),
    "waterfallChart":("Category", "Y",      -1, 1),
    "gauge":         (None,       "Y",       0, 1),
    "map":           ("Location", "Size",   -1, 1),
    "card":          (None,       "Values",  0, 1),
    "tableEx":       ("Values",   "Values", -1, -1),
    "slicer":        ("Field",    None,       1, 0),
    "textbox":       (None,       None,      0, 0),
}

# scatter: measure[0]→X, measure[1]→Y, measure[2]→Size
_SCATTER_MEAS_WELLS = ["X", "Y", "Size"]


def _bim_index(bim: dict) -> dict[str, tuple[str, dict[str, str]]]:
    """Build {table_lower: (actual_table_name, {col_lower: actual_col})}."""
    idx: dict[str, tuple[str, dict[str, str]]] = {}
    for t in bim["model"]["tables"]:
        cols = {c["name"].lower(): c["name"] for c in t.get("columns", [])}
        idx[t["name"].lower()] = (t["name"], cols)
    return idx


def _resolve_dim(d: dict, idx: dict) -> dict:
    """Resolve table/column to actual bim casing (exact then fuzzy via _norm)."""
    raw_tbl = d.get("table", "")
    raw_col = d.get("column", "")

    # table: exact then fuzzy
    entry = idx.get(raw_tbl.lower())
    if not entry:
        norm_tbl = _norm(raw_tbl)
        candidates = [v for k, v in idx.items() if _norm(v[0]).startswith(norm_tbl) or norm_tbl.startswith(_norm(v[0]))]
        entry = candidates[0] if len(candidates) == 1 else None
    if not entry:
        return d
    actual_tbl, cols = entry

    # column: exact then fuzzy
    actual_col = cols.get(raw_col.lower())
    if not actual_col:
        norm_col = _norm(raw_col)
        for col_actual in cols.values():
            if _norm(col_actual) == norm_col:
                actual_col = col_actual
                break
    return {**d, "table": actual_tbl, "column": actual_col or raw_col}


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
            entry: dict = {"queryRef": ref}
            if not projections.get(well):  # first item in this well → active
                entry["active"] = True
            projections.setdefault(well, []).append(entry)

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

    if visual_type == "scatterChart":
        for i, mname in enumerate(measure_names[:len(_SCATTER_MEAS_WELLS)]):
            m = meas_by_name.get(mname)
            if m:
                _add_meas(m, _SCATTER_MEAS_WELLS[i])
    elif meas_well and max_meas != 0:
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


def _add_calc_col(bim: dict, table: str, name: str, expression: str, data_type: str = "string") -> bool:
    for t in bim["model"]["tables"]:
        if t["name"] == table:
            existing = [c["name"] for c in t.get("columns", [])]
            if name not in existing:
                t.setdefault("columns", []).append({
                    "type": "calculated",
                    "name": name,
                    "lineageTag": str(uuid.uuid4()),
                    "dataType": data_type,
                    "expression": expression,
                    "summarizeBy": "none",
                })
                return True
    return False


def apply_translation(response: dict, paths: dict) -> None:
    all_measures = response.get("measures", [])
    visuals_map  = {v["qlik_id"]: v for v in response.get("visuals", [])}

    bim = json.loads(paths["bim"].read_text(encoding="utf-8"))
    bim_idx = _bim_index(bim)
    # build {table_lower: actual_table_name}
    tbl_name_map = {k: v[0] for k, v in bim_idx.items()}

    rename: dict[str, str] = {}
    added = 0
    skipped = 0
    for m in all_measures:
        req_tbl    = m.get("table", "")
        actual_tbl = tbl_name_map.get(req_tbl.lower())
        if not actual_tbl:
            norm_req = _norm(req_tbl)
            candidates = [v for k, v in tbl_name_map.items() if _norm(v).startswith(norm_req) or norm_req.startswith(_norm(v))]
            actual_tbl = candidates[0] if len(candidates) == 1 else None
        if not actual_tbl:
            print(f"  [WARN] table '{req_tbl}' introuvable dans model.bim — mesure '{m['name']}' ignorée")
            skipped += 1
            continue
        m["table"] = actual_tbl  # normalize casing for _build_prototype_query
        # auto-fix DAX column references
        fixed_expr, dax_fixes = _fix_dax_refs(m["expression"], bim_idx)
        if dax_fixes:
            print(f"  [DAX-FIX] '{m['name']}': {'; '.join(dax_fixes)}")
        m["expression"] = fixed_expr
        for t in bim["model"]["tables"]:
            if t["name"] == actual_tbl:
                col_names_lower = {c["name"].lower() for c in t.get("columns", [])}
                meas_name = m["name"]
                if meas_name.lower() in col_names_lower:
                    meas_name = meas_name + " M"
                    rename[m["name"]] = meas_name
                existing = [x["name"] for x in t.get("measures", [])]
                if meas_name not in existing:
                    t.setdefault("measures", []).append({
                        "name": meas_name,
                        "expression": fixed_expr,
                        "lineageTag": str(uuid.uuid4()),
                    })
                    added += 1
                break
    for m in all_measures:
        m["_safe_name"] = rename.get(m["name"], m["name"])

    # --- calculated_columns from LLM response ---
    calc_added = 0
    for cc in response.get("calculated_columns", []):
        req_tbl = cc.get("table", "")
        actual_tbl = tbl_name_map.get(req_tbl.lower())
        if not actual_tbl:
            print(f"  [WARN] calc_col table '{req_tbl}' introuvable")
            continue
        expr, dax_fixes = _fix_dax_refs(cc.get("expression", "\"\""), bim_idx)
        if dax_fixes:
            print(f"  [DAX-FIX] calc_col '{cc['name']}': {'; '.join(dax_fixes)}")
        if _add_calc_col(bim, actual_tbl, cc["name"], expr, cc.get("data_type", "string")):
            print(f"  [CALC-COL] {actual_tbl}[{cc['name']}] ajoutée")
            calc_added += 1
            bim_idx = _bim_index(bim)  # refresh index

    # --- detect missing dimension columns → add placeholder calc cols ---
    placeholder_added = 0
    for visual in visuals_map.values():
        for d in visual.get("dimensions", []):
            resolved = _resolve_dim(d, bim_idx)  # fuzzy resolve first
            entry = bim_idx.get(resolved.get("table", "").lower())
            if not entry:
                continue
            actual_tbl, cols = entry
            col = resolved.get("column", "")
            if col and col.lower() not in cols:
                placeholder = f"\"TODO: DAX pour {col} (colonne calculee Qlik)\""
                if _add_calc_col(bim, actual_tbl, col, placeholder, "string"):
                    print(f"  [PLACEHOLDER] {actual_tbl}[{col}] — expression à compléter dans Power BI Desktop")
                    placeholder_added += 1
                    bim_idx = _bim_index(bim)

    paths["bim"].write_text(json.dumps(bim, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {added} mesures | {calc_added} col. calculées | {placeholder_added} placeholders | {len(rename)} renommées | {skipped} ignorées")

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
            resolved_dims = [_resolve_dim(d, bim_idx) for d in translated.get("dimensions", [])]
            sf = translated.get("slicer_field")
            if sf and "." in sf:
                tbl_s, col_s = sf.split(".", 1)
                entry = bim_idx.get(tbl_s.lower())
                if entry:
                    actual_tbl_s, cols_s = entry
                    sf = f"{actual_tbl_s}.{cols_s.get(col_s.lower(), col_s)}"
            projections, proto, col_props = _build_prototype_query(
                resolved_dims,
                translated.get("measures", []),
                all_measures,
                sf,
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

def _call_api(system: str, user: str, trace=None) -> str:
    from openai import AzureOpenAI
    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )
    model = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    gen = (trace or obs._Noop()).generation(
        name="visual_translation",
        model=model,
        input=messages,
    )

    print("Appel Azure OpenAI...")
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=8192,
    )
    result = response.choices[0].message.content
    gen.end(output=result)
    return result


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
    etl_context = json.loads(paths["etl_context"].read_text(encoding="utf-8")) if paths["etl_context"].exists() else None
    sheets      = visual_data.get("sheets", [])

    if etl_context:
        print(f"  etl_context chargé ({len(etl_context.get('tables', []))} tables)")
    else:
        print("  etl_context absent — prompt sans données ETL")

    if args.mode == "apply":
        if not paths["response"].exists():
            print(f"Introuvable : {paths['response']}")
            sys.exit(1)
        apply_translation(json.loads(paths["response"].read_text(encoding="utf-8")), paths)
        return

    system, user = build_prompt(bim, sheets, script_data, etl_context)

    with obs.trace("visual_translation_run", example=args.example, mode=args.mode) as trace:
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
            raw     = _call_api(system, user, trace=trace)
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            paths["response"].write_text(cleaned, encoding="utf-8")
            print(f"Reponse LLM -> {paths['response']}")

        response = json.loads(paths["response"].read_text(encoding="utf-8"))
        print("\nApplication de la traduction...")
        apply_sp = trace.span(name="apply_translation")
        apply_translation(response, paths)
        apply_sp.end()

    print(f"\nDone. Ouvrez {paths['bim'].parent.parent / REPORT_NAME}.pbip dans Power BI Desktop.")


if __name__ == "__main__":
    main()
