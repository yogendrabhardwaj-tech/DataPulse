# Build a Python Data Validation Engine + Column Profiling + Interactive Dashboard

You are an expert Python engineer and front-end dashboard designer.
Create a complete, generalized CSV data validation engine in a single file `validate.py` that compares any source and destination CSV pair using mapping files and a composite key.
Generate output files and a polished self-contained interactive HTML dashboard with drill-down, profiling, rule visibility, and raw data browsing.

Do not ask questions. Make reasonable assumptions and implement everything described below.

---

## 1. Inputs

The program reads four CSV files. All paths are declared as variables at the top of the file (USER CONFIGURATION block) so users can change them without touching CLI flags:

```python
folder_path   = "Data_Student"           # Base folder shared by all CSV files
SOURCE_PATH   = folder_path + "/employee_source.csv"
DEST_PATH     = folder_path + "/employee_destination.csv"
COLMAP_PATH   = folder_path + "/Mapping_Rule/column_mapping.csv"
VALMAP_PATH   = folder_path + "/Mapping_Rule/value_mapping.csv"
OUTPUT_DIR    = "output"
OUTPUT_FORMAT = "both"    # "json", "csv", or "both"
TOLERANCE     = 0.01
KEY_COLS      = None      # list of source key column names, or None to use is_key from colmap
MATCH_MODE    = "leftout" # full | leftout | rightout | union
```

`folder_path` is a convenience variable so all four file paths share a common base without repeating it.

CLI flags (`--source`, `--dest`, `--colmap`, `--valmap`, `--outdir`, `--format`, `--tolerance`, `--key-cols`, `--match-mode`) still work and override the config block when passed.

### column_mapping.csv columns
- `source_column` — source CSV column name
- `destination_column` — destination CSV column name
- `matching_rule` — transformation/comparison rule (see Section 3). May be a standard rule code **or a human-readable description** — the engine detects the effective rule from value_mapping content.
- `is_key` — `true` / `false`; marks composite key columns

### value_mapping.csv columns
- `column_name` — matches `source_column` in column_mapping.csv
- `source_value` — original source value (or source date-format token for date conversion spec)
- `destination_value` — expected destination value after mapping (or destination date-format token)
- `rule_description` — human-readable label (displayed in dashboard, ignored by engine)

**Special use of value_mapping for date format spec:** A row whose `source_value` and `destination_value` are both date format tokens (e.g. `YYYY-MM-DD` and `DD-MMM-YYYY`) defines a date conversion rule. This allows column_mapping.csv to use a human-readable description instead of a `DATE_FORMAT:…` code.

---

## 2. Column Name Matching (Case-Insensitive)

All column lookups across source rows, destination rows, and mapping files must be **case-insensitive and whitespace-tolerant**.

Implement two module-level helpers used everywhere:
- `_ci_get(row, col)` — returns the value for `col` from `row` dict using case-insensitive key match; returns `''` if not found
- `_ci_has(row, col)` — returns `True` if `col` exists in `row` (case-insensitive)

This ensures `Emp_ID` in a CSV matches `emp_id` in the mapping file without requiring users to align casing.

---

## 3. Transformation Engine

### apply_transformation(value, rule)
Stateless, returns `(transformed_value, error_or_None)`. Rules can be **chained with `|`** e.g. `STRIP_PREFIX:C-|NUMERIC`.

| Rule | Behaviour |
|---|---|
| `DIRECT` | Trim whitespace only |
| `NUMERIC` | Parse as float, compare within tolerance |
| `DECIMAL` | Normalize trailing zeros using `Decimal.normalize()` + `format(..., 'f')` so `1200.50 == 1200.5` but `1200.50 != 1200.51` |
| `VALUE_MAP` | No-op in transformer — caller resolves via value_mapping.csv |
| `STRIP_PREFIX:<p>` | Remove leading prefix `p` (case-insensitive match) |
| `DATE_FORMAT:<src>-><dst>` | Convert date between named formats |

Supported date format tokens: `YYYY-MM-DD`, `DD-MMM-YYYY`, `MM/DD/YYYY`, `DD/MM/YYYY`, `YYYY/MM/DD`, `YYYYMMDD`, `DD-MM-YYYY`, `MM-DD-YYYY`

For `DECIMAL` comparisons, apply `Decimal.normalize()` to both source (transformed) and destination values before comparing. If parsing fails, fall back to string comparison.

### _effective_rule(src_col, rule, val_mapping)
Module-level helper called before `apply_transformation` in the comparison loops.

Checks if `val_mapping[src_col]` contains a **date-format spec entry** (an entry where both `source_value` and `destination_value` are date format tokens, e.g. `YYYY-MM-DD` and `DD-MMM-YYYY`). If so, returns the derived `DATE_FORMAT:<src>-><dst>` rule string. Otherwise returns `rule` unchanged.

This allows `matching_rule` in column_mapping.csv to be a human-readable description (e.g. "Convert date format YYYY-MM-DD to DD-MMM-YYYY") when the actual format tokens are stored in value_mapping.csv.

In the comparison loop, use `effective_rule = _effective_rule(src_col, rule, val_mapping)` for `apply_transformation`. Skip the VALUE_MAP lookup block when `effective_rule != rule` (date-spec override was applied).

---

## 4. Composite Key Matching

### 4a. Standard Matching
- Source key columns are transformed using their `matching_rule` before indexing
- Destination key columns are used as-is
- A match is: `transformed_source_key == destination_key`
- Build a `composite_key` string by joining transformed key parts with `|`

### 4b. Duplicate Key Detection
Before matching, detect and report:
- `DUPLICATE_KEY_SOURCE` — same transformed composite key appears more than once in source
- `DUPLICATE_KEY_DEST` — same composite key appears more than once in destination

### 4c. Partial Key Pairing (Migration Key Mismatch)
When the full composite key has no match, attempt partial pairing instead of immediately reporting two separate missing-key issues:

1. Collect non-duplicate unmatched source keys and non-duplicate unmatched destination keys
2. For each unmatched destination key, find the unmatched source key with the highest number of matching key parts (greedy best-score)
3. If a match with score > 0 is found, pair them as `MIGRATION_KEY_MISMATCH` — a **single issue record** representing both records
4. Store `key_parts_comparison` — list of `{src_col, dst_col, src_val, dst_val, matched}` for each key part
5. Remaining unpaired keys are reported as `KEY_MISSING_IN_DEST` / `KEY_MISSING_IN_SOURCE`

---

## 5. Issue Types

| Issue Type | Severity | Trigger |
|---|---|---|
| `KEY_MISSING_IN_DEST` | High | Source key not found in destination (no partial match) |
| `KEY_MISSING_IN_SOURCE` | Medium | Destination key not found in source (no partial match) |
| `MIGRATION_KEY_MISMATCH` | High | Partial composite key match — one or more key parts differ |
| `DUPLICATE_KEY_SOURCE` | High | Duplicate composite key in source |
| `DUPLICATE_KEY_DEST` | High | Duplicate composite key in destination |
| `VALUE_MISMATCH` | Low | Column value differs after transformation |
| `NULL_MISMATCH` | Medium | One side null, the other not |
| `TRANSFORMATION_ERROR` | Medium | Transformation rule failed |
| `VALUE_MAPPING_MISSING` | Medium | Source value has no entry in value_mapping.csv |
| `COLUMN_MAPPING_MISSING` | Medium | Mapped source column not found in source data |

---

## 6. Match Modes (Asymmetric Join Semantics)

`MATCH_MODE` controls which issue types are suppressed and which rows appear in View Data.

| Mode | Description | Issues suppressed |
|---|---|---|
| `full` | Full outer join — show all records from both sides | None |
| `leftout` | Source-primary — all source visible; destination shows only exact matches | `KEY_MISSING_IN_SOURCE`, `KEY_MISSING_IN_DEST` |
| `rightout` | Destination-primary — all destination visible; source shows only exact matches | `KEY_MISSING_IN_DEST` |
| `union` | Inner join — both sides show only exactly-matched rows | `KEY_MISSING_IN_SOURCE` |

Implement a module-level dict:
```python
_MODE_SUPPRESS: Dict[str, frozenset] = {
    'full':     frozenset(),
    'leftout':  frozenset({'KEY_MISSING_IN_SOURCE', 'KEY_MISSING_IN_DEST'}),
    'rightout': frozenset({'KEY_MISSING_IN_DEST'}),
    'union':    frozenset({'KEY_MISSING_IN_SOURCE'}),
}
```

### Hidden Row Indices (Asymmetric View Data Filtering)

Compute hidden row indices **before** applying the suppress filter to `detail_rows`.

```
leftout / union  → dst_hidden = dest row indices from KEY_MISSING_IN_SOURCE + MIGRATION_KEY_MISMATCH issues
rightout / union → src_hidden = source row indices from KEY_MISSING_IN_DEST + MIGRATION_KEY_MISMATCH issues
full             → both hidden lists empty
```

Store as `summary.source_hidden_row_indices` and `summary.dest_hidden_row_indices` (lists of int). The dashboard JS builds `_srcHidden` / `_dstHidden` Sets and filters `SRC_DATA.rows` / `DST_DATA.rows` in `showVD()`.

Also use these hidden index sets to compute `mode_src_rows` / `mode_dst_rows` for **all** profiling calls so column profiles and value-count comparisons always match what View Data shows.

---

## 7. Column-Level Data Profiling

Compute per-column profiling for: source dataset, destination dataset, matched rows (source-side), matched rows (destination-side). All profiling uses `mode_src_rows` / `mode_dst_rows` (mode-filtered rows).

Metrics per column:
- `inferred_data_type` (string / integer / decimal / boolean / date)
- `row_count`, `null_count`, `null_percent`
- `distinct_count`, `distinct_percent`
- `min`, `max` (numeric or date)
- `mean`, `std` (numeric)
- `top_values` (top 5 with counts)
- `length_stats` — `min_len`, `max_len`, `avg_len`
- Quality flags: `has_whitespace_issues`, `has_case_inconsistency`, `has_invalid_dates`, `has_non_numeric_in_numeric_column`
- `mapping_info` — `{source_column, destination_column}`
- `rules_info` — `{matching_rule, value_mapping_rules_used}`

### Column Value Profiling (_column_value_profiles)

Per-column value-count comparison: how many times each value appears in source vs destination. Source values are **transformed to the destination format before counting** so gaps reflect genuine data differences, not format differences.

Three-path detection per column (handles both standard rule codes and human-readable descriptions):

1. **Date format spec in val_mapping** — if val_mapping for this column contains an entry where both `source_value` and `destination_value` are date-format tokens, build the DATE_FORMAT rule and apply it to source values before counting.
2. **Value mapping** — if val_mapping has regular entries for this column, OR the rule string contains `VALUE_MAP` or `VALUE MAPPING`, map source values via the lookup table before counting.
3. **Standard rule code** — otherwise call `apply_transformation(value, rule)` for standard codes (DATE_FORMAT:…, STRIP_PREFIX:…, NUMERIC, DECIMAL, DIRECT).

Output per column: list of rows `{source_value, dest_value, source_count, dest_count, gap}`. Also includes `total_source`, `total_dest`, `source_column`, `destination_column`, `matching_rule`.

---

## 8. Output Files

All written to `--outdir` (default `output/`).

### validation_results.json
```
{
  summary: {
    source_row_count, destination_row_count,
    source_key_count, matched_key_count,
    unmatched_source_keys, extra_dest_keys,
    total_issues, key_issues, value_mismatch_count,
    pass_rate, mismatch_count_by_type, mismatch_count_by_column,
    composite_key_columns_source, composite_key_columns_dest,
    tolerance, match_mode,
    source_hidden_row_indices, dest_hidden_row_indices,
    run_timestamp, source_file, dest_file,
    source_columns, sample_source_rows (first 3)
  },
  detail_rows: [ { per-issue record } ]
}
```

Each detail row includes: `composite_key`, `dest_composite_key` (for MIGRATION_KEY_MISMATCH), `source_row_index`, `dest_row_index`, `source_column`, `destination_column`, `issue_type`, `severity`, `rule_applied`, `source_value_raw`, `source_value_transformed`, `dest_value_raw`, `dest_value_transformed`, `expected_dest_value`, `difference`, `key_parts_comparison` (for MIGRATION_KEY_MISMATCH), `timestamp`.

### validation_results.csv
Flat CSV of issue rows only (failures).

### row_match_summary.csv
**One row per source record** — full column-by-column audit trail.

Columns: `composite_key`, `overall_result` (PASS/FAIL/MISSING_IN_DEST/DUPLICATE_KEY_SOURCE), `issues_count`, then for each non-key mapped column (in mapping order): `{src_col}_source`, `{dst_col}_destination`, `{src_col}_match` (PASS / issue_type / N/A).

### profiling.json
```
{
  source: { column_profiles: [...] },
  destination: { column_profiles: [...] },
  matched: { source_column_profiles: [...], dest_column_profiles: [...] },
  value_counts: [ { source_column, destination_column, matching_rule, rows, total_source, total_dest } ]
}
```

### mappings.json
```
{
  column_mapping: [ rows from column_mapping.csv ],
  value_mapping: { col: [ {source_value, destination_value} ] }
}
```

### dashboard.html
Fully self-contained, no external dependencies, no server required.

---

## 9. Interactive HTML Dashboard

Embed all JSON data inline in the HTML (`DATA`, `SRC_DATA`, `DST_DATA`). Use vanilla JS only.

### Tab Bar
Tabs left-aligned. On the **right side** of the tab bar, always show three meta badges (unaffected by match mode):
- **Source [N]** — original source CSV row count
- **Destination [N]** — original destination CSV row count
- **Mode LEFTOUT** — current match mode in uppercase

CSS for tab bar meta badges:
```css
.tab-meta { margin-left:auto; display:flex; align-items:center; gap:8px; padding:8px 0 8px 16px; flex-shrink:0; }
.tab-badge { display:inline-flex; align-items:center; gap:4px; background:#0f172a; border:1px solid #334155; border-radius:20px; padding:3px 10px; font-size:.72rem; color:#94a3b8; white-space:nowrap; }
.tab-badge span { color:#60a5fa; font-weight:600; }
```

### Tab: Summary
- KPI cards with tooltips: Pass Rate, Source Rows, Matched Keys, Total Issues, Key Issues, Value Mismatches, Missing in Dest, Extra in Dest
- Run Information panel (files, key columns, tolerance, match mode, timestamp)
- Sample Source Records (first 3 rows)
- Bar charts: Issues by Type, Value Mismatches by Column

### Tab: Issues
Filterable and sortable table. Filters: text search, issue type dropdown, severity dropdown, column dropdown.

Each row has a **status icon** (first column) that shows a styled floating tooltip on hover:
- **ℹ (blue)** for `KEY_MISSING_IN_DEST` / `KEY_MISSING_IN_SOURCE` — shows which side the record is missing from
- **✕ (red)** for `VALUE_MISMATCH` / `NULL_MISMATCH` — tooltip lists **all mismatching columns** for that composite key with source and destination values: `• col_name → dst_col: source VALUE | destination VALUE`
- **⚡ (purple)** for `MIGRATION_KEY_MISMATCH` — tooltip shows matched and mismatched key parts with column names and values
- **⚠ (yellow)** for duplicate key, transformation, mapping errors — specific message per type

For `MIGRATION_KEY_MISMATCH` rows, the Destination Value cell renders **color-coded key-part badges**:
- Green badge with **M** superscript = key part matched
- Red badge with **U** superscript = key part unmatched, showing `src_val↔dst_val`

The floating tooltip is a `position:fixed` div (`#gtip`) updated on `mousemove`. It supports HTML (bold, bullets).

Table columns: [icon] Composite Key | Src Col | Dst Col | Issue Type | Severity | Src Raw | Src Transformed | Dest Value | Expected | Diff | Rule

Paginated (50 rows/page), sortable by clicking column headers.

### Tab: Profiling
Sub-tabs: Source | Destination | Matched (Src) | Matched (Dst)

Grid of column cards showing all profiling metrics, quality flags, top values, and mapping/rule info.

Below the column cards, include a **Column Value Profiling** section:
- One card per mapped column showing a comparison table of value counts after transformation
- Columns: Source Value | Destination Value | Source Count | Destination Count | Gap
- Gap cell: green if 0, red if positive (more in source), blue if negative (more in destination)
- Shows `total_source` and `total_dest` row counts per card

### Tab: Mappings
Column Mappings table and Value Mappings table side-by-side.

### Tab: View Data
Browse mode-filtered source or destination data.

**Sub-buttons:** View Source Data | View Destination Data

When the tab loads, apply mode filtering:
```javascript
const _srcHidden = new Set(DATA.summary.source_hidden_row_indices || []);
const _dstHidden = new Set(DATA.summary.dest_hidden_row_indices || []);

function showVD(which, el) {
  const hiddenSet = which === 'source' ? _srcHidden : _dstHidden;
  const allRows   = which === 'source' ? SRC_DATA.rows : DST_DATA.rows;
  vd_data = hiddenSet.size ? allRows.filter(r => !hiddenSet.has(r._row_idx)) : allRows;
  ...
}
```

Each row in `SRC_DATA.rows` / `DST_DATA.rows` must carry a `_row_idx` field equal to its 0-based index in the original CSV so the hidden-set filter works correctly after sorting and filtering.

**Column ordering** (schema-independent, driven by column_mapping at runtime):
- Composite key columns first (from `is_key=true` rows in column_mapping)
- Non-key columns follow in mapping file order
- Lookup is case-insensitive — `Emp_ID` in CSV matches `emp_id` in mapping
- Any unmapped columns appended at the end

**Row highlighting** (driven by `source_row_index` / `dest_row_index` cross-referenced against `detail_rows`):

For source view:
- Red `rgba(239,68,68,0.18)` — VALUE_MISMATCH or NULL_MISMATCH
- Green `rgba(34,197,94,0.18)` — KEY_MISSING_IN_DEST or MIGRATION_KEY_MISMATCH source row
- No highlight — matched and all values passed

For destination view:
- Red `rgba(239,68,68,0.18)` — VALUE_MISMATCH or NULL_MISMATCH
- Sky-blue `rgba(56,189,248,0.18)` — KEY_MISSING_IN_SOURCE or MIGRATION_KEY_MISMATCH destination row
- No highlight — matched and all values passed

**Legend** displayed above the table as styled cards matching the colours above with label and description.

**Controls:**
- Rows-per-page selector — options: always 10/25/50, adds 100/250/500/1000 as source row count grows, always includes All
- Per-column filter inputs (one per column, filter as you type)
- Sortable column headers (click to sort asc/desc, ▲/▼ indicator)
- Pagination with row-range counter

---

## 10. Colour Theme

Dark theme throughout:
- Background: `#0f172a`
- Card/panel: `#1e293b`
- Border: `#334155`
- Text: `#e2e8f0` / `#cbd5e1` / `#94a3b8`
- Accent blue: `#60a5fa` / `#1d4ed8`
- Green pass: `#22c55e`
- Red fail: `#ef4444`
- Orange warning: `#f97316`
- Purple migration: `#a855f7`

---

## 11. Using With a Different Schema

The engine contains zero schema-specific code. To switch schemas:
1. Replace `column_mapping.csv` — set `is_key=true` for new key columns, set appropriate `matching_rule` per column (standard codes or human-readable descriptions both work)
2. Replace `value_mapping.csv` — add lookup entries for code columns; use format token entries for date conversions
3. Update `folder_path` (or individual path variables) in the config block at the top of `validate.py`

Column name casing differences between mapping files and CSV headers are handled automatically via `_ci_get`.
