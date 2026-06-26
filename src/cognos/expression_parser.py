"""Analyseur d'expressions Cognos pour classification LLM.

Détecte les patterns dans les expressions Cognos:
- ?param? → paramètre dynamique
- CASE WHEN ?p_timeview? contains 'MTD' → logique de temps
- [Actual] - [Budget] → calcul de variance
- ParamDisplayValue('Year') → référence de paramètre

Classifie les expressions pour router vers le bon appel LLM:
- time_logic → Call 2 (parameter_logic.py)
- variance → Call 3 (variance_formatting.py)
- simple → traitement direct
"""
import re
from typing import Any


# Patterns pour détecter les références de paramètres
PARAM_PATTERN = re.compile(r'\?(\w+)\?')
CASE_PATTERN = re.compile(r'CASE\s+WHEN(.+?)END', re.IGNORECASE | re.DOTALL)
VARIANCE_PATTERN = re.compile(r'\[?\w+\]?\s*[-+/]\s*\[?\w+\]?', re.IGNORECASE)


def extract_param_references(expression: str) -> list[str]:
    """Extrait les références de paramètres (?param?) d'une expression.

    Args:
        expression: Expression Cognos

    Returns:
        Liste de noms de paramètres trouvés
    """
    if not expression:
        return []
    return list(set(PARAM_PATTERN.findall(expression)))


def classify_expression_type(expression: str) -> str:
    """Classifie une expression par type de logique.

    Args:
        expression: Expression Cognos

    Returns:
        Type: "time_logic", "variance", "simple", "unknown"
    """
    if not expression:
        return "unknown"

    expr_upper = expression.upper()

    # Détection logique de temps (paramètre de vue temporelle)
    if any(p in expr_upper for p in ("?P_TIMEVIEW?", "TIMEVIEW", "MTD", "QTD", "YTD")):
        if "CASE" in expr_upper and "WHEN" in expr_upper:
            return "time_logic"

    # Détection variance (soustraction/division de mesures)
    if VARIANCE_PATTERN.search(expression):
        # Vérifier si c'est vraiment une variance (noms typiques)
        if any(k in expr_upper for k in ("VARIANCE", "ACTUAL", "BUDGET", "PRIOR")):
            return "variance"

    # Expressions avec CASE mais non temporelles
    if "CASE" in expr_upper and "WHEN" in expr_upper:
        params = extract_param_references(expression)
        if params:
            return "time_logic"  # Param switching

    # Par défaut, expression simple
    return "simple"


def extract_case_branches(case_expr: str) -> list[dict]:
    """Extrait les branches d'une expression CASE WHEN.

    Args:
        case_expr: Expression CASE complète

    Returns:
        Liste de {condition, value}
    """
    if not case_expr or "CASE" not in case_expr.upper():
        return []

    branches = []

    # Pattern pour capturer WHEN ... THEN
    when_pattern = re.compile(r'WHEN\s+(.+?)\s+THEN\s+(.+?)(?=\s+WHEN|\s+ELSE|\s+END)', re.IGNORECASE | re.DOTALL)

    for match in when_pattern.finditer(case_expr):
        condition = match.group(1).strip()
        value = match.group(2).strip()
        branches.append({
            "condition": condition,
            "value": value
        })

    # Capturer ELSE si présent
    else_pattern = re.compile(r'ELSE\s+(.+?)\s*END', re.IGNORECASE | re.DOTALL)
    else_match = else_pattern.search(case_expr)
    if else_match:
        branches.append({
            "condition": "ELSE",
            "value": else_match.group(1).strip()
        })

    return branches


def extract_measure_names(expression: str) -> list[str]:
    """Extrait les noms de mesures référencées dans une expression.

    Args:
        expression: Expression Cognos

    Returns:
        Liste de noms de mesures (entre crochets ou identifiants)
    """
    if not expression:
        return []

    measures = []

    # Pattern [MeasureName]
    bracket_pattern = re.compile(r'\[([\w\s]+)\]')
    for match in bracket_pattern.finditer(expression):
        measures.append(match.group(1))

    # Pattern pour les identifiants de dataItem (mots seuls en majuscule souvent)
    # C'est heuristique - on assume que les références de mesures sont en majuscules
    word_pattern = re.compile(r'\b([A-Z][A-Z0-9_]*)\b')
    for match in word_pattern.finditer(expression):
        word = match.group(1)
        # Exclure les mots-clés SQL
        if word not in ("CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL", "TOTAL"):
            if word not in measures:
                measures.append(word)

    return list(set(measures))


def classify_expressions_from_queries(xml_data: dict) -> dict:
    """Classifie toutes les expressions depuis les données XML.

    Args:
        xml_data: Dict produit par xml_parser.parse_cognos_xml()

    Returns:
        Dict par type: {time_logic: [...], variance: [...], simple: [...]}
    """
    result = {
        "time_logic": [],
        "variance": [],
        "simple": [],
        "unknown": []
    }

    for query_name, query in xml_data.get("queries", {}).items():
        for item in query.get("expressions", []):
            expr_text = item.get("expression", "")
            if not expr_text:
                continue

            expr_type = classify_expression_type(expr_text)
            branches = extract_case_branches(expr_text) if "CASE" in expr_text.upper() else []
            params = extract_param_references(expr_text)
            measures = extract_measure_names(expr_text)

            result[expr_type].append({
                "name": item.get("name", ""),
                "query": query_name,
                "type": expr_type,
                "expression": expr_text,
                "branches": branches,
                "parameters": params,
                "measures": measures
            })

    return result


def build_prompt_context(classified: dict, xml_data: dict) -> dict:
    """Construit le contexte pour les prompts LLM.

    Args:
        classified: Dict produit par classify_expressions_from_queries()
        xml_data: Dict produit par xml_parser.parse_cognos_xml()

    Returns:
        Dict avec contexte structuré pour chaque appel LLM
    """
    # Contexte pour Call 1: Time Intelligence
    time_context = {
        "parameters": xml_data.get("parameters", []),
        "time_logic_expressions": classified.get("time_logic", [])
    }

    # Contexte pour Call 2: Parameter Logic (toutes les expressions avec paramètres)
    param_expressions = []
    for expr in classified.get("time_logic", []):
        if expr.get("parameters"):
            param_expressions.append(expr)

    param_context = {
        "parameters": xml_data.get("parameters", []),
        "parameter_expressions": param_expressions
    }

    # Contexte pour Call 3: Variance & Formatting
    variance_context = {
        "variance_expressions": classified.get("variance", []),
        "namedStyles": xml_data.get("namedStyles", {})
    }

    return {
        "time_intelligence": time_context,
        "parameter_logic": param_context,
        "variance_formatting": variance_context
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _test():
    """Tests basiques du parser d'expressions."""
    # Test extraction paramètres
    expr1 = "CASE WHEN ?p_timeview? contains 'MTD' THEN [MTD] WHEN ?p_timeview? contains 'QTD' THEN [QTD] ELSE [Value] END"
    params = extract_param_references(expr1)
    assert params == ["p_timeview"], f"Expected ['p_timeview'], got {params}"

    # Test classification
    assert classify_expression_type(expr1) == "time_logic"
    assert classify_expression_type("[Actual] - [Budget]") == "variance"

    # Test extraction branches
    branches = extract_case_branches(expr1)
    assert len(branches) == 3, f"Expected 3 branches, got {len(branches)}"

    print("Expression parser tests passed!")


if __name__ == "__main__":
    _test()
