#!/usr/bin/env python3
"""
Generalized CSV Data Validation Engine
=======================================
Works with ANY source/destination CSV pair given:
  - column_mapping.csv  (source_column, destination_column, matching_rule, is_key)
  - value_mapping.csv   (column_name, source_value, destination_value, rule_description)

Supported matching_rule values:
  DIRECT                        trim + case-insensitive compare
  TRIM                          strip leading/trailing whitespace only
  UPPERCASE                     convert source value to uppercase before compare
  LOWERCASE                     convert source value to lowercase before compare
  NUMERIC                       parse as float, compare within --tolerance
  DECIMAL                       exact value match ignoring trailing zeros  e.g. 1200.50 == 1200.5
  VALUE_MAP                     look up source value in value_mapping.csv
  STRIP_PREFIX:<prefix>         remove leading prefix before compare  e.g. STRIP_PREFIX:E
  ZEROPAD:<length>              left-pad with zeros to target length  e.g. ZEROPAD:12  (1001 → 000000001001)
  DATE_FORMAT:<src>-><dst>      convert date format                   e.g. DATE_FORMAT:YYYY-MM-DD->DD-MMM-YYYY
  Rules can be chained with |   e.g. STRIP_PREFIX:E|NUMERIC  or  STRIP_PREFIX:CUST|ZEROPAD:12  or  TRIM|UPPERCASE

Supported date format tokens: YYYY-MM-DD, DD-MMM-YYYY, MM/DD/YYYY, DD/MM/YYYY,
                               YYYY/MM/DD, YYYYMMDD, DD-MM-YYYY, MM-DD-YYYY
"""

import argparse
import csv
import json
import os
import sys
import statistics
from collections import defaultdict, Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# USER CONFIGURATION — update these values instead of passing CLI flags
# ---------------------------------------------------------------------------
folder_path     = "Data_Folder_Template"            # Base folder for all CSV files
SOURCE_FOLDER   = folder_path + "/Source"           # Folder containing source CSV file(s)
                                                    #   All .csv files in the folder are merged automatically
DEST_FOLDER     = folder_path + "/Destination"      # Folder containing destination CSV file(s)
COLMAP_PATH     = folder_path + "/Mapping_Rule/column_mapping.csv"
VALMAP_PATH     = folder_path + "/Mapping_Rule/value_mapping.csv"
OUTPUT_DIR      = "output"                          # Directory to write output files
OUTPUT_FORMAT   = "both"                            # "json", "csv", or "both"
TOLERANCE       = 0.01                              # Numeric comparison tolerance
KEY_COLS        = None                              # Override key columns e.g. ["EmpID", "Dept"]
                                                    # Set to None to use is_key=true from colmap
MATCH_MODE      = "leftout"                         # Comparison scope (case-insensitive):
                                                    #   full     – all records from both sides
                                                    #   leftout  – all source; dest shows exact matches only
                                                    #   rightout – all dest; source shows exact matches only
                                                    #   union    – inner join: only exactly-matched records
RUN_MODE        = "TEST"                            # "TEST" – shows corruption_summary.txt on Mappings tab
                                                    # Any other value – hides it (use "PROD" for production)

# CSV separator characters for each input file type.
# Use "," for standard CSV, "|" for pipe-delimited, "\t" for tab-delimited, etc.
SOURCE_SEP      = ","   # Separator for source data CSV file(s)
DEST_SEP        = "|"   # Separator for destination data CSV file(s)
COLMAP_SEP      = ","   # Separator for column_mapping.csv
VALMAP_SEP      = ","   # Separator for value_mapping.csv

# ---------------------------------------------------------------------------
# Transformation engine
# ---------------------------------------------------------------------------

_DATE_FMT_MAP = {
    'YYYY-MM-DD':  '%Y-%m-%d',
    'DD-MMM-YYYY': '%d-%b-%Y',
    'MM/DD/YYYY':  '%m/%d/%Y',
    'DD/MM/YYYY':  '%d/%m/%Y',
    'YYYY/MM/DD':  '%Y/%m/%d',
    'YYYYMMDD':    '%Y%m%d',
    'DD-MM-YYYY':  '%d-%m-%Y',
    'MM-DD-YYYY':  '%m-%d-%Y',
}


def apply_transformation(value: str, rule: str) -> Tuple[str, Optional[str]]:
    """
    Apply one or more | -separated transformation rules to *value*.
    Returns (transformed_value, error_message_or_None).
    VALUE_MAP is intentionally a no-op here — the caller resolves it.
    """
    val = str(value or '').strip()

    if not rule or rule.upper() in ('DIRECT', 'VALUE_MAP'):
        return val, None

    error = None
    for part in rule.split('|'):
        part = part.strip()
        up = part.upper()

        if up in ('DIRECT', 'VALUE_MAP', 'TRIM'):
            val = val.strip()
        elif up == 'LOWERCASE':
            val = val.lower()
        elif up == 'UPPERCASE':
            val = val.upper()
        elif up == 'NUMERIC':
            try:
                f = float(val)
                # Produce integer string for whole numbers ("001234" → "1234", not "1234.0")
                # so key matching works when destination stores integers.
                # Fractional values keep their decimal form ("1234.56" → "1234.56").
                val = str(int(f)) if f == int(f) else str(f)
            except ValueError:
                error = f'Cannot parse "{val}" as numeric'
        elif up == 'DECIMAL':
            try:
                # normalize() removes trailing zeros; format 'f' avoids scientific notation
                # 1200.50 → '1200.5' | 1200.00 → '1200' | 850.000 → '850'
                val = format(Decimal(val).normalize(), 'f')
            except InvalidOperation:
                error = f'Cannot parse "{val}" as decimal'
        elif up.startswith('STRIP_PREFIX:'):
            prefix = part[len('STRIP_PREFIX:'):]
            if val.upper().startswith(prefix.upper()):
                val = val[len(prefix):]
        elif up.startswith('ZEROPAD:'):
            pad_spec = part[len('ZEROPAD:'):]
            try:
                target_len = int(pad_spec)
                if target_len < 1:
                    raise ValueError
                val = val.zfill(target_len)
            except ValueError:
                error = f'ZEROPAD requires a positive integer length, got "{pad_spec}"'
        elif up.startswith('DATE_FORMAT:'):
            fmt_spec = part[len('DATE_FORMAT:'):]
            if '->' not in fmt_spec:
                error = f'Invalid DATE_FORMAT rule — expected SRC->DST, got "{part}"'
            else:
                src_key, dst_key = fmt_spec.split('->', 1)
                src_py = _DATE_FMT_MAP.get(src_key.strip())
                dst_py = _DATE_FMT_MAP.get(dst_key.strip())
                if not src_py or not dst_py:
                    error = f'Unknown date format token in "{part}"'
                elif not val:
                    pass  # empty/null — leave as-is, null-check handles it later
                else:
                    try:
                        dt = datetime.strptime(val, src_py)
                        val = dt.strftime(dst_py).upper()
                    except ValueError:
                        _ambiguous = {'MM/DD/YYYY', 'DD/MM/YYYY'}
                        hint = (' — MM/DD/YYYY and DD/MM/YYYY are ambiguous when day ≤ 12; '
                                'verify the correct format token is used'
                                if src_key.strip().upper() in _ambiguous else '')
                        error = f'Cannot parse date "{val}" with format "{src_key}"{hint}'
        else:
            pass  # unknown rule — ignore gracefully

        if error:
            break

    return val, error


def _effective_rule(src_col: str, rule: str, val_mapping: Dict[str, Dict[str, str]]) -> str:
    """Return the effective transformation rule for a column.

    Handles human-readable matching_rule descriptions for date columns in two ways:

    1. val_mapping date-spec entry — if value_mapping.csv holds a format-token entry
       for this column (e.g. source_value='YYYY-MM-DD', destination_value='DD-MMM-YYYY'),
       derive and return the corresponding DATE_FORMAT:<src>-><dst> rule.

    2. Rule string scan — if no val_mapping date spec exists, scan the rule string
       itself for known date format tokens (e.g. "Convert date format YYYY-MM-DD to
       DD-MMM-YYYY (allow null)"). If exactly two different tokens are found, build
       the DATE_FORMAT rule using their order of appearance.

    Falls through to the original rule for standard codes (DATE_FORMAT:…, VALUE_MAP,
    DIRECT, NUMERIC, etc.) and non-date human-readable descriptions.
    """
    # Skip if already a standard DATE_FORMAT code
    if rule.upper().startswith('DATE_FORMAT:'):
        return rule

    # Priority 1: date-spec entry in val_mapping
    col_val_map = val_mapping.get(src_col, {})
    for sv, dv in col_val_map.items():
        sv_up = sv.strip().upper()
        dv_up = dv.strip().upper()
        if sv_up in _DATE_FMT_MAP and dv_up in _DATE_FMT_MAP:
            return f'DATE_FORMAT:{sv_up}->{dv_up}'

    # Priority 2: scan rule string for date format tokens
    rule_up = rule.upper()
    # Sort tokens longest-first to avoid shorter tokens matching inside longer ones
    tokens_by_len = sorted(_DATE_FMT_MAP.keys(), key=len, reverse=True)
    found: List[Tuple[int, str]] = []  # (position, token)
    for tok in tokens_by_len:
        pos = rule_up.find(tok.upper())
        if pos != -1:
            found.append((pos, tok))
    # Keep the two earliest-appearing distinct tokens
    found_sorted = sorted(found, key=lambda x: x[0])
    unique_tokens = []
    seen_pos: set = set()
    for pos, tok in found_sorted:
        # Skip if this position is already covered by a previously found token
        overlap = any(abs(pos - p) < len(tok) for p in seen_pos)
        if not overlap:
            unique_tokens.append(tok)
            seen_pos.add(pos)
        if len(unique_tokens) == 2:
            break
    if len(unique_tokens) == 2:
        return f'DATE_FORMAT:{unique_tokens[0]}->{unique_tokens[1]}'

    return rule


def _ci_get(row: Dict, col: str) -> str:
    """Case-insensitive, whitespace-tolerant dict lookup for CSV row dicts."""
    col_norm = col.strip().lower()
    for k, v in row.items():
        if k.strip().lower() == col_norm:
            return str(v or '').strip()
    return ''


def _ci_has(row: Dict, col: str) -> bool:
    col_norm = col.strip().lower()
    return any(k.strip().lower() == col_norm for k in row)


# ---------------------------------------------------------------------------
# CSV loaders
# ---------------------------------------------------------------------------

def _load_csv(path: str, delimiter: str = ',') -> Tuple[List[str], List[Dict]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f'File not found: {path}')
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return headers, rows


def _load_folder_csv(folder: str, label: str = 'folder', delimiter: str = ',') -> Tuple[List[str], List[Dict], List[str]]:
    """Find all .csv files in *folder*, validate consistent schema, and merge into one dataset.

    Returns (headers, merged_rows, loaded_file_paths).

    Raises:
        FileNotFoundError – folder does not exist OR contains no CSV files.
        ValueError         – one or more files have a different set of column headers
                             from the first file found (schema mismatch).
    """
    if not os.path.isdir(folder):
        raise FileNotFoundError(
            f'{label.title()} folder not found: "{folder}"\n'
            f'  Check that the folder path is correct and the folder exists.'
        )

    all_files = sorted(f for f in os.listdir(folder) if f.lower().endswith('.csv'))
    if not all_files:
        raise FileNotFoundError(
            f'No CSV files found in {label} folder: "{folder}"\n'
            f'  Add at least one .csv file to the folder and try again.'
        )

    ref_headers: Optional[List[str]] = None
    ref_file: str = ''
    merged_rows: List[Dict] = []
    loaded_paths: List[str] = []
    schema_errors: List[str] = []

    for fname in all_files:
        fpath = os.path.join(folder, fname)
        try:
            headers, rows = _load_csv(fpath, delimiter=delimiter)
        except Exception as exc:
            raise FileNotFoundError(f'Failed to read {fpath}: {exc}') from exc

        if ref_headers is None:
            # First file — establish reference schema
            ref_headers = headers
            ref_file = fname
        else:
            # Compare against reference schema (case-insensitive, order-independent)
            norm_ref = sorted(h.strip().lower() for h in ref_headers)
            norm_cur = sorted(h.strip().lower() for h in headers)
            if norm_ref != norm_cur:
                ref_set = set(h.strip().lower() for h in ref_headers)
                cur_set = set(h.strip().lower() for h in headers)
                missing = sorted(ref_set - cur_set)
                extra   = sorted(cur_set - ref_set)
                parts = [f'  "{fname}" schema differs from reference "{ref_file}":']
                if missing:
                    parts.append(f'    Columns missing in "{fname}" : {missing}')
                if extra:
                    parts.append(f'    Extra columns in "{fname}"   : {extra}')
                schema_errors.append('\n'.join(parts))
                continue  # collect all mismatches before raising

        merged_rows.extend(rows)
        loaded_paths.append(fpath)
        print(f'[INFO]   Merged {len(rows):>6,} rows from {fname}')

    if schema_errors:
        raise ValueError(
            f'{label.title()} folder — cannot merge files due to schema mismatch:\n'
            + '\n'.join(schema_errors)
            + '\n  All files in the folder must have identical column headers.'
        )

    return ref_headers or [], merged_rows, loaded_paths


def _load_column_mapping(path: str, delimiter: str = ',') -> List[Dict]:
    _, rows = _load_csv(path, delimiter=delimiter)
    required = {'source_column', 'destination_column', 'matching_rule'}
    if rows:
        missing = required - set(rows[0].keys())
        if missing:
            raise ValueError(
                f'column_mapping.csv is missing required columns: {sorted(missing)}. '
                f'Required: source_column, destination_column, matching_rule'
            )
    return rows


def _load_value_mapping(path: str, delimiter: str = ',') -> Dict[str, Dict[str, str]]:
    _, rows = _load_csv(path, delimiter=delimiter)
    result: Dict[str, Dict[str, str]] = defaultdict(dict)
    if not rows:
        return result
    required = {'column_name', 'source_value', 'destination_value'}
    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError(
            f'value_mapping.csv is missing required columns: {sorted(missing)}. '
            f'Required: column_name, source_value, destination_value'
        )
    for row in rows:
        col = row['column_name'].strip()
        src_val = row['source_value'].strip()
        dst_val = row['destination_value'].strip()
        result[col][src_val] = dst_val
    return result


# ---------------------------------------------------------------------------
# Column profiling
# ---------------------------------------------------------------------------

def _infer_type(values: List[str]) -> str:
    non_null = [v for v in values if v not in ('', None)]
    if not non_null:
        return 'string'

    bool_vals = {'true', 'false', 'yes', 'no', '0', '1', 'y', 'n'}
    if all(v.lower() in bool_vals for v in non_null):
        return 'boolean'

    try:
        [int(v) for v in non_null]
        return 'integer'
    except ValueError:
        pass

    try:
        [float(v) for v in non_null]
        return 'decimal'
    except ValueError:
        pass

    date_fmts = ['%Y-%m-%d', '%d-%b-%Y', '%m/%d/%Y', '%d/%m/%Y', '%Y%m%d']
    for fmt in date_fmts:
        try:
            [datetime.strptime(v, fmt) for v in non_null]
            return 'date'
        except ValueError:
            continue

    return 'string'


def _profile_column(col_name: str, values: List[str]) -> Dict:
    total = len(values)
    non_null = [v for v in values if v not in ('', None)]
    null_count = total - len(non_null)
    dtype = _infer_type(non_null)
    counter: Counter = Counter(non_null)

    p: Dict[str, Any] = {
        'column': col_name,
        'inferred_data_type': dtype,
        'row_count': total,
        'null_count': null_count,
        'null_percent': round(null_count / total * 100, 2) if total else 0,
        'distinct_count': len(counter),
        'distinct_percent': round(len(counter) / total * 100, 2) if total else 0,
        'top_values': [{'value': v, 'count': c} for v, c in counter.most_common(5)],
        'sample_values': non_null[:5],
    }

    if dtype in ('integer', 'decimal'):
        try:
            nums = [float(v) for v in non_null]
            p['min'] = min(nums)
            p['max'] = max(nums)
            p['mean'] = round(statistics.mean(nums), 4)
            p['median'] = statistics.median(nums)
            p['std'] = round(statistics.stdev(nums), 4) if len(nums) > 1 else 0.0
        except Exception:
            pass

    if dtype == 'date':
        date_fmts = ['%Y-%m-%d', '%d-%b-%Y', '%m/%d/%Y', '%d/%m/%Y', '%Y%m%d']
        parsed = []
        for v in non_null:
            for fmt in date_fmts:
                try:
                    parsed.append(datetime.strptime(v, fmt))
                    break
                except ValueError:
                    continue
        if parsed:
            p['min'] = min(parsed).isoformat()
            p['max'] = max(parsed).isoformat()

    if dtype == 'string':
        lengths = [len(v) for v in non_null]
        if lengths:
            p['length_stats'] = {
                'min_len': min(lengths),
                'max_len': max(lengths),
                'avg_len': round(sum(lengths) / len(lengths), 2),
            }

    p['has_whitespace_issues'] = any(v != v.strip() for v in non_null)
    p['has_case_inconsistency'] = (
        len({v.lower() for v in non_null}) < len(set(non_null)) if dtype == 'string' else False
    )
    p['has_invalid_dates'] = 0
    if dtype == 'string' and any(kw in col_name.lower() for kw in ('date', 'dt', 'time', 'day')):
        fmts = ['%Y-%m-%d', '%d-%b-%Y', '%m/%d/%Y']
        p['has_invalid_dates'] = sum(
            1 for v in non_null
            if not any(_try_parse_date(v, fmt) for fmt in fmts)
        )

    p['has_non_numeric_in_numeric_column'] = (
        sum(1 for v in non_null if not _is_numeric(v)) if dtype in ('integer', 'decimal') else 0
    )
    return p


def _try_parse_date(v: str, fmt: str) -> bool:
    try:
        datetime.strptime(v, fmt)
        return True
    except ValueError:
        return False


def _is_numeric(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def _profile_dataset(rows: List[Dict], columns: List[str]) -> List[Dict]:
    return [_profile_column(col, [str(r.get(col, '') or '') for r in rows]) for col in columns]


def _column_value_profiles(
    src_rows: List[Dict], dst_rows: List[Dict],
    col_mapping: List[Dict], val_mapping: Dict[str, Dict[str, str]],
) -> List[Dict]:
    """Per-column value-count comparison: how many times each value appears in source vs destination.

    Source values are transformed to the destination format before counting so
    gaps reflect genuine data differences, not format differences.

    Detection logic (handles both standard rule codes and human-readable descriptions):
      1. If val_mapping has a date-format spec entry (e.g. 'yyyy-mm-dd' → 'dd-MMM-yyyy')
         for this column → apply the corresponding DATE_FORMAT transformation.
      2. If val_mapping has regular value entries OR the rule string contains
         'VALUE_MAP' / 'VALUE MAPPING' → apply value mapping.
      3. Otherwise → apply apply_transformation(value, rule) — works for standard
         codes (DATE_FORMAT:…, STRIP_PREFIX:…, NUMERIC, DECIMAL, DIRECT).
    """
    # Date format token patterns recognised in val_mapping spec entries
    _DATE_TOKENS: Dict[str, str] = {
        'yyyy-mm-dd': 'YYYY-MM-DD', 'dd-mmm-yyyy': 'DD-MMM-YYYY',
        'mm/dd/yyyy': 'MM/DD/YYYY', 'dd/mm/yyyy':  'DD/MM/YYYY',
        'yyyy/mm/dd': 'YYYY/MM/DD', 'yyyymmdd':     'YYYYMMDD',
        'dd-mm-yyyy': 'DD-MM-YYYY', 'mm-dd-yyyy':   'MM-DD-YYYY',
    }

    def _date_fmt_rule(col_val_map: Dict[str, str]) -> Optional[str]:
        """Return a DATE_FORMAT:src->dst rule string if val_mapping holds a format spec."""
        for sv, dv in col_val_map.items():
            sv_key = sv.strip().lower()
            dv_key = dv.strip().lower()
            if sv_key in _DATE_TOKENS and dv_key in _DATE_TOKENS:
                return f'DATE_FORMAT:{_DATE_TOKENS[sv_key]}->{_DATE_TOKENS[dv_key]}'
        return None

    profiles = []
    for m in col_mapping:
        src_col   = m['source_column'].strip()
        dst_col   = m['destination_column'].strip()
        rule      = m['matching_rule'].strip()
        rule_up   = rule.upper()

        raw_src_vals: List[str] = [_ci_get(row, src_col) for row in src_rows]
        dst_vals:     List[str] = [_ci_get(row, dst_col) for row in dst_rows]
        dst_counter:  Counter   = Counter(dst_vals)
        col_val_map:  Dict[str, str] = val_mapping.get(src_col, {})

        rows_out: List[Dict] = []

        # ── 1. Date format spec stored in val_mapping ─────────────────────────
        dfr = _date_fmt_rule(col_val_map)
        if dfr:
            transformed: List[str] = []
            for sv in raw_src_vals:
                t, err = apply_transformation(sv, dfr)
                transformed.append(t if not err else sv)
            src_counter: Counter = Counter(transformed)
            all_vals = sorted(set(list(src_counter.keys()) + list(dst_counter.keys())))
            for v in all_vals:
                sc = src_counter.get(v, 0)
                dc = dst_counter.get(v, 0)
                rows_out.append({'source_value': v, 'dest_value': v,
                                 'source_count': sc, 'dest_count': dc, 'gap': sc - dc})

        # ── 2. Value mapping (by rule code OR by val_mapping having entries) ──
        elif col_val_map or 'VALUE_MAP' in rule_up or 'VALUE MAPPING' in rule_up:
            src_counter = Counter(raw_src_vals)
            covered_dst: set = set()
            for sv in sorted(src_counter.keys()):
                dv = col_val_map.get(sv, sv)
                sc = src_counter[sv]
                dc = dst_counter.get(dv, 0)
                covered_dst.add(dv)
                rows_out.append({'source_value': sv, 'dest_value': dv,
                                 'source_count': sc, 'dest_count': dc, 'gap': sc - dc})
            for dv in sorted(dst_counter.keys()):
                if dv not in covered_dst:
                    rows_out.append({'source_value': '', 'dest_value': dv,
                                     'source_count': 0, 'dest_count': dst_counter[dv],
                                     'gap': -dst_counter[dv]})

        # ── 3. Rule-based transformation (standard codes) ────────────────────
        else:
            transformed = []
            for sv in raw_src_vals:
                t, err = apply_transformation(sv, rule)
                transformed.append(t if not err else sv)
            src_counter = Counter(transformed)
            all_vals = sorted(set(list(src_counter.keys()) + list(dst_counter.keys())))
            for v in all_vals:
                sc = src_counter.get(v, 0)
                dc = dst_counter.get(v, 0)
                rows_out.append({'source_value': v, 'dest_value': v,
                                 'source_count': sc, 'dest_count': dc, 'gap': sc - dc})

        profiles.append({
            'source_column':      src_col,
            'destination_column': dst_col,
            'matching_rule':      rule,
            'rows':               rows_out,
            'total_source':       len(raw_src_vals),
            # total_matched_dest: count of mode-filtered destination rows for this column.
            # Reflects what View Data shows (matched rows only in leftout/union modes),
            # NOT the raw destination row count.
            'total_matched_dest': len(dst_vals),
        })
    return profiles


# ---------------------------------------------------------------------------
# Match-mode suppression map
# ---------------------------------------------------------------------------

_VALID_MODES = {'full', 'leftout', 'rightout', 'union'}

# Issue types suppressed for each mode
#
# Suppression aligns with View Data visibility:
#   An issue type is suppressed when the rows it refers to are hidden from View Data.
#
#   full     – nothing hidden → nothing suppressed
#   leftout  – destination hides KEY_MISSING_IN_SOURCE rows + MIGRATION dest rows
#              → suppress KEY_MISSING_IN_SOURCE (dest-only records irrelevant)
#              KEY_MISSING_IN_DEST and MIGRATION_KEY_MISMATCH source rows remain visible
#   rightout – source hides KEY_MISSING_IN_DEST rows + MIGRATION source rows
#              → suppress KEY_MISSING_IN_DEST (source-only records irrelevant)
#              KEY_MISSING_IN_SOURCE and MIGRATION_KEY_MISMATCH dest rows remain visible
#   union    – inner join: both sides hide all non-exact-match rows
#              → suppress all three key/migration issue types; only show issues
#                 for exactly-matched records (VALUE_MISMATCH, NULL_MISMATCH, etc.)
_MODE_SUPPRESS: Dict[str, frozenset] = {
    'full':     frozenset(),
    'leftout':  frozenset({'KEY_MISSING_IN_SOURCE'}),
    'rightout': frozenset({'KEY_MISSING_IN_DEST'}),
    'union':    frozenset({'KEY_MISSING_IN_SOURCE', 'KEY_MISSING_IN_DEST', 'MIGRATION_KEY_MISMATCH'}),
}


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------

def validate(
    source_folder: str,
    dest_folder: str,
    colmap_path: str,
    valmap_path: str,
    outdir: str,
    tolerance: float = 0.01,
    key_cols_override: Optional[List[str]] = None,
    output_format: str = 'both',
    match_mode: str = 'full',
    source_sep: str = ',',
    dest_sep: str = ',',
    colmap_sep: str = ',',
    valmap_sep: str = ',',
) -> Dict:
    """Run the full validation pipeline and write output files.

    Returns a dict with two top-level keys:
      'summary'     — run metadata and aggregate counts (pass_rate, matched_key_count,
                      total_issues, mismatch_count_by_type, source/dest hidden row indices, …)
      'detail_rows' — list of issue dicts, one per detected problem.  Each dict contains:
                      composite_key, issue_type, severity, source_column, destination_column,
                      source_value_raw, source_value_transformed, dest_value_raw,
                      dest_value_transformed, rule_applied, expected_dest_value, difference,
                      source_row_index, dest_row_index, timestamp.
    """

    # ── Load inputs ───────────────────────────────────────────────────────
    print(f'[INFO] Loading source folder      : {source_folder}')
    src_headers, src_rows, src_files = _load_folder_csv(source_folder, 'source', delimiter=source_sep)
    print(f'[INFO]   Total: {len(src_rows):,} rows, {len(src_headers)} columns across '
          f'{len(src_files)} file(s)')

    print(f'[INFO] Loading destination folder : {dest_folder}')
    dst_headers, dst_rows, dst_files = _load_folder_csv(dest_folder, 'destination', delimiter=dest_sep)
    print(f'[INFO]   Total: {len(dst_rows):,} rows, {len(dst_headers)} columns across '
          f'{len(dst_files)} file(s)')

    print(f'[INFO] Loading column map   : {colmap_path}')
    col_mapping = _load_column_mapping(colmap_path, delimiter=colmap_sep)
    print(f'[INFO]   {len(col_mapping)} mapped column(s)')

    print(f'[INFO] Loading value map    : {valmap_path}')
    val_mapping = _load_value_mapping(valmap_path, delimiter=valmap_sep)
    total_val_rules = sum(len(v) for v in val_mapping.values())
    print(f'[INFO]   {total_val_rules} value rule(s) across {len(val_mapping)} column(s)')

    os.makedirs(outdir, exist_ok=True)

    # ── Resolve and validate match mode early ─────────────────────────────
    # Done here so comparison loops can use it to avoid generating issues
    # that would be hidden by the current mode (e.g. VALUE_MISMATCH for
    # MIGRATION_KEY_MISMATCH pairs whose rows are not shown in View Data).
    mode = match_mode.strip().lower()
    if mode not in _VALID_MODES:
        print(f'[WARN] Unknown match_mode "{match_mode}" — defaulting to "full". '
              f'Valid values: {", ".join(sorted(_VALID_MODES))}')
        mode = 'full'

    # ── Determine composite key columns ───────────────────────────────────
    if key_cols_override:
        src_key_cols = [c.strip() for c in key_cols_override]
    else:
        src_key_cols = [
            m['source_column'].strip()
            for m in col_mapping
            if str(m.get('is_key', '')).strip().lower() in ('true', 'yes', '1')
        ]

    if not src_key_cols:
        print('[WARN] No key columns found (is_key=true). Using first source column as key.')
        src_key_cols = [src_headers[0]] if src_headers else []

    src_to_dst_col = {m['source_column'].strip(): m['destination_column'].strip() for m in col_mapping}
    dst_key_cols = [src_to_dst_col.get(c, c) for c in src_key_cols]

    print(f'[INFO] Composite key (source)     : {src_key_cols}')
    print(f'[INFO] Composite key (destination): {dst_key_cols}')

    # ── Build key indices ─────────────────────────────────────────────────
    # Source keys are TRANSFORMED using matching_rule before indexing.
    # Destination keys are assumed to already be in transformed form (direct strip only).

    rule_by_src_col = {m['source_column'].strip(): m['matching_rule'].strip() for m in col_mapping}

    def _src_key(row: Dict) -> str:
        parts = []
        for col in src_key_cols:
            raw = _ci_get(row, col)
            rule = rule_by_src_col.get(col, 'DIRECT')
            transformed, _ = apply_transformation(raw, rule)
            parts.append(transformed)
        return '|'.join(parts)

    def _dst_key(row: Dict) -> str:
        return '|'.join(_ci_get(row, col) for col in dst_key_cols)

    src_key_index: Dict[str, List[int]] = defaultdict(list)
    for i, row in enumerate(src_rows):
        src_key_index[_src_key(row)].append(i)

    dst_key_index: Dict[str, List[int]] = defaultdict(list)
    for i, row in enumerate(dst_rows):
        dst_key_index[_dst_key(row)].append(i)

    # Reverse maps used by row_match_summary
    src_idx_to_key: Dict[int, str] = {
        idx: key for key, indices in src_key_index.items() for idx in indices
    }
    src_to_dst_idx: Dict[int, int] = {
        src_indices[0]: dst_key_index[key][0]
        for key, src_indices in src_key_index.items()
        if dst_key_index.get(key) and len(src_indices) == 1
    }

    # ── Validate ──────────────────────────────────────────────────────────
    timestamp = datetime.utcnow().isoformat() + 'Z'
    detail_rows: List[Dict] = []
    matched_src_idx: set = set()
    matched_dst_idx: set = set()

    def _issue(composite_key, src_i, dst_i, src_col, dst_col, issue_type,
               severity, rule_applied, src_raw, src_trans, dst_raw, dst_trans,
               expected_dst=None, difference=None):
        entry: Dict[str, Any] = {
            'composite_key': composite_key,
            'source_row_index': src_i,
            'dest_row_index': dst_i,
            'source_column': src_col,
            'destination_column': dst_col,
            'issue_type': issue_type,
            'severity': severity,
            'rule_applied': rule_applied,
            'source_value_raw': src_raw,
            'source_value_transformed': src_trans,
            'dest_value_raw': dst_raw,
            'dest_value_transformed': dst_trans,
            'expected_dest_value': expected_dst,
            'difference': difference,
            'timestamp': timestamp,
        }
        # Attach raw key field values for traceability
        if src_i is not None:
            for kc in src_key_cols:
                entry[f'key_{kc}_source'] = _ci_get(src_rows[src_i], kc)
        if dst_i is not None:
            for kc in dst_key_cols:
                entry[f'key_{kc}_dest'] = _ci_get(dst_rows[dst_i], kc)
        detail_rows.append(entry)

    # Duplicate key checks
    for key, indices in src_key_index.items():
        if len(indices) > 1:
            for idx in indices:
                _issue(key, idx, None, ','.join(src_key_cols), '', 'DUPLICATE_KEY_SOURCE',
                       'High', 'composite_key', key, key, '', '', None, None)

    for key, indices in dst_key_index.items():
        if len(indices) > 1:
            for idx in indices:
                _issue(key, None, idx, '', ','.join(dst_key_cols), 'DUPLICATE_KEY_DEST',
                       'High', 'composite_key', '', '', key, key, None, None)

    # Pre-compute the effective transformation rule once per column.
    # _effective_rule() involves dict lookups and regex scans — doing it here (once)
    # rather than inside the per-row loop avoids repeating that work for every matched pair.
    col_effective_rules: Dict[str, str] = {
        m['source_column'].strip(): _effective_rule(
            m['source_column'].strip(), m['matching_rule'].strip(), val_mapping
        )
        for m in col_mapping
    }
    # Warn once for columns using ambiguous date formats.
    _ambiguous_date_fmts = {'MM/DD/YYYY', 'DD/MM/YYYY'}
    for _col, _eff in col_effective_rules.items():
        if _eff.upper().startswith('DATE_FORMAT:'):
            _spec = _eff[len('DATE_FORMAT:'):]
            _src_tok = _spec.split('->')[0].strip().upper()
            if _src_tok in _ambiguous_date_fmts:
                print(f'[WARN] Column "{_col}" uses ambiguous date format "{_src_tok}" — '
                      f'MM/DD/YYYY and DD/MM/YYYY are indistinguishable when day ≤ 12. '
                      f'Verify the correct token is configured.')

    # Match and compare — collect unmatched non-duplicate src keys for partial pairing
    n_src_keys = len(src_key_index)
    print(f'[INFO] Matching and comparing {n_src_keys:,} source key(s) against destination ...')
    unmatched_src_keys: Dict[str, List[int]] = {}
    _progress_step = max(1, n_src_keys // 4)   # print at ~25 / 50 / 75 %

    for _ki, (key, src_indices) in enumerate(src_key_index.items(), 1):
        if n_src_keys >= 5000 and _ki % _progress_step == 0:
            print(f'[INFO]   ... {_ki:,}/{n_src_keys:,} keys processed ({_ki*100//n_src_keys}%)')
        dst_indices = dst_key_index.get(key)

        if not dst_indices:
            if len(src_indices) == 1:
                unmatched_src_keys[key] = src_indices   # defer: attempt partial pairing
            else:
                for idx in src_indices:                  # duplicates → report immediately
                    _issue(key, idx, None, ','.join(src_key_cols), '', 'KEY_MISSING_IN_DEST',
                           'High', 'composite_key', key, key, '', '', None, None)
            continue

        src_i = src_indices[0]
        dst_i = dst_indices[0]
        matched_src_idx.add(src_i)
        matched_dst_idx.add(dst_i)

        src_row = src_rows[src_i]
        dst_row = dst_rows[dst_i]

        for mapping in col_mapping:
            src_col = mapping['source_column'].strip()
            dst_col = mapping['destination_column'].strip()
            rule = mapping['matching_rule'].strip()

            if src_col in src_key_cols:
                continue  # key columns not compared as values

            if not _ci_has(src_row, src_col):
                _issue(key, src_i, dst_i, src_col, dst_col, 'COLUMN_MAPPING_MISSING',
                       'Medium', rule, '', '', '', '', None, None)
                continue

            src_raw = _ci_get(src_row, src_col)
            dst_raw = _ci_get(dst_row, dst_col)

            # Resolve effective rule (handles human-readable descriptions that map
            # to date-format specs stored in value_mapping.csv)
            effective_rule = _effective_rule(src_col, rule, val_mapping)

            # Apply transformation to source value
            src_trans, trans_err = apply_transformation(src_raw, effective_rule)
            if trans_err:
                _issue(key, src_i, dst_i, src_col, dst_col, 'TRANSFORMATION_ERROR',
                       'Medium', rule, src_raw, src_trans, dst_raw, dst_raw, None, None)
                continue

            # Apply value mapping (skip when a date-spec override was applied)
            expected_dst = None
            rule_applied = rule
            is_date_spec = effective_rule != rule
            if not is_date_spec and (rule.upper() == 'VALUE_MAP' or src_col in val_mapping):
                col_val_map = val_mapping.get(src_col, {})
                if src_raw in col_val_map:
                    src_trans = col_val_map[src_raw]
                    expected_dst = src_trans
                    rule_applied = f'{rule} ({src_raw}->{src_trans})'
                elif src_raw and rule.upper() == 'VALUE_MAP':
                    _issue(key, src_i, dst_i, src_col, dst_col, 'VALUE_MAPPING_MISSING',
                           'Medium', rule, src_raw, src_trans, dst_raw, dst_raw, None, None)
                    continue

            dst_trans = dst_raw  # destination already in final form

            # Null mismatch
            src_null = src_trans in ('', None)
            dst_null = dst_trans in ('', None)
            if src_null != dst_null:
                _issue(key, src_i, dst_i, src_col, dst_col, 'NULL_MISMATCH',
                       'Medium', rule_applied, src_raw, src_trans, dst_raw, dst_trans,
                       expected_dst, None)
                continue
            if src_null and dst_null:
                continue

            # Compare
            diff = None
            matched_val = False
            if 'DECIMAL' in rule.upper():
                # Exact value match ignoring decimal place formatting
                # e.g. 1200.50 == 1200.5 == 1200.500  but  1200.50 != 1200.51
                try:
                    s_dec = Decimal(src_trans).normalize()
                    d_dec = Decimal(dst_trans).normalize()
                    matched_val = (s_dec == d_dec)
                    diff = float(abs(s_dec - d_dec)) if not matched_val else None
                    # Normalise both for display (fixed-point, no scientific notation)
                    src_trans = format(s_dec, 'f')
                    dst_trans = format(d_dec, 'f')
                except InvalidOperation:
                    matched_val = src_trans.strip().lower() == dst_trans.strip().lower()
            else:
                try:
                    s_num = float(src_trans)
                    d_num = float(dst_trans)
                    diff = abs(s_num - d_num)
                    matched_val = diff <= tolerance
                except (ValueError, TypeError):
                    matched_val = src_trans.strip().lower() == dst_trans.strip().lower()

            if not matched_val:
                _issue(key, src_i, dst_i, src_col, dst_col, 'VALUE_MISMATCH',
                       'Low', rule_applied, src_raw, src_trans, dst_raw, dst_trans,
                       expected_dst, diff)

    # ── Partial composite-key pairing ────────────────────────────────────────
    # Non-duplicate dst keys that have no src match — candidates for pairing
    unmatched_dst_keys: Dict[str, List[int]] = {
        k: v for k, v in dst_key_index.items()
        if k not in src_key_index and len(v) == 1
    }

    def _compare_key_parts(sk: str, dk: str) -> List[Dict]:
        sp, dp = sk.split('|'), dk.split('|')
        if len(sp) != len(dp):
            return []
        return [
            {
                'src_col': src_key_cols[i] if i < len(src_key_cols) else f'key_{i}',
                'dst_col': dst_key_cols[i] if i < len(dst_key_cols) else f'key_{i}',
                'src_val': sp[i],
                'dst_val': dp[i],
                'matched': sp[i] == dp[i],
            }
            for i in range(len(sp))
        ]

    paired_src: set = set()
    paired_dst: set = set()

    n_unmatched_dst = len(unmatched_dst_keys)
    if n_unmatched_dst:
        print(f'[INFO] Partial-key pairing: {len(unmatched_src_keys):,} unmatched source key(s), '
              f'{n_unmatched_dst:,} unmatched destination key(s)')

    # Greedy best-score pairing
    for dst_key, dst_indices in unmatched_dst_keys.items():
        dp = dst_key.split('|')
        best_src, best_score = None, -1
        for src_key in unmatched_src_keys:
            if src_key in paired_src:
                continue
            sp = src_key.split('|')
            if len(sp) != len(dp):
                continue
            score = sum(1 for s, d in zip(sp, dp) if s == d)
            if score > best_score:
                best_score, best_src = score, src_key

        if best_src and best_score > 0:
            paired_src.add(best_src)
            paired_dst.add(dst_key)
            src_idx = unmatched_src_keys[best_src][0]
            dst_idx = dst_indices[0]
            detail_rows.append({
                'composite_key':           best_src,
                'dest_composite_key':      dst_key,
                'source_row_index':        src_idx,
                'dest_row_index':          dst_idx,
                'source_column':           ','.join(src_key_cols),
                'destination_column':      ','.join(dst_key_cols),
                'issue_type':              'MIGRATION_KEY_MISMATCH',
                'severity':                'High',
                'rule_applied':            'composite_key',
                'source_value_raw':        best_src,
                'source_value_transformed': best_src,
                'dest_value_raw':          dst_key,
                'dest_value_transformed':  dst_key,
                'key_parts_comparison':    _compare_key_parts(best_src, dst_key),
                'expected_dest_value':     None,
                'difference':              None,
                'timestamp':               timestamp,
            })

            # ── Also compare non-key column values for this partial-match pair ──
            # Only in 'full' mode: all other modes hide at least one side of the
            # migration pair from View Data, so column-level issues would never
            # be shown and must not be generated.
            if mode != 'full':
                continue
            src_row = src_rows[src_idx]
            dst_row = dst_rows[dst_idx]
            for mapping in col_mapping:
                src_col = mapping['source_column'].strip()
                dst_col = mapping['destination_column'].strip()
                rule    = mapping['matching_rule'].strip()

                if src_col in src_key_cols:
                    continue  # key columns already handled above

                if not _ci_has(src_row, src_col):
                    _issue(best_src, src_idx, dst_idx, src_col, dst_col,
                           'COLUMN_MAPPING_MISSING', 'Medium', rule,
                           '', '', '', '', None, None)
                    continue

                src_raw  = _ci_get(src_row, src_col)
                dst_raw  = _ci_get(dst_row, dst_col)
                effective_rule = col_effective_rules.get(src_col, rule)
                src_trans, trans_err = apply_transformation(src_raw, effective_rule)
                if trans_err:
                    _issue(best_src, src_idx, dst_idx, src_col, dst_col,
                           'TRANSFORMATION_ERROR', 'Medium', rule,
                           src_raw, src_trans, dst_raw, dst_raw, None, None)
                    continue

                expected_dst = None
                rule_applied = rule
                is_date_spec = effective_rule != rule
                if not is_date_spec and (rule.upper() == 'VALUE_MAP' or src_col in val_mapping):
                    col_val_map = val_mapping.get(src_col, {})
                    if src_raw in col_val_map:
                        src_trans    = col_val_map[src_raw]
                        expected_dst = src_trans
                        rule_applied = f'{rule} ({src_raw}->{src_trans})'
                    elif src_raw and rule.upper() == 'VALUE_MAP':
                        _issue(best_src, src_idx, dst_idx, src_col, dst_col,
                               'VALUE_MAPPING_MISSING', 'Medium', rule,
                               src_raw, src_trans, dst_raw, dst_raw, None, None)
                        continue

                dst_trans = dst_raw
                src_null  = src_trans in ('', None)
                dst_null  = dst_trans in ('', None)
                if src_null != dst_null:
                    _issue(best_src, src_idx, dst_idx, src_col, dst_col,
                           'NULL_MISMATCH', 'Medium', rule_applied,
                           src_raw, src_trans, dst_raw, dst_trans, expected_dst, None)
                    continue
                if src_null and dst_null:
                    continue

                diff = None
                matched_val = False
                if 'DECIMAL' in rule.upper():
                    try:
                        s_dec = Decimal(src_trans).normalize()
                        d_dec = Decimal(dst_trans).normalize()
                        matched_val = (s_dec == d_dec)
                        diff = float(abs(s_dec - d_dec)) if not matched_val else None
                        src_trans = format(s_dec, 'f')
                        dst_trans = format(d_dec, 'f')
                    except InvalidOperation:
                        matched_val = src_trans.strip().lower() == dst_trans.strip().lower()
                else:
                    try:
                        s_num = float(src_trans)
                        d_num = float(dst_trans)
                        diff  = abs(s_num - d_num)
                        matched_val = diff <= tolerance
                    except (ValueError, TypeError):
                        matched_val = src_trans.strip().lower() == dst_trans.strip().lower()

                if not matched_val:
                    _issue(best_src, src_idx, dst_idx, src_col, dst_col,
                           'VALUE_MISMATCH', 'Low', rule_applied,
                           src_raw, src_trans, dst_raw, dst_trans, expected_dst, diff)

    # KEY_MISSING_IN_DEST for unpaired source keys
    for src_key, src_indices in unmatched_src_keys.items():
        if src_key not in paired_src:
            for idx in src_indices:
                _issue(src_key, idx, None, ','.join(src_key_cols), '', 'KEY_MISSING_IN_DEST',
                       'High', 'composite_key', src_key, src_key, '', '', None, None)

    # KEY_MISSING_IN_SOURCE for unpaired / duplicate destination keys
    for key, dst_indices in dst_key_index.items():
        if key not in src_key_index:
            if key in paired_dst:
                continue   # already covered by MIGRATION_KEY_MISMATCH
            for idx in dst_indices:
                _issue(key, None, idx, '', ','.join(dst_key_cols), 'KEY_MISSING_IN_SOURCE',
                       'Medium', 'composite_key', '', '', key, key, None, None)

    # ── Apply match-mode filter ───────────────────────────────────────────
    # mode was already validated and resolved at the top of validate()
    suppress = _MODE_SUPPRESS[mode]

    # Collect hidden row indices BEFORE filtering so the dashboard can
    # hide those rows in View Data.
    #
    # Asymmetric join semantics:
    #   leftout  – source is primary: show ALL source rows (even unmatched).
    #              Destination shows ONLY exactly-matched rows; hide both
    #              KEY_MISSING_IN_SOURCE and MIGRATION_KEY_MISMATCH dest rows.
    #   rightout – destination is primary: show ALL destination rows.
    #              Source shows ONLY exactly-matched rows; hide both
    #              KEY_MISSING_IN_DEST and MIGRATION_KEY_MISMATCH src rows.
    #   union    – inner join: both sides show only exactly-matched rows.
    #   full     – show everything on both sides.
    src_hidden_indices: List[int] = []
    dst_hidden_indices: List[int] = []
    if mode in ('rightout', 'union'):
        # Source must be an exact match — hide KEY_MISSING_IN_DEST + MIGRATION
        src_hidden_indices = list({
            r['source_row_index'] for r in detail_rows
            if r['issue_type'] in ('KEY_MISSING_IN_DEST', 'MIGRATION_KEY_MISMATCH')
            and r.get('source_row_index') is not None
        })
    if mode in ('leftout', 'union'):
        # Destination must be an exact match — hide KEY_MISSING_IN_SOURCE + MIGRATION
        dst_hidden_indices = list({
            r['dest_row_index'] for r in detail_rows
            if r['issue_type'] in ('KEY_MISSING_IN_SOURCE', 'MIGRATION_KEY_MISMATCH')
            and r.get('dest_row_index') is not None
        })

    if suppress:
        before = len(detail_rows)
        detail_rows = [r for r in detail_rows if r['issue_type'] not in suppress]
        print(f'[INFO] Match mode: {mode} — suppressed {before - len(detail_rows)} issue(s) '
              f'({", ".join(sorted(suppress))})')
    else:
        print(f'[INFO] Match mode: {mode}')

    # Safety-net pass: remove issues that reference a HIDDEN primary row so that
    # every issue the user sees in the Issues tab has a navigable row in View Data.
    #
    # MIGRATION_KEY_MISMATCH is intentionally excluded — it always has one hidden
    # and one visible row (e.g. leftout hides the MIGRATION dest row but the source
    # row is fully visible).  Removing it would silently hide real discrepancies.
    # MIGRATION visibility is governed exclusively by _MODE_SUPPRESS above.
    if src_hidden_indices or dst_hidden_indices:
        _sh = set(src_hidden_indices)
        _dh = set(dst_hidden_indices)
        before2 = len(detail_rows)
        detail_rows = [
            r for r in detail_rows
            if r['issue_type'] == 'MIGRATION_KEY_MISMATCH'   # never removed by safety-net
            or not (
                (r.get('source_row_index') is not None and r['source_row_index'] in _sh) or
                (r.get('dest_row_index')   is not None and r['dest_row_index']   in _dh)
            )
        ]
        n_leaked = before2 - len(detail_rows)
        if n_leaked:
            print(f'[WARN] Safety-net removed {n_leaked} issue(s) referencing hidden rows — '
                  f'check that all comparison paths respect the mode guard')

    # ── Source record accounting self-check ───────────────────────────────
    # In modes that show all source rows (full / leftout), every source row
    # must appear in exactly one bucket: matched, KEY_MISSING_IN_DEST,
    # MIGRATION_KEY_MISMATCH, or DUPLICATE_KEY_SOURCE.  A gap here means
    # the engine has a bug that would cause silent data loss on the dashboard.
    #
    # Kept as [WARN] (not an exception) intentionally: raising here would abort
    # the run before any output is written, leaving the user with nothing to
    # inspect.  A visible warning in the console + the QA suite's accounting
    # test are the right remediation path — the partial output is still useful.
    if mode in ('full', 'leftout'):
        accounted: set = set(matched_src_idx)
        for r in detail_rows:
            if r['issue_type'] in ('KEY_MISSING_IN_DEST', 'MIGRATION_KEY_MISMATCH',
                                   'DUPLICATE_KEY_SOURCE') and r.get('source_row_index') is not None:
                accounted.add(r['source_row_index'])
        unaccounted = set(range(len(src_rows))) - accounted
        if unaccounted:
            print(f'[WARN] Source accounting gap: {len(unaccounted)} source row(s) not in any '
                  f'result bucket — possible engine bug. Indices: {sorted(unaccounted)[:10]}')

    # ── Summary ───────────────────────────────────────────────────────────
    issues_by_type = Counter(r['issue_type'] for r in detail_rows)
    issues_by_col = Counter(
        r['source_column'] for r in detail_rows if r['issue_type'] == 'VALUE_MISMATCH'
    )
    total_matched = len(matched_src_idx)
    pass_rate = round(total_matched / len(src_rows) * 100, 2) if src_rows else 0.0

    summary = {
        'source_row_count': len(src_rows),
        'destination_row_count': len(dst_rows),
        'source_key_count': len(src_key_index),
        'matched_key_count': total_matched,
        'unmatched_source_keys': len(src_key_index) - total_matched,
        'extra_dest_keys': sum(1 for k in dst_key_index if k not in src_key_index),
        'total_issues': len(detail_rows),
        'key_issues': sum(1 for r in detail_rows if r['severity'] == 'High'),
        'migration_key_count': issues_by_type.get('MIGRATION_KEY_MISMATCH', 0),
        'value_mismatch_count': issues_by_type.get('VALUE_MISMATCH', 0),
        'pass_rate': pass_rate,
        'mismatch_count_by_type': dict(issues_by_type),
        'mismatch_count_by_column': dict(issues_by_col),
        'composite_key_columns_source': src_key_cols,
        'composite_key_columns_dest': dst_key_cols,
        'run_timestamp': timestamp,
        'source_file': os.path.basename(source_folder),
        'dest_file': os.path.basename(dest_folder),
        'source_files': [os.path.basename(f) for f in src_files],
        'dest_files': [os.path.basename(f) for f in dst_files],
        'tolerance': tolerance,
        'match_mode': mode,
        'source_hidden_row_indices': src_hidden_indices,
        'dest_hidden_row_indices':   dst_hidden_indices,
        'sample_source_rows': src_rows[:5],
        'source_columns': src_headers,
    }

    # ── Profiling ─────────────────────────────────────────────────────────
    matched_src_rows = [src_rows[i] for i in sorted(matched_src_idx)]
    matched_dst_rows = [dst_rows[i] for i in sorted(matched_dst_idx)]

    # Filter source/destination rows according to match mode so that all
    # profiling views (column stats + value counts) reflect the same rows
    # that are visible in View Data for the current mode.
    _src_hidden_set = set(src_hidden_indices)
    _dst_hidden_set = set(dst_hidden_indices)
    mode_src_rows = [r for i, r in enumerate(src_rows) if i not in _src_hidden_set]
    mode_dst_rows = [r for i, r in enumerate(dst_rows) if i not in _dst_hidden_set]

    profiling: Dict = {
        'source':      {'column_profiles': _profile_dataset(mode_src_rows, src_headers)},
        'destination': {'column_profiles': _profile_dataset(mode_dst_rows, dst_headers)},
        'matched': {
            'source_column_profiles': _profile_dataset(matched_src_rows, src_headers),
            'dest_column_profiles':   _profile_dataset(matched_dst_rows, dst_headers),
        },
        'value_counts': _column_value_profiles(mode_src_rows, mode_dst_rows, col_mapping, val_mapping),
    }

    # Enrich source profiles with mapping metadata
    rule_lookup = {m['source_column'].strip(): m for m in col_mapping}
    for p in profiling['source']['column_profiles']:
        m = rule_lookup.get(p['column'])
        if m:
            p['mapping_info'] = {
                'source_column': m['source_column'],
                'destination_column': m['destination_column'],
            }
            p['rules_info'] = {
                'matching_rule': m['matching_rule'],
                'value_mapping_rules_used': [
                    {'source_value': sv, 'destination_value': dv}
                    for sv, dv in val_mapping.get(m['source_column'].strip(), {}).items()
                ],
            }

    # ── Mappings export ───────────────────────────────────────────────────
    mappings_export = {
        'column_mapping': col_mapping,
        'value_mapping': {
            col: [{'source_value': sv, 'destination_value': dv} for sv, dv in rules.items()]
            for col, rules in val_mapping.items()
        },
    }

    # ── Write outputs ─────────────────────────────────────────────────────
    results = {'summary': summary, 'detail_rows': detail_rows}

    if output_format in ('json', 'both'):
        _write_json(os.path.join(outdir, 'datapulse_results.json'), results)
        _write_json(os.path.join(outdir, 'profiling.json'), profiling)
        _write_json(os.path.join(outdir, 'mappings.json'), mappings_export)

    if output_format in ('csv', 'both'):
        _write_csv(os.path.join(outdir, 'datapulse_results.csv'), detail_rows)

    _write_row_summary(
        os.path.join(outdir, 'row_match_summary.csv'),
        src_rows, dst_rows, col_mapping,
        src_key_cols, src_idx_to_key, src_to_dst_idx, detail_rows,
    )

    dashboard_path = os.path.join(outdir, 'dashboard.html')

    # Load corruption summary when RUN_MODE is TEST
    corruption_summary = ''
    if RUN_MODE.strip().upper() == 'TEST':
        cs_path = os.path.join(os.path.dirname(os.path.abspath(colmap_path)), 'corruption_summary.txt')
        if os.path.isfile(cs_path):
            try:
                with open(cs_path, 'r', encoding='utf-8') as _csf:
                    corruption_summary = _csf.read()
                print(f'[INFO] TEST mode — loaded corruption summary: {cs_path}')
            except Exception as _e:
                print(f'[WARN] Could not read corruption summary: {_e}')
        else:
            print(f'[INFO] TEST mode — no corruption_summary.txt found at {cs_path}')

    _generate_dashboard(results, profiling, mappings_export, dashboard_path,
                        src_rows, src_headers, dst_rows, dst_headers,
                        corruption_summary=corruption_summary)

    print(f'\n[DONE] Pass rate: {pass_rate}% | Matched: {total_matched}/{len(src_rows)} | '
          f'Issues: {len(detail_rows)} | Dashboard: {dashboard_path}')
    return results


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _write_json(path: str, data: Any):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f'[INFO] Written: {path}')


def _write_csv(path: str, rows: List[Dict]):
    if rows:
        # Collect all fieldnames across every row (rows can have different dynamic key fields)
        seen: set = set()
        fieldnames: List[str] = []
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    fieldnames.append(k)
                    seen.add(k)
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows({k: row.get(k, '') for k in fieldnames} for row in rows)
    else:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('no_issues\n')
    print(f'[INFO] Written: {path}')


# ---------------------------------------------------------------------------
# Dashboard generator
# ---------------------------------------------------------------------------

def _generate_dashboard(results: Dict, profiling: Dict, mappings: Dict, out_path: str,
                        src_rows: List[Dict] = None, src_headers: List[str] = None,
                        dst_rows: List[Dict] = None, dst_headers: List[str] = None,
                        corruption_summary: str = ''):
    """Produce a fully self-contained, dependency-free interactive HTML dashboard."""

    def _safe_json(obj):
        return (
            json.dumps(obj, default=str)
            .replace('</script>', r'<\/script>')
            .replace('<!--', r'<\!--')
        )

    def _he(text):
        """Minimal HTML escape for plain-text blocks."""
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    r_json = _safe_json(results)
    p_json = _safe_json(profiling)
    m_json = _safe_json(mappings)
    src_json = _safe_json({'cols': src_headers or [], 'rows': [{**r, '_row_idx': i} for i, r in enumerate(src_rows or [])]})
    dst_json = _safe_json({'cols': dst_headers or [], 'rows': [{**r, '_row_idx': i} for i, r in enumerate(dst_rows or [])]})

    if corruption_summary.strip():
        _cs_onclick = (
            "var b=this.nextElementSibling,"
            "o=b.style.display==='none';"
            "b.style.display=o?'block':'none';"
            "this.querySelector('.cs-arrow').style.transform=o?'rotate(0deg)':'rotate(-90deg)'"
        )
        _cs_text = _he(corruption_summary)
        cs_block = (
            '<div class="card" style="margin-bottom:18px;border-color:#6366f1">'
            + f'<div style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none" onclick="{_cs_onclick}">'
            + '<h3 style="display:flex;align-items:center;gap:8px;margin:0">'
            '<span style="background:linear-gradient(135deg,#3b4fd8,#4338ca);color:#fff;'
            'border-radius:5px;padding:2px 9px;font-size:.63rem;letter-spacing:.09em;font-weight:700">'
            'TEST MODE</span>Corruption Summary</h3>'
            '<span class="cs-arrow" style="color:#6366f1;font-size:1.1rem;'
            'transition:transform .2s;display:inline-block">&#9660;</span>'
            '</div>'
            '<div style="margin-top:14px">'
            '<pre style="font-family:monospace;font-size:.76rem;color:#9aafc8;line-height:1.65;'
            'white-space:pre-wrap;padding:14px 16px;background:#07102a;border-radius:8px;'
            + f'border:1px solid #182040;overflow-x:auto;margin:0">{_cs_text}</pre>'
            + '</div></div>'
        )
    else:
        cs_block = ''

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DataPulse — Data Migration Validator</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#06091a;color:#e2e8f5;font-size:14px;line-height:1.5;min-height:100vh}}
button{{font-family:inherit;cursor:pointer}}
@keyframes fadeIn{{from{{opacity:0}}to{{opacity:1}}}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:none}}}}
@keyframes glow{{0%,100%{{box-shadow:0 0 10px rgba(99,102,241,.25)}}50%{{box-shadow:0 0 28px rgba(99,102,241,.55)}}}}
@keyframes dashDraw{{from{{stroke-dashoffset:320}}to{{stroke-dashoffset:0}}}}
/* ── Header ── */
.hdr{{background:linear-gradient(135deg,#07102a 0%,#0a1535 55%,#0c1840 100%);border-bottom:1px solid #182040;padding:0 36px;position:relative;overflow:hidden}}
.hdr::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 55% 130% at 0% 50%,rgba(99,102,241,.08),transparent 65%);pointer-events:none}}
.hdr-inner{{display:flex;align-items:center;justify-content:space-between;padding:17px 0;gap:20px;position:relative}}
.brand{{display:flex;align-items:center;gap:14px}}
.brand-icon{{width:40px;height:40px;border-radius:11px;flex-shrink:0;background:linear-gradient(135deg,#4f46e5,#7c3aed);display:flex;align-items:center;justify-content:center;box-shadow:0 0 22px rgba(99,102,241,.45);animation:glow 3s ease-in-out infinite}}
.brand-icon svg{{width:22px;height:22px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
.brand h1{{font-size:1.38rem;font-weight:800;color:#f0f4ff;letter-spacing:-.025em}}
.brand .sub{{font-size:.72rem;color:#8898b8;margin-top:1px}}
.hdr-ekg{{height:30px;width:190px;opacity:.55;flex-shrink:0}}
.hdr-stamp{{font-size:.69rem;color:#7a8eaf;background:#07102a;border:1px solid #182040;border-radius:8px;padding:5px 12px;font-family:monospace;white-space:nowrap}}
/* ── Nav ── */
.nav{{position:sticky;top:0;z-index:50;background:rgba(6,9,26,.95);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-bottom:1px solid #182040;display:flex;align-items:center;padding:0 36px;gap:1px;overflow-x:auto}}
.tab{{position:relative;padding:13px 16px;cursor:pointer;color:#8898b8;font-size:.81rem;font-weight:500;border:none;background:none;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;white-space:nowrap;flex-shrink:0}}
.tab:hover{{color:#8898b8}}
.tab.active{{color:#818cf8;border-bottom-color:#6366f1}}
.tab.active::after{{content:'';position:absolute;bottom:-1px;left:15%;right:15%;height:2px;background:linear-gradient(90deg,transparent,#6366f1,#8b5cf6,transparent);filter:blur(1px)}}
.nav-div{{width:1px;background:#182040;margin:10px 5px;align-self:stretch;flex-shrink:0}}
.nav-meta{{margin-left:auto;display:flex;align-items:center;gap:5px;padding:8px 0 8px 12px;flex-shrink:0}}
.meta-chip{{display:inline-flex;align-items:center;gap:4px;background:#07102a;border:1px solid #182040;border-radius:20px;padding:3px 10px;font-size:.68rem;color:#7a8eaf;white-space:nowrap}}
.meta-chip span{{color:#818cf8;font-weight:700}}
.tab-divider{{width:1px;background:#182040;margin:10px 5px;align-self:stretch;flex-shrink:0}}
.tab-util{{color:#7a8eaf;font-size:.78rem}}
.tab-util:hover{{color:#9aafc8}}
.tab-util.active{{color:#818cf8;border-bottom-color:#6366f1}}
/* ── Body ── */
.body{{padding:22px 36px}}
.panel{{display:none}}.panel.active{{display:block;animation:fadeIn .18s ease}}
/* ── KPI ── */
.kpi-row{{display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin-bottom:18px}}
.kpi{{background:#0e1830;border:1px solid #182040;border-radius:12px;padding:14px 12px 12px;position:relative;cursor:default;min-width:0;text-align:center;overflow:hidden;transition:border-color .2s,transform .18s}}
.kpi::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#6366f1 40%,#8b5cf6 60%,transparent);opacity:.7}}
.kpi:hover{{border-color:#2a3d6a;transform:translateY(-2px)}}
.kpi .lbl{{font-size:.65rem;color:#8898b8;font-weight:700;letter-spacing:.05em;text-transform:uppercase;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:18px;margin-bottom:3px}}
.kpi .val{{font-size:1.5rem;font-weight:800;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.025em}}
.kpi .sub{{font-size:.61rem;color:#6b7e9e;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.kpi .tip-icon{{position:absolute;top:9px;right:9px;width:15px;height:15px;border-radius:50%;background:#111d35;color:#7a8eaf;font-size:.6rem;display:flex;align-items:center;justify-content:center;font-weight:700;border:1px solid #182040}}
.tooltip{{position:absolute;bottom:calc(100% + 10px);left:50%;transform:translateX(-50%);background:#07102a;border:1px solid #243360;color:#c5d1e8;font-size:.71rem;padding:10px 14px;border-radius:10px;width:220px;line-height:1.55;z-index:200;pointer-events:none;opacity:0;transition:opacity .15s;text-transform:none;letter-spacing:0;font-weight:400;box-shadow:0 8px 32px rgba(0,0,0,.7);text-align:left}}
.tooltip::after{{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:5px solid transparent;border-top-color:#243360}}
.kpi:hover .tooltip{{opacity:1}}
/* ── Cards ── */
.card{{background:#0e1830;border:1px solid #182040;border-radius:12px;padding:20px;margin-bottom:18px;position:relative;overflow:hidden}}
.card::after{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(99,102,241,.22),transparent)}}
.card h3{{font-size:.73rem;font-weight:700;color:#9aafc8;margin-bottom:16px;letter-spacing:.07em;text-transform:uppercase}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
/* ── Run info ── */
.ri-row{{display:grid;grid-template-columns:42% 58%;padding:5px 0;border-bottom:1px solid #0e1628;font-size:.77rem}}
.ri-row:last-child{{border-bottom:none}}
.ri-k{{color:#7a8eaf;font-weight:500}}.ri-v{{color:#c5d1e8;font-weight:500;word-break:break-word}}
/* ── Charts ── */
.bar-item{{margin-bottom:12px}}.bar-item:last-child{{margin-bottom:0}}
.bar-lbl{{font-size:.72rem;color:#8898b8;margin-bottom:5px;display:flex;justify-content:space-between;align-items:center}}
.bar-count{{font-weight:700;color:#c5d1e8;font-size:.76rem;background:#07102a;border-radius:4px;padding:1px 7px;border:1px solid #182040}}
.bar-track{{background:#07102a;border-radius:6px;height:9px;overflow:hidden;border:1px solid #182040}}
.bar-fill{{height:100%;border-radius:6px;transition:width .7s cubic-bezier(.16,1,.3,1)}}
/* ── SP toggle ── */
.sp-wrap{{display:flex;background:#07102a;border:1px solid #182040;border-radius:10px;padding:3px;gap:2px;flex-shrink:0}}
.sp-btn{{padding:6px 18px;font-size:.78rem;font-weight:600;border:none;background:transparent;color:#7a8eaf;border-radius:8px;transition:all .15s}}
.sp-btn.active{{background:linear-gradient(135deg,#3b4fd8,#4338ca);color:#fff;box-shadow:0 2px 10px rgba(99,102,241,.4)}}
/* ── Tables ── */
.tbl-wrap{{overflow:auto;border-radius:10px;border:1px solid #182040}}
table{{width:100%;border-collapse:collapse;font-size:.78rem}}
th{{background:#07102a;color:#7a8eaf;font-weight:700;text-align:left;padding:10px 12px;border-bottom:1px solid #182040;white-space:nowrap;cursor:pointer;transition:color .15s;font-size:.67rem;letter-spacing:.06em;text-transform:uppercase;position:sticky;top:0;z-index:1}}
th:hover{{color:#c5d1e8}}
td{{padding:9px 12px;border-bottom:1px solid #0c1525;color:#c5d1e8;vertical-align:top;word-break:break-word}}
tr:hover td{{background:rgba(99,102,241,.04)}}
tr:last-child td{{border-bottom:none}}
/* ── Badges ── */
.badge{{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:.65rem;font-weight:700;letter-spacing:.04em;white-space:nowrap}}
.bH{{background:rgba(239,68,68,.14);color:#fc8585;border:1px solid rgba(239,68,68,.35)}}
.bM{{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.35)}}
.bL{{background:rgba(99,102,241,.12);color:#a5b4fc;border:1px solid rgba(99,102,241,.3)}}
/* ── Filters ── */
.filters{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center}}
.filters input,.filters select{{background:#07102a;border:1px solid #182040;border-radius:8px;padding:7px 12px;color:#e2e8f5;font-size:.78rem;transition:border-color .15s;outline:none;font-family:inherit}}
.filters input:focus,.filters select:focus{{border-color:#6366f1}}
.filters input{{min-width:220px}}
.cnt{{color:#7a8eaf;font-size:.74rem;margin-left:auto}}
/* ── Pagination ── */
.pg{{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:12px;font-size:.78rem;color:#7a8eaf}}
.pg button{{background:#0e1830;border:1px solid #182040;border-radius:8px;padding:5px 14px;color:#8898b8;transition:all .15s;font-size:.78rem;font-family:inherit}}
.pg button:hover:not(:disabled){{background:#182040;color:#c5d1e8;border-color:#2a3d6a}}
.pg button:disabled{{opacity:.25;cursor:default}}
/* ── Profiling ── */
.ptabs{{display:flex;gap:4px;margin-bottom:18px;flex-wrap:wrap;padding:3px;background:#07102a;border:1px solid #182040;border-radius:10px;width:fit-content}}
.ptab{{padding:6px 14px;border-radius:7px;cursor:pointer;font-size:.78rem;border:none;background:transparent;color:#7a8eaf;font-weight:500;transition:all .15s;font-family:inherit}}
.ptab:hover{{color:#8898b8}}.ptab.active{{background:#182040;color:#818cf8;font-weight:700}}
.pgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(265px,1fr));gap:14px}}
.pc{{background:#07102a;border:1px solid #182040;border-radius:10px;padding:16px;transition:border-color .15s}}
.pc:hover{{border-color:#2a3d6a}}
.pc .pn{{font-weight:700;color:#818cf8;margin-bottom:4px;font-size:.87rem}}
.pc .dt{{font-size:.64rem;color:#6b7e9e;background:#0e1830;border-radius:4px;padding:2px 8px;display:inline-block;margin-bottom:10px;border:1px solid #182040;letter-spacing:.05em;text-transform:uppercase}}
.ps{{display:flex;justify-content:space-between;font-size:.74rem;padding:3px 0;border-bottom:1px solid #0c1525}}
.ps:last-child{{border-bottom:none}}
.ps .k{{color:#7a8eaf}}.ps .v{{color:#c5d1e8;font-weight:500;text-align:right;max-width:60%;word-break:break-all}}
.chip{{display:inline-block;background:#0e1830;color:#818cf8;padding:2px 8px;border-radius:5px;font-size:.69rem;font-family:monospace;border:1px solid #1e2d54}}
.key-chip{{font-size:.63rem;background:rgba(99,102,241,.14);color:#818cf8;border-radius:4px;padding:2px 6px;font-weight:700;border:1px solid rgba(99,102,241,.35)}}
/* ── Row icons ── */
.row-icon{{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;font-size:.68rem;font-weight:700;cursor:default;flex-shrink:0;user-select:none}}
.ico-info{{background:rgba(99,102,241,.18);color:#818cf8;border:1px solid rgba(99,102,241,.3)}}
.ico-err{{background:rgba(239,68,68,.18);color:#f87171;border:1px solid rgba(239,68,68,.3)}}
.ico-warn{{background:rgba(245,158,11,.18);color:#fbbf24;border:1px solid rgba(245,158,11,.3)}}
/* ── View Data ── */
.vd-btn{{background:#07102a;border:1px solid #182040;border-radius:8px;padding:8px 20px;color:#8898b8;font-size:.82rem;font-weight:500;transition:all .15s;font-family:inherit}}
.vd-btn:hover{{background:#0e1830;color:#c5d1e8}}
.vd-btn.active{{background:linear-gradient(135deg,#3b4fd8,#4338ca);border-color:#6366f1;color:#fff;box-shadow:0 2px 12px rgba(99,102,241,.35)}}
.vd-fi{{min-width:105px;max-width:150px;background:#07102a;border:1px solid #182040;border-radius:6px;padding:5px 8px;color:#e2e8f5;font-size:.73rem;transition:border-color .15s;outline:none;font-family:inherit}}
.vd-fi:focus{{border-color:#6366f1}}
.vd-lc{{cursor:pointer;user-select:none;border-radius:10px;padding:10px 12px;transition:filter .15s,box-shadow .15s;position:relative;display:flex;align-items:flex-start;gap:8px;flex:1;min-width:0}}
.vd-lc:hover{{filter:brightness(1.12)}}
.vd-lc.vd-lc-active{{box-shadow:0 0 0 2px #6366f1,0 0 20px rgba(99,102,241,.2)}}
.vd-lc-badge{{display:none;position:absolute;top:-7px;right:-7px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border-radius:9px;font-size:.59rem;font-weight:800;padding:2px 6px;pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,.5)}}
.vd-lc-active .vd-lc-badge{{display:block}}
/* ── VC filter ── */
.vc-fbtn{{background:#0e1830;border:1px solid #182040;color:#8898b8;border-radius:7px;padding:5px 12px;font-size:.76rem;font-weight:500;transition:all .15s;font-family:inherit}}
.vc-fbtn:hover{{background:#182040;color:#c5d1e8}}
.vc-fbtn.active{{background:linear-gradient(135deg,#3b4fd8,#4338ca);border-color:#6366f1;color:#fff}}
/* ── Docs ── */
.doc-section{{margin-bottom:24px}}
.doc-section h4{{font-size:.76rem;font-weight:700;color:#818cf8;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #182040;letter-spacing:.07em;text-transform:uppercase}}
.doc-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
.doc-card{{background:#07102a;border:1px solid #182040;border-radius:10px;padding:16px}}
.doc-card h5{{font-size:.8rem;font-weight:700;color:#e2e8f5;margin-bottom:8px}}
.doc-card p{{font-size:.76rem;color:#8898b8;line-height:1.55;margin-bottom:6px}}
.doc-card p:last-child{{margin-bottom:0}}
.doc-tbl{{width:100%;border-collapse:collapse;font-size:.76rem;margin-top:6px}}
.doc-tbl th{{background:#07102a;color:#6b7e9e;font-weight:600;text-align:left;padding:7px 10px;border-bottom:1px solid #182040}}
.doc-tbl td{{padding:6px 10px;border-bottom:1px solid #0c1525;color:#c5d1e8;vertical-align:top}}
.doc-tbl tr:hover td{{background:#0e1830}}
.doc-tbl td:first-child{{font-family:monospace;color:#818cf8;white-space:nowrap}}
.doc-cmd{{background:#030812;border:1px solid #182040;border-radius:8px;padding:12px 16px;font-family:monospace;font-size:.76rem;color:#6ee7b7;margin:8px 0;line-height:1.6;white-space:pre-wrap}}
.doc-note{{background:rgba(99,102,241,.06);border-left:3px solid #6366f1;border-radius:0 8px 8px 0;padding:10px 14px;font-size:.75rem;color:#818cf8;line-height:1.55;margin:8px 0}}
/* ── Responsive ── */
@media(max-width:1100px){{.kpi-row{{grid-template-columns:repeat(4,1fr)}}}}
@media(max-width:700px){{.two-col{{grid-template-columns:1fr}}.kpi-row{{grid-template-columns:repeat(2,1fr)}}.body{{padding:16px 18px}}.hdr{{padding-left:18px;padding-right:18px}}.nav{{padding:0 18px}}.hdr-ekg{{display:none}}}}
</style>
</head>
<body>

<!-- HEADER -->
<header class="hdr">
<div class="hdr-inner">
  <div class="brand">
    <div class="brand-icon">
      <svg viewBox="0 0 24 24"><polyline points="2,12 5,12 7,4 9,20 11,10 13,16 15,12 22,12"/></svg>
    </div>
    <div>
      <h1>DataPulse</h1>
      <div class="sub">Your data migration heartbeat.</div>
    </div>
  </div>
  <svg class="hdr-ekg" viewBox="0 0 190 30" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <polyline points="0,15 25,15 31,7 37,23 43,15 58,15 64,3 70,27 76,15 100,15 106,9 112,21 118,15 145,15 151,10 157,20 163,15 190,15"
      fill="none" stroke="#10b981" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
      stroke-dasharray="310" stroke-dashoffset="310">
      <animate attributeName="stroke-dashoffset" from="310" to="0" dur="2s" fill="freeze" begin="0.4s"/>
    </polyline>
  </svg>
  <div class="hdr-stamp" id="rts"></div>
</div>
</header>

<!-- NAV -->
<nav class="nav">
  <button class="tab active" onclick="showTab('summary',this)">Summary</button>
  <button class="tab" onclick="showTab('issues',this)">Issues</button>
  <button class="tab" onclick="showTab('profiling',this)">Profiling</button>
  <button class="tab" onclick="showTab('mappings',this)">Mappings</button>
  <button class="tab" onclick="showTab('viewdata',this);if(!vd_inited){{vd_inited=true;showVD('source',document.getElementById('vd-src-btn'));}}">View Data</button>
  <div class="nav-div"></div>
  <button class="tab tab-util" onclick="showTab('howto',this)">&#63; How to Use</button>
  <button class="tab tab-util" onclick="showTab('readme',this)">&#9654; Read Me!</button>
  <button class="tab tab-util" onclick="showTab('qaresults',this)">&#10003; QA Results</button>
  <div class="nav-meta">
    <div class="meta-chip" title="Source row count">Source <span id="tmb-src">—</span></div>
    <div class="meta-chip" title="Destination row count">Destination <span id="tmb-dst">—</span></div>
    <div class="meta-chip" title="Match mode">Mode <span id="tmb-mode">—</span></div>
  </div>
</nav>

<div class="body">

<!-- SUMMARY -->
<div id="tab-summary" class="panel active">
  <div class="kpi-row" id="kpi-row"></div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-bottom:18px;align-items:stretch">
    <div class="card" style="margin-bottom:0"><h3>Run Information</h3><div id="run-info"></div></div>
    <div class="card" style="margin-bottom:0;display:flex;flex-direction:column"><h3>Issues by Type</h3><div id="by-type" style="flex:1;overflow-y:auto"></div></div>
    <div class="card" style="margin-bottom:0;display:flex;flex-direction:column"><h3>Value Mismatches by Column</h3><div id="by-col" style="flex:1;overflow-y:auto"></div></div>
  </div>
  <div class="card" style="min-width:0;overflow:hidden">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap">
      <div class="sp-wrap">
        <button id="sp-btn-src" class="sp-btn active" onclick="switchSP('src')">Source Records</button>
        <button id="sp-btn-dst" class="sp-btn" onclick="switchSP('dst')">Destination Records</button>
      </div>
      <span style="font-size:.71rem;color:#6b7e9e" id="sp-subtitle">raw data, no match mode applied</span>
      <div style="display:flex;align-items:center;gap:8px;margin-left:auto">
        <span style="color:#7a8eaf;font-size:.77rem">Rows per page:</span>
        <select id="sp-rpp" onchange="spRppChange()" style="background:#07102a;border:1px solid #182040;border-radius:7px;padding:4px 10px;color:#e2e8f5;font-size:.77rem;outline:none"></select>
        <span class="cnt" id="sp-cnt" style="white-space:nowrap"></span>
      </div>
    </div>
    <div class="tbl-wrap" style="max-height:none;overflow-x:auto;overflow-y:visible">
      <table><thead id="sp-thead"></thead><tbody id="sp-tbody"></tbody></table>
    </div>
    <div class="pg" style="margin-top:10px">
      <button id="sp-pbtn" onclick="pgSP(-1)">&#8249; Prev</button>
      <span id="sp-pinfo"></span>
      <button id="sp-nbtn" onclick="pgSP(1)">Next &#8250;</button>
    </div>
  </div>
</div>

<!-- ISSUES -->
<div id="tab-issues" class="panel">
  <div class="card">
    <div class="filters">
      <input id="srch" placeholder="Search key, column, value…" oninput="applyF()">
      <select id="ftype" onchange="applyF()"><option value="">All Types</option></select>
      <select id="fsev" onchange="applyF()"><option value="">All Severities</option><option>High</option><option>Medium</option><option>Low</option></select>
      <select id="fcol" onchange="applyF()"><option value="">All Columns</option></select>
      <span class="cnt" id="icnt"></span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th style="width:28px"></th>
          <th style="min-width:200px">Analysis</th>
          <th onclick="sortBy('composite_key')">Composite Key</th>
          <th onclick="sortBy('source_column')">Src Col</th>
          <th onclick="sortBy('destination_column')">Dst Col</th>
          <th onclick="sortBy('issue_type')">Issue Type</th>
          <th onclick="sortBy('severity')">Sev</th>
          <th>Src Raw</th><th>Src Transformed</th><th>Dest Value</th><th>Expected</th>
          <th onclick="sortBy('difference')">Diff</th><th>Rule</th>
        </tr></thead>
        <tbody id="itbody"></tbody>
      </table>
    </div>
    <div class="pg"><button id="pbtn" onclick="pg(-1)" disabled>‹</button><span id="pinfo"></span><button id="nbtn" onclick="pg(1)">›</button></div>
  </div>
</div>

<!-- PROFILING -->
<div id="tab-profiling" class="panel">
  <div class="ptabs">
    <button class="ptab active" data-pt="source" onclick="showPT(this)">Source</button>
    <button class="ptab" data-pt="destination" onclick="showPT(this)">Destination</button>
    <button class="ptab" data-pt="matched_src" onclick="showPT(this)">Matched (Src)</button>
    <button class="ptab" data-pt="matched_dst" onclick="showPT(this)">Matched (Dst)</button>
    <button class="ptab" data-pt="value_counts" onclick="showPT(this)">Column Value Profiling</button>
  </div>
  <div class="pgrid" id="pgrid"></div>
  <div id="vcgrid" style="display:none"></div>
</div>

<!-- MAPPINGS -->
<div id="tab-mappings" class="panel">
  {cs_block}
  <div class="two-col">
    <div class="card"><h3>Column Mappings</h3>
      <div class="tbl-wrap" style="max-height:none"><table>
        <thead><tr><th>Source Column</th><th>Destination Column</th><th>Rule</th><th>Key?</th></tr></thead>
        <tbody id="cmtbody"></tbody>
      </table></div>
    </div>
    <div class="card"><h3>Value Mappings</h3>
      <div class="tbl-wrap" style="max-height:none"><table>
        <thead><tr><th>Column</th><th>Source Value</th><th>&#8594; Destination</th></tr></thead>
        <tbody id="vmtbody"></tbody>
      </table></div>
    </div>
  </div>

  <!-- RULE GUIDE -->
  <div class="card" style="margin-top:4px">
    <div style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none" onclick="var b=document.getElementById('rg-body'),o=b.style.display==='none';b.style.display=o?'block':'none';document.getElementById('rg-arr').style.transform=o?'rotate(0deg)':'rotate(-90deg)'">
      <h3 style="margin-bottom:0;display:flex;align-items:center;gap:8px">
        <span style="background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;border-radius:5px;padding:2px 9px;font-size:.63rem;letter-spacing:.09em;font-weight:700">GUIDE</span>
        Column Mapping Rule Reference
      </h3>
      <span id="rg-arr" style="color:#6366f1;font-size:1.1rem;transition:transform .2s;display:inline-block">&#9660;</span>
    </div>
    <div id="rg-body" style="margin-top:20px">

      <!-- FILE FORMATS -->
      <div style="margin-bottom:26px">
        <div style="font-size:.71rem;font-weight:700;color:#818cf8;letter-spacing:.07em;text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #182040">Mapping File Formats</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
          <div style="background:#07102a;border:1px solid #182040;border-radius:9px;padding:14px 16px">
            <div style="font-size:.7rem;font-weight:700;color:#9aafc8;margin-bottom:8px;letter-spacing:.04em">column_mapping.csv</div>
            <div style="font-family:monospace;font-size:.73rem;color:#c5d1e8;line-height:1.9">
              <span style="color:#7a8eaf">Required columns:</span><br>
              <span style="color:#818cf8">source_column</span>, <span style="color:#818cf8">destination_column</span>, <span style="color:#818cf8">matching_rule</span>, <span style="color:#6ee7b7">is_key</span><br>
              <span style="color:#7a8eaf">Example rows:</span><br>
              <span style="color:#c5d1e8">Customer_ID, cust_key, STRIP_PREFIX:CUST|NUMERIC, false</span><br>
              <span style="color:#c5d1e8">CD_Number, cd_key, STRIP_PREFIX:CD-|NUMERIC, <span style="color:#6ee7b7">true</span></span>
            </div>
            <div style="margin-top:10px;font-size:.68rem;color:#7a8eaf;line-height:1.65">
              Set <span style="color:#6ee7b7;font-weight:700">is_key = true</span> on columns that together uniquely identify a record (composite key). All key columns combined must be unique per row. Column name matching is case-insensitive and whitespace-tolerant.
            </div>
          </div>
          <div style="background:#07102a;border:1px solid #182040;border-radius:9px;padding:14px 16px">
            <div style="font-size:.7rem;font-weight:700;color:#9aafc8;margin-bottom:8px;letter-spacing:.04em">value_mapping.csv</div>
            <div style="font-family:monospace;font-size:.73rem;color:#c5d1e8;line-height:1.9">
              <span style="color:#7a8eaf">Required columns:</span><br>
              <span style="color:#818cf8">column_name</span>, <span style="color:#818cf8">source_value</span>, <span style="color:#818cf8">destination_value</span><br>
              <span style="color:#7a8eaf">Example rows (Interest_Payout_Frequency):</span><br>
              <span style="color:#c5d1e8">Interest_Payout_Frequency, MONTHLY, M</span><br>
              <span style="color:#c5d1e8">Interest_Payout_Frequency, QUARTERLY, Q</span><br>
              <span style="color:#c5d1e8">Interest_Payout_Frequency, AT_MATURITY, A</span>
            </div>
            <div style="margin-top:10px;font-size:.68rem;color:#7a8eaf;line-height:1.65">
              Required only when <span style="color:#a5b4fc;font-family:monospace">matching_rule = VALUE_MAP</span>. One row per source&#8594;destination substitution. The <span style="color:#818cf8">column_name</span> must match the <span style="color:#818cf8">source_column</span> from column_mapping.csv (case-insensitive).
            </div>
          </div>
        </div>
      </div>

      <!-- RULES REFERENCE -->
      <div style="margin-bottom:26px">
        <div style="font-size:.71rem;font-weight:700;color:#818cf8;letter-spacing:.07em;text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #182040">Matching Rules Reference</div>
        <div class="tbl-wrap" style="max-height:none">
          <table>
            <thead>
              <tr>
                <th style="width:20%">Rule</th>
                <th style="width:32%">What It Does</th>
                <th style="width:24%">Example &nbsp;(source &#8594; result)</th>
                <th style="width:24%">When to Use</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">DIRECT</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Trims leading/trailing whitespace, then compares case-insensitively. No value transformation applied.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">USD</span> &#8594; <span style="color:#6ee7b7">USD</span><br><span style="color:#fbbf24"> Active </span> &#8594; <span style="color:#6ee7b7">active</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Plain text columns — currency codes, country codes, names — that are identical in both systems (case or spacing may differ).</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">TRIM</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Strips leading and trailing whitespace from the source value. Equivalent to DIRECT when used alone; useful as an explicit first step in a chain.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">&nbsp; USD &nbsp;</span> &#8594; <span style="color:#6ee7b7">USD</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Use at the start of a chain when source values may have padding spaces before another rule is applied (e.g. <span style="font-family:monospace">TRIM|NUMERIC</span>).</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">UPPERCASE</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Converts the source value to all uppercase letters before comparing.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">active</span> &#8594; <span style="color:#6ee7b7">ACTIVE</span><br><span style="color:#fbbf24">New York</span> &#8594; <span style="color:#6ee7b7">NEW YORK</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Source stores mixed or lowercase text; destination stores uppercase. Can be chained (e.g. <span style="font-family:monospace">TRIM|UPPERCASE</span>).</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">LOWERCASE</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Converts the source value to all lowercase letters before comparing.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">ACTIVE</span> &#8594; <span style="color:#6ee7b7">active</span><br><span style="color:#fbbf24">New York</span> &#8594; <span style="color:#6ee7b7">new york</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Source stores mixed or uppercase text; destination stores lowercase. Can be chained (e.g. <span style="font-family:monospace">TRIM|LOWERCASE</span>).</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">NUMERIC</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Parses both sides as floating-point numbers and compares within the configured tolerance (default 0.01). Whole-number results drop the decimal.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">5.2500</span> &#8594; <span style="color:#6ee7b7">5.25</span><br><span style="color:#fbbf24">12.00</span> &#8594; <span style="color:#6ee7b7">12</span><br><span style="color:#fbbf24">001234</span> &#8594; <span style="color:#6ee7b7">1234</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Interest rates, amounts, term months, counts — any number where precision or leading zeros may differ between systems.</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">DECIMAL</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Strips trailing zeros then compares exactly. 1200.50 equals 1200.5 — but 1200.51 does not. Stricter than NUMERIC (no tolerance band).</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">899.90</span> &#8594; <span style="color:#6ee7b7">899.9</span><br><span style="color:#fbbf24">1200.00</span> &#8594; <span style="color:#6ee7b7">1200</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Financial amounts where the exact value must match but trailing-zero formatting differs (e.g. Oracle vs SQL Server decimal storage).</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">VALUE_MAP</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Looks up the source value in value_mapping.csv and substitutes the mapped destination value before comparing. Reports VALUE_MAPPING_MISSING if no entry exists.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">MONTHLY</span> &#8594; <span style="color:#6ee7b7">M</span><br><span style="color:#fbbf24">ACTIVE</span> &#8594; <span style="color:#6ee7b7">A</span><br><span style="color:#fbbf24">YES</span> &#8594; <span style="color:#6ee7b7">1</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Source uses descriptive text / long codes; destination uses short codes or numeric flags. Requires entries in value_mapping.csv for every possible source value.</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">STRIP_PREFIX:&lt;p&gt;</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Removes the leading prefix <em>p</em> from the source value (case-insensitive). The remainder is kept as-is and passed to the next rule in the chain.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">CUST001234</span> &#8594; <span style="color:#6ee7b7">001234</span><br><span style="color:#fbbf24">BR-001</span> &#8594; <span style="color:#6ee7b7">001</span><br><span style="color:#fbbf24">CD-1000001</span> &#8594; <span style="color:#6ee7b7">1000001</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Source IDs carry a system prefix that destination omits. Chain with NUMERIC to also strip leading zeros, or with ZEROPAD to pad to a fixed length.</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">ZEROPAD:&lt;N&gt;</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Left-pads the value with zeros until it is exactly N characters long. If the value is already N or more characters it is not changed.</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">1001</span> &#8594; <span style="color:#6ee7b7">0000001001</span> (N=10)<br><span style="color:#fbbf24">CUST1001</span> &#8594; <span style="color:#6ee7b7">000000001001</span> (strip+pad)</td>
                <td style="font-size:.74rem;color:#7a8eaf">Destination stores fixed-width zero-padded IDs. Chain after STRIP_PREFIX when the source also has a text prefix.</td>
              </tr>
              <tr>
                <td><span style="font-family:monospace;font-size:.79rem;color:#a5b4fc;background:#0e1830;padding:3px 8px;border-radius:4px;border:1px solid #1e2d54;white-space:nowrap">DATE_FORMAT:<br>&lt;src&gt;-&gt;&lt;dst&gt;</span></td>
                <td style="font-size:.77rem;color:#c5d1e8">Parses the source date using the <em>src</em> token and reformats it using the <em>dst</em> token before comparing to the destination. Month abbreviations are uppercased (JAN, FEB…).</td>
                <td style="font-family:monospace;font-size:.75rem"><span style="color:#fbbf24">2024-01-15</span> &#8594; <span style="color:#6ee7b7">15-JAN-2024</span><br><span style="color:#fbbf24">20240115</span> &#8594; <span style="color:#6ee7b7">01/15/2024</span></td>
                <td style="font-size:.74rem;color:#7a8eaf">Source and destination store dates in different formats (ISO vs Oracle, US vs EU, compact vs delimited). See Date Format Tokens below.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- CHAINING -->
      <div style="margin-bottom:26px">
        <div style="font-size:.71rem;font-weight:700;color:#818cf8;letter-spacing:.07em;text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #182040">Chaining Rules with |</div>
        <div style="font-size:.77rem;color:#9aafc8;margin-bottom:12px;line-height:1.65">
          Separate multiple rules with <span style="font-family:monospace;color:#a5b4fc;background:#0e1830;padding:1px 6px;border-radius:4px;border:1px solid #1e2d54">|</span> to apply them left-to-right in sequence. The output of each step feeds into the next. This lets you compose complex transformations from simple building blocks.
        </div>
        <div class="tbl-wrap" style="max-height:none">
          <table>
            <thead>
              <tr><th style="width:32%">Rule Chain</th><th style="width:16%">Source Value</th><th style="width:32%">Step-by-Step</th><th style="width:20%">Final Result</th></tr>
            </thead>
            <tbody>
              <tr>
                <td style="font-family:monospace;font-size:.76rem;color:#a5b4fc">STRIP_PREFIX:CUST|NUMERIC</td>
                <td style="font-family:monospace;font-size:.76rem;color:#fbbf24">CUST001234</td>
                <td style="font-size:.74rem;color:#9aafc8">1. Strip "CUST" &#8594; <span style="font-family:monospace">001234</span><br>2. NUMERIC &#8594; parse as number, drop leading zeros</td>
                <td style="font-family:monospace;font-size:.76rem;color:#6ee7b7">1234</td>
              </tr>
              <tr>
                <td style="font-family:monospace;font-size:.76rem;color:#a5b4fc">STRIP_PREFIX:CD-|NUMERIC</td>
                <td style="font-family:monospace;font-size:.76rem;color:#fbbf24">CD-1000001</td>
                <td style="font-size:.74rem;color:#9aafc8">1. Strip "CD-" &#8594; <span style="font-family:monospace">1000001</span><br>2. NUMERIC &#8594; already integer, unchanged</td>
                <td style="font-family:monospace;font-size:.76rem;color:#6ee7b7">1000001</td>
              </tr>
              <tr>
                <td style="font-family:monospace;font-size:.76rem;color:#a5b4fc">STRIP_PREFIX:BR-|NUMERIC</td>
                <td style="font-family:monospace;font-size:.76rem;color:#fbbf24">BR-001</td>
                <td style="font-size:.74rem;color:#9aafc8">1. Strip "BR-" &#8594; <span style="font-family:monospace">001</span><br>2. NUMERIC &#8594; drop leading zeros</td>
                <td style="font-family:monospace;font-size:.76rem;color:#6ee7b7">1</td>
              </tr>
              <tr>
                <td style="font-family:monospace;font-size:.76rem;color:#a5b4fc">STRIP_PREFIX:CUST|ZEROPAD:12</td>
                <td style="font-family:monospace;font-size:.76rem;color:#fbbf24">CUST1001</td>
                <td style="font-size:.74rem;color:#9aafc8">1. Strip "CUST" &#8594; <span style="font-family:monospace">1001</span><br>2. ZEROPAD:12 &#8594; pad left to 12 chars</td>
                <td style="font-family:monospace;font-size:.76rem;color:#6ee7b7">000000001001</td>
              </tr>
              <tr>
                <td style="font-family:monospace;font-size:.76rem;color:#a5b4fc">STRIP_PREFIX:E|ZEROPAD:8</td>
                <td style="font-family:monospace;font-size:.76rem;color:#fbbf24">E4521</td>
                <td style="font-size:.74rem;color:#9aafc8">1. Strip "E" &#8594; <span style="font-family:monospace">4521</span><br>2. ZEROPAD:8 &#8594; pad left to 8 chars</td>
                <td style="font-family:monospace;font-size:.76rem;color:#6ee7b7">00004521</td>
              </tr>
              <tr>
                <td style="font-family:monospace;font-size:.76rem;color:#a5b4fc">ZEROPAD:10</td>
                <td style="font-family:monospace;font-size:.76rem;color:#fbbf24">1001</td>
                <td style="font-size:.74rem;color:#9aafc8">1. ZEROPAD:10 &#8594; pad left to 10 chars (no prefix to strip)</td>
                <td style="font-family:monospace;font-size:.76rem;color:#6ee7b7">0000001001</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- DATE TOKENS -->
      <div style="margin-bottom:26px">
        <div style="font-size:.71rem;font-weight:700;color:#818cf8;letter-spacing:.07em;text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #182040">Date Format Tokens</div>
        <div style="font-size:.77rem;color:#9aafc8;margin-bottom:12px">
          Use these tokens in <span style="font-family:monospace;color:#a5b4fc;background:#0e1830;padding:2px 7px;border-radius:4px;border:1px solid #1e2d54">DATE_FORMAT:&lt;source_token&gt;-&gt;&lt;dest_token&gt;</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px">
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">YYYY-MM-DD</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">DD-MMM-YYYY</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">MM/DD/YYYY</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">DD/MM/YYYY</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">YYYY/MM/DD</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">YYYYMMDD</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">DD-MM-YYYY</span>
          <span style="font-family:monospace;font-size:.75rem;color:#a5b4fc;background:#07102a;border:1px solid #1e2d54;border-radius:5px;padding:4px 10px">MM-DD-YYYY</span>
        </div>
        <div class="tbl-wrap" style="max-height:none">
          <table>
            <thead><tr><th>Token</th><th>Format Description</th><th>Example</th><th>Common System</th></tr></thead>
            <tbody>
              <tr><td style="font-family:monospace;color:#a5b4fc">YYYY-MM-DD</td><td style="font-size:.76rem;color:#9aafc8">Year-Month-Day (ISO 8601)</td><td style="font-family:monospace;color:#6ee7b7">2024-01-15</td><td style="font-size:.74rem;color:#7a8eaf">REST APIs, PostgreSQL, MySQL, Python defaults</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">DD-MMM-YYYY</td><td style="font-size:.76rem;color:#9aafc8">Day-MonthAbbr-Year (uppercased)</td><td style="font-family:monospace;color:#6ee7b7">15-JAN-2024</td><td style="font-size:.74rem;color:#7a8eaf">Oracle DB, banking core systems, SWIFT</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">MM/DD/YYYY</td><td style="font-size:.76rem;color:#9aafc8">Month/Day/Year (US format)</td><td style="font-family:monospace;color:#6ee7b7">01/15/2024</td><td style="font-size:.74rem;color:#7a8eaf">US Excel exports, American legacy systems</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">DD/MM/YYYY</td><td style="font-size:.76rem;color:#9aafc8">Day/Month/Year (European format)</td><td style="font-family:monospace;color:#6ee7b7">15/01/2024</td><td style="font-size:.74rem;color:#7a8eaf">UK/EU systems, SAP European instances</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">YYYY/MM/DD</td><td style="font-size:.76rem;color:#9aafc8">Year/Month/Day with slashes</td><td style="font-family:monospace;color:#6ee7b7">2024/01/15</td><td style="font-size:.74rem;color:#7a8eaf">Some Asian systems, certain CSV exports</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">YYYYMMDD</td><td style="font-size:.76rem;color:#9aafc8">Compact ISO (no separators)</td><td style="font-family:monospace;color:#6ee7b7">20240115</td><td style="font-size:.74rem;color:#7a8eaf">Mainframe exports, file naming, EDI</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">DD-MM-YYYY</td><td style="font-size:.76rem;color:#9aafc8">Day-Month-Year with dashes</td><td style="font-family:monospace;color:#6ee7b7">15-01-2024</td><td style="font-size:.74rem;color:#7a8eaf">Some EU/AU legacy systems</td></tr>
              <tr><td style="font-family:monospace;color:#a5b4fc">MM-DD-YYYY</td><td style="font-size:.76rem;color:#9aafc8">Month-Day-Year with dashes</td><td style="font-family:monospace;color:#6ee7b7">01-15-2024</td><td style="font-size:.74rem;color:#7a8eaf">Some US legacy systems</td></tr>
            </tbody>
          </table>
        </div>
        <div style="margin-top:10px;font-size:.7rem;color:#7a8eaf;line-height:1.65;background:#07102a;border:1px solid rgba(245,158,11,.25);border-radius:8px;padding:10px 14px">
          <strong style="color:#fbbf24">Warning — ambiguous formats:</strong> MM/DD/YYYY and DD/MM/YYYY are indistinguishable when the day value is 12 or less (e.g. 01/06/2024 could be 1 Jun or 6 Jan). Use the correct token for your data to avoid silent mismatches.
        </div>
      </div>

      <!-- REAL-WORLD EXAMPLES -->
      <div>
        <div style="font-size:.71rem;font-weight:700;color:#818cf8;letter-spacing:.07em;text-transform:uppercase;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #182040">Real-World Examples — CD (Certificate of Deposit) Dataset</div>
        <div style="font-size:.77rem;color:#9aafc8;margin-bottom:12px;line-height:1.65">
          The table below shows all 14 column mappings from the CD sample dataset. Use it as a reference for how to combine rules to handle real migration scenarios.
        </div>
        <div class="tbl-wrap" style="max-height:none">
          <table>
            <thead>
              <tr><th>Source Column</th><th>Destination Column</th><th>Rule</th><th>Source Example</th><th>&#8594; Result</th><th>Key?</th><th>Notes</th></tr>
            </thead>
            <tbody>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Customer_ID</td>
                <td style="color:#c5d1e8;font-size:.76rem">cust_key</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc;white-space:nowrap">STRIP_PREFIX:CUST|NUMERIC</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">CUST001234</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">1234</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Strip "CUST" then drop leading zeros</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">CD_Number</td>
                <td style="color:#c5d1e8;font-size:.76rem">cd_key</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc;white-space:nowrap">STRIP_PREFIX:CD-|NUMERIC</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">CD-1000001</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">1000001</td>
                <td><span class="key-chip">KEY</span></td>
                <td style="font-size:.72rem;color:#7a8eaf">Primary record key — must be unique per row</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Issue_Date</td>
                <td style="color:#c5d1e8;font-size:.76rem">issue_dt</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc;white-space:nowrap">DATE_FORMAT:YYYY-MM-DD-&gt;DD-MMM-YYYY</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">2024-01-15</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">15-JAN-2024</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">ISO &#8594; Oracle/banking format</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Term_Months</td>
                <td style="color:#c5d1e8;font-size:.76rem">term_mo</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">NUMERIC</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">12.00</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">12</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Decimal form vs integer — NUMERIC handles both</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Principal_Amount</td>
                <td style="color:#c5d1e8;font-size:.76rem">principal_amt</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">NUMERIC</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">10000.50</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">10000.5</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Tolerates minor floating-point rounding</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Interest_Rate</td>
                <td style="color:#c5d1e8;font-size:.76rem">rate_pct</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">NUMERIC</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">5.2500</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">5.25</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Extra trailing zeros stripped</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Interest_Payout_Frequency</td>
                <td style="color:#c5d1e8;font-size:.76rem">payout_freq_cd</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">VALUE_MAP</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">MONTHLY / QUARTERLY / AT_MATURITY</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">M / Q / A</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Descriptive text &#8594; short code</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Rate_Type</td>
                <td style="color:#c5d1e8;font-size:.76rem">rate_type_cd</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">VALUE_MAP</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">FIXED / VARIABLE</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">F / V</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Two-way lookup</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Auto_Renew</td>
                <td style="color:#c5d1e8;font-size:.76rem">auto_renew_ind</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">VALUE_MAP</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">YES / NO</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">1 / 0</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Boolean text &#8594; integer flag</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Status</td>
                <td style="color:#c5d1e8;font-size:.76rem">status_cd</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">VALUE_MAP</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">ACTIVE / MATURED / CLOSED</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">A / M / C</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Three-way lookup</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Branch_Code</td>
                <td style="color:#c5d1e8;font-size:.76rem">branch_id</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc;white-space:nowrap">STRIP_PREFIX:BR-|NUMERIC</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">BR-001</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">1</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Strip dash-prefix, drop leading zeros</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Currency</td>
                <td style="color:#c5d1e8;font-size:.76rem">ccy_cd</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">DIRECT</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">USD</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">USD</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Identical in both — no transformation needed</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Interest_Accrual_Method</td>
                <td style="color:#c5d1e8;font-size:.76rem">accrual_cd</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">VALUE_MAP</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">30_360 / ACT_365</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">30360 / ACT365</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Underscore removed in destination code</td>
              </tr>
              <tr>
                <td style="color:#c5d1e8;font-size:.76rem">Brokered_Flag</td>
                <td style="color:#c5d1e8;font-size:.76rem">brokered_ind</td>
                <td style="font-family:monospace;font-size:.73rem;color:#a5b4fc">VALUE_MAP</td>
                <td style="font-family:monospace;font-size:.75rem;color:#fbbf24">YES / NO</td>
                <td style="font-family:monospace;font-size:.75rem;color:#6ee7b7">1 / 0</td>
                <td></td>
                <td style="font-size:.72rem;color:#7a8eaf">Boolean text &#8594; integer flag</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

    </div><!-- /rg-body -->
  </div><!-- /rule guide card -->
</div>

<!-- VIEW DATA -->
<div id="tab-viewdata" class="panel">
  <div class="card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap">
      <button class="vd-btn active" id="vd-src-btn" onclick="showVD('source',this)">Source Data&nbsp;<span id="vd-src-cnt" style="font-size:.72rem;font-weight:700;background:rgba(255,255,255,.13);border-radius:20px;padding:1px 8px;letter-spacing:.01em"></span></button>
      <button class="vd-btn" id="vd-dst-btn" onclick="showVD('destination',this)">Destination Data&nbsp;<span id="vd-dst-cnt" style="font-size:.72rem;font-weight:700;background:rgba(255,255,255,.13);border-radius:20px;padding:1px 8px;letter-spacing:.01em"></span></button>
      <div style="display:flex;align-items:center;gap:8px;margin-left:auto">
        <span style="color:#7a8eaf;font-size:.77rem">Rows per page:</span>
        <select id="vd-rpp" onchange="vdRppChange()" style="background:#07102a;border:1px solid #182040;border-radius:7px;padding:4px 10px;color:#e2e8f5;font-size:.77rem;outline:none"></select>
        <span class="cnt" id="vd-cnt" style="white-space:nowrap"></span>
      </div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:nowrap;margin-bottom:16px">
      <div id="vd-lc-mismatch" class="vd-lc" onclick="vdLegendFilter('mismatch',this)" title="Click to filter — click again to clear" style="background:rgba(239,68,68,0.09);border:1px solid rgba(239,68,68,0.3)">
        <span class="vd-lc-badge">ON</span>
        <span style="width:10px;height:10px;border-radius:3px;background:rgba(239,68,68,0.7);flex-shrink:0;margin-top:3px"></span>
        <div><div style="font-size:.74rem;font-weight:700;color:#fca5a5">Value Mismatch&nbsp;<span id="vd-cnt-mismatch" style="font-size:.7rem;background:rgba(239,68,68,.18);color:#fca5a5;border-radius:20px;padding:1px 7px"></span></div><div style="font-size:.66rem;color:#7a8eaf;margin-top:1px">Matched but columns differ</div></div>
      </div>
      <div id="vd-lc-green" class="vd-lc" onclick="vdLegendFilter('green',this)" title="Click to filter — click again to clear" style="background:rgba(16,185,129,0.09);border:1px solid rgba(16,185,129,0.3)">
        <span class="vd-lc-badge">ON</span>
        <span style="width:10px;height:10px;border-radius:3px;background:rgba(16,185,129,0.7);flex-shrink:0;margin-top:3px"></span>
        <div><div id="vd-lbl-green" style="font-size:.74rem;font-weight:700;color:#6ee7b7">Missing in Destination&nbsp;<span id="vd-cnt-green" style="font-size:.7rem;background:rgba(16,185,129,.18);color:#6ee7b7;border-radius:20px;padding:1px 7px"></span></div><div id="vd-sub-green" style="font-size:.66rem;color:#7a8eaf;margin-top:1px">Source key not in destination</div></div>
      </div>
      <div id="vd-lc-skyblue" class="vd-lc" onclick="vdLegendFilter('skyblue',this)" title="Click to filter — click again to clear" style="background:rgba(56,189,248,0.09);border:1px solid rgba(56,189,248,0.3)">
        <span class="vd-lc-badge">ON</span>
        <span style="width:10px;height:10px;border-radius:3px;background:rgba(56,189,248,0.7);flex-shrink:0;margin-top:3px"></span>
        <div><div id="vd-lbl-skyblue" style="font-size:.74rem;font-weight:700;color:#38bdf8">Extra in Destination&nbsp;<span id="vd-cnt-skyblue" style="font-size:.7rem;background:rgba(56,189,248,.18);color:#38bdf8;border-radius:20px;padding:1px 7px"></span></div><div id="vd-sub-skyblue" style="font-size:.66rem;color:#7a8eaf;margin-top:1px">Destination key not in source</div></div>
      </div>
      <div id="vd-lc-pass" class="vd-lc" onclick="vdLegendFilter('pass',this)" title="Click to filter — click again to clear" style="background:rgba(99,102,241,0.07);border:1px solid rgba(99,102,241,0.2)">
        <span class="vd-lc-badge">ON</span>
        <span style="width:10px;height:10px;border-radius:3px;background:#1e2d42;border:1px solid #2d3c56;flex-shrink:0;margin-top:3px"></span>
        <div><div style="font-size:.74rem;font-weight:700;color:#818cf8">No Issue&nbsp;<span id="vd-cnt-pass" style="font-size:.7rem;background:rgba(99,102,241,.18);color:#818cf8;border-radius:20px;padding:1px 7px"></span></div><div style="font-size:.66rem;color:#7a8eaf;margin-top:1px">All column values passed</div></div>
      </div>
    </div>
    <div style="overflow-x:auto;margin-bottom:12px">
      <div style="display:flex;gap:5px;flex-wrap:nowrap;min-width:max-content" id="vd-filters"></div>
    </div>
    <div class="tbl-wrap" style="max-height:none;overflow-x:auto;overflow-y:visible">
      <table><thead id="vd-thead"></thead><tbody id="vd-tbody"></tbody></table>
    </div>
    <div class="pg">
      <button id="vd-pbtn" onclick="pgVD(-1)" disabled>&#8249;</button>
      <span id="vd-pinfo"></span>
      <button id="vd-nbtn" onclick="pgVD(1)">&#8250;</button>
    </div>
  </div>
</div>

<!-- HOW TO USE -->
<div id="tab-howto" class="panel">
<div style="padding:4px 0 18px">
  <div class="doc-section">
    <h4>Quick Start — three steps</h4>
    <div class="doc-grid" style="grid-template-columns:repeat(3,1fr)">
      <div class="doc-card">
        <h5>1 &nbsp;Configure</h5>
        <p>Open <code style="color:#818cf8">check_datapulse.py</code> and update the config block at the top:</p>
        <div class="doc-cmd">folder_path   = "My_Dataset"
SOURCE_FOLDER = folder_path + "/Source"
DEST_FOLDER   = folder_path + "/Destination"
COLMAP_PATH   = folder_path + "/Mapping_Rule/column_mapping.csv"
VALMAP_PATH   = folder_path + "/Mapping_Rule/value_mapping.csv"
OUTPUT_DIR    = "output"
MATCH_MODE    = "leftout"</div>
        <p>Drop source CSV(s) in <code style="color:#818cf8">Source/</code> and destination CSV(s) in <code style="color:#818cf8">Destination/</code>. Multiple files are merged automatically.</p>
      </div>
      <div class="doc-card">
        <h5>2 &nbsp;Run the engine</h5>
        <p>From the project folder:</p>
        <div class="doc-cmd">python check_datapulse.py</div>
        <p>Or pass CLI overrides:</p>
        <div class="doc-cmd">python check_datapulse.py \
  --source My_Dataset/Source \
  --dest   My_Dataset/Destination \
  --outdir output \
  leftout</div>
        <p>Output files are written to <code style="color:#818cf8">output/</code>.</p>
      </div>
      <div class="doc-card">
        <h5>3 &nbsp;Run QA &amp; review</h5>
        <p>Validate the output:</p>
        <div class="doc-cmd">python QA_Validation/run_qa.py</div>
        <p>Open this dashboard in any browser — no server needed. Use the <b>QA Results</b> tab to review the QA report inline.</p>
        <div class="doc-note">All output files are self-contained. Share <code>dashboard.html</code> or <code>QA_Testing_Result.html</code> without any other files.</div>
      </div>
    </div>
  </div>
  <div class="doc-section">
    <h4>Dashboard Tabs</h4>
    <table class="doc-tbl">
      <tr><th style="width:140px">Tab</th><th>What it shows</th></tr>
      <tr><td>Summary</td><td>KPI cards, Run Information, Issues by Type chart, Value Mismatches by Column, Source / Destination record browser</td></tr>
      <tr><td>Issues</td><td>Filterable, sortable table of every issue. Hover the icon for full detail. MIGRATION rows show colour-coded key-part comparison.</td></tr>
      <tr><td>Profiling</td><td>Column-level statistics for Source / Destination / Matched subsets. Column Value Profiling shows side-by-side value counts after transformation.</td></tr>
      <tr><td>Mappings</td><td>Column mapping and value mapping rule tables as loaded from CSV files.</td></tr>
      <tr><td>View Data</td><td>Browse mode-filtered source or destination data with row highlighting, per-column filters, sortable headers.</td></tr>
      <tr><td>QA Results</td><td>QA Testing Result report embedded inline. Use the Reload button to refresh after re-running QA.</td></tr>
    </table>
  </div>
  <div class="doc-section">
    <h4>View Data — Row Colours</h4>
    <table class="doc-tbl">
      <tr><th style="width:160px">Colour</th><th>Meaning</th><th>Legend Filter</th></tr>
      <tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:rgba(239,68,68,0.5);vertical-align:middle;margin-right:6px"></span>Red</td><td>Row matched on key but one or more column values differ</td><td>Value Mismatch</td></tr>
      <tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:rgba(16,185,129,0.5);vertical-align:middle;margin-right:6px"></span>Green</td><td>Source key not found in destination (or MIGRATION source row)</td><td>Missing in Destination</td></tr>
      <tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:rgba(56,189,248,0.5);vertical-align:middle;margin-right:6px"></span>Sky-blue</td><td>Destination key not found in source (or MIGRATION destination row)</td><td>Extra in Destination</td></tr>
      <tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:#1e2d42;border:1px solid #2d3c56;vertical-align:middle;margin-right:6px"></span>None</td><td>Row matched and all column values passed</td><td>No Issue</td></tr>
    </table>
    <div class="doc-note" style="margin-top:10px">Click any legend card to filter View Data to only that row type. Click again to clear the filter.</div>
  </div>
  <div class="doc-section">
    <h4>Match Modes</h4>
    <table class="doc-tbl">
      <tr><th style="width:100px">Mode</th><th>Source View Data</th><th>Destination View Data</th><th>Issues suppressed</th></tr>
      <tr><td>full</td><td>All source rows</td><td>All destination rows</td><td>None</td></tr>
      <tr><td>leftout</td><td>All source rows</td><td>Exact-match rows only</td><td>KEY_MISSING_IN_SOURCE</td></tr>
      <tr><td>rightout</td><td>Exact-match rows only</td><td>All destination rows</td><td>KEY_MISSING_IN_DEST</td></tr>
      <tr><td>union</td><td>Exact-match rows only</td><td>Exact-match rows only</td><td>KEY_MISSING_IN_SOURCE, KEY_MISSING_IN_DEST, MIGRATION_KEY_MISMATCH</td></tr>
    </table>
  </div>
  <div class="doc-section">
    <h4>Issue Types</h4>
    <table class="doc-tbl">
      <tr><th style="width:220px">Issue</th><th style="width:80px">Severity</th><th>Description</th></tr>
      <tr><td>KEY_MISSING_IN_DEST</td><td><span style="color:#fc8585;font-weight:600">High</span></td><td>Source composite key has no matching record in the destination</td></tr>
      <tr><td>KEY_MISSING_IN_SOURCE</td><td><span style="color:#fbbf24;font-weight:600">Medium</span></td><td>Destination composite key has no matching record in the source</td></tr>
      <tr><td>MIGRATION_KEY_MISMATCH</td><td><span style="color:#fc8585;font-weight:600">High</span></td><td>Partial key match found — one or more key parts differ</td></tr>
      <tr><td>DUPLICATE_KEY_SOURCE</td><td><span style="color:#fc8585;font-weight:600">High</span></td><td>Composite key appears more than once in the source</td></tr>
      <tr><td>DUPLICATE_KEY_DEST</td><td><span style="color:#fc8585;font-weight:600">High</span></td><td>Composite key appears more than once in the destination</td></tr>
      <tr><td>VALUE_MISMATCH</td><td><span style="color:#a5b4fc;font-weight:600">Low</span></td><td>Matched row has a column value difference after transformation</td></tr>
      <tr><td>NULL_MISMATCH</td><td><span style="color:#fbbf24;font-weight:600">Medium</span></td><td>One side is null/empty, the other is not</td></tr>
      <tr><td>TRANSFORMATION_ERROR</td><td><span style="color:#fbbf24;font-weight:600">Medium</span></td><td>Transformation rule failed to process the source value</td></tr>
      <tr><td>VALUE_MAPPING_MISSING</td><td><span style="color:#fbbf24;font-weight:600">Medium</span></td><td>Source value has no entry in value_mapping.csv</td></tr>
      <tr><td>COLUMN_MAPPING_MISSING</td><td><span style="color:#fbbf24;font-weight:600">Medium</span></td><td>Mapped source column not found in the source data</td></tr>
    </table>
  </div>
</div>
</div>

<!-- READ ME -->
<div id="tab-readme" class="panel">
<div style="padding:4px 0 18px">
  <div class="doc-section">
    <h4>Project Overview</h4>
    <div class="doc-note"><b>DataPulse</b> — a generalized CSV data validation engine. Compares any source and destination CSV pair. Schema is driven entirely by two mapping files — no hardcoded column names. Run with <code>python check_datapulse.py</code>. Validate output with <code>python QA_Validation/run_qa.py</code>.</div>
  </div>
  <div class="doc-section">
    <h4>Folder Structure</h4>
    <div class="doc-cmd">My_Dataset/
&#9500;&#9472; Source/                &#9612; drop source CSV file(s) here
&#9474;   &#9492;&#9472; data_source.csv
&#9500;&#9472; Destination/           &#9612; drop destination CSV file(s) here
&#9474;   &#9492;&#9472; data_destination.csv
&#9492;&#9472; Mapping_Rule/
    &#9500;&#9472; column_mapping.csv  &#9612; defines column pairs + rules
    &#9492;&#9472; value_mapping.csv   &#9612; defines code translations

QA_Validation/
&#9492;&#9472; QA_Testing_Result.html  &#9612; Generated QA report</div>
  </div>
  <div class="doc-section">
    <h4>Mapping Files</h4>
    <div class="doc-grid">
      <div class="doc-card">
        <h5>column_mapping.csv</h5>
        <p>Defines source &#8594; destination column pairs and transformation rules. Set <code>is_key=true</code> for composite key columns.</p>
        <div class="doc-cmd">source_column,destination_column,matching_rule,is_key
Customer_ID,cust_key,STRIP_PREFIX:CUST|NUMERIC,True
Issue_Date,issue_dt,DATE_FORMAT:YYYY-MM-DD->DD-MMM-YYYY,True
Status,status_cd,VALUE_MAP,False
Amount,amount,NUMERIC,False</div>
      </div>
      <div class="doc-card">
        <h5>value_mapping.csv</h5>
        <p>Defines code translations for VALUE_MAP columns.</p>
        <div class="doc-cmd">column_name,source_value,destination_value,rule_description
Status,ACTIVE,A,CD is currently active
Status,MATURED,M,CD has matured
Status,CLOSED,C,CD has been closed
Auto_Renew,YES,1,Auto-renewal enabled
Auto_Renew,NO,0,Auto-renewal disabled</div>
        <p>Also supports date format specs: set <code>source_value = YYYY-MM-DD</code> and <code>destination_value = DD-MMM-YYYY</code> to define date conversion without a DATE_FORMAT rule code.</p>
      </div>
    </div>
  </div>
  <div class="doc-section">
    <h4>Matching Rules</h4>
    <table class="doc-tbl">
      <tr><th style="width:240px">Rule</th><th>Description</th></tr>
      <tr><td>DIRECT</td><td>Trim + case-insensitive string compare</td></tr>
      <tr><td>NUMERIC</td><td>Parse as float, compare within tolerance (default &#177;0.01). Whole numbers match integers: "001234" = "1234"</td></tr>
      <tr><td>DECIMAL</td><td>Exact value ignoring trailing zeros — 1200.50 = 1200.5 but 1200.50 &#8800; 1200.51</td></tr>
      <tr><td>VALUE_MAP</td><td>Translate source value via value_mapping.csv</td></tr>
      <tr><td>STRIP_PREFIX:&lt;p&gt;</td><td>Remove leading prefix before compare — e.g. STRIP_PREFIX:E turns E101 &#8594; 101</td></tr>
      <tr><td>DATE_FORMAT:SRC-&gt;DST</td><td>Convert date format — e.g. DATE_FORMAT:YYYY-MM-DD-&gt;DD-MMM-YYYY</td></tr>
    </table>
    <div class="doc-note" style="margin-top:8px">Rules can be chained with <code>|</code> — e.g. <code>STRIP_PREFIX:CD-|NUMERIC</code></div>
    <div class="doc-note" style="margin-top:6px">Date format tokens: YYYY-MM-DD &nbsp;&#183;&nbsp; DD-MMM-YYYY &nbsp;&#183;&nbsp; MM/DD/YYYY &nbsp;&#183;&nbsp; DD/MM/YYYY &nbsp;&#183;&nbsp; YYYY/MM/DD &nbsp;&#183;&nbsp; YYYYMMDD &nbsp;&#183;&nbsp; DD-MM-YYYY &nbsp;&#183;&nbsp; MM-DD-YYYY</div>
  </div>
  <div class="doc-section">
    <h4>CLI Reference</h4>
    <div class="doc-cmd">python check_datapulse.py \
  --source  Data/Source \
  --dest    Data/Destination \
  --colmap  Data/Mapping_Rule/column_mapping.csv \
  --valmap  Data/Mapping_Rule/value_mapping.csv \
  --outdir  output \
  --format  both \
  --tolerance 0.01 \
  leftout</div>
    <table class="doc-tbl" style="margin-top:10px">
      <tr><th style="width:130px">Flag</th><th>Default</th><th>Description</th></tr>
      <tr><td>--source</td><td>SOURCE_FOLDER</td><td>Folder containing source CSV file(s) — all .csv files merged</td></tr>
      <tr><td>--dest</td><td>DEST_FOLDER</td><td>Folder containing destination CSV file(s) — all .csv files merged</td></tr>
      <tr><td>--colmap</td><td>COLMAP_PATH</td><td>Path to column_mapping.csv</td></tr>
      <tr><td>--valmap</td><td>VALMAP_PATH</td><td>Path to value_mapping.csv</td></tr>
      <tr><td>--outdir</td><td>output</td><td>Directory to write output files</td></tr>
      <tr><td>--format</td><td>both</td><td>json / csv / both</td></tr>
      <tr><td>--tolerance</td><td>0.01</td><td>Numeric comparison tolerance</td></tr>
      <tr><td>mode (positional)</td><td>leftout</td><td>full / leftout / rightout / union</td></tr>
    </table>
  </div>
  <div class="doc-section">
    <h4>Output Files</h4>
    <table class="doc-tbl">
      <tr><th style="width:220px">File</th><th>Description</th></tr>
      <tr><td>dashboard.html</td><td>Self-contained interactive dashboard — this file</td></tr>
      <tr><td>datapulse_results.json</td><td>Summary object + full issue detail rows</td></tr>
      <tr><td>datapulse_results.csv</td><td>Flat CSV of issues only</td></tr>
      <tr><td>row_match_summary.csv</td><td>One row per source record with PASS/FAIL per column</td></tr>
      <tr><td>profiling.json</td><td>Column statistics for source / destination / matched subsets</td></tr>
      <tr><td>mappings.json</td><td>Normalized column and value mapping rules</td></tr>
    </table>
  </div>
</div>
</div>

<!-- QA RESULTS -->
<div id="tab-qaresults" class="panel">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
    <span style="font-size:.77rem;color:#7a8eaf">Embedded QA report &mdash; run <code style="color:#818cf8;font-size:.77rem">python QA_Validation/run_qa.py</code> first to generate it</span>
    <div style="display:flex;gap:8px;align-items:center">
      <button onclick="var f=document.getElementById('qa-frame');f.src=f.src" style="background:#0e1830;border:1px solid #182040;color:#9aafc8;font-size:.74rem;padding:4px 10px;border-radius:6px" title="Reload the QA report">&#8635; Reload</button>
      <a href="../QA_Validation/QA_Testing_Result.html" target="_blank" style="background:#0e1830;border:1px solid #182040;color:#9aafc8;font-size:.74rem;padding:4px 10px;border-radius:6px;text-decoration:none" title="Open in a new browser tab">&#8599; New Tab</a>
    </div>
  </div>
  <iframe id="qa-frame" src="../QA_Validation/QA_Testing_Result.html"
    style="width:100%;height:calc(100vh - 155px);border:1px solid #182040;border-radius:10px;background:#07102a;display:block"></iframe>
</div>

</div>
<script>
const DATA={r_json};
const PROF={p_json};
const MAPS={m_json};
const PAGE=50;
let fi=[...DATA.detail_rows],sk=null,sd=1,cp=1,pt='source';

const keyMismatchDetails={{}};
DATA.detail_rows.forEach(r=>{{
  if((r.issue_type==='VALUE_MISMATCH'||r.issue_type==='NULL_MISMATCH')&&r.source_column){{
    if(!keyMismatchDetails[r.composite_key])keyMismatchDetails[r.composite_key]=[];
    const already=keyMismatchDetails[r.composite_key].some(x=>x.src_col===r.source_column);
    if(!already)keyMismatchDetails[r.composite_key].push({{src_col:r.source_column,dst_col:r.destination_column||r.source_column,src_val:r.source_value_raw,dst_val:r.dest_value_raw}});
  }}
}});

document.addEventListener('mousemove',e=>{{
  const _g=document.getElementById('gtip');
  if(_g&&_g.style.display!=='none'){{_g.style.left=Math.min(e.clientX+14,window.innerWidth-290)+'px';_g.style.top=(e.clientY+14)+'px';}}
}});
function showTip(html){{const _g=document.getElementById('gtip');if(_g){{_g.innerHTML=html;_g.style.display='block';}}}}
function hideTip(){{const _g=document.getElementById('gtip');if(_g)_g.style.display='none';}}

function keyCompHtml(r){{
  const parts=r.key_parts_comparison||[];
  if(!parts.length)return esc(r.dest_value_raw||'');
  return parts.map((p,i)=>{{
    const sep=i>0?'<span style="color:#7a8eaf;margin:0 2px">|</span>':'';
    if(p.matched)return sep+`<span style="color:#10b981;background:rgba(16,185,129,.15);border-radius:3px;padding:1px 6px;font-family:monospace;font-size:.7rem">${{esc(p.dst_val)}}<sup style="font-size:.55rem;font-weight:700">M</sup></span>`;
    return sep+`<span style="color:#ef4444;background:rgba(239,68,68,.15);border-radius:3px;padding:1px 6px;font-family:monospace;font-size:.7rem">${{esc(p.src_val)}}&#8596;${{esc(p.dst_val)}}<sup style="font-size:.55rem;font-weight:700">U</sup></span>`;
  }}).join('');
}}

function rowIconHtml(r){{
  let cls,sym,tip;
  if(r.issue_type==='KEY_MISSING_IN_DEST'){{cls='ico-info';sym='ℹ';tip='Present in <b>source</b> — not found in destination';}}
  else if(r.issue_type==='KEY_MISSING_IN_SOURCE'){{cls='ico-info';sym='ℹ';tip='Present in <b>destination</b> — not found in source';}}
  else if(r.issue_type==='VALUE_MISMATCH'||r.issue_type==='NULL_MISMATCH'){{
    cls='ico-err';sym='✕';
    const details=keyMismatchDetails[r.composite_key]||[{{src_col:r.source_column,dst_col:r.destination_column,src_val:r.source_value_raw,dst_val:r.dest_value_raw}}];
    const lines=details.map(d=>`&bull; <b>${{esc(d.src_col)}}</b> &#8594; <b>${{esc(d.dst_col)}}</b>: source <b>${{esc(String(d.src_val))}}</b> | dest <b>${{esc(String(d.dst_val))}}</b>`).join('<br>');
    tip=`Row matched but ${{details.length}} column${{details.length>1?'s':''}} differ:<br>${{lines}}`;
  }}
  else if(r.issue_type==='MIGRATION_KEY_MISMATCH'){{
    cls='ico-warn';sym='⚡';
    const parts=(r.key_parts_comparison||[]);
    const unmatched=parts.filter(p=>!p.matched).map(p=>`<b>${{esc(p.src_col)}}</b>: src <b>${{esc(p.src_val)}}</b> | dst <b>${{esc(p.dst_val)}}</b>`).join('<br>');
    const matched=parts.filter(p=>p.matched).map(p=>esc(p.src_col)).join(', ');
    tip=`Partial key match.<br>`+(matched?`Matched: <b>${{matched}}</b><br>`:'')+(unmatched?`Mismatched:<br>${{unmatched}}`:'');
  }}
  else if(r.issue_type==='DUPLICATE_KEY_SOURCE'){{cls='ico-warn';sym='⚠';tip='Duplicate composite key in <b>source</b>';}}
  else if(r.issue_type==='DUPLICATE_KEY_DEST'){{cls='ico-warn';sym='⚠';tip='Duplicate composite key in <b>destination</b>';}}
  else if(r.issue_type==='TRANSFORMATION_ERROR'){{cls='ico-warn';sym='⚠';tip='Error applying transformation rule to this value';}}
  else if(r.issue_type==='VALUE_MAPPING_MISSING'){{cls='ico-warn';sym='⚠';tip='Source value has no mapping entry in value_mapping.csv';}}
  else if(r.issue_type==='COLUMN_MAPPING_MISSING'){{cls='ico-warn';sym='⚠';tip='Source column not found in data — check column_mapping.csv';}}
  else{{cls='ico-warn';sym='⚠';tip=esc(r.issue_type);}}
  return `<span class="row-icon ${{cls}}" data-tip="${{tip.replace(/"/g,'&quot;')}}" onmouseenter="showTip(this.dataset.tip)" onmouseleave="hideTip()">${{sym}}</span>`;
}}

function rowAnalysis(r){{
  if(r.issue_type==='KEY_MISSING_IN_DEST')return 'Present in source — not found in destination';
  if(r.issue_type==='KEY_MISSING_IN_SOURCE')return 'Present in destination — not found in source';
  if(r.issue_type==='VALUE_MISMATCH'||r.issue_type==='NULL_MISMATCH'){{
    const d=keyMismatchDetails[r.composite_key]||[];
    if(d.length)return d.map(x=>x.src_col+': "'+x.src_val+'" → "'+x.dst_val+'"').join(' | ');
    return r.source_column+': "'+r.source_value_raw+'" → "'+r.dest_value_raw+'"';
  }}
  if(r.issue_type==='MIGRATION_KEY_MISMATCH'){{const bad=(r.key_parts_comparison||[]).filter(p=>!p.matched);return 'Key mismatch — '+bad.map(p=>p.src_col+': "'+p.src_val+'" ≠ "'+p.dst_val+'"').join(', ');}}
  if(r.issue_type==='DUPLICATE_KEY_SOURCE')return 'Duplicate composite key in source';
  if(r.issue_type==='DUPLICATE_KEY_DEST')return 'Duplicate composite key in destination';
  if(r.issue_type==='TRANSFORMATION_ERROR')return 'Transformation rule failed';
  if(r.issue_type==='VALUE_MAPPING_MISSING')return 'Value not in value_mapping.csv';
  if(r.issue_type==='COLUMN_MAPPING_MISSING')return 'Source column not found in data';
  return r.issue_type;
}}

document.addEventListener('DOMContentLoaded',()=>{{
  document.getElementById('rts').textContent='Run: '+(DATA.summary.run_timestamp||'');
  const _s=DATA.summary;
  const _tmbSrc=document.getElementById('tmb-src');
  const _tmbDst=document.getElementById('tmb-dst');
  const _tmbMode=document.getElementById('tmb-mode');
  if(_tmbSrc)_tmbSrc.textContent='['+(_s.source_row_count||0)+']';
  if(_tmbDst)_tmbDst.textContent='['+(_s.destination_row_count||0)+']';
  if(_tmbMode)_tmbMode.textContent=(_s.match_mode||'full').toUpperCase();
  buildKPI();buildRunInfo();buildSampleTable();buildCharts();buildFilters();renderIssues();renderProf('source');renderMaps();
}});

function showTab(name,el){{
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
}}

function animateVal(el,target){{
  const isNum=!isNaN(parseFloat(String(target)));
  if(!isNum){{el.textContent=target;return;}}
  const end=parseFloat(String(target).replace('%',''));
  const isPct=String(target).includes('%');
  const dur=600,steps=30,inc=end/steps;
  let cur=0,t=0;
  const iv=setInterval(()=>{{t++;cur=Math.min(cur+inc,end);el.textContent=(isPct?cur.toFixed(1)+'%':Math.round(cur).toLocaleString());if(t>=steps)clearInterval(iv);}},dur/steps);
}}

function buildKPI(){{
  const s=DATA.summary,r=s.pass_rate||0;
  const rc=r>=95?'#10b981':r>=80?'#f59e0b':'#ef4444';
  const kpis=[
    {{l:'Pass Rate',v:r+'%',s:'matched / source rows',c:rc,tip:'Percentage of source records that matched a destination record and passed all column comparisons.'}},
    {{l:'Source Rows',v:s.source_row_count,s:'total records',tip:'Total records loaded from source CSV(s).'}},
    {{l:'Matched Keys',v:s.matched_key_count,s:'of '+s.source_key_count,tip:'Source records that found an exact composite key match in destination.'}},
    {{l:'Total Issues',v:s.total_issues,s:'all types',c:s.total_issues?'#f59e0b':'#10b981',tip:'Sum of all detected issues across every issue type.'}},
    {{l:'Key Issues',v:s.key_issues,s:'HIGH severity',c:s.key_issues?'#ef4444':'#10b981',tip:'Critical composite key problems. Fix before investigating value issues.'}},
    {{l:'Value Mismatches',v:s.value_mismatch_count,s:'column level',tip:'Matched records where a column value differs between source and destination.'}},
    {{l:'Missing in Dest',v:s.unmatched_source_keys,s:'src keys',tip:'Source composite keys not found in destination.'}},
    {{l:'Extra in Dest',v:s.extra_dest_keys,s:'dst keys',tip:'Destination composite keys with no matching source record.'}},
  ];
  document.getElementById('kpi-row').innerHTML=kpis.map((k,i)=>
    `<div class="kpi">
       <div class="tip-icon">?</div>
       <div class="tooltip">${{k.tip}}</div>
       <div class="lbl">${{k.l}}</div>
       <div class="val" style="color:${{k.c||'#e2e8f5'}}" id="kv-${{i}}">${{k.v}}</div>
       <div class="sub">${{k.s}}</div>
     </div>`
  ).join('');
  kpis.forEach((k,i)=>{{
    const el=document.getElementById('kv-'+i);
    if(el)animateVal(el,k.v);
  }});
}}

function buildRunInfo(){{
  const s=DATA.summary;
  const fmtFiles=(files,folder)=>{{const fl=files&&files.length?files:[folder||'—'];return fl.join(', ');}};
  const rows=[
    ['Source Folder',s.source_file||'—'],['Source File(s)',fmtFiles(s.source_files,s.source_file)],
    ['Destination Folder',s.dest_file||'—'],['Destination File(s)',fmtFiles(s.dest_files,s.dest_file)],
    ['Source Rows',s.source_row_count],['Destination Rows',s.destination_row_count],
    ['Composite Key (Src)',(s.composite_key_columns_source||[]).join(' + ')],
    ['Composite Key (Dst)',(s.composite_key_columns_dest||[]).join(' + ')],
    ['Numeric Tolerance',s.tolerance!=null?'±'+s.tolerance:'—'],
    ['Match Mode',(s.match_mode||'full').toUpperCase()],
    ['Run Timestamp',s.run_timestamp||'—'],
  ];
  document.getElementById('run-info').innerHTML=rows.map(([k,v])=>
    `<div class="ri-row"><span class="ri-k">${{esc(k)}}</span><span class="ri-v">${{esc(String(v))}}</span></div>`
  ).join('');
}}

let sp_data=[],sp_filtered=[],sp_cols=[],sp_page=1,sp_sk=null,sp_sd=1,sp_rpp=25,sp_mode='src';
function buildSampleTable(){{switchSP('src');}}
function switchSP(mode){{
  sp_mode=mode;
  const isSrc=mode==='src';
  document.getElementById('sp-btn-src').classList.toggle('active',isSrc);
  document.getElementById('sp-btn-dst').classList.toggle('active',!isSrc);
  const ds=isSrc?SRC_DATA:DST_DATA;
  sp_cols=ds.cols||[];sp_data=ds.rows||[];sp_sk=null;sp_sd=1;sp_page=1;
  if(!sp_cols.length||!sp_data.length){{document.getElementById('sp-thead').innerHTML='';document.getElementById('sp-tbody').innerHTML=`<tr><td style="color:#7a8eaf">No ${{isSrc?'source':'destination'}} data</td></tr>`;document.getElementById('sp-cnt').textContent='';return;}}
  buildSPRpp();applySPFilter();
}}
function buildSPRpp(){{
  const n=sp_data.length,opts=[10,25,50];
  if(n>50)opts.push(100);if(n>100)opts.push(250);if(n>250)opts.push(500);if(n>500)opts.push(1000);opts.push(0);
  const sel=document.getElementById('sp-rpp');
  sel.innerHTML=opts.map(v=>`<option value="${{v}}">${{v===0?'All':v}}</option>`).join('');
  sel.value=opts.includes(25)?25:opts[0];sp_rpp=parseInt(sel.value)||0;
}}
function spRppChange(){{sp_rpp=parseInt(document.getElementById('sp-rpp').value)||0;sp_page=1;renderSP();}}
function applySPFilter(){{sp_filtered=sp_data.slice();if(sp_sk){{sp_filtered.sort((a,b)=>String(a[sp_sk]??'').localeCompare(String(b[sp_sk]??''),undefined,{{numeric:true}})*sp_sd);}}sp_page=1;renderSP();}}
function sortSP(k){{if(sp_sk===k)sp_sd*=-1;else{{sp_sk=k;sp_sd=1;}}applySPFilter();}}
function renderSP(){{
  const tot=sp_filtered.length,rpp=sp_rpp===0?tot:sp_rpp,tp=Math.max(1,Math.ceil(tot/(rpp||1)));
  sp_page=Math.min(sp_page,tp);
  const s=(sp_page-1)*(rpp||tot),page=sp_filtered.slice(s,s+(rpp||tot));
  document.getElementById('sp-cnt').textContent=`${{s+1}}-${{Math.min(s+(rpp||tot),tot)}} of ${{tot}} rows`;
  document.getElementById('sp-pinfo').textContent=`${{sp_page}} / ${{tp}}`;
  document.getElementById('sp-pbtn').disabled=sp_page<=1;document.getElementById('sp-nbtn').disabled=sp_page>=tp;
  document.getElementById('sp-thead').innerHTML='<tr>'+sp_cols.map(c=>{{const arrow=sp_sk===c?(sp_sd===1?' ▲':' ▼'):'';return `<th onclick="sortSP(${{JSON.stringify(c)}})" style="font-size:.69rem">${{esc(c)}}${{arrow}}</th>`;}}).join('')+'</tr>';
  document.getElementById('sp-tbody').innerHTML=page.map(r=>'<tr>'+sp_cols.map(c=>`<td style="font-size:.72rem;white-space:nowrap">${{esc(String(r[c]??''))}}</td>`).join('')+'</tr>').join('');
}}
function pgSP(d){{sp_page+=d;renderSP();}}

function buildCharts(){{
  const s=DATA.summary;
  const tc={{KEY_MISSING_IN_DEST:'#ef4444',KEY_MISSING_IN_SOURCE:'#f97316',DUPLICATE_KEY_SOURCE:'#dc2626',MIGRATION_KEY_MISMATCH:'#8b5cf6',DUPLICATE_KEY_DEST:'#c2410c',VALUE_MISMATCH:'#6366f1',NULL_MISMATCH:'#a855f7',TRANSFORMATION_ERROR:'#f59e0b',VALUE_MAPPING_MISSING:'#ec4899',COLUMN_MAPPING_MISSING:'#06b6d4'}};
  const bt=s.mismatch_count_by_type||{{}};const mv=Math.max(1,...Object.values(bt));
  document.getElementById('by-type').innerHTML=Object.entries(bt).length?
    Object.entries(bt).map(([t,c])=>`<div class="bar-item"><div class="bar-lbl"><span>${{esc(t)}}</span><span class="bar-count">${{c}}</span></div><div class="bar-track"><div class="bar-fill" style="width:${{c/mv*100}}%;background:${{tc[t]||'#6366f1'}}"></div></div></div>`).join('')
    :'<p style="color:#6b7e9e;font-size:.8rem">No issues</p>';
  const bc=s.mismatch_count_by_column||{{}};const mv2=Math.max(1,...Object.values(bc));
  document.getElementById('by-col').innerHTML=Object.entries(bc).length?
    Object.entries(bc).sort(([,a],[,b])=>b-a).map(([c,n])=>`<div class="bar-item"><div class="bar-lbl"><span>${{esc(c)}}</span><span class="bar-count">${{n}}</span></div><div class="bar-track"><div class="bar-fill" style="width:${{n/mv2*100}}%;background:linear-gradient(90deg,#6366f1,#8b5cf6)"></div></div></div>`).join('')
    :'<p style="color:#6b7e9e;font-size:.8rem">No value mismatches</p>';
}}

function buildFilters(){{
  const types=[...new Set(DATA.detail_rows.map(r=>r.issue_type))].sort();
  const cols=[...new Set(DATA.detail_rows.map(r=>r.source_column).filter(Boolean))].sort();
  const ts=document.getElementById('ftype');types.forEach(t=>{{const o=document.createElement('option');o.value=o.text=t;ts.appendChild(o);}});
  const cs=document.getElementById('fcol');cols.forEach(c=>{{const o=document.createElement('option');o.value=o.text=c;cs.appendChild(o);}});
}}
function applyF(){{
  const q=document.getElementById('srch').value.toLowerCase(),t=document.getElementById('ftype').value,sv=document.getElementById('fsev').value,c=document.getElementById('fcol').value;
  fi=DATA.detail_rows.filter(r=>{{
    if(t&&r.issue_type!==t)return false;if(sv&&r.severity!==sv)return false;if(c&&r.source_column!==c)return false;
    if(q){{const h=[r.composite_key,r.source_column,r.destination_column,r.source_value_raw,r.dest_value_raw,r.issue_type].join(' ').toLowerCase();if(!h.includes(q))return false;}}
    return true;
  }});
  cp=1;renderIssues();
}}
function sortBy(k){{if(sk===k)sd*=-1;else{{sk=k;sd=1;}}fi.sort((a,b)=>String(a[k]||'').localeCompare(String(b[k]||''),undefined,{{numeric:true}})*sd);cp=1;renderIssues();}}
function renderIssues(){{
  const tot=fi.length,tp=Math.max(1,Math.ceil(tot/PAGE));cp=Math.min(cp,tp);
  const s=(cp-1)*PAGE,page=fi.slice(s,s+PAGE);
  document.getElementById('icnt').textContent=`${{s+1}}-${{Math.min(s+PAGE,tot)}} of ${{tot}}`;
  document.getElementById('pinfo').textContent=`${{cp}} / ${{tp}}`;
  document.getElementById('pbtn').disabled=cp<=1;document.getElementById('nbtn').disabled=cp>=tp;
  const sc={{High:'bH',Medium:'bM',Low:'bL'}};
  document.getElementById('itbody').innerHTML=page.map(r=>{{
    const isMig=r.issue_type==='MIGRATION_KEY_MISMATCH';
    const destCell=isMig?`<td>${{keyCompHtml(r)}}</td>`:`<td style="color:#fca5a5">${{esc(r.dest_value_raw)}}</td>`;
    const srcKeyCell=isMig?`<td style="font-family:monospace;font-size:.72rem">${{esc(r.composite_key)}}<br><span style="font-size:.64rem;color:#7a8eaf">src key</span></td>`:`<td style="font-family:monospace;font-size:.72rem">${{esc(r.composite_key)}}</td>`;
    return `<tr>
    <td style="text-align:center;padding:8px 4px">${{rowIconHtml(r)}}</td>
    <td style="font-size:.73rem;color:#9aafc8;max-width:240px;word-break:break-word">${{esc(rowAnalysis(r))}}</td>
    ${{srcKeyCell}}
    <td style="color:#818cf8">${{esc(r.source_column)}}</td>
    <td style="color:#a5b4fc">${{esc(r.destination_column)}}</td>
    <td style="font-family:monospace;font-size:.69rem">${{esc(r.issue_type)}}</td>
    <td><span class="badge ${{sc[r.severity]||'bL'}}">${{r.severity}}</span></td>
    <td>${{esc(r.source_value_raw)}}</td>
    <td style="color:#6ee7b7">${{esc(r.source_value_transformed)}}</td>
    ${{destCell}}
    <td style="color:#fde68a">${{esc(r.expected_dest_value||'')}}</td>
    <td style="color:#f97316">${{r.difference!=null?Number(r.difference).toFixed(4):''}}</td>
    <td><span class="chip">${{esc(r.rule_applied||'')}}</span></td>
  </tr>`;
  }}).join('');
}}
function pg(d){{cp+=d;renderIssues();}}

function showPT(el){{
  document.querySelectorAll('.ptab').forEach(t=>t.classList.remove('active'));el.classList.add('active');pt=el.dataset.pt;
  const pg=document.getElementById('pgrid'),vc=document.getElementById('vcgrid');
  if(pt==='value_counts'){{pg.style.display='none';vc.style.display='block';renderValueCounts();}}
  else{{pg.style.display='';vc.style.display='none';renderProf(pt);}}
}}
function renderProf(tab){{
  let profs;
  if(tab==='source')profs=PROF.source?.column_profiles;
  else if(tab==='destination')profs=PROF.destination?.column_profiles;
  else if(tab==='matched_src')profs=PROF.matched?.source_column_profiles;
  else profs=PROF.matched?.dest_column_profiles;
  if(!profs?.length){{document.getElementById('pgrid').innerHTML='<p style="color:#6b7e9e;font-size:.8rem">No data</p>';return;}}
  document.getElementById('pgrid').innerHTML=profs.map(p=>{{
    const st=[['Rows',p.row_count],['Nulls',`${{p.null_count}} (${{p.null_percent}}%)`],['Distinct',`${{p.distinct_count}} (${{p.distinct_percent}}%)`]];
    if(p.min!=null)st.push(['Min',p.min]);if(p.max!=null)st.push(['Max',p.max]);if(p.mean!=null)st.push(['Mean',p.mean]);if(p.std!=null)st.push(['Std',p.std]);
    if(p.length_stats)st.push(['Len (min/max)',`${{p.length_stats.min_len}}/${{p.length_stats.max_len}}`]);
    if(p.mapping_info?.destination_column)st.push(['Maps to',p.mapping_info.destination_column]);
    const flags=[];
    if(p.has_whitespace_issues)flags.push('⚠ whitespace');if(p.has_invalid_dates)flags.push(`⚠ ${{p.has_invalid_dates}} bad dates`);if(p.has_case_inconsistency)flags.push('⚠ case mix');
    const tv=(p.top_values||[]).map(t=>`${{esc(String(t.value))}}(${{t.count}})`).join(', ');
    const rule=p.rules_info?.matching_rule?`<div class="ps"><span class="k">Rule</span><span class="v"><span class="chip">${{esc(p.rules_info.matching_rule)}}</span></span></div>`:'';
    return `<div class="pc"><div class="pn">${{esc(p.column)}}</div><span class="dt">${{p.inferred_data_type}}</span>
      ${{st.map(([k,v])=>`<div class="ps"><span class="k">${{k}}</span><span class="v">${{esc(String(v))}}</span></div>`).join('')}}
      ${{rule}}${{tv?`<div class="ps"><span class="k">Top</span><span class="v" style="font-size:.68rem">${{tv}}</span></div>`:''}}
      ${{flags.length?`<div style="margin-top:6px;font-size:.67rem;color:#f59e0b">${{flags.join(' · ')}}</div>`:''}}</div>`;
  }}).join('');
}}

let vcFilterState='all';
function toggleVC(id){{
  const body=document.getElementById('vc-body-'+id),arrow=document.getElementById('vc-arrow-'+id),hdr=document.getElementById('vc-hdr-'+id);
  const open=body.style.display!=='none';body.style.display=open?'none':'block';arrow.textContent=open?'▶':'▼';hdr.style.borderBottom=open?'none':'1px solid #182040';
}}
function _vcRowOk(tr,idx){{
  const gapOk=vcFilterState==='all'||tr.dataset.gap===vcFilterState;
  const fi=document.getElementById('vc-fi-'+idx);const q=fi?fi.value.trim().toLowerCase():'';
  return gapOk&&(!q||(tr.dataset.srcval||'').toLowerCase().includes(q));
}}
function _vcRefreshCard(card,idx){{
  let vis=0;card.querySelectorAll('tr[data-gap]').forEach(tr=>{{const show=_vcRowOk(tr,idx);tr.style.display=show?'':'none';if(show)vis++;}});
  const nm=card.querySelector('.vc-nomatch');if(nm)nm.style.display=vis===0?'':'none';
}}
function setVCFilter(type,el){{
  vcFilterState=type;
  document.querySelectorAll('.vc-fbtn').forEach(b=>b.classList.toggle('active',b.dataset.f===type));
  document.querySelectorAll('.vc-card').forEach(card=>_vcRefreshCard(card,card.dataset.vcidx));
}}
function filterVCCard(idx){{const card=document.querySelector('.vc-card[data-vcidx="'+idx+'"]');if(card)_vcRefreshCard(card,idx);}}

function renderValueCounts(){{
  const vcData=PROF.value_counts||[];const el=document.getElementById('vcgrid');
  if(!vcData.length){{el.innerHTML='<p style="color:#6b7e9e;font-size:.8rem">No data</p>';return;}}
  let cntAll=0,cntPos=0,cntNeg=0,cntZero=0;
  vcData.forEach(c=>c.rows.forEach(r=>{{cntAll++;if(r.gap>0)cntPos++;else if(r.gap<0)cntNeg++;else cntZero++;}}));
  const filterBar=`<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap">
    <span style="color:#7a8eaf;font-size:.77rem;margin-right:4px">Filter by GAP:</span>
    <button class="vc-fbtn active" data-f="all" onclick="setVCFilter('all',this)">All (${{cntAll}})</button>
    <button class="vc-fbtn" data-f="positive" onclick="setVCFilter('positive',this)"><span style="color:#ef4444">▲</span> Src &gt; Dst (${{cntPos}})</button>
    <button class="vc-fbtn" data-f="negative" onclick="setVCFilter('negative',this)"><span style="color:#38bdf8">▼</span> Dst &gt; Src (${{cntNeg}})</button>
    <button class="vc-fbtn" data-f="zero" onclick="setVCFilter('zero',this)"><span style="color:#10b981">●</span> Balanced (${{cntZero}})</button>
  </div>`;
  const cards=vcData.map((c,idx)=>{{
    const isMap=c.matching_rule&&c.matching_rule.toUpperCase().includes('VALUE_MAP');const expanded=idx===0;
    const posC=c.rows.filter(r=>r.gap>0).length,negC=c.rows.filter(r=>r.gap<0).length;
    const thead=isMap?'<th>Source Value</th><th style="color:#a5b4fc">&#8594; Dest Value</th><th style="text-align:right">In Source</th><th style="text-align:right">In Destination</th><th style="text-align:right">GAP</th>':'<th>Value</th><th style="text-align:right">In Source</th><th style="text-align:right">In Destination</th><th style="text-align:right">GAP</th>';
    const bodyRows=c.rows.map(r=>{{
      const gap=r.gap,gapType=gap>0?'positive':gap<0?'negative':'zero';
      const gapStyle=gap>0?'color:#ef4444;font-weight:700':gap<0?'color:#38bdf8;font-weight:700':'color:#10b981';
      const gapText=gap>0?'+'+gap:String(gap);const srcval=esc((r.source_value+' '+r.dest_value).trim());
      const valCells=isMap?`<td style="font-family:monospace">${{esc(r.source_value)}}</td><td style="font-family:monospace;color:#9aafc8">${{esc(r.dest_value)}}</td>`:`<td style="font-family:monospace">${{esc(r.source_value)}}</td>`;
      return `<tr data-gap="${{gapType}}" data-srcval="${{srcval}}">${{valCells}}<td style="text-align:right">${{r.source_count}}</td><td style="text-align:right">${{r.dest_count}}</td><td style="text-align:right;${{gapStyle}}">${{gapText}}</td></tr>`;
    }}).join('');
    const totalSpan=isMap?'<td colspan="2" style="font-weight:700;color:#c5d1e8;border-top:2px solid #182040">Total</td>':'<td style="font-weight:700;color:#c5d1e8;border-top:2px solid #182040">Total</td>';
    const totalRow=`<tr>${{totalSpan}}<td style="text-align:right;font-weight:700;color:#e2e8f5;border-top:2px solid #182040">${{c.total_source}}</td><td style="text-align:right;font-weight:700;color:#e2e8f5;border-top:2px solid #182040">${{c.total_matched_dest}}</td><td style="border-top:2px solid #182040"></td></tr>`;
    const badges=(posC?`<span style="background:rgba(239,68,68,.12);color:#fca5a5;border-radius:4px;padding:1px 7px;font-size:.67rem;font-weight:700">&#9650;${{posC}}</span>`:'')+(negC?`<span style="background:rgba(56,189,248,.12);color:#7dd3fc;border-radius:4px;padding:1px 7px;font-size:.67rem;font-weight:700;margin-left:4px">&#9660;${{negC}}</span>`:'');
    const cardFilter=c.rows.length>10?`<div style="margin-bottom:10px"><input id="vc-fi-${{idx}}" placeholder="Filter values…" oninput="filterVCCard(${{idx}})" style="background:#07102a;border:1px solid #182040;border-radius:7px;padding:5px 10px;color:#e2e8f5;font-size:.77rem;width:220px;outline:none"></div>`:'';
    return `<div class="card vc-card" data-vcidx="${{idx}}" style="margin-bottom:10px;padding:0;overflow:hidden">
      <div id="vc-hdr-${{idx}}" onclick="toggleVC(${{idx}})" style="display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;user-select:none;border-bottom:${{expanded?'1px solid #182040':'none'}}">
        <span id="vc-arrow-${{idx}}" style="color:#6366f1;font-size:.74rem;width:12px;flex-shrink:0">${{expanded?'▼':'▶'}}</span>
        <span style="font-weight:700;color:#c5d1e8;font-size:.88rem">${{esc(c.source_column)}}</span>
        <span style="color:#7a8eaf;font-size:.81rem">&#8594; ${{esc(c.destination_column)}}</span>
        <span class="chip" style="font-size:.69rem;margin-left:4px">${{esc(c.matching_rule)}}</span>
        ${{badges}}<span style="margin-left:auto;font-size:.71rem;color:#6b7e9e">${{c.rows.length}} value${{c.rows.length!==1?'s':''}}</span>
      </div>
      <div id="vc-body-${{idx}}" style="display:${{expanded?'block':'none'}};padding:14px 16px">
        ${{cardFilter}}
        <div class="vc-nomatch" style="display:none;color:#6b7e9e;font-size:.77rem;padding:6px 0 10px">No values match the current filter.</div>
        <div class="tbl-wrap" style="max-height:320px;border:none"><table><thead><tr>${{thead}}</tr></thead><tbody>${{bodyRows}}${{totalRow}}</tbody></table></div>
      </div>
    </div>`;
  }}).join('');
  el.innerHTML=filterBar+cards;vcFilterState='all';
}}

function renderMaps(){{
  const cm=MAPS.column_mapping||[];
  document.getElementById('cmtbody').innerHTML=cm.map(m=>`<tr>
    <td style="color:#818cf8;font-family:monospace">${{esc(m.source_column)}}</td>
    <td style="color:#a5b4fc">${{esc(m.destination_column)}}</td>
    <td><span class="chip">${{esc(m.matching_rule)}}</span></td>
    <td>${{String(m.is_key||'').toLowerCase()==='true'?'<span class="key-chip">KEY</span>':''}}</td>
  </tr>`).join('');
  const vm=MAPS.value_mapping||{{}};
  document.getElementById('vmtbody').innerHTML=Object.entries(vm).flatMap(([col,rules])=>
    rules.map((r,i)=>`<tr>
      <td style="color:#818cf8">${{i===0?esc(col):''}}</td>
      <td>${{esc(r.source_value)}}</td>
      <td style="color:#6ee7b7">${{esc(r.destination_value)}}</td>
    </tr>`)
  ).join('');
}}

function esc(s){{return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}

const SRC_DATA={src_json};
const DST_DATA={dst_json};

const srcRowStatus={{}};const dstRowStatus={{}};
(function(){{
  const MISMATCH=new Set(['VALUE_MISMATCH','NULL_MISMATCH','TRANSFORMATION_ERROR','VALUE_MAPPING_MISSING','COLUMN_MAPPING_MISSING']);
  DATA.detail_rows.forEach(r=>{{
    if(r.issue_type==='KEY_MISSING_IN_DEST'&&r.source_row_index!=null)srcRowStatus[r.source_row_index]='missing_in_dest';
    else if(r.issue_type==='KEY_MISSING_IN_SOURCE'&&r.dest_row_index!=null)dstRowStatus[r.dest_row_index]='missing_in_source';
    else if(r.issue_type==='MIGRATION_KEY_MISMATCH'){{if(r.source_row_index!=null)srcRowStatus[r.source_row_index]='migration_src';if(r.dest_row_index!=null)dstRowStatus[r.dest_row_index]='migration_dst';}}
    else if(MISMATCH.has(r.issue_type)){{if(r.source_row_index!=null)srcRowStatus[r.source_row_index]='mismatch';if(r.dest_row_index!=null)dstRowStatus[r.dest_row_index]='mismatch';}}
  }});
}})();

const _srcHidden=new Set(DATA.summary.source_hidden_row_indices||[]);
const _dstHidden=new Set(DATA.summary.dest_hidden_row_indices||[]);
let vd_inited=false,vd_which='source',vd_data=[],vd_filtered=[],vd_cols=[],vd_page=1,vd_sk=null,vd_sd=1,vd_rpp=25,vd_legend_filter=null;

function buildVDCols(which){{
  const cm=MAPS.column_mapping||[];
  const isKey=m=>String(m.is_key||'').trim().toLowerCase()==='true';
  const keyEntries=cm.filter(isKey),nonKeyEntries=cm.filter(m=>!isKey(m));
  const ordered=[...keyEntries,...nonKeyEntries];
  const rawCols=which==='source'?SRC_DATA.cols:DST_DATA.cols;
  const trimMap={{}};rawCols.forEach(c=>{{trimMap[c.trim().toLowerCase()]=c;}});
  const cols=[];
  ordered.forEach(m=>{{const raw=which==='source'?m.source_column:m.destination_column;const actual=trimMap[(raw||'').trim().toLowerCase()];if(actual&&!cols.includes(actual))cols.push(actual);}});
  rawCols.forEach(c=>{{if(!cols.includes(c))cols.push(c);}});
  return cols;
}}
function vdLegendFilter(key,el){{
  if(vd_legend_filter===key){{vd_legend_filter=null;document.querySelectorAll('.vd-lc').forEach(c=>c.classList.remove('vd-lc-active'));}}
  else{{vd_legend_filter=key;document.querySelectorAll('.vd-lc').forEach(c=>c.classList.remove('vd-lc-active'));el.classList.add('vd-lc-active');}}
  vd_page=1;applyVDFilter();
}}
function showVD(which,el){{
  vd_which=which;document.querySelectorAll('.vd-btn').forEach(b=>b.classList.remove('active'));el.classList.add('active');
  vd_legend_filter=null;document.querySelectorAll('.vd-lc').forEach(c=>c.classList.remove('vd-lc-active'));
  const hiddenSet=which==='source'?_srcHidden:_dstHidden;const allRows=which==='source'?SRC_DATA.rows:DST_DATA.rows;
  vd_data=hiddenSet.size?allRows.filter(r=>!hiddenSet.has(r._row_idx)):allRows;
  vd_cols=buildVDCols(which);vd_sk=null;vd_sd=1;vd_page=1;buildVDRpp();buildVDFilters();applyVDFilter();
  const isSrc=which==='source';
  // Button record counts
  const srcN=(SRC_DATA.rows||[]).length-_srcHidden.size;
  const dstN=(DST_DATA.rows||[]).length-_dstHidden.size;
  const sc=document.getElementById('vd-src-cnt');const dc=document.getElementById('vd-dst-cnt');
  if(sc)sc.textContent=srcN.toLocaleString();
  if(dc)dc.textContent=dstN.toLocaleString();
  // Flip green/skyblue labels based on perspective
  const lg=document.getElementById('vd-lbl-green'),sg=document.getElementById('vd-sub-green');
  const lb=document.getElementById('vd-lbl-skyblue'),sb=document.getElementById('vd-sub-skyblue');
  if(lg){{const cg=document.getElementById('vd-cnt-green');lg.childNodes[0].textContent=isSrc?'Missing in Destination\u00a0':'Missing in Source\u00a0';if(sg)sg.textContent=isSrc?'Source key not in destination':'Destination key not in source';}}
  if(lb){{lb.childNodes[0].textContent=isSrc?'Extra in Destination\u00a0':'Extra in Source\u00a0';if(sb)sb.textContent=isSrc?'Destination key not in source':'Source key not in destination';}}
  // Legend counts from mode-filtered vd_data
  const statusMap=isSrc?srcRowStatus:dstRowStatus;
  const _gSt=new Set(['missing_in_dest','migration_src']);const _bSt=new Set(['missing_in_source','migration_dst']);
  let cM=0,cG=0,cB=0,cP=0;
  vd_data.forEach(r=>{{const st=statusMap[r._row_idx];if(st==='mismatch')cM++;else if(_gSt.has(st))cG++;else if(_bSt.has(st))cB++;else cP++;}});
  const _sb=(id,n)=>{{const e=document.getElementById(id);if(e)e.textContent=n?n.toLocaleString():'0';}};
  _sb('vd-cnt-mismatch',cM);_sb('vd-cnt-green',cG);_sb('vd-cnt-skyblue',cB);_sb('vd-cnt-pass',cP);
}}
function buildVDRpp(){{
  const n=vd_data.length,opts=[10,25,50];
  if(n>50)opts.push(100);if(n>100)opts.push(250);if(n>250)opts.push(500);if(n>500)opts.push(1000);opts.push(0);
  const sel=document.getElementById('vd-rpp');
  sel.innerHTML=opts.map(v=>`<option value="${{v}}">${{v===0?'All':v}}</option>`).join('');
  sel.value=opts.includes(25)?25:opts[0];vd_rpp=parseInt(sel.value)||0;
}}
function vdRppChange(){{vd_rpp=parseInt(document.getElementById('vd-rpp').value)||0;vd_page=1;renderVD();}}
function buildVDFilters(){{
  document.getElementById('vd-filters').innerHTML=vd_cols.map(c=>`<input class="vd-fi" placeholder="Filter ${{esc(c)}}…" data-col="${{esc(c)}}" oninput="applyVDFilter()">`).join('');
}}
function applyVDFilter(){{
  const filters={{}};document.querySelectorAll('#vd-filters input').forEach(inp=>{{if(inp.value)filters[inp.dataset.col]=inp.value.toLowerCase();}});
  vd_filtered=vd_data.filter(row=>{{for(const[col,q]of Object.entries(filters)){{if(!String(row[col]??'').toLowerCase().includes(q))return false;}}return true;}});
  if(vd_legend_filter){{
    const statusMap=vd_which==='source'?srcRowStatus:dstRowStatus;
    const _greenSt=new Set(['missing_in_dest','migration_src']);const _blueSt=new Set(['missing_in_source','migration_dst']);
    vd_filtered=vd_filtered.filter(row=>{{
      const st=statusMap[row._row_idx];
      if(vd_legend_filter==='mismatch')return st==='mismatch';
      if(vd_legend_filter==='green')return _greenSt.has(st);
      if(vd_legend_filter==='skyblue')return _blueSt.has(st);
      if(vd_legend_filter==='pass')return !st;
      return true;
    }});
  }}
  if(vd_sk){{vd_filtered.sort((a,b)=>String(a[vd_sk]??'').localeCompare(String(b[vd_sk]??''),undefined,{{numeric:true}})*vd_sd);}}
  vd_page=1;renderVD();
}}
function sortVD(k){{if(vd_sk===k)vd_sd*=-1;else{{vd_sk=k;vd_sd=1;}}applyVDFilter();}}
function renderVD(){{
  const tot=vd_filtered.length,rpp=vd_rpp===0?tot:vd_rpp,tp=Math.max(1,Math.ceil(tot/(rpp||1)));
  vd_page=Math.min(vd_page,tp);const s=(vd_page-1)*(rpp||tot),page=vd_filtered.slice(s,s+(rpp||tot));
  document.getElementById('vd-cnt').textContent=`${{s+1}}-${{Math.min(s+(rpp||tot),tot)}} of ${{tot}} rows`;
  document.getElementById('vd-pinfo').textContent=`${{vd_page}} / ${{tp}}`;
  document.getElementById('vd-pbtn').disabled=vd_page<=1;document.getElementById('vd-nbtn').disabled=vd_page>=tp;
  document.getElementById('vd-thead').innerHTML='<tr>'+vd_cols.map(c=>{{const arrow=vd_sk===c?(vd_sd===1?' ▲':' ▼'):'';return `<th onclick="sortVD(${{JSON.stringify(c)}})">${{esc(c)}}${{arrow}}</th>`;}}).join('')+'</tr>';
  const statusMap=vd_which==='source'?srcRowStatus:dstRowStatus;
  const rowBg={{mismatch:'background:rgba(239,68,68,0.15);',missing_in_dest:'background:rgba(16,185,129,0.13);',missing_in_source:'background:rgba(56,189,248,0.13);',migration_src:'background:rgba(16,185,129,0.13);',migration_dst:'background:rgba(56,189,248,0.13);'}};
  document.getElementById('vd-tbody').innerHTML=page.map(r=>{{
    const st=statusMap[r._row_idx],bg=rowBg[st]||'';
    return '<tr style="'+bg+'">'+vd_cols.map(c=>`<td>${{esc(String(r[c]??''))}}</td>`).join('')+'</tr>';
  }}).join('');
}}
function pgVD(d){{vd_page+=d;renderVD();}}
</script>
<div id="gtip" style="position:fixed;display:none;background:#07102a;border:1px solid #243360;color:#c5d1e8;font-size:.72rem;padding:10px 14px;border-radius:10px;max-width:270px;line-height:1.6;z-index:9999;pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.7)"></div>
</body>
</html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[INFO] Written: {out_path}')


# ---------------------------------------------------------------------------
# Row match summary writer
# ---------------------------------------------------------------------------

def _write_row_summary(
    path: str,
    src_rows: List[Dict],
    dst_rows: List[Dict],
    col_mapping: List[Dict],
    src_key_cols: List[str],
    src_idx_to_key: Dict[int, str],
    src_to_dst_idx: Dict[int, int],
    detail_rows: List[Dict],
):
    """Write row_match_summary.csv — one row per source record with full column comparison."""

    # Per-source-row issue lookup
    row_issues: Dict[int, List[Dict]] = defaultdict(list)
    for r in detail_rows:
        if r.get('source_row_index') is not None:
            row_issues[r['source_row_index']].append(r)

    # Non-key mapped columns (in mapping file order)
    src_key_lower = {c.lower() for c in src_key_cols}
    non_key_maps = [m for m in col_mapping
                    if m['source_column'].strip().lower() not in src_key_lower]

    # Build fieldnames
    fieldnames = ['composite_key', 'overall_result', 'issues_count']
    for m in non_key_maps:
        sc = m['source_column'].strip()
        dc = m['destination_column'].strip()
        fieldnames += [f'{sc}_source', f'{dc}_destination', f'{sc}_match']

    VALUE_ISSUE_TYPES = {
        'VALUE_MISMATCH', 'NULL_MISMATCH', 'TRANSFORMATION_ERROR',
        'VALUE_MAPPING_MISSING', 'COLUMN_MAPPING_MISSING',
    }

    rows_out = []
    for i, src_row in enumerate(src_rows):
        key     = src_idx_to_key.get(i, '')
        issues  = row_issues.get(i, [])
        itypes  = {r['issue_type'] for r in issues}

        if 'DUPLICATE_KEY_SOURCE' in itypes:
            overall = 'DUPLICATE_KEY_SOURCE'
        elif 'KEY_MISSING_IN_DEST' in itypes:
            overall = 'MISSING_IN_DEST'
        elif itypes & VALUE_ISSUE_TYPES:
            overall = 'FAIL'
        else:
            overall = 'PASS'

        # column → issue_type for value-level problems
        col_issue = {
            r['source_column']: r['issue_type']
            for r in issues if r['issue_type'] in VALUE_ISSUE_TYPES
        }

        dst_idx = src_to_dst_idx.get(i)
        dst_row = dst_rows[dst_idx] if dst_idx is not None else {}

        row_out: Dict[str, str] = {
            'composite_key':  key,
            'overall_result': overall,
            'issues_count':   str(len(col_issue)),
        }

        for m in non_key_maps:
            sc = m['source_column'].strip()
            dc = m['destination_column'].strip()
            src_val = _ci_get(src_row, sc)
            dst_val = _ci_get(dst_row, dc) if dst_row else 'N/A'

            if not dst_row:
                match = 'N/A'
            elif sc in col_issue:
                match = col_issue[sc]          # e.g. VALUE_MISMATCH
            else:
                match = 'PASS'

            row_out[f'{sc}_source']      = src_val
            row_out[f'{dc}_destination'] = dst_val
            row_out[f'{sc}_match']       = match

        rows_out.append(row_out)

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows_out)
    print(f'[INFO] Written: {path}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generalized CSV Data Validation Engine',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
COLUMN MAPPING FILE  (column_mapping.csv)
  Required columns : source_column, destination_column, matching_rule
  Optional column  : is_key  (true/false — marks composite key columns)

  Supported matching_rule values:
    DIRECT                          trim + case-insensitive string compare
    TRIM                            strip leading/trailing whitespace only
    UPPERCASE                       convert source to uppercase before compare
    LOWERCASE                       convert source to lowercase before compare
    NUMERIC                         float compare within --tolerance
    DECIMAL                         exact match ignoring trailing zeros  e.g. 1200.50 == 1200.5
    VALUE_MAP                       resolve via value_mapping.csv
    STRIP_PREFIX:<prefix>           strip leading prefix  e.g. STRIP_PREFIX:E
    ZEROPAD:<length>                left-pad with zeros to N digits  e.g. ZEROPAD:12  (1001 → 000000001001)
    DATE_FORMAT:<src>-><dst>        convert date format  e.g. DATE_FORMAT:YYYY-MM-DD->DD-MMM-YYYY
    Rules can be chained with |     e.g. STRIP_PREFIX:CUST|ZEROPAD:12  or  TRIM|UPPERCASE

  Supported date format tokens:
    YYYY-MM-DD  DD-MMM-YYYY  MM/DD/YYYY  DD/MM/YYYY  YYYY/MM/DD  YYYYMMDD  DD-MM-YYYY  MM-DD-YYYY

VALUE MAPPING FILE  (value_mapping.csv)
  Required columns : column_name, source_value, destination_value
  Optional column  : rule_description

OUTPUTS (written to --outdir)
  datapulse_results.json    detail rows + summary
  datapulse_results.csv     flat CSV of detail rows
  profiling.json            column-level profiling for all subsets
  mappings.json             normalized column and value mapping rules
  dashboard.html            self-contained interactive HTML dashboard
"""
    )
    parser.add_argument('--source',     default=SOURCE_FOLDER, help='Folder containing source CSV file(s) — all .csv files are merged')
    parser.add_argument('--dest',       default=DEST_FOLDER,   help='Folder containing destination CSV file(s) — all .csv files are merged')
    parser.add_argument('--colmap',     default=COLMAP_PATH,   help='Path to column_mapping.csv')
    parser.add_argument('--valmap',     default=VALMAP_PATH,   help='Path to value_mapping.csv')
    parser.add_argument('--outdir',     default=OUTPUT_DIR,    help='Output directory (default: output)')
    parser.add_argument('--format',     default=OUTPUT_FORMAT, choices=['json', 'csv', 'both'],
                        help='Output format (default: both)')
    parser.add_argument('--tolerance',  type=float, default=TOLERANCE,
                        help='Numeric comparison tolerance (default: 0.01)')
    parser.add_argument('--key-cols',   nargs='+', metavar='COL', default=KEY_COLS,
                        help='Override composite key source columns (space-separated). '
                             'If omitted, uses is_key=true rows in column_mapping.csv.')
    parser.add_argument('--source-sep', default=SOURCE_SEP,
                        help='Field separator for source CSV files (default: ,)')
    parser.add_argument('--dest-sep',   default=DEST_SEP,
                        help='Field separator for destination CSV files (default: ,)')
    parser.add_argument('--colmap-sep', default=COLMAP_SEP,
                        help='Field separator for column_mapping.csv (default: ,)')
    parser.add_argument('--valmap-sep', default=VALMAP_SEP,
                        help='Field separator for value_mapping.csv (default: ,)')
    parser.add_argument('mode', nargs='?', default=MATCH_MODE,
                        help='Comparison scope (case-insensitive): '
                             'full=all records; '
                             'leftout=matched records only (ignore source-only and dest-only); '
                             'rightout=all dest + matching source (ignore source-only); '
                             'union=all source + matching dest (ignore dest-only). '
                             'If omitted, uses MATCH_MODE from config block (currently: %(default)s).')
    args = parser.parse_args()

    try:
        validate(
            source_folder=args.source,
            dest_folder=args.dest,
            colmap_path=args.colmap,
            valmap_path=args.valmap,
            outdir=args.outdir,
            tolerance=args.tolerance,
            key_cols_override=args.key_cols,
            output_format=args.format,
            match_mode=args.mode,
            source_sep=args.source_sep,
            dest_sep=args.dest_sep,
            colmap_sep=args.colmap_sep,
            valmap_sep=args.valmap_sep,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
