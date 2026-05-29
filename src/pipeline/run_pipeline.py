"""Orchestrateur de la pipeline Qlik → SQL → CSV.

Modes :
  paste      Génère le prompt → attend que l'utilisateur colle le SQL dans output/intermediate/sql/generated.sql
  api        Génère le prompt → appelle Claude API → exécute automatiquement
  skip-llm   Saute la génération LLM, relance uniquement sql_runner sur generated.sql existant
"""
import argparse
import os
import pathlib
import sys

from dotenv import load_dotenv
load_dotenv()

ROOT = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))  # src/ → pipeline imports work

import observability as obs


def _wait_for_sql(sql_file: pathlib.Path) -> None:
    print("\n" + "="*60)
    print("ÉTAPE MANUELLE")
    print(f"  1. Ouvrez {sql_file.parent}/prompt.txt")
    print("  2. Copiez le contenu dans votre chat LLM (Claude, GPT…)")
    print(f"  3. Collez la réponse dans {sql_file}")
    print("="*60)
    input("\nAppuyez sur Entrée une fois generated.sql créé…")
    if not sql_file.exists():
        print("generated.sql introuvable. Abandon.")
        sys.exit(1)


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
        name="sql_generation",
        model=model,
        input=messages,
    )

    print("Appel Azure OpenAI…")
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=8192,
    )
    result = response.choices[0].message.content
    gen.end(output=result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline Qlik → SQL DuckDB → CSV")
    parser.add_argument("--mode", choices=["paste", "api", "skip-llm"], default="paste")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    example = os.getenv("EXAMPLE_NAME", "")
    if not example:
        print("EXAMPLE_NAME non défini dans .env. Abandon.")
        sys.exit(1)

    sql_file = ROOT / "examples" / example / "intermediate" / "sql" / "generated.sql"

    from pipeline.prompt_builder import build as build_prompt
    from pipeline.sql_runner import run as run_sql
    from pbip.schema_builder import build as build_schema

    with obs.trace("pipeline_run", example=example, mode=args.mode) as trace:
        if args.mode == "skip-llm":
            if not sql_file.exists():
                print(f"{sql_file} introuvable. Lancez d'abord --mode paste ou --mode api.")
                sys.exit(1)
            print("Mode skip-llm : generated.sql existant utilisé.")

        elif args.mode == "paste":
            build_prompt(example=example, write=True)
            _wait_for_sql(sql_file)

        elif args.mode == "api":
            system, user = build_prompt(example=example, write=True)
            sql = _call_api(system, user, trace=trace)
            sql_file.parent.mkdir(parents=True, exist_ok=True)
            sql_file.write_text(sql, encoding="utf-8")
            print(f"SQL généré → {sql_file}")

        sql_sp = trace.span(name="sql_execution")
        run_sql(example=example, debug=args.debug)
        sql_sp.end()

        if args.mode in ("paste", "api"):
            print("\n=== Schéma sémantique ===")
            schema_sp = trace.span(name="schema_build")
            build_schema(example=example, mode=args.mode, trace=schema_sp)
            schema_sp.end()


if __name__ == "__main__":
    main()
