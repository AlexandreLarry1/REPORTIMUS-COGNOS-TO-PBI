"""Orchestrateur de la pipeline Qlik → SQL → CSV.

Modes :
  paste      Génère le prompt → attend que l'utilisateur colle le SQL dans sql/generated.sql
  api        Génère le prompt → appelle Claude API → exécute automatiquement
  skip-llm   Saute la génération LLM, relance uniquement sql_runner sur generated.sql existant
"""
import argparse
import pathlib
import sys

ROOT    = pathlib.Path(__file__).parent.parent
SQL_DIR = ROOT / "sql"
SQL_FILE = SQL_DIR / "generated.sql"


def _wait_for_sql() -> None:
    print("\n" + "="*60)
    print("ÉTAPE MANUELLE")
    print("  1. Ouvrez sql/prompt.txt")
    print("  2. Copiez le contenu dans votre chat LLM (Claude, GPT…)")
    print("  3. Collez la réponse dans sql/generated.sql")
    print("="*60)
    input("\nAppuyez sur Entrée une fois sql/generated.sql créé…")
    if not SQL_FILE.exists():
        print("sql/generated.sql introuvable. Abandon.")
        sys.exit(1)


def _call_api(system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError:
        print("Package 'anthropic' manquant. Installez-le : pip install anthropic")
        sys.exit(1)

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY") or (ROOT / ".env").read_text().split("ANTHROPIC_API_KEY=")[-1].split()[0]
    client = anthropic.Anthropic(api_key=api_key)

    print("Appel Claude API…")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline Qlik → SQL DuckDB → CSV")
    parser.add_argument(
        "--mode",
        choices=["paste", "api", "skip-llm"],
        default="paste",
        help="paste: prompt manuel | api: appel Claude | skip-llm: relance sql_runner seul",
    )
    parser.add_argument("--debug", action="store_true", help="Exporter aussi les tables intermédiaires")
    args = parser.parse_args()

    from pipeline.prompt_builder import build as build_prompt
    from pipeline.sql_runner import run as run_sql

    if args.mode == "skip-llm":
        if not SQL_FILE.exists():
            print(f"sql/generated.sql introuvable. Lancez d'abord --mode paste ou --mode api.")
            sys.exit(1)
        print("Mode skip-llm : sql/generated.sql existant utilisé.")

    elif args.mode == "paste":
        build_prompt(write=True)
        _wait_for_sql()

    elif args.mode == "api":
        system, user = build_prompt(write=True)
        sql = _call_api(system, user)
        SQL_DIR.mkdir(exist_ok=True)
        SQL_FILE.write_text(sql, encoding="utf-8")
        print(f"SQL généré → {SQL_FILE}")

    run_sql(sql_file=SQL_FILE, debug=args.debug)


if __name__ == "__main__":
    main()
