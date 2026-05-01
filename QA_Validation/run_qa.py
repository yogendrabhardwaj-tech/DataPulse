#!/usr/bin/env python3
"""
DataPulse QA Validation Suite
================================
Validates every output file produced by check_datapulse.py for structural
integrity, mathematical consistency, and cross-file agreement.

Run AFTER check_datapulse.py:
    python QA_Validation/run_qa.py
    python QA_Validation/run_qa.py --outdir my_output --report my_report.html

Opens QA_Testing_Result.html in QA_Validation/ folder when done.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# Path to check_datapulse.py output folder (relative to this script's location)
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), '..', 'output')
QA_REPORT   = os.path.join(os.path.dirname(__file__), 'QA_Testing_Result.html')

# Expected output files from check_datapulse.py
EXPECTED_FILES = [
    'datapulse_results.json',
    'datapulse_results.csv',
    'row_match_summary.csv',
    'profiling.json',
    'mappings.json',
    'dashboard.html',
]

VALID_ISSUE_TYPES = {
    'KEY_MISSING_IN_DEST', 'KEY_MISSING_IN_SOURCE', 'MIGRATION_KEY_MISMATCH',
    'DUPLICATE_KEY_SOURCE', 'DUPLICATE_KEY_DEST', 'VALUE_MISMATCH',
    'NULL_MISMATCH', 'TRANSFORMATION_ERROR', 'VALUE_MAPPING_MISSING',
    'COLUMN_MAPPING_MISSING',
}
VALID_SEVERITIES    = {'High', 'Medium', 'Low'}
VALID_MATCH_MODES   = {'full', 'leftout', 'rightout', 'union'}
VALID_OVERALL_RESULTS = {
    'PASS', 'FAIL', 'MISSING_IN_DEST', 'DUPLICATE_KEY_SOURCE', 'DUPLICATE_KEY_DEST',
}

# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

class TR:
    """Single test result."""
    __slots__ = ('category', 'name', 'status', 'details', 'expected', 'actual')

    def __init__(self, category: str, name: str, status: str,
                 details: str = '', expected: str = '', actual: str = ''):
        self.category = category
        self.name     = name
        self.status   = status       # PASS | FAIL | WARN | SKIP
        self.details  = details
        self.expected = expected
        self.actual   = actual


_results: List[TR] = []


def chk(category: str, name: str, ok: bool,
        details: str = '', expected: str = '', actual: str = '',
        warn: bool = False) -> bool:
    status = 'PASS' if ok else ('WARN' if warn else 'FAIL')
    _results.append(TR(category, name, status, details, expected, actual))
    icon = {'PASS': 'PASS', 'FAIL': 'FAIL', 'WARN': 'WARN'}[status]
    print(f'  [{icon}] {name}' + (f' — {details}' if not ok and details else ''))
    return ok


def skip(category: str, name: str, reason: str = '') -> None:
    _results.append(TR(category, name, 'SKIP', reason))
    print(f'  [SKIP] {name}' + (f' — {reason}' if reason else ''))


def section(title: str) -> None:
    print(f'\n{"="*60}')
    print(f'  {title}')
    print(f'{"="*60}')


# ---------------------------------------------------------------------------
# Helper: safe load
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Optional[Dict]:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _read_csv(path: str) -> Optional[List[Dict]]:
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))
    except Exception:
        return None


# ===========================================================================
# TEST CATEGORIES
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Output Files
# ---------------------------------------------------------------------------

def test_output_files(outdir: str) -> None:
    section('1. Output Files — all expected files must exist')
    for fname in EXPECTED_FILES:
        fpath = os.path.join(outdir, fname)
        exists = os.path.isfile(fpath)
        size   = os.path.getsize(fpath) if exists else 0
        chk('Output Files', f'{fname} exists',
            exists,
            expected='File exists on disk',
            actual='File missing' if not exists else f'Exists ({size:,} bytes)')


# ---------------------------------------------------------------------------
# 2. JSON Parsing
# ---------------------------------------------------------------------------

def test_json_parsing(outdir: str) -> None:
    section('2. JSON Parsing — all JSON files must be valid')
    for fname in ('datapulse_results.json', 'profiling.json', 'mappings.json'):
        fpath = os.path.join(outdir, fname)
        if not os.path.isfile(fpath):
            skip('JSON Parsing', f'{fname} is valid JSON', 'File missing — skipped')
            continue
        data = _read_json(fpath)
        chk('JSON Parsing', f'{fname} is valid JSON',
            data is not None,
            details='JSON parse error' if data is None else '',
            expected='Parses without error',
            actual='Invalid JSON' if data is None else 'Valid JSON')


# ---------------------------------------------------------------------------
# 3. Summary Structure
# ---------------------------------------------------------------------------

def test_summary_structure(summary: Dict) -> None:
    section('3. Summary Structure — required keys and value types')
    cat = 'Summary Structure'

    required_keys = [
        'source_row_count', 'destination_row_count', 'source_key_count',
        'matched_key_count', 'unmatched_source_keys', 'extra_dest_keys',
        'total_issues', 'key_issues', 'value_mismatch_count', 'pass_rate',
        'mismatch_count_by_type', 'mismatch_count_by_column',
        'composite_key_columns_source', 'composite_key_columns_dest',
        'run_timestamp', 'source_file', 'dest_file', 'tolerance', 'match_mode',
    ]
    missing = [k for k in required_keys if k not in summary]
    chk(cat, 'summary has all required keys',
        len(missing) == 0,
        details=f'Missing: {missing}' if missing else '',
        expected='All required keys present',
        actual=f'{len(missing)} key(s) missing: {missing}' if missing else 'All present')

    for field in ('source_row_count', 'destination_row_count', 'source_key_count',
                  'matched_key_count', 'total_issues'):
        val = summary.get(field)
        chk(cat, f'{field} is a non-negative integer',
            isinstance(val, int) and val >= 0,
            expected='int >= 0',
            actual=repr(val))

    pr = summary.get('pass_rate', -1)
    chk(cat, 'pass_rate is in range [0, 100]',
        isinstance(pr, (int, float)) and 0.0 <= pr <= 100.0,
        expected='0.0 <= pass_rate <= 100.0',
        actual=repr(pr))

    mode = summary.get('match_mode', '')
    chk(cat, f'match_mode is a valid value',
        str(mode).lower() in VALID_MATCH_MODES,
        expected=f'One of {sorted(VALID_MATCH_MODES)}',
        actual=repr(mode))

    for field in ('composite_key_columns_source', 'composite_key_columns_dest'):
        val = summary.get(field)
        chk(cat, f'{field} is a non-empty list',
            isinstance(val, list) and len(val) > 0,
            expected='Non-empty list',
            actual=repr(val))

    ts = summary.get('run_timestamp', '')
    chk(cat, 'run_timestamp is non-empty',
        isinstance(ts, str) and len(ts) > 0,
        expected='Non-empty string',
        actual=repr(ts))

    for field in ('source_files', 'dest_files'):
        val = summary.get(field)
        chk(cat, f'{field} is a list',
            isinstance(val, list),
            expected='List of file names',
            actual=repr(val),
            warn=True)


# ---------------------------------------------------------------------------
# 4. Summary Math Integrity
# ---------------------------------------------------------------------------

def test_summary_math(summary: Dict, detail_rows: List[Dict]) -> None:
    section('4. Summary Math — counts must be internally consistent')
    cat = 'Summary Math'

    src  = summary.get('source_row_count', 0)
    sk   = summary.get('source_key_count', 0)
    mk   = summary.get('matched_key_count', 0)
    umk  = summary.get('unmatched_source_keys', 0)
    ti   = summary.get('total_issues', 0)
    ki   = summary.get('key_issues', 0)
    pr   = summary.get('pass_rate', 0)
    mbt  = summary.get('mismatch_count_by_type', {})

    chk(cat, 'matched_key_count + unmatched_source_keys == source_key_count',
        mk + umk == sk,
        expected=f'{mk} + {umk} = {sk}',
        actual=f'{mk} + {umk} = {mk + umk}')

    actual_total = len(detail_rows)
    chk(cat, 'total_issues == len(detail_rows)',
        ti == actual_total,
        expected=f'total_issues = {ti}',
        actual=f'len(detail_rows) = {actual_total}')

    if src > 0:
        expected_pr = round(mk / src * 100, 2)
        chk(cat, 'pass_rate formula: round(matched / source_rows * 100, 2)',
            abs(pr - expected_pr) < 0.01,
            expected=f'{expected_pr}%',
            actual=f'{pr}%')

    by_type_sum = sum(mbt.values()) if isinstance(mbt, dict) else -1
    chk(cat, 'mismatch_count_by_type values sum == total_issues',
        by_type_sum == ti,
        expected=str(ti),
        actual=str(by_type_sum))

    actual_high = sum(1 for r in detail_rows if r.get('severity') == 'High')
    chk(cat, 'key_issues == count of High severity rows in detail_rows',
        ki == actual_high,
        expected=str(ki),
        actual=str(actual_high))

    actual_vm = sum(1 for r in detail_rows if r.get('issue_type') == 'VALUE_MISMATCH')
    stated_vm = summary.get('value_mismatch_count', 0)
    chk(cat, 'value_mismatch_count matches VALUE_MISMATCH rows in detail_rows',
        stated_vm == actual_vm,
        expected=str(stated_vm),
        actual=str(actual_vm))

    stated_mg = summary.get('migration_key_count', 0)
    actual_mg = sum(1 for r in detail_rows if r.get('issue_type') == 'MIGRATION_KEY_MISMATCH')
    chk(cat, 'migration_key_count matches MIGRATION_KEY_MISMATCH rows in detail_rows',
        stated_mg == actual_mg,
        expected=str(stated_mg),
        actual=str(actual_mg))

    # ── Unmatched source key accounting (the class of bug that caused the
    #    "Missing in Destination shows only 1 row" issue) ──────────────────
    # In full / leftout mode every unmatched source key must appear in
    # detail_rows as either KEY_MISSING_IN_DEST or MIGRATION_KEY_MISMATCH.
    # A gap here means the safety-net silently swallowed records.
    mode = str(summary.get('match_mode', '')).lower()
    if mode in ('full', 'leftout'):
        km_in_dest  = sum(1 for r in detail_rows if r.get('issue_type') == 'KEY_MISSING_IN_DEST')
        mg_count    = actual_mg
        src_dup     = sum(1 for r in detail_rows if r.get('issue_type') == 'DUPLICATE_KEY_SOURCE')
        # unmatched_source_keys counts UNIQUE unmatched keys; duplicates share one key entry
        # so we compare: KEY_MISSING_IN_DEST + MIGRATION == unmatched_source_keys
        key_accounted = km_in_dest + mg_count
        chk(cat,
            f'KEY_MISSING_IN_DEST ({km_in_dest}) + MIGRATION ({mg_count}) == unmatched_source_keys ({umk})',
            key_accounted == umk,
            details='Gap means the safety-net may have silently removed records' if key_accounted != umk else '',
            expected=str(umk),
            actual=str(key_accounted))

    # ── Source row index coverage ─────────────────────────────────────────
    # Every source row index referenced in detail_rows must be a valid index.
    # Also verify: in full/leftout mode, matched + green-status rows == source rows
    if mode in ('full', 'leftout') and src > 0:
        green_src_indices = set()
        for r in detail_rows:
            if r.get('issue_type') in ('KEY_MISSING_IN_DEST', 'MIGRATION_KEY_MISMATCH'):
                if r.get('source_row_index') is not None:
                    green_src_indices.add(r['source_row_index'])
        green_count = len(green_src_indices)
        expected_green = umk   # one source row per unmatched key (non-duplicate)
        chk(cat,
            f'Distinct green source row indices ({green_count}) == unmatched_source_keys ({umk})',
            green_count == expected_green,
            details='Mismatch means View Data green filter will show wrong row count' if green_count != expected_green else '',
            expected=str(expected_green),
            actual=str(green_count))


# ---------------------------------------------------------------------------
# 5. Detail Rows Validation
# ---------------------------------------------------------------------------

def test_detail_rows(detail_rows: List[Dict], summary: Dict) -> None:
    section('5. Detail Rows — schema, value ranges, cross-checks')
    cat = 'Detail Rows'

    src_count = summary.get('source_row_count', 0)
    dst_count = summary.get('destination_row_count', 0)

    if not detail_rows:
        chk(cat, 'detail_rows is present (may be empty)',
            True, details='No issues found — all checks skipped',
            expected='List (empty is valid)', actual='Empty list')
        return

    required_fields = {'composite_key', 'issue_type', 'severity',
                       'source_column', 'destination_column', 'timestamp'}
    bad_schema = [
        i for i, r in enumerate(detail_rows)
        if not required_fields.issubset(set(r.keys()))
    ]
    chk(cat, 'All detail rows have required fields',
        len(bad_schema) == 0,
        details=f'Rows missing fields: {bad_schema[:5]}' if bad_schema else '',
        expected='All required fields present in every row',
        actual=f'{len(bad_schema)} row(s) missing fields' if bad_schema else 'All OK')

    bad_types = [r['issue_type'] for r in detail_rows
                 if r.get('issue_type') not in VALID_ISSUE_TYPES]
    chk(cat, 'All issue_type values are from the known set',
        len(bad_types) == 0,
        details=f'Unknown types: {sorted(set(bad_types))[:5]}' if bad_types else '',
        expected=f'One of {sorted(VALID_ISSUE_TYPES)}',
        actual=f'{len(bad_types)} unknown value(s): {sorted(set(bad_types))[:5]}' if bad_types else 'All valid')

    bad_sev = [r.get('severity') for r in detail_rows
               if r.get('severity') not in VALID_SEVERITIES]
    chk(cat, 'All severity values are High / Medium / Low',
        len(bad_sev) == 0,
        expected='High | Medium | Low',
        actual=f'{len(bad_sev)} invalid value(s): {sorted(set(bad_sev))[:5]}' if bad_sev else 'All valid')

    null_key_rows = [i for i, r in enumerate(detail_rows)
                     if not r.get('composite_key')]
    chk(cat, 'No detail row has an empty composite_key',
        len(null_key_rows) == 0,
        expected='composite_key non-empty in all rows',
        actual=f'{len(null_key_rows)} row(s) with empty key' if null_key_rows else 'All OK')

    orphan_rows = [
        i for i, r in enumerate(detail_rows)
        if r.get('source_row_index') is None and r.get('dest_row_index') is None
    ]
    chk(cat, 'No row has both source_row_index and dest_row_index as None',
        len(orphan_rows) == 0,
        expected='At least one index present per row',
        actual=f'{len(orphan_rows)} orphan row(s)' if orphan_rows else 'All OK')

    src_idxs = [r['source_row_index'] for r in detail_rows
                if r.get('source_row_index') is not None]
    bad_src = [i for i in src_idxs if not (0 <= i < src_count)]
    chk(cat, 'source_row_index values are within [0, source_row_count-1]',
        len(bad_src) == 0,
        expected=f'[0, {src_count - 1}]',
        actual=f'{len(bad_src)} out-of-range value(s): {bad_src[:3]}' if bad_src else 'All in range')

    dst_idxs = [r['dest_row_index'] for r in detail_rows
                if r.get('dest_row_index') is not None]
    bad_dst = [i for i in dst_idxs if not (0 <= i < dst_count)]
    chk(cat, 'dest_row_index values are within [0, dest_row_count-1]',
        len(bad_dst) == 0,
        expected=f'[0, {dst_count - 1}]',
        actual=f'{len(bad_dst)} out-of-range value(s): {bad_dst[:3]}' if bad_dst else 'All in range')

    # mismatch_count_by_type individual key checks
    mbt = summary.get('mismatch_count_by_type', {})
    from collections import Counter
    actual_by_type = Counter(r.get('issue_type') for r in detail_rows)
    wrong_counts = {k: (v, actual_by_type.get(k, 0))
                    for k, v in mbt.items() if v != actual_by_type.get(k, 0)}
    chk(cat, 'mismatch_count_by_type entries match actual detail_rows counts',
        len(wrong_counts) == 0,
        details='; '.join(f'{k}: stated={v[0]} actual={v[1]}' for k, v in wrong_counts.items()) if wrong_counts else '',
        expected='Stated counts == actual counts',
        actual=f'{len(wrong_counts)} mismatch(es)' if wrong_counts else 'All match')


# ---------------------------------------------------------------------------
# 6. Row Match Summary CSV
# ---------------------------------------------------------------------------

def test_row_match_summary(outdir: str, summary: Dict) -> None:
    section('6. Row Match Summary CSV — structure and count integrity')
    cat = 'Row Match Summary'

    fpath = os.path.join(outdir, 'row_match_summary.csv')
    if not os.path.isfile(fpath):
        skip(cat, 'row_match_summary.csv structure', 'File missing'); return

    rows = _read_csv(fpath)
    if rows is None:
        chk(cat, 'row_match_summary.csv is valid CSV', False,
            expected='Parses without error', actual='CSV parse error'); return

    chk(cat, 'row_match_summary.csv is valid CSV',
        True, expected='Parses without error', actual='Valid CSV')

    src_count = summary.get('source_row_count', 0)
    chk(cat, f'Row count == source_row_count ({src_count:,})',
        len(rows) == src_count,
        expected=str(src_count),
        actual=str(len(rows)))

    required_cols = {'composite_key', 'overall_result', 'issues_count'}
    if rows:
        present = set(rows[0].keys())
        missing_cols = required_cols - {c.strip().lower() for c in present}
        chk(cat, 'Required columns present (composite_key, overall_result, issues_count)',
            len(missing_cols) == 0,
            expected='All required columns present',
            actual=f'Missing: {sorted(missing_cols)}' if missing_cols else 'All present')

    bad_results = [r.get('overall_result') for r in rows
                   if r.get('overall_result') not in VALID_OVERALL_RESULTS]
    chk(cat, 'overall_result values are from the valid set',
        len(bad_results) == 0,
        expected=f'One of {sorted(VALID_OVERALL_RESULTS)}',
        actual=f'{len(bad_results)} invalid value(s): {sorted(set(bad_results))[:5]}' if bad_results else 'All valid')

    bad_counts = []
    for r in rows:
        try:
            if int(r.get('issues_count', 0)) < 0:
                bad_counts.append(r.get('issues_count'))
        except (ValueError, TypeError):
            bad_counts.append(r.get('issues_count'))
    chk(cat, 'issues_count is a non-negative integer in all rows',
        len(bad_counts) == 0,
        expected='>= 0',
        actual=f'{len(bad_counts)} invalid value(s)' if bad_counts else 'All valid')

    pass_count   = sum(1 for r in rows if r.get('overall_result') == 'PASS')
    mk           = summary.get('matched_key_count', 0)
    detail_rows  = _read_json(os.path.join(outdir, 'datapulse_results.json'))
    if detail_rows:
        vm_keys = {r.get('composite_key') for r in detail_rows.get('detail_rows', [])
                   if r.get('issue_type') == 'VALUE_MISMATCH'}
        fail_in_summary = sum(1 for r in rows if r.get('overall_result') == 'FAIL')
        chk(cat, 'FAIL count in row_match_summary == distinct VALUE_MISMATCH composite keys',
            fail_in_summary == len(vm_keys),
            expected=str(len(vm_keys)),
            actual=str(fail_in_summary),
            warn=True)  # warn only — composite key collisions can cause slight drift


# ---------------------------------------------------------------------------
# 7. Profiling JSON
# ---------------------------------------------------------------------------

def test_profiling(outdir: str, summary: Dict) -> None:
    section('7. Profiling JSON — structure and data quality')
    cat = 'Profiling'

    fpath = os.path.join(outdir, 'profiling.json')
    if not os.path.isfile(fpath):
        skip(cat, 'profiling structure', 'File missing'); return

    prof = _read_json(fpath)
    if prof is None:
        chk(cat, 'profiling.json is valid JSON', False,
            expected='Parses without error', actual='JSON parse error'); return

    for key in ('source', 'destination', 'matched', 'value_counts'):
        chk(cat, f'profiling.json has "{key}" key',
            key in prof,
            expected='Key present',
            actual='Missing' if key not in prof else 'Present')

    if 'source' in prof:
        src_profiles = prof['source'].get('column_profiles', [])
        src_cols = summary.get('source_columns', [])
        chk(cat, 'Source column_profiles count matches source column count',
            len(src_profiles) == len(src_cols),
            expected=str(len(src_cols)),
            actual=str(len(src_profiles)))

        req_profile_fields = {'column', 'inferred_data_type', 'row_count',
                               'null_count', 'null_percent', 'distinct_count'}
        bad_profiles = [p.get('column', '?') for p in src_profiles
                        if not req_profile_fields.issubset(set(p.keys()))]
        chk(cat, 'All source column profiles have required fields',
            len(bad_profiles) == 0,
            expected='All required fields present',
            actual=f'Missing fields in: {bad_profiles[:5]}' if bad_profiles else 'All OK')

        bad_pct = [p.get('column') for p in src_profiles
                   if not (0.0 <= float(p.get('null_percent', 0)) <= 100.0)]
        chk(cat, 'null_percent is in [0, 100] for all source profiles',
            len(bad_pct) == 0,
            expected='0.0 – 100.0',
            actual=f'Out of range in: {bad_pct[:5]}' if bad_pct else 'All valid')

    if 'value_counts' in prof:
        vc = prof['value_counts']
        mappings = _read_json(os.path.join(outdir, 'mappings.json'))
        if mappings:
            col_count = len(mappings.get('column_mapping', []))
            chk(cat, 'value_counts entry count == column_mapping count',
                len(vc) == col_count,
                expected=str(col_count),
                actual=str(len(vc)))

        req_vc_fields = {'source_column', 'destination_column', 'matching_rule',
                         'rows', 'total_source', 'total_matched_dest'}
        bad_vc = [v.get('source_column', '?') for v in vc
                  if not req_vc_fields.issubset(set(v.keys()))]
        chk(cat, 'All value_counts entries have required fields',
            len(bad_vc) == 0,
            expected='All required fields present',
            actual=f'Missing fields in: {bad_vc[:5]}' if bad_vc else 'All OK')

        src_count = summary.get('source_row_count', 0)
        wrong_totals = [v['source_column'] for v in vc
                        if v.get('total_source', -1) != src_count]
        chk(cat, 'value_counts total_source == source_row_count for all columns',
            len(wrong_totals) == 0,
            expected=str(src_count),
            actual=f'{len(wrong_totals)} column(s) differ: {wrong_totals[:5]}' if wrong_totals else 'All match')


# ---------------------------------------------------------------------------
# 8. Mappings JSON
# ---------------------------------------------------------------------------

def test_mappings(outdir: str) -> None:
    section('8. Mappings JSON — structure and completeness')
    cat = 'Mappings'

    fpath = os.path.join(outdir, 'mappings.json')
    if not os.path.isfile(fpath):
        skip(cat, 'mappings structure', 'File missing'); return

    maps = _read_json(fpath)
    if maps is None:
        chk(cat, 'mappings.json is valid JSON', False,
            expected='Parses without error', actual='JSON parse error'); return

    for key in ('column_mapping', 'value_mapping'):
        chk(cat, f'mappings.json has "{key}" key',
            key in maps,
            expected='Key present',
            actual='Missing' if key not in maps else 'Present')

    cm = maps.get('column_mapping', [])
    chk(cat, 'column_mapping is non-empty',
        len(cm) > 0,
        expected='At least 1 entry',
        actual=f'{len(cm)} entry(ies)')

    required_cm = {'source_column', 'destination_column', 'matching_rule'}
    bad_cm = [i for i, m in enumerate(cm)
              if not required_cm.issubset(set(str(k).strip().lower() for k in m.keys()))]
    chk(cat, 'Each column_mapping entry has source_column, destination_column, matching_rule',
        len(bad_cm) == 0,
        expected='All required fields present',
        actual=f'{len(bad_cm)} entry(ies) missing fields' if bad_cm else 'All OK')

    is_key_cols = [m for m in cm
                   if str(m.get('is_key', '')).strip().lower() in ('true', 'yes', '1')]
    chk(cat, 'At least one column_mapping entry has is_key=true',
        len(is_key_cols) > 0,
        expected='>= 1 key column',
        actual=f'{len(is_key_cols)} key column(s)')

    vm = maps.get('value_mapping', {})
    chk(cat, 'value_mapping is a dict',
        isinstance(vm, dict),
        expected='dict',
        actual=type(vm).__name__)


# ---------------------------------------------------------------------------
# 9. Dashboard HTML
# ---------------------------------------------------------------------------

def test_dashboard(outdir: str) -> None:
    section('9. Dashboard HTML — validity and data injection')
    cat = 'Dashboard'

    fpath = os.path.join(outdir, 'dashboard.html')
    if not os.path.isfile(fpath):
        skip(cat, 'dashboard structure', 'File missing'); return

    size = os.path.getsize(fpath)
    chk(cat, 'dashboard.html is larger than 50 KB (not a stub)',
        size > 50_000,
        expected='> 50,000 bytes',
        actual=f'{size:,} bytes')

    try:
        with open(fpath, encoding='utf-8') as f:
            html = f.read()
    except Exception as exc:
        chk(cat, 'dashboard.html readable as UTF-8', False,
            actual=str(exc)); return

    chk(cat, 'dashboard.html contains <!DOCTYPE html>',
        '<!DOCTYPE html>' in html or '<!doctype html>' in html.lower(),
        expected='<!DOCTYPE html> present',
        actual='Present' if '<!DOCTYPE html>' in html else 'Not found')

    has_data = 'const DATA=' in html or 'var DATA=' in html
    chk(cat, 'dashboard.html contains DATA JSON injection (const DATA= or var DATA=)',
        has_data,
        expected='const DATA= or var DATA= in script',
        actual='Present' if has_data else 'Not found')

    has_src = 'SRC_DATA=' in html
    chk(cat, 'dashboard.html contains SRC_DATA injection',
        has_src,
        expected='SRC_DATA= present',
        actual='Present' if has_src else 'Not found')

    has_dst = 'DST_DATA=' in html
    chk(cat, 'dashboard.html contains DST_DATA injection',
        has_dst,
        expected='DST_DATA= present',
        actual='Present' if has_dst else 'Not found')

    # Detect "= undefined" assignment patterns (not localeCompare locale arg)
    import re as _re
    bad_undef = _re.findall(r'(?<![,(])\s*=\s*undefined\b', html)
    chk(cat, 'dashboard.html has no unintended "= undefined" assignments',
        len(bad_undef) == 0,
        expected='No "= undefined" assignments',
        actual=f'{len(bad_undef)} occurrence(s) found' if bad_undef else 'Clean',
        warn=True)


# ---------------------------------------------------------------------------
# 10. Cross-File Consistency
# ---------------------------------------------------------------------------

def test_cross_file(outdir: str, summary: Dict, detail_rows: List[Dict]) -> None:
    section('10. Cross-File Consistency — files must agree with each other')
    cat = 'Cross-File'

    # datapulse_results.csv row count == total_issues
    csv_path = os.path.join(outdir, 'datapulse_results.csv')
    if os.path.isfile(csv_path):
        csv_rows = _read_csv(csv_path)
        ti = summary.get('total_issues', 0)
        if csv_rows is not None and not (len(csv_rows) == 1 and 'no_issues' in (csv_rows[0] or {})):
            actual_csv = len(csv_rows) if csv_rows else 0
            chk(cat, 'datapulse_results.csv row count == total_issues',
                actual_csv == ti,
                expected=str(ti),
                actual=str(actual_csv))
        else:
            chk(cat, 'datapulse_results.csv row count == total_issues',
                ti == 0,
                expected='0 issues',
                actual='no_issues placeholder (valid when 0 issues)')
    else:
        skip(cat, 'datapulse_results.csv vs total_issues', 'CSV file missing')

    # row_match_summary row count == source_row_count
    rms_path = os.path.join(outdir, 'row_match_summary.csv')
    if os.path.isfile(rms_path):
        rms_rows = _read_csv(rms_path)
        src_count = summary.get('source_row_count', 0)
        if rms_rows is not None:
            chk(cat, 'row_match_summary.csv row count == source_row_count',
                len(rms_rows) == src_count,
                expected=str(src_count),
                actual=str(len(rms_rows)))
    else:
        skip(cat, 'row_match_summary.csv row count vs source_row_count', 'File missing')

    # profiling source row_count == source_row_count for mode-filtered rows
    prof_path = os.path.join(outdir, 'profiling.json')
    if os.path.isfile(prof_path):
        prof = _read_json(prof_path)
        if prof and 'source' in prof:
            src_profiles = prof['source'].get('column_profiles', [])
            if src_profiles:
                # All profiles must agree on row_count
                counts = {p.get('row_count') for p in src_profiles}
                chk(cat, 'All source column profiles share the same row_count',
                    len(counts) == 1,
                    expected='All profiles same row_count',
                    actual=f'Multiple row_counts: {counts}' if len(counts) > 1 else f'Consistent: {counts}')
    else:
        skip(cat, 'Profiling source row_count consistency', 'profiling.json missing')

    # mappings.json column_mapping count == profiling value_counts count
    maps_path = os.path.join(outdir, 'mappings.json')
    if os.path.isfile(maps_path) and os.path.isfile(prof_path):
        maps = _read_json(maps_path)
        prof = _read_json(prof_path)
        if maps and prof:
            cm_count = len(maps.get('column_mapping', []))
            vc_count = len(prof.get('value_counts', []))
            chk(cat, 'column_mapping count == value_counts entries in profiling',
                cm_count == vc_count,
                expected=str(cm_count),
                actual=str(vc_count))
    else:
        skip(cat, 'column_mapping count vs value_counts', 'Required files missing')

    # source_files listed in summary exist in the inferred folder
    src_folder = os.path.normpath(
        os.path.join(outdir, '..', summary.get('source_file', ''), '..'))
    source_files = summary.get('source_files', [])
    if source_files:
        # Try to locate the folder by finding actual file paths
        # We check relative to the output's parent
        parent = os.path.normpath(os.path.join(outdir, '..'))
        found = 0
        for sf in source_files:
            for root, dirs, files in os.walk(parent):
                if sf in files:
                    found += 1
                    break
        chk(cat, 'All source_files listed in summary are locatable on disk',
            found == len(source_files),
            expected=f'{len(source_files)} file(s) found',
            actual=f'{found}/{len(source_files)} found',
            warn=True)
    else:
        skip(cat, 'source_files locatable on disk', 'source_files not in summary')


# ===========================================================================
# MAIN RUNNER
# ===========================================================================

def run_all(outdir: str) -> List[TR]:
    print(f'\nDataPulse QA Validation Suite')
    print(f'Output folder : {os.path.abspath(outdir)}')
    print(f'Started       : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    # 1. Output files
    test_output_files(outdir)

    # 2. JSON parsing
    test_json_parsing(outdir)

    # Load shared data for remaining tests
    results_path = os.path.join(outdir, 'datapulse_results.json')
    data = _read_json(results_path)
    if data is None:
        print('\n[ABORT] datapulse_results.json missing or invalid — cannot run remaining tests.')
        for cat in ('Summary Structure', 'Summary Math', 'Detail Rows', 'Row Match Summary',
                    'Profiling', 'Mappings', 'Dashboard', 'Cross-File'):
            skip(cat, f'{cat} tests', 'datapulse_results.json unreadable — skipped')
        return _results

    summary     = data.get('summary', {})
    detail_rows = data.get('detail_rows', [])

    # 3–10. All remaining categories
    test_summary_structure(summary)
    test_summary_math(summary, detail_rows)
    test_detail_rows(detail_rows, summary)
    test_row_match_summary(outdir, summary)
    test_profiling(outdir, summary)
    test_mappings(outdir)
    test_dashboard(outdir)
    test_cross_file(outdir, summary, detail_rows)

    return _results


# ===========================================================================
# HTML REPORT GENERATOR
# ===========================================================================

def _esc(s: str) -> str:
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def generate_html(results: List[TR], outdir: str, report_path: str) -> None:
    total  = len(results)
    passed = sum(1 for r in results if r.status == 'PASS')
    failed = sum(1 for r in results if r.status == 'FAIL')
    warned = sum(1 for r in results if r.status == 'WARN')
    skipped= sum(1 for r in results if r.status == 'SKIP')
    pass_rate = round(passed / total * 100, 1) if total else 0.0
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Group by category
    cats: Dict[str, List[TR]] = {}
    for r in results:
        cats.setdefault(r.category, []).append(r)

    # Status colours
    sc = {'PASS': '#22c55e', 'FAIL': '#ef4444', 'WARN': '#f59e0b', 'SKIP': '#64748b'}
    bg = {'PASS': 'rgba(34,197,94,0.12)', 'FAIL': 'rgba(239,68,68,0.12)',
          'WARN': 'rgba(245,158,11,0.12)', 'SKIP': 'rgba(100,116,139,0.08)'}

    def badge(status: str) -> str:
        c = sc.get(status, '#888')
        b = bg.get(status, 'transparent')
        return (f'<span style="display:inline-block;padding:2px 10px;border-radius:9999px;'
                f'font-size:.72rem;font-weight:700;background:{b};color:{c};'
                f'border:1px solid {c}40">{status}</span>')

    rows_html = []
    for cat, cat_results in cats.items():
        cat_pass = sum(1 for r in cat_results if r.status == 'PASS')
        cat_total = len(cat_results)
        cat_fail  = sum(1 for r in cat_results if r.status == 'FAIL')
        hdr_color = '#ef4444' if cat_fail else '#22c55e'
        rows_html.append(
            f'<tr><td colspan="5" style="background:#0f172a;padding:10px 14px;'
            f'font-weight:700;font-size:.82rem;color:{hdr_color};'
            f'border-top:2px solid #334155;letter-spacing:.04em">'
            f'{_esc(cat)} &nbsp;<span style="color:#475569;font-weight:400;font-size:.74rem">'
            f'{cat_pass}/{cat_total} passed</span></td></tr>'
        )
        for r in cat_results:
            detail_html = ''
            if r.status in ('FAIL', 'WARN') and (r.details or r.expected or r.actual):
                detail_html = (
                    f'<br><span style="font-size:.7rem;color:#94a3b8">'
                    + (f'<b>Details:</b> {_esc(r.details)}<br>' if r.details else '')
                    + (f'<b>Expected:</b> {_esc(r.expected)}&nbsp;&nbsp;'
                       f'<b>Actual:</b> {_esc(r.actual)}' if r.expected or r.actual else '')
                    + '</span>'
                )
            rows_html.append(
                f'<tr>'
                f'<td style="padding:8px 14px;color:#cbd5e1;border-bottom:1px solid #1e293b;'
                f'word-break:break-word">{_esc(r.name)}{detail_html}</td>'
                f'<td style="padding:8px 14px;border-bottom:1px solid #1e293b;'
                f'white-space:nowrap">{badge(r.status)}</td>'
                f'</tr>'
            )

    kpi_items = [
        ('Total Tests',  str(total),   '#60a5fa', 'All test cases executed'),
        ('Passed',       str(passed),  '#22c55e', 'Tests that passed'),
        ('Failed',       str(failed),  '#ef4444', 'Tests that failed — require attention'),
        ('Warnings',     str(warned),  '#f59e0b', 'Non-critical issues'),
        ('Skipped',      str(skipped), '#64748b', 'Tests skipped due to missing prerequisites'),
        ('Pass Rate',    f'{pass_rate}%',
         '#22c55e' if pass_rate == 100 else ('#f59e0b' if pass_rate >= 80 else '#ef4444'),
         'Percentage of PASS results'),
    ]

    kpi_html = ''.join(
        f'<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;'
        f'padding:14px 12px;text-align:center;min-width:0">'
        f'<div style="font-size:.72rem;color:#94a3b8;font-weight:600;margin-bottom:4px">{_esc(l)}</div>'
        f'<div style="font-size:1.6rem;font-weight:700;color:{c}">{_esc(v)}</div>'
        f'<div style="font-size:.65rem;color:#475569;margin-top:2px">{_esc(tip)}</div>'
        f'</div>'
        for l, v, c, tip in kpi_items
    )

    overall_color = '#22c55e' if failed == 0 else '#ef4444'
    overall_label = 'ALL TESTS PASSED' if failed == 0 and warned == 0 else \
                    'PASSED WITH WARNINGS' if failed == 0 else 'TESTS FAILED'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DataPulse QA Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f172a;color:#e2e8f0;font-size:14px;padding:0}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
th{{background:#0f172a;color:#64748b;font-weight:600;text-align:left;
    padding:9px 14px;border-bottom:2px solid #334155;white-space:nowrap}}
tr:hover td{{background:#1a2744}}
</style>
</head>
<body>
<div style="background:linear-gradient(135deg,#1e3a5f,#1e40af);padding:22px 36px;
            border-bottom:1px solid #1e3a8a;display:flex;align-items:center;
            justify-content:space-between">
  <div>
    <div style="font-size:1.4rem;font-weight:700;color:#fff">DataPulse — QA Testing Result</div>
    <div style="color:#93c5fd;font-size:.8rem;margin-top:4px">
      Output folder: {_esc(os.path.abspath(outdir))} &nbsp;|&nbsp; {_esc(ts)}
    </div>
  </div>
  <div style="background:rgba(0,0,0,.25);border:2px solid {overall_color};border-radius:10px;
              padding:10px 24px;text-align:center;flex-shrink:0">
    <div style="font-size:.7rem;color:#94a3b8;font-weight:600;letter-spacing:.08em">OVERALL</div>
    <div style="font-size:1rem;font-weight:800;color:{overall_color};margin-top:2px">
      {_esc(overall_label)}
    </div>
  </div>
</div>

<div style="padding:28px 36px">

  <!-- KPI row -->
  <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:28px">
    {kpi_html}
  </div>

  <!-- Test table -->
  <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;
              overflow:hidden;margin-bottom:28px">
    <div style="padding:14px 18px;border-bottom:1px solid #334155;
                display:flex;align-items:center;gap:12px">
      <span style="font-size:.95rem;font-weight:600;color:#cbd5e1">Test Results</span>
      <span style="font-size:.75rem;color:#475569">{total} test cases across {len(cats)} categories</span>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th style="width:85%">Test Case</th>
            <th style="width:15%">Status</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Legend -->
  <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:.75rem;color:#64748b">
    <span><b style="color:#22c55e">PASS</b> — Test assertion met</span>
    <span><b style="color:#ef4444">FAIL</b> — Test assertion failed — must be investigated</span>
    <span><b style="color:#f59e0b">WARN</b> — Non-critical issue, review recommended</span>
    <span><b style="color:#64748b">SKIP</b> — Test skipped (prerequisite not met)</span>
  </div>
</div>
</body>
</html>"""

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'\n[DONE] QA report written: {report_path}')


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description='DataPulse QA Validation Suite — validates check_datapulse.py outputs'
    )
    parser.add_argument(
        '--outdir', default=OUTPUT_DIR,
        help=f'Path to check_datapulse.py output folder (default: {OUTPUT_DIR})'
    )
    parser.add_argument(
        '--report', default=QA_REPORT,
        help=f'Path for QA HTML report (default: {QA_REPORT})'
    )
    args = parser.parse_args()

    outdir = os.path.normpath(args.outdir)
    if not os.path.isdir(outdir):
        print(f'[ERROR] Output folder not found: {outdir}', file=sys.stderr)
        print(f'        Run check_datapulse.py first to generate output files.', file=sys.stderr)
        sys.exit(1)

    results = run_all(outdir)

    # Print summary
    total   = len(results)
    passed  = sum(1 for r in results if r.status == 'PASS')
    failed  = sum(1 for r in results if r.status == 'FAIL')
    warned  = sum(1 for r in results if r.status == 'WARN')
    skipped = sum(1 for r in results if r.status == 'SKIP')
    pr      = round(passed / total * 100, 1) if total else 0.0
    print(f'\n{"="*60}')
    print(f'  RESULTS: {passed} passed | {failed} failed | {warned} warned | {skipped} skipped')
    print(f'  PASS RATE: {pr}% ({passed}/{total})')
    print(f'{"="*60}')

    generate_html(results, outdir, args.report)

    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
