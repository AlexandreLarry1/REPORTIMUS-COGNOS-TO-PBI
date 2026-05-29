# REPORTIMUS — Qlik to Power BI Migration

## Architecture

```
REPORTIMUS-QLIK-TO-POWERBI/
│
├── examples/
│   └── <example-name>/
│       ├── input/                 # CSVs source (QVD exportés)
│       ├── intermediate/
│       │   ├── extraction.json    # Script Qlik + variables (jalon 1)
│       │   ├── schema_prompt.txt  # Prompt LLM2 (schéma)
│       │   ├── schema_response.json  # Réponse LLM2 (relations + types)
│       │   └── sql/
│       │       ├── prompt.txt        # Prompt LLM1 (SQL)
│       │       ├── generated.sql     # SQL DuckDB produit par LLM1
│       │       └── etl_context.json  # Tables + colonnes post-ETL
│       └── pbip/                  # Artefacts Power BI générés
│           ├── MigrationQlikPBI.SemanticModel/model.bim
│           └── MigrationQlikPBI.Report/report.json
│
├── src/
│   ├── pipeline/
│   │   └── run_pipeline.py        # Orchestrateur ETL (LLM1 SQL + LLM2 schéma)
│   └── pbip/
│       ├── schema_builder.py      # LLM2 : inférence relations + types
│       ├── pbip_builder.py        # Génère model.bim depuis CSVs + schema
│       └── visual_translator.py   # LLM3 : mesures DAX + report.json
│
├── requirements.txt
├── .env                           # EXAMPLE_NAME, Azure OpenAI keys (gitignored)
└── .env.example
```

---

## Pipeline complète (version schéma)

3 LLM calls dans l'ordre : **SQL → Schéma → Visuels**

### Étape 1 — ETL + inférence schéma

```bash
# Mode api (entièrement automatique — LLM1 SQL + LLM2 schéma)
python -m pipeline.run_pipeline --mode api

# Mode paste (manuel — coller les réponses LLM dans les fichiers intermédiaires)
python -m pipeline.run_pipeline --mode paste

# Mode skip-llm (relance ETL uniquement, schema_response.json existant réutilisé)
python -m pipeline.run_pipeline --mode skip-llm
```

Sorties : `intermediate/sql/etl_context.json`, `intermediate/schema_response.json`, CSVs dans `input/`

### Étape 2 — Génération du modèle BIM

```bash
python src/pbip/pbip_builder.py
```

Lit `schema_response.json` si présent (relations LLM) ou bascule sur heuristiques.
Sortie : `pbip/MigrationQlikPBI.SemanticModel/model.bim`

### Étape 3 — Traduction des visuels

```bash
# Mode api (entièrement automatique — LLM3 mesures DAX + patches report.json)
python src/pbip/visual_translator.py --mode api

# Mode paste (coller la réponse JSON dans intermediate/visual_response.json)
python src/pbip/visual_translator.py --mode paste

# Mode apply (réappliquer visual_response.json existant sans rappeler le LLM)
python src/pbip/visual_translator.py --mode apply
```

Sortie : `pbip/MigrationQlikPBI.Report/report.json` + mesures/calc cols dans `model.bim`

### Relancer une étape isolée

```bash
# Regénérer uniquement le BIM (sans retoucher le SQL ni le schéma)
python src/pbip/pbip_builder.py

# Regénérer uniquement les visuels (sans retoucher le BIM)
python src/pbip/visual_translator.py --mode skip-llm
```

### Config `.env`

```
EXAMPLE_NAME=edr-demo-schema
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_API_VERSION=2024-12-01-preview
AZURE_OPENAI_DEPLOYMENT=gpt-4.1
```

---

## Prompt builder

Le prompt LLM1 (SQL) est construit dynamiquement :

| Composant | Source |
|---|---|
| Tables disponibles + colonnes | scan de `input/*.csv` |
| Variables résolues | `extraction.json` → champ `variables` |
| Script Qlik | `extraction.json` → champ `script` |
| Règles de traduction | template fixe dans `run_pipeline.py` |

---

## Bugs corrigés

### `generated.sql` — `AnneeMois` : mois `00` au lieu du mois réel

**Symptôme** : colonne `AnneeMois` produisait `2023-00`, `2024-00`, etc. (~70 % des lignes).

**Cause** : `SUBSTR(LPAD(CAST(PERIO AS VARCHAR), 2, '0'), -2)` — index négatif retourne vide en DuckDB.

**Fix** :
```sql
-- Avant
GJAHR || '-' || SUBSTR(LPAD(CAST(PERIO AS VARCHAR), 2, '0'), -2) AS AnneeMois
-- Après
GJAHR || '-' || LPAD(CAST(CAST(PERIO AS INTEGER) AS VARCHAR), 2, '0') AS AnneeMois
```

> Ce bug est aussi corrigé dans le template SYSTEM du `prompt_builder.py` :
> `right(str, n) → RIGHT(str, n)` (et non `SUBSTR(str, -n)`).

### `sql_runner.py` — 3 écarts de format sur l'export CSV (vs GT Qlik)

Corrigés dans `_postprocess()` appliqué à chaque table finale exportée.

#### 1. `Date comptable` : serial Excel → date ISO

**Symptôme** : `45746` au lieu de `2025-03-30`.

**Cause** : `BUDAT` stocké comme serial Excel (jours depuis 30/12/1899).

**Fix** :
```python
pd.to_datetime(numeric[mask], unit="D", origin=pd.Timestamp("1899-12-30")).dt.strftime("%Y-%m-%d")
```

#### 2. Colonnes entières stockées en float (`1.0` → `1`)

**Symptôme** : `Est un Ordre Interne`, `Numéro de commande` exportés avec `.0`.

**Cause** : DuckDB retourne ces colonnes en `float64` dès qu'elles contiennent des NULL.

**Fix** : détection automatique des colonnes float dont toutes les valeurs non-nulles sont entières → cast en `int` string.

#### 3. Valeurs nulles : `NaN` → `'-'`

**Symptôme** : colonnes optionnelles vides, le GT Qlik utilise `'-'`.

**Fix** : `df.astype(object).fillna("-")`.
