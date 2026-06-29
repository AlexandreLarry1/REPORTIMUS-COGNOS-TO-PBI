"""Parser déterministe pour DATA_DICTIONARY.md.

Extrait sans LLM les sections structurées (tables Markdown) :
- table_map       : display name → csv stem   (pour _resolve_to_csv et relations)
- relationships   : {fromTable, fromColumn, toTable, toColumn} prêts pour pbip_builder
- shared_keys     : noms de colonnes partagées entre tables
- raw_text        : markdown complet → passé verbatim au LLM Phase 2
"""
import pathlib
import re


def parse_data_dictionary(md_path: pathlib.Path) -> dict:
    if not md_path.exists():
        return {"table_map": {}, "relationships": [], "shared_keys": [], "raw_text": ""}

    text = md_path.read_text(encoding="utf-8")
    table_map = _parse_tables_section(text)
    relationships = _parse_relationships_section(text, table_map)
    shared_keys = _parse_shared_keys(text)

    return {
        "table_map": table_map,
        "relationships": relationships,
        "shared_keys": shared_keys,
        "raw_text": text,
    }


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_tables_section(text: str) -> dict[str, str]:
    """Parse '## Tables' → {display_name: csv_stem}."""
    table_map: dict[str, str] = {}
    in_section = False

    for line in text.splitlines():
        if re.match(r"^##\s+Tables", line):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", line):
            break
        if not in_section or not line.startswith("|"):
            continue
        if "|---|" in line:
            continue

        cols = [c.strip().strip("`") for c in line.split("|")[1:-1]]
        if len(cols) < 2 or cols[0] in ("Table", ""):
            continue

        display_name = cols[0].strip()
        csv_file = cols[1].replace(".csv", "").strip()
        if display_name and csv_file:
            table_map[display_name] = csv_file

    return table_map


def _parse_relationships_section(text: str, table_map: dict[str, str]) -> list[dict]:
    """Parse '## Intended relationships' → [{fromTable, fromColumn, toTable, toColumn}].

    Skips conceptual dimensions (entries where 'from' is in parentheses).
    Handles multiple comma-separated 'to' tables.
    """
    rels: list[dict] = []
    in_section = False

    for line in text.splitlines():
        if re.match(r"^##\s+Intended relationships", line):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", line):
            break
        if not in_section or not line.startswith("|"):
            continue
        if "|---|" in line:
            continue

        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 3:
            continue

        from_raw, to_raw, on_col = cols[0], cols[1], cols[2]

        # Skip header row and conceptual dims (in parentheses)
        if from_raw.startswith("From") or from_raw.startswith("("):
            continue

        on_col = on_col.strip()
        if not on_col:
            continue

        from_csv = _resolve_to_csv_stem(from_raw, table_map)
        if not from_csv:
            continue

        # Handle multiple 'to' tables (comma-separated, strip "(via ...)" notes)
        to_entries = [_strip_via(t.strip()) for t in to_raw.split(",")]
        for to_entry in to_entries:
            if not to_entry:
                continue
            to_csv = _resolve_to_csv_stem(to_entry, table_map)
            if not to_csv:
                continue
            rels.append({
                "fromTable": from_csv,
                "fromColumn": on_col,
                "toTable": to_csv,
                "toColumn": on_col,
            })

    return rels


def _parse_shared_keys(text: str) -> list[str]:
    """Parse '## Shared keys' → [column_name, ...]."""
    keys: list[str] = []
    in_section = False

    for line in text.splitlines():
        if re.match(r"^##\s+Shared keys", line):
            in_section = True
            continue
        if in_section and re.match(r"^##\s+", line):
            break
        if not in_section:
            continue

        m = re.match(r"-\s+\*\*([^*]+)\*\*", line)
        if m:
            keys.append(m.group(1).strip())

    return keys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_to_csv_stem(display_name: str, table_map: dict[str, str]) -> str | None:
    """Map a display name to a CSV stem via table_map, with fuzzy fallback."""
    # Exact match
    if display_name in table_map:
        return table_map[display_name]

    # Case-insensitive exact
    lower = display_name.lower()
    for k, v in table_map.items():
        if k.lower() == lower:
            return v

    # Substring match (display_name contained in a key or vice versa)
    for k, v in table_map.items():
        if lower in k.lower() or k.lower() in lower:
            return v

    return None


def _strip_via(s: str) -> str:
    """Remove '(via ...)' annotations and extra whitespace."""
    return re.sub(r"\s*\(via[^)]*\)", "", s).strip()
