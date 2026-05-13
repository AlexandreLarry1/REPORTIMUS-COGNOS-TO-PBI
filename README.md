# REPORTIMUS — Qlik to Power BI Migration

## Architecture

```
REPORTIMUS-QLIK-TO-POWERBI/
│
├── Extract_elements_Qlik/
│   └── Extract_QLik_Elements.py   # WebSocket Qlik → extraction.json
│
├── inputs/                        # CSV SAP (gitignorés, volumineuse)
│   ├── COOI.csv                   # Engagements
│   ├── EKKN.csv                   # Imputations commandes
│   ├── FMIOI.csv                  # Données FM
│   └── PRPS.csv                   # OTP / projets
│
├── extraction/
│   └── extraction.json            # Script Qlik + variables (sortie jalon 1)
│
├── sql/
│   ├── prompt.txt                 # Prompt LLM généré dynamiquement (gitignored)
│   └── generated.sql              # SQL DuckDB produit par le LLM (gitignored)
│
├── output/
│   ├── final/                     # Tables finales exportées en CSV
│   └── debug/                     # Tables intermédiaires (optionnel, --debug)
│
├── pipeline/
│   ├── prompt_builder.py          # Génère le prompt depuis inputs/ + extraction.json
│   ├── sql_runner.py              # Exécute generated.sql + postprocess + export
│   └── run_pipeline.py            # Orchestrateur CLI
│
├── requirements.txt
├── .env                           # ANTHROPIC_API_KEY, QLIK_WS_URL (gitignored)
└── .env.example
```

---

## Pipeline complète

### Jalon 1 — Extraire le script Qlik

Prérequis : Qlik Desktop ouvert avec l'app chargée.

```bash
python Extract_elements_Qlik/Extract_QLik_Elements.py
# → extraction/extraction.json
```

### Jalon 2 — Générer et exécuter le SQL

#### Mode `paste` (sans API, par défaut)

```bash
python -m pipeline.run_pipeline --mode paste
# 1. Génère sql/prompt.txt automatiquement
# 2. Attend que vous colliez la réponse du LLM dans sql/generated.sql
# 3. Exécute et exporte vers output/final/
```

#### Mode `api` (entièrement automatique)

```bash
# Ajouter ANTHROPIC_API_KEY dans .env
python -m pipeline.run_pipeline --mode api
# Génère le prompt → appelle Claude API → exécute → exporte
```

#### Mode `skip-llm` (relance uniquement le runner)

```bash
python -m pipeline.run_pipeline --mode skip-llm
python -m pipeline.run_pipeline --mode skip-llm --debug  # inclut tables intermédiaires
```

---

## Prompt builder

Le prompt est construit dynamiquement :

| Composant | Source |
|---|---|
| Tables disponibles + colonnes | scan de `inputs/*.csv` |
| Variables résolues | `extraction/extraction.json` → champ `variables` |
| Script Qlik | `extraction/extraction.json` → champ `script` |
| Règles de traduction | template fixe dans `pipeline/prompt_builder.py` |

Les tables cibles sont **déduites du script Qlik** par le LLM — pas besoin de les lister explicitement.

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
