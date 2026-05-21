"""Execute generated.sql on input/ CSVs via DuckDB, postprocess and export."""
import csv
import duckdb
import pathlib
import pandas as pd

ROOT = pathlib.Path(__file__).parent.parent


def _col_aliases(csv_path: pathlib.Path) -> str:
    with open(csv_path, newline="", encoding="utf-8") as f:
        headers = next(csv.reader(f))
    parts = []
    for h in headers:
        clean = h.split(".", 1)[1] if "." in h else h
        parts.append(f'"{h}" AS "{clean}"')
    return ", ".join(parts)


def load_inputs(con: duckdb.DuckDBPyConnection, inputs_dir: pathlib.Path) -> None:
    for csv_path in sorted(inputs_dir.glob("*.csv")):
        cols = _col_aliases(csv_path)
        view_name = f"{csv_path.stem}_src"
        con.execute(
            f"CREATE VIEW {view_name} AS SELECT {cols} FROM read_csv_auto("
            f"'{csv_path.as_posix()}', strict_mode=false, null_padding=true, "
            f"ignore_errors=true, parallel=false, all_varchar=true)"
        )
        print(f"  {view_name} ← {csv_path.name}")


def _postprocess(df: pd.DataFrame) -> pd.DataFrame:
    if "Date comptable" in df.columns:
        numeric = pd.to_numeric(df["Date comptable"], errors="coerce")
        mask = numeric.notna()
        df.loc[mask, "Date comptable"] = pd.to_datetime(
            numeric[mask], unit="D", origin=pd.Timestamp("1899-12-30")
        ).dt.strftime("%Y-%m-%d")

    for col in df.columns:
        if df[col].dtype == float:
            non_null = df[col].dropna()
            if len(non_null) > 0 and (non_null == non_null.round()).all():
                df[col] = df[col].apply(lambda x: str(int(x)) if pd.notna(x) else x)

    df = df.astype(object).fillna("-")
    return df


def run(example: str, debug: bool = False) -> None:
    example_dir  = ROOT / "examples" / example
    inputs_dir   = example_dir / "input"
    intermediate = example_dir / "intermediate"
    sql_file     = intermediate / "sql" / "generated.sql"
    out_final    = intermediate / "final"
    out_debug    = intermediate / "debug"

    con = duckdb.connect()
    print("Chargement des inputs...")
    load_inputs(con, inputs_dir)

    print(f"Exécution de {sql_file.name}...")
    sql = sql_file.read_text(encoding="utf-8")
    for stmt in (s.strip() for s in sql.split(";") if s.strip()):
        con.execute(stmt)

    rows = con.execute(
        "SELECT table_name, table_type FROM information_schema.tables"
    ).fetchall()
    final_tables = [r[0] for r in rows if r[1] == "BASE TABLE"]
    inter_tables = [r[0] for r in rows if r[1] == "LOCAL TEMPORARY"]

    print(f"\nTables finales  : {final_tables}")
    if debug:
        print(f"Tables debug    : {inter_tables}")

    out_final.mkdir(parents=True, exist_ok=True)
    for t in final_tables:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        df = _postprocess(con.execute(f"SELECT * FROM {t}").df())
        df.to_csv(out_final / f"{t}.csv", index=False)
        print(f"  → examples/{example}/output/intermediate/final/{t}.csv  ({n:,} lignes)")

    if debug:
        out_debug.mkdir(parents=True, exist_ok=True)
        for t in inter_tables:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            df = _postprocess(con.execute(f"SELECT * FROM {t}").df())
            df.to_csv(out_debug / f"{t}.csv", index=False)
            print(f"  → examples/{example}/output/intermediate/debug/{t}.csv  ({n:,} lignes)")


if __name__ == "__main__":
    import argparse, os
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--example", default=os.getenv("EXAMPLE_NAME", ""))
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    run(example=args.example, debug=args.debug)
