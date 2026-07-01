"""Build a Power BI theme JSON from Cognos CSS color palette.

Extracts hex colors from namedStyles + crosstab styles in the Cognos extraction,
then generates a valid PBI theme JSON (dataColors + visualStyles) and applies it
to the report.json / StaticResources folder of the PBIP output.
"""
import json
import pathlib

_FALLBACK_COLORS = [
    "#0078D4", "#005A9E", "#00B4D8", "#038387",
    "#498205", "#C19C00", "#C50F1F", "#881798",
]

_EXCLUDED = {"#000000", "#FFFFFF", "#000", "#FFF"}


def extract_color_palette(xml_data: dict) -> list[str]:
    """Collect unique hex colors from Cognos namedStyles + crosstab styles.

    Falls back to a modern corporate palette if the Cognos XML has no CSS colors.
    """
    colors: list[str] = []
    seen: set[str] = set()

    def _add(color: str) -> None:
        c = color.upper()
        if len(c) in (7, 4) and c not in seen and c not in _EXCLUDED:
            seen.add(c)
            colors.append(c)

    def _scan_style(style: dict) -> None:
        for key in ("backgroundColor", "color"):
            val = style.get(key, "")
            if val and isinstance(val, str) and val.startswith("#"):
                _add(val)

    for style_def in xml_data.get("namedStyles", {}).values():
        for case in style_def.get("cases", []):
            _scan_style(case.get("style", {}))
        _scan_style(style_def.get("default", {}))
        _scan_style(style_def.get("remaining", {}))

    for crosstab in xml_data.get("crosstabs", []):
        for inter in crosstab.get("intersections", []):
            _scan_style(inter.get("style", {}))
        for dim in crosstab.get("rows", []) + crosstab.get("columns", []):
            for member in dim.get("members", []):
                _scan_style(member.get("style", {}))
        for val in crosstab.get("styles", {}).values():
            if isinstance(val, dict):
                _scan_style(val)

    # Scan vizControl properties (vizPropertyCSSColorValue, vizPropertyPaletteValue)
    for vc in xml_data.get("viz_controls", []):
        for pvalue in vc.get("properties", {}).values():
            if pvalue and isinstance(pvalue, str) and pvalue.startswith("#"):
                _add(pvalue)

    return colors if colors else list(_FALLBACK_COLORS)


def build_pbi_theme(palette: list[str], font: str = "Segoe UI") -> dict:
    """Build a valid PBI theme JSON dict from a color palette."""
    data_colors = (palette * 3)[:8]
    primary = data_colors[0]

    return {
        "name": "CognosMigratedTheme",
        "dataColors": data_colors,
        "background": "#FAFAFA",
        "foreground": "#252525",
        "tableAccent": primary,
        "maximum": "#00B050",
        "minimum": "#FF4444",
        "neutral": "#808080",
        "visualStyles": {
            "*": {
                "*": {
                    "fontSize": [{"value": 10}],
                    "fontFamily": [{"value": font}],
                }
            },
            "matrix": {
                "*": {
                    "columnHeaders": [{"properties": {
                        "fontColor": {"solid": {"color": "#FFFFFF"}},
                        "backColor": {"solid": {"color": primary}},
                        "outline": "LeftRight",
                    }}],
                    "total": [{"properties": {
                        "fontColor": {"solid": {"color": primary}},
                    }}],
                }
            },
            "tableEx": {
                "*": {
                    "header": [{"properties": {
                        "fontColor": {"solid": {"color": "#FFFFFF"}},
                        "backColor": {"solid": {"color": primary}},
                    }}],
                }
            },
        },
    }


def apply_theme_to_report(
    xml_data: dict,
    pbip_dir: pathlib.Path,
    report_path: pathlib.Path,
    report_name: str,
) -> None:
    """Extract Cognos palette → build PBI theme → write file → patch report.json."""
    THEME_NAME = "CognosTheme"

    palette = extract_color_palette(xml_data)
    theme = build_pbi_theme(palette)

    theme_dir = (
        pbip_dir / f"{report_name}.Report"
        / "StaticResources" / "SharedResources" / "BaseThemes"
    )
    theme_dir.mkdir(parents=True, exist_ok=True)
    theme_path = theme_dir / f"{THEME_NAME}.json"
    theme_path.write_text(json.dumps(theme, ensure_ascii=False, indent=2), encoding="utf-8")

    report = json.loads(report_path.read_text(encoding="utf-8"))

    config_str = report.get("config", "{}")
    config = json.loads(config_str) if isinstance(config_str, str) else config_str
    config.setdefault("themeCollection", {}).setdefault("baseTheme", {})["name"] = THEME_NAME
    report["config"] = json.dumps(config, ensure_ascii=False, separators=(",", ":"))

    for pkg_entry in report.get("resourcePackages", []):
        for item in pkg_entry.get("resourcePackage", {}).get("items", []):
            if item.get("type") == 202:
                item["name"] = THEME_NAME
                item["path"] = f"BaseThemes/{THEME_NAME}.json"

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Theme Cognos: {len(palette)} couleur(s) -> {theme_path.name}")
