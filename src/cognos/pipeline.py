"""Orchestrateur principal de la pipeline Cognos vers Power BI.

Coordonne les 3 phases de la pipeline hybride:
- Phase 1: Parseur Déterministe (0 LLM)
- Phase 2: Traduction LLM Ciblée (3 appels LLM)
- Phase 3: Générateur Déterministe (0 LLM)

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
from cognos.llm import time_intelligence, parameter_logic, variance_formatting
from cognos import pbip_generator, validator

# Imports des modules existants pour réutilisation
from pbip import pbip_builder


def run_phase1_deterministic(xml_path: pathlib.Path, output_dir: pathlib.Path, trace=None) -> dict:
    """Phase 1: Parseur Déterministe (0 appel LLM).

    Extrait et convertit le XML Cognos vers JSON intermédiaire.

    Args:
        xml_path: Chemin vers le XML Cognos
        output_dir: Répertoire de sortie
        trace: Observability trace

    Returns:
        Dict avec xml_data + visual_data
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

    # 3. Classifier les expressions pour LLM
    print("3. Classification des expressions...")
    classified = expression_parser.classify_expressions_from_queries(xml_data)
    llm_context = expression_parser.build_prompt_context(classified, xml_data)
    print(f"   {len(classified['time_logic'])} expressions time_logic")
    print(f"   {len(classified['variance'])} expressions variance")

    parser_sp.end()

    return {
        "xml_data": xml_data,
        "visual_data": visual_data,
        "classified": classified,
        "llm_context": llm_context
    }


def run_phase2_llm(phase1_output: dict, output_dir: pathlib.Path, mode: str = "api", trace=None) -> dict:
    """Phase 2: Traduction LLM Ciblée (3 appels LLM).

    Args:
        phase1_output: Dict produit par run_phase1_deterministic
        output_dir: Répertoire de sortie
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec les outputs des 3 appels LLM
    """
    print("\n=== Phase 2: Traduction LLM Ciblée ===")

    llm_outputs = {}

    # Call 1: Time Intelligence
    print("\n[Call 1/3] Time Intelligence Foundation...")
    time_sp = (trace or obs._Noop()).span(name="LLM_Time_Intelligence")

    time_intel_path = output_dir / "time_intelligence.json"
    time_intelligence.run(
        etl_context={},  # Sera rempli si CSV disponibles
        output_path=time_intel_path,
        mode=mode,
        trace=trace
    )
    llm_outputs["time_intelligence"] = json.loads(time_intel_path.read_text(encoding="utf-8"))

    time_sp.end()

    # Call 2: Parameter Logic
    print("\n[Call 2/3] Parameter Switching Logic...")
    param_sp = (trace or obs._Noop()).span(name="LLM_Switch_Logic")

    param_logic_path = output_dir / "parameter_logic.json"
    parameter_logic.run(
        xml_data=phase1_output["xml_data"],
        time_intel_path=time_intel_path,
        output_path=param_logic_path,
        mode=mode,
        trace=trace
    )
    llm_outputs["parameter_logic"] = json.loads(param_logic_path.read_text(encoding="utf-8"))

    param_sp.end()

    # Call 3: Variance & Formatting
    print("\n[Call 3/3] Variance & Formatting Rules...")
    var_sp = (trace or obs._Noop()).span(name="LLM_Variance_Formatting")

    var_fmt_path = output_dir / "variance_formatting.json"
    variance_formatting.run(
        xml_data=phase1_output["xml_data"],
        param_logic_path=param_logic_path,
        output_path=var_fmt_path,
        mode=mode,
        trace=trace
    )
    llm_outputs["variance_formatting"] = json.loads(var_fmt_path.read_text(encoding="utf-8"))

    var_sp.end()

    return llm_outputs


def run_phase3_generator(
    example: str,
    phase1_output: dict,
    llm_outputs: dict,
    output_dir: pathlib.Path,
    report_name: str = "MigrationCognosPBI",
    trace=None
) -> None:
    """Phase 3: Générateur Déterministe (0 appel LLM).

    Génère le projet .pbip final.

    Args:
        example: Nom de l'exemple
        phase1_output: Dict de Phase 1
        llm_outputs: Dict de Phase 2
        output_dir: Répertoire de sortie
        report_name: Nom du rapport (défaut: MigrationCognosPBI)
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
    visual_json_path = output_dir / "visual_extraction.json"
    pbip_builder.build_report(output_dir, pbip_dir)
    print(f"   Rapport généré")

    # 3. Appliquer les spécificités Cognos
    bim_path = pbip_dir / f"{pbip_builder.REPORT_NAME}.SemanticModel" / "model.bim"
    report_path = pbip_dir / f"{pbip_builder.REPORT_NAME}.Report" / "report.json"

    # Renommer vers le bon nom de rapport Cognos
    final_bim_path = pbip_dir / f"{report_name}.SemanticModel" / "model.bim"
    final_report_path = pbip_dir / f"{report_name}.Report" / "report.json"

    if bim_path != final_bim_path:
        # Déplacer le dossier
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

    # Fusionner tables paramètres et mesures LLM
    print("   Tables paramètres...")

    # Charger les données XML
    xml_data = json.loads((output_dir / "cognos_extraction.json").read_text(encoding="utf-8"))

    # Créer et fusionner les tables paramètres
    param_tables = pbip_generator.create_parameter_tables(xml_data)
    param_rels = pbip_generator.create_parameter_relationships(param_tables)
    pbip_generator.merge_parameter_tables_to_bim(bim_path, param_tables, param_rels)

    # Fusionner les mesures LLM
    llm_outputs = {
        "time_intelligence": json.loads((output_dir / "time_intelligence.json").read_text(encoding="utf-8")),
        "parameter_logic": json.loads((output_dir / "parameter_logic.json").read_text(encoding="utf-8")),
        "variance_formatting": json.loads((output_dir / "variance_formatting.json").read_text(encoding="utf-8"))
    }
    pbip_generator.merge_measures_to_bim(bim_path, llm_outputs)

    # Créer et appliquer les bookmarks
    bookmarks = pbip_generator.create_bookmarks(xml_data)
    pbip_generator.apply_bookmarks_to_report(report_path, bookmarks)

    # 4. Créer le fichier .pbip entry point
    entry_point = pbip_dir / f"{report_name}.pbip"
    entry_point.write_text(json.dumps({
        "version": "1.0",
        "artifacts": [
            {"report": {"path": f"{report_name}.Report"}}
        ]
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n   -> {entry_point}")

    # 5. Valider le modèle avant ouverture
    print("5. Validation model.bim...")
    validation_errors = validator.validate_model(bim_path)
    exit_code = validator.print_validation_report(validation_errors)
    if exit_code != 0:
        print("\n⚠️ Erreurs bloquantes détectées - Power BI rejettera probablement ce modèle")

    gen_sp.end()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline Cognos vers Power BI"
    )
    parser.add_argument("--example", required=True, help="Nom de l'exemple (répertoire)")
    parser.add_argument("--xml", required=True, help="Chemin vers le XML Cognos")
    parser.add_argument("--mode", choices=["api", "paste"], default="api", help="Mode LLM")
    parser.add_argument("--skip-llm", action="store_true", help="Sauter les appels LLM (réutiliser outputs existants)")
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
        phase1_output = run_phase1_deterministic(xml_path, intermediate_dir, trace)

        # Phase 2: LLM (optionnel)
        llm_outputs = {}
        if not args.skip_llm:
            llm_outputs = run_phase2_llm(phase1_output, intermediate_dir, args.mode, trace)
        else:
            print("\n=== Phase 2: Skip LLM (réutilisation outputs existants) ===")
            for name in ["time_intelligence", "parameter_logic", "variance_formatting"]:
                json_path = intermediate_dir / f"{name}.json"
                if json_path.exists():
                    llm_outputs[name] = json.loads(json_path.read_text(encoding="utf-8"))
                    print(f"   {name}.json chargé")

        # Phase 3: Générateur
        run_phase3_generator(example, phase1_output, llm_outputs, intermediate_dir, "MigrationCognosPBI", trace)

    print(f"\nOK Pipeline terminée -> {example_dir / 'pbip'}")
    print(f"  Ouvrez {example_dir / 'pbip' / 'MigrationCognosPBI.pbip'} dans Power BI Desktop")


if __name__ == "__main__":
    main()
