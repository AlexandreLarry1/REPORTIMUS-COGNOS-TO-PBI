# REPORTIMUS — Session Summary

## Approche générale

Migration déterministe Cognos Analytics → Power BI Premium (PBIP) en 3 phases :

1. **Phase 1** — Parser XML Cognos + DATA_DICTIONARY → artefacts intermédiaires JSON
2. **Phase 2** — Traduction DAX (déterministe + 1 appel LLM pour expressions complexes)
3. **Phase 3** — Génération PBIP (model.bim + report.json + wiring LLM)

Hypothèse centrale : le maximum de logique doit être déterministe (reproductible, testable, sans coût LLM). Le LLM intervient uniquement pour les expressions ambiguës et le wiring visuel.

---

## Pipeline actuelle

```
assurance.xml
DATA_DICTIONARY.md
input/*.csv
     │
     ▼
┌─────────────────────────────────────────────────┐
│  PHASE 1 — Parseur déterministe                 │
│                                                 │
│  xml_parser.py                                  │
│    ├── vizControl  → viz_controls[]             │
│    ├── listControl → list_controls[]            │
│    ├── selectValue → select_values[]            │
│    └── reportDataStore → data_stores{}          │
│                                                 │
│  layout_parser.py                               │
│    ├── IBM type → PBI type (_IBM_TO_PBI)        │
│    ├── slots → available_fields                 │
│    └── migration_note si approximation          │
│         → visual_extraction.json               │
│                                                 │
│  data_dictionary.py                             │
│    ├── tables map (display → csv stem)          │
│    ├── relationships (avec direction)           │
│    └── shared keys                             │
│                                                 │
│  csv_schema.json  ←  input/*.csv               │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  PHASE 2 — Traduction unifiée                   │
│                                                 │
│  deterministic_translator.py                    │
│    ├── Type A (ref simple) → SUM/AVERAGE/       │
│    │   SELECTEDVALUE selon dtype colonne        │
│    ├── Type B (variance) → déterministe         │
│    ├── Type C (param switch) → SWITCH()         │
│    ├── Type D/H → différé LLM                   │
│    └── conditional_time_intel → MTD/QTD/YTD    │
│         (résolution dynamique fact + date)      │
│                                                 │
│  unified_translation.py  [1 appel LLM]          │
│    └── expressions D/H → DAX                   │
│         → merged_translation.json              │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  PHASE 3 — Générateur PBIP                      │
│                                                 │
│  pbip_builder.py                                │
│    ├── BIM tables depuis CSV (types auto)       │
│    ├── Relations :                              │
│    │   DATA_DICTIONARY > schema > inférées      │
│    │   + auto-flip direction (unicité CSV)      │
│    └── model.bim                               │
│                                                 │
│  pbip_generator.py                              │
│    ├── report.json (pages + visuels vides)      │
│    ├── Param tables (déconnectées)              │
│    ├── _Measures table                          │
│    │   + patch SELECTEDVALUE num → SUM/AVG     │
│    ├── viz_translation.py  [1 appel LLM]        │
│    │   ├── BIM inventory → LLM                  │
│    │   └── visual_extraction → LLM             │
│    │        → visual_wiring.json               │
│    ├── wire_from_spec()                         │
│    │   ├── SELECTEDVALUE unwrap → Aggregation   │
│    │   ├── Categ. wells: mesure → vraie colonne │
│    │   ├── anchor_table: cross-table redirect   │
│    │   └── dtype-aware: string ≠ SUM           │
│    └── MigrationCognosPBI.pbip                 │
│         + migration_report.json                 │
└─────────────────────────────────────────────────┘
```

### Artefacts intermédiaires

| Fichier | Contenu |
|---|---|
| `cognos_extraction.json` | XML parsé brut (queries, vizControls, dataStores…) |
| `visual_extraction.json` | Visuels par page avec slots IBM et champs |
| `csv_schema.json` | Schéma colonnes + types des CSV |
| `merged_translation.json` | Mesures DAX finales |
| `visual_wiring.json` | Wells PBI assignés par LLM |
| `migration_report.json` | Visuels sans équivalent natif + notes |
| `llm_traces/` | Prompts + réponses LLM horodatés |

---

## Problèmes rencontrés et fixes (ordre chronologique)

### 1. `COGNOS_XML_PATH` absent du `.env`
**Problème** : chemin XML hardcodé en CLI.
**Fix** : variable `.env` `COGNOS_XML_PATH` + fallback dans `pipeline.py`.

---

### 2. Parser XML incomplet sur `assurance.xml`
**Problème** : `assurance.xml` utilise `<vizControl>` (format IBM moderne), pas `<crosstab>`. Le parser original retournait 0 visuels.
**Hypothèse** : les rapports Cognos modernes n'exposent pas de crosstabs natifs, ils utilisent des `vizControl` avec `reportDataStore`.
**Fix** : `xml_parser.py` — ajout `parse_viz_controls()`, `parse_data_stores()`, `parse_list_controls()`, `parse_select_values()`.

---

### 3. DATA_DICTIONARY.md — parsing déterministe vs LLM
**Question** : faut-il un LLM pour interpréter le business dictionary ?
**Décision** : parsing déterministe suffisant (structure Markdown tabulaire stable). `data_dictionary.py` extrait 5 tables, 1 relation, 4 clés partagées sans appel LLM.

---

### 4. Types IBM sans équivalent natif → tous en `tableEx`
**Problème** : `heatmap`, `wordcloud`, `radar`, `river`, `marimekko`, `packedBubble`, `hierarchicalPackedBubble` dégradaient en `tableEx` inutilement.
**Fix** : `layout_parser.py` — mappings améliorés :

| IBM | PBI | Note |
|---|---|---|
| `heatmap` | `matrix` | + conditional formatting manuel |
| `wordcloud` | `treemap` | taille mot ≈ aire tuile |
| `radar` | `lineChart` | mêmes axes, pas circulaire |
| `river` | `stackedAreaChart` | flux sur le temps préservé |
| `marimekko` | `hundredPercentStackedColumnChart` | proportions ok, largeur variable perdue |
| `packedBubble` | `scatterChart` | bulles + taille |
| `hierarchicalPackedBubble` | `treemap` | hiérarchie + taille |
| `boxplot` / `network` | `tableEx` | pas d'équivalent natif |

---

### 5. `mode: "calculated"` invalide — erreur PBI à l'ouverture
**Erreur** : `Impossible de convertir la valeur « calculated » en type requis « ModeType »`.
**Cause** : la table de mesures avait `partitions[0].mode = "calculated"` qui n'existe pas dans la spec BIM.
**Fix** : `pbip_generator.py` → `mode: "import"` + `source.type: "calculated"` + expression `DATATABLE`.

---

### 6. Nom `"Measures"` réservé dans PBI
**Erreur** : `Table named "Measures" non pris en charge dans le schéma du modèle de données`.
**Fix** : renommage en `"_Measures"` + colonne dummy cachée obligatoire.

---

### 7. Direction de relation inversée
**Erreur** : `customer_analysis[Vehicle Class]` côté "one" mais la colonne a des doublons.
**Cause** : DATA_DICTIONARY `targets → customer_analysis` interprété littéralement. `customer_analysis` est le côté many, `targets` est le côté one (valeurs uniques par segment).
**Fix** : `_relationships_from_schema()` dans `pbip_builder.py` lit les CSVs pour détecter l'unicité et flip automatiquement la direction si nécessaire.

---

### 8. Visuels vides — colonnes brutes en puits valeur
**Problème** : colonnes numériques dans `Y`/`Size` d'un chart → PBI ignore une `Column` brute dans un puits valeur de chart, mais l'accepte pour `tableEx`.
**Fix** : `_build_select()` — détection dtype BIM → `Aggregation(Sum)` pour colonnes numériques dans `_VALUE_WELLS`, `Column` brut pour strings et visuels `tableEx`/`matrix`.

---

### 9. Mesures `SELECTEDVALUE` → BLANK en contexte multi-lignes
**Problème** : toutes les mesures simples étaient générées comme `SELECTEDVALUE('t'[col])`. Retournent BLANK dès que le contexte contient plusieurs lignes (scatter, line groupé) → bubble chart : 1 seule bulle, line charts : lignes plates.
**Fix BIM** : `apply_cognos_specifics()` — patch automatique post-génération : `SELECTEDVALUE` de colonne numérique → `SUM()` ou `AVERAGE()` (selon le nom de la mesure).
**Fix wiring** : `_unwrap_selectedvalue()` — pour les puits valeur, détecte le pattern `SELECTEDVALUE('t'[col])` et produit une `Aggregation` directe sur la colonne.

---

### 10. Champs catégoriels de `_Measures` en puits `Category`
**Problème** : `Employment Status`, `Month`, `Education`… existent comme mesures DAX dans `_Measures`. Le LLM viz les référençait en `Category`/`Details` → PBI ne peut pas itérer sur une mesure DAX comme axe catégoriel.
**Fix** : `wire_from_spec()` — si mesure dans puit catégoriel (`_CATEGORY_WELLS`), recherche la vraie colonne dans les tables de données via `_col_name_to_table` et redirige.
**Prompt** : règle ajoutée : les puits catégoriels doivent toujours pointer vers `table[column]` depuis une vraie table, jamais `_Measures`.

---

### 11. Cross-table mismatch → lignes constantes
**Problème** : LLM wire `renewals[Month]` (Category) + `offers[Accepted]` (Y) dans le même visuel. Pas de relation entre ces tables → PBI agrège `offers` en entier pour chaque mois → valeur identique → ligne plate.
**Fix** : `wire_from_spec()` — pre-pass `_anchor_table` : identifie la table du puits catégoriel, redirige les valeurs d'une autre table vers la même table si la colonne existe (`renewals[Accepted]` existe et `Month` est dans `renewals`).

---

### 12. SUM invalide sur colonnes string
**Erreur** : `Aggregation(Sum)` sur colonne string → `Education`, `Employment Status` → erreur de chargement PBI.
**Fix** : `_build_select()` — `Aggregation` uniquement pour `dataType in _NUMERIC_TYPES`. Strings → `Column` brut.

---

### 13. Layout — tous les visuels à `(0, 0)`
**Problème** : Cognos XML ne stocke pas les positions pixel dans les `vizControl`. Tous les visuels générés à `col:0, row:0` → empilement dans le coin supérieur gauche.
**Fix** : `_auto_layout()` dans `pbip_builder.py` — détecte les positions identiques et distribue en grille : slicers pleine largeur en haut, charts 3 par rangée (10 cols × 5 rows), tables pleine largeur.

---

### 14. Canvas trop petit — visuels coupés
**Problème** : `CANVAS_H = 720px` fixe → visuels dépassant le bas de la page invisibles.
**Fix** : hauteur de section calculée dynamiquement depuis la position réelle du dernier visuel (`max_bottom`).

---

### 15. `conditional_time_intel` hardcodée sur `fact_data[Value]`
**Problème** : mesures MTD/QTD/YTD généraient `SUM(fact_data[Value])` quel que soit l'exemple.
**Fix** : `deterministic_translator.py` — résolution dynamique : cherche le premier `fact_`-prefixed table → plus grande table → première table. Résout aussi la colonne date dynamiquement depuis `csv_schema`.

---

## État final — exemple `cognos-demo-complex`

| Métrique | Valeur |
|---|---|
| Pages | 6 (Overview, Offers, Open Complaints, Targets, Vehicle, Renewals) |
| Visuels parsés | 39 (37 vizControls + 1 listControl + 1 selectValue) |
| Mesures DAX | 38 dans `_Measures` |
| Relations BIM | 1 (DATA_DICTIONARY) |
| Warnings migration | 15 |
| Erreurs validation BIM | 0 |

---

## Flags de la pipeline

```bash
python src/cognos/pipeline.py                  # run complet (2 LLM calls)
python src/cognos/pipeline.py --skip-llm       # réutilise merged_translation.json + re-wire viz
python src/cognos/pipeline.py --mode paste     # prompts écrits dans fichiers, réponse manuelle
python src/cognos/pipeline.py --example <nom>  # surcharge COGNOS_EXAMPLE_NAME
python src/cognos/pipeline.py --xml <chemin>   # surcharge COGNOS_XML_PATH
```

---

## Limites connues / dette technique

| Problème | Statut | Localisation |
|---|---|---|
| `offers` sans colonne `Month` → Line charts plats | Données manquantes | `input/offers.csv` |
| Positions Cognos non exposées dans XML | Accepté — auto-grille | `pbip_builder.py:_auto_layout` |
| `<textItem>/<staticValue>` non parsés (zones texte) | Non implémenté | `xml_parser.py` |
| `boxplot` / `network` → `tableEx` | Accepté — pas d'équivalent natif | `layout_parser.py` |
| LLM peut mapper `color→Details` au lieu de `Series` | Prompt corrigé, re-run LLM requis | `viz_translation.py` |
| `bubble` IBM sans slot `categories` → 1 point si pas de Details | Wiring LLM à améliorer | `viz_translation.py` prompt |
