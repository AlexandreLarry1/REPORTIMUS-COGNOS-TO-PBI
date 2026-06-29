"""Execute generated.sql on input/ CSVs via DuckDB, postprocess and export."""
import csv
import datetime
import json
import duckdb
import pathlib
import pandas as pd

ROOT = pathlib.Path(__file__).parent.parent.parent


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


def _to_json_safe(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        if pd.isnull(val):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return val


def _table_context(con: duckdb.DuckDBPyConnection, table: str, sample_rows: int = 3) -> dict:
    cols = con.execute(
        f"SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_name = '{table}' ORDER BY ordinal_position"
    ).fetchall()
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    raw = con.execute(f"SELECT * FROM {table} LIMIT {sample_rows}").df().to_dict(orient="records")
    sample = [{k: _to_json_safe(v) for k, v in row.items()} for row in raw]
    return {
        "name": table,
        "row_count": n,
        "columns": [{"name": c, "type": t} for c, t in cols],
        "sample": sample,
    }


def run(example: str, debug: bool = False) -> None:
    example_dir  = ROOT / "examples" / example
    inputs_dir   = example_dir / "input"
    intermediate = example_dir / "intermediate"
    sql_file     = intermediate / "sql" / "generated.sql"
    log_file     = intermediate / "sql" / "run.log"
    context_file = intermediate / "sql" / "etl_context.json"
    out_final    = intermediate / "final"
    out_debug    = intermediate / "debug"

    log_lines: list[str] = [
        f"run_at={datetime.datetime.now().isoformat(timespec='seconds')}",
        f"example={example}",
        f"sql_file={sql_file}",
        "",
    ]

    def _log(line: str) -> None:
        print(line)
        log_lines.append(line)

    con = duckdb.connect()
    _log("Chargement des inputs...")
    load_inputs(con, inputs_dir)

    _log(f"Exécution de {sql_file.name}...")
    sql = sql_file.read_text(encoding="utf-8")
    errors: list[dict] = []
    for stmt in (s.strip() for s in sql.split(";") if s.strip()):
        try:
            con.execute(stmt)
        except Exception as e:
            preview = stmt[:120].replace("\n", " ")
            msg = f"[ERREUR] {e} | stmt: {preview}"
            _log(msg)
            errors.append({"error": str(e), "stmt_preview": preview})

    rows = con.execute(
        "SELECT table_name, table_type FROM information_schema.tables"
    ).fetchall()
    final_tables = [r[0] for r in rows if r[1] == "BASE TABLE"]
    inter_tables = [r[0] for r in rows if r[1] == "LOCAL TEMPORARY"]

    _log(f"\nTables finales  : {final_tables}")
    if debug:
        _log(f"Tables debug    : {inter_tables}")

    out_final.mkdir(parents=True, exist_ok=True)
    context_tables = []
    for t in final_tables:
        df = _postprocess(con.execute(f"SELECT * FROM {t}").df())
        df.to_csv(out_final / f"{t}.csv", index=False)
        ctx = _table_context(con, t)
        context_tables.append(ctx)
        _log(f"  [OK] {t} → final/{t}.csv  ({ctx['row_count']:,} lignes)")

    if debug:
        out_debug.mkdir(parents=True, exist_ok=True)
        for t in inter_tables:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            df = _postprocess(con.execute(f"SELECT * FROM {t}").df())
            df.to_csv(out_debug / f"{t}.csv", index=False)
            _log(f"  [DEBUG] {t} → debug/{t}.csv  ({n:,} lignes)")

    etl_context = {
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "example": example,
        "tables": context_tables,
        "errors": errors,
    }
    context_file.write_text(json.dumps(etl_context, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ETL context → {context_file}")

    log_lines.append(f"\nerreurs={len(errors)}")
    log_file.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"Log écrit    → {log_file}")


if __name__ == "__main__":
    import argparse, os
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--example", default=os.getenv("EXAMPLE_NAME", ""))
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    run(example=args.example, debug=args.debug)
