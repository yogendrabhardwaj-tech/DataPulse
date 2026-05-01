# Dashboard Display Rules — Explicit Reference

This document defines every rule used to determine what data is shown, hidden, or highlighted across all dashboard sections. Use it as a reference when interpreting results or troubleshooting unexpected output.

---

## 1. Match Mode — Core Concept

`MATCH_MODE` is the single setting that governs display behaviour across all tabs. Think of it as a SQL JOIN type applied to the source and destination datasets.

| Mode | SQL analogy | Intent |
|---|---|---|
| `full` | FULL OUTER JOIN | Show all records from both sides, report all issues |
| `leftout` | LEFT-primary | Focus on source — show all source records; destination shows only its matching side |
| `rightout` | RIGHT-primary | Focus on destination — show all destination records; source shows only its matching side |
| `union` | INNER JOIN | Focus on matched pairs only — both sides show only exact matches |

---

## 2. View Data — Row Visibility Rules

### Which rows appear in each tab

The table below defines which source rows and which destination rows are **visible** in the View Data tab for each mode.

| Mode | Source View Data | Destination View Data |
|---|---|---|
| `full` | All source rows | All destination rows |
| `leftout` | All source rows | Only exactly-matched destination rows |
| `rightout` | Only exactly-matched source rows | All destination rows |
| `union` | Only exactly-matched source rows | Only exactly-matched destination rows |

**Exactly-matched** means the composite key in the source (after transformation) was found verbatim in the destination.

**MIGRATION_KEY_MISMATCH rows** (partial key match — one or more key parts differ) are treated as follows:

| Mode | MIGRATION source row visible? | MIGRATION destination row visible? |
|---|---|---|
| `full` | Yes | Yes |
| `leftout` | Yes (source is primary — all source rows shown) | No (destination shows only exact matches) |
| `rightout` | No (source shows only exact matches) | Yes (destination is primary — all destination rows shown) |
| `union` | No | No |

### How visibility is implemented

At the end of the validation run, two lists of row indices are computed **before** any issue suppression:

```
source_hidden_row_indices  — row indices (0-based) to hide from Source View Data
dest_hidden_row_indices    — row indices (0-based) to hide from Destination View Data
```

| Mode | source_hidden_row_indices contains | dest_hidden_row_indices contains |
|---|---|---|
| `full` | (empty) | (empty) |
| `leftout` | (empty) | dest row indices from KEY_MISSING_IN_SOURCE issues + MIGRATION_KEY_MISMATCH dest row indices |
| `rightout` | src row indices from KEY_MISSING_IN_DEST issues + MIGRATION_KEY_MISMATCH src row indices | (empty) |
| `union` | src row indices from KEY_MISSING_IN_DEST issues + MIGRATION_KEY_MISMATCH src row indices | dest row indices from KEY_MISSING_IN_SOURCE issues + MIGRATION_KEY_MISMATCH dest row indices |

These lists are stored in `summary.source_hidden_row_indices` and `summary.dest_hidden_row_indices` in `validation_results.json` and embedded in `dashboard.html`. The dashboard JS builds `_srcHidden` / `_dstHidden` Sets and filters `SRC_DATA.rows` / `DST_DATA.rows` at render time:

```javascript
const _srcHidden = new Set(DATA.summary.source_hidden_row_indices || []);
const _dstHidden = new Set(DATA.summary.dest_hidden_row_indices || []);

// In showVD():
vd_data = hiddenSet.size
  ? allRows.filter(r => !hiddenSet.has(r._row_idx))
  : allRows;
```

Each row in `SRC_DATA.rows` / `DST_DATA.rows` carries a `_row_idx` field equal to its 0-based position in the original CSV. This ensures the filter works correctly after sorting or per-column filtering.

---

## 3. View Data — Row Highlight Colours

Row highlighting is driven by cross-referencing each row's `_row_idx` against `detail_rows` in `validation_results.json`.

### Source View Data

| Highlight colour | Condition |
|---|---|
| Red `rgba(239,68,68,0.18)` | Row has at least one `VALUE_MISMATCH` or `NULL_MISMATCH` issue |
| Green `rgba(34,197,94,0.18)` | Row has a `KEY_MISSING_IN_DEST` issue (source key not in destination) |
| Green `rgba(34,197,94,0.18)` | Row is the source side of a `MIGRATION_KEY_MISMATCH` pair |
| No highlight | Row matched and all column values passed |

### Destination View Data

| Highlight colour | Condition |
|---|---|
| Red `rgba(239,68,68,0.18)` | Row has at least one `VALUE_MISMATCH` or `NULL_MISMATCH` issue |
| Sky-blue `rgba(56,189,248,0.18)` | Row has a `KEY_MISSING_IN_SOURCE` issue (destination key not in source) |
| Sky-blue `rgba(56,189,248,0.18)` | Row is the destination side of a `MIGRATION_KEY_MISMATCH` pair |
| No highlight | Row matched and all column values passed |

**Priority:** Red (value mismatch) takes precedence over key-missing colours. A matched row with a value mismatch is always red regardless of key status.

### Colour Legend (displayed above the table)

Four cards are shown above the View Data table as a visual legend:

| Card colour | Label | Description |
|---|---|---|
| Red | Value Mismatch | Record matched but one or more column values differ |
| Green | Missing in Destination | Source key has no corresponding destination record |
| Sky-blue | Missing in Source | Destination key has no corresponding source record |
| Grey (no fill) | Matched — All Pass | Record matched and all column values are correct |

---

## 4. View Data — Column Order

Column ordering in the View Data table is schema-independent. The order is determined at runtime from `column_mapping.csv`:

1. **Composite key columns first** — all columns where `is_key = True`, in mapping file order
2. **Non-key mapped columns** — all columns where `is_key = False`, in mapping file order
3. **Unmapped columns last** — any columns present in the CSV that have no entry in column_mapping.csv, appended alphabetically

Column name matching between the CSV header and column_mapping.csv is **case-insensitive**. `Student_ID` in the CSV header matches `student_id` in the mapping file.

---

## 5. View Data — Controls

| Control | Behaviour |
|---|---|
| Source / Destination sub-buttons | Switch between source and destination datasets |
| Per-column filter inputs | One text input per column; rows are filtered as you type (case-insensitive substring match) |
| Sortable column headers | Click to sort ascending; click again for descending; ▲/▼ indicator shown |
| Rows-per-page selector | Options: 10, 25, 50, and dynamically adds 100/250/500/1000 as row count grows; always includes All |
| Pagination | Previous/Next buttons with current range counter (e.g. "Showing 1–50 of 312") |

---

## 6. Issues Tab — Issue Suppression Rules

The Issues tab only shows issues that are **relevant to the current match mode**. An issue type is suppressed when the rows it refers to are not visible in View Data.

| Mode | Issue types shown | Issue types suppressed |
|---|---|---|
| `full` | All issue types | (none) |
| `leftout` | All except KEY_MISSING_IN_SOURCE | `KEY_MISSING_IN_SOURCE` |
| `rightout` | All except KEY_MISSING_IN_DEST | `KEY_MISSING_IN_DEST` |
| `union` | VALUE_MISMATCH, NULL_MISMATCH, TRANSFORMATION_ERROR, VALUE_MAPPING_MISSING, COLUMN_MAPPING_MISSING, DUPLICATE_KEY_SOURCE, DUPLICATE_KEY_DEST | `KEY_MISSING_IN_DEST`, `KEY_MISSING_IN_SOURCE`, `MIGRATION_KEY_MISMATCH` |

**Rationale for union suppression:** In union (inner join) mode, only exactly-matched records are shown. KEY_MISSING_IN_DEST, KEY_MISSING_IN_SOURCE, and MIGRATION_KEY_MISMATCH all refer to records that are hidden from View Data — showing them in Issues would be misleading since those records are out of scope for this comparison mode.

---

## 7. Issues Tab — Row Icons and Tooltips

Each issue row shows a status icon in the first column. Hovering the icon shows a floating tooltip with full detail.

| Icon | Colour | Triggered by | Tooltip content |
|---|---|---|---|
| ℹ | Blue | `KEY_MISSING_IN_DEST`, `KEY_MISSING_IN_SOURCE` | Which side the record is missing from and its composite key |
| ✕ | Red | `VALUE_MISMATCH`, `NULL_MISMATCH` | All mismatching columns for that composite key: `• col → dst_col: SOURCE value \| DESTINATION value` |
| ⚡ | Purple | `MIGRATION_KEY_MISMATCH` | Each key part with matched/unmatched status, showing source value and destination value side by side |
| ⚠ | Yellow | `DUPLICATE_KEY_SOURCE`, `DUPLICATE_KEY_DEST`, `TRANSFORMATION_ERROR`, `VALUE_MAPPING_MISSING`, `COLUMN_MAPPING_MISSING` | Specific message for the error type |

**Tooltip behaviour:** The tooltip is a `position:fixed` div (`#gtip`) repositioned on every `mousemove`. It renders HTML (supports bold text and bullet lists). It appears on icon hover only, not on row hover.

### MIGRATION_KEY_MISMATCH destination cell rendering

For MIGRATION_KEY_MISMATCH rows, the Destination Value cell renders colour-coded key-part badges instead of a plain value:

| Badge style | Meaning |
|---|---|
| Green badge with **M** superscript | Key part matched between source and destination |
| Red badge with **U** superscript | Key part did not match; shows `src_val ↔ dst_val` |

---

## 8. Issues Tab — Filters and Sort

| Filter | Behaviour |
|---|---|
| Text search | Searches all columns of the issues table simultaneously |
| Issue Type dropdown | Filter to a single issue type or show All |
| Severity dropdown | Filter by High / Medium / Low or show All |
| Column dropdown | Filter to issues on a specific source column |
| Column header click | Sort ascending; click again for descending; ▲/▼ indicator |

Pagination: 50 rows per page, with Previous/Next and row-range counter.

---

## 9. Profiling Tab — Data Scope Rules

All profiling views use **mode-filtered rows** — the same rows that appear in View Data for the current match mode.

| Profiling sub-tab | Data source |
|---|---|
| Source | `mode_src_rows` — source CSV rows after removing `source_hidden_row_indices` |
| Destination | `mode_dst_rows` — destination CSV rows after removing `dest_hidden_row_indices` |
| Matched (Src) | Source rows whose composite key was found exactly in destination |
| Matched (Dst) | Destination rows whose composite key was found exactly in source |

This means profiling statistics (null counts, distinct counts, top values, etc.) always reflect the visible dataset for the current mode — not the full CSV.

---

## 10. Column Value Profiling — Transformation Rules

The Column Value Profiling section (bottom of the Profiling tab) shows side-by-side value counts after applying transformations to source values, so gaps reflect genuine data differences rather than format differences.

The transformation applied per column follows three detection paths, tried in order:

| Priority | Condition | Action |
|---|---|---|
| 1 | `value_mapping.csv` has an entry for this column where BOTH `source_value` and `destination_value` are date format tokens (e.g. `YYYY-MM-DD` → `DD-MMM-YYYY`) | Build and apply `DATE_FORMAT:<src>-><dst>` rule |
| 2 | `value_mapping.csv` has regular value entries for this column, OR the `matching_rule` string contains `VALUE_MAP` or `VALUE MAPPING` | Apply the value mapping table: source values are translated to their destination equivalents before counting |
| 3 | Neither of the above | Apply `apply_transformation(value, matching_rule)` — works for standard rule codes: `DATE_FORMAT:…`, `STRIP_PREFIX:…`, `NUMERIC`, `DECIMAL`, `DIRECT` |

**Gap column:** `source_count − destination_count`.
- **0** (green) — counts are equal after transformation
- **Positive** (red) — more source occurrences than destination
- **Negative** (blue) — more destination occurrences than source

---

## 11. Summary Tab — KPI Definitions

| KPI card | Definition |
|---|---|
| Pass Rate | `matched_key_count / source_row_count × 100` — percentage of source records with an exact key match in destination |
| Source Rows | Total rows in source CSV (unfiltered) |
| Matched Keys | Source records whose composite key was found exactly in destination |
| Total Issues | Count of `detail_rows` after mode suppression |
| Key Issues | Count of `detail_rows` where `severity = High` |
| Value Mismatches | Count of `VALUE_MISMATCH` issues after mode suppression |
| Missing in Dest | Count of `KEY_MISSING_IN_DEST` issues (0 in leftout/union modes) |
| Extra in Dest | Count of destination keys not found in source (0 in rightout/union modes) |

---

## 12. Tab Bar Meta Badges

The right side of the tab bar always shows three badges. These reflect the **original CSV file counts**, unaffected by match mode or filtering:

| Badge | Value |
|---|---|
| Source [N] | Total rows in the source CSV file |
| Destination [N] | Total rows in the destination CSV file |
| Mode XXXX | Current `MATCH_MODE` value in uppercase |

---

## 13. Composite Key — Construction and Matching Rules

### Building the source composite key
1. For each column where `is_key = True` in column_mapping.csv (in mapping file order):
   - Read the raw source value using case-insensitive column lookup (`_ci_get`)
   - Apply `apply_transformation(value, matching_rule)` to get the transformed value
2. Join all transformed key parts with `|` separator

### Matching
- A source record matches a destination record when:
  `transformed_source_composite_key == destination_composite_key_as_is`
- Destination key columns are read as-is (no transformation applied)
- Column name lookup is case-insensitive on both sides

### Duplicate key detection
Before matching, any composite key that appears more than once in source is flagged as `DUPLICATE_KEY_SOURCE`. Any key appearing more than once in destination is flagged as `DUPLICATE_KEY_DEST`. Duplicate rows are excluded from key-matching to avoid ambiguous pairings.

### Partial key matching (MIGRATION_KEY_MISMATCH)
When a source key has no exact destination match:
1. Split the source key and each unmatched destination key into individual parts
2. Count how many parts match between each source/destination candidate pair
3. Pair the source key with the destination key that has the highest match count (greedy)
4. If the best score > 0, record as `MIGRATION_KEY_MISMATCH` with `key_parts_comparison` detail
5. If no candidate scores > 0, record as `KEY_MISSING_IN_DEST` / `KEY_MISSING_IN_SOURCE`

---

## 14. Transformation Engine — Rule Precedence

When processing a source value for comparison, the engine applies rules in this order:

1. **Resolve effective rule** (`_effective_rule`):
   - If `value_mapping.csv` has a date-format spec for this column → derive `DATE_FORMAT:…` rule
   - Else if the `matching_rule` string contains two date format tokens → derive `DATE_FORMAT:…` from the tokens found in the string
   - Else → use `matching_rule` as-is

2. **Apply transformation** (`apply_transformation`):
   - `DATE_FORMAT:…`, `STRIP_PREFIX:…`, `NUMERIC`, `DECIMAL` → transform the value
   - `DIRECT`, `VALUE_MAP`, unknown strings → return value trimmed, no transformation
   - Empty/null source value with `DATE_FORMAT` → pass through without error (null handled downstream)

3. **Apply value mapping** (only when effective rule == original rule, i.e. no date-spec override was applied):
   - If `matching_rule == VALUE_MAP` or source column has entries in value_mapping.csv:
     - Look up `source_raw_value` in the column's value map
     - If found → replace `src_transformed` with the mapped destination value
     - If not found and rule is `VALUE_MAP` → raise `VALUE_MAPPING_MISSING` issue

4. **Compare** transformed source value against raw destination value:
   - For `DECIMAL` rules → normalize both sides with `Decimal.normalize()` before comparing
   - For `NUMERIC` columns → compare as floats within tolerance
   - For all others → case-insensitive string compare

---

## 15. Output File — Content Summary

| File | Always written? | Content |
|---|---|---|
| `dashboard.html` | Yes | Self-contained interactive dashboard; all data embedded inline |
| `validation_results.json` | When format = json or both | `summary` object + `detail_rows` array |
| `validation_results.csv` | When format = csv or both | Flat CSV of `detail_rows` (issues only) |
| `row_match_summary.csv` | Always | One row per source record: composite key, overall result (PASS/FAIL/MISSING_IN_DEST/DUPLICATE_KEY_SOURCE), issue count, and for each non-key mapped column: source value, destination value, match result (PASS / issue_type / N/A) |
| `profiling.json` | When format = json or both | Column profiles for source, destination, and matched subsets + `value_counts` array |
| `mappings.json` | When format = json or both | Normalized column and value mapping rules |

---

## 16. Quick Reference — Mode Behaviour at a Glance

```
┌──────────────┬──────────────────────────────┬──────────────────────────────┬─────────────────────────────────────────┐
│ Mode         │ Source View Data             │ Destination View Data        │ Issues tab shows                        │
├──────────────┼──────────────────────────────┼──────────────────────────────┼─────────────────────────────────────────┤
│ full         │ All rows                     │ All rows                     │ All issue types                         │
│ leftout      │ All rows                     │ Exact-match rows only        │ All except KEY_MISSING_IN_SOURCE        │
│ rightout     │ Exact-match rows only        │ All rows                     │ All except KEY_MISSING_IN_DEST          │
│ union        │ Exact-match rows only        │ Exact-match rows only        │ VALUE_MISMATCH, NULL_MISMATCH,          │
│              │                              │                              │ TRANSFORMATION_ERROR,                   │
│              │                              │                              │ VALUE_MAPPING_MISSING,                  │
│              │                              │                              │ COLUMN_MAPPING_MISSING,                 │
│              │                              │                              │ DUPLICATE_KEY_SOURCE/DEST               │
└──────────────┴──────────────────────────────┴──────────────────────────────┴─────────────────────────────────────────┘

MIGRATION_KEY_MISMATCH row visibility:
  full      → source row visible,     destination row visible
  leftout   → source row visible,     destination row HIDDEN
  rightout  → source row HIDDEN,      destination row visible
  union     → source row HIDDEN,      destination row HIDDEN  (issue suppressed)
```
