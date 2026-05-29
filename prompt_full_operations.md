# Generic Prompt — Qlik to Power BI (SQL Server) Migration

## What you are doing

You are migrating a Qlik Sense application to Power BI Desktop using pbi-tools.
You will receive 3 files as input. From these 3 files alone you will:
1. Understand the full data model
2. Understand the full report layout
3. Generate a Python pipeline script (`stage_data.py`)
4. Generate a pbi-tools project folder (`output_pbi.zip`)

You figure out everything from the files. There are no hardcoded tables, columns,
or visual layouts in this prompt. Every decision is derived from reading the inputs.

---

## Input files

| File | What it contains |
|------|-----------------|
| `script_qlik.txt` | Qlik Sense load script — source of truth for the data model |
| `visual_description.txt` | Qlik sheet/widget dump — source of truth for the report layout |
| `reference_pbi.zip` | An existing pbi-tools extracted project — source of truth for file format and encoding |

You will also receive two parameters:
- `--sql_server` : SQL Server instance name (e.g. `CH-7NVV014`)
- `--sql_database` : Database name (e.g. `InvestmentTracking`)

---

## PHASE 1 — Read and understand the reference zip

Open `reference_pbi.zip` and extract the following technical facts. Apply every
one of them without exception to every file you generate.

### 1a — TMDL encoding facts

1. **BOM**: Read the raw bytes of any `.tmdl` file. If the first 3 bytes are
   `EF BB BF`, every `.tmdl` you write must start with those bytes.

2. **Line endings**: Check whether the file uses `\r\n` or `\n`.
   Every `.tmdl` you write must use the same line ending.

3. **Culture**: Read `Model/model.tmdl` → `culture: <value>`. Use it in your `model.tmdl`.

4. **Compatibility level**: Read `Model/database.tmdl` → `compatibilityLevel: <value>`.
   Use it in your `database.tmdl`.

5. **DateTableTemplate**: Find the file in `Model/tables/` whose name starts with
   `DateTableTemplate`. Copy it unchanged into your output.

6. **`source =` indentation**: Open any table `.tmdl`, find the `source =` block.
   Note the exact tab count before `let` and before each step line.
   The working pattern is: `source =` at `\t\t`, `let` at `\t\t\t\t`,
   each step at `\t\t\t\t    ` (4 tabs + 4 spaces), `in` at `\t\t\t\t`.

### 1b — Report visual folder structure

7. **Files per visual**: Look at the visual folders. Note that the reference
   may be **inconsistent** — some visual folders in the reference can contain a
   stray `dataTransforms.json` while others (correctly) do not. **Trust this
   rule over the reference**: the mandatory set is exactly **4 files** per
   visual container: `config.json`, `query.json`, `visualContainer.json`,
   `filters.json`. **Never generate `dataTransforms.json`** — its presence
   causes parse errors in pbi-tools. If the reference contains one, ignore it;
   do not copy it forward.

8. **config.json structure**: Note the required `singleVisual` fields:
   `visualType`, `projections`, `prototypeQuery`, `objects: {}`,
   `drillFilterOtherVisuals: true`, `hasDefaultSort: true`.

9. **query.json structure**: Note the `SemanticQueryDataShapeCommand` with
   `Query` and `Binding`.

### 1c — Mandatory static files

10. **Enumerate every file in the reference zip**. Every file that is NOT a
    generated table `.tmdl`, page `section.json`, or visual
    `config.json / query.json / visualContainer.json / filters.json`
    must be **copied verbatim** into your output zip at the same relative path.

    The mandatory static files are:

    | File | Location |
    |------|----------|
    | `.pbixproj.json` | root |
    | `Version.txt` | root |
    | `DiagramLayout.json` | root |
    | `ReportMetadata.json` | root |
    | `ReportSettings.json` | root |
    | `Report/report.json` | Report/ |
    | `Report/config.json` | Report/ |
    | `Model/cultures/<culture>.tmdl` | Model/cultures/ |
    | `Model/tables/DateTableTemplate_*.tmdl` | Model/tables/ |
    | `StaticResources/SharedResources/BaseThemes/<theme>.json` | StaticResources/… |

    If the reference zip contains any additional file not in this list, copy it too.
    **Never omit a file that exists in the reference zip** unless it belongs to a
    table, page, or visual that you are replacing.

    **Copy byte-for-byte.** Static files must be copied as raw bytes — preserve
    the original BOM, line endings, and encoding exactly. Do **not** parse and
    re-serialize them (re-serializing JSON can strip the BOM, swap CRLF→LF, or
    reorder keys, any of which can break the project). The one exception is a
    stray `dataTransforms.json`, which is never copied (see item 7).

---

## PHASE 2 — Read and understand the Qlik script

Open `script_qlik.txt`. The file may contain multiple tab sections separated
by `///$tab`. Pick the **authoritative** section as follows:

1. For each tab, count its **CSV-sourced LOAD blocks only** — i.e. blocks of
   the form `[Table]: LOAD … FROM [$(vDataPath)file.csv]`. **Exclude**
   `Resident`, `MAPPING LOAD`, `CONCATENATE`, and `JOIN` blocks from the count;
   those are Qlik-internal and are handled separately (see 2c, 2d).
2. If one tab has strictly more CSV-sourced LOAD blocks, use it.
3. **If several tabs tie** (common — the same model copied across tabs with
   minor naming differences), break the tie with the visual description:
   choose the tab whose **column labels (`AS [...]`) match the field names in
   `visual_description.txt`**. Build the set of every dimension/measure field
   referenced by the visuals, and pick the tab where the most of those fields
   exist as `AS [...]` labels. The visuals are the ground truth for the report,
   so the model must be the one they actually resolve against.

   Example signal: if the visuals reference `[Valeur Marche (EUR)]`,
   `[Volatilite (%)]` and `[Annee-Mois]`, prefer the tab that spells the
   columns exactly that way over a tab that spells them
   `[Valeur Marché EUR]`, `[Volatilité (%)]`, `[Année-Mois]`.

Use this single chosen tab as the source of truth for the entire data model.

### Step 2a — Extract each table

For each block matching:
```
[TableName]:
LOAD
    ...
FROM [$(vDataPath)filename.csv]
(... options ...)
[WHERE condition];
```

Extract:
- **Table name** (identifier before the colon)
- **CSV filename** from the FROM clause. The SQL view name is **`vw_` +
  the lowercase Qlik table name** (the identifier before the colon),
  e.g. `Dim_Clients` → `vw_dim_clients`, `Fact_Positions` → `vw_fact_positions`.
  Because Qlik table names already carry their `Dim_`/`Fact_` prefix, do **not**
  prepend the schema again — `vw_dim_dim_clients` is wrong. The view lives in
  the `[dim]` or `[fact]` schema, so its full name is `[dim].[vw_dim_clients]`.
- **Every column**, categorised as:
  - *Renamed*: `source_col AS [Target Name]`
  - *Computed*: any expression with `If(`, `Num#(`, `Date#(`, `Year(`, `Month(`,
    `ApplyMap(`, `&`, `+`, `-`, `*`, `/`
  - *Pass-through*: bare column name with no AS clause
- **WHERE clause** if present

Skip `LOAD ... Resident [...]` blocks — those are Qlik-internal.

**MasterCalendar special rule**: The `[MasterCalendar]` table is built from
`Resident [Dim_Calendrier]` in Qlik. In Power BI, expose it as a **physical table**
`[dim].[MasterCalendar]` (materialized by `stage_data.py`), not a view. Its TMDL
partition query reads `SELECT * FROM [dim].[MasterCalendar]` (no `vw_` prefix).

**Fact_Cours special rule**: If `Fact_Cours` (cours historiques) appears in the
Qlik script but none of the visuals reference any of its columns, **omit it**
from the Power BI model entirely. An unrelated table with no visual usage creates
noise and risks an extra Date relationship.

**Orphan-field remap rule**: After deciding which tables to omit, check every
field used by a visual against the columns of the tables you kept. If a visual
references a field whose **only** owner is an omitted table (e.g. a `Sum([Volume])`
KPI where `Volume` lived only in the omitted `Fact_Cours`), do not break the
visual and do not silently drop the omitted table back in. Instead remap the
field to the **closest equivalent column in a kept table** on the same page —
typically a same-grain quantity/value column on the page's primary fact (e.g.
`Volume` → `Fact_Positions[Quantite Position]` on a positions page). Record each
such remap so the field resolves cleanly in the visual's `prototypeQuery`. If no
reasonable equivalent exists, drop only that one measure from the visual rather
than reintroducing the omitted table.

### Step 2b — Translate computed columns to T-SQL

| Qlik pattern | T-SQL translation |
|---|---|
| `Date#(col, fmt)` | `TRY_CAST([col] AS DATE)` |
| `Num#(col)` | `TRY_CAST([col] AS FLOAT)` |
| `Year(Today()) - Year(expr)` | `YEAR(GETDATE()) - YEAR(<translated_expr>)` |
| `a & ' ' & b` | `CONCAT([a], ' ', [b])` |
| `If(condition, val_true, val_false)` | `CASE WHEN <cond> THEN <true> ELSE <false> END` |
| `col >= 0` inside If | `TRY_CAST([col] AS FLOAT) >= 0` |
| `ApplyMap('Map', col, default)` | `CASE [col] WHEN … THEN … ELSE default END` |
| `colA - colB` (numeric) | `TRY_CAST([colA] AS FLOAT) - TRY_CAST([colB] AS FLOAT)` |
| `dateA - dateB` (date) | `DATEDIFF(DAY, TRY_CAST([dateB] AS DATE), TRY_CAST([dateA] AS DATE))` |

Always use `TRY_CAST`, never `CAST`.

### Step 2c — Extract MAPPING LOAD INLINE tables

Find all `MapName: MAPPING LOAD * INLINE [...]` blocks and convert to
`CASE … WHEN … THEN … ELSE … END` expressions used wherever
`ApplyMap('MapName', …)` appears.

### Step 2d — MasterCalendar

The `[MasterCalendar]` block in Qlik loads from `Resident [Dim_Calendrier]`.
Its columns become the physical `[dim].[MasterCalendar]` table.
Ensure these computed columns are present:
- `CONCAT('T', CEILING(CAST([Mois] AS FLOAT)/3))` → `Trimestre Label`
- `CONCAT('S', CASE WHEN [Mois] <= 6 THEN 1 ELSE 2 END)` → `Semestre Label`
- `CONVERT(VARCHAR(7), [Date], 120)` → `AAAA-MM`

### Step 2e — Star schema and relationships

Classify each table:
- **Dimension**: name starts with `Dim_`, or descriptive-only columns
- **Fact**: name starts with `Fact_`, or has numeric measures + FK columns

Identify relationships from shared column names:
- `XxxID` in fact → same column as key in a dimension → relationship
- `[Date]` in facts → links to `MasterCalendar.[Date]`

**Ambiguous path rule**: If a fact table has `ClientID` AND a path already
exists via another table (e.g. fact → Dim_Portefeuilles → Dim_Clients),
do NOT add a direct fact → Dim_Clients relationship.

**Relationship naming**: Use descriptive names, not UUIDs.
Format: `<FactTable>_<DimTable>`, e.g. `Positions_MasterCalendar`,
`Portefeuilles_Clients`. This is required — UUID names cause issues.

---

## PHASE 3 — Read and understand the visual description

Open `visual_description.txt`. Parse every sheet and widget.

### Parsing

- `📄 Sheet: 'Page Name'  (id=sheet_id)` → new page
- `🔲 Widget id: widget_id` → new visual
- `type      :` → visual type
- `position  :` → look for `px(x, y) WxH` — percentages × 1280 / 720 for pixels
- `dimensions:` → lines starting `- [` are dimension fields
- `measures  :` → lines starting `- ` (not `- [`) are measure expressions

### Visual type mapping

| Qlik type | Power BI visualType |
|---|---|
| kpi | card |
| piechart | pieChart |
| barchart | barChart |
| linechart | lineChart |
| combochart | lineChart |
| scatterplot | scatterChart |
| table | tableEx |
| pivot-table | pivotTable |
| filterpane | slicer |
| waterfallchart | waterfallChart |
| mekkochart | treemap |

### Measure expression → aggregation function

Translate the Qlik aggregation verb to the Power BI `Function` integer:

| Qlik expression prefix | PBI Function | Name prefix in Select |
|---|---|---|
| `Sum(…)` | 0 | `Sum(alias.col)` |
| `Avg(…)` | 1 | `Avg(alias.col)` |
| `Min(…)` | 3 | `Min(alias.col)` |
| `Max(…)` | 4 | `Max(alias.col)` |
| `Count(Distinct …)` | 5 | `CountNonNull(alias.col)` |
| `Count(…)` (non-distinct) | 5 | `CountNonNull(alias.col)` |

**Count(Distinct X)** always maps to `Function: 5` (CountNonNull) and the
`Name` is `CountNonNull(alias.col)`. Never use `Function: 2` for counts.

### Composite / ratio measures

A Power BI `prototypeQuery` `Select` entry can encode a **single aggregation of
a single column** — it cannot encode arbitrary arithmetic. When a Qlik measure
is a composite expression (a ratio, sum of two aggregations, etc.), such as
`Sum([Valeur Marche (EUR)]) / Count(Distinct ClientID)` or
`Count(Distinct PortefeuilleID) / Count(Distinct ClientID)`:

- Take the **first (leading) aggregation** in the expression and use it as the
  projection — its function integer, its column, and its `Name`. This keeps the
  visual valid and field-resolvable.
- Treat a leading set-analysis aggregation
  (`Count({<Severite={'Critique'}>} AlerteID)`) the same way: strip the
  `{< … >}` modifier, keep the aggregation verb and the column (`AlerteID`).
- Do **not** attempt to emit the division/arithmetic in the query. If the exact
  ratio is required, it belongs in a DAX measure created in Power BI after
  import — note this in the output summary, do not fabricate it in JSON.

This rule is mandatory: emitting two aggregations joined by `/` in a single
`Select` entry, or inventing a `Divide` node, produces an invalid visual.

### Table alias convention

Assign a **short, meaningful, consistent alias** to each table.
Derive it from the table name — not just the first 2 characters of a random string.
Use the same alias for the same table across all visuals on all pages.

Recommended aliases (adapt to whatever tables exist in the model):

| Table | Alias |
|---|---|
| MasterCalendar | mc |
| Dim_Clients | dc |
| Dim_Conseillers | dcon |
| Dim_Instruments | di |
| Dim_Portefeuilles | dp |
| Fact_Positions | fp |
| Fact_Performances | fperf (when both Positions and Performances appear in same visual) |
| Fact_Transactions | ft |
| Fact_Alertes | fa |

When a visual uses only one fact table that could collide, use the shorter form.
When two fact tables appear in the same visual, disambiguate (e.g. `fp` vs `fperf`).

**If the same `Name` would appear twice in one Select array** (e.g. two separate
`CountNonNull(fa.AlerteID)` entries), append a numeric suffix to the second one:
`CountNonNull(fa.AlerteID)2`, `CountNonNull(fa.AlerteID)3`, etc.

### Select `Name` and `queryRef` format

- **Dimension column**: `Name` = `"alias.ColumnName"`, `queryRef` = `"alias.ColumnName"`
- **Aggregated measure**: `Name` = `"FuncName(alias.ColumnName)"`,
  `queryRef` = `"FuncName(alias.ColumnName)"`
- `NativeReferenceName` = bare column name with no alias prefix, e.g. `"Volatilite (%)"`

### Projection roles per visual type

| Visual type | Dimension roles | Measure roles |
|---|---|---|
| card | — | `Values` |
| pieChart | `Category` (first dim, `active: true`) | `Y` |
| barChart | `Category` (first dim, `active: true`), `Series` (second dim if present) | `Y` |
| lineChart | `Category` (first dim, `active: true`) | `Y` |
| scatterChart | `Details` (dim, `active: true`) | `X`, `Y`, `Size` |
| tableEx | `Values` (all dims, first has `active: true`) | `Values` |
| pivotTable | `Rows` (first dim, `active: true`), `Columns` (second dim) | `Values` |
| waterfallChart | — | `Y` |
| treemap | `Category` (first dim, `active: true`), `Details` (second dim) | `Values` |
| slicer | `Values` (dim if present) | — |

**`active: true`** must be added to the **first** dimension entry in its role.
Measure entries never have `active`.

### Field → table mapping

For each field name in a visual, look it up against the column names extracted
in Phase 2. The table that owns the column is the table to reference in `From`.
If a dimension column (e.g. `Annee-Mois`) belongs to `MasterCalendar`, use
alias `mc` and entity `MasterCalendar`.

### Visual `name` field

The `name` field in `config.json` must be a **meaningful short identifier**
derived from the visual type and content, not the raw Qlik widget id.
Format: `<type_abbrev><ordinal>_<topic>`, e.g. `kpi01_aum`, `pie02_vsbench`,
`bar01_classe`, `pvt01_perf`, `sc01_positions`.

---

## PHASE 4 — Generate stage_data.py

### Configuration

Read from `.env` using `python-dotenv`. Required keys: `SQL_SERVER`,
`SQL_DATABASE`, `SQL_USERNAME`, `SQL_PASSWORD`, `SQL_DRIVER`, `DATA_PATH`.

### Python timestamp

Use `pd.Timestamp.now('UTC').isoformat()` for the `_stg_loaded_at` field.
**Never use** `pd.Timestamp.utcnow()` — it is deprecated in Pandas 4 and will
be removed in a future version.

### SQL view naming convention

All views use the prefix `vw_` followed by the lowercase **Qlik table name**
(which already includes its `dim_`/`fact_` prefix). Do **not** add the schema a
second time:
- Dimension views: `[dim].[vw_dim_clients]`, `[dim].[vw_dim_instruments]`, …
- Fact views: `[fact].[vw_fact_transactions]`, `[fact].[vw_fact_positions]`, …
- MasterCalendar is a **physical table** `[dim].[MasterCalendar]`, not a view.

(`vw_dim_dim_clients` — doubling the prefix — is a bug.)

### Staging step

`stage_csv_files(engine)` loads every CSV as raw strings (`dtype=str`) into
`[stg]` with `if_exists='replace'`. Adds `_stg_loaded_at` and `_stg_source_file`.
**Returns** the set of staging table names successfully created.

### View creation

`create_views(engine, staged)` receives the staged set.
- **Skip** any view whose source staging table is absent; emit a WARNING log.
- Uses `CREATE OR ALTER VIEW [schema].[vw_name]`
- Applies all column renames, computed translations, and WHERE filters from Phase 2.

### Physical table materialization (`SELECT … INTO`)

`create_computed_tables(engine, staged)` creates any table that must be
materialized as a physical SQL Server table (e.g. `[dim].[MasterCalendar]`).

**`SELECT … INTO` always produces nullable columns in SQL Server regardless of
the source expression or data content. This is a SQL Server engine constraint,
not a data issue. A PRIMARY KEY cannot be defined on a nullable column (error 8111).**

The mandatory 3-step pattern for **every** physical table created via `SELECT … INTO`
is:

```sql
-- Step 1: materialize
SELECT <columns>
INTO [schema].[TableName]
FROM <source>;

-- Step 2: make the PK column NOT NULL (required before any PRIMARY KEY)
ALTER TABLE [schema].[TableName] ALTER COLUMN [PKColumn] <datatype> NOT NULL;

-- Step 3: add the PRIMARY KEY
ALTER TABLE [schema].[TableName] ADD CONSTRAINT PK_TableName PRIMARY KEY ([PKColumn]);
```

**Apply this 3-step pattern to every table materialized with `SELECT … INTO`,
not just MasterCalendar. Never emit the `ADD CONSTRAINT` step without the
preceding `ALTER COLUMN … NOT NULL` step. Omitting step 2 always causes
SQL Server error 8111 at runtime.**

Each step must be executed as a separate statement. Do not combine them.

### Verification

`verify_results(engine)` runs `SELECT COUNT(*) FROM` on every staging table,
view, and computed table. Logs row counts.

### Execution order

```
1. create_schemas(engine)
2. staged = stage_csv_files(engine)
3. create_views(engine, staged)
4. create_computed_tables(engine, staged)
5. verify_results(engine)
```

---

## PHASE 5 — Generate the pbi-tools project

### Table TMDL files

**Naming**: The PBI table name matches the Qlik table name exactly
(e.g. `Dim_Clients`, `Fact_Positions`, `MasterCalendar`).

**Partition query** — use `vw_` prefix for all tables except MasterCalendar:
```m
let
    Source = Sql.Database("SQL_SERVER", "SQL_DATABASE",
             [Query="SELECT * FROM [schema].[vw_schema_tablename]"])
in
    Source
```
For MasterCalendar: `SELECT * FROM [dim].[MasterCalendar]` (no `vw_`).

**Column definitions**:
- `dataType` inference:
  - `Date#(…)` expression → `dateTime`, `formatString: Long Date`,
    **add** `annotation UnderlyingDateTimeDataType = Date`
  - `Num#(…)` + name contains `%` → `decimal`, `formatString: 0.00%`
  - `Num#(…)` + name suggests money → `decimal`, `formatString: #,0.00 "EUR"`
  - `Num#(…)` + name suggests integer count → `int64`, `formatString: 0`
  - `Num#(…)` other → `decimal`
  - Text/ID → `string`
- `summarizeBy`:
  - Totals (monetary, volume) → `sum`
  - Rates, averages, percentages → `average`
  - Minimums (e.g. drawdown) → `min`
  - ID and text columns → `none`
- `isKey` on the first ID column of each dimension table
- `sourceColumn` = exact column name the SQL view returns (no brackets)
- Every column annotation line is `annotation SummarizationSetBy = Automatic`

**Column block format** (one blank line between annotation lines is wrong —
annotations go consecutively under the column):
```
\tcolumn 'ColumnName'
\t\tdataType: string
\t\tsummarizeBy: none
\t\tsourceColumn: ColumnName

\t\tannotation SummarizationSetBy = Automatic
```
For date columns add the extra annotation on the line immediately after:
```
\t\tannotation SummarizationSetBy = Automatic
\t\tannotation UnderlyingDateTimeDataType = Date
```

**Partition block**:
```
\tpartition TableName = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database("SERVER", "DB", [Query="SELECT * FROM [schema].[view]"])
\t\t\t\tin
\t\t\t\t    Source
```

**Closing annotation** (mandatory, on its own line after the partition):
```
\tannotation PBI_ResultType = Table
```

### model.tmdl

Include these exact annotations:
```
annotation __PBI_TimeIntelligenceEnabled = 1
annotation PBI_QueryOrder = ["MasterCalendar", <dim tables…>, <fact tables…>]
```
List `MasterCalendar` first in both `PBI_QueryOrder` and `ref table` entries,
followed by dimensions, then facts.

### database.tmdl

```
database <sql_database_name>
\tcompatibilityLevel: 1600
```

### relationships.tmdl

Use **descriptive names** (never UUIDs). Format: `FactTable_DimTable` or
`DimTable_DimTable` for dimension hierarchies.
```
relationship Positions_MasterCalendar
\tfromColumn: Fact_Positions.Date
\ttoColumn: MasterCalendar.Date
```
No quotes around column references. No comments.

### Report pages

For each sheet from Phase 3, create `Report/sections/NNN_page_name/`:
- **Ordinal** follows the natural reading order of the sheets as they appear
  in `visual_description.txt` (0, 1, 2, …). The folder prefix `NNN` is the
  ordinal zero-padded to 3 digits (`000`, `001`, …).
- `page_name` in the folder is the sheet's display name. If the name contains a
  path separator (`/`), URL-encode it as `%2F` in the **folder name** (the
  reference does this, e.g. `001_S%2F4`), but keep the unescaped name in
  `section.json`'s `displayName`.
- `section.json` fields: `displayName` (exact original sheet name, accents and
  spaces preserved), `displayOption: 1`, `height: 720`, `name` (original sheet
  id, verbatim), `ordinal`, `width: 1280`.

### Visual container folders

Folder name: `NNNNN_visualType (shortId)` where `shortId` is the first 5 chars
of the widget id from the visual description. The `NNNNN` prefix follows the
reference's stepping (the reference increments by 1000 per visual within a
page: `00000`, `01000`, `02000`, …) — match whatever stepping the reference
uses.

**Each folder contains exactly 4 files**:

#### `visualContainer.json`
```json
{
  "height": <px>,
  "tabOrder": <ordinal>,
  "width": <px>,
  "x": <px>,
  "y": <px>,
  "z": <ordinal>
}
```

#### `config.json`
```json
{
  "name": "<meaningful_name>",
  "layouts": [{"id": 0, "position": {"x":…,"y":…,"width":…,"height":…,"z":…,"tabOrder":…}}],
  "singleVisual": {
    "visualType": "<type>",
    "projections": {
      "<Role>": [{"queryRef": "<Name_value>", "active": true}, …]
    },
    "prototypeQuery": {
      "Version": 2,
      "From": [{"Name": "<alias>", "Entity": "<TableName>", "Type": 0}],
      "Select": [
        {
          "Column": {"Expression": {"SourceRef": {"Source": "<alias>"}}, "Property": "<col>"},
          "Name": "<alias>.<col>",
          "NativeReferenceName": "<col>"
        },
        {
          "Aggregation": {
            "Expression": {"Column": {"Expression": {"SourceRef": {"Source": "<alias>"}}, "Property": "<col>"}},
            "Function": <int>
          },
          "Name": "<FuncName(alias.col)>",
          "NativeReferenceName": "<col>"
        }
      ]
    },
    "drillFilterOtherVisuals": true,
    "hasDefaultSort": true,
    "objects": {}
  }
}
```

#### `query.json`
Wrap the identical `prototypeQuery` in:
```json
{
  "Commands": [{
    "SemanticQueryDataShapeCommand": {
      "Query": <prototypeQuery>,
      "Binding": {
        "Primary": {"Groupings": [{"Projections": [0,1,…]}]},
        "DataReduction": {"DataVolume": 3, "Primary": {"Top": {}}},
        "Version": 1
      },
      "ExecutionMetricsKind": 1
    }
  }]
}
```

#### `filters.json`
```json
[]
```

---

## PHASE 6 — Quality checks before output

1. **No ambiguous relationships**: For every table pair (A, B), at most one
   active relationship path. Remove any that create a second path.

2. **No `//` comment lines** in any `.tmdl` file.

3. **`in` keyword indentation**: `in` inside `source =` must be at `\t\t\t\t`
   (4 tabs exactly). Verify every table file.

4. **`annotation PBI_ResultType = Table`**: Present at the end of every table `.tmdl`.

5. **`SELECT … INTO` PK safety**: For every physical table created via
   `SELECT … INTO`, confirm the generated code contains the full 3-step sequence
   in this exact order:
   1. `SELECT … INTO [schema].[TableName] …`
   2. `ALTER TABLE [schema].[TableName] ALTER COLUMN [PKColumn] <datatype> NOT NULL`
   3. `ALTER TABLE [schema].[TableName] ADD CONSTRAINT PK_TableName PRIMARY KEY ([PKColumn])`

   The `ALTER COLUMN … NOT NULL` step (step 2) is mandatory and must appear
   between the `SELECT INTO` and the `ADD CONSTRAINT`. Missing it causes SQL
   Server error 8111 at runtime. This applies to **every** materialized table,
   not just MasterCalendar.

6. **BOM + CRLF**: Every `.tmdl` starts with `EF BB BF` and uses `\r\n` line endings.

7. **SourceRef aliases consistent**: In every `config.json`, every
   `SourceRef.Source` value matches an alias defined in the `From` array.
   Aliases are never hardcoded to arbitrary values — they follow the convention
   in Phase 3 (table alias convention table).

8. **SQL backslash**: If the server name contains `\`, verify `.tmdl` bytes
   contain exactly one backslash (Python string escaping can double it).

9. **Exactly 4 files per visual**: `config.json`, `query.json`,
   `visualContainer.json`, `filters.json`. No `dataTransforms.json`.

10. **All static files present**: Cross-check the output zip manifest against
    the reference zip. Every file from Phase 1c must be present at its exact path.

11. **Relationship names are descriptive**: No UUID strings in `relationships.tmdl`.

12. **`active: true` on first dimension**: Every visual's first dimension
    projection entry has `"active": true`. Measure entries never have it.

13. **Duplicate Select Names**: If the same aggregation expression appears more
    than once in a Select array, suffix duplicates with `2`, `3`, etc.

14. **Composite measures reduced**: No `Select` entry contains arithmetic
    (`/`, `+`, `-`, `*`) or more than one aggregation. Each ratio/composite
    Qlik measure was reduced to its leading aggregation (see Phase 3).

15. **Every visual field resolves**: Each dimension and each (reduced) measure
    field maps to a column on a table that exists in the final model. No field
    points at an omitted table; orphan fields were remapped (see Phase 2a) or
    dropped. Every `SourceRef.Source` matches a `From` alias (already covered by
    check 7), and every `From.Entity` is a table present in `Model/tables/`.

16. **Generated JSON encoding**: Generated `.json` files are valid JSON, use the
    same line endings as the reference's generated JSON, and preserve non-ASCII
    characters (accents in page/visual names) rather than escaping or mangling
    them.

17. **Static files byte-identical**: Each copied static file is byte-for-byte
    identical to its counterpart in the reference zip (BOM, line endings,
    encoding unchanged). Re-serialized copies fail this check.

18. **Zip root folder**: The output zip uses a single top-level project folder
    (e.g. `output_pbi/`) containing the project, mirroring how the reference zip
    wraps its project in one root folder. Do not place project files at the zip
    root with no wrapper if the reference used one.

---

## PHASE 7 — Output

Produce exactly two files:

**`stage_data.py`**: Complete, runnable Python script. No placeholder comments,
no TODOs. Views named `vw_` + lowercase Qlik table name (e.g. `vw_dim_clients`,
`vw_fact_positions`) — never double the schema prefix (`vw_dim_dim_clients`).

**`output_pbi.zip`**: pbi-tools project folder:
```
output_pbi/
├── .pbixproj.json                              (copy from reference zip)
├── Version.txt                                 (copy from reference zip)
├── DiagramLayout.json                          (copy from reference zip)
├── ReportMetadata.json                         (copy from reference zip)
├── ReportSettings.json                         (copy from reference zip)
├── StaticResources/
│   └── SharedResources/
│       └── BaseThemes/
│           └── <theme>.json                    (copy from reference zip)
├── Model/
│   ├── database.tmdl
│   ├── model.tmdl
│   ├── relationships.tmdl
│   ├── cultures/
│   │   └── <culture>.tmdl                      (copy from reference zip)
│   └── tables/
│       ├── DateTableTemplate_*.tmdl            (copy from reference zip)
│       ├── MasterCalendar.tmdl                 (generated — physical table)
│       └── <one .tmdl per remaining table>
└── Report/
    ├── report.json                             (copy from reference zip)
    ├── config.json                             (copy from reference zip)
    └── sections/
        └── <NNN_page_name>/
            ├── section.json
            ├── config.json          (= {} or [])
            ├── filters.json         (= [])
            └── visualContainers/
                └── <NNNNN_type (id5)>/
                    ├── config.json          (generated)
                    ├── query.json           (generated)
                    ├── visualContainer.json (generated)
                    └── filters.json         (= [])
```

> **Final zip checklist** — before zipping, diff your output manifest against
> the reference zip manifest. Any file from the reference that you have not
> explicitly replaced must still be present. Any visual folder that has anything
> other than exactly the 4 required files is a bug.

Compile and test with:
```bash
pbi-tools compile output_pbi/ -format PBIT -outPath output.pbit
```
