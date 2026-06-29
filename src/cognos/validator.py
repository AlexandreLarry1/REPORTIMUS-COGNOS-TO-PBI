"""Validateur model.bim - détecte erreurs Power BI avant ouverture.

Vérifie:
- Colonnes manquantes (référencées dans mesures/relations mais non définies)
- Noms de tables/colonnes dupliqués
- Relations invalides (tables/colonnes inexistantes)
- Mesures avec syntaxe DAX basiquement invalide
- Champs requis manquants
"""
import json
import pathlib
import re
from typing import Any


class ValidationError:
    """Une erreur de validation."""
    def __init__(self, level: str, code: str, message: str, location: str = ""):
        self.level = level  # "error" | "warning"
        self.code = code
        self.message = message
        self.location = location

    def __str__(self) -> str:
        loc = f" [{self.location}]" if self.location else ""
        return f"[{self.level.upper()}] {self.code}: {self.message}{loc}"


def validate_model(bim_path: pathlib.Path) -> list[ValidationError]:
    """Valide un fichier model.bim et retourne la liste des erreurs.

    Args:
        bim_path: Chemin vers model.bim

    Returns:
        Liste de ValidationError
    """
    if not bim_path.exists():
        return [ValidationError("error", "FILE_NOT_FOUND", f"Fichier introuvable: {bim_path}")]

    try:
        bim = json.loads(bim_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [ValidationError("error", "INVALID_JSON", f"JSON invalide: {e}")]

    errors: list[ValidationError] = []

    # Structure BIM attendue
    model = bim.get("model", {})
    tables = model.get("tables", [])
    relationships = model.get("relationships", [])

    # 1. Noms de tables dupliqués
    errors.extend(_check_duplicate_tables(tables))

    # 2. Construire l'index des colonnes
    col_index = _build_column_index(tables)
    errors.extend(_check_duplicate_columns(tables))

    # 3. Valider les relations
    errors.extend(_check_relationships(relationships, col_index))

    # 4. Valider les mesures et colonnes calculées
    errors.extend(_check_measures_and_calc_cols(tables, col_index))

    # 5. Champs requis
    errors.extend(_check_required_fields(bim, tables))

    return errors


def _build_column_index(tables: list) -> dict[str, dict[str, Any]]:
    """Construit un index {table: {col: col_def}}."""
    index: dict[str, dict[str, Any]] = {}
    for table in tables:
        name = table.get("name")
        if not name:
            continue
        index[name] = {}
        for col in table.get("columns", []):
            col_name = col.get("name")
            if col_name:
                index[name][col_name] = col
    return index


def _check_duplicate_tables(tables: list) -> list[ValidationError]:
    """Vérifie les noms de tables dupliqués."""
    seen: dict[str, int] = {}
    for table in tables:
        name = table.get("name", "")
        if name:
            seen[name] = seen.get(name, 0) + 1

    errors: list[ValidationError] = []
    for name, count in seen.items():
        if count > 1:
            errors.append(ValidationError(
                "error",
                "DUPLICATE_TABLE",
                f"Table dupliquée ({count} occurrences)",
                name
            ))
    return errors


def _check_duplicate_columns(tables: list) -> list[ValidationError]:
    """Vérifie les colonnes dupliquées dans chaque table."""
    errors: list[ValidationError] = []
    for table in tables:
        table_name = table.get("name", "?")
        seen: dict[str, int] = {}
        for col in table.get("columns", []):
            col_name = col.get("name", "")
            if col_name:
                seen[col_name] = seen.get(col_name, 0) + 1

        for col_name, count in seen.items():
            if count > 1:
                errors.append(ValidationError(
                    "error",
                    "DUPLICATE_COLUMN",
                    f"Colonne dupliquée ({count} occurrences)",
                    f"{table_name}[{col_name}]"
                ))
    return errors


def _check_relationships(rels: list, col_index: dict) -> list[ValidationError]:
    """Vérifie que les relations référencent des tables/colonnes existantes."""
    errors: list[ValidationError] = []
    seen: set = set()

    for i, rel in enumerate(rels):
        ft = rel.get("fromTable")
        fc = rel.get("fromColumn")
        tt = rel.get("toTable")
        tc = rel.get("toColumn")

        # Clé unique pour détecter les relations dupliquées
        key = (ft, fc, tt, tc)
        if key in seen:
            errors.append(ValidationError(
                "warning",
                "DUPLICATE_RELATIONSHIP",
                "Relation dupliquée",
                f"{ft}[{fc}] -> {tt}[{tc}]"
            ))
            continue
        seen.add(key)

        # Vérifier que les tables existent
        if ft not in col_index:
            errors.append(ValidationError(
                "error",
                "RELATION_FROM_TABLE_MISSING",
                f"Table source introuvable",
                f"{ft}[{fc}]"
            ))

        if tt not in col_index:
            errors.append(ValidationError(
                "error",
                "RELATION_TO_TABLE_MISSING",
                f"Table destination introuvable",
                f"{tt}[{tc}]"
            ))

        # Vérifier que les colonnes existent
        if ft in col_index and fc and fc not in col_index[ft]:
            errors.append(ValidationError(
                "error",
                "RELATION_FROM_COLUMN_MISSING",
                f"Colonne source introuvable",
                f"{ft}[{fc}]"
            ))

        if tt in col_index and tc and tc not in col_index[tt]:
            errors.append(ValidationError(
                "error",
                "RELATION_TO_COLUMN_MISSING",
                f"Colonne destination introuvable",
                f"{tt}[{tc}]"
            ))

    return errors


def _build_measure_index(tables: list) -> dict[str, set[str]]:
    """Build {table_name: {measure_names}} index."""
    index: dict[str, set[str]] = {}
    for table in tables:
        name = table.get("name")
        if name:
            index[name] = {m["name"] for m in table.get("measures", []) if m.get("name")}
    return index


def _check_measures_and_calc_cols(tables: list, col_index: dict) -> list[ValidationError]:
    """Vérifie les mesures DAX et colonnes calculées."""
    errors: list[ValidationError] = []
    measure_index = _build_measure_index(tables)

    for table in tables:
        table_name = table.get("name", "?")

        # Mesures
        for measure in table.get("measures", []):
            m_name = measure.get("name", "")
            expr = measure.get("expression", "")

            if not m_name:
                errors.append(ValidationError(
                    "warning",
                    "MEASURE_NO_NAME",
                    "Mesure sans nom",
                    table_name
                ))
                continue

            if not expr:
                errors.append(ValidationError(
                    "warning",
                    "MEASURE_NO_EXPRESSION",
                    "Mesure sans expression",
                    f"{table_name}[{m_name}]"
                ))
                continue

            # Vérifications syntaxiques basiques DAX
            errors.extend(_check_dax_syntax(expr, f"{table_name}[{m_name}]"))

            # Vérifier les références de colonnes
            errors.extend(_check_column_references(expr, table_name, col_index, measure_index))

        # Colonnes calculées
        for col in table.get("columns", []):
            if col.get("isDefaultDataDisplay", False) is False:  # Calculated column
                c_name = col.get("name", "")
                expr = col.get("expression", "")
                if expr:
                    errors.extend(_check_dax_syntax(expr, f"{table_name}[{c_name}]"))
                    errors.extend(_check_column_references(expr, table_name, col_index, measure_index))

    return errors


def _check_dax_syntax(expr: str, location: str) -> list[ValidationError]:
    """Vérifications syntaxiques basiques DAX."""
    errors: list[ValidationError] = []

    # Compter les parenthèses
    if expr.count("(") != expr.count(")"):
        errors.append(ValidationError(
            "error",
            "DAX_UNBALANCED_PARENS",
            "Parenthèses non équilibrées",
            location
        ))

    # Détecter les crochets non fermés
    if expr.count("[") != expr.count("]"):
        errors.append(ValidationError(
            "error",
            "DAX_UNBALANCED_BRACKETS",
            "Crochets non équilibrés",
            location
        ))

    # Détecter les guillemets non fermés
    single_quotes = [m.start() for m in re.finditer(r"(?<!')'(?!')", expr)]
    if len(single_quotes) % 2 != 0:
        errors.append(ValidationError(
            "warning",
            "DAX_UNBALANCED_QUOTES",
            "Guillemets simples possiblement non équilibrés",
            location
        ))

    return errors


def _check_column_references(
    expr: str,
    context_table: str,
    col_index: dict,
    measure_index: dict | None = None,
) -> list[ValidationError]:
    """Vérifie que les colonnes référencées dans l'expression existent."""
    errors: list[ValidationError] = []
    measure_index = measure_index or {}

    # Pattern pour détecter les références de colonnes: table[col] ou [col]
    refs = re.findall(r'(\w+)\[(\w+)\]', expr)

    for table_name, col_name in refs:
        if table_name not in col_index:
            errors.append(ValidationError(
                "warning",
                "DAX_TABLE_NOT_FOUND",
                f"Table référencée introuvable",
                f"{context_table}: {table_name}[{col_name}]"
            ))
            continue

        # col could be a measure in that table — not an error
        if col_name not in col_index[table_name] and col_name not in measure_index.get(table_name, set()):
            errors.append(ValidationError(
                "warning",
                "DAX_COLUMN_NOT_FOUND",
                f"Colonne référencée introuvable",
                f"{table_name}[{col_name}]"
            ))

    # Références implicites [col] (colonne dans la table courante).
    # Exclude [col] preceded by ' (already covered as qualified ref above).
    implicit = re.findall(r"(?<![\w'])\[(\w+)\]", expr)
    all_known = col_index.get(context_table, {})
    all_known_measures = measure_index.get(context_table, set())
    for col_name in implicit:
        if col_name not in all_known and col_name not in all_known_measures:
            errors.append(ValidationError(
                "warning",
                "DAX_IMPLICIT_COLUMN_NOT_FOUND",
                f"Colonne implicite introuvable dans table courante",
                f"{context_table}[{col_name}]"
            ))

    return errors


def _check_required_fields(bim: dict, tables: list) -> list[ValidationError]:
    """Vérifie la présence des champs requis."""
    errors: list[ValidationError] = []

    # Champs au niveau racine
    if "name" not in bim:
        errors.append(ValidationError("error", "MISSING_BIM_NAME", "Champ 'name' manquant"))
    if "model" not in bim:
        errors.append(ValidationError("error", "MISSING_MODEL", "Champ 'model' manquant"))

    # Champs requis pour chaque table
    for table in tables:
        name = table.get("name", "?")
        if "name" not in table:
            errors.append(ValidationError("error", "MISSING_TABLE_NAME", "Table sans nom"))

        # Vérifier columns ou partitions
        has_cols = "columns" in table
        has_parts = "partitions" in table
        if not has_cols and not has_parts:
            errors.append(ValidationError(
                "warning",
                "TABLE_EMPTY",
                "Table sans colonnes ni partitions",
                name
            ))

    return errors


def print_validation_report(errors: list[ValidationError]) -> int:
    """Affiche le rapport de validation et retourne le code de sortie.

    Returns:
        0 si aucun error, 1 si au moins un error
    """
    if not errors:
        print("OK - Validation OK - pas d'erreurs detectees")
        return 0

    error_count = sum(1 for e in errors if e.level == "error")
    warning_count = sum(1 for e in errors if e.level == "warning")

    print(f"\nKO - Validation terminee: {error_count} erreurs, {warning_count} warnings\n")

    # Group by level
    errors_first = [e for e in errors if e.level == "error"]
    warnings = [e for e in errors if e.level == "warning"]

    for e in errors_first:
        print(f"  {e}")

    if warnings and errors_first:
        print("\n  Warnings:")

    for e in warnings:
        print(f"  {e}")

    return 1 if error_count > 0 else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Validateur model.bim")
    parser.add_argument("bim", help="Chemin vers model.bim")
    args = parser.parse_args()

    bim_path = pathlib.Path(args.bim)
    errors = validate_model(bim_path)
    exit_code = print_validation_report(errors)

    # Indiquer si Power BI risque de rejeter le modele
    if any(e.level == "error" for e in errors):
        print("\nWARNING - Erreurs bloquantes detectees - Power BI rejettera probablement ce modele")
    else:
        print("\nOK - Modele valide pour ouverture Power BI Desktop")

    exit(exit_code)


if __name__ == "__main__":
    main()
