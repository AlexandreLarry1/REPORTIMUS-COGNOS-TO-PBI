# REPORTIMUS — Cognos to Power BI Migration

Automated pipeline converting IBM Cognos reports (XML v17.5) to Power BI projects (`.pbip`).

## Architecture

```
REPORTIMUS-COGNOS-TO-POWERBI/
│
├── examples/
│   ├── cognos-demo/                        # Finance sample
│   │   ├── Cognos - Sample File (Finance) - simple.xml
│   │   └── input/                          # CSV data files (gitignored)
│   └── cognos-demo-complex/                # Insurance sample
│       ├── assurance.xml
│       ├── DATA_DICTIONARY.md              # Table descriptions + relationships
│       └── input/                          # CSV data files (gitignored)
│
├── src/
│   ├── cognos/                # Cognos pipeline (entry point)
│   │   ├── pipeline.py        # Orchestrator — run this
│   │   ├── xml_parser.py      # Phase 1: XML → JSON extraction (0 LLM)
│   │   ├── layout_parser.py   # Phase 1: visuals extraction (0 LLM)
│   │   ├── expression_parser.py       # Phase 1: expression classification (0 LLM)
│   │   ├── deterministic_translator.py  # Phase 2: local DAX translation (0 LLM)
│   │   ├── data_dictionary.py         # Phase 1: DATA_DICTIONARY.md parsing
│   │   ├── pbip_generator.py          # Phase 3: PBIP assembly (0 LLM)
│   │   ├── validator.py               # Phase 3: model.bim validation
│   │   ├── css_parser.py              # CSS style parsing
│   │   └── llm/
│   │       ├── unified_translation.py  # Phase 2: 1 LLM call (DAX for D & H types)
│   │       └── viz_translation.py      # Phase 3: 1 LLM call (visual well wiring)
│   ├── pbip/
│   │   └── pbip_builder.py    # BIM + report.json generation from CSVs
│   ├── utils.py               # Shared utilities (_uid, call_api_azure, …)
│   └── observability.py       # Langfuse tracing
│
├── scripts/
│   └── fetch_langfuse_traces.py   # Debug: dump last LLM trace
│
├── docs/
│   ├── BRIEF.md               # 1-prompt architecture design spec
│   ├── STATUS.md              # Known issues + fix log
│   └── spec.md                # Functional specifications
│
└── .env.example
```

### Pipeline stages

| Phase | LLM calls | Output |
|-------|-----------|--------|
| 1 — Deterministic parsing | 0 | `cognos_extraction.json`, `visual_extraction.json`, `csv_schema.json` |
| 2 — Translation | 1 (types D & H only) | `merged_translation.json` |
| 3 — PBIP generation | 1 (visual wiring) | `.pbip` project folder |

---

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials:

```
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-4.1
AZURE_OPENAI_API_VERSION=2025-01-01-preview
```

---

## Run

**Finance example:**
```powershell
python src/cognos/pipeline.py `
  --example cognos-demo `
  --xml "examples/cognos-demo/Cognos - Sample File (Finance) - simple.xml"
```

**Insurance example:**
```powershell
python src/cognos/pipeline.py `
  --example cognos-demo-complex `
  --xml examples/cognos-demo-complex/assurance.xml
```

Output: `examples/<name>/pbip/<ReportName>.pbip` — open in Power BI Desktop.

### Flags

| Flag | Effect |
|------|--------|
| `--mode paste` | Write prompts to file, paste LLM responses manually |
| `--skip-llm` | Reuse existing `merged_translation.json` (skip DAX LLM call) |
| `--skip-data` | Reuse existing BIM, rerun viz wiring only |

---

## Adding a new example

1. Create `examples/<name>/input/` with your CSV files
2. Place your Cognos XML in `examples/<name>/`
3. Optionally add `examples/<name>/DATA_DICTIONARY.md` (table descriptions + relationships)
4. Run: `python src/cognos/pipeline.py --example <name> --xml examples/<name>/<file>.xml`

---

## LLM observability (Langfuse)

Set `LANGFUSE_ENABLED=true` in `.env` (keys in `.env.example`) and `pip install langfuse`.
Every pipeline run is fully traced: prompts, responses, errors.

```powershell
python scripts/fetch_langfuse_traces.py           # last trace full dump
python scripts/fetch_langfuse_traces.py --last 5  # summary of last 5 traces
python scripts/fetch_langfuse_traces.py --id <id> # specific trace
```
