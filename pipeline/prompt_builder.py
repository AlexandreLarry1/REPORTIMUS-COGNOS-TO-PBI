"""Build LLM prompt dynamically from inputs/ CSV headers + extraction/extraction.json."""
import csv
import json
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
INPUTS_DIR  = ROOT / "inputs"
EXTRACTION  = ROOT / "extraction" / "extraction.json"
SQL_DIR     = ROOT / "sql"

_RULES = """\
- MAPPING LOAD              → CREATE OR REPLACE TEMP TABLE
- RESIDENT <table>          → FROM <table>
- ApplyMap('Map', key, def) → COALESCE((SELECT val FROM Map WHERE k=key), def)
- NOT Exists(Key, field)    → NOT EXISTS (SELECT 1 FROM table WHERE col=field)
- Concaténation &           → ||
- left(str, n)              → SUBSTR(str, 1, n)
- right(str, n)             → RIGHT(str, n)
- mid(str, pos)             → SUBSTR(str, pos)
- MAXSTRING()               → MAX()
- wildmatch(f,'a','b')>0   → f IN ('a','b')
- if(cond, a, b)            → CASE WHEN cond THEN a ELSE b END
- Rowno()                   → ROW_NUMBER() OVER ()\
"""

_CONSTRAINTS = """\
- Pas de UPDATE sur TEMP TABLE → recréer avec SELECT
- CREATE OR REPLACE TABLE pour les tables finales (celles dans des STORE ou utilisées hors script)
- CREATE OR REPLACE TEMP TABLE pour les intermédiaires
- Index non supportés sur TEMP TABLE → les omettre
- SUBSTR(str, -n) retourne vide en DuckDB → utiliser RIGHT(str, n)
- Pour les périodes SAP : LPAD(CAST(CAST(PERIO AS INTEGER) AS VARCHAR), 2, '0')\
"""


def _csv_columns(path: pathlib.Path) -> list[str]:
    with open(path, newline="", encoding="utf-8") as f:
        headers = next(csv.reader(f))
    return [h.split(".", 1)[1] if "." in h else h for h in headers]


def _inputs_block() -> str:
    lines = ["Tables disponibles dans DuckDB (chargées depuis inputs/) :"]
    for p in sorted(INPUTS_DIR.glob("*.csv")):
        cols = _csv_columns(p)
        lines.append(f"- {p.stem} : {', '.join(cols)}")
    return "\n".join(lines)


def build_system(inputs_block: str) -> str:
    return f"""\
Tu es un expert en migration Qlik → SQL DuckDB.

{inputs_block}

Règles de traduction strictes :
{_RULES}

Contraintes DuckDB :
{_CONSTRAINTS}

Retourne UNIQUEMENT du SQL valide DuckDB, sans explication, sans markdown.
Chaque bloc séparé par un commentaire -- [NOM_TABLE].
Génère toutes les tables définies dans le script (intermédiaires et finales).
Les tables finales utilisent CREATE OR REPLACE TABLE, les intermédiaires CREATE OR REPLACE TEMP TABLE.\
"""


def build_user(variables: list[dict], script: str) -> str:
    vars_block = "\n".join(f"- {v['name']} = {v['definition']}" for v in variables)
    return f"""\
Voici le script Qlik et les variables résolues.
Génère les requêtes SQL DuckDB pour reproduire exactement toutes les tables du script.

--- VARIABLES ---
{vars_block}

--- SCRIPT QLIK ---
{script}\
"""


def build(write: bool = True) -> tuple[str, str]:
    data = json.loads(EXTRACTION.read_text(encoding="utf-8"))
    inputs_block = _inputs_block()
    system = build_system(inputs_block)
    user   = build_user(data["variables"], data["script"])

    if write:
        SQL_DIR.mkdir(exist_ok=True)
        prompt_path = SQL_DIR / "prompt.txt"
        prompt_path.write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
        print(f"Prompt écrit → {prompt_path}")

    return system, user


if __name__ == "__main__":
    build()
    print("Copiez sql/prompt.txt dans le chat LLM, puis collez le résultat dans sql/generated.sql")
