Voici le Markdow récapitulatif de l'ensemble des spécifications de la pipeline de migration Cognos vers Power BI :

```markdown
# Spécifications Fonctionnelles et Techniques : Pipeline de Migration Cognos vers Power BI

## 1. Objectif
Concevoir une pipeline automatisée en Python permettant de convertir un rapport IBM Cognos (format XML v17.5) en un rapport Power BI (format projet `.pbip`, version Avril 2026), en s'appuyant sur des fichiers CSV bruts comme source de données. L'approche est **hybride** : déterministe pour la structure et le formatage, et assistée par LLM (GPT-4o) pour la traduction logique complexe.

---

## 2. Inputs & Outputs

### Inputs
*   **Fichier XML Cognos :** Spécification de rapport (contient le layout, le style CSS, la logique de filtres, et les requêtes/mesures).
*   **Fichiers CSV (Plats) :** Données brutes non pré-agrégées (ex: une ligne par date/compte). *L'aggrégation (MTD, QTD, YTD) doit être recréée dans Power BI.*

### Output
*   **Fichier `.pbip` :** Projet Power BI (format dossier compressé) contenant :
    *   Des fichiers **TMDL** pour le modèle sémantique (Tables, Dimensions, Mesures DAX).
    *   Un fichier **JSON** pour le layout du rapport (Visuals, Matrices, Formatage, Signets).

---

## 3. Architecture Générale (Logique Hybride)
La pipeline refuse l'approche "Tout LLM" pour éviter la perte de contexte, les halluccinations et la non-reproductibilité. Elle est divisée en 3 phases :

1.  **Phase 1 : Parseur Déterministe (0 appel LLM)** - Extraction et nettoyage du XML.
2.  **Phase 2 : Traduction LLM Ciblée (3 appels LLM)** - Traduction de la logique métier Cognos en DAX.
3.  **Phase 3 : Générateur Déterministe (0 appel LLM)** - Assemblage des fichiers TMDL et JSON.

---

## 4. Spécifications du Parseur Déterministe (Python)
Utilise `xml.etree` ou `lxml` (XPath) pour extraire un JSON intermédiaire (AST) sans perte sémantique.

*   **Extraction des Paramètres (Slicers PBI) :** Identification des prompts (ex: `?Region?`, `?p_timeview?`, `?_as_of_date?`) et de leurs cascades (`Year` -> `Quarter` -> `Month`).
*   **Extraction des Layouts :** Identification des objets visuels (ex: `Crosstab1`, `Crosstab2`) et de leur hiérarchie (Rows, Columns, Members).
*   **Extraction des Expressions :** Isolement des blocs `<expression>` contenant du code Cognos (CASE WHEN, fonctions de dates).
*   **Extraction des Styles (CSS) :** Récupération exacte des propriétés visuelles (couleurs de fond, bordures, padding, polices).

---

## 5. Stratégie et Ségrégation des Appels LLM (GPT-4o)
Pour garantir la traçabilité et limiter les coûts, les appels LLM sont cloisonnés par type de complexité logique.

### Call 1 : "Time Intelligence Foundation"
*   **Input :** Schéma des fichiers CSV bruts (noms des colonnes de dates et de valeurs).
*   **Mission :** Générer les mesures DAX de base pour recréer les agrégations manquantes (ex: `MTD`, `QTD`, `YTD`, `Prior_MTD`).
*   **Output :** JSON contenant le code DAX des mesures de base (ex: `CALCULATE(SUM(...), DATESMTD(...))`).

### Call 2 : "Parameter Switching Logic"
*   **Input :** Expressions Cognos contenant les `CASE WHEN ?p_timeview?...` + Résultat du Call 1.
*   **Mission :** Traduire la logique de paramètres dynamiques Cognos en DAX. Dans PBI, cela implique de créer des mesures utilisant `SWITCH(TRUE(), SELECTEDVALUE(...))` interagissant avec des tables de paramètres déconnectées.
*   **Output :** JSON contenant le code DAX des mesures métier finales (`[Actual]`, `[Budget]`, `[Prior_Actual]`, etc.).

### Call 3 : "Variance & Formatting Rules"
*   **Input :** Noms des mesures générées au Call 2 + Règles de formatage conditionnel XML.
*   **Mission :** Calculer les variances (`[Actual] - [Budget]`, `DIVIDE(...)`) et générer des mesures DAX qui retournent des codes Hexadécimaux (ex: `"#25CAC8"`) pour les couleurs conditionnelles dynamiques basées sur les valeurs des comptes.
*   **Output :** JSON contenant le DAX des variances et les règles de couleurs HEX.

---

## 6. Spécifications de Génération Déterministe (.pbip)

### 6.1. Génération du Modèle Sémantique (TMDL)
*   Création des fichiers de définition de tables.
*   Injections strictes des blocs DAX générés par le LLM dans les fichiers `measures.tmdl`.
*   Création des tables déconnectées pour gérer les paramètres (Remplacement des `?param?` Cognos).

### 6.2. Génération du Rapport Visuel (JSON)
*   **Fidélité Cosmétique (100% déterministe) :** Mapping direct des CSS Cognos vers le JSON Power BI.
    *   *Exemple :* `border-top:1pt solid black` devient `"borderTop": {"style": "solid", "width": 1, "color": "#000000"}`.
    *   Gestion exacte des padding (ex: `padding-left:25px` pour la hiérarchie des comptes de niveau 2).
*   **Fidélité Logique d'Affichage (100% déterministe) :** Remplacement du système `conditionalRender` de Cognos par le système de **Signets (Bookmarks)** et **Panneaux de sélection (Selection Panes)** de Power BI.
    *   *Exemple :* Si `Crosstab1` a `refVariable="FV"`, le générateur crée deux matrices dans le JSON, masque l'une par défaut, et crée un bouton de signet pour basculer entre elles.

---

## 7. Observabilité et Tracking (Langfuse)
Tous les appels LLM doivent être instrumentalisés avec **Langfuse** pour permettre le débogage fin et le suivi des performances/coûts.

*   **Trace Principale :** `Cognos_to_PBI_Migration` (Englobe tout le processus).
*   **Spans (Sous-étapes tracées) :**
    1.  `Deterministic_Parser` (Log les paramètres et les visuels extraits).
    2.  `LLM_Time_Intelligence` (Log le schéma CSV en Input, le DAX généré en Output).
    3.  `LLM_Switch_Logic` (Log les expressions Cognos en Input, le DAX en Output).
    4.  `LLM_Variance_Formatting` (Log les règles de couleurs en Input, les mesures HEX en Output).
    5.  `PBI_Generator` (Log le statut de succès de la création des fichiers TMDL/JSON).
*   **Implémentation :** Utilisation des décorateurs `@observe` de l'SDK Python Langfuse et exécution de `langfuse.flush()` en fin de script.

---

## 8. Stack Technique
*   **Langage :** Python 3.10+
*   **Parsing XML :** `lxml` ou `xml.etree.ElementTree`
*   **LLM :** API OpenAI (Modèle : `gpt-4o`)
*   **Tracking LLM :** SDK `langfuse`
*   **Génération PBI :** Écriture de fichiers système (paths, json, texte) pour générer la structure `.pbip` standard de Power BI (Avril 2026).
```