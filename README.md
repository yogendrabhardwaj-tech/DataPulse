# CSV Data Validation Engine

A generalized Python tool for comparing any source and destination CSV pair during data migration or ETL processes. Schema is driven entirely by two mapping files — no changes to `validate.py` are needed when switching datasets.

Produces JSON/CSV result files and a **fully self-contained interactive HTML dashboard** with issue drill-down, column profiling, value-count comparisons, and raw data browsing.

---

## Requirements

- Python 3.8 or later
- Standard library only — no `pip install` required

---

## Quick Start

**1. Set your file paths** in the config block at the top of `validate.py`:

```python
folder_path   = "Data_Student"           # folder containing your CSV files
SOURCE_PATH   = folder_path + "/employee_source.csv"
DEST_PATH     = folder_path + "/employee_destination.csv"
COLMAP_PATH   = folder_path + "/Mapping_Rule/column_mapping.csv"
VALMAP_PATH   = folder_path + "/Mapping_Rule/value_mapping.csv"
OUTPUT_DIR    = "output"
MATCH_MODE    = "leftout"                # full | leftout | rightout | union
```

**2. Run the engine:**

```bash
python validate.py
```

**3. Open the dashboard:**

```
output/dashboard.html
```

Open this file in any browser — no server needed.

---

## Folder Structure

```
DataValidation/
├── validate.py                        # Main engine (single file, no dependencies)
├── Data_Student/
│   ├── employee_source.csv            # Source data
│   ├── employee_destination.csv       # Destination data
│   └── Mapping_Rule/
│       ├── column_mapping.csv         # Column mapping and transformation rules
│       └── value_mapping.csv          # Value lookup / code translation table
└── output/                            # Generated after running validate.py
    ├── dashboard.html                 # Interactive HTML dashboard
    ├── validation_results.json        # Full results with summary + issue detail
    ├── validation_results.csv         # Issues only, flat CSV
    ├── row_match_summary.csv          # One row per source record, PASS/FAIL per column
    ├── profiling.json                 # Column-level statistics
    └── mappings.json                  # Normalized mapping rules
```

---

## Mapping Files

### column_mapping.csv

Defines which source columns map to which destination columns, how to transform them, and which columns form the composite key.

| Column | Description |
|---|---|
| `source_column` | Column name in the source CSV |
| `destination_column` | Column name in the destination CSV |
| `matching_rule` | How to transform/compare the source value (see rules below) |
| `is_key` | `True` / `False` — marks composite key columns |

**Example:**

```csv
source_column,destination_column,matching_rule,is_key
Student_ID,student_key,STRIP_PREFIX:S,True
Student_Name,student_full_name,DIRECT,False
Program_Status,status_cd,VALUE_MAP,False
Course_Code,course_id,STRIP_PREFIX:TR-,True
Academic_Year,year,DIRECT,True
Course_Fee,fee_amount,NUMERIC,False
Enrollment_Date,enroll_dt,DATE_FORMAT:YYYY-MM-DD->DD-MMM-YYYY,False
Credits,credit_points,NUMERIC,False
```

> Column name casing is **case-insensitive** — `Student_ID` in the CSV matches `student_id` in the mapping automatically.

> The `matching_rule` field accepts standard codes **or human-readable descriptions** — the engine infers the effective rule from value_mapping.csv content and from date-format tokens found in the description string (e.g. "Convert date format YYYY-MM-DD to DD-MMM-YYYY").

### value_mapping.csv

Defines code translations for VALUE_MAP columns and date format conversion specs.

| Column | Description |
|---|---|
| `column_name` | Must match `source_column` in column_mapping.csv |
| `source_value` | Original value in source CSV (or source date-format token) |
| `destination_value` | Expected value in destination CSV (or destination date-format token) |
| `rule_description` | Human-readable label — shown in dashboard, not used by engine |

**Example — code translation:**

```csv
column_name,source_value,destination_value,rule_description
Program_Status,ENROLLED,01,Currently enrolled
Program_Status,COMPLETED,03,Successfully completed
Program_Status,DROPPED,04,Dropped from program
Exam_Result,PASS,P,Exam passed
Exam_Result,FAIL,F,Exam failed
Scholarship_Eligible,YES,1,Eligible
Scholarship_Eligible,NO,0,Not eligible
```

**Example — date format spec (alternative to DATE_FORMAT rule code):**

```csv
column_name,source_value,destination_value,rule_description
Enrollment_Date,yyyy-mm-dd,dd-MMM-yyyy,Date format conversion
```

---

## Matching Rules

| Rule | Example | Description |
|---|---|---|
| `DIRECT` | `DIRECT` | Trim + case-insensitive string compare |
| `NUMERIC` | `NUMERIC` | Parse as float, compare within tolerance (default ±0.01) |
| `DECIMAL` | `DECIMAL` | Exact value ignoring trailing zeros — `1200.50 == 1200.5` |
| `VALUE_MAP` | `VALUE_MAP` | Translate source value via value_mapping.csv |
| `STRIP_PREFIX:<p>` | `STRIP_PREFIX:S` | Remove leading prefix before compare |
| `DATE_FORMAT:<src>-><dst>` | `DATE_FORMAT:YYYY-MM-DD->DD-MMM-YYYY` | Convert date format |

Rules can be **chained with `\|`**:

```
STRIP_PREFIX:TR-|NUMERIC
```

**Supported date format tokens:**

| Token | Example |
|---|---|
| `YYYY-MM-DD` | `2024-01-15` |
| `DD-MMM-YYYY` | `15-JAN-2024` |
| `MM/DD/YYYY` | `01/15/2024` |
| `DD/MM/YYYY` | `15/01/2024` |
| `YYYY/MM/DD` | `2024/01/15` |
| `YYYYMMDD` | `20240115` |
| `DD-MM-YYYY` | `15-01-2024` |
| `MM-DD-YYYY` | `01-15-2024` |

---

## Match Modes

Controls which records are compared and which rows appear in the dashboard View Data tabs.

| Mode | Description |
|---|---|
| `full` | Compare all records from both files (full outer join) |
| `leftout` | Source-primary: all source rows visible; destination shows only exact matches |
| `rightout` | Destination-primary: all destination rows visible; source shows only exact matches |
| `union` | Inner join: only exactly matched records on both sides |

Set in the config block or pass via CLI:

```bash
python validate.py --match-mode leftout
```

---

## CLI Flags

All config block values can be overridden from the command line:

```bash
python validate.py \
  --source  data/source.csv \
  --dest    data/dest.csv \
  --colmap  data/Mapping_Rule/column_mapping.csv \
  --valmap  data/Mapping_Rule/value_mapping.csv \
  --outdir  output \
  --format  both \
  --tolerance 0.01 \
  --match-mode leftout
```

---

## Issue Types

| Issue | Severity | Meaning |
|---|---|---|
| `KEY_MISSING_IN_DEST` | High | Source record has no matching key in destination |
| `KEY_MISSING_IN_SOURCE` | Medium | Destination record has no matching key in source |
| `MIGRATION_KEY_MISMATCH` | High | Partial key match found — one or more key parts differ |
| `DUPLICATE_KEY_SOURCE` | High | Composite key appears more than once in source |
| `DUPLICATE_KEY_DEST` | High | Composite key appears more than once in destination |
| `VALUE_MISMATCH` | Low | Matched row has a column value difference |
| `NULL_MISMATCH` | Medium | One side is null, the other is not |
| `TRANSFORMATION_ERROR` | Medium | Transformation rule failed to process source value |
| `VALUE_MAPPING_MISSING` | Medium | Source value has no entry in value_mapping.csv |
| `COLUMN_MAPPING_MISSING` | Medium | Mapped source column not found in source data |

---

## Dashboard Tabs

| Tab | What it shows |
|---|---|
| **Summary** | KPI cards (pass rate, matched keys, total issues), run info, sample source rows, bar charts |
| **Issues** | Filterable/sortable table of all issues. Hover icons show full mismatch detail per row. |
| **Profiling** | Column statistics for source / destination / matched subsets. Includes Column Value Profiling — side-by-side value counts after transformation. |
| **Mappings** | Column mapping and value mapping rule tables |
| **View Data** | Browse mode-filtered source or destination data with row highlighting, per-column filters, and sortable headers |

**Row highlighting in View Data:**

| Colour | Meaning |
|---|---|
| Red | Row has one or more value mismatches |
| Green (source) | Source key not found in destination |
| Sky-blue (destination) | Destination key not found in source |
| No colour | Fully matched and all values pass |

---

## Output Files

| File | Description |
|---|---|
| `dashboard.html` | Self-contained interactive dashboard — open in any browser |
| `validation_results.json` | Summary object + full issue detail rows (JSON) |
| `validation_results.csv` | Flat CSV of issues only |
| `row_match_summary.csv` | One row per source record with PASS/FAIL per column |
| `profiling.json` | Column-level statistics for source, destination, and matched subsets |
| `mappings.json` | Normalized column and value mapping rules |

---

## Switching to a Different Dataset

No changes to `validate.py` are needed. Only the mapping files and config block need to be updated:

1. Place your source and destination CSV files in a folder
2. Create `column_mapping.csv` — set `is_key=True` for composite key columns, set the appropriate rule per column
3. Create `value_mapping.csv` — add code translations for lookup columns (leave empty if none)
4. Update `folder_path` (or individual path variables) in the config block at the top of `validate.py`

Column name casing differences between the mapping files and CSV headers are resolved automatically.
