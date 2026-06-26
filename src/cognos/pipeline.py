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
from cognos import deterministic_translator
from cognos.llm import unified_translation
from cognos import pbip_generator, validator

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
    print(f"   {len(classified['unresolved'])} expressions unresolved (types D & H → LLM)")
    print(f"   {len(classified['param_switches'])} expressions param_switches (→ deterministic SWITCH)")
    print(f"   {len(classified['variance'])} expressions variance (→ deterministic)")

    # 4. Read CSV schema for deterministic translation + TI column check
    print("4. Lecture schéma CSV...")
    if example_name:
        example_dir = ROOT / "examples" / example_name
    else:
        example_dir = xml_path.parent.parent  # fallback
    input_dir = example_dir / "input"
    csv_schema = {}
    if input_dir.exists():
        csv_schema = deterministic_translator.read_csv_schema(input_dir)
    csv_schema_path = output_dir / "csv_schema.json"
    csv_schema_path.write_text(json.dumps(csv_schema, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   -> {csv_schema_path} ({len(csv_schema)} tables)")

    parser_sp.end()

    return {
        "xml_data": xml_data,
        "visual_data": visual_data,
        "classified": classified,
        "prompt_context": prompt_context,
        "csv_schema": csv_schema,
    }


def run_phase2_translation(phase1_output: dict, output_dir: pathlib.Path, mode: str = "api", trace=None) -> dict:
    """Phase 2: Traduction unifiée (déterministe + 1 appel LLM).

    Args:
        phase1_output: Dict produit par run_phase1_deterministic
        output_dir: Répertoire de sortie
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec {measures: [...], parameter_tables: [...]}
    """
    print("\n=== Phase 2: Traduction Unifiée ===")

    # --- Deterministic translation (0 LLM) ---
    print("\n[1/2] Traduction déterministe...")
    det_sp = (trace or obs._Noop()).span(name="Deterministic_Translation")

    det_result = deterministic_translator.translate_deterministic(
        classified=phase1_output["classified"],
        named_styles=phase1_output["xml_data"].get("namedStyles", {}),
        csv_schema=phase1_output["csv_schema"],
        parameters=phase1_output["xml_data"].get("parameters", []),
    )

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
        csv_schema=phase1_output["csv_schema"],
        deterministic_measure_names=det_measure_names,
        output_path=unified_output_path,
        mode=mode,
        trace=trace,
    )

    llm_sp.end()

    # --- Merge deterministic + LLM measures ---
    all_measures = det_result["measures"] + llm_result.get("measures", [])
    all_param_tables = det_result["parameter_tables"] + llm_result.get("parameter_tables", [])

    merged = {
        "measures": all_measures,
        "parameter_tables": all_param_tables,
    }

    # Write merged output for Phase 3
    merged_path = output_dir / "merged_translation.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n   Total: {len(all_measures)} mesures, {len(all_param_tables)} tables paramètres")
    print(f"   -> {merged_path}")

    return merged


def run_phase3_generator(
    example: str,
    phase1_output: dict,
    merged_translation: dict,
    output_dir: pathlib.Path,
    report_name: str = "MigrationCognosPBI",
    trace=None
) -> None:
    """Phase 3: Générateur Déterministe (0 appel LLM).

    Génère le projet .pbip final avec la nouvelle architecture.

    Args:
        example: Nom de l'exemple
        phase1_output: Dict de Phase 1
        merged_translation: Dict de Phase 2 (measures + parameter_tables)
        output_dir: Répertoire de sortie
        report_name: Nom du rapport
        trace: Observability trace
    """
    gen_sp = (trace or obs._Noop()).span(name="PBI_Generator")

    print("\n=== Phase 3: Générateur Déterministe ===")

    # Répertoires
    example_dir = ROOT / "examples" / example
    input_dir = example_dir / "input"
    pbip_dir = example_dir / "pbip"
    pbip_dir.mkdir(parents=True, exist_ok=True)

    # 1. Générer le modèle sémantique de base depuis CSV
    print("1. Génération modèle sémantique depuis CSV...")
    if input_dir.exists() and list(input_dir.glob("*.csv")):
        table_names = pbip_builder.build_semantic_model(input_dir, pbip_dir)
        print(f"   {len(table_names)} tables créées")
    else:
        print("   Pas de CSV - modèle vide")

    # 2. Générer le rapport de base depuis visual_extraction.json
    print("2. Génération rapport depuis visual_extraction.json...")
    pbip_builder.build_report(output_dir, pbip_dir)
    print(f"   Rapport généré")

    # 3. Appliquer les spécificités Cognos
    bim_path = pbip_dir / f"{pbip_builder.REPORT_NAME}.SemanticModel" / "model.bim"
    report_path = pbip_dir / f"{pbip_builder.REPORT_NAME}.Report" / "report.json"

    # Renommer vers le bon nom de rapport Cognos
    final_bim_path = pbip_dir / f"{report_name}.SemanticModel" / "model.bim"
    final_report_path = pbip_dir / f"{report_name}.Report" / "report.json"

    if bim_path != final_bim_path:
        old_sm = pbip_dir / f"{pbip_builder.REPORT_NAME}.SemanticModel"
        new_sm = pbip_dir / f"{report_name}.SemanticModel"
        if old_sm.exists():
            shutil.move(str(old_sm), str(new_sm))

        old_report = pbip_dir / f"{pbip_builder.REPORT_NAME}.Report"
        new_report = pbip_dir / f"{report_name}.Report"
        if old_report.exists():
            shutil.move(str(old_report), str(new_report))

        bim_path = new_sm / "model.bim"
        report_path = new_report / "report.json"

    print("3. Application spécificités Cognos...")

    # Créer un model.bim minimal si pas de CSV
    if not bim_path.exists():
        print("   Création model.bim minimal...")
        bim = {
            "name": "SemanticModel",
            "compatibilityLevel": 1550,
            "model": {
                "culture": "fr-FR",
                "tables": [],
                "relationships": []
            }
        }
        bim_path.parent.mkdir(parents=True, exist_ok=True)
        bim_path.write_text(json.dumps(bim, ensure_ascii=False, indent=2), encoding="utf-8")

    xml_data = phase1_output["xml_data"]
    visual_data = phase1_output["visual_data"]
    csv_schema = phase1_output["csv_schema"]

    # 4. Tables paramètres (derived from XML + CSV, NOT hardcoded)
    print("   Tables paramètres...")
    param_tables = pbip_generator.create_parameter_tables(xml_data, csv_schema)
    param_rels = pbip_generator.create_parameter_relationships(param_tables)
    # Merge LLM-provided parameter tables if any
    for pt in merged_translation.get("parameter_tables", []):
        # Convert LLM param table spec to BIM table
        if not any(t["name"] == pt["name"] for t in param_tables):
            rows = [{pt["column"]: v, f"{pt['column']}_Label": v} for v in pt.get("values", [])]
            param_tables.append(
                pbip_generator._build_disconnected_table(pt["name"], pt["column"], rows)
            )
    pbip_generator.merge_parameter_tables_to_bim(bim_path, param_tables, param_rels)

    # 5. Mesures (flat contract — deterministic + LLM)
    print("   Mesures...")
    pbip_generator.merge_measures_to_bim(bim_path, merged_translation.get("measures", []))

    # 6. Conditional formatting (link color measures to visuals)
    color_measures = [m for m in merged_translation.get("measures", []) if m.get("type") == "color"]
    if color_measures:
        print("   Conditional formatting...")
        pbip_generator.apply_conditional_formatting_to_report(report_path, color_measures)

    # 7. Bookmarks (real dual-matrix + selection-pane toggle)
    print("   Bookmarks...")
    bookmarks = pbip_generator.create_bookmarks(xml_data, visual_data)
    pbip_generator.apply_bookmarks_to_report(report_path, bookmarks)

    # 8. Créer le fichier .pbip entry point
    entry_point = pbip_dir / f"{report_name}.pbip"
    entry_point.write_text(json.dumps({
        "version": "1.0",
        "artifacts": [
            {"report": {"path": f"{report_name}.Report"}}
        ]
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n   -> {entry_point}")

    # 9. Valider le modèle
    print("9. Validation model.bim...")
    validation_errors = validator.validate_model(bim_path)
    exit_code = validator.print_validation_report(validation_errors)
    if exit_code != 0:
        print("\n⚠️ Erreurs bloquantes détectées - Power BI rejettera probablement ce modèle")

    gen_sp.end()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline Cognos vers Power BI (1-prompt architecture)"
    )
    parser.add_argument("--example", required=True, help="Nom de l'exemple (répertoire)")
    parser.add_argument("--xml", required=True, help="Chemin vers le XML Cognos")
    parser.add_argument("--mode", choices=["api", "paste"], default="api", help="Mode LLM")
    parser.add_argument("--skip-llm", action="store_true", help="Sauter l'appel LLM (réutiliser outputs existants)")
    args = parser.parse_args()

    example = args.example
    xml_path = pathlib.Path(args.xml)

    if not xml_path.exists():
        print(f"Erreur: XML introuvable {xml_path}")
        sys.exit(1)

    # Répertoires
    example_dir = ROOT / "examples" / example
    intermediate_dir = example_dir / "intermediate"
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    print(f"Example: {example}")
    print(f"XML: {xml_path}")
    print(f"Output: {example_dir / 'pbip'}\n")

    # Trace principale
    with obs.trace("Cognos_to_PBI_Migration", example=example, mode=args.mode) as trace:
        # Phase 1: Parseur Déterministe
        phase1_output = run_phase1_deterministic(xml_path, intermediate_dir, example, trace)

        # Phase 2: Traduction (déterministe + LLM)
        if not args.skip_llm:
            merged_translation = run_phase2_translation(phase1_output, intermediate_dir, args.mode, trace)
        else:
            print("\n=== Phase 2: Skip LLM (réutilisation outputs existants) ===")
            merged_path = intermediate_dir / "merged_translation.json"
            if merged_path.exists():
                merged_translation = json.loads(merged_path.read_text(encoding="utf-8"))
                print(f"   merged_translation.json chargé ({len(merged_translation.get('measures', []))} mesures)")
            else:
                # Rebuild from deterministic only (no LLM)
                print("   merged_translation.json introuvable - reconstruction déterministe seule")
                det_result = deterministic_translator.translate_deterministic(
                    classified=phase1_output["classified"],
                    named_styles=phase1_output["xml_data"].get("namedStyles", {}),
                    csv_schema=phase1_output["csv_schema"],
                    parameters=phase1_output["xml_data"].get("parameters", []),
                )
                merged_translation = {
                    "measures": det_result["measures"],
                    "parameter_tables": det_result["parameter_tables"],
                }

        # Phase 3: Générateur
        run_phase3_generator(example, phase1_output, merged_translation, intermediate_dir, "MigrationCognosPBI", trace)

    print(f"\nOK Pipeline terminée -> {example_dir / 'pbip'}")
    print(f"  Ouvrez {example_dir / 'pbip' / 'MigrationCognosPBI.pbip'} dans Power BI Desktop")


if __name__ == "__main__":
    main()