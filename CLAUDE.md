# Project conventions

## No direct PBIP patches

Never patch `model.bim` or `report.json` directly to fix a data/visual problem.
Every fix must target the source that produces the broken output:

| Symptom | Root cause | Where to fix |
|---|---|---|
| Wrong/missing relationship | `_infer_relationships` missed a pattern | `pbip_builder.py` |
| Column type wrong (string instead of number) | `_infer_col_types` or `_m_expression` | `pbip_builder.py` |
| Calculated column is TODO or wrong DAX | LLM missed it OR `_infer_calc_expr` pattern missing | `visual_translator.py` prompt or `_infer_calc_expr` |
| Visual empty / wrong wells | `_WELLS` dict wrong, or `_build_prototype_query` logic | `visual_translator.py` |
| Measure BLANK | Relationship missing upstream, or DAX expression wrong | Fix relationship source first, then DAX |

If a direct patch is unavoidable as a stopgap (e.g. PBI Desktop import needed immediately),
document it here as a known debt with the pipeline fix location, and implement the durable fix in the same session.

## Pipeline stages

```
run_pipeline.py       → ETL: QVD/CSV → fact/dim CSVs in input/
pbip_builder.py       → BIM: tables + relationships + M expressions
visual_translator.py  → BIM: measures + calc cols + report.json visuals
```

Each stage rewrites its outputs from scratch (or updates them). A direct patch to
`model.bim` or `report.json` will be overwritten on the next pipeline run.
