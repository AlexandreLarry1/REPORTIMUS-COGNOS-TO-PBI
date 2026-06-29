"""Convertisseur CSS Cognos vers format Power BI JSON.

Parse les attributs CSS de Cognos (<CSS value="...">) et les convertit
en objets JSON compatibles avec le format Power BI.

Mappings:
- border-top:1pt solid black → borderTop: {style: "solid", width: 1, color: "#000000"}
- padding-left:25px → paddingLeft: 25
- background-color:#25CAC8 → backgroundColor: "#25CAC8"
"""
import re
from typing import Any


# Unités CSS vers valeurs numériques
_PT_TO_PX = 4 / 3  # 1pt ≈ 1.33px


def parse_css_string(css: str) -> dict:
    """Parse une chaîne CSS vers dict.

    Args:
        css: Chaîne CSS (ex: "border:1pt solid black;color:red")

    Returns:
        Dict de propriétés
    """
    result = {}
    if not css:
        return result

    # Split par ; en respectant les parenthèses
    parts = re.split(r';(?![^()]*\))', css)

    for part in parts:
        part = part.strip()
        if not part or ':' not in part:
            continue

        prop, val = part.split(':', 1)
        prop = prop.strip().lower()
        val = val.strip()

        if not val:
            continue

        # Mapper vers format Power BI
        mapped = _map_property(prop, val)
        if mapped:
            result.update(mapped)

    return result


def _map_property(prop: str, val: str) -> dict:
    """Map une propriété CSS vers format PBI.

    Returns:
        Dict avec la propriété PBI (vide si non supporté)
    """
    # Propriétés de bordure
    if prop in ("border", "border-top", "border-bottom", "border-left", "border-right"):
        return _parse_border(prop, val)

    # Propriétés de padding
    if prop in ("padding", "padding-left", "padding-right", "padding-top", "padding-bottom"):
        return _parse_padding(prop, val)

    # Propriétés de fond
    if prop == "background-color":
        return {"backgroundColor": _parse_color(val)}

    # Propriété de couleur
    if prop == "color":
        return {"color": _parse_color(val)}

    # Propriétés de police
    if prop == "font-size":
        return {"fontSize": _parse_size(val)}
    if prop == "font-weight":
        return {"fontWeight": val if val in ("bold", "normal") else "normal"}

    # Propriétés d'alignement
    if prop == "text-align":
        align_map = {"left": "Left", "center": "Center", "right": "Right"}
        return {"textAlign": {"expr": {"Literal": {"Value": f"'{align_map.get(val.lower(), val)}'"}}}}
    if prop == "vertical-align":
        v_align_map = {"top": "Top", "middle": "Middle", "bottom": "Bottom"}
        return {"verticalAlign": {"expr": {"Literal": {"Value": f"'{v_align_map.get(val.lower(), val)}'"}}}}

    # Dimensions
    if prop in ("width", "height"):
        return {prop: _parse_size(val)}

    # Display
    if prop == "display":
        return {"display": val}

    return {}


def _parse_border(prop: str, val: str) -> dict:
    """Parse une propriété border CSS.

    border: 1pt solid black → {width: 1.33, style: "solid", color: "#000000"}
    """
    parts = val.split()
    if len(parts) < 2:
        return {}

    width = _parse_size(parts[0]) if parts[0] else 1
    style = parts[1] if len(parts) > 1 else "solid"
    color = _parse_color(parts[2]) if len(parts) > 2 else "#000000"

    # Nom de la propriété PBI
    prop_map = {
        "border": "border",
        "border-top": "borderTop",
        "border-bottom": "borderBottom",
        "border-left": "borderLeft",
        "border-right": "borderRight"
    }
    pbi_prop = prop_map.get(prop.lower(), "border")

    return {
        pbi_prop: {
            "style": style,
            "width": width,
            "color": color
        }
    }


def _parse_padding(prop: str, val: str) -> dict:
    """Parse une propriété padding CSS.

    padding-left: 25px → {paddingLeft: 25}
    """
    size = _parse_size(val)
    if size is None:
        return {}

    prop_map = {
        "padding": "padding",
        "padding-left": "paddingLeft",
        "padding-right": "paddingRight",
        "padding-top": "paddingTop",
        "padding-bottom": "paddingBottom"
    }
    pbi_prop = prop_map.get(prop.lower(), "padding")

    return {pbi_prop: size}


def _parse_color(val: str) -> str:
    """Parse une couleur CSS vers hex.

    Args:
        val: Valeur CSS (black, #25CAC8, rgb(0,0,0))

    Returns:
        Chaîne hex "#RRGGBB"
    """
    val = val.strip().lower()

    # Déjà en hex
    if val.startswith("#"):
        return val.upper()

    # Noms de couleurs basiques
    color_map = {
        "black": "#000000", "white": "#FFFFFF",
        "red": "#FF0000", "green": "#008000", "blue": "#0000FF",
        "yellow": "#FFFF00", "orange": "#FFA500",
        "purple": "#800080", "pink": "#FFC0CB",
        "gray": "#808080", "grey": "#808080",
        "olive": "#808000", "fuchsia": "#FF00FF"
    }

    if val in color_map:
        return color_map[val]

    # rgb(r, g, b)
    rgb_match = re.match(r'rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', val)
    if rgb_match:
        r, g, b = rgb_match.groups()
        return f"#{int(r):02X}{int(g):02X}{int(b):02X}"

    # Valeur inconnue, retourner noir par défaut
    return "#000000"


def _parse_size(val: str) -> int | float | None:
    """Parse une taille CSS vers nombre.

    Args:
        val: "10pt", "25px", "100"

    Returns:
        Taille en pixels (float)
    """
    val = val.strip().lower()

    # Extraire le nombre
    match = re.match(r'([\d.]+)(pt|px|)?', val)
    if not match:
        return None

    num = float(match.group(1))
    unit = match.group(2) or "px"

    if unit == "pt":
        return round(num * _PT_TO_PX)
    return int(num)


def css_to_pbi_format(css_dict: dict) -> dict:
    """Convertit un dict CSS vers format propriétés Power BI.

    Args:
        css_dict: Dict de propriétés CSS (output de parse_css_string)

    Returns:
        Dict format Power BI pour visual properties
    """
    result = {}

    for prop, val in css_dict.items():
        if isinstance(val, dict) and "style" in val:
            # Border
            result[prop] = val
        elif isinstance(val, str) and val.startswith("#"):
            # Color
            result[prop] = {"expr": {"Literal": {"Value": f"'{val}'"}}}
        elif isinstance(val, (int, float)):
            # Size
            result[prop] = {"expr": {"Literal": {"Value": str(val)}}}
        elif isinstance(val, str):
            # String value
            result[prop] = {"expr": {"Literal": {"Value": f"'{val}'"}}}

    return result


def merge_styles(base: dict, override: dict) -> dict:
    """Fusionne deux dicts de styles (override écrase base).

    Args:
        base: Style de base
        override: Style à appliquer par-dessus

    Returns:
        Dict fusionné
    """
    result = base.copy()
    result.update(override)
    return result


# ---------------------------------------------------------------------------
# Tests rapides
# ---------------------------------------------------------------------------

def _test():
    """Tests basiques du parser CSS."""
    tests = [
        ("border-top:1pt solid black", {"borderTop": {"style": "solid", "width": 1, "color": "#000000"}}),
        ("padding-left:25px", {"paddingLeft": 25}),
        ("background-color:#25CAC8", {"backgroundColor": "#25CAC8"}),
        ("color:fuchsia", {"color": "#FF00FF"}),
        ("font-size:10pt", {"fontSize": 13}),
        ("text-align:center", {"textAlign": {"expr": {"Literal": {"Value": "'Center'"}}}}),
    ]

    for css, expected in tests:
        result = parse_css_string(css)
        for key, val in expected.items():
            if key in result:
                if isinstance(val, dict):
                    assert result[key] == val, f"Failed: {css} → {result[key]} != {val}"
                else:
                    assert result[key] == val, f"Failed: {css} → {result[key]} != {val}"
            else:
                assert False, f"Failed: {css} → missing key {key}"

    print("CSS parser tests passed!")


if __name__ == "__main__":
    _test()
