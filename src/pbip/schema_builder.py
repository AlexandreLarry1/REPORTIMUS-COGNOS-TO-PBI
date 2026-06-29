"""Étape 2b : inférence LLM du schéma sémantique (relations + types de colonnes).

Modes :
  paste      Génère le prompt → attend que l'utilisateur colle le JSON dans intermediate/schema_response.json
  api        Génère le prompt → appelle Azure OpenAI → écrit schema_response.json
  skip-llm   Saute le LLM, schema_response.json existant utilisé tel quel
"""
import argparse
import json
import os
import pathlib
import re
import sys

from dotenv import load_dotenv
load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))  # src/
import observability as obs

_SCRIPT_MAX_CHARS = 12_000

_SYSTEM = """\
Tu es un expert Power BI et modélisation de données.
Analyse le script Qlik et les tables SQL produites.
Retourne UNIQUEMENT un objet JSON valide, sans markdown, sans commentaires.
L'objet doit avoir exactement deux clés :
  "relationships" : liste de {fromTable, fromColumn, toTable, toColumn}
  "column_types_overrides" : liste de {table, column, dataType}
dataType valides : "string", "int64", "double", "dateTime"

Règles strictes pour les relations :
- Ne crée une relation que si la colonne existe réellement dans la table source.
- Si un chemin indirect A→B→C existe déjà, ne crée PAS la relation directe A→C : Power BI interdit les chemins ambigus entre deux tables.
- Chaque paire (fromTable, toTable) ne doit apparaître qu'une seule fois.

Ne retourne rien d'autre que le JSON."""


def _build_prompt(extraction: dict, etl_context: dict) -> tuple[str, str]:
    script = extraction.get("script", "")
    variables = extraction.get("variables", {})

    if isinstance(variables, dict):
        var_lines = "\n".join(f"  {k} = {v}" for k, v in variables.items()) if variables else "  (aucune)"
    else:
        var_lines = "\n".join(f"  {v['name']} = {v.get('definition', '')}" for v in variables) if variables else "  (aucune)"

    truncated = ""
    if len(script) > _SCRIPT_MAX_CHARS:
        script = script[:_SCRIPT_MAX_CHARS]
        truncated = "\n[...tronqué]"

    tables_text = ""
    for tbl in etl_context.get("tables", []):
        cols = ", ".join(c["name"] for c in tbl.get("columns", []))
        tables_text += f"  {tbl['name']} ({tbl.get('row_count', '?')} lignes) : {cols}\n"

    user = (
        f"## Variables Qlik\n{var_lines}\n\n"
        f"## Script Qlik\n{script}{truncated}\n\n"
        f"## Tables disponibles (produites par le SQL)\n{tables_text.strip()}"
    )
    return _SYSTEM, user


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
        name="schema_inference",
        model=model,
        input=messages,
    )

    print("Appel Azure OpenAI (inférence schéma)…")
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4096,
    )
    result = response.choices[0].message.content
    gen.end(output=result)
    return result


def _parse_response(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    data = json.loads(cleaned.strip())
    if "relationships" not in data or "column_types_overrides" not in data:
        raise ValueError("Réponse LLM manque 'relationships' ou 'column_types_overrides'")
    return data


def build(example: str, mode: str, trace=None) -> None:
    inter = ROOT / "examples" / example / "intermediate"
    extraction_path  = inter / "extraction.json"
    etl_context_path = inter / "sql" / "etl_context.json"
    prompt_path      = inter / "schema_prompt.txt"
    out_path         = inter / "schema_response.json"

    if mode == "skip-llm":
        if out_path.exists():
            print("Mode skip-llm : schema_response.json existant utilisé.")
        else:
            print("Mode skip-llm : schema_response.json absent, pipeline continue sans schéma LLM.")
        return

    extraction  = json.loads(extraction_path.read_text(encoding="utf-8"))
    etl_context = json.loads(etl_context_path.read_text(encoding="utf-8"))

    system, user = _build_prompt(extraction, etl_context)
    prompt_path.write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
    print(f"Prompt → {prompt_path}")

    if mode == "paste":
        print("\n" + "=" * 60)
        print("ÉTAPE MANUELLE")
        print(f"  1. Ouvrez {prompt_path}")
        print("  2. Copiez le contenu dans votre chat LLM")
        print(f"  3. Collez la réponse JSON dans {out_path}")
        print("=" * 60)
        input("\nAppuyez sur Entrée une fois schema_response.json créé…")
        if not out_path.exists():
            print("schema_response.json introuvable. Abandon.")
            sys.exit(1)
        return

    # mode == "api"
    raw  = _call_api(system, user, trace=trace)
    data = _parse_response(raw)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Schema → {out_path}  ({len(data['relationships'])} relations, {len(data['column_types_overrides'])} overrides)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inférence LLM du schéma sémantique")
    parser.add_argument("--mode", choices=["paste", "api", "skip-llm"], default="paste")
    args = parser.parse_args()

    example = os.getenv("EXAMPLE_NAME", "")
    if not example:
        print("EXAMPLE_NAME non défini dans .env. Abandon.")
        sys.exit(1)

    with obs.trace("schema_build_run", example=example, mode=args.mode) as trace:
        build(example=example, mode=args.mode, trace=trace)


if __name__ == "__main__":
    main()
