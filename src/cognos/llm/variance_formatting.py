"""LLM Call 3: Variance & Formatting Rules.

Calcule les variances (Actual - Budget, % variance) et génère des mesures DAX
qui retournent des codes HEX pour les couleurs conditionnelles dynamiques.

Input:
- Noms des mesures générées au Call 2
- Règles de formatage conditionnel XML

Output:
- JSON avec mesures de variance + mesures HEX pour couleurs
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
Ta tâche est de:
1. Créer des mesures de variance (différences, pourcentages)
2. Créer des mesures DAX qui retournent des codes HEX pour les couleurs conditionnelles

Dans Cognos:
- Les variances sont calculées: [Actual] - [Budget], [Variance] / [Budget]
- Les couleurs conditionnelles sont basées sur les valeurs des comptes

Dans Power BI:
- Les calculs de variance utilisent DIVIDE pour éviter les divisions par zéro
- Les couleurs dynamiques sont des mesures qui retournent des codes HEX
- Format: "#RRGGBB"

Retourne UNIQUEMENT un objet JSON valide, sans markdown, sans commentaires.
L'objet doit avoir cette structure:
{
  "variance_measures": [
    {"name": "<nom>", "expression": "<DAX>", "description": "<description>"}
  ],
  "color_measures": [
    {"name": "<nom>", "expression": "<DAX>", "description": "<description>", "target_field": "<champ>"}
  ]
}

Exemples:

Variance basique:
{
  "name": "Variance",
  "expression": "[Actual] - [Budget]",
  "description": "Différence Actual - Budget"
}

Variance %:
{
  "name": "Variance_Pct",
  "expression": "DIVIDE([Variance], [Budget], 0)",
  "description": "Pourcentage de variance"
}

Couleur conditionnelle:
{
  "name": "Variance_Color",
  "expression": "IF([Variance_Pct] >= 0, \"#25CAC8\", \"#FF6168\")",
  "description": "Vert si positif, rouge si négatif",
  "target_field": "Variance_Pct"
}

Couleur multi-conditions:
{
  "name": "Account_Color",
  "expression": "SWITCH([Account_Code], \"6599\", \"#25CAC8\", \"6499\", \"#DDB00E\", \"#FFFFFF\")",
  "description": "Couleur par compte",
  "target_field": "Account_Code"
}

Règles:
1. Utiliser DIVIDE(numerator, denominator, alternate_result) pour les pourcentages
2. Les couleurs HEX sont entre guillemets doubles
3. IF(condition, true_value, false_value) pour les conditions binaires
4. SWITCH pour les mappings valeur -> couleur
"""


def build_prompt(xml_data: dict, param_logic: dict) -> tuple[str, str]:
    """Construit le prompt pour l'appel LLM Variance & Formatting.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()
        param_logic: Dict produit par parameter_logic.py

    Returns:
        (system_prompt, user_prompt)
    """
    lines = ["## Mesures disponibles (du Call 2 - Parameter Logic):\n"]

    # Mesures existantes
    for measure in param_logic.get("measures", []):
        lines.append(f"- [{measure['name']}]: {measure.get('description', '')}")

    # Expressions de variance détectées
    lines.append("\n## Expressions Cognos avec calculs détectés:\n")

    for query_name, query in xml_data.get("queries", {}).items():
        for item in query.get("dataItems", []):
            expr = item.get("expression", "")
            name = item.get("name", "")

            if expr and any(op in expr.upper() for op in ("-", "/", "DIVIDE")):
                lines.append(f"\n{name}:")
                lines.append(f"  Expression: {expr}")

    # Styles conditionnels (couleurs)
    lines.append("\n## Styles conditionnels (couleurs) détectés:\n")

    for style_name, style_data in xml_data.get("namedStyles", {}).items():
        if style_data.get("type") == "advanced":
            lines.append(f"\nStyle: {style_name}")
            for case in style_data.get("cases", []):
                condition = case.get("condition", "")
                style = case.get("style", {})
                bg = style.get("backgroundColor", "")
                if bg:
                    lines.append(f"  IF {condition} THEN background={bg}")

    user_prompt = "\n".join(lines) + "\n\nGénère les mesures de variance et les mesures de couleurs HEX."

    return _SYSTEM_PROMPT, user_prompt


def parse_response(raw: str) -> dict:
    """Parse la réponse LLM et extrait le JSON.

    Args:
        raw: Réponse brute de l'LLM

    Returns:
        Dict avec les mesures de variance et couleur
    """
    # Nettoyer le markdown
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)

    try:
        data = json.loads(cleaned.strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"Réponse LLM JSON invalide: {e}") from e

    # Valider la structure
    if "variance_measures" not in data and "color_measures" not in data:
        raise ValueError("Réponse LLM manque 'variance_measures' ou 'color_measures'")

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
        name="variance_formatting_llm",
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


def run(xml_data: dict, param_logic_path: pathlib.Path, output_path: pathlib.Path,
         mode: str = "api", trace=None) -> dict:
    """Exécute le Call LLM 3: Variance & Formatting.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()
        param_logic_path: Chemin vers parameter_logic.json
        output_path: Chemin où écrire variance_formatting.json
        mode: "api" ou "paste"
        trace: Observability trace

    Returns:
        Dict avec les mesures générées
    """
    # Charger les résultats du Call 2
    param_logic = json.loads(param_logic_path.read_text(encoding="utf-8"))

    system, user = build_prompt(xml_data, param_logic)

    if mode == "paste":
        # Écrire le prompt pour collage manuel
        prompt_path = output_path.parent / "variance_formatting_prompt.txt"
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

    var_count = len(data.get("variance_measures", []))
    col_count = len(data.get("color_measures", []))
    print(f"Variance & Formatting -> {output_path} ({var_count} variance, {col_count} color measures)")

    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LLM Call 3: Variance & Formatting Rules")
    parser.add_argument("--xml-data", required=True, help="Chemin vers le JSON extrait du XML")
    parser.add_argument("--param-logic", required=True, help="Chemin vers parameter_logic.json")
    parser.add_argument("--output", required=True, help="Chemin de sortie variance_formatting.json")
    parser.add_argument("--mode", choices=["api", "paste"], default="api", help="Mode d'exécution")
    args = parser.parse_args()

    xml_path = pathlib.Path(args.xml_data)
    logic_path = pathlib.Path(args.param_logic)
    out_path = pathlib.Path(args.output)

    if not xml_path.exists():
        print(f"Erreur: fichier introuvable {xml_path}")
        return
    if not logic_path.exists():
        print(f"Erreur: fichier introuvable {logic_path}")
        return

    xml_data = json.loads(xml_path.read_text(encoding="utf-8"))

    with obs.trace("variance_formatting_run") as trace:
        run(xml_data, logic_path, out_path, mode=args.mode, trace=trace)


if __name__ == "__main__":
    main()
