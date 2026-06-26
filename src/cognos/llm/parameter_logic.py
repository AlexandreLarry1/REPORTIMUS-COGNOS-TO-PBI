"""LLM Call 2: Parameter Switching Logic.

Traduit la logique de paramètres dynamiques Cognos (CASE WHEN ?param?)
vers des mesures DAX utilisant SWITCH(TRUE(), SELECTEDVALUE(...)).

Input:
- Expressions Cognos avec CASE WHEN ?p_timeview? + résultat Call 1
- Paramètres Cognos

Output:
- JSON avec mesures DAX utilisant SWITCH pour logique de paramètres
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
Tu es un expert en migration Cognos vers Power BI.
Ta tâche est de traduire les expressions Cognos CASE WHEN avec paramètres
en mesures DAX utilisant SWITCH et SELECTEDVALUE.

Dans Cognos:
- Les paramètres sont référencés comme ?param_name?
- CASE WHEN ?p_timeview? contains 'MTD' THEN [MTD] ...

Dans Power BI:
- Les paramètres sont gérés par des tables déconnectées (ex: TimeView[View])
- Utiliser SWITCH(TRUE(), SELECTEDVALUE(...), value, ...)
- Les valeurs sont des chaînes entre guillemets

Retourne UNIQUEMENT un objet JSON valide, sans markdown, sans commentaires.
L'objet doit avoir cette structure:
{
  "measures": [
    {"name": "<nom>", "expression": "<DAX>", "description": "<description>", "source_field": "<champ>"}
  ]
}

Exemples de traduction:

Cognos:
CASE WHEN ?p_timeview? contains 'MTD' THEN [MTD]
WHEN ?p_timeview? contains 'QTD' THEN [QTD]
WHEN ?p_timeview? contains 'YTD' THEN [YTD]
ELSE [Value]
END

-> DAX:
SWITCH(
    SELECTEDVALUE('TimeView'[View], "Full"),
    "MTD", [MTD],
    "QTD", [QTD],
    "YTD", [YTD],
    "Full", [Value]
)

Cognos:
CASE WHEN ParamDisplayValue('Year') = '2022' THEN [2022]
WHEN ParamDisplayValue('Year') = '2023' THEN [2023]
ELSE [Total]
END

-> DAX:
SWITCH(
    SELECTEDVALUE('Years'[Year], "All"),
    "2022", [Value_2022],
    "2023", [Value_2023],
    "All", [Total]
)

Règles importantes:
1. SELECTEDVALUE utilise la table paramètre et sa colonne de valeurs
2. La deuxième valeur de SELECTEDVALUE est la valeur par défaut
3. Les valeurs de comparaison sont des chaînes entre guillemets
4. Utiliser les noms de tables et colonnes exactement comme fournis
"""


def build_prompt(xml_data: dict, time_intel: dict) -> tuple[str, str]:
    """Construit le prompt pour l'appel LLM Parameter Logic.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()
        time_intel: Dict produit par time_intelligence.py

    Returns:
        (system_prompt, user_prompt)
    """
    lines = ["## Paramètres Cognos:\n"]

    # Paramètres
    for param in xml_data.get("parameters", []):
        name = param.get("name", "")
        default = param.get("defaultValue", "")
        options = [opt.get("value", "") for opt in param.get("options", [])]

        lines.append(f"Paramètre: {name}")
        lines.append(f"  Défaut: {default}")
        lines.append(f"  Options: {', '.join(options) if options else '(auto)'}")
        lines.append("")

    # Mesures de time intelligence disponibles
    lines.append("\n## Mesures Time Intelligence disponibles:\n")
    for measure in time_intel.get("measures", []):
        lines.append(f"- [{measure['name']}]: {measure.get('description', '')}")

    # Expressions à traduire
    lines.append("\n## Expressions Cognos à traduire:\n")

    for query_name, query in xml_data.get("queries", {}).items():
        for expr in query.get("expressions", []):
            expr_text = expr.get("expression", "")
            expr_name = expr.get("name", "")

            if expr_text and ("CASE" in expr_text.upper() or "?" in expr_text):
                lines.append(f"\nExpression: {expr_name}")
                lines.append(f"```cognos")
                lines.append(expr_text)
                lines.append(f"```")

    user_prompt = "\n".join(lines) + "\n\nTraduis ces expressions en mesures DAX avec SWITCH."

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
        name="parameter_logic_llm",
        model=model,
        input=messages,
    )

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4096,
    )
    result = response.choices[0].message.content
    gen.end(output=result)
    return result


def run(xml_data: dict, time_intel_path: pathlib.Path, output_path: pathlib.Path,
         mode: str = "api", trace=None) -> dict:
    """Exécute le Call LLM 2: Parameter Switching Logic.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()
        time_intel_path: Chemin vers time_intelligence.json
        output_path: Chemin où écrire parameter_logic.json
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec les mesures générées
    """
    # Charger les résultats du Call 1
    time_intel = json.loads(time_intel_path.read_text(encoding="utf-8"))

    system, user = build_prompt(xml_data, time_intel)

    if mode == "paste":
        # Écrire le prompt pour collage manuel
        prompt_path = output_path.parent / "parameter_logic_prompt.txt"
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
    print(f"Parameter Logic -> {output_path} ({len(data['measures'])} measures)")

    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LLM Call 2: Parameter Switching Logic")
    parser.add_argument("--xml-data", required=True, help="Chemin vers le JSON extrait du XML (xml_parser output)")
    parser.add_argument("--time-intel", required=True, help="Chemin vers time_intelligence.json")
    parser.add_argument("--output", required=True, help="Chemin de sortie parameter_logic.json")
    parser.add_argument("--mode", choices=["api", "paste"], default="api", help="Mode d'exécution")
    args = parser.parse_args()

    xml_path = pathlib.Path(args.xml_data)
    intel_path = pathlib.Path(args.time_intel)
    out_path = pathlib.Path(args.output)

    if not xml_path.exists():
        print(f"Erreur: fichier introuvable {xml_path}")
        return
    if not intel_path.exists():
        print(f"Erreur: fichier introuvable {intel_path}")
        return

    xml_data = json.loads(xml_path.read_text(encoding="utf-8"))

    with obs.trace("parameter_logic_run") as trace:
        run(xml_data, intel_path, out_path, mode=args.mode, trace=trace)


if __name__ == "__main__":
    main()
