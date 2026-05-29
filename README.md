# REPORTIMUS — Qlik to Power BI Migration

## Architecture

```
REPORTIMUS-QLIK-TO-POWERBI/
│
├── examples/
│   └── <example-name>/
│       ├── input/                    # CSVs source (exported from Qlik/QVD)
│       ├── intermediate/
│       │   ├── extraction.json       # Qlik script + variables (step 0 output)
│       │   ├── visual_extraction.json
│       │   ├── visual_response.json
│       │   └── sql/
│       │       ├── prompt.txt        # LLM1 prompt (SQL generation)
│       │       ├── generated.sql     # SQL DuckDB produced by LLM1
│       │       └── etl_context.json  # Tables + columns post-ETL
│       └── pbip/                     # Generated Power BI artifacts
│           ├── MigrationQlikPBI.SemanticModel/model.bim
│           └── MigrationQlikPBI.Report/report.json
│
├── src/
│   ├── extract/
│   │   ├── Extract_Data.py           # Step 0a: extract CSVs from Qlik via websocket
│   │   └── Extract_QLik_Elements.py  # Step 0b: extract script + variables → extraction.json
│   ├── pipeline/
│   │   └── run_pipeline.py           # Orchestrator: LLM1 (SQL) + LLM2 (schema)
│   └── pbip/
│       ├── schema_builder.py         # LLM2: infer relations + types
│       ├── pbip_builder.py           # Generate model.bim from CSVs + schema
│       └── visual_translator.py      # LLM3: DAX measures + report.json
│
├── requirements.txt
├── .env                              # gitignored — copy from .env.example
└── .env.example
```

---

## Getting started

### 1. Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your Azure OpenAI credentials:

```
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-4.1
AZURE_OPENAI_API_VERSION=2025-01-01-preview
```

---

### Option A — Mock data (no Qlik required)

The `examples/edr-demo` folder contains preloaded mock data and `extraction.json`.

```
EXAMPLE_NAME=edr-demo
```

Then jump directly to step 2 below.

---

### Option B — Real Qlik data

Requires Qlik Sense Desktop running locally with your `.qvf` loaded.

```
EXAMPLE_NAME=<your-example-name>
QLIK_WS_URL=ws://localhost:4848/app/
QLIK_QVF_NAME=YourApp.qvf
```

**Step 0 — Extract from Qlik**

```powershell
cd src
python extract/Extract_Data.py          # exports CSVs to examples/<name>/input/
python extract/Extract_QLik_Elements.py # generates examples/<name>/intermediate/extraction.json
```

Then continue with step 1 below.

---

### Step 1 — ETL + schema inference (LLM1 + LLM2)

```powershell
cd src

# Auto mode (Azure OpenAI generates SQL automatically)
python pipeline/run_pipeline.py --mode api

# Manual mode (paste LLM response yourself into generated.sql)
python pipeline/run_pipeline.py --mode paste

# Skip LLM (re-run ETL with existing generated.sql)
python pipeline/run_pipeline.py --mode skip-llm
```

Outputs: `intermediate/sql/etl_context.json`, `intermediate/schema_response.json`, CSVs in `intermediate/final/`

### Step 2 — Generate semantic model (BIM)

```powershell
python src/pbip/pbip_builder.py
```

Output: `pbip/MigrationQlikPBI.SemanticModel/model.bim`

### Step 3 — Translate visuals (LLM3)

```powershell
# Auto mode
python src/pbip/visual_translator.py --mode api

# Manual mode (paste response into intermediate/visual_response.json)
python src/pbip/visual_translator.py --mode paste

# Re-apply existing visual_response.json without LLM call
python src/pbip/visual_translator.py --mode apply
```

Output: `pbip/MigrationQlikPBI.Report/report.json` + DAX measures in `model.bim`

---

## Optional: LLM observability (Langfuse)

Add to `.env`:

```
LANGFUSE_ENABLED=true
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

Install: `pip install langfuse`

---

## Bug fixes reference

### `generated.sql` — `AnneeMois` : month `00` instead of real month

**Cause**: `SUBSTR(LPAD(...), -2)` — negative index returns empty in DuckDB.

**Fix**:
```sql
-- Before
GJAHR || '-' || SUBSTR(LPAD(CAST(PERIO AS VARCHAR), 2, '0'), -2) AS AnneeMois
-- After
GJAHR || '-' || LPAD(CAST(CAST(PERIO AS INTEGER) AS VARCHAR), 2, '0') AS AnneeMois
```

### `sql_runner.py` — 3 CSV format gaps vs Qlik ground truth

Fixed in `_postprocess()` applied to each final exported table.

#### 1. `Date comptable`: Excel serial → ISO date

**Cause**: `BUDAT` stored as Excel serial (days since 30/12/1899).

**Fix**: `pd.to_datetime(numeric[mask], unit="D", origin=pd.Timestamp("1899-12-30")).dt.strftime("%Y-%m-%d")`

#### 2. Integer columns stored as float (`1.0` → `1`)

**Cause**: DuckDB returns float64 when columns contain NULLs.

**Fix**: auto-detect float columns where all non-null values are integers → cast to int string.

#### 3. Null values: `NaN` → `'-'`

**Fix**: `df.astype(object).fillna("-")`
