#!/usr/bin/env python3
"""DataPulse — Streamlit Data Validation Wizard
Run with:  streamlit run app.py
"""

import io
import os
import sys
import json
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from check_datapulse import validate  # noqa: E402

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="DataPulse",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECTS_FILE = "projects.json"
OUTPUT_BASE   = "output"

STEPS = [
    "Data Ingestion",
    "Column Mapping",
    "Value Mapping",
    "Review & Save",
    "Run Validation",
    "Results",
]

ALL_RULES    = ["DIRECT", "TRIM", "UPPERCASE", "LOWERCASE", "NUMERIC", "DECIMAL",
                "VALUE_MAP", "STRIP_PREFIX", "ZEROPAD", "DATE_FORMAT"]
CHAIN_RULES  = ["NUMERIC", "ZEROPAD", "UPPERCASE", "LOWERCASE", "TRIM"]
DATE_TOKENS  = ["YYYY-MM-DD", "DD-MMM-YYYY", "MM/DD/YYYY", "DD/MM/YYYY",
                "YYYY/MM/DD", "YYYYMMDD", "DD-MM-YYYY", "MM-DD-YYYY"]
SEP_OPTIONS  = {"Comma ( , )": ",", "Pipe ( | )": "|", "Tab": "\t", "Semicolon ( ; )": ";"}
MATCH_MODES  = ["leftout", "full", "rightout", "union"]

# ---------------------------------------------------------------------------
# Session-state initialisation
# ---------------------------------------------------------------------------
_DEFAULTS: dict = {
    "page":               "home",   # home | project_setup | wizard | dashboard
    "project_name":       "",
    "project_description": "",
    "active_output_dir":  "",
    "step":               1,
    "src_folder":         "",
    "dst_folder":         "",
    "src_sep":            ",",
    "dst_sep":            ",",
    "src_df":             None,
    "dst_df":             None,
    "col_mapping":        [],
    "val_mapping":        [],
    "colmap_path":        "",
    "valmap_path":        "",
    "match_mode":         "leftout",
    "tolerance":          0.01,
    "run_mode":           "PROD",
    "colmap_sep":         ",",
    "valmap_sep":         ",",
    "run_results":        None,
    "run_error":          None,
}

def _init_state() -> None:
    for k, v in _DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# Project / run-history helpers
# ---------------------------------------------------------------------------
def _load_projects() -> list[dict]:
    if not os.path.exists(PROJECTS_FILE):
        return []
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_project(proj: dict) -> None:
    projects = _load_projects()
    for i, p in enumerate(projects):
        if p["name"].lower() == proj["name"].lower():
            projects[i] = proj
            break
    else:
        projects.insert(0, proj)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2)


def _get_run_history() -> list[dict]:
    """Scan output/ sub-folders for run_meta.json, return newest-first.
    Also includes the legacy flat output/ folder if it has results but no run_meta."""
    runs = []

    if os.path.isdir(OUTPUT_BASE):
        for entry in os.scandir(OUTPUT_BASE):
            if not entry.is_dir():
                continue
            meta_path    = os.path.join(entry.path, "run_meta.json")
            results_path = os.path.join(entry.path, "datapulse_results.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    meta["output_dir"] = entry.path
                    runs.append(meta)
                except Exception:
                    pass
            elif os.path.exists(results_path):
                # Legacy timestamped folder without run_meta — infer timestamp from folder name
                try:
                    with open(results_path, "r", encoding="utf-8") as f:
                        res = json.load(f)
                    ts = res.get("summary", {}).get("run_timestamp", "")
                    runs.append({
                        "project_name":        entry.name,
                        "project_description": "",
                        "run_timestamp":       ts,
                        "src_folder":          "",
                        "dst_folder":          "",
                        "output_dir":          entry.path,
                    })
                except Exception:
                    pass

    # Legacy flat output/ (old single-run structure)
    flat_results = os.path.join(OUTPUT_BASE, "datapulse_results.json")
    flat_meta    = os.path.join(OUTPUT_BASE, "run_meta.json")
    if os.path.exists(flat_results) and not os.path.exists(flat_meta):
        try:
            with open(flat_results, "r", encoding="utf-8") as f:
                res = json.load(f)
            ts = res.get("summary", {}).get("run_timestamp", "")
            runs.append({
                "project_name":        "Previous Run (legacy)",
                "project_description": "",
                "run_timestamp":       ts,
                "src_folder":          "",
                "dst_folder":          "",
                "output_dir":          OUTPUT_BASE,
            })
        except Exception:
            pass

    runs.sort(key=lambda r: r.get("run_timestamp", ""), reverse=True)
    return runs


def _format_run_label(meta: dict) -> str:
    ts   = meta.get("run_timestamp", "")
    proj = meta.get("project_name", "Unknown")
    try:
        dt     = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        ts_fmt = dt.strftime("%b-%d-%Y  %I:%M %p")
    except Exception:
        ts_fmt = ts
    return f"{proj}  —  {ts_fmt}"


def _write_run_meta(out_dir: str) -> None:
    meta = {
        "project_name":        st.session_state.project_name,
        "project_description": st.session_state.project_description,
        "run_timestamp":       datetime.utcnow().isoformat() + "Z",
        "src_folder":          st.session_state.src_folder,
        "dst_folder":          st.session_state.dst_folder,
    }
    with open(os.path.join(out_dir, "run_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _load_folder(folder: str, sep: str) -> tuple[pd.DataFrame | None, list[str], str | None]:
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
            if sorted(c.strip().lower() for c in df.columns) != ref_cols:
                return None, [], f'Schema mismatch: "{fname}" has different columns'
        dfs.append(df)
    return pd.concat(dfs, ignore_index=True), files, None


def _col_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.columns:
        series = df[col]
        nulls  = (series == "").sum() + series.isna().sum()
        rows.append({
            "Column":   col,
            "Nulls":    int(nulls),
            "Null %":   round(nulls / max(len(series), 1) * 100, 1),
            "Distinct": int(series.nunique()),
            "Sample":   ", ".join(str(v) for v in series.dropna().unique()[:3]),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Shared wizard UI helpers
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
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Rows",          f"{len(df):,}")
    m2.metric("Columns",       len(df.columns))
    total_null = int((df == "").sum().sum() + df.isna().sum().sum())
    m3.metric("Null Cells",    f"{total_null:,}")
    m4.metric("Distinct Rows", f"{df.drop_duplicates().shape[0]:,}")
    with st.expander("Column summary", expanded=False):
        st.dataframe(_col_summary(df), use_container_width=True, hide_index=True)
    n = st.slider(f"Preview rows — {label}", 5, min(200, len(df)), 10,
                  key=f"prev_slider_{label}")
    st.dataframe(df.head(n), use_container_width=True, hide_index=True)


def _sep_selectbox(label: str, current: str, key: str) -> str:
    reverse = {v: k for k, v in SEP_OPTIONS.items()}
    display = reverse.get(current, "Comma ( , )")
    chosen  = st.selectbox(label, list(SEP_OPTIONS.keys()),
                           index=list(SEP_OPTIONS.keys()).index(display), key=key)
    return SEP_OPTIONS[chosen]


# ---------------------------------------------------------------------------
# Rule builder widget
# ---------------------------------------------------------------------------
def _rule_builder(prefix: str) -> str:
    base   = st.selectbox("Matching rule", ALL_RULES, key=f"{prefix}_base")
    suffix = ""
    if base == "STRIP_PREFIX":
        p      = st.text_input("Prefix to strip (e.g. CUST, BR-, CD-)", key=f"{prefix}_pfx")
        suffix = f":{p}"
    elif base == "ZEROPAD":
        n      = st.number_input("Target length", min_value=1, max_value=64,
                                 value=10, step=1, key=f"{prefix}_zn")
        suffix = f":{int(n)}"
    elif base == "DATE_FORMAT":
        dc1, dc2 = st.columns(2)
        src_tok  = dc1.selectbox("Source format", DATE_TOKENS, key=f"{prefix}_df_src")
        dst_tok  = dc2.selectbox("Dest format",   DATE_TOKENS, index=1, key=f"{prefix}_df_dst")
        suffix   = f":{src_tok}->{dst_tok}"
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
# LANDING PAGE
# ---------------------------------------------------------------------------
def _page_home() -> None:
    st.markdown(
        "<div style='text-align:center;padding:32px 0 24px'>"
        "<span style='font-size:3rem'>🔍</span><br>"
        "<span style='font-size:2.2rem;font-weight:800;color:#818cf8'>DataPulse</span><br>"
        "<span style='font-size:1rem;color:#7a8eaf'>Data Validation Platform</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    runs   = _get_run_history()
    latest = runs[0] if runs else None

    c1, c2 = st.columns(2, gap="large")

    # ── Card 1: last run ─────────────────────────────────────────────────
    with c1:
        if latest:
            ts   = latest.get("run_timestamp", "")
            try:
                dt     = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts_fmt = dt.strftime("%b-%d-%Y  %I:%M %p")
            except Exception:
                ts_fmt = ts

            proj_name   = latest.get("project_name", "Unknown Project")
            proj_desc   = latest.get("project_description", "")
            pass_rate   = "—"
            total_iss   = "—"
            src_rows    = "—"
            dst_rows    = "—"
            res_path    = os.path.join(latest["output_dir"], "datapulse_results.json")
            if os.path.exists(res_path):
                try:
                    with open(res_path, "r", encoding="utf-8") as f:
                        _r = json.load(f)["summary"]
                    pass_rate = f"{_r.get('pass_rate', 0):.1f}%"
                    total_iss = f"{_r.get('total_issues', 0):,}"
                    src_rows  = f"{_r.get('source_row_count', 0):,}"
                    dst_rows  = f"{_r.get('dest_row_count', 0):,}"
                except Exception:
                    pass

            pr_colour = "#4ade80" if pass_rate not in ("—", "0.0%") else "#f87171"
            st.markdown(
                f"""<div style='border:2px solid #6366f1;border-radius:14px;padding:24px;
                background:#1e1b4b;height:260px;box-sizing:border-box'>
                <div style='font-size:.75rem;letter-spacing:.08em;color:#a5b4fc;margin-bottom:4px'>
                LAST DATAPULSE CHECK</div>
                <div style='font-size:1.3rem;font-weight:700;color:#e2e8f0'>{proj_name}</div>
                <div style='font-size:.9rem;color:#818cf8;margin-bottom:14px'>{proj_desc}</div>
                <div style='font-size:.85rem;color:#a5b4fc;margin-bottom:14px'>🕐 {ts_fmt}</div>
                <div style='display:flex;gap:28px'>
                  <div><div style='font-size:1.5rem;font-weight:800;color:{pr_colour}'>{pass_rate}</div>
                       <div style='font-size:.72rem;color:#7a8eaf'>Pass Rate</div></div>
                  <div><div style='font-size:1.5rem;font-weight:800;color:#f87171'>{total_iss}</div>
                       <div style='font-size:.72rem;color:#7a8eaf'>Issues</div></div>
                  <div><div style='font-size:1.5rem;font-weight:800;color:#94a3b8'>{src_rows}</div>
                       <div style='font-size:.72rem;color:#7a8eaf'>Src Rows</div></div>
                  <div><div style='font-size:1.5rem;font-weight:800;color:#94a3b8'>{dst_rows}</div>
                       <div style='font-size:.72rem;color:#7a8eaf'>Dst Rows</div></div>
                </div>
                </div>""",
                unsafe_allow_html=True,
            )
            st.write("")
            if st.button(f"📊  View Last Check  —  {ts_fmt}",
                         use_container_width=True, type="primary", key="btn_view_last"):
                st.session_state.active_output_dir   = latest["output_dir"]
                st.session_state.project_name        = latest.get("project_name", "")
                st.session_state.project_description = latest.get("project_description", "")
                st.session_state.page = "dashboard"
                st.rerun()
        else:
            st.markdown(
                """<div style='border:1px solid #334155;border-radius:14px;padding:24px;
                background:#0f172a;height:260px;box-sizing:border-box;display:flex;
                align-items:center;justify-content:center;text-align:center'>
                <div>
                <div style='font-size:2.5rem;margin-bottom:8px'>📭</div>
                <div style='color:#475569;font-size:.95rem'>No previous runs found</div>
                <div style='color:#334155;font-size:.82rem;margin-top:4px'>
                Run your first validation to see results here</div>
                </div></div>""",
                unsafe_allow_html=True,
            )

    # ── Card 2: new check ─────────────────────────────────────────────────
    with c2:
        st.markdown(
            """<div style='border:2px solid #16a34a;border-radius:14px;padding:24px;
            background:#052e16;height:260px;box-sizing:border-box'>
            <div style='font-size:.75rem;letter-spacing:.08em;color:#4ade80;margin-bottom:4px'>
            NEW VALIDATION</div>
            <div style='font-size:1.3rem;font-weight:700;color:#e2e8f0;margin-bottom:8px'>
            Check DataPulse of New Data</div>
            <div style='font-size:.9rem;color:#86efac;line-height:1.55'>
            Select or create a project, load source &amp; destination data,
            build or reuse column mappings, run validation,
            and explore results in the interactive dashboard.
            </div>
            </div>""",
            unsafe_allow_html=True,
        )
        st.write("")
        if st.button("▶  Check DataPulse of New Data",
                     use_container_width=True, type="primary", key="btn_new_check"):
            st.session_state.page = "project_setup"
            st.rerun()

    # ── Run history ──────────────────────────────────────────────────────
    if len(runs) > 1:
        st.divider()
        st.subheader("Run History")
        labels = [_format_run_label(r) for r in runs]
        sel = st.selectbox("Select a previous run", labels, key="hist_sel")
        if st.button("Open Selected Run →", key="btn_hist_open"):
            idx    = labels.index(sel)
            chosen = runs[idx]
            st.session_state.active_output_dir   = chosen["output_dir"]
            st.session_state.project_name        = chosen.get("project_name", "")
            st.session_state.project_description = chosen.get("project_description", "")
            st.session_state.page = "dashboard"
            st.rerun()


# ---------------------------------------------------------------------------
# PROJECT SETUP PAGE
# ---------------------------------------------------------------------------
def _page_project_setup() -> None:
    st.header("Project Setup")
    st.caption("Name your project so each validation run is easy to identify later.")

    projects   = _load_projects()
    proj_names = [p["name"] for p in projects]

    mode = st.radio("", ["Create new project", "Continue existing project"],
                    horizontal=True, key="proj_mode")

    name        = ""
    desc        = ""
    src_folder  = ""
    dst_folder  = ""
    mapping_dir = ""
    proj        = {}

    if mode == "Continue existing project" and proj_names:
        sel_name = st.selectbox("Select project", proj_names, key="proj_sel")
        proj     = next(p for p in projects if p["name"] == sel_name)
        name     = proj["name"]
        desc     = proj.get("description", "")
        src_folder  = proj.get("src_folder", "")
        dst_folder  = proj.get("dst_folder", "")
        mapping_dir = proj.get("mapping_dir", "")

        st.info(desc or "No description saved for this project.")
        cc1, cc2, cc3 = st.columns(3)
        cc1.caption(f"**Source:** `{src_folder}`")
        cc2.caption(f"**Dest:** `{dst_folder}`")
        cc3.caption(f"**Mapping:** `{mapping_dir}`")

    elif mode == "Continue existing project" and not proj_names:
        st.info("No saved projects yet — fill in the form below to create one.")
        mode = "Create new project"

    if mode == "Create new project":
        name        = st.text_input("Project name *",
                                    placeholder="e.g. CD Portfolio Migration Q1 2026",
                                    key="proj_name_input")
        desc        = st.text_area("Description (optional)",
                                   placeholder="Brief description of what this validation covers…",
                                   key="proj_desc_input", height=80)
        src_folder  = st.text_input("Source folder path",
                                    placeholder="e.g. Data_Folder_Template/Source",
                                    key="proj_src")
        dst_folder  = st.text_input("Destination folder path",
                                    placeholder="e.g. Data_Folder_Template/Destination",
                                    key="proj_dst")
        mapping_dir = st.text_input("Mapping_Rule folder",
                                    placeholder="e.g. Data_Folder_Template/Mapping_Rule",
                                    key="proj_map")
    else:
        desc = proj.get("description", "") if proj else desc

    st.divider()
    cb1, _, cb2 = st.columns([2, 6, 2])
    if cb1.button("← Back to Home", key="proj_back"):
        st.session_state.page = "home"
        st.rerun()
    if cb2.button("Continue →", type="primary", use_container_width=True,
                  key="proj_continue", disabled=not name.strip()):
        proj_data = {
            "name":        name.strip(),
            "description": desc.strip() if isinstance(desc, str) else "",
            "src_folder":  src_folder.strip(),
            "dst_folder":  dst_folder.strip(),
            "mapping_dir": mapping_dir.strip(),
            "created":     datetime.now().isoformat(),
        }
        _save_project(proj_data)
        st.session_state.project_name        = proj_data["name"]
        st.session_state.project_description = proj_data["description"]
        if src_folder.strip():
            st.session_state.src_folder = src_folder.strip()
        if dst_folder.strip():
            st.session_state.dst_folder = dst_folder.strip()
        if mapping_dir.strip():
            colmap = os.path.join(mapping_dir.strip(), "column_mapping.csv")
            valmap = os.path.join(mapping_dir.strip(), "value_mapping.csv")
            if os.path.exists(colmap):
                st.session_state.colmap_path = colmap
            if os.path.exists(valmap):
                st.session_state.valmap_path = valmap
        st.session_state.page = "wizard"
        st.session_state.step = 1
        st.rerun()


# ---------------------------------------------------------------------------
# NATIVE STREAMLIT DASHBOARD
# ---------------------------------------------------------------------------
def _dash_load(out_dir: str) -> tuple[dict, dict, dict]:
    """Load results, profiling, mappings JSON from out_dir. Returns (results, profiling, mappings)."""
    def _read(name):
        p = os.path.join(out_dir, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    return _read("datapulse_results.json"), _read("profiling.json"), _read("mappings.json")


def _dash_summary_tab(summary: dict, detail_rows: list[dict]) -> None:
    by_type   = summary.get("mismatch_count_by_type", {})
    by_col    = summary.get("mismatch_count_by_column", {})
    pass_rate = summary.get("pass_rate", 0)
    pr_delta  = f"{'✓ All clear' if pass_rate == 100 else f'{100 - pass_rate:.1f}% with issues'}"

    # KPIs
    k = st.columns(8)
    k[0].metric("Pass Rate",         f"{pass_rate:.1f}%", pr_delta)
    k[1].metric("Source Rows",       f"{summary.get('source_row_count', 0):,}")
    k[2].metric("Dest Rows",         f"{summary.get('dest_row_count', 0):,}")
    k[3].metric("Matched Keys",      f"{summary.get('matched_key_count', 0):,}")
    k[4].metric("Total Issues",      f"{summary.get('total_issues', 0):,}")
    k[5].metric("Missing in Dest",   f"{by_type.get('KEY_MISSING_IN_DEST', 0):,}")
    k[6].metric("Value Mismatches",  f"{by_type.get('VALUE_MISMATCH', 0):,}")
    k[7].metric("Migration Mismatch",f"{summary.get('migration_key_count', 0):,}")

    st.divider()

    # Run info  |  Charts
    ri_col, c1_col, c2_col = st.columns([2, 3, 3])

    with ri_col:
        st.markdown("**Run Information**")
        ts = summary.get("run_timestamp", "")
        try:
            dt     = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_fmt = dt.strftime("%Y-%m-%d  %H:%M:%S UTC")
        except Exception:
            ts_fmt = ts
        info_rows = [
            ("Project",     st.session_state.project_name or "—"),
            ("Timestamp",   ts_fmt),
            ("Match Mode",  summary.get("match_mode", "—").upper()),
            ("Tolerance",   str(summary.get("tolerance", "—"))),
            ("Source File(s)", ", ".join(summary.get("source_files", []))),
            ("Dest File(s)",   ", ".join(summary.get("dest_files", []))),
            ("Key Columns (Src)", ", ".join(summary.get("composite_key_columns_source", []))),
            ("Key Columns (Dst)", ", ".join(summary.get("composite_key_columns_dest", []))),
        ]
        for lbl, val in info_rows:
            st.markdown(
                f"<div style='display:flex;border-bottom:1px solid #1e293b;padding:4px 0'>"
                f"<span style='color:#7a8eaf;min-width:140px;font-size:.82rem'>{lbl}</span>"
                f"<span style='color:#e2e8f0;font-size:.82rem'>{val}</span></div>",
                unsafe_allow_html=True,
            )

    with c1_col:
        st.markdown("**Issues by Type**")
        if by_type:
            df_bt = pd.DataFrame({"Count": by_type}).sort_values("Count", ascending=False)
            st.bar_chart(df_bt, use_container_width=True, height=220)
        else:
            st.success("No issues found.")

    with c2_col:
        st.markdown("**Value Mismatches by Column**")
        if by_col:
            df_bc = pd.DataFrame({"Mismatches": by_col}).sort_values("Mismatches", ascending=False)
            st.bar_chart(df_bc, use_container_width=True, height=220)
        else:
            st.info("No value mismatches.")


def _dash_issues_tab(detail_rows: list[dict]) -> None:
    if not detail_rows:
        st.success("🎉  No issues found — 100% pass rate!")
        return

    df = pd.DataFrame(detail_rows)
    st.caption(f"{len(df):,} total issues")

    f1, f2, f3 = st.columns([3, 2, 3])
    types    = ["All"] + sorted(df["issue_type"].unique().tolist())
    sevs     = ["All"] + sorted(df["severity"].unique().tolist())
    sel_type = f1.selectbox("Issue type", types, key="d_type")
    sel_sev  = f2.selectbox("Severity",   sevs,  key="d_sev")
    search   = f3.text_input("Search composite key", placeholder="partial key value…",
                              key="d_search")

    mask = pd.Series([True] * len(df), index=df.index)
    if sel_type != "All":
        mask &= df["issue_type"] == sel_type
    if sel_sev != "All":
        mask &= df["severity"] == sel_sev
    if search:
        mask &= df["composite_key"].astype(str).str.contains(search, na=False, case=False)

    filtered = df[mask]
    st.dataframe(filtered, use_container_width=True, hide_index=True, height=500)

    buf = io.StringIO()
    filtered.to_csv(buf, index=False)
    st.download_button("⬇️  Download filtered issues (CSV)",
                       data=buf.getvalue(), file_name="issues_filtered.csv", mime="text/csv")


def _dash_profiling_tab(profiling: dict) -> None:
    if not profiling:
        st.info("Profiling data not available.")
        return

    _PROFILE_COLS = [
        "column", "inferred_data_type", "row_count", "null_count", "null_percent",
        "distinct_count", "distinct_percent",
    ]

    def _render_profiles(profiles: list[dict], label: str) -> None:
        if not profiles:
            st.info(f"No profiling data for {label}.")
            return
        rows = []
        for p in profiles:
            row = {c: p.get(c, "") for c in _PROFILE_COLS}
            row["top_values"] = ", ".join(
                f"{v['value']}({v['count']})" for v in p.get("top_values", [])[:3]
            )
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    sub = st.tabs(["Source", "Destination", "Matched (Src)", "Matched (Dst)", "Value Counts"])

    with sub[0]:
        _render_profiles(profiling.get("source", {}).get("column_profiles", []), "Source")
    with sub[1]:
        _render_profiles(profiling.get("destination", {}).get("column_profiles", []), "Destination")
    with sub[2]:
        _render_profiles(profiling.get("matched_source", {}).get("column_profiles", []), "Matched Source")
    with sub[3]:
        _render_profiles(profiling.get("matched_dest", {}).get("column_profiles", []), "Matched Dest")
    with sub[4]:
        vc = profiling.get("column_value_profiles", [])
        if not vc:
            st.info("No value count profiles available.")
        else:
            col_names = [c.get("column", str(i)) for i, c in enumerate(vc)]
            sel_col   = st.selectbox("Column", col_names, key="prof_vc_col")
            idx       = col_names.index(sel_col)
            entry     = vc[idx]
            sv, dv    = st.columns(2)
            with sv:
                st.caption(f"**Source — {sel_col}** (after transform)")
                src_vc = entry.get("source_value_counts", {})
                if src_vc:
                    df_sv = pd.DataFrame(src_vc.items(), columns=["Value", "Count"]).sort_values(
                        "Count", ascending=False
                    )
                    st.dataframe(df_sv, use_container_width=True, hide_index=True, height=300)
            with dv:
                st.caption(f"**Destination — {entry.get('destination_column', sel_col)}**")
                dst_vc = entry.get("destination_value_counts", {})
                if dst_vc:
                    df_dv = pd.DataFrame(dst_vc.items(), columns=["Value", "Count"]).sort_values(
                        "Count", ascending=False
                    )
                    st.dataframe(df_dv, use_container_width=True, hide_index=True, height=300)


def _dash_mappings_tab(mappings: dict) -> None:
    if not mappings:
        st.info("Mappings data not available.")
        return

    st.subheader("Column Mapping")
    cm = mappings.get("column_mapping", [])
    if cm:
        st.dataframe(pd.DataFrame(cm), use_container_width=True, hide_index=True)

    st.subheader("Value Mapping")
    vm = mappings.get("value_mapping", {})
    if vm:
        rows = []
        for col_name, entries in vm.items():
            for src_val, dst_val in entries.items():
                rows.append({"column": col_name, "source_value": src_val, "dest_value": dst_val})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No value mappings.")


def _dash_viewdata_tab(out_dir: str, summary: dict, detail_rows: list[dict]) -> None:
    meta_path = os.path.join(out_dir, "run_meta.json")
    if not os.path.exists(meta_path):
        st.info("run_meta.json not found — cannot locate source/destination files.")
        return
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    src_folder = meta.get("src_folder", "")
    dst_folder = meta.get("dst_folder", "")

    side = st.radio("View", ["Source", "Destination"], horizontal=True, key="vd_side")
    folder = src_folder if side == "Source" else dst_folder

    if not folder or not os.path.isdir(folder):
        st.warning(f"Folder not found: `{folder}`")
        return

    with st.spinner("Loading data…"):
        df, files, err = _load_folder(folder, ",")
    if err:
        # try pipe
        df, files, err2 = _load_folder(folder, "|")
        if err2:
            st.error(err)
            return

    if df is None:
        st.error("Could not load data.")
        return

    # Build set of keys with issues for row colouring
    val_mismatch_keys  = set()
    missing_dest_keys  = set()
    missing_src_keys   = set()
    migration_src_keys = set()
    migration_dst_keys = set()

    for row in detail_rows:
        ck  = str(row.get("composite_key", ""))
        it  = row.get("issue_type", "")
        s   = row.get("side", side)
        if it == "VALUE_MISMATCH":
            val_mismatch_keys.add(ck)
        elif it == "KEY_MISSING_IN_DEST" and side == "Source":
            missing_dest_keys.add(ck)
        elif it == "KEY_MISSING_IN_SOURCE" and side == "Destination":
            missing_src_keys.add(ck)
        elif it == "MIGRATION_KEY_MISMATCH":
            if side == "Source":
                migration_src_keys.add(ck)
            else:
                migration_dst_keys.add(ck)

    key_src_cols = summary.get("composite_key_columns_source", [])
    key_dst_cols = summary.get("composite_key_columns_dest",   [])
    key_cols     = key_src_cols if side == "Source" else key_dst_cols

    # Reorder: key columns first
    ordered_cols = [c for c in key_cols if c in df.columns] + \
                   [c for c in df.columns  if c not in key_cols]
    df = df[ordered_cols]

    # Build composite key per row for highlight matching
    def _row_key(r):
        return "|".join(str(r.get(c, "")) for c in key_cols if c in r)

    def _highlight(row):
        ck = _row_key(row)
        if ck in val_mismatch_keys:
            return ["background-color: rgba(239,68,68,0.22)"] * len(row)
        if ck in missing_dest_keys or ck in migration_src_keys:
            return ["background-color: rgba(34,197,94,0.22)"] * len(row)
        if ck in missing_src_keys or ck in migration_dst_keys:
            return ["background-color: rgba(56,189,248,0.22)"] * len(row)
        return [""] * len(row)

    # Legend
    st.markdown(
        "<div style='display:flex;gap:16px;margin-bottom:8px;font-size:.8rem'>"
        "<span style='background:rgba(239,68,68,0.22);padding:2px 8px;border-radius:4px'>"
        "■ Value mismatch</span>"
        "<span style='background:rgba(34,197,94,0.22);padding:2px 8px;border-radius:4px'>"
        "■ Missing in dest / migration src</span>"
        "<span style='background:rgba(56,189,248,0.22);padding:2px 8px;border-radius:4px'>"
        "■ Missing in source / migration dst</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    rpp = st.selectbox("Rows per page", [25, 50, 100, 250, 500], key="vd_rpp")
    total_pages = max(1, (len(df) - 1) // rpp + 1)
    page_num    = st.number_input("Page", min_value=1, max_value=total_pages,
                                  value=1, key="vd_page")

    start = (page_num - 1) * rpp
    chunk = df.iloc[start: start + rpp]

    if key_cols and all(c in chunk.columns for c in key_cols):
        styled = chunk.style.apply(_highlight, axis=1)
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.dataframe(chunk, use_container_width=True, hide_index=True)

    st.caption(f"Showing rows {start+1}–{min(start+rpp, len(df))} of {len(df):,}  "
               f"({total_pages} page{'s' if total_pages > 1 else ''})")


def _page_dashboard() -> None:
    out_dir = st.session_state.active_output_dir
    if not out_dir or not os.path.isdir(out_dir):
        st.error(f"Output folder not found: `{out_dir}`")
        if st.button("← Back to Home"):
            st.session_state.page = "home"
            st.rerun()
        return

    results, profiling, mappings = _dash_load(out_dir)
    summary     = results.get("summary", {})
    detail_rows = results.get("detail_rows", [])

    # Header bar
    proj = st.session_state.project_name or "DataPulse"
    ts   = summary.get("run_timestamp", "")
    try:
        ts_fmt = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%b-%d-%Y  %I:%M %p")
    except Exception:
        ts_fmt = ts
    mode_badge = summary.get("match_mode", "").upper()
    src_n  = summary.get("source_row_count", 0)
    dst_n  = summary.get("dest_row_count", 0)

    st.markdown(
        f"<div style='display:flex;align-items:center;justify-content:space-between;"
        f"background:#0f172a;border:1px solid #1e293b;border-radius:10px;"
        f"padding:10px 18px;margin-bottom:12px'>"
        f"<div><span style='font-size:1.15rem;font-weight:700;color:#818cf8'>🔍 {proj}</span>"
        f"<span style='color:#475569;font-size:.85rem;margin-left:12px'>{ts_fmt}</span></div>"
        f"<div style='display:flex;gap:10px'>"
        f"<span style='background:#1e293b;border-radius:6px;padding:3px 10px;"
        f"font-size:.8rem;color:#94a3b8'>Source [{src_n:,}]</span>"
        f"<span style='background:#1e293b;border-radius:6px;padding:3px 10px;"
        f"font-size:.8rem;color:#94a3b8'>Destination [{dst_n:,}]</span>"
        f"<span style='background:#1e293b;border-radius:6px;padding:3px 10px;"
        f"font-size:.8rem;color:#a5b4fc'>Mode {mode_badge}</span>"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    # Download HTML
    html_path = os.path.join(out_dir, "dashboard.html")
    if os.path.exists(html_path):
        with open(html_path, "rb") as fh:
            st.download_button("⬇️  Download Full Dashboard (HTML)", data=fh.read(),
                               file_name="dashboard.html", mime="text/html")

    tabs = st.tabs(["📊 Summary", "⚠️ Issues", "📈 Profiling", "📋 Mappings", "🗂️ View Data"])

    with tabs[0]:
        _dash_summary_tab(summary, detail_rows)
    with tabs[1]:
        _dash_issues_tab(detail_rows)
    with tabs[2]:
        _dash_profiling_tab(profiling)
    with tabs[3]:
        _dash_mappings_tab(mappings)
    with tabs[4]:
        _dash_viewdata_tab(out_dir, summary, detail_rows)


# ---------------------------------------------------------------------------
# Step 1 — Data Ingestion
# ---------------------------------------------------------------------------
def _step1() -> None:
    st.header("Step 1 — Data Ingestion & Preview")

    if st.session_state.project_name:
        st.caption(f"Project: **{st.session_state.project_name}**"
                   + (f"  —  {st.session_state.project_description}"
                      if st.session_state.project_description else ""))

    left, right = st.columns(2)

    for side, col_widget, folder_key, sep_key, df_key, label in [
        ("Source",      left,  "src_folder", "src_sep", "src_df", "Source"),
        ("Destination", right, "dst_folder", "dst_sep", "dst_df", "Destination"),
    ]:
        with col_widget:
            st.subheader(f"{side} Data")
            folder = st.text_input(
                f"{side} folder path",
                value=st.session_state[folder_key],
                key=f"{folder_key}_input",
                placeholder="e.g. Data_Folder_Template/Source",
            )
            sep = _sep_selectbox("File separator", st.session_state[sep_key],
                                 key=f"{sep_key}_sel")

            btn_col, reload_col = st.columns([3, 1])
            load_clicked   = btn_col.button(f"Load {side}", use_container_width=True,
                                            key=f"load_{side}")
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

    if st.session_state.col_mapping:
        st.subheader(f"Column Mappings  ({len(st.session_state.col_mapping)})")
        cm_df  = pd.DataFrame(st.session_state.col_mapping)
        edited = st.data_editor(
            cm_df,
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            key="cm_editor",
            column_config={
                "seq":           st.column_config.NumberColumn("Seq", width="small"),
                "is_key":        st.column_config.SelectboxColumn("Key?", options=["true", "false"]),
                "matching_rule": st.column_config.TextColumn("Rule", width="large"),
            },
        )
        sc1, _ = st.columns([2, 8])
        if sc1.button("Save edits", key="cm_save"):
            st.session_state.col_mapping = edited.dropna(
                subset=["source_column", "destination_column"]
            ).to_dict("records")
            st.success("Mappings updated.")

        with st.expander("🔍  Column value preview (select a pair)"):
            pairs   = [f"{m['source_column']}  →  {m['destination_column']}"
                       for m in st.session_state.col_mapping]
            sel_pair = st.selectbox("Pair", pairs, key="cm_pair_sel")
            if sel_pair:
                idx  = pairs.index(sel_pair)
                sc   = st.session_state.col_mapping[idx]["source_column"]
                dc   = st.session_state.col_mapping[idx]["destination_column"]
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
# Step 3 — Value Mapping
# ---------------------------------------------------------------------------
def _step3() -> None:
    st.header("Step 3 — Column Value Mapping  (Rule Creation)")

    vm_cols = [m for m in st.session_state.col_mapping
               if "VALUE_MAP" in m["matching_rule"].upper()]

    if not vm_cols:
        st.info("No columns use VALUE_MAP.  Nothing to configure here — proceed to next step.")
        _nav(prev_step=2, next_step=4)
        return

    for mapping in vm_cols:
        src_col = mapping["source_column"]
        dst_col = mapping["destination_column"]
        st.subheader(f"{src_col}  →  {dst_col}")

        with st.expander("Distinct value reference", expanded=False):
            rv1, rv2 = st.columns(2)
            with rv1:
                st.caption(f"Source  —  {src_col}")
                st.dataframe(
                    st.session_state.src_df[src_col].value_counts().reset_index()
                    .rename(columns={src_col: "Value", "count": "Count"}),
                    use_container_width=True, hide_index=True, height=220,
                )
            with rv2:
                st.caption(f"Destination  —  {dst_col}")
                st.dataframe(
                    st.session_state.dst_df[dst_col].value_counts().reset_index()
                    .rename(columns={dst_col: "Value", "count": "Count"}),
                    use_container_width=True, hide_index=True, height=220,
                )

        col_rules = [r for r in st.session_state.val_mapping
                     if r["column_name"] == src_col]

        if st.button(f"Auto-generate rows from source distinct values  ({src_col})",
                     key=f"autogen_{src_col}"):
            existing = {r["source_value"] for r in col_rules}
            new_rows = [
                {"column_name": src_col, "source_value": v,
                 "destination_value": "", "rule_description": ""}
                for v in sorted(
                    str(v) for v in st.session_state.src_df[src_col].dropna().unique()
                    if str(v) not in existing
                )
            ]
            st.session_state.val_mapping = (
                [r for r in st.session_state.val_mapping if r["column_name"] != src_col]
                + col_rules + new_rows
            )
            st.rerun()

        dest_vals = sorted(str(v) for v in st.session_state.dst_df[dst_col].dropna().unique())
        rules_df  = pd.DataFrame(
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
                ),
                "rule_description":  st.column_config.TextColumn("Description (optional)"),
            },
        )
        bc1, _ = st.columns([2, 8])
        if bc1.button(f"Save rules  ({src_col})", key=f"vm_save_{src_col}", type="primary"):
            clean = edited.dropna(subset=["source_value"])
            clean = clean[clean["source_value"].str.strip() != ""]
            if clean["source_value"].duplicated().sum():
                st.error("Duplicate source values found — remove them before saving.")
            else:
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
# Step 4 — Review & Save
# ---------------------------------------------------------------------------
def _step4() -> None:
    st.header("Step 4 — Review & Save Mapping Files")

    st.subheader("Column Mapping")
    cm_df = pd.DataFrame(st.session_state.col_mapping)[
        ["seq", "source_column", "destination_column", "matching_rule", "is_key"]
    ] if st.session_state.col_mapping else pd.DataFrame(
        columns=["seq", "source_column", "destination_column", "matching_rule", "is_key"]
    )
    st.dataframe(cm_df, use_container_width=True, hide_index=True)

    st.subheader("Value Mapping")
    if st.session_state.val_mapping:
        st.dataframe(
            pd.DataFrame(st.session_state.val_mapping)[
                ["column_name", "source_value", "destination_value", "rule_description"]
            ],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No value mapping rules defined.")

    st.divider()
    st.subheader("Save Location")

    src_parent  = str(Path(st.session_state.src_folder).parent) \
        if st.session_state.src_folder else ""
    base_folder = st.text_input(
        "Base data folder (Mapping_Rule/ sub-folder created here)",
        value=src_parent, key="save_base_folder",
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
                cm_df[["source_column", "destination_column", "matching_rule", "is_key"]].to_csv(
                    save_path, index=False
                )
                st.session_state.colmap_path = save_path
                st.success(f"Saved → `{save_path}`")
            except Exception as exc:
                st.error(f"Save failed: {exc}")
    with sc2:
        if st.button("💾  Save value_mapping.csv", use_container_width=True, type="primary"):
            try:
                os.makedirs(mapping_dir, exist_ok=True)
                save_path = os.path.join(mapping_dir, "value_mapping.csv")
                vm_out = pd.DataFrame(st.session_state.val_mapping)[
                    ["column_name", "source_value", "destination_value", "rule_description"]
                ] if st.session_state.val_mapping else pd.DataFrame(
                    columns=["column_name", "source_value", "destination_value", "rule_description"]
                )
                vm_out.to_csv(save_path, index=False)
                st.session_state.valmap_path = save_path
                st.success(f"Saved → `{save_path}`")
            except Exception as exc:
                st.error(f"Save failed: {exc}")

    st.divider()
    st.subheader("Download (without saving to disk)")
    dl1, dl2 = st.columns(2)

    cm_buf = io.StringIO()
    cm_df[["source_column", "destination_column", "matching_rule", "is_key"]].to_csv(
        cm_buf, index=False
    )
    dl1.download_button("⬇️  column_mapping.csv", data=cm_buf.getvalue(),
                        file_name="column_mapping.csv", mime="text/csv",
                        use_container_width=True)
    if st.session_state.val_mapping:
        vm_buf = io.StringIO()
        pd.DataFrame(st.session_state.val_mapping)[
            ["column_name", "source_value", "destination_value", "rule_description"]
        ].to_csv(vm_buf, index=False)
        dl2.download_button("⬇️  value_mapping.csv", data=vm_buf.getvalue(),
                            file_name="value_mapping.csv", mime="text/csv",
                            use_container_width=True)

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

        src    = r1c1.text_input("Source folder",      value=st.session_state.src_folder,  key="run_src")
        dst    = r1c1.text_input("Destination folder", value=st.session_state.dst_folder,  key="run_dst")
        cmap   = r1c1.text_input("column_mapping.csv", value=st.session_state.colmap_path, key="run_cmap")
        vmap   = r1c1.text_input("value_mapping.csv",  value=st.session_state.valmap_path, key="run_vmap")

        out_base   = r1c2.text_input("Output base folder",
                                     value=st.session_state.get("output_base", OUTPUT_BASE),
                                     key="run_out_base",
                                     help="Runs are saved as timestamped sub-folders inside here")
        match_mode = r1c2.selectbox("Match mode", MATCH_MODES,
                                    index=MATCH_MODES.index(st.session_state.match_mode),
                                    key="run_mode_sel")
        tolerance  = r1c2.number_input("Tolerance", value=st.session_state.tolerance,
                                       min_value=0.0, format="%.4f", key="run_tol")
        run_mode   = r1c2.selectbox("Run mode", ["PROD", "TEST"], key="run_mode_flag")

        st.write("**CSV Separators**")
        src_sep  = _sep_selectbox("Source",      st.session_state.src_sep,    "run_src_sep")
        dst_sep  = _sep_selectbox("Destination", st.session_state.dst_sep,    "run_dst_sep")
        cmap_sep = _sep_selectbox("ColMap",      st.session_state.colmap_sep, "run_cm_sep")
        vmap_sep = _sep_selectbox("ValMap",      st.session_state.valmap_sep, "run_vm_sep")

    if st.button("▶  Run Validation", type="primary", use_container_width=True):
        # Build timestamped output dir
        slug    = "".join(c if c.isalnum() else "_" for c in
                          st.session_state.project_name)[:30].strip("_") or "run"
        ts_str  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(out_base.strip() or OUTPUT_BASE, f"{slug}_{ts_str}")
        os.makedirs(out_dir, exist_ok=True)

        # Persist config
        for k, v in [("src_folder", src), ("dst_folder", dst),
                     ("colmap_path", cmap), ("valmap_path", vmap),
                     ("match_mode", match_mode), ("tolerance", tolerance),
                     ("run_mode", run_mode), ("src_sep", src_sep),
                     ("dst_sep", dst_sep), ("colmap_sep", cmap_sep),
                     ("valmap_sep", vmap_sep)]:
            st.session_state[k] = v
        st.session_state["output_base"]        = out_base.strip() or OUTPUT_BASE
        st.session_state["active_output_dir"]  = out_dir

        log_buf = io.StringIO()
        with st.spinner("Running validation engine…"):
            try:
                with redirect_stdout(log_buf):
                    results = validate(
                        source_folder = src,
                        dest_folder   = dst,
                        colmap_path   = cmap,
                        valmap_path   = vmap,
                        outdir        = out_dir,
                        tolerance     = tolerance,
                        match_mode    = match_mode,
                        source_sep    = src_sep,
                        dest_sep      = dst_sep,
                        colmap_sep    = cmap_sep,
                        valmap_sep    = vmap_sep,
                    )
                _write_run_meta(out_dir)
                st.session_state.run_results = results
                st.session_state.run_error   = None
                st.success(f"Validation complete!  Results saved to `{out_dir}`")
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
# Step 6 — Results (summary + link to dashboard)
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

    results     = st.session_state.run_results
    summary     = results.get("summary", {})
    detail_rows = results.get("detail_rows", [])
    by_type     = summary.get("mismatch_count_by_type", {})

    k = st.columns(7)
    k[0].metric("Pass Rate",         f"{summary.get('pass_rate', 0):.1f}%")
    k[1].metric("Source Rows",       f"{summary.get('source_row_count', 0):,}")
    k[2].metric("Dest Rows",         f"{summary.get('dest_row_count', 0):,}")
    k[3].metric("Matched Keys",      f"{summary.get('matched_key_count', 0):,}")
    k[4].metric("Total Issues",      f"{summary.get('total_issues', 0):,}")
    k[5].metric("Missing in Dest",   f"{by_type.get('KEY_MISSING_IN_DEST', 0):,}")
    k[6].metric("Value Mismatches",  f"{by_type.get('VALUE_MISMATCH', 0):,}")

    st.divider()

    # Open full dashboard button
    out_dir = st.session_state.active_output_dir
    if out_dir:
        st.info(f"Results saved to: `{out_dir}`")
        if st.button("📊  Open Full Dashboard", type="primary", use_container_width=True,
                     key="open_dash_btn"):
            st.session_state.page = "dashboard"
            st.rerun()

    st.divider()

    if detail_rows:
        df = pd.DataFrame(detail_rows)
        st.subheader(f"Issues  ({len(df):,})")
        f1, f2, f3 = st.columns([3, 2, 3])
        types    = ["All"] + sorted(df["issue_type"].unique().tolist())
        sevs     = ["All"] + sorted(df["severity"].unique().tolist())
        sel_type = f1.selectbox("Issue type", types, key="res_type")
        sel_sev  = f2.selectbox("Severity",   sevs,  key="res_sev")
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
        filt_buf = io.StringIO()
        df[mask].to_csv(filt_buf, index=False)
        st.download_button("⬇️  Download filtered issues (CSV)",
                           data=filt_buf.getvalue(), file_name="issues_filtered.csv",
                           mime="text/csv")
    else:
        st.success("🎉  No issues found — 100% pass rate!")

    # HTML download
    if out_dir:
        dash_path = os.path.join(out_dir, "dashboard.html")
        if os.path.exists(dash_path):
            with open(dash_path, "rb") as fh:
                st.download_button("⬇️  Download full dashboard (HTML)",
                                   data=fh.read(), file_name="dashboard.html",
                                   mime="text/html", use_container_width=True)

    _nav(prev_step=5, next_step=None)


# ---------------------------------------------------------------------------
# Sidebars
# ---------------------------------------------------------------------------
def _render_sidebar_home() -> None:
    with st.sidebar:
        st.markdown(
            "<div style='text-align:center;padding:10px 0 4px'>"
            "<span style='font-size:2rem'>🔍</span><br>"
            "<span style='font-size:1.1rem;font-weight:800;color:#818cf8'>DataPulse</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.divider()

        runs = _get_run_history()
        if runs:
            st.markdown("**Recent Runs**")
            for r in runs[:5]:
                ts  = r.get("run_timestamp", "")
                try:
                    dt     = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts_lbl = dt.strftime("%b %d  %I:%M %p")
                except Exception:
                    ts_lbl = ts[:16]
                proj = r.get("project_name", "—")
                lbl  = f"{proj[:18]}…" if len(proj) > 18 else proj
                if st.button(f"📁 {lbl}\n{ts_lbl}", key=f"sb_run_{r['output_dir']}",
                             use_container_width=True):
                    st.session_state.active_output_dir   = r["output_dir"]
                    st.session_state.project_name        = r.get("project_name", "")
                    st.session_state.project_description = r.get("project_description", "")
                    st.session_state.page = "dashboard"
                    st.rerun()


def _render_sidebar_dashboard() -> None:
    with st.sidebar:
        st.markdown(
            "<div style='text-align:center;padding:10px 0 4px'>"
            "<span style='font-size:2rem'>🔍</span><br>"
            "<span style='font-size:1.1rem;font-weight:800;color:#818cf8'>DataPulse</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.divider()
        if st.button("← Home", use_container_width=True):
            st.session_state.page = "home"
            st.rerun()
        if st.button("▶  New Check", use_container_width=True, type="primary"):
            st.session_state.page = "project_setup"
            st.rerun()

        st.divider()
        runs = _get_run_history()
        if len(runs) > 1:
            st.markdown("**Switch Run**")
            labels = [_format_run_label(r) for r in runs]
            sel    = st.selectbox("", labels, key="dash_sb_run",
                                  index=0 if runs else 0,
                                  label_visibility="collapsed")
            if st.button("Load", use_container_width=True, key="dash_sb_load"):
                idx    = labels.index(sel)
                chosen = runs[idx]
                st.session_state.active_output_dir   = chosen["output_dir"]
                st.session_state.project_name        = chosen.get("project_name", "")
                st.session_state.project_description = chosen.get("project_description", "")
                st.rerun()


def _render_sidebar_wizard() -> None:
    with st.sidebar:
        st.markdown(
            "<div style='text-align:center;padding:10px 0 4px'>"
            "<span style='font-size:2rem'>🔍</span><br>"
            "<span style='font-size:1.1rem;font-weight:800;color:#818cf8'>DataPulse</span><br>"
            "<span style='font-size:.72rem;color:#7a8eaf'>Data Validation Wizard</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        if st.session_state.project_name:
            st.markdown(
                f"<div style='text-align:center;padding:4px 8px;background:#1e1b4b;"
                f"border-radius:6px;font-size:.78rem;color:#a5b4fc;margin-bottom:4px'>"
                f"📁 {st.session_state.project_name}</div>",
                unsafe_allow_html=True,
            )
        st.divider()

        current = st.session_state.step
        for i, name in enumerate(STEPS, 1):
            if i < current:
                icon, style, disabled = "✅", "color:#4ade80;font-weight:600", False
            elif i == current:
                icon, style, disabled = "▶",  "color:#818cf8;font-weight:700", True
            else:
                icon, style, disabled = f"{i}.", "color:#475569",              True
            label = f"{icon}  {name}"
            if not disabled:
                if st.button(label, key=f"sb_{i}", use_container_width=True):
                    st.session_state.step = i
                    st.rerun()
            else:
                st.button(label, key=f"sb_{i}", use_container_width=True, disabled=True)

        st.divider()
        st.caption(f"Step {current} of {len(STEPS)}")
        if st.button("← Back to Home", use_container_width=True):
            st.session_state.page = "home"
            st.rerun()
        if st.button("🔄  Reset wizard", use_container_width=True,
                     help="Clear all state"):
            for k, v in _DEFAULTS.items():
                st.session_state[k] = v
            _load_folder.clear()
            st.rerun()


# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------
_init_state()

page = st.session_state.page

if page == "home":
    _render_sidebar_home()
    _page_home()

elif page == "project_setup":
    _render_sidebar_home()
    _page_project_setup()

elif page == "dashboard":
    _render_sidebar_dashboard()
    _page_dashboard()

elif page == "wizard":
    _render_sidebar_wizard()
    _render_progress()
    {1: _step1, 2: _step2, 3: _step3, 4: _step4, 5: _step5, 6: _step6}.get(
        st.session_state.step, _step1
    )()

else:
    st.session_state.page = "home"
    st.rerun()
