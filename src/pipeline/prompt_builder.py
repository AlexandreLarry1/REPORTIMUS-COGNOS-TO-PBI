"""Build LLM prompt from examples/<example>/input/ CSVs + intermediate/extraction.json."""
import csv
import json
import pathlib

ROOT = pathlib.Path(__file__).parent.parent.parent

_RULES = (
    "- MAPPING LOAD              -> CREATE OR REPLACE TEMP TABLE\n"
    "- RESIDENT <table>          -> FROM <table>\n"
    "- ApplyMap('Map', key, def) -> COALESCE((SELECT val FROM Map WHERE k=key), def)\n"
    "- NOT Exists(Key, field)    -> NOT EXISTS (SELECT 1 FROM table WHERE col=field)\n"
    "- Concatenation &           -> ||\n"
    "- left(str, n)              -> SUBSTR(str, 1, n)\n"
    "- right(str, n)             -> RIGHT(str, n)\n"
    "- mid(str, pos)             -> SUBSTR(str, pos)\n"
    "- MAXSTRING()               -> MAX()\n"
    "- wildmatch(f,'a','b')>0   -> f IN ('a','b')\n"
    "- if(cond, a, b)            -> CASE WHEN cond THEN a ELSE b END\n"
    "- Rowno()                   -> ROW_NUMBER() OVER ()"
)

_CONSTRAINTS = (
    "- Pas de UPDATE sur TEMP TABLE -> recreer avec SELECT\n"
    "- CREATE OR REPLACE TABLE pour les tables finales\n"
    "- CREATE OR REPLACE TEMP TABLE pour les intermediaires\n"
    "- Index non supportes sur TEMP TABLE -> les omettre\n"
    "- SUBSTR(str, -n) retourne vide en DuckDB -> utiliser RIGHT(str, n)\n"
    "- Pour les periodes SAP : LPAD(CAST(CAST(PERIO AS INTEGER) AS VARCHAR), 2, '0')\n"
    "- Toutes les colonnes sources sont VARCHAR. Toujours caster explicitement avant "
    "toute operation arithmetique ou numerique : CAST(col AS DOUBLE), etc.\n"
    "- Les dates sources sont au format M/D/YYYY (ex: '4/4/2024'). "
    "Les valeurs manquantes sont '-'. Toujours utiliser TRY_STRPTIME (jamais strptime ni CAST AS DATE) : TRY_STRPTIME(col, '%m/%d/%Y').\n"
    "- AddYears(col, n) -> TRY_STRPTIME(col, '%m/%d/%Y') + INTERVAL (n) YEAR\n"
    "- AddMonths(col, n) -> TRY_STRPTIME(col, '%m/%d/%Y') + INTERVAL (n) MONTH\n"
    "- Floor(col) -> FLOOR(CAST(col AS DOUBLE))"
)


def _csv_columns(path: pathlib.Path) -> list[str]:
    with open(path, newline="", encoding="utf-8") as f:
        headers = next(csv.reader(f))
    return [h.split(".", 1)[1] if "." in h else h for h in headers]


def _inputs_block(inputs_dir: pathlib.Path) -> str:
    lines = ["Tables sources disponibles dans DuckDB (suffixe _src, READ-ONLY) :"]
    for p in sorted(inputs_dir.glob("*.csv")):
        cols = _csv_columns(p)
        lines.append(f"- {p.stem}_src : {', '.join(cols)}")
    lines.append("\nUtilise <NomTable>_src comme source dans les FROM/JOIN. "
                 "Cree les tables finales sans suffixe (ex: CREATE OR REPLACE TABLE AUM AS SELECT ... FROM AUM_src).")
    return "\n".join(lines)


def build_system(inputs_block: str) -> str:
    return (
        "Tu es un expert en migration Qlik -> SQL DuckDB.\n\n"
        f"{inputs_block}\n\n"
        "IMPORTANT : les tables sources (_src) contiennent deja les valeurs finales "
        "telles qu'affichees dans Qlik (post-transformation : dates formatees, calculs appliques, etc.). "
        "NE PAS re-appliquer les transformations du script (AddYears, Floor, Date(), etc.) sur les colonnes sources. "
        "Pour chaque table du script qui charge depuis un QVD, faire simplement "
        "CREATE OR REPLACE TABLE <Nom> AS SELECT <colonnes> FROM <Nom>_src. "
        "Appliquer les transformations UNIQUEMENT pour les tables derivees qui combinent plusieurs sources (RESIDENT, Mapping, jointures).\n\n"
        f"Regles de traduction strictes :\n{_RULES}\n\n"
        f"Contraintes DuckDB :\n{_CONSTRAINTS}\n\n"
        "Retourne UNIQUEMENT du SQL valide DuckDB, sans explication, sans markdown.\n"
        "Chaque bloc separe par un commentaire -- [NOM_TABLE].\n"
        "Genere toutes les tables definies dans le script (intermediaires et finales).\n"
        "Les tables finales utilisent CREATE OR REPLACE TABLE, "
        "les intermediaires CREATE OR REPLACE TEMP TABLE."
    )


def build_user(variables: list[dict], script: str) -> str:
    vars_block = "\n".join(f"- {v['name']} = {v['definition']}" for v in variables)
    return (
        "Voici le script Qlik et les variables resolues.\n"
        "Genere les requetes SQL DuckDB pour reproduire exactement toutes les tables du script.\n\n"
        f"--- VARIABLES ---\n{vars_block}\n\n"
        f"--- SCRIPT QLIK ---\n{script}"
    )


def build(example: str, write: bool = True) -> tuple[str, str]:
    example_dir  = ROOT / "examples" / example
    inputs_dir   = example_dir / "input"
    intermediate = example_dir / "intermediate"
    sql_dir      = intermediate / "sql"

    data = json.loads((intermediate / "extraction.json").read_text(encoding="utf-8"))
    inputs_block = _inputs_block(inputs_dir)
    system = build_system(inputs_block)
    user   = build_user(data["variables"], data["script"])

    if write:
        sql_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = sql_dir / "prompt.txt"
        prompt_path.write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
        print(f"Prompt ecrit -> {prompt_path}")

    return system, user


if __name__ == "__main__":
    import argparse
    import os
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--example", default=os.getenv("EXAMPLE_NAME", ""))
    args = p.parse_args()
    build(example=args.example)
