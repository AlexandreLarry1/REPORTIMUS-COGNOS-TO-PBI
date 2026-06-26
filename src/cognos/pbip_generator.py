"""Générateur PBIP spécifique Cognos.

Génère les éléments du projet Power BI spécifiques à la migration Cognos:
1. Tables paramètres déconnectées (remplacement des ?param?)
2. Bookmarks (remplacement des conditionalRender/refVariable)
3. Intégration des mesures LLM dans le modèle sémantique
4. Assemblage final du .pbip
"""
import json
import pathlib
import uuid
from typing import Any

# Import des modules existants pour réutilisation
ROOT = pathlib.Path(__file__).parent.parent.parent

# Constants
REPORT_NAME = "MigrationCognosPBI"
CANVAS_W = 1280.0
CANVAS_H = 720.0


def _uid() -> str:
    """Génère un UUID unique."""
    return str(uuid.uuid4())


def _hex20() -> str:
    """Génère un identifiant hex court."""
    return uuid.uuid4().hex[:20]


def create_parameter_tables(xml_data: dict) -> list[dict]:
    """Crée les tables de paramètres déconnectées pour remplacer ?param?.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()

    Returns:
        Liste de définitions de tables BIM
    """
    tables = []

    for param in xml_data.get("parameters", []):
        name = param.get("name", "")
        options = param.get("options", [])

        if not name:
            continue

        # Créer les lignes de la table
        rows = []
        for opt in options:
            value = opt.get("value", "")
            label = opt.get("label", value)
            rows.append({f"{name}": value, f"{name}_Label": label})

        # Si pas d'options, créer une table avec une valeur par défaut
        if not rows:
            default_val = param.get("defaultValue", "All")
            rows.append({f"{name}": default_val, f"{name}_Label": default_val})

        # Structure de la table BIM
        table = {
            "name": f"Param_{name}",
            "lineageTag": _uid(),
            "columns": [
                {
                    "name": name,
                    "dataType": "string",
                    "sourceColumn": name,
                    "lineageTag": _uid(),
                    "summarizeBy": "none"
                },
                {
                    "name": f"{name}_Label",
                    "dataType": "string",
                    "sourceColumn": f"{name}_Label",
                    "lineageTag": _uid(),
                    "summarizeBy": "none"
                }
            ],
            "measures": [],
            "partitions": [{
                "name": "Partition",
                "mode": "import",
                "source": {
                    "type": "m",
                    "expression": [
                        "let",
                        f'    Source = #table(type table [{name}=text, {name}_Label=text], {rows})',
                        "in",
                        "    Source"
                    ]
                }
            }]
        }
        tables.append(table)

    # Tables hiérarchiques pour Year/Quarter/Month (selon réponse utilisateur)
    # Créer des tables séparées liées
    year_table = {
        "name": "Param_Year",
        "lineageTag": _uid(),
        "columns": [
            {"name": "Year", "dataType": "string", "sourceColumn": "Year", "lineageTag": _uid(), "summarizeBy": "none"},
            {"name": "Year_Sort", "dataType": "int64", "sourceColumn": "Year_Sort", "lineageTag": _uid(), "summarizeBy": "none"}
        ],
        "measures": [],
        "partitions": [{
            "name": "Partition",
            "mode": "import",
            "source": {
                "type": "m",
                "expression": [
                    "let",
                    '    Source = #table(type table [Year=text, Year_Sort=int64], {{"Year", "Year_Sort"},',
                    '        {"2022", 2022}, {"2023", 2023}, {"2024", 2024}',
                    '    }),',
                    "in",
                    "    Source"
                ]
            }
        }]
    }
    tables.append(year_table)

    # Table Quarter avec relation Year
    quarter_table = {
        "name": "Param_Quarter",
        "lineageTag": _uid(),
        "columns": [
            {"name": "Quarter", "dataType": "string", "sourceColumn": "Quarter", "lineageTag": _uid(), "summarizeBy": "none"},
            {"name": "Year", "dataType": "string", "sourceColumn": "Year", "lineageTag": _uid(), "summarizeBy": "none"},
            {"name": "SortOrder", "dataType": "int64", "sourceColumn": "SortOrder", "lineageTag": _uid(), "summarizeBy": "none"}
        ],
        "measures": [],
        "partitions": [{
            "name": "Partition",
            "mode": "import",
            "source": {
                "type": "m",
                "expression": [
                    "let",
                    '    Source = #table(type table [Quarter=text, Year=text, SortOrder=int64], {{"Quarter", "Year", "SortOrder"},',
                    '        {"Q1", "2022", 1}, {"Q2", "2022", 2}, {"Q3", "2022", 3}, {"Q4", "2022", 4},',
                    '        {"Q1", "2023", 1}, {"Q2", "2023", 2}, {"Q3", "2023", 3}, {"Q4", "2023", 4},',
                    '        {"Q1", "2024", 1}, {"Q2", "2024", 2}, {"Q3", "2024", 3}, {"Q4", "2024", 4}',
                    '    }),',
                    "in",
                    "    Source"
                ]
            }
        }]
    }
    tables.append(quarter_table)

    # Table Month avec relation Quarter
    month_table = {
        "name": "Param_Month",
        "lineageTag": _uid(),
        "columns": [
            {"name": "Month", "dataType": "string", "sourceColumn": "Month", "lineageTag": _uid(), "summarizeBy": "none"},
            {"name": "Quarter", "dataType": "string", "sourceColumn": "Quarter", "lineageTag": _uid(), "summarizeBy": "none"},
            {"name": "SortOrder", "dataType": "int64", "sourceColumn": "SortOrder", "lineageTag": _uid(), "summarizeBy": "none"}
        ],
        "measures": [],
        "partitions": [{
            "name": "Partition",
            "mode": "import",
            "source": {
                "type": "m",
                "expression": [
                    "let",
                    '    Source = #table(type table [Month=text, Quarter=text, SortOrder=int64], {{"Month", "Quarter", "SortOrder"},',
                    '        {"Jan", "Q1", 1}, {"Feb", "Q1", 2}, {"Mar", "Q1", 3},',
                    '        {"Apr", "Q2", 4}, {"May", "Q2", 5}, {"Jun", "Q2", 6},',
                    '        {"Jul", "Q3", 7}, {"Aug", "Q3", 8}, {"Sep", "Q3", 9},',
                    '        {"Oct", "Q4", 10}, {"Nov", "Q4", 11}, {"Dec", "Q4", 12}',
                    '    }),',
                    "in",
                    "    Source"
                ]
            }
        }]
    }
    tables.append(month_table)

    return tables


def create_parameter_relationships(tables: list[dict]) -> list[dict]:
    """Crée les relations entre tables de paramètres hiérarchiques.

    Args:
        tables: Liste des tables créées par create_parameter_tables()

    Returns:
        Liste de relations BIM
    """
    relationships = []

    # Quarter → Year
    relationships.append({
        "name": "Param_Quarter_Param_Year",
        "fromTable": "Param_Quarter",
        "fromColumn": "Year",
        "toTable": "Param_Year",
        "toColumn": "Year"
    })

    # Month → Quarter
    relationships.append({
        "name": "Param_Month_Param_Quarter",
        "fromTable": "Param_Month",
        "fromColumn": "Quarter",
        "toTable": "Param_Quarter",
        "toColumn": "Quarter"
    })

    return relationships


def create_bookmarks(xml_data: dict) -> list[dict]:
    """Crée les bookmarks Power BI pour remplacer conditionalRender/refVariable.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()

    Returns:
        Liste de bookmarks pour report.json
    """
    bookmarks = []

    # Analyser les variables pour identifier les vues
    variables = xml_data.get("variables", [])

    for var in variables:
        name = var.get("name", "")
        values = var.get("values", [])

        if not name or not values:
            continue

        # Créer un bookmark pour chaque valeur
        for val in values:
            bookmark = {
                "name": f"{name}_{val}",
                "displayName": name.replace("_", " ").title() + f" - {val}",
                "enabled": True
            }
            bookmarks.append(bookmark)

    return bookmarks


def merge_measures_to_bim(bim_path: pathlib.Path, llm_outputs: dict) -> None:
    """Fusionne les mesures LLM dans le modèle sémantique existant.

    Args:
        bim_path: Chemin vers model.bim existant
        llm_outputs: Dict avec les outputs des 3 appels LLM
    """
    bim = json.loads(bim_path.read_text(encoding="utf-8"))

    # Fusionner les mesures de tous les appels LLM
    all_measures = []

    # Time Intelligence (Call 1)
    time_intel = llm_outputs.get("time_intelligence", {})
    for measure in time_intel.get("measures", []):
        all_measures.append({
            "name": measure["name"],
            "expression": measure["expression"],
            "formatString": "0.00" if "Pct" not in measure["name"] else "0.00%",
            "lineageTag": _uid()
        })

    # Parameter Logic (Call 2)
    param_logic = llm_outputs.get("parameter_logic", {})
    for measure in param_logic.get("measures", []):
        all_measures.append({
            "name": measure["name"],
            "expression": measure["expression"],
            "lineageTag": _uid()
        })

    # Variance & Formatting (Call 3)
    var_fmt = llm_outputs.get("variance_formatting", {})

    # Variance measures
    for measure in var_fmt.get("variance_measures", []):
        all_measures.append({
            "name": measure["name"],
            "expression": measure["expression"],
            "formatString": "0.00" if "Pct" not in measure["name"] else "0.00%",
            "lineageTag": _uid()
        })

    # Color measures (mesures qui retournent des HEX)
    for measure in var_fmt.get("color_measures", []):
        all_measures.append({
            "name": measure["name"],
            "expression": measure["expression"],
            "formatString": "",  # Pas de formatage pour les couleurs
            "lineageTag": _uid(),
            "isHidden": True  # Les mesures de couleur sont cachées
        })

    # Trouver la table fact pour ajouter les mesures
    for table in bim["model"]["tables"]:
        if table["name"].startswith("fact_"):
            table.setdefault("measures", []).extend(all_measures)
            print(f"  {len(all_measures)} mesures ajoutées à {table['name']}")
            break
    else:
        # Si pas de table fact, créer une table de mesures
        measures_table = {
            "name": "Measures",
            "lineageTag": _uid(),
            "columns": [],
            "measures": all_measures,
            "partitions": [{
                "name": "Partition",
                "mode": "calculated",
                "source": {"type": "none"}
            }]
        }
        bim["model"]["tables"].append(measures_table)
        print(f"  Table Measures créée avec {len(all_measures)} mesures")

    # Écrire le BIM mis à jour
    bim_path.write_text(json.dumps(bim, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_parameter_tables_to_bim(bim_path: pathlib.Path, param_tables: list[dict], relationships: list[dict]) -> None:
    """Ajoute les tables de paramètres au modèle sémantique.

    Args:
        bim_path: Chemin vers model.bim
        param_tables: Tables créées par create_parameter_tables()
        relationships: Relations créées par create_parameter_relationships()
    """
    bim = json.loads(bim_path.read_text(encoding="utf-8"))

    # Ajouter les tables
    for table in param_tables:
        bim["model"]["tables"].append(table)
        print(f"  Table paramètre ajoutée: {table['name']}")

    # Ajouter les relations
    bim["model"].setdefault("relationships", []).extend(relationships)
    print(f"  {len(relationships)} relations paramètres ajoutées")

    bim_path.write_text(json.dumps(bim, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_bookmarks_to_report(report_path: pathlib.Path, bookmarks: list[dict]) -> None:
    """Applique les bookmarks au rapport Power BI.

    Args:
        report_path: Chemin vers report.json
        bookmarks: Liste créée par create_bookmarks()
    """
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # Ajouter les bookmarks
    report.setdefault("config", "{}")
    if isinstance(report["config"], str):
        # Config est un string JSON
        config = json.loads(report["config"])
    else:
        config = report["config"]

    config["bookmarks"] = []

    for bm in bookmarks:
        config["bookmarks"].append({
            "name": bm["name"],
            "displayName": bm["displayName"],
            "enabled": bm.get("enabled", True)
        })

    report["config"] = json.dumps(config) if isinstance(report["config"], str) else config

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {len(bookmarks)} bookmarks appliqués au rapport")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Générateur PBIP Cognos")
    parser.add_argument("--xml-json", required=True, help="JSON extrait du XML Cognos")
    parser.add_argument("--time-intel", help="time_intelligence.json")
    parser.add_argument("--param-logic", help="parameter_logic.json")
    parser.add_argument("--var-fmt", help="variance_formatting.json")
    parser.add_argument("--bim-path", required=True, help="Chemin vers model.bim")
    parser.add_argument("--report-path", required=True, help="Chemin vers report.json")
    args = parser.parse_args()

    xml_path = pathlib.Path(args.xml_json)
    bim_path = pathlib.Path(args.bim_path)
    report_path = pathlib.Path(args.report_path)

    xml_data = json.loads(xml_path.read_text(encoding="utf-8"))

    print("=== Génération PBIP Cognos ===\n")

    # 1. Créer les tables de paramètres
    print("1. Tables paramètres:")
    param_tables = create_parameter_tables(xml_data)
    relationships = create_parameter_relationships(param_tables)
    merge_parameter_tables_to_bim(bim_path, param_tables, relationships)

    # 2. Fusionner les mesures LLM si fournies
    llm_outputs = {}
    if args.time_intel:
        llm_outputs["time_intelligence"] = json.loads(pathlib.Path(args.time_intel).read_text(encoding="utf-8"))
    if args.param_logic:
        llm_outputs["parameter_logic"] = json.loads(pathlib.Path(args.param_logic).read_text(encoding="utf-8"))
    if args.var_fmt:
        llm_outputs["variance_formatting"] = json.loads(pathlib.Path(args.var_fmt).read_text(encoding="utf-8"))

    if llm_outputs:
        print("\n2. Mesures LLM:")
        merge_measures_to_bim(bim_path, llm_outputs)

    # 3. Créer et appliquer les bookmarks
    print("\n3. Bookmarks:")
    bookmarks = create_bookmarks(xml_data)
    apply_bookmarks_to_report(report_path, bookmarks)

    print("\nDone.")


if __name__ == "__main__":
    main()
