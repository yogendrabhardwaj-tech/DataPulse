import pandas as pd  # type: ignore
import numpy as np  # type: ignore
from datetime import datetime, timedelta
import os
import random
from collections import Counter, defaultdict

# ----------------------------
# CONFIG (edit if needed)
# ----------------------------
RANDOM_SEED = 42
np.random.seed(RANDOM_SEED)
random.seed(RANDOM_SEED)

# Files
SOURCE_CSV = "Source/cd_source.csv"
COLUMN_MAPPING_CSV = "Mapping_Rule/column_mapping.csv"
VALUE_MAPPING_CSV = "Mapping_Rule/value_mapping.csv"

OUTDIR = "Destination"  # output folder for generated files
os.makedirs(OUTDIR, exist_ok=True)

DEST_CLEAN_OUT = os.path.join(OUTDIR, "cd_destination_clean.csv")
DEST_CORRUPT_OUT = os.path.join(OUTDIR, "cd_destination_corrupt.csv")
SUMMARY_TXT_OUT = os.path.join(OUTDIR, "corruption_summary.txt")

# Composite key columns (logical)
COMPOSITE_KEY_SOURCE = ["Customer_ID", "CD_Number", "Issue_Date"]
COMPOSITE_KEY_DEST = ["cust_key", "cd_key", "issue_dt"]

# Numeric tolerance used by your validator (only for ensuring mismatch is obvious)
NUM_TOL = 0.01

# ----------------------------
# Helpers: transform + mapping
# ----------------------------
def load_files():
    src = pd.read_csv(SOURCE_CSV)
    colmap = pd.read_csv(COLUMN_MAPPING_CSV)
    valmap = pd.read_csv(VALUE_MAPPING_CSV)
    return src, colmap, valmap

def build_value_map(valmap: pd.DataFrame):
    """
    returns dict: { column_name: {source_value: destination_value} }
    """
    d = defaultdict(dict)
    for _, r in valmap.iterrows():
        d[str(r["column_name"])][str(r["source_value"])] = str(r["destination_value"])
    return d

_DATE_FMT_MAP = {
    "YYYY-MM-DD":  "%Y-%m-%d",
    "DD-MMM-YYYY": "%d-%b-%Y",
    "MM/DD/YYYY":  "%m/%d/%Y",
    "DD/MM/YYYY":  "%d/%m/%Y",
    "YYYY/MM/DD":  "%Y/%m/%d",
    "YYYYMMDD":    "%Y%m%d",
    "DD-MM-YYYY":  "%d-%m-%Y",
    "MM-DD-YYYY":  "%m-%d-%Y",
}

def apply_rule_transform(series: pd.Series, rule: str, value_map=None, column_name=None):
    """
    Parse standard rule codes (same codes used by check_datapulse.py):
      STRIP_PREFIX:<p>  — remove leading prefix p
      DATE_FORMAT:<src>-><dst>  — convert date format using token map
      NUMERIC           — convert to number; whole numbers become int
      DECIMAL           — convert to float
      VALUE_MAP         — map via value_map[column_name]
      DIRECT            — no change
    Chains with | are handled left-to-right.
    """
    s = series.copy()

    for part in (rule or "DIRECT").split("|"):
        part = part.strip()
        part_up = part.upper()

        if part_up.startswith("STRIP_PREFIX:"):
            prefix = part[len("STRIP_PREFIX:"):]
            s = s.astype(str).str.lstrip().str.replace(prefix, "", n=1, regex=False).str.strip()

        elif part_up.startswith("DATE_FORMAT:"):
            spec = part[len("DATE_FORMAT:"):]
            if "->" not in spec:
                continue
            src_tok, dst_tok = spec.split("->", 1)
            py_src = _DATE_FMT_MAP.get(src_tok.strip().upper())
            py_dst = _DATE_FMT_MAP.get(dst_tok.strip().upper())
            if not py_src or not py_dst:
                continue
            out = []
            for v in s.astype(str):
                v = v.strip()
                if not v or v.lower() == "nan":
                    out.append("")
                else:
                    try:
                        out.append(datetime.strptime(v, py_src).strftime(py_dst).upper())
                    except ValueError:
                        out.append(v)
            s = pd.Series(out, index=series.index)

        elif part_up == "NUMERIC":
            def _to_num(v):
                try:
                    f = float(str(v).replace(",", ""))
                    return int(f) if f == int(f) else f
                except (ValueError, TypeError):
                    return v
            s = s.map(_to_num)

        elif part_up == "DECIMAL":
            def _to_dec(v):
                try:
                    return float(str(v).replace(",", ""))
                except (ValueError, TypeError):
                    return v
            s = s.map(_to_dec)

        elif part_up == "VALUE_MAP":
            if value_map is not None and column_name is not None:
                cmap = value_map.get(column_name, {})
                s = s.astype(str).map(lambda v: cmap.get(v, v))

        # DIRECT / unknown: no change

    return s

def generate_clean_destination(src, colmap, valmap):
    """
    Generate destination dataframe from source using mapping + rules.
    """
    value_map = build_value_map(valmap)

    dest = pd.DataFrame()

    for _, m in colmap.iterrows():
        s_col = m["source_column"]
        d_col = m["destination_column"]
        rule = m.get("matching_rule", "")

        if s_col not in src.columns:
            raise ValueError(f"Source column missing: {s_col}")

        # Apply transformation and/or value mapping
        dest[d_col] = apply_rule_transform(
            src[s_col],
            rule=rule,
            value_map=value_map,
            column_name=s_col
        )

    return dest

# ----------------------------
# Corruption engine
# ----------------------------
def mutate_value(val, kind="auto"):
    """
    Mutate a single cell so it *definitely* becomes different.
    """
    if pd.isna(val):
        return "CORRUPT"

    # numeric
    if isinstance(val, (int, np.integer)):
        return int(val) + random.randint(5, 5000)

    if isinstance(val, (float, np.floating)):
        # force beyond tolerance
        delta = max(10 * NUM_TOL, round(random.uniform(0.05, 5.0), 2))
        return float(val) + delta

    # strings / codes / dates
    v = str(val)

    # if looks like DD-MMM-YYYY
    try:
        dt = datetime.strptime(v, "%d-%b-%Y")
        dt2 = dt + timedelta(days=random.randint(1, 9))
        return dt2.strftime("%d-%b-%Y").upper()
    except:
        pass

    # if single letter code
    if len(v) == 1 and v.isalpha():
        options = [c for c in list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") if c != v]
        return random.choice(options)

    # otherwise modify string
    return v + "_X"

def inject_value_mismatches(dest, row_ids, n_cols_each, key_cols):
    """
    Type 1 corruption:
    - Composite key matches (key columns untouched)
    - Random non-key columns mutated for the given row_ids
    row_ids must be pre-selected by the caller (disjoint from Type 2 rows).
    """
    non_key_cols = [c for c in dest.columns if c not in key_cols]

    changes = []  # (row_idx, col, old, new)

    for i in row_ids:
        cols = random.sample(non_key_cols, k=min(n_cols_each, len(non_key_cols)))
        for c in cols:
            old = dest.at[i, c]
            new = mutate_value(old)
            dest.at[i, c] = new
            changes.append((i, c, old, new))

    return list(row_ids), changes

def inject_partial_key_mismatches(dest, n_rows, key_cols):
    """
    Type 2 corruption:
    - Partial composite key match
    - Change exactly ONE component of composite key
    - Ensure it doesn't collide with existing keys
    """
    idx = np.random.choice(dest.index, size=n_rows, replace=False)

    # Track existing composite keys
    existing_keys = set(tuple(x) for x in dest[key_cols].astype(str).itertuples(index=False, name=None))

    changes = []  # (row_idx, key_col_changed, old, new)

    for i in idx:
        # choose which key component to corrupt
        kcol = random.choice(key_cols)
        old = dest.at[i, kcol]

        # create a new key value that is very unlikely to exist
        if kcol in ["cust_key", "cd_key"] and str(old).isdigit():
            new = int(old) + random.randint(9000000, 9999999)
        else:
            new = mutate_value(old)

        # ensure new composite key is unique
        attempt = 0
        while attempt < 10:
            key_tuple = []
            for kc in key_cols:
                key_tuple.append(str(new) if kc == kcol else str(dest.at[i, kc]))
            key_tuple = tuple(key_tuple)

            if key_tuple not in existing_keys:
                existing_keys.add(key_tuple)
                break

            # retry
            attempt += 1
            if kcol in ["cust_key", "cd_key"] and str(old).isdigit():
                new = int(old) + random.randint(9000000, 9999999)
            else:
                new = mutate_value(old)

        dest.at[i, kcol] = new
        changes.append((i, kcol, old, new))

    return idx.tolist(), changes

def write_summary(params, type1_rows, type1_changes, type2_rows, type2_changes, dest_clean, dest_corrupt):
    """
    Write a human-friendly summary for validation.
    """
    with open(SUMMARY_TXT_OUT, "w") as f:
        f.write("DATA CORRUPTION SUMMARY (Certificate of Deposit)\n")
        f.write("=================================================\n\n")
        f.write(f"Random Seed: {RANDOM_SEED}\n\n")

        f.write("User Inputs:\n")
        f.write(f" - % Type1 (Key matches, value mismatch): {params['pct_value_mismatch']}%\n")
        f.write(f" - % Type2 (Partial composite key mismatch): {params['pct_partial_key_mismatch']}%\n")
        f.write(f" - Columns corrupted per Type1 record: {params['num_cols_discrepancy']}\n\n")

        f.write("Row Counts:\n")
        f.write(f" - Total destination rows (clean): {len(dest_clean)}\n")
        f.write(f" - Total destination rows (corrupt): {len(dest_corrupt)}\n")
        f.write(f" - Type1 corrupted rows: {len(type1_rows)}\n")
        f.write(f" - Type2 corrupted rows: {len(type2_rows)}\n\n")

        # Column frequency for type1
        col_counter = Counter([c for _, c, _, _ in type1_changes])
        f.write("Type1 Column Discrepancy Frequency (top 20):\n")
        for col, cnt in col_counter.most_common(20):
            f.write(f" - {col}: {cnt}\n")
        f.write("\n")

        f.write("Sample Type1 Changes (first 20):\n")
        for i, (row_i, col, old, new) in enumerate(type1_changes[:20]):
            f.write(f" - row={row_i}, col={col}, old={old} -> new={new}\n")
        f.write("\n")

        f.write("Sample Type2 Key Changes (first 20):\n")
        for i, (row_i, kcol, old, new) in enumerate(type2_changes[:20]):
            f.write(f" - row={row_i}, key_col={kcol}, old={old} -> new={new}\n")
        f.write("\n")

        # Composite key examples
        f.write("Sample Composite Keys (first 20 corrupt rows):\n")
        sample_rows = (type1_rows + type2_rows)[:20]
        for r in sample_rows:
            key_vals = [str(dest_corrupt.at[r, c]) for c in COMPOSITE_KEY_DEST]
            f.write(f" - {'|'.join(key_vals)}\n")

    print(f"[OK] Summary written: {SUMMARY_TXT_OUT}")

# ----------------------------
# Main program (interactive)
# ----------------------------
def main():
    src, colmap, valmap = load_files()

    # Generate clean destination
    dest_clean = generate_clean_destination(src, colmap, valmap)

    # Ask user inputs one-by-one (as requested)
    pct_value_mismatch = float(input("1) Enter % of corrupt records (key matches but column values not matching): ").strip())
    pct_partial_key_mismatch = float(input("2) Enter % of corrupt records (partial composite key match): ").strip())
    num_cols_discrepancy = int(input("3) Enter number of columns to randomly corrupt per affected record (Type1): ").strip())

    # Basic guardrails
    pct_value_mismatch = max(0, min(100, pct_value_mismatch))
    pct_partial_key_mismatch = max(0, min(100, pct_partial_key_mismatch))
    num_cols_discrepancy = max(1, num_cols_discrepancy)

    total = len(dest_clean)

    # Ensure disjoint sets (avoid same row being both type1 and type2)
    n1 = int(round(total * (pct_value_mismatch / 100.0)))
    n2 = int(round(total * (pct_partial_key_mismatch / 100.0)))

    # If sum exceeds total, cap type2
    if n1 + n2 > total:
        n2 = max(0, total - n1)

    dest_corrupt = dest_clean.copy()

    # Choose Type1 rows first, then Type2 from remaining
    all_idx = np.array(dest_corrupt.index)
    np.random.shuffle(all_idx)

    type1_idx = all_idx[:n1]
    remaining = all_idx[n1:]
    type2_idx = remaining[:n2]

    # Apply corruption
    type1_rows, type1_changes = inject_value_mismatches(
        dest_corrupt, row_ids=type1_idx, n_cols_each=num_cols_discrepancy, key_cols=COMPOSITE_KEY_DEST
    )

    # For type2, we want to hit the specific chosen rows
    # easiest: temporarily subset by index order
    # We'll do manual injection using those row ids:
    # reuse logic but without resampling:
    # (small adaptation)
    def inject_partial_on_specific_rows(dest, row_ids, key_cols):
        existing_keys = set(tuple(x) for x in dest[key_cols].astype(str).itertuples(index=False, name=None))
        changes = []
        for i in row_ids:
            kcol = random.choice(key_cols)
            old = dest.at[i, kcol]
            if kcol in ["cust_key", "cd_key"] and str(old).isdigit():
                new = int(old) + random.randint(9000000, 9999999)
            else:
                new = mutate_value(old)

            attempt = 0
            while attempt < 10:
                key_tuple = tuple(str(new) if kc == kcol else str(dest.at[i, kc]) for kc in key_cols)
                if key_tuple not in existing_keys:
                    existing_keys.add(key_tuple)
                    break
                attempt += 1
                if kcol in ["cust_key", "cd_key"] and str(old).isdigit():
                    new = int(old) + random.randint(9000000, 9999999)
                else:
                    new = mutate_value(old)

            dest.at[i, kcol] = new
            changes.append((i, kcol, old, new))
        return list(row_ids), changes

    type2_rows, type2_changes = inject_partial_on_specific_rows(dest_corrupt, type2_idx, COMPOSITE_KEY_DEST)

    # Save outputs
    dest_clean.to_csv(DEST_CLEAN_OUT, index=False)
    dest_corrupt.to_csv(DEST_CORRUPT_OUT, index=False)

    params = {
        "pct_value_mismatch": pct_value_mismatch,
        "pct_partial_key_mismatch": pct_partial_key_mismatch,
        "num_cols_discrepancy": num_cols_discrepancy
    }

    write_summary(params, type1_rows, type1_changes, type2_rows, type2_changes, dest_clean, dest_corrupt)

    print("\n[OK] Files created:")
    print(f" - {DEST_CLEAN_OUT}")
    print(f" - {DEST_CORRUPT_OUT}")
    print(f" - {SUMMARY_TXT_OUT}")

if __name__ == "__main__":
    main()