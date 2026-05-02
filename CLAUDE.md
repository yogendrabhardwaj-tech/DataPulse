# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Two complementary interfaces share the same validation engine (`check_datapulse.py`):

| Interface | File | When to use |
|---|---|---|
| **Streamlit Wizard** | `app.py` | Interactive, guided end-to-end flow — build mappings, run validation, view results in browser |
| **CLI Engine** | `check_datapulse.py` | Scripted/automated runs; takes folder paths as arguments or reads config block at top of file |

---

## Streamlit Wizard (`app.py`)

### Start the wizard
```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`. Six-step wizard:

| Step | What happens |
|---|---|
| 1. Data Ingestion | Select source/destination folder paths and CSV separators; preview data, column summaries |
| 2. Column Mapping | Pair source ↔ destination columns; interactive rule builder (all 10 rules + chaining); see distinct values per pair |
| 3. Value Mapping | For each VALUE_MAP column: auto-generate rows from distinct source values; destination value dropdown populated from dest data |
| 4. Review & Save | Final review tables; save `column_mapping.csv` and `value_mapping.csv` to `<base>/Mapping_Rule/`; in-browser download |
| 5. Run Validation | Config overrides (match mode, tolerance, separators, run mode); runs `validate()` from engine; captures log output |
| 6. Results | KPI metrics; filterable/searchable issues table; embedded interactive HTML dashboard; download buttons |

### Architecture
- `app.py` imports `validate()` directly from `check_datapulse.py` — zero duplication of engine logic.
- All in-app data operations use `pandas`; the engine itself still uses the stdlib `csv` module.
- Session state (`st.session_state`) drives wizard navigation; each step is a `_stepN()` function.
- `@st.cache_data` on the folder loader prevents redundant re-reads across reruns.
- The "Reset wizard" sidebar button clears all state and cache.

### Dependencies
```
streamlit>=1.28.0
pandas>=1.5.0
```

---

## CLI Engine (`check_datapulse.py`) — Quickest Way to Run

Edit the config block at the top of `check_datapulse.py` (lines ~36–52), then:

```bash
python check_datapulse.py
```

Open `output/dashboard.html` in any browser after running.

## Folder Structure Convention

Source and destination data live in **sub-folders** (`Source/` and `Destination/`) inside the base folder. All `.csv` files in each folder are merged automatically — no filename hardcoding needed.

```
Data_Folder_Template/
├── Source/                   ← drop source CSV file(s) here
│   └── cd_source.csv
├── Destination/              ← drop destination CSV file(s) here
│   └── cd_destination.csv
└── Mapping_Rule/
    ├── column_mapping.csv
    └── value_mapping.csv
```

## User Configuration Block (Top of check_datapulse.py)

All inputs are declared as variables at the top of the file — no CLI flags needed:

```python
folder_path     = "Data_Folder_Template"
SOURCE_FOLDER   = folder_path + "/Source"           # Folder containing source CSV file(s)
DEST_FOLDER     = folder_path + "/Destination"      # Folder containing destination CSV file(s)
COLMAP_PATH     = folder_path + "/Mapping_Rule/column_mapping.csv"
VALMAP_PATH     = folder_path + "/Mapping_Rule/value_mapping.csv"
OUTPUT_DIR      = "output"
OUTPUT_FORMAT   = "both"          # "json", "csv", or "both"
TOLERANCE       = 0.01
KEY_COLS        = None            # e.g. ["EmpID"] or None to use colmap is_key
MATCH_MODE      = "leftout"       # full | leftout | rightout | union
```

`folder_path` is a convenience variable so all paths share a common base.

CLI flags still work and override the config block values when passed.

## CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--source` | `SOURCE_FOLDER` | Source folder (all .csv files merged) |
| `--dest` | `DEST_FOLDER` | Destination folder (all .csv files merged) |
| `--colmap` | `COLMAP_PATH` | column_mapping.csv path |
| `--valmap` | `VALMAP_PATH` | value_mapping.csv path |
| `--outdir` | `OUTPUT_DIR` | Where to write output files |
| `--format` | `OUTPUT_FORMAT` | `json`, `csv`, or `both` |
| `--tolerance` | `TOLERANCE` | Numeric comparison tolerance |
| `--key-cols` | `KEY_COLS` | Override composite key columns |
| `--match-mode` | `MATCH_MODE` | Comparison scope (see below) |

## Match Modes (Asymmetric Join Semantics)

`MATCH_MODE` controls which records appear in the dashboard View Data tabs and which issue types are suppressed.

| Mode | SQL analogy | Source View Data | Destination View Data | Issues suppressed |
|---|---|---|---|---|
| `full` | FULL OUTER JOIN | All source rows | All destination rows | None |
| `leftout` | LEFT-primary | All source rows | Exact-match rows only | `KEY_MISSING_IN_SOURCE` |
| `rightout` | RIGHT-primary | Exact-match rows only | All destination rows | `KEY_MISSING_IN_DEST` |
| `union` | INNER JOIN | Exact-match rows only | Exact-match rows only | `KEY_MISSING_IN_SOURCE`, `KEY_MISSING_IN_DEST`, `MIGRATION_KEY_MISMATCH` |

**Asymmetric rule for MIGRATION_KEY_MISMATCH:**
- `leftout` / `union`: destination rows of MIGRATION_KEY_MISMATCH pairs are hidden (destination shows only exact matches).
- `rightout` / `union`: source rows of MIGRATION_KEY_MISMATCH pairs are hidden (source shows only exact matches).

Hidden row indices are computed from pre-filter `detail_rows` and stored in `summary.source_hidden_row_indices` / `summary.dest_hidden_row_indices`. The dashboard JS builds `_srcHidden` / `_dstHidden` Sets from these and filters `SRC_DATA.rows` / `DST_DATA.rows` in `showVD()` at render time.

## Mapping File Formats

### column_mapping.csv
```
source_column, destination_column, matching_rule, is_key
```
- `is_key` = `true` marks composite key columns (drives record matching)
- Column name matching is **case-insensitive and whitespace-tolerant** — `Emp_ID` in CSV matches `emp_id` in mapping
- `matching_rule` can be a standard code (see below) **or a human-readable description** — the engine detects the effective rule from the value_mapping content and rule string patterns

### value_mapping.csv
```
column_name, source_value, destination_value, rule_description
```
- `column_name` must match the `source_column` value in column_mapping.csv (case-insensitive)
- Supports two special uses in addition to value substitution:
  - **Date format spec**: entries whose `source_value` / `destination_value` are date format tokens (e.g. `YYYY-MM-DD` → `DD-MMM-YYYY`) define a date conversion rule without needing a `DATE_FORMAT:…` code in column_mapping.csv
  - `rule_description` is displayed in the dashboard Mappings tab; it is not used by the engine

### Supported `matching_rule` values

| Rule | Description |
|---|---|
| `DIRECT` | Trim + case-insensitive compare |
| `NUMERIC` | Float compare within `--tolerance` |
| `DECIMAL` | Exact value match ignoring trailing zeros — `1200.50 == 1200.5` but `1200.50 != 1200.51` |
| `VALUE_MAP` | Resolve source value via value_mapping.csv |
| `STRIP_PREFIX:<p>` | Remove leading prefix `p` before compare |
| `DATE_FORMAT:<src>-><dst>` | Convert date format (see tokens below) |

Rules can be chained with `|` e.g. `STRIP_PREFIX:C-|NUMERIC`

**Date format tokens:** `YYYY-MM-DD`, `DD-MMM-YYYY`, `MM/DD/YYYY`, `DD/MM/YYYY`, `YYYY/MM/DD`, `YYYYMMDD`, `DD-MM-YYYY`, `MM-DD-YYYY`

## Architecture

`check_datapulse.py` is a single-file engine with these layers:

1. **`_ci_get(row, col)` / `_ci_has(row, col)`** — module-level case-insensitive, whitespace-tolerant dict accessors used everywhere a CSV row column is looked up
2. **`apply_transformation(value, rule)`** — stateless rule parser; handles `STRIP_PREFIX`, `DATE_FORMAT`, `NUMERIC`, `DECIMAL`, `DIRECT`, chained with `|`. `NUMERIC` produces integer strings for whole numbers (`"001234"` → `"1234"`, not `"1234.0"`) so key matching works when destination stores integers.
3. **`_load_csv(path)`** — single-file CSV loader with header validation
4. **`_load_folder_csv(folder, label)`** — finds all `.csv` files in a folder, validates all have identical column headers (schema mismatch → `ValueError` with per-file diff), merges rows, returns `(headers, rows, loaded_paths)`. Raises `FileNotFoundError` if folder missing or empty.
4. **`_profile_column` / `_profile_dataset`** — per-column stats (type inference, nulls, distinct, min/max, top values, length stats, quality flags)
5. **`_column_value_profiles`** — per-column value-count comparison between source and destination using mode-filtered rows; applies transformations before counting so gaps reflect real data differences (three-path detection: date spec in val_mapping → value mapping → standard rule code)
6. **`_MODE_SUPPRESS`** — dict mapping each mode to a frozenset of issue types to remove from `detail_rows`
7. **`validate()`** — main pipeline: load → key index → duplicate check → match → partial-key pairing → compare → compute hidden indices → apply mode filter → profile → write outputs
8. **`_write_row_summary()`** — writes `row_match_summary.csv` with one row per source record and full column pass/fail detail
9. **`_generate_dashboard()`** — embeds all JSON data into a self-contained HTML file with vanilla JS (no external dependencies)

### Key Matching Logic
- Source key columns are **transformed** (e.g. `STRIP_PREFIX:E` turns `E101` → `101`) before indexing
- Destination key columns are used **as-is**
- Column name lookup is **case-insensitive** — `Emp_ID` in CSV matches `emp_id` in mapping
- Matching is: `transformed_source_key == destination_key`

### Migration Key Mismatch (Partial Key Matching)
When a full composite key has no match, the engine attempts **partial key pairing**:
- Unmatched source keys are compared part-by-part against unmatched destination keys
- Best partial match (highest number of matching key parts) is paired as `MIGRATION_KEY_MISMATCH`
- Paired records are reported as a **single issue** (not two separate missing-key issues)
- Truly unmatched keys with no partial match remain `KEY_MISSING_IN_DEST` / `KEY_MISSING_IN_SOURCE`

### Hidden Row Indices
Hidden row indices are computed **before** the mode-suppress filter removes issues from `detail_rows`:

```
leftout / union  → dst_hidden = KEY_MISSING_IN_SOURCE dest rows + MIGRATION_KEY_MISMATCH dest rows
rightout / union → src_hidden = KEY_MISSING_IN_DEST src rows + MIGRATION_KEY_MISMATCH src rows
full             → both hidden lists are empty
```

Stored in `summary.source_hidden_row_indices` and `summary.dest_hidden_row_indices` for the dashboard JS. Also used to compute `mode_src_rows` / `mode_dst_rows` for all profiling calls so profiling always matches what View Data shows.

### Column Value Profiling (_column_value_profiles)
Runs on `mode_src_rows` / `mode_dst_rows` (already mode-filtered). Applies transformations to source values before building value-count comparisons so that format differences (e.g. date format) do not create false gaps.

Three-path detection per column (handles standard rule codes and human-readable descriptions equally):
1. **Date format spec** — if val_mapping for this column contains a date-token pair entry (e.g. `YYYY-MM-DD` → `DD-MMM-YYYY`), build and apply the corresponding `DATE_FORMAT:…` rule
2. **Value mapping** — if val_mapping has regular entries for this column, OR the rule string contains `VALUE_MAP` or `VALUE MAPPING`, map source values via the lookup table
3. **Standard rule code** — otherwise call `apply_transformation(value, rule)` (works for `DATE_FORMAT:…`, `STRIP_PREFIX:…`, `NUMERIC`, `DECIMAL`, `DIRECT`)

### Outputs Written to `--outdir`

| File | Content |
|---|---|
| `datapulse_results.json` | `summary` object + `detail_rows` array |
| `datapulse_results.csv` | Flat CSV of issue rows only |
| `row_match_summary.csv` | **Every source row** with full column-by-column PASS/FAIL audit |
| `profiling.json` | Column profiles for source / destination / matched subsets + value_counts |
| `mappings.json` | Normalized column and value mapping rules |
| `dashboard.html` | Self-contained interactive dashboard (no server needed) |

### Issue Types

| Issue Type | Severity | Description |
|---|---|---|
| `KEY_MISSING_IN_DEST` | High | Source composite key not found in destination |
| `KEY_MISSING_IN_SOURCE` | Medium | Destination composite key not found in source |
| `MIGRATION_KEY_MISMATCH` | High | Partial key match found — one or more key parts differ (data migration discrepancy) |
| `DUPLICATE_KEY_SOURCE` | High | Duplicate composite key in source |
| `DUPLICATE_KEY_DEST` | High | Duplicate composite key in destination |
| `VALUE_MISMATCH` | Low | Matched row has column value difference |
| `NULL_MISMATCH` | Medium | One side is null, the other is not |
| `TRANSFORMATION_ERROR` | Medium | Rule failed to transform source value |
| `VALUE_MAPPING_MISSING` | Medium | Source value has no entry in value_mapping.csv |
| `COLUMN_MAPPING_MISSING` | Medium | Mapped source column not found in source data |

## Dashboard Tabs

| Tab | Description |
|---|---|
| **Summary** | KPIs, run info, source/destination records toggle, issues-by-type and by-column charts |
| **Issues** | Filterable/sortable issue table. Each row has an icon (ℹ info / ✕ error / ⚡ migration / ⚠ warning) with hover tooltip showing full mismatch detail. `MIGRATION_KEY_MISMATCH` rows show color-coded key-part comparison (green M = matched, red U = unmatched) |
| **Profiling** | Column-level statistics for source / destination / matched subsets. Sub-tabs: Source / Destination / Matched (Src) / Matched (Dst). Also includes a **Column Value Profiling** section showing side-by-side value counts after applying transformations. |
| **Mappings** | Column mapping and value mapping rule tables |
| **View Data** | Browse mode-filtered source or destination data. Composite key columns appear first (driven by `is_key=true` in colmap, case-insensitive). Row highlighting driven by issue status. Per-column filters, sortable headers, dynamic rows-per-page, pagination. Table height is auto (no vertical scrollbar). |

### Tab Bar Meta Badges
The right side of the tab bar always shows three badges (unaffected by match mode):
- **Source [N]** — original source CSV row count
- **Destination [N]** — original destination CSV row count
- **Mode LEFTOUT** — current match mode in uppercase

### Summary Tab Layout
Three equal-width cards in a `repeat(3,1fr)` CSS grid with `align-items:stretch` so all three reach the same height:
1. **Run Information** — key-value rows using `grid-template-columns:1fr 1fr` per row (scoped inline, does not affect `.ps` in Profiling)
2. **Issues by Type** — bar chart; `display:flex;flex-direction:column` + `flex:1` on chart div fills card height
3. **Value Mismatches by Column** — same flex pattern as Issues by Type

Below the three cards: a full-width **Source / Destination Records** card with a toggle button (`switchSP('src')` / `switchSP('dst')`). State variable `sp_mode` tracks current view. Rows-per-page selector built dynamically by `buildSPRpp()` based on data size. No vertical scrollbar (`max-height:none;overflow-x:auto;overflow-y:visible`).

### View Data — Row Highlight Legend

| Colour | Meaning |
|---|---|
| Red `rgba(239,68,68,0.18)` | Row exists in both but one or more column values differ |
| Green `rgba(34,197,94,0.18)` | Source key not found in destination (or MIGRATION src row) |
| Sky-blue `rgba(56,189,248,0.18)` | Destination key not found in source (or MIGRATION dst row) |
| No colour | Row matched and all column values passed |

View Data only shows rows permitted by the current match mode (`source_hidden_row_indices` / `dest_hidden_row_indices` are used to filter). Table wrapper uses `max-height:none;overflow-x:auto;overflow-y:visible` — height is determined by rows-per-page, no vertical scrollbar.

### Source/Destination Records Toggle (Summary Tab JS)
Key state and functions:
- `sp_mode` — `'src'` or `'dst'`, current view
- `switchSP(mode)` — swaps data source (`SRC_DATA` / `DST_DATA`), resets sort/page/rpp, re-renders
- `buildSPRpp()` — builds rows-per-page `<select>` options dynamically: `[10,25,50]` + `100/250/500/1000` when data is large enough + `0` (All)
- `applySPFilter()` — applies current sort, resets to page 1, calls `renderSP()`

## row_match_summary.csv Structure

One row per source record. Columns:

| Column | Example |
|---|---|
| `composite_key` | `308\|506\|2023` |
| `overall_result` | `PASS` / `FAIL` / `MISSING_IN_DEST` / `DUPLICATE_KEY_SOURCE` |
| `issues_count` | `1` |
| `{src_col}_source` | `899.90` |
| `{dst_col}_destination` | `899.99` |
| `{src_col}_match` | `PASS` / `VALUE_MISMATCH` / `NULL_MISMATCH` / `N/A` |

Non-key mapped columns repeat the three-column block in mapping file order.

## Using With a Different Schema

Replace the two mapping files. `check_datapulse.py` has zero schema-specific code:
1. Create a new base folder (e.g. `My_Dataset/`) with `Source/`, `Destination/`, and `Mapping_Rule/` sub-folders
2. Drop source CSV file(s) into `Source/` and destination CSV file(s) into `Destination/`
3. Set `is_key=true` for composite key columns in `column_mapping.csv`
4. Add value mappings for any code-lookup columns in `value_mapping.csv`
5. Set the appropriate `matching_rule` per column (standard codes or human-readable descriptions work)
6. Update `folder_path` in the config block

Column name casing differences between the mapping file and the CSV headers are handled automatically.

### Folder-based Merge Rules
- All `.csv` files in `Source/` are merged in filename-sort order
- All `.csv` files in `Destination/` are merged in filename-sort order
- All files in each folder **must have identical column headers** — the engine compares headers case-insensitively and reports per-file differences if they don't match
- If a folder is empty or doesn't exist, a clear error is printed and the run aborts

---

## Sample Datasets

### CD (Certificate of Deposit) — 20,000 rows

Generated by `generate_cd_data.py` (requires `pandas`, `numpy`). Run once to create the files:

```bash
python generate_cd_data.py
```

Files written to `cd_sample_20k/`:

| File | Description |
|---|---|
| `cd_source.csv` | 20,000 rows — 14 columns in source format |
| `cd_destination.csv` | 20,000 rows — 14 columns in destination format (codes, stripped keys) |
| `Mapping_Rule/column_mapping.csv` | Column mapping for CD dataset |
| `Mapping_Rule/value_mapping.csv` | Value mappings for CD dataset |

**To validate the CD dataset**, update the config block in `check_datapulse.py`:

```python
folder_path   = "Data_Folder_Template"   # already points here by default
SOURCE_FOLDER = folder_path + "/Source"
DEST_FOLDER   = folder_path + "/Destination"
COLMAP_PATH   = folder_path + "/Mapping_Rule/column_mapping.csv"
VALMAP_PATH   = folder_path + "/Mapping_Rule/value_mapping.csv"
OUTPUT_DIR    = "output"
MATCH_MODE    = "leftout"
```

The CD files are already placed in `Data_Folder_Template/Source/` and `Data_Folder_Template/Destination/`.

**CD schema transformations:**

| Source Column | Destination Column | Rule | Notes |
|---|---|---|---|
| `Customer_ID` | `cust_key` | `STRIP_PREFIX:CUST\|NUMERIC` | `CUST001234` → `1234` |
| `CD_Number` | `cd_key` | `STRIP_PREFIX:CD-\|NUMERIC` | `CD-1000001` → `1000001` — **key** |
| `Issue_Date` | `issue_dt` | `DATE_FORMAT:YYYY-MM-DD->DD-MMM-YYYY` | `2024-01-15` → `15-JAN-2024` |
| `Term_Months` | `term_mo` | `NUMERIC` | |
| `Principal_Amount` | `principal_amt` | `NUMERIC` | |
| `Interest_Rate` | `rate_pct` | `NUMERIC` | |
| `Interest_Payout_Frequency` | `payout_freq_cd` | `VALUE_MAP` | MONTHLY→M, QUARTERLY→Q, AT_MATURITY→A |
| `Rate_Type` | `rate_type_cd` | `VALUE_MAP` | FIXED→F, VARIABLE→V |
| `Auto_Renew` | `auto_renew_ind` | `VALUE_MAP` | YES→1, NO→0 |
| `Status` | `status_cd` | `VALUE_MAP` | ACTIVE→A, MATURED→M, CLOSED→C |
| `Branch_Code` | `branch_id` | `STRIP_PREFIX:BR-\|NUMERIC` | `BR-001` → `1` |
| `Currency` | `ccy_cd` | `DIRECT` | USD |
| `Interest_Accrual_Method` | `accrual_cd` | `VALUE_MAP` | 30_360→30360, ACT_365→ACT365 |
| `Brokered_Flag` | `brokered_ind` | `VALUE_MAP` | YES→1, NO→0 |
