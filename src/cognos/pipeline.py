"""Orchestrateur principal de la pipeline Cognos vers Power BI (1-prompt architecture).

Coordonne les phases de la pipeline:
- Phase 1: Parseur Déterministe (0 LLM) — XML + classification
- Phase 2: Traduction unifiée (1 appel LLM) — déterministe + LLM ciblé
- Phase 3: Générateur Déterministe (0 LLM) — assemblage .pbip

Per BRIEF.md: remplace l'ancienne architecture à 3 appels LLM par un seul
appel unifié. Les traductions déterministes sont effectuées localement et
seules les expressions non résolues (types D & H) sont envoyées au LLM.

Avec observabilité Langfuse complète.
"""
import argparse
import json
import os
import pathlib
import shutil
import sys
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import observability as obs

# Imports des modules Cognos
from cognos import xml_parser, layout_parser, expression_parser
from cognos import deterministic_translator, data_dictionary
from cognos.llm import unified_translation, viz_translation, layout_translation
from cognos import pbip_generator, validator, theme_builder

# Imports des modules existants pour réutilisation
from pbip import pbip_builder


def run_phase1_deterministic(
    xml_path: pathlib.Path,
    output_dir: pathlib.Path,
    example_name: str = "",
    trace=None,
) -> dict:
    """Phase 1: Parseur Déterministe (0 appel LLM).

    Extrait et convertit le XML Cognos vers JSON intermédiaire + classifie
    les expressions par structure (report-agnostic).

    Args:
        xml_path: Chemin vers le XML Cognos
        output_dir: Répertoire de sortie
        example_name: Nom de l'exemple (pour localiser le dossier input/CSV)
        trace: Observability trace

    Returns:
        Dict avec xml_data + visual_data + classified + prompt_context + csv_schema
    """
    parser_sp = (trace or obs._Noop()).span(name="Deterministic_Parser")

    print("=== Phase 1: Parseur Déterministe ===")

    # 1. Parser le XML Cognos
    print("1. Parsing XML Cognos...")
    xml_data = xml_parser.parse_cognos_xml(xml_path)
    xml_json_path = output_dir / "cognos_extraction.json"
    xml_json_path.write_text(json.dumps(xml_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   -> {xml_json_path}")
    print(f"   {len(xml_data['parameters'])} paramètres, {len(xml_data['crosstabs'])} crosstabs")

    # 2. Convertir le layout vers visual_extraction.json
    print("2. Conversion layout Cognos...")
    visual_data = layout_parser.parse_cognos_layout(xml_data)
    visual_data = layout_parser.add_slicers_to_sheets(visual_data, xml_data)
    visual_json_path = output_dir / "visual_extraction.json"
    visual_json_path.write_text(json.dumps(visual_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   -> {visual_json_path}")
    print(f"   {len(visual_data['sheets'])} pages")

    # 3. Classifier les expressions (report-agnostic — no hardcoded keywords)
    print("3. Classification des expressions...")
    classified = expression_parser.classify_expressions_from_queries(xml_data)
    prompt_context = expression_parser.build_prompt_context(classified, xml_data)
    print(f"   {len(classified['unresolved'])} expressions unresolved (types D & H -> LLM)")
    print(f"   {len(classified['param_switches'])} expressions param_switches (-> deterministic SWITCH)")
    print(f"   {len(classified['variance'])} expressions variance (-> deterministic)")

    # 4. Read CSV schema for deterministic translation + TI column check
    print("4. Lecture schéma CSV...")
    if example_name:
        example_dir = ROOT / "examples" / example_name
    else:
        example_dir = xml_path.parent.parent  # fallback
    input_dir = example_dir / "input"
    csv_schema = {}
    csv_schema_with_samples = {}
    if input_dir.exists():
        csv_schema = deterministic_translator.read_csv_schema(input_dir)
        csv_schema_with_samples = deterministic_translator.read_csv_schema_with_samples(input_dir)
    csv_schema_path = output_dir / "csv_schema.json"
    csv_schema_path.write_text(json.dumps(csv_schema, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   -> {csv_schema_path} ({len(csv_schema)} tables)")

    # 5. Read DATA_DICTIONARY.md if present (business context + explicit relationships)
    print("5. Lecture DATA_DICTIONARY...")
    dd_path = example_dir / "DATA_DICTIONARY.md"
    data_dict = data_dictionary.parse_data_dictionary(dd_path)
    if data_dict["table_map"]:
        print(f"   {len(data_dict['table_map'])} tables, {len(data_dict['relationships'])} relations, {len(data_dict['shared_keys'])} clés partagées")
    else:
        print("   (absent)")

    parser_sp.end()

    return {
        "xml_data": xml_data,
        "visual_data": visual_data,
        "classified": classified,
        "prompt_context": prompt_context,
        "csv_schema": csv_schema,
        "csv_schema_with_samples": csv_schema_with_samples,
        "data_dict": data_dict,
    }


def run_phase2a_translation(phase1_output: dict, output_dir: pathlib.Path, mode: str = "api", trace=None) -> dict:
    """Phase 2a: Traduction unifiée (déterministe + 1 appel LLM DAX).

    Args:
        phase1_output: Dict produit par run_phase1_deterministic
        output_dir: Répertoire de sortie
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec {measures: [...], parameter_tables: [...]}
    """
    print("\n=== Phase 2a: Traduction Unifiée (DAX) ===")

    # --- Deterministic translation (0 LLM) ---
    print("\n[1/2] Traduction déterministe...")
    det_sp = (trace or obs._Noop()).span(name="Deterministic_Translation")

    det_result = deterministic_translator.translate_deterministic(
        classified=phase1_output["classified"],
        named_styles=phase1_output["xml_data"].get("namedStyles", {}),
        csv_schema=phase1_output["csv_schema"],
        parameters=phase1_output["xml_data"].get("parameters", []),
        xml_data=phase1_output["xml_data"],
    )

    aggregate_map_path = output_dir / "aggregate_map.json"
    aggregate_map_path.write_text(
        json.dumps(det_result.get("aggregate_map", {}), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"   -> {aggregate_map_path}")

    print(f"   {len(det_result['measures'])} mesures déterministes générées")
    print(f"   {len(det_result['deferred'])} expressions différées vers le LLM")

    det_sp.end()

    # --- Single unified LLM call ---
    print("\n[2/2] Appel LLM unifié (expressions non résolues)...")
    llm_sp = (trace or obs._Noop()).span(name="LLM_Unified_Translation")

    det_measure_names = [m["name"] for m in det_result["measures"]]
    unified_output_path = output_dir / "unified_translation.json"

    llm_result = unified_translation.run(
        prompt_context=phase1_output["prompt_context"],
        deferred=det_result["deferred"],
        csv_schema=phase1_output.get("csv_schema_with_samples") or phase1_output["csv_schema"],
        deterministic_measure_names=det_measure_names,
        output_path=unified_output_path,
        mode=mode,
        trace=trace,
        data_dictionary_text=phase1_output.get("data_dict", {}).get("raw_text", ""),
    )

    llm_sp.end()

    # --- Merge deterministic + LLM measures ---
    all_measures = det_result["measures"] + llm_result.get("measures", [])
    all_param_tables = det_result["parameter_tables"] + llm_result.get("parameter_tables", [])

    merged = {
        "measures": all_measures,
        "parameter_tables": all_param_tables,
        "aggregate_map": det_result.get("aggregate_map", {}),
    }

    # Write merged output for Phase 3
    merged_path = output_dir / "merged_translation.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n   Total: {len(all_measures)} mesures, {len(all_param_tables)} tables paramètres")
    print(f"   -> {merged_path}")

    return merged


def run_phase2b_visual_llm(
    phase1_output: dict,
    bim_path: pathlib.Path,
    output_dir: pathlib.Path,
    mode: str = "api",
    trace=None,
) -> dict:
    """Phase 2b: LLM visuel — câblage des puits + layout/titres (2 appels LLM).

    Args:
        phase1_output: Dict de Phase 1
        bim_path: Chemin vers model.bim (BIM complet avec mesures)
        output_dir: Répertoire de sortie
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec {visual_wiring: [...], layout_pages: [...], primary_color: str}
    """
    print("\n=== Phase 2b: LLM Visuel (viz + layout) ===")

    xml_data = phase1_output["xml_data"]
    visual_data = phase1_output["visual_data"]

    palette = theme_builder.extract_color_palette(xml_data)
    primary_color = palette[0] if palette else "#0078D4"

    # VIZ LLM — map Cognos fields → PBI wells
    print("  VIZ wiring (LLM)...")
    viz_wiring_path = output_dir / "visual_wiring.json"
    visual_wiring = viz_translation.run(
        visual_extraction=visual_data,
        bim_path=bim_path,
        output_path=viz_wiring_path,
        mode=mode,
        trace=trace,
        xml_data=xml_data,
    )

    # Layout LLM — positions + titles + header text (1 call/page)
    print("  Layout (LLM)...")
    layout_pages = layout_translation.run(
        visual_extraction=visual_data,
        output_dir=output_dir,
        mode=mode,
        trace=trace,
    )

    return {
        "visual_wiring": visual_wiring,
        "layout_pages": layout_pages,
        "primary_color": primary_color,
    }


def run_phase3_generator(
    example: str,
    phase1_output: dict,
    merged_translation: dict,
    phase2b_output: dict,
    output_dir: pathlib.Path,
    report_name: str = "MigrationCognosPBI",
    trace=None,
) -> None:
    """Phase 3: Générateur Déterministe (0 appel LLM).

    Génère le projet .pbip final. Toutes les décisions LLM ont déjà été prises
    en Phase 2 — cette étape est purement déterministe.

    Args:
        example: Nom de l'exemple
        phase1_output: Dict de Phase 1
        merged_translation: Dict de Phase 2a (measures + parameter_tables)
        phase2b_output: Dict de Phase 2b (visual_wiring + layout_pages + primary_color)
        output_dir: Répertoire de sortie
        report_name: Nom du rapport
        trace: Observability trace
    """
    gen_sp = (trace or obs._Noop()).span(name="PBI_Generator")

    print("\n=== Phase 3: Générateur Déterministe ===")

    example_dir = ROOT / "examples" / example
    pbip_dir = example_dir / "pbip"
    bim_path = pbip_dir / f"{report_name}.SemanticModel" / "model.bim"
    report_path = pbip_dir / f"{report_name}.Report" / "report.json"

    visual_data = phase1_output["visual_data"]
    xml_data = phase1_output["xml_data"]
    primary_color = phase2b_output["primary_color"]
    visual_wiring = phase2b_output["visual_wiring"]
    layout_pages = phase2b_output["layout_pages"]

    # 6. Conditional formatting (color measures → visuals)
    color_measures = [m for m in merged_translation.get("measures", []) if m.get("type") == "color"]
    if color_measures:
        print("   Conditional formatting...")
        pbip_generator.apply_conditional_formatting_to_report(report_path, color_measures)

    # 7. Apply viz wiring → prototypeQuery + projections
    print("   Visual wiring...")
    column_aggregates = merged_translation.get("aggregate_map", {}).get("by_column", {})
    pbip_generator.wire_from_spec(report_path, visual_wiring, bim_path, column_aggregates=column_aggregates)
    pbip_generator.wire_slots_fallback(report_path, visual_data, bim_path, column_aggregates=column_aggregates)

    # 8. Layout + styling (all deterministic — LLM results from phase 2b)
    print("   Layout + styling...")
    pbip_generator.apply_layout_to_report(report_path, layout_pages, primary_color)
    pbip_generator.apply_visual_styles_to_report(report_path, primary_color)

    # 9. Slicer defaults (runs after wire_from_spec so Field well is resolved)
    pbip_generator.apply_slicer_defaults_to_report(report_path, visual_data, visual_wiring)

    # 10. Bookmarks
    print("   Bookmarks...")
    bookmarks = pbip_generator.create_bookmarks(xml_data, visual_data)
    pbip_generator.apply_bookmarks_to_report(report_path, bookmarks)

    # 11. Rename cfg.name → human-readable title (PBI Selection pane)
    pbip_generator.apply_display_names_to_report(report_path, layout_pages)

    # 12. Entry point
    entry_point = pbip_dir / f"{report_name}.pbip"
    entry_point.write_text(json.dumps({
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{report_name}.Report"}}]
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n   -> {entry_point}")

    # 13. Validate
    print("   Validation model.bim...")
    validation_errors = validator.validate_model(bim_path)
    exit_code = validator.print_validation_report(validation_errors)
    if exit_code != 0:
        print("\n⚠️ Erreurs bloquantes détectées - Power BI rejettera probablement ce modèle")

    gen_sp.end()


def _build_bim_and_base_report(
    example: str,
    phase1_output: dict,
    merged_translation: dict,
    output_dir: pathlib.Path,
    report_name: str,
    trace=None,
    sql_config: dict | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Deterministic: build BIM + base report.json from scratch.

    This step must complete before Phase 2b (viz LLM needs the full BIM inventory).

    Returns:
        (bim_path, report_path)
    """
    example_dir = ROOT / "examples" / example
    input_dir = example_dir / "input"
    pbip_dir = example_dir / "pbip"

    print("\n=== BIM Build (déterministe) ===")

    # Clean output
    if pbip_dir.exists():
        shutil.rmtree(pbip_dir, ignore_errors=True)
    pbip_dir.mkdir(parents=True, exist_ok=True)

    aggregate_map = merged_translation.get("aggregate_map", {})

    # Semantic model from CSV
    data_dict = phase1_output.get("data_dict", {})
    if input_dir.exists() and list(input_dir.glob("*.csv")):
        table_names = pbip_builder.build_semantic_model(
            input_dir, pbip_dir, report_name,
            explicit_relationships=data_dict.get("relationships"),
            sql_config=sql_config,
            column_aggregates=aggregate_map.get("by_column", {}),
        )
        mode_label = "DirectQuery SQL" if sql_config else "Import CSV"
        print(f"   {len(table_names)} tables créées ({mode_label})")
    else:
        print("   Pas de CSV — modèle vide")

    # Base report.json
    pbip_builder.build_report(output_dir, pbip_dir, report_name)

    # Theme
    report_path = pbip_dir / f"{report_name}.Report" / "report.json"
    theme_builder.apply_theme_to_report(
        xml_data=phase1_output["xml_data"],
        pbip_dir=pbip_dir,
        report_path=report_path,
        report_name=report_name,
    )

    bim_path = pbip_dir / f"{report_name}.SemanticModel" / "model.bim"

    # Minimal BIM if no CSV
    if not bim_path.exists():
        bim = {"name": "SemanticModel", "compatibilityLevel": 1550,
               "model": {"culture": "fr-FR", "tables": [], "relationships": []}}
        bim_path.parent.mkdir(parents=True, exist_ok=True)
        bim_path.write_text(json.dumps(bim, ensure_ascii=False, indent=2), encoding="utf-8")

    xml_data = phase1_output["xml_data"]
    csv_schema = phase1_output["csv_schema"]

    # Parameter tables
    param_tables = pbip_generator.create_parameter_tables(xml_data, csv_schema)
    param_rels = pbip_generator.create_parameter_relationships(param_tables)
    for pt in merged_translation.get("parameter_tables", []):
        if not any(t["name"] == pt["name"] for t in param_tables):
            rows = [{pt["column"]: v, f"{pt['column']}_Label": v} for v in pt.get("values", [])]
            param_tables.append(pbip_generator._build_disconnected_table(pt["name"], pt["column"], rows))
    pbip_generator.merge_parameter_tables_to_bim(bim_path, param_tables, param_rels)

    # Measures
    pbip_generator.merge_measures_to_bim(
        bim_path, merged_translation.get("measures", []),
        aggregate_by_name=aggregate_map.get("by_name", {}),
    )

    print(f"   BIM prêt: {bim_path}")
    return bim_path, report_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline Cognos vers Power BI"
    )
    parser.add_argument("--example", default=None)
    parser.add_argument("--xml", default=None)
    parser.add_argument("--mode", choices=["api", "paste"], default="api")
    parser.add_argument("--skip-llm", action="store_true", help="Réutiliser merged_translation.json existant")
    parser.add_argument("--skip-data", action="store_true", help="Réutiliser BIM existant, relancer seulement viz+layout LLM")
    args = parser.parse_args()

    example = args.example or os.environ.get("COGNOS_EXAMPLE_NAME")
    xml_raw = args.xml or os.environ.get("COGNOS_XML_PATH")

    if not example:
        print("Erreur: --example requis ou COGNOS_EXAMPLE_NAME manquant dans .env")
        sys.exit(1)
    if not xml_raw:
        print("Erreur: --xml requis ou COGNOS_XML_PATH manquant dans .env")
        sys.exit(1)

    xml_path = pathlib.Path(xml_raw)
    if not xml_path.is_absolute():
        xml_path = ROOT / xml_path
    if not xml_path.exists():
        print(f"Erreur: XML introuvable {xml_path}")
        sys.exit(1)

    example_dir = ROOT / "examples" / example
    intermediate_dir = example_dir / "intermediate"
    intermediate_dir.mkdir(parents=True, exist_ok=True)
    report_name = "MigrationCognosPBI"

    sql_server = os.environ.get("SQL_SERVER")
    sql_config = None
    if sql_server:
        sql_config = {
            "server": sql_server,
            "database": os.environ["SQL_DATABASE"],
            "user": os.environ.get("SQL_USER"),
            "password": os.environ.get("SQL_PASSWORD"),
            "schema": os.environ.get("SQL_SCHEMA", "dbo"),
        }
        print(f"SQL Server mode: {sql_config['server']} / {sql_config['database']} (DirectQuery)")
    else:
        print("CSV mode (SQL_SERVER absent du .env)")

    print(f"Example: {example}")
    print(f"XML: {xml_path}")
    print(f"Output: {example_dir / 'pbip'}\n")

    with obs.trace("Cognos_to_PBI_Migration", example=example, mode=args.mode) as trace:

        if args.skip_data:
            # ── SKIP-DATA MODE: reuse BIM, rerun 2b + 3 ─────────────────────
            print("=== Mode --skip-data : réutilisation BIM ===")
            cognos_json = intermediate_dir / "cognos_extraction.json"
            if not cognos_json.exists():
                print("Erreur: intermediate files manquants — lance d'abord sans --skip-data")
                sys.exit(1)

            xml_data = json.loads(cognos_json.read_text(encoding="utf-8"))
            from cognos import layout_parser as _lp
            visual_data = _lp.parse_cognos_layout(xml_data)
            visual_data = _lp.add_slicers_to_sheets(visual_data, xml_data)
            (intermediate_dir / "visual_extraction.json").write_text(
                json.dumps(visual_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            pbip_dir = example_dir / "pbip"
            bim_path = pbip_dir / f"{report_name}.SemanticModel" / "model.bim"
            report_path = pbip_dir / f"{report_name}.Report" / "report.json"
            if not bim_path.exists():
                print(f"Erreur: BIM introuvable — lance d'abord sans --skip-data")
                sys.exit(1)

            # Rebuild base report.json
            pbip_builder.build_report(intermediate_dir, pbip_dir, report_name)
            theme_builder.apply_theme_to_report(xml_data, pbip_dir, report_path, report_name)

            # Phase 2b: viz + layout LLM
            phase1_proxy = {"xml_data": xml_data, "visual_data": visual_data}
            phase2b = run_phase2b_visual_llm(phase1_proxy, bim_path, intermediate_dir, args.mode, trace)

            # Phase 3: deterministic apply
            merged_path = intermediate_dir / "merged_translation.json"
            merged = json.loads(merged_path.read_text(encoding="utf-8")) if merged_path.exists() else {"measures": [], "parameter_tables": []}
            run_phase3_generator(example, phase1_proxy, merged, phase2b, intermediate_dir, report_name, trace)

            print(f"\nOK --skip-data terminé -> {pbip_dir}")

        else:
            # ── FULL PIPELINE ────────────────────────────────────────────────
            # Phase 1: parse
            phase1_output = run_phase1_deterministic(xml_path, intermediate_dir, example, trace)

            # Phase 2a: DAX LLM
            if not args.skip_llm:
                merged_translation = run_phase2a_translation(phase1_output, intermediate_dir, args.mode, trace)
            else:
                print("\n=== Phase 2a: Skip LLM (réutilisation merged_translation.json) ===")
                merged_path = intermediate_dir / "merged_translation.json"
                if merged_path.exists():
                    merged_translation = json.loads(merged_path.read_text(encoding="utf-8"))
                    print(f"   {len(merged_translation.get('measures', []))} mesures chargées")
                else:
                    det_result = deterministic_translator.translate_deterministic(
                        classified=phase1_output["classified"],
                        named_styles=phase1_output["xml_data"].get("namedStyles", {}),
                        csv_schema=phase1_output["csv_schema"],
                        parameters=phase1_output["xml_data"].get("parameters", []),
                        xml_data=phase1_output["xml_data"],
                    )
                    merged_translation = {
                        "measures": det_result["measures"],
                        "parameter_tables": det_result["parameter_tables"],
                        "aggregate_map": det_result.get("aggregate_map", {}),
                    }

            # BIM build (deterministic — must complete before phase 2b)
            bim_path, _ = _build_bim_and_base_report(example, phase1_output, merged_translation, intermediate_dir, report_name, trace, sql_config=sql_config)

            # Phase 2b: viz + layout LLM (uses completed BIM)
            phase2b_output = run_phase2b_visual_llm(phase1_output, bim_path, intermediate_dir, args.mode, trace)

            # Phase 3: deterministic apply (pure det — no LLM)
            run_phase3_generator(example, phase1_output, merged_translation, phase2b_output, intermediate_dir, report_name, trace)

    print(f"\nOK Pipeline terminée -> {example_dir / 'pbip'}")
    print(f"  Ouvrez {example_dir / 'pbip' / 'MigrationCognosPBI.pbip'} dans Power BI Desktop")


if __name__ == "__main__":
    main()