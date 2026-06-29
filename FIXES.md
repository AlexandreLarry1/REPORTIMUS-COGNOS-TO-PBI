# Fixes appliqués — Migration Qlik → PBIP

## 1. Générateur PBIP (`3_pbip_generator/`)

| # | Erreur | Fix |
|---|--------|-----|
| G1 | `.pbip` référençait `dataset` au lieu de `report` | Ajout dossier `DFIN_Engagement.Report/` + `definition.pbir` liant au SemanticModel |
| G2 | `definition.pbism` absent | Ajout `{"version": "4.2"}` dans le SemanticModel |
| G3 | `model.tmdl` ligne 1 = `semantic model Model` (keyword invalide) | Remplacé par `model Model` |
| G4 | `compatibilityLevel` absent dans `model.tmdl` | Ajout `compatibilityLevel: 1700` + `defaultPowerBIDataSourceVersion: powerBI_V3` |
| G5 | Noms de colonnes avec apostrophe ex. `Type d'analyse` → TMDL invalide | `_tmdl_name()` : escape `'` → `''` dans les identifiants quotés |

## 2. Output LLM (`outputs/translation/translated_scripts.json`)

| # | Table | Erreur | Fix |
|---|-------|--------|-----|
| L1 | Engagement | `_{0}=key` — syntaxe M invalide (pas d'accès indexé sur record) | Remplacé par `_[KEY_EKKN_COOI_PORD] = key` |
| L2 | Engagement | `?{0}??{...}` — opérateurs inexistants en M | Remplacé par `if Table.RowCount(...) > 0 then ...{0}[col] else default` |
| L3 | Engagement | `MapOTP_OTP1` / `MapOTP_OTP2` — tables hallucin ées par le LLM | Remplacées par colonnes `null` |
| L4 | MapFMIOI | `in [MapFMIOI = ..., MapOTP_OTP1 = ...]` — retourne un Record, pas une Table | Neutralisé par stub |
| L5 | V1_Engagement | `[V1_Engagement.Type d'analyse]` — interprété comme chain access `.` au lieu d'un nom de colonne | Nécessite `[#"V1_Engagement.Type d'analyse"]` |
| L6 | Tous | M code trop complexe avec références croisées cassant le moteur M à l'ouverture | **Stubs `#table(cols, {})` sur tous les tables** pour débloquer l'ouverture |

## 3. Parseur Qlik (`1_qlik_parser/`)

| # | Erreur | Fix |
|---|--------|-----|
| P1 | `GetVariableList` retournait 0 variables | Remplacé par `CreateSessionObject` avec `qVariableListDef` + `GetLayout` |
| P2 | `GetTablesAndKeys` crashait sur `raw_keys` (strings pas dicts) | Suppression du fallback raw_keys, détection des associations par nom de champ présent dans >1 table |
| P3 | Associations = 0 (`qIsKey` toujours False dans ce modèle Qlik) | Détection par champ commun entre tables (modèle associatif Qlik) |

---

## État actuel

- PBIP s'ouvre dans Power BI Desktop ✅
- 5 tables déclarées dans le modèle ✅
- M code CSV généré (plus de stubs) — données réelles à charger via Refresh ✅
- Colonnes complexes dérivées (KeyCC, fusions) : `null` en attendant vraie logique ⚠️
- LLM M code original : trop bugué pour être utilisé directement ⚠️

---

## 4. Connecteur données (`export_qvd_to_csv.py`, `build_map_tables.py`, `patch_m_with_csv.py`)

| # | Problème | Fix |
|---|----------|-----|
| D1 | Package `qvd` : API `qvd.read()` inexistante | Utiliser `from qvd.qvd_reader import read` |
| D2 | Package `qvd` : pandas absent | `pip install pandas` |
| D3 | FMIOI.qvd = 5.2M lignes → CSV 1.5 GB, trop lourd pour PBI | Pré-calcul `MapFMIOI.csv` (2 cols, 1.9M lignes, 41 MB) via `build_map_tables.py` |
| D4 | Noms de colonnes M code (ASCII sans accents) ≠ `sourceColumn` TMDL (UTF-8 avec accents) | M code réécrit avec les noms Unicode exacts de l'extraction (`"Numéro de pièce"` etc.) |
| D5 | `COOI.BUDAT` = nombre entier Qlik (serial Excel) pas une date | Conversion M : `#date(1899,12,30) + #duration([_budat_num], 0, 0, 0)` |
| D6 | Colonnes dérivées complexes (KeyCC, fusions OI/OTP) non calculables depuis COOI seul | Ajoutées comme colonnes `null` pour ne pas bloquer le modèle |
| D7 | `MapOTP_OTP1`/`MapOTP_OTP2` hallucin ées par LLM → manquantes dans modèle | `MapOTP.csv` pré-calculé depuis PRPS.qvd, injecté dans le M code Engagement |
| D8 | `List.Contains({"22","24","60"}, ...)` dans f-string Python → évalué comme set Python `{'22', '24', '60'}` → "Jeton Literal attendu" en M | Doubler les accolades : `{{"22","24","60"}}` dans tous les littéraux de liste M à l'intérieur des f-strings |
| D9 | `[Type de pièce d'origine - engagement]` dans M code → apostrophe + tiret invalides en identifiant M non quoté → "Identificateur non valide" | Remplacer par `[#"Type de pièce d'origine - engagement"]` |

## 5. Pipeline LLM (`2_llm_assistant/`)

| # | Problème | Fix |
|---|----------|-----|
| LLM1 | `ApplyMap` traduit en `Table.SelectRows` dans `each` → O(n²), 45 GB sur 363K lignes | Règle dans `script_translation.txt` : `ApplyMap` → `Table.NestedJoin + Table.ExpandTableColumn` |
| LLM2 | `main_translate.py` n'injectait pas les relations détectées → LLM ignorait les jointures | `main_translate.py` charge `detected_relationships.json` et passe les relations par table à `translate_table()` |
| LLM3 | `_validate()` ne détectait pas le pattern O(n²) | Regex ajoutée : `Table.SelectRows` dans `Table.AddColumn each` → warning `confidence=0.5` |

## 6. Source substitution RESIDENT (`source_substitution.py`)

Fixes appliqués lors de l'intégration de V1_Engagement (table RESIDENT Engagement → query PBI).

| # | Erreur PBI | Source du problème | Localisation fix | Durable ? |
|---|-----------|-------------------|-----------------|-----------|
| R1 | `"Type de valeur" introuvable` | LLM avait déjà écrit `Source_raw = Csv.Document(COOI.csv)` au lieu d'un stub `#table` — pattern RESIDENT non détecté | `source_substitution.py` : RESIDENT handler étendu pour détecter le pattern 2-lignes `Source_raw / PromoteHeaders` en plus du stub `#table` | **Durable** — couvre les deux formes que le LLM peut générer |
| R2 | `Référence cyclique` | V1_Engagement définit une variable locale `Engagement = ...` qui masque la query PBI du même nom → `Source = Engagement` se référence lui-même | `source_substitution.py` : `rename_var_outside_strings()` détecte les variables locales qui masquent le nom RESIDENT et les renomme en `_Engagement` sans toucher aux littéraux de chaînes | **Durable** — générique pour tout RESIDENT dont la variable locale = nom du parent |
| R3 | Régression : noms de colonnes corrompus (`"_Engagement"` au lieu de `"Engagement"`) | Premier run du rename utilisait `re.sub` global sans protection des strings → renommait à l'intérieur des littéraux `"..."` | `source_substitution.py` : `restore_in_strings()` + `rename_var_outside_strings()` (regex alternant `"[^"]*"` et `\bname\b` pour ne substituer qu'hors-chaînes) ; step de restauration ajouté pour idempotence | **Durable** — pattern réutilisable pour tout rename d'identifiant M |
| R4 | `"REF_LIGNE" existe déjà` | Engagement produit déjà `REF_LIGNE` ; V1_Engagement (RESIDENT) tente un `Table.AddColumn(..., "REF_LIGNE", each [REF_LIGNE] & ...)` → doublon | `source_substitution.py` : `fix_self_referencing_add_column()` — détecte `AddColumn(src, "Col", each [Col] & ...)` et convertit en 3 étapes : add `_tmp_Col` → remove old → rename | **Durable** — s'applique à tout RESIDENT qui recalcule une colonne existante |
| R6 | `"Année-Numéro de pièce OR" existe déjà` | Engagement pré-calcule déjà les colonnes OI/OTP pour toutes les lignes ; `Table.ExpandTableColumn` sur le join OI tente de les recréer | `source_substitution.py` : `fix_expand_column_conflicts()` — insère un step `Table.RemoveColumns(src, List.Intersect({Table.ColumnNames(src), expandCols}))` avant chaque `ExpandTableColumn` sur table `_tmp*`, retirant uniquement les colonnes déjà présentes | **Durable** — générique pour tout RESIDENT dont la source pré-calcule des colonnes réutilisées dans les joins |
| R7 | Chargement extrêmement lent (O(n²)) | `List.Contains(list, val)` dans `Table.SelectRows each` = O(n×m) sur 363k lignes ; aggravé car V1_Engagement transporte toutes les colonnes d'Engagement (R5) | `source_substitution.py` : `fix_list_contains_antijoin()` — détecte les `XList = List.Distinct(...)` puis remplace chaque `Table.SelectRows(each not List.Contains(...))` par `Table.NestedJoin(..., JoinKind.LeftAnti)` + `Table.RemoveColumns` ; gère simple et double condition | **Durable** — générique pour tout RESIDENT avec ce pattern d'exclusion mutuelle |
| R5 | `"Contrat Engagement" introuvable` (et 15 autres) | LLM génère `Table.SelectColumns` sur des étapes intermédiaires en listant le schéma **final** de la table, incluant des colonnes pas encore ajoutées à ce step (ajoutées par des NestedJoin ultérieurs) | `source_substitution.py` : `strip_intermediate_select_columns()` — remplace chaque `VarN = Table.SelectColumns(prev, {...})` par `VarN = prev` (pass-through), résolvant les 16 colonnes manquantes d'un coup | **Patch partiel** — supprime l'enforcement de schéma intermédiaire ; acceptable car `Table.Combine` final gère les colonnes manquantes avec null. À terme : LLM ne devrait pas générer des SelectColumns sur des tables RESIDENT avant que tous les joins soient faits |

## Comment avancer

### Prochaines étapes prioritaires

1. **Ouvrir le PBIP et lancer un Refresh** — valider que les 363K lignes COOI chargent dans Engagement
2. **Corriger les colonnes complexes nulles** (`KeyCC`, fusions OI/OTP/CC) si nécessaire pour les rapports
3. **Remplacer CSV par SQL/SAP direct** quand la connexion SAP HANA est disponible — changer uniquement la ligne `Source =` dans chaque partition
4. **Valider le M code LLM via un validator** avant injection — ajouter une étape de vérification syntaxique dans `2_llm_assistant/` pour éviter les mêmes bugs au prochain run
