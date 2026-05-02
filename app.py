#!/usr/bin/env python3
"""DataPulse — Streamlit Data Validation Wizard
Run with:  streamlit run app.py
"""

import io
import os
import sys
import csv
import json
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd
import streamlit as st

# Allow importing check_datapulse from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from check_datapulse import validate  # noqa: E402

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="DataPulse Wizard",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STEPS = [
    "Data Ingestion",
    "Column Mapping",
    "Value Mapping",
    "Review & Save",
    "Run Validation",
    "Results",
]

ALL_RULES = [
    "DIRECT", "TRIM", "UPPERCASE", "LOWERCASE",
    "NUMERIC", "DECIMAL", "VALUE_MAP",
    "STRIP_PREFIX", "ZEROPAD", "DATE_FORMAT",
]

CHAIN_RULES = ["NUMERIC", "ZEROPAD", "UPPERCASE", "LOWERCASE", "TRIM"]

DATE_TOKENS = [
    "YYYY-MM-DD", "DD-MMM-YYYY", "MM/DD/YYYY", "DD/MM/YYYY",
    "YYYY/MM/DD", "YYYYMMDD", "DD-MM-YYYY", "MM-DD-YYYY",
]

SEP_OPTIONS  = {"Comma ( , )": ",", "Pipe ( | )": "|", "Tab": "\t", "Semicolon ( ; )": ";"}
MATCH_MODES  = ["leftout", "full", "rightout", "union"]

# ---------------------------------------------------------------------------
# Session-state initialisation
# ---------------------------------------------------------------------------
_DEFAULTS: dict = {
    "step":         1,
    "src_folder":   "",
    "dst_folder":   "",
    "src_sep":      ",",
    "dst_sep":      ",",
    "src_df":       None,
    "dst_df":       None,
    "col_mapping":  [],   # list[dict] — source_column, destination_column, matching_rule, is_key, seq
    "val_mapping":  [],   # list[dict] — column_name, source_value, destination_value, rule_description
    "colmap_path":  "",
    "valmap_path":  "",
    "output_dir":   "output",
    "match_mode":   "leftout",
    "tolerance":    0.01,
    "run_mode":     "PROD",
    "colmap_sep":   ",",
    "valmap_sep":   ",",
    "run_results":  None,
    "run_error":    None,
}

def _init_state() -> None:
    for k, v in _DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _load_folder(folder: str, sep: str) -> tuple[pd.DataFrame | None, list[str], str | None]:
    """Load + merge all CSVs in *folder*.  Returns (df, file_list, error_or_None)."""
    if not folder or not os.path.isdir(folder):
        return None, [], f'Folder not found: "{folder}"'
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".csv"))
    if not files:
        return None, [], f'No CSV files found in "{folder}"'
    dfs, ref_cols = [], None
    for fname in files:
        path = os.path.join(folder, fname)
        try:
            df = pd.read_csv(path, sep=sep, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        except Exception as exc:
            return None, [], f'Cannot read "{fname}": {exc}'
        if ref_cols is None:
            ref_cols = sorted(c.strip().lower() for c in df.columns)
        else:
            cur_cols = sorted(c.strip().lower() for c in df.columns)
            if cur_cols != ref_cols:
                return None, [], f'Schema mismatch: "{fname}" has different columns'
        dfs.append(df)
    merged = pd.concat(dfs, ignore_index=True)
    return merged, files, None


def _col_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Return a per-column summary DataFrame."""
    rows = []
    for col in df.columns:
        series = df[col]
        nulls  = (series == "").sum() + series.isna().sum()
        rows.append({
            "Column":    col,
            "Nulls":     int(nulls),
            "Null %":    round(nulls / max(len(series), 1) * 100, 1),
            "Distinct":  int(series.nunique()),
            "Sample":    ", ".join(str(v) for v in series.dropna().unique()[:3]),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------
def _render_progress() -> None:
    step = st.session_state.step
    cols = st.columns(len(STEPS))
    for i, (col, name) in enumerate(zip(cols, STEPS), 1):
        if i < step:
            col.markdown(
                f'<div style="background:#052e16;border:1px solid #16a34a;border-radius:8px;'
                f'padding:8px 4px;text-align:center">'
                f'<div style="font-size:.8rem;font-weight:700;color:#4ade80">✓ {i}</div>'
                f'<div style="font-size:.65rem;color:#4ade80">{name}</div></div>',
                unsafe_allow_html=True,
            )
        elif i == step:
            col.markdown(
                f'<div style="background:#1e1b4b;border:2px solid #6366f1;border-radius:8px;'
                f'padding:8px 4px;text-align:center">'
                f'<div style="font-size:.8rem;font-weight:700;color:#818cf8">▶ {i}</div>'
                f'<div style="font-size:.65rem;color:#a5b4fc">{name}</div></div>',
                unsafe_allow_html=True,
            )
        else:
            col.markdown(
                f'<div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;'
                f'padding:8px 4px;text-align:center">'
                f'<div style="font-size:.8rem;font-weight:700;color:#475569">{i}</div>'
                f'<div style="font-size:.65rem;color:#475569">{name}</div></div>',
                unsafe_allow_html=True,
            )
    st.write("")


def _nav(prev_step: int | None = None, next_step: int | None = None,
         can_next: bool = True) -> None:
    """Render Prev / Next navigation buttons."""
    c_prev, _, c_next = st.columns([2, 6, 2])
    if prev_step and c_prev.button("← Back", use_container_width=True):
        st.session_state.step = prev_step
        st.rerun()
    if next_step:
        if c_next.button("Next →", use_container_width=True, type="primary",
                         disabled=not can_next):
            st.session_state.step = next_step
            st.rerun()


def _dataset_panel(df: pd.DataFrame, label: str) -> None:
    """Summary metrics + column table + configurable row preview."""
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Rows",    f"{len(df):,}")
    m2.metric("Columns", len(df.columns))
    total_null = int((df == "").sum().sum() + df.isna().sum().sum())
    m3.metric("Null Cells",  f"{total_null:,}")
    m4.metric("Distinct Rows", f"{df.drop_duplicates().shape[0]:,}")

    with st.expander("Column summary", expanded=False):
        st.dataframe(_col_summary(df), use_container_width=True, hide_index=True)

    n = st.slider(f"Preview rows — {label}", 5, min(200, len(df)), 10, key=f"prev_slider_{label}")
    st.dataframe(df.head(n), use_container_width=True, hide_index=True)


def _sep_selectbox(label: str, current: str, key: str) -> str:
    """Separator picker — returns the actual separator character."""
    reverse = {v: k for k, v in SEP_OPTIONS.items()}
    display = reverse.get(current, "Comma ( , )")
    chosen  = st.selectbox(label, list(SEP_OPTIONS.keys()),
                           index=list(SEP_OPTIONS.keys()).index(display), key=key)
    return SEP_OPTIONS[chosen]


# ---------------------------------------------------------------------------
# Rule builder widget
# ---------------------------------------------------------------------------
def _rule_builder(prefix: str) -> str:
    """Interactive rule builder; returns the full rule string."""
    base = st.selectbox("Matching rule", ALL_RULES, key=f"{prefix}_base")

    suffix = ""
    if base == "STRIP_PREFIX":
        p = st.text_input("Prefix to strip (e.g. CUST, BR-, CD-)", key=f"{prefix}_pfx")
        suffix = f":{p}"
    elif base == "ZEROPAD":
        n = st.number_input("Target length", min_value=1, max_value=64,
                            value=10, step=1, key=f"{prefix}_zn")
        suffix = f":{int(n)}"
    elif base == "DATE_FORMAT":
        dc1, dc2 = st.columns(2)
        src_tok = dc1.selectbox("Source format", DATE_TOKENS, key=f"{prefix}_df_src")
        dst_tok = dc2.selectbox("Dest format",   DATE_TOKENS, index=1, key=f"{prefix}_df_dst")
        suffix  = f":{src_tok}->{dst_tok}"

    full_rule = f"{base}{suffix}"

    if st.checkbox("Chain with another rule ( | )", key=f"{prefix}_do_chain"):
        chain_base = st.selectbox("Chain rule", CHAIN_RULES, key=f"{prefix}_chain_base")
        chain_sfx  = ""
        if chain_base == "ZEROPAD":
            cn = st.number_input("Chain target length", min_value=1, value=10,
                                 step=1, key=f"{prefix}_czn")
            chain_sfx = f":{int(cn)}"
        full_rule = f"{full_rule}|{chain_base}{chain_sfx}"

    st.caption(f"Rule → `{full_rule}`")
    return full_rule


# ---------------------------------------------------------------------------
# Step 1 — Data Ingestion
# ---------------------------------------------------------------------------
def _step1() -> None:
    st.header("Step 1 — Data Ingestion & Preview")

    left, right = st.columns(2)

    for side, col_widget, folder_key, sep_key, df_key, label in [
        ("Source",      left,  "src_folder", "src_sep", "src_df", "Source"),
        ("Destination", right, "dst_folder", "dst_sep", "dst_df", "Destination"),
    ]:
        with col_widget:
            st.subheader(f"{side} Data")
            folder = st.text_input(f"{side} folder path",
                                   value=st.session_state[folder_key],
                                   key=f"{folder_key}_input",
                                   placeholder="e.g. Data_Folder_Template/Source")
            sep = _sep_selectbox("File separator", st.session_state[sep_key],
                                 key=f"{sep_key}_sel")

            btn_col, reload_col = st.columns([3, 1])
            load_clicked   = btn_col.button(f"Load {side}", use_container_width=True, key=f"load_{side}")
            reload_clicked = reload_col.button("↺", key=f"reload_{side}",
                                               help="Clear cache and reload")

            if reload_clicked:
                _load_folder.clear()

            if load_clicked or reload_clicked:
                if not folder.strip():
                    st.error("Enter a folder path first.")
                else:
                    with st.spinner(f"Loading {side.lower()} data…"):
                        df, files, err = _load_folder(folder.strip(), sep)
                    if err:
                        st.error(err)
                        st.session_state[df_key] = None
                    else:
                        st.session_state[folder_key] = folder.strip()
                        st.session_state[sep_key]    = sep
                        st.session_state[df_key]     = df
                        st.success(f"Loaded **{len(df):,}** rows from **{len(files)}** file(s)")

            if st.session_state[df_key] is not None:
                _dataset_panel(st.session_state[df_key], label)

    can_next = (st.session_state.src_df is not None
                and st.session_state.dst_df is not None)
    if not can_next:
        st.info("Load both Source and Destination folders to continue.")

    _nav(prev_step=None, next_step=2, can_next=can_next)


# ---------------------------------------------------------------------------
# Step 2 — Column Mapping Wizard
# ---------------------------------------------------------------------------
def _step2() -> None:
    st.header("Step 2 — Column Mapping Wizard")

    src_all = list(st.session_state.src_df.columns)
    dst_all = list(st.session_state.dst_df.columns)

    mapped_src = {m["source_column"]      for m in st.session_state.col_mapping}
    mapped_dst = {m["destination_column"] for m in st.session_state.col_mapping}

    avail_src = [c for c in src_all if c not in mapped_src]
    avail_dst = [c for c in dst_all if c not in mapped_dst]

    # ── Add new mapping ───────────────────────────────────────────────────
    with st.expander("➕  Add Column Mapping", expanded=True):
        c1, c2, c3 = st.columns([3, 3, 1])
        src_sel = c1.selectbox("Source column",      avail_src or ["(all mapped)"],
                               key="cm_src_sel")
        dst_sel = c2.selectbox("Destination column", avail_dst or ["(all mapped)"],
                               key="cm_dst_sel")
        is_key  = c3.checkbox("Key?", key="cm_is_key", help="Mark as composite key column")

        rule_str = _rule_builder("cm_rb")

        if st.button("Add Mapping", type="primary", use_container_width=True,
                     disabled=(not avail_src or not avail_dst)):
            seq = len(st.session_state.col_mapping) + 1
            st.session_state.col_mapping.append({
                "seq":                seq,
                "source_column":      src_sel,
                "destination_column": dst_sel,
                "matching_rule":      rule_str,
                "is_key":             "true" if is_key else "false",
            })
            st.rerun()

    # ── Current mappings ──────────────────────────────────────────────────
    if st.session_state.col_mapping:
        st.subheader(f"Column Mappings  ({len(st.session_state.col_mapping)})")

        cm_df   = pd.DataFrame(st.session_state.col_mapping)
        edited  = st.data_editor(
            cm_df,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            key="cm_editor",
            column_config={
                "seq":          st.column_config.NumberColumn("Seq", width="small"),
                "is_key":       st.column_config.SelectboxColumn("Key?", options=["true", "false"]),
                "matching_rule": st.column_config.TextColumn("Rule", width="large"),
            },
        )

        sc1, sc2 = st.columns([2, 8])
        if sc1.button("Save edits", key="cm_save"):
            st.session_state.col_mapping = edited.dropna(
                subset=["source_column", "destination_column"]
            ).to_dict("records")
            st.success("Mappings updated.")

        # ── Distinct value preview ────────────────────────────────────────
        with st.expander("🔍  Column value preview (select a pair)"):
            pairs = [f"{m['source_column']}  →  {m['destination_column']}"
                     for m in st.session_state.col_mapping]
            sel_pair = st.selectbox("Pair", pairs, key="cm_pair_sel")
            if sel_pair:
                idx   = pairs.index(sel_pair)
                sc    = st.session_state.col_mapping[idx]["source_column"]
                dc    = st.session_state.col_mapping[idx]["destination_column"]
                vc1, vc2 = st.columns(2)
                with vc1:
                    st.caption(f"**Source  —  {sc}**")
                    s_vc = (st.session_state.src_df[sc]
                            .value_counts().reset_index()
                            .rename(columns={sc: "Value", "count": "Count"}))
                    st.dataframe(s_vc.head(30), use_container_width=True,
                                 hide_index=True, height=250)
                with vc2:
                    st.caption(f"**Destination  —  {dc}**")
                    d_vc = (st.session_state.dst_df[dc]
                            .value_counts().reset_index()
                            .rename(columns={dc: "Value", "count": "Count"}))
                    st.dataframe(d_vc.head(30), use_container_width=True,
                                 hide_index=True, height=250)

    can_next = len(st.session_state.col_mapping) > 0
    if not can_next:
        st.info("Add at least one column mapping to continue.")

    _nav(prev_step=1, next_step=3, can_next=can_next)


# ---------------------------------------------------------------------------
# Step 3 — Value Mapping (Rule Creation)
# ---------------------------------------------------------------------------
def _step3() -> None:
    st.header("Step 3 — Column Value Mapping  (Rule Creation)")

    vm_cols = [m for m in st.session_state.col_mapping
               if "VALUE_MAP" in m["matching_rule"].upper()]

    if not vm_cols:
        st.info("No columns use VALUE_MAP in the column mapping.  "
                "Nothing to configure here — proceed to the next step.")
        _nav(prev_step=2, next_step=4)
        return

    for mapping in vm_cols:
        src_col = mapping["source_column"]
        dst_col = mapping["destination_column"]

        st.subheader(f"{src_col}  →  {dst_col}")

        # Reference: distinct values side-by-side
        with st.expander("Distinct value reference", expanded=False):
            rv1, rv2 = st.columns(2)
            with rv1:
                st.caption(f"Source  —  {src_col}")
                s_vc = (st.session_state.src_df[src_col]
                        .value_counts().reset_index()
                        .rename(columns={src_col: "Value", "count": "Count"}))
                st.dataframe(s_vc, use_container_width=True, hide_index=True, height=220)
            with rv2:
                st.caption(f"Destination  —  {dst_col}")
                d_vc = (st.session_state.dst_df[dst_col]
                        .value_counts().reset_index()
                        .rename(columns={dst_col: "Value", "count": "Count"}))
                st.dataframe(d_vc, use_container_width=True, hide_index=True, height=220)

        # Current rules for this column
        col_rules = [r for r in st.session_state.val_mapping
                     if r["column_name"] == src_col]

        # Auto-generate button
        if st.button(f"Auto-generate rows from source distinct values  ({src_col})",
                     key=f"autogen_{src_col}"):
            existing_src_vals = {r["source_value"] for r in col_rules}
            src_vals = sorted(
                str(v) for v in st.session_state.src_df[src_col].dropna().unique()
                if str(v) not in existing_src_vals
            )
            new_rows = [
                {"column_name": src_col, "source_value": v,
                 "destination_value": "", "rule_description": ""}
                for v in src_vals
            ]
            st.session_state.val_mapping = (
                [r for r in st.session_state.val_mapping if r["column_name"] != src_col]
                + col_rules
                + new_rows
            )
            st.rerun()

        # Editable rules table
        dest_vals = sorted(str(v) for v in st.session_state.dst_df[dst_col].dropna().unique())

        rules_df = pd.DataFrame(
            col_rules if col_rules
            else [{"column_name": src_col, "source_value": "",
                   "destination_value": "", "rule_description": ""}]
        )

        edited = st.data_editor(
            rules_df,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            key=f"vm_editor_{src_col}",
            column_config={
                "column_name":       st.column_config.TextColumn("Column", disabled=True),
                "source_value":      st.column_config.TextColumn("Source Value"),
                "destination_value": st.column_config.SelectboxColumn(
                    "Destination Value", options=dest_vals,
                    help="Choose from distinct destination values",
                ),
                "rule_description":  st.column_config.TextColumn("Description (optional)"),
            },
        )

        bc1, bc2 = st.columns([2, 8])
        if bc1.button(f"Save rules  ({src_col})", key=f"vm_save_{src_col}", type="primary"):
            # Validate: no duplicate source values, no blank source values
            clean = edited.dropna(subset=["source_value"])
            clean = clean[clean["source_value"].str.strip() != ""]
            dups  = clean["source_value"].duplicated().sum()
            if dups:
                st.error(f"{dups} duplicate source value(s) found — remove them before saving.")
            else:
                # Ensure column_name is filled
                clean = clean.copy()
                clean["column_name"] = src_col
                st.session_state.val_mapping = (
                    [r for r in st.session_state.val_mapping if r["column_name"] != src_col]
                    + clean.to_dict("records")
                )
                st.success(f"Saved {len(clean)} rules for **{src_col}**.")

        st.divider()

    _nav(prev_step=2, next_step=4)


# ---------------------------------------------------------------------------
# Step 4 — Review & Persist Mapping Artifacts
# ---------------------------------------------------------------------------
def _step4() -> None:
    st.header("Step 4 — Review & Save Mapping Files")

    # ── Column mapping review ─────────────────────────────────────────────
    st.subheader("Column Mapping")
    cm_df = pd.DataFrame(st.session_state.col_mapping)[
        ["seq", "source_column", "destination_column", "matching_rule", "is_key"]
    ] if st.session_state.col_mapping else pd.DataFrame(
        columns=["seq", "source_column", "destination_column", "matching_rule", "is_key"]
    )
    st.dataframe(cm_df, use_container_width=True, hide_index=True)

    # ── Value mapping review ──────────────────────────────────────────────
    st.subheader("Value Mapping")
    if st.session_state.val_mapping:
        vm_df = pd.DataFrame(st.session_state.val_mapping)[
            ["column_name", "source_value", "destination_value", "rule_description"]
        ]
        st.dataframe(vm_df, use_container_width=True, hide_index=True)
    else:
        st.info("No value mapping rules defined.")

    st.divider()

    # ── Save location ─────────────────────────────────────────────────────
    st.subheader("Save Location")

    # Auto-derive base folder from source folder path
    src_parent = str(Path(st.session_state.src_folder).parent) \
        if st.session_state.src_folder else ""
    base_folder = st.text_input(
        "Base data folder (Mapping_Rule/ sub-folder will be created here)",
        value=src_parent,
        key="save_base_folder",
        placeholder="e.g. Data_Folder_Template",
    )
    mapping_dir = os.path.join(base_folder, "Mapping_Rule") if base_folder else "Mapping_Rule"
    st.caption(f"Files will be saved to `{mapping_dir}/`")

    sc1, sc2 = st.columns(2)

    with sc1:
        if st.button("💾  Save column_mapping.csv", use_container_width=True, type="primary"):
            try:
                os.makedirs(mapping_dir, exist_ok=True)
                save_path = os.path.join(mapping_dir, "column_mapping.csv")
                out_df = cm_df[["source_column", "destination_column", "matching_rule", "is_key"]]
                out_df.to_csv(save_path, index=False)
                st.session_state.colmap_path = save_path
                st.success(f"Saved → `{save_path}`")
            except Exception as exc:
                st.error(f"Save failed: {exc}")

    with sc2:
        if st.button("💾  Save value_mapping.csv", use_container_width=True, type="primary"):
            try:
                os.makedirs(mapping_dir, exist_ok=True)
                save_path = os.path.join(mapping_dir, "value_mapping.csv")
                if st.session_state.val_mapping:
                    vm_out = pd.DataFrame(st.session_state.val_mapping)[
                        ["column_name", "source_value", "destination_value", "rule_description"]
                    ]
                else:
                    vm_out = pd.DataFrame(
                        columns=["column_name", "source_value", "destination_value", "rule_description"]
                    )
                vm_out.to_csv(save_path, index=False)
                st.session_state.valmap_path = save_path
                st.success(f"Saved → `{save_path}`")
            except Exception as exc:
                st.error(f"Save failed: {exc}")

    # ── In-browser download (no disk write needed) ────────────────────────
    st.divider()
    st.subheader("Download (without saving to disk)")
    dl1, dl2 = st.columns(2)

    cm_buf = io.StringIO()
    cm_df[["source_column", "destination_column", "matching_rule", "is_key"]].to_csv(
        cm_buf, index=False
    )
    dl1.download_button(
        "⬇️  column_mapping.csv",
        data=cm_buf.getvalue(),
        file_name="column_mapping.csv",
        mime="text/csv",
        use_container_width=True,
    )

    if st.session_state.val_mapping:
        vm_buf = io.StringIO()
        pd.DataFrame(st.session_state.val_mapping)[
            ["column_name", "source_value", "destination_value", "rule_description"]
        ].to_csv(vm_buf, index=False)
        dl2.download_button(
            "⬇️  value_mapping.csv",
            data=vm_buf.getvalue(),
            file_name="value_mapping.csv",
            mime="text/csv",
            use_container_width=True,
        )

    can_next = bool(st.session_state.colmap_path)
    if not can_next:
        st.info("Save column_mapping.csv to disk first — the engine reads it from a file path.")
    _nav(prev_step=3, next_step=5, can_next=can_next)


# ---------------------------------------------------------------------------
# Step 5 — Run Validation
# ---------------------------------------------------------------------------
def _step5() -> None:
    st.header("Step 5 — Run Validation")

    with st.expander("Configuration", expanded=True):
        r1c1, r1c2 = st.columns(2)

        src   = r1c1.text_input("Source folder",      value=st.session_state.src_folder,  key="run_src")
        dst   = r1c1.text_input("Destination folder", value=st.session_state.dst_folder,  key="run_dst")
        cmap  = r1c1.text_input("column_mapping.csv", value=st.session_state.colmap_path, key="run_cmap")
        vmap  = r1c1.text_input("value_mapping.csv",  value=st.session_state.valmap_path, key="run_vmap")

        out_dir    = r1c2.text_input("Output directory",  value=st.session_state.output_dir, key="run_out")
        match_mode = r1c2.selectbox("Match mode", MATCH_MODES,
                                    index=MATCH_MODES.index(st.session_state.match_mode),
                                    key="run_mode_sel")
        tolerance  = r1c2.number_input("Tolerance", value=st.session_state.tolerance,
                                       min_value=0.0, format="%.4f", key="run_tol")
        run_mode   = r1c2.selectbox("Run mode", ["PROD", "TEST"], key="run_mode_flag")

        st.write("**CSV Separators**")
        sc1, sc2, sc3, sc4 = st.columns(4)
        src_sep  = _sep_selectbox("Source",      st.session_state.src_sep,  "run_src_sep")
        dst_sep  = _sep_selectbox("Destination", st.session_state.dst_sep,  "run_dst_sep")
        cmap_sep = _sep_selectbox("ColMap",       st.session_state.colmap_sep, "run_cm_sep")
        vmap_sep = _sep_selectbox("ValMap",       st.session_state.valmap_sep, "run_vm_sep")

    if st.button("▶  Run Validation", type="primary", use_container_width=True):
        # Persist config back to session state
        for k, v in [("src_folder", src), ("dst_folder", dst),
                     ("colmap_path", cmap), ("valmap_path", vmap),
                     ("output_dir", out_dir), ("match_mode", match_mode),
                     ("tolerance", tolerance), ("run_mode", run_mode),
                     ("src_sep", src_sep), ("dst_sep", dst_sep),
                     ("colmap_sep", cmap_sep), ("valmap_sep", vmap_sep)]:
            st.session_state[k] = v

        log_buf = io.StringIO()
        with st.spinner("Running validation engine…"):
            try:
                with redirect_stdout(log_buf):
                    results = validate(
                        source_folder   = src,
                        dest_folder     = dst,
                        colmap_path     = cmap,
                        valmap_path     = vmap,
                        outdir          = out_dir,
                        tolerance       = tolerance,
                        match_mode      = match_mode,
                        source_sep      = src_sep,
                        dest_sep        = dst_sep,
                        colmap_sep      = cmap_sep,
                        valmap_sep      = vmap_sep,
                    )
                st.session_state.run_results = results
                st.session_state.run_error   = None
                st.success("Validation complete!  Advancing to Results…")
            except Exception as exc:
                st.session_state.run_error   = str(exc)
                st.session_state.run_results = None
                st.error(f"Validation failed: {exc}")

        log_output = log_buf.getvalue()
        if log_output:
            with st.expander("Validation log", expanded=False):
                st.code(log_output, language=None)

        if st.session_state.run_results:
            st.session_state.step = 6
            st.rerun()

    _nav(prev_step=4, next_step=None)


# ---------------------------------------------------------------------------
# Step 6 — Results
# ---------------------------------------------------------------------------
def _step6() -> None:
    st.header("Step 6 — Results")

    if not st.session_state.run_results:
        if st.session_state.run_error:
            st.error(f"Last run failed: {st.session_state.run_error}")
        else:
            st.warning("No results yet.  Run validation in Step 5 first.")
        _nav(prev_step=5, next_step=None)
        return

    results      = st.session_state.run_results
    summary      = results.get("summary", {})
    detail_rows  = results.get("detail_rows", [])
    by_type      = summary.get("mismatch_count_by_type", {})

    # ── KPI row ───────────────────────────────────────────────────────────
    k = st.columns(7)
    k[0].metric("Pass Rate",          f"{summary.get('pass_rate', 0):.1f}%")
    k[1].metric("Source Rows",        f"{summary.get('source_row_count', 0):,}")
    k[2].metric("Dest Rows",          f"{summary.get('dest_row_count', 0):,}")
    k[3].metric("Matched Keys",       f"{summary.get('matched_key_count', 0):,}")
    k[4].metric("Total Issues",       f"{summary.get('total_issues', 0):,}")
    k[5].metric("Missing in Dest",    f"{by_type.get('KEY_MISSING_IN_DEST', 0):,}")
    k[6].metric("Value Mismatches",   f"{by_type.get('VALUE_MISMATCH', 0):,}")

    st.divider()

    # ── Issues table ──────────────────────────────────────────────────────
    if detail_rows:
        df = pd.DataFrame(detail_rows)

        st.subheader(f"Issues  ({len(df):,})")
        f1, f2, f3 = st.columns([3, 2, 3])
        types    = ["All"] + sorted(df["issue_type"].unique().tolist())
        sevs     = ["All"] + sorted(df["severity"].unique().tolist())
        sel_type = f1.selectbox("Issue type", types,  key="res_type")
        sel_sev  = f2.selectbox("Severity",   sevs,   key="res_sev")
        search   = f3.text_input("Search composite key", key="res_search",
                                 placeholder="partial key value…")

        mask = pd.Series([True] * len(df), index=df.index)
        if sel_type != "All":
            mask &= df["issue_type"] == sel_type
        if sel_sev != "All":
            mask &= df["severity"] == sel_sev
        if search:
            mask &= df["composite_key"].str.contains(search, na=False, case=False)

        st.dataframe(df[mask], use_container_width=True, hide_index=True, height=400)

        # CSV download of filtered issues
        filt_buf = io.StringIO()
        df[mask].to_csv(filt_buf, index=False)
        st.download_button("⬇️  Download filtered issues (CSV)",
                           data=filt_buf.getvalue(),
                           file_name="issues_filtered.csv",
                           mime="text/csv")
    else:
        st.success("🎉  No issues found — 100 % pass rate!")

    st.divider()

    # ── Full HTML dashboard ───────────────────────────────────────────────
    dash_path = os.path.join(st.session_state.output_dir, "dashboard.html")
    if os.path.exists(dash_path):
        st.subheader("Interactive Dashboard")

        with open(dash_path, "rb") as fh:
            st.download_button(
                "⬇️  Download full dashboard (HTML)",
                data=fh.read(),
                file_name="dashboard.html",
                mime="text/html",
                use_container_width=True,
            )

        with open(dash_path, "r", encoding="utf-8") as fh:
            html_content = fh.read()
        st.components.v1.html(html_content, height=860, scrolling=True)
    else:
        st.info(f"Dashboard not found at `{dash_path}`.  "
                "It is generated automatically when the engine runs.")

    _nav(prev_step=5, next_step=None)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            "<div style='text-align:center;padding:10px 0 4px'>"
            "<span style='font-size:2rem'>🔍</span><br>"
            "<span style='font-size:1.1rem;font-weight:800;color:#818cf8'>DataPulse</span><br>"
            "<span style='font-size:.72rem;color:#7a8eaf'>Data Validation Wizard</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.divider()

        current = st.session_state.step
        for i, name in enumerate(STEPS, 1):
            if i < current:
                icon, style = "✅", "color:#4ade80;font-weight:600"
                disabled    = False
            elif i == current:
                icon, style = "▶", "color:#818cf8;font-weight:700"
                disabled    = True
            else:
                icon, style = f"{i}.", "color:#475569"
                disabled    = True

            label = f"{icon}  {name}"
            if not disabled:
                if st.button(label, key=f"sb_{i}", use_container_width=True):
                    st.session_state.step = i
                    st.rerun()
            else:
                st.button(label, key=f"sb_{i}", use_container_width=True,
                          disabled=True)

        st.divider()
        st.caption(f"Step {current} of {len(STEPS)}")

        if st.button("🔄  Reset wizard", use_container_width=True, help="Clear all state"):
            for k, v in _DEFAULTS.items():
                st.session_state[k] = v
            _load_folder.clear()
            st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
_init_state()
_render_sidebar()

_render_progress()

step_fns = {1: _step1, 2: _step2, 3: _step3, 4: _step4, 5: _step5, 6: _step6}
step_fns.get(st.session_state.step, _step1)()
