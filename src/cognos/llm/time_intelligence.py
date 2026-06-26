"""LLM Call 1: Time Intelligence Foundation.

Génère les mesures DAX de base pour l'intelligence temporelle:
- MTD (Month-to-Date)
- QTD (Quarter-to-Date)
- YTD (Year-to-Date)
- Prior_MTD, Prior_QTD, Prior_YTD (périodes précédentes)

Input: Schéma CSV avec colonnes dates et valeurs
Output: JSON avec mesures DAX
"""
import json
import os
import pathlib
import re
import sys
from dotenv import load_dotenv

load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import observability as obs


_SYSTEM_PROMPT = """\
Tu es un expert Power BI et modélisation de données.
Analyse le schéma des fichiers CSV bruts et génère les mesures DAX de base pour l'intelligence temporelle.

Le fichier contient des données transactionnelles non agrégées (une ligne par date/entité).
Tu dois créer des mesures DAX qui agrègent ces données selon différentes périodes temporelles.

Retourne UNIQUEMENT un objet JSON valide, sans markdown, sans commentaires.
L'objet doit avoir cette structure:
{
  "measures": [
    {"name": "<nom>", "expression": "<DAX>", "description": "<description>"}
  ]
}

Règles pour les expressions DAX:
1. Utiliser CALCULATE avec les fonctions DATESMTD, DATESQTD, DATESYTD
2. Pour Prior_*, utiliser SAMEPERIODLASTYEAR ou DATEADD
3. La mesure d'agrégation de base est SUM([Value]) où Value est la colonne de valeurs
4. Adapter les noms de tables et colonnes exactement comme fournis

Exemple de sortie attendue:
{
  "measures": [
    {"name": "MTD", "expression": "CALCULATE(SUM(fact_data[Value]), DATESMTD(dim_time[Date]))", "description": "Month-to-Date"},
    {"name": "QTD", "expression": "CALCULATE(SUM(fact_data[Value]), DATESQTD(dim_time[Date]))", "description": "Quarter-to-Date"},
    {"name": "YTD", "expression": "CALCULATE(SUM(fact_data[Value]), DATESYTD(dim_time[Date]))", "description": "Year-to-Date"},
    {"name": "Prior_MTD", "expression": "CALCULATE([MTD], SAMEPERIODLASTYEAR(dim_time[Date]))", "description": "Month-to-Date période précédente"},
    {"name": "Prior_QTD", "expression": "CALCULATE([QTD], SAMEPERIODLASTYEAR(dim_time[Date]))", "description": "Quarter-to-Date période précédente"},
    {"name": "Prior_YTD", "expression": "CALCULATE([YTD], SAMEPERIODLASTYEAR(dim_time[Date]))", "description": "Year-to-Date période précédente"}
  ]
}
"""


def build_prompt(etl_context: dict) -> tuple[str, str]:
    """Construit le prompt pour l'appel LLM Time Intelligence.

    Args:
        etl_context: Dict avec tables CSV (nom, colonnes, types)

    Returns:
        (system_prompt, user_prompt)
    """
    lines = ["## Tables CSV disponibles (schéma):\n"]

    for table in etl_context.get("tables", []):
        cols = []
        date_col = None
        value_cols = []

        for col in table.get("columns", []):
            col_name = col["name"]
            col_type = col["type"]
            cols.append(f"  {col_name} ({col_type})")

            if col_type in ("dateTime", "date", "datetime"):
                date_col = col_name
            elif col_type in ("double", "int64", "decimal", "int"):
                value_cols.append(col_name)

        if cols:
            lines.append(f"Table: {table['name']}")
            lines.extend(cols)
            if date_col:
                lines.append(f"  >> Colonne date identifiée: {date_col}")
            if value_cols:
                lines.append(f"  >> Colonnes de valeur: {', '.join(value_cols)}")
            lines.append("")

    user_prompt = "\n".join(lines) + "\nGénère les mesures DAX de base pour l'intelligence temporelle."

    return _SYSTEM_PROMPT, user_prompt


def parse_response(raw: str) -> dict:
    """Parse la réponse LLM et extrait le JSON.

    Args:
        raw: Réponse brute de l'LLM

    Returns:
        Dict avec les mesures DAX
    """
    # Nettoyer le markdown
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)

    try:
        data = json.loads(cleaned.strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"Réponse LLM JSON invalide: {e}") from e

    if "measures" not in data:
        raise ValueError("Réponse LLM manque la clé 'measures'")

    return data


def call_api(system: str, user: str, trace=None) -> str:
    """Appelle l'API OpenAI pour générer les mesures.

    Args:
        system: Prompt système
        user: Prompt utilisateur
        trace: Observability trace (optionnel)

    Returns:
        Réponse brute de l'LLM
    """
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
        name="time_intelligence_llm",
        model=model,
        input=messages,
    )

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=2048,
    )
    result = response.choices[0].message.content
    gen.end(output=result)
    return result


def run(etl_context: dict, output_path: pathlib.Path, mode: str = "api", trace=None) -> dict:
    """Exécute le Call LLM 1: Time Intelligence.

    Args:
        etl_context: Dict avec schéma CSV (depuis etl_context.json)
        output_path: Chemin où écrire time_intelligence.json
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec les mesures générées
    """
    system, user = build_prompt(etl_context)

    if mode == "paste":
        # Écrire le prompt pour collage manuel
        prompt_path = output_path.parent / "time_intelligence_prompt.txt"
        prompt_path.write_text(f"SYSTEM:\n{system}\n\nUSER:\n{user}", encoding="utf-8")
        print(f"Prompt écrit -> {prompt_path}")
        print("\nCopiez dans votre LLM et collez la réponse JSON dans:")
        print(f"  {output_path}")
        input("\nAppuyez sur Entrée une fois le fichier créé...")

        if not output_path.exists():
            raise FileNotFoundError(f"Fichier introuvable: {output_path}")

        return json.loads(output_path.read_text(encoding="utf-8"))

    # Mode API
    raw = call_api(system, user, trace)
    data = parse_response(raw)

    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Time Intelligence -> {output_path} ({len(data['measures'])} measures)")

    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LLM Call 1: Time Intelligence Foundation")
    parser.add_argument("--etl-context", required=True, help="Chemin vers etl_context.json")
    parser.add_argument("--output", required=True, help="Chemin de sortie time_intelligence.json")
    parser.add_argument("--mode", choices=["api", "paste"], default="api", help="Mode d'exécution")
    args = parser.parse_args()

    etl_path = pathlib.Path(args.etl_context)
    out_path = pathlib.Path(args.output)

    if not etl_path.exists():
        print(f"Erreur: fichier introuvable {etl_path}")
        return

    etl_context = json.loads(etl_path.read_text(encoding="utf-8"))

    with obs.trace("time_intelligence_run") as trace:
        run(etl_context, out_path, mode=args.mode, trace=trace)


if __name__ == "__main__":
    main()
