from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from typing import Any

import streamlit as st

from .db import (
    PROJECT_TYPE_INDIVIDUAL,
    ROLE_ADMIN,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT_CFO,
    count_pending_workday_verification,
    get_class_by_id,
    get_classes,
    get_submission_budget_preview,
    list_orders,
    list_teams_for_class,
)
from .statuses import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_PROCESSING,
    STATUS_STYLES,
    STATUS_WITHDRAWN,
)


UW_PURPLE = "#4b2e83"
UW_PURPLE_DARK = "#3d2569"
UW_GOLD = "#b7a57a"
TEXT_PRIMARY = "#1f2937"
TEXT_SECONDARY = "#6b7280"
SUCCESS = "#15803d"
WARNING = "#d97706"
DANGER = "#dc2626"
PAGE_BG = "#f9fafb"
CARD_BG = "#ffffff"
BORDER_SUBTLE = "#e5e7eb"


def render_uw_banner() -> None:
    st.markdown(
        """
        <div style="
            background-color: #4b2e83;
            color: white;
            padding: 12px 24px;
            margin: -1rem -1rem 1.5rem -1rem;
            border-bottom: 3px solid #b7a57a;
            display: flex;
            align-items: center;
            gap: 10px;
        ">
            <span style="font-size: 1.8rem; font-weight: 800; letter-spacing: -0.02em;">W</span>
            <span style="font-size: 0.9rem; font-weight: 600; letter-spacing: 0.05em;">· UNIVERSITY of WASHINGTON</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def inject_prt_styles() -> None:
    """Global design tokens: UW purple, typography, cards, buttons, status badges."""
    st.markdown(
        f"""
<style>
  :root {{
    --prt-primary: {UW_PURPLE};
    --prt-primary-dark: {UW_PURPLE_DARK};
    --prt-gold: {UW_GOLD};
    --prt-text: {TEXT_PRIMARY};
    --prt-text-secondary: {TEXT_SECONDARY};
    --prt-success: {SUCCESS};
    --prt-warning: {WARNING};
    --prt-danger: {DANGER};
    --prt-page-bg: {PAGE_BG};
    --prt-card-bg: {CARD_BG};
    --prt-border: {BORDER_SUBTLE};
  }}
  .stApp {{
    background-color: var(--prt-page-bg) !important;
  }}
  /* Streamlit default header — hide rainbow gradient decoration */
  div[data-testid="stDecoration"] {{
    display: none !important;
  }}
  header[data-testid="stHeader"] {{
    background-color: var(--prt-page-bg) !important;
    background-image: none !important;
  }}
  section[data-testid="stMain"] > div {{
    background-color: var(--prt-page-bg) !important;
  }}
  .main .block-container {{
    padding-top: 1.5rem;
    padding-bottom: 2.5rem;
  }}
  /* H1 — page title */
  .main h1,
  section[data-testid="stMain"] h1,
  .main [data-testid="stMarkdownContainer"] h1 {{
    font-size: 2rem !important;
    font-weight: 700 !important;
    color: var(--prt-text) !important;
    letter-spacing: -0.02em;
  }}
  /* H2 — section titles (section_header) */
  .prt-section-header h2 {{
    font-size: 1.5rem !important;
    font-weight: 700 !important;
    margin: 0 0 0.35rem 0 !important;
    letter-spacing: -0.02em;
    color: var(--prt-primary) !important;
  }}
  .prt-section-header .prt-section-desc {{
    margin: 0 0 0.75rem 0 !important;
    color: var(--prt-text-secondary) !important;
    font-size: 0.82rem !important;
    font-weight: 400 !important;
    line-height: 1.45;
  }}
  /* H3 — ##### subsection headers */
  .main h3,
  .main h4,
  .main h5,
  section[data-testid="stMain"] h3,
  section[data-testid="stMain"] h4,
  section[data-testid="stMain"] h5 {{
    font-size: 1rem !important;
    font-weight: 600 !important;
    color: var(--prt-text) !important;
    margin-top: 0 !important;
  }}
  /* Body */
  .main [data-testid="stMarkdownContainer"] p {{
    font-size: 0.95rem;
    color: var(--prt-text);
  }}
  /* Captions — metadata */
  [data-testid="stCaption"],
  [data-testid="stCaption"] p,
  div[data-testid="stCaption"] {{
    font-size: 0.82rem !important;
    color: var(--prt-text-secondary) !important;
    font-weight: 400 !important;
  }}
  .prt-divider {{
    border: none;
    border-top: 1px solid var(--prt-border);
    margin: 1.25rem 0 1.5rem 0;
  }}
  /* Bordered containers — cards (opt-in via marker to avoid clutter) */
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-card-surface) > div {{
    border-radius: 12px !important;
    border: 1px solid var(--prt-border) !important;
    background: var(--prt-card-bg) !important;
    padding: 16px !important;
  }}
  /* Flat panels — background only, no card border */
  div[data-testid="stVerticalBlock"]:has(.prt-flat-panel) {{
    background: #f9fafb !important;
    padding: 16px !important;
    border-radius: 8px !important;
    border: none !important;
    box-shadow: none !important;
  }}
  /* Admin orders table — spreadsheet-style rows, no per-cell boxes */
  div[data-testid="stVerticalBlock"]:has(.prt-admin-table-region) [data-testid="column"] {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
  }}
  div[data-testid="stVerticalBlock"]:has(.prt-admin-table-region) [data-testid="column"] > div {{
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    background: transparent !important;
  }}
  div[data-testid="stVerticalBlock"]:has(.prt-admin-table-region) div[data-testid="stVerticalBlockBorderWrapper"] > div {{
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    padding: 0 !important;
  }}
  div[data-testid="stVerticalBlock"]:has(.prt-admin-table-region) [data-testid="stHorizontalBlock"] {{
    border-radius: 0 !important;
    box-shadow: none !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    padding: 0.45rem 0 !important;
    border-bottom: 1px solid #f3f4f6 !important;
    background: transparent !important;
  }}
  div[data-testid="stVerticalBlock"]:has(.prt-admin-table-region) [data-testid="column"] [data-testid="stHorizontalBlock"] {{
    border-bottom: none !important;
    padding: 0 !important;
    margin: 0 !important;
  }}
  /* Instructor — student expanders: white background (no grey panel) */
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) [data-testid="stExpander"],
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) [data-testid="stExpander"] details,
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) [data-testid="stExpander"] > div,
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests)
    [data-testid="stExpander"] [data-testid="stVerticalBlock"] {{
    background-color: #ffffff !important;
    background: #ffffff !important;
  }}
  /* Submission windows list — row dividers, no row cards */
  .prt-sw-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    padding: 0.65rem 0;
    border-bottom: 1px solid #f3f4f6;
    margin: 0;
  }}
  .prt-sw-row:last-child {{
    border-bottom: none;
  }}
  .prt-sw-row-name {{
    font-weight: 700;
    color: var(--prt-text);
    flex: 1 1 8rem;
    min-width: 8rem;
  }}
  .prt-sw-row-date {{
    flex: 2 1 12rem;
    color: var(--prt-text-secondary);
    font-size: 0.82rem;
  }}
  .prt-sw-row-actions {{
    flex: 0 0 auto;
  }}
  /* Archive list rows — no card chrome */
  div[data-testid="stVerticalBlock"]:has(.prt-archive-order-mark) {{
    background: transparent !important;
    box-shadow: none !important;
    border: none !important;
  }}
  a {{
    color: var(--prt-primary) !important;
  }}
  /* Status badges */
  .prt-badge {{
    display: inline-flex;
    align-items: center;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 0.82rem;
    font-weight: 600;
    line-height: 1.25;
  }}
  .prt-badge-legacy {{
    background: #f3f4f6;
    color: var(--prt-text-secondary);
  }}
  /* Primary buttons */
  button[kind="primary"],
  [data-testid="baseButton-primary"] {{
    background-color: var(--prt-primary) !important;
    border-color: var(--prt-primary) !important;
    color: #ffffff !important;
    font-weight: 600 !important;
  }}
  button[kind="primary"]:hover,
  [data-testid="baseButton-primary"]:hover {{
    background-color: var(--prt-primary-dark) !important;
    border-color: var(--prt-primary-dark) !important;
    color: #ffffff !important;
  }}
  button[kind="primary"]:disabled,
  [data-testid="baseButton-primary"]:disabled {{
    background-color: #d1d5db !important;
    border-color: #d1d5db !important;
    color: #6b7280 !important;
  }}
  /* Secondary — outline purple */
  button[kind="secondary"],
  [data-testid="baseButton-secondary"] {{
    background-color: #ffffff !important;
    color: var(--prt-primary) !important;
    border: 1px solid var(--prt-primary) !important;
    font-weight: 600 !important;
  }}
  button[kind="secondary"]:hover,
  [data-testid="baseButton-secondary"]:hover {{
    background-color: #f3f0f9 !important;
    border-color: var(--prt-primary-dark) !important;
    color: var(--prt-primary-dark) !important;
  }}
  button[kind="secondary"]:disabled,
  [data-testid="baseButton-secondary"]:disabled {{
    background-color: #e5e7eb !important;
    border-color: #d1d5db !important;
    color: #9ca3af !important;
  }}
  /* Danger — Reject / Delete (Streamlit encodes widget key in element id) */
  button[kind="secondary"][id*="ins_rej_"],
  button[kind="secondary"][id*="_rj_"],
  button[kind="secondary"][id*="rej_go_"],
  button[kind="secondary"][id*="ins_btn_confirm_delete"],
  button[kind="secondary"][id*="ins_btn_delete_course"] {{
    background-color: var(--prt-danger) !important;
    border-color: #b91c1c !important;
    color: #ffffff !important;
  }}
  button[kind="secondary"][id*="ins_rej_"]:hover,
  button[kind="secondary"][id*="_rj_"]:hover,
  button[kind="secondary"][id*="rej_go_"]:hover,
  button[kind="secondary"][id*="ins_btn_confirm_delete"]:hover,
  button[kind="secondary"][id*="ins_btn_delete_course"]:hover {{
    background-color: #b91c1c !important;
    border-color: #991b1b !important;
    color: #ffffff !important;
  }}
  .stDownloadButton > button[kind="primary"] {{
    background-color: var(--prt-primary) !important;
    border-color: var(--prt-primary) !important;
    color: #ffffff !important;
    font-weight: 600 !important;
  }}
  .stDownloadButton > button[kind="primary"]:hover {{
    background-color: var(--prt-primary-dark) !important;
    border-color: var(--prt-primary-dark) !important;
    color: #ffffff !important;
  }}
  .stDownloadButton > button[kind="secondary"] {{
    background-color: #ffffff !important;
    color: var(--prt-primary) !important;
    border: 1px solid var(--prt-primary) !important;
    font-weight: 600 !important;
  }}
  .stDownloadButton > button[kind="secondary"]:hover {{
    background-color: #f3f0f9 !important;
    border-color: var(--prt-primary-dark) !important;
    color: var(--prt-primary-dark) !important;
  }}
  /* Tabs */
  .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {{
    color: var(--prt-primary) !important;
    border-bottom-color: var(--prt-primary) !important;
    font-weight: 700 !important;
  }}
  .stTabs [data-baseweb="tab-list"] button {{
    color: #4b5563 !important;
  }}
  [data-baseweb="tab-highlight"],
  div[data-baseweb="tab-highlight"] {{
    background-color: var(--prt-primary) !important;
  }}
  ul[data-baseweb="tab-list"] li[aria-selected="true"] {{
    border-bottom-color: var(--prt-primary) !important;
    color: var(--prt-primary) !important;
  }}
  /* Sidebar */
  section[data-testid="stSidebar"] {{
    background-color: #f3f0f9 !important;
    border-right: 1px solid var(--prt-border) !important;
  }}
  section[data-testid="stSidebar"] > div {{
    background-color: #f3f0f9 !important;
  }}
  section[data-testid="stSidebar"] [data-testid="stCaption"] {{
    font-size: 0.82rem !important;
    color: var(--prt-text-secondary) !important;
  }}
  /* Instructor register / requests — form label emphasis */
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-register) label[data-testid="stWidgetLabel"] p,
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-register) [data-testid="stRadio"] label p {{
    font-weight: 600 !important;
    font-size: 0.95rem !important;
    color: var(--prt-text) !important;
  }}
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) label[data-testid="stWidgetLabel"] p {{
    font-weight: 600 !important;
    color: var(--prt-text) !important;
  }}
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) div[data-testid="stTextInput"] input {{
    min-height: 2.75rem !important;
    font-size: 1rem !important;
    padding: 0.5rem 0.75rem !important;
  }}
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) [data-testid="stExpander"] summary,
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) [data-testid="stExpander"] summary p,
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-ins-section-requests) [data-testid="stExpander"] summary span {{
    font-weight: 600 !important;
    font-size: 1rem !important;
  }}
  /* Student submit — budget panel & item blocks */
  .prt-budget-panel-title {{
    font-size: 1rem !important;
    font-weight: 600 !important;
    color: var(--prt-text) !important;
    margin: 0 0 0.85rem 0 !important;
  }}
  .prt-budget-card .prt-budget-accent {{
    width: 4px;
    min-height: 11rem;
    background: var(--prt-primary);
    border-radius: 4px;
    margin-top: 0.2rem;
  }}
  .prt-budget-cost-line {{
    font-size: 0.95rem !important;
    font-weight: 600 !important;
    color: var(--prt-text) !important;
    margin: 0.85rem 0 0.35rem 0 !important;
    line-height: 1.35;
  }}
  .prt-student-item-idx {{
    font-size: 0.82rem !important;
    color: var(--prt-text-secondary) !important;
    letter-spacing: 0.06em !important;
    text-transform: uppercase !important;
    margin: 0 0 0.65rem 0 !important;
    font-weight: 600 !important;
  }}
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.prt-student-form-labels) label[data-testid="stWidgetLabel"] p {{
    font-weight: 600 !important;
    color: var(--prt-text) !important;
  }}
  section[data-testid="stMain"] button[kind="primary"][data-testid="baseButton-primary"] {{
    font-weight: 600 !important;
  }}
  /* Admin data table */
  .prt-admin-th {{
    font-weight: 700;
    font-size: 0.82rem;
    color: var(--prt-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 0;
    padding: 0 0 6px 0;
  }}
  .prt-admin-td {{
    font-size: 0.95rem;
    color: var(--prt-text);
    margin: 0;
    line-height: 1.35;
  }}
  .prt-admin-table-head-rule {{
    border: none;
    border-bottom: 2px solid #e5e7eb;
    margin: 0 0 0.5rem 0;
  }}
  /* Admin metrics row */
  section[data-testid="stMain"] div[data-testid="stHorizontalBlock"]:has(.prt-admin-metrics-anchor)
    div[data-testid="column"]:nth-child(2) [data-testid="stMetricValue"] {{
    color: inherit !important;
  }}
  .prt-sidebar-heading {{
    font-size: 1rem !important;
    font-weight: 600 !important;
    color: var(--prt-text) !important;
    margin: 0 0 0.35rem 0 !important;
  }}
  .prt-sidebar-line {{
    font-size: 0.82rem;
    margin: 0.15rem 0;
    font-weight: 400;
    color: var(--prt-text-secondary);
  }}
  .prt-sidebar-line strong {{
    color: var(--prt-text);
  }}
  .prt-sidebar-pending {{
    color: var(--prt-warning) !important;
  }}
  .prt-sidebar-approved {{
    color: var(--prt-success) !important;
  }}
  .prt-sidebar-rejected {{
    color: var(--prt-text-secondary) !important;
  }}
  .prt-sidebar-callout {{
    margin: 0.15rem 0;
    padding: 4px 8px;
    border-radius: 8px;
    font-size: 0.82rem;
    font-weight: 600;
    border: 1px solid #fcd34d;
    background: #fef3c7;
    color: var(--prt-warning);
  }}
  .prt-sidebar-callout-muted {{
    margin: 0.15rem 0;
    font-size: 0.82rem;
    color: var(--prt-text-secondary);
  }}
  .prt-week-heading {{
    font-size: 1rem;
    font-weight: 600;
    color: var(--prt-text);
    margin: 0 0 0.5rem 0;
  }}
  .prt-order-card-title {{
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--prt-text);
    margin: 0 0 4px 0;
  }}
  .prt-order-card-meta {{
    font-size: 0.82rem;
    color: var(--prt-text-secondary);
    margin: 0 0 6px 0;
  }}
  .prt-order-card-line {{
    font-size: 0.95rem;
    color: var(--prt-text);
    margin: 4px 0;
  }}
  .prt-order-card-window {{
    font-size: 0.82rem;
    color: var(--prt-text-secondary);
    margin: 0.15rem 0 0 0;
  }}
  a.prt-link-pill {{
    display: inline-block;
    margin-top: 0.35rem;
    padding: 0.4rem 0.9rem;
    background: #f9fafb;
    border: 1px solid var(--prt-border);
    border-radius: 8px;
    font-weight: 600;
    color: var(--prt-text) !important;
    text-decoration: none;
    font-size: 0.82rem;
  }}
</style>
        """,
        unsafe_allow_html=True,
    )


def section_header(title: str, description: str | None = None) -> None:
    desc_html = (
        f'<p class="prt-section-desc">{html.escape(description)}</p>' if description else ""
    )
    st.markdown(
        f'<div class="prt-section-header"><h2>{html.escape(title)}</h2>{desc_html}</div>',
        unsafe_allow_html=True,
    )


def hr_divider() -> None:
    st.markdown('<hr class="prt-divider" />', unsafe_allow_html=True)


def _get_or_init_active_class_id() -> int:
    classes = get_classes()
    if not classes:
        st.session_state.active_class_id = 1
        return 1
    if "active_class_id" not in st.session_state:
        st.session_state.active_class_id = int(classes[0]["id"])
    return int(st.session_state.active_class_id)


def _parse_order_created_local(row: Any) -> datetime | None:
    if "created_at" not in row.keys():
        return None
    s = row["created_at"]
    if s is None:
        return None
    try:
        raw = str(s).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        return None


def _local_week_start_end() -> tuple[datetime, datetime]:
    now = datetime.now().astimezone()
    weekday = now.weekday()
    start = (now - timedelta(days=weekday)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return start, end


def _order_belongs_to_student(row: Any, user_name: str) -> bool:
    un = (user_name or "").strip()
    if not un:
        return False
    cfo = (row["cfo_name_snapshot"] or "").strip() if "cfo_name_snapshot" in row.keys() else ""
    snap = (row["team_number_snapshot"] or "").strip() if "team_number_snapshot" in row.keys() else ""
    return cfo == un or snap == un


def _instructor_awaiting_review(row: Any) -> bool:
    if "archived" in row.keys() and row["archived"]:
        return False
    if "instructor_status" not in row.keys() or row["instructor_status"] is None:
        return True
    v = str(row["instructor_status"]).strip()
    if not v:
        return True
    return v == STATUS_PENDING


def _sidebar_student_summary(class_id: int, user_name: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "total": 0,
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "remaining_budget": None,
    }
    un = (user_name or "").strip()
    if not un:
        return out
    try:
        orders = list_orders(class_id=class_id, exclude_withdrawn=False)
    except Exception:
        return out
    mine = [o for o in orders if _order_belongs_to_student(o, un)]
    out["total"] = len(mine)
    for o in mine:
        stt = str(o["status"]) if "status" in o.keys() else STATUS_PENDING
        if stt in (STATUS_PENDING, STATUS_PROCESSING):
            out["pending"] += 1
        elif stt == STATUS_WITHDRAWN:
            pass
        elif stt == STATUS_APPROVED:
            out["approved"] += 1
        elif stt == STATUS_REJECTED:
            out["rejected"] += 1
    meta = get_class_by_id(int(class_id)) or {}
    is_individual = (meta.get("project_type") or "").strip().lower() == PROJECT_TYPE_INDIVIDUAL
    preview = None
    if is_individual:
        preview = get_submission_budget_preview(int(class_id), un)
    else:
        try:
            teams = list_teams_for_class(int(class_id))
        except Exception:
            teams = []
        match = next((t for t in teams if (t.get("cfo_name") or "").strip() == un), None)
        if match:
            preview = get_submission_budget_preview(int(class_id), str(match["team_number"]))
    if preview is not None:
        out["remaining_budget"] = float(preview[2])
    return out


def _sidebar_instructor_summary(class_id: int) -> dict[str, Any]:
    out = {"total": 0, "awaiting": 0, "approved": 0, "rejected": 0}
    try:
        orders = list_orders(class_id=class_id)
    except Exception:
        return out
    out["total"] = len(orders)
    for o in orders:
        if _instructor_awaiting_review(o):
            out["awaiting"] += 1
        ir = o["instructor_status"] if "instructor_status" in o.keys() else None
        inst = str(ir).strip() if ir is not None else ""
        if inst == STATUS_APPROVED:
            out["approved"] += 1
        elif inst == STATUS_REJECTED:
            out["rejected"] += 1
    return out


def _admin_awaiting_admin_review(row: Any) -> bool:
    """True if this order is in the admin queue (instructor done or legacy; admin still pending)."""
    if "archived" in row.keys() and row["archived"]:
        return False
    adm = row["admin_status"] if "admin_status" in row.keys() else None
    if str(adm or "").strip() != STATUS_PENDING:
        return False
    ir = row["instructor_status"] if "instructor_status" in row.keys() else None
    if ir is None or (isinstance(ir, str) and not str(ir).strip()):
        return True
    return str(ir).strip() == STATUS_APPROVED


def _sidebar_admin_week_summary(class_id: int) -> dict[str, Any]:
    out = {"orders_week": 0, "pending": 0, "approved": 0, "rejected": 0, "spend_week": 0.0}
    try:
        orders = list_orders(class_id=class_id)
    except Exception:
        return out
    w0, w1 = _local_week_start_end()
    for o in orders:
        dt = _parse_order_created_local(o)
        if dt is None or not (w0 <= dt < w1):
            continue
        out["orders_week"] += 1
        stt = str(o["status"]) if "status" in o.keys() else ""
        if stt == STATUS_APPROVED:
            out["approved"] += 1
            try:
                out["spend_week"] += float(o["total_price"] or 0)
            except (TypeError, ValueError):
                pass
        elif stt == STATUS_REJECTED:
            out["rejected"] += 1
        else:
            out["pending"] += 1
    return out


def _render_sidebar_student_panel(class_id: int, user_name: str) -> None:
    st.sidebar.markdown("##### My Submissions")
    s = _sidebar_student_summary(class_id, user_name)
    st.sidebar.caption(f"Total items submitted this class: **{s['total']}**")
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line prt-sidebar-pending">Pending: {s["pending"]} items</p>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line prt-sidebar-approved">Approved: {s["approved"]} items</p>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line prt-sidebar-rejected">Rejected: {s["rejected"]} items</p>',
        unsafe_allow_html=True,
    )
    rb = s["remaining_budget"]
    if rb is not None:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-line" style="margin-top:0.35rem">Remaining budget: <strong>${rb:,.2f}</strong></p>',
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.caption("Remaining budget: —")


def _render_sidebar_instructor_panel(class_id: int) -> None:
    st.sidebar.markdown("##### Class Overview")
    s = _sidebar_instructor_summary(class_id)
    st.sidebar.caption(f"Total submissions for this class: **{s['total']}**")
    if s["awaiting"] > 0:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-callout">Awaiting your review: {s["awaiting"]} items</p>',
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-callout-muted">Awaiting your review: {s["awaiting"]} items</p>',
            unsafe_allow_html=True,
        )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line">Approved by you: {s["approved"]} items</p>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line">Rejected by you: {s["rejected"]} items</p>',
        unsafe_allow_html=True,
    )


def _render_sidebar_admin_panel(class_id: int) -> None:
    st.sidebar.markdown("##### This Week")
    s = _sidebar_admin_week_summary(class_id)
    st.sidebar.caption(f"Orders this week: **{s['orders_week']}**")
    if s["pending"] > 0:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-callout">Pending: {s["pending"]}</p>',
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-callout-muted">Pending: {s["pending"]}</p>',
            unsafe_allow_html=True,
        )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line">Approved: {s["approved"]}</p>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line">Rejected: {s["rejected"]}</p>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        f'<p class="prt-sidebar-line">Total spend this week: <strong>${s["spend_week"]:,.2f}</strong></p>',
        unsafe_allow_html=True,
    )
    try:
        pwk = int(count_pending_workday_verification(class_id))
    except Exception:
        pwk = 0
    if pwk > 0:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-callout">Pending Workday: {pwk}</p>',
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown(
            f'<p class="prt-sidebar-callout-muted">Pending Workday: {pwk}</p>',
            unsafe_allow_html=True,
        )


def _clear_p1_session_on_class_change() -> None:
    """Student submit form uses `p1_*` keys; clear them when switching class to avoid stale state."""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("p1_"):
            del st.session_state[k]
    st.session_state.p1_item_count = 1


def render_sidebar() -> dict[str, Any]:
    classes = get_classes()
    active_class_id = _get_or_init_active_class_id()

    st.sidebar.markdown("### Context")
    uname = st.session_state.get("user_name") or ""
    urole = st.session_state.get("user_role") or ""
    if uname or urole:
        st.sidebar.markdown(
            f'<p style="margin:0 0 0.5rem 0;font-size:0.95rem;color:{TEXT_PRIMARY}"><b>{html.escape(uname)}</b></p>'
            f'<p style="margin:0 0 1rem 0;font-size:0.82rem;color:{TEXT_SECONDARY}">{html.escape(urole)}</p>',
            unsafe_allow_html=True,
        )
    if st.sidebar.button("Sign Out", key="prt_sign_out"):
        for k in ("user_id", "user_name", "user_email", "user_role"):
            if k in st.session_state:
                del st.session_state[k]
        st.rerun()

    if classes:
        current_name = next(
            (c["name"] for c in classes if int(c["id"]) == active_class_id),
            classes[0]["name"],
        )
        new_name = st.sidebar.selectbox(
            "Class",
            options=[c["name"] for c in classes],
            index=[c["name"] for c in classes].index(current_name),
            on_change=_clear_p1_session_on_class_change,
        )
        st.session_state.active_class_id = int(next(c["id"] for c in classes if c["name"] == new_name))
    else:
        st.sidebar.caption("No classes yet. Register a course from Instructor view.")

    st.sidebar.divider()
    if classes:
        aid = int(st.session_state.active_class_id)
        uname_sidebar = (st.session_state.get("user_name") or "").strip()
        if urole == ROLE_STUDENT_CFO:
            _render_sidebar_student_panel(aid, uname_sidebar)
        elif urole == ROLE_INSTRUCTOR:
            _render_sidebar_instructor_panel(aid)
        elif urole == ROLE_ADMIN:
            _render_sidebar_admin_panel(aid)

    return {"active_class_id": st.session_state.get("active_class_id")}


def status_badge_html(status: str) -> str:
    style = STATUS_STYLES.get(status)
    if not style:
        esc = html.escape(str(status))
        return f'<span class="prt-badge prt-badge-legacy">{esc}</span>'
    return (
        f'<span class="prt-badge" style="background:{style.background};color:{style.color}">{html.escape(style.label)}</span>'
    )


def default_deadline_range() -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=30), today + timedelta(days=60)

