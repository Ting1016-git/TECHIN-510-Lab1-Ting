# This app is a Purchase Request Tracker built with Streamlit and SQLite.
# It helps Dorothy, the GIX Program Coordinator, manage student purchase
# requests across multiple teams and vendors. Students (CFOs) submit orders,
# instructors review and approve them, and Dorothy gives final approval and
# tracks delivery. I manually changed the app subtitle from
# "Use the tabs below to work with purchase requests for the selected class."
# to "Submit and track purchase requests for your GIX class."
from __future__ import annotations

import csv
import html
import io
import re
import sqlite3
import time as stdlib_time
from pathlib import Path
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from prt.config import get_receipts_dir
from prt.db import (
    PROJECT_TYPE_INDIVIDUAL,
    ROLE_ADMIN,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT_CFO,
    add_custom_submission_window,
    admin_update_order_details,
    authenticate_user,
    create_class,
    create_orders_batch,
    create_user,
    delete_class_by_id,
    export_orders_csv_rows,
    get_budget_summary_by_team,
    get_class_by_id,
    get_class_project_type,
    get_classes,
    get_email_settings,
    get_first_admin_email,
    get_order,
    get_providers,
    get_student_submission_window_ui_state,
    get_submission_budget_preview,
    get_submission_window_label,
    get_user_email_by_full_name,
    init_db,
    list_archived_orders,
    list_orders,
    order_eligible_for_student_withdraw_or_edit,
    list_submission_windows,
    mark_lost_and_create_replacement,
    mark_received,
    count_pending_workday_verification,
    resolve_window_for_order_submission,
    set_workday_verified,
    save_email_settings,
    save_receipt_path,
    set_instructor_order_status,
    set_order_status,
    set_order_withdrawn,
    set_submission_window_active,
    update_order_details,
)
from prt.email_utils import send_notification, send_test_email
from prt.finance import compute_amount, parse_decimal
from prt.statuses import (
    STATUS_APPROVED,
    STATUS_LOST,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
)
from prt.ui import (
    _admin_awaiting_admin_review,
    hr_divider,
    inject_prt_styles,
    render_sidebar,
    render_uw_banner,
    section_header,
    status_badge_html,
)


def _format_window_datetime_line(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    mm = f"{dt.minute:02d}"
    return f"{dt.strftime('%A %B %d, ')}{h}:{mm} {ampm}"


def _format_countdown_remaining(end: datetime, start: datetime) -> str:
    if end <= start:
        return "0 minutes remaining"
    delta: timedelta = end - start
    days = delta.days
    seconds = delta.seconds
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not parts:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts) + " remaining"

SUBMIT_PROVIDER_OPTIONS = [
    "Amazon",
    "Digikey",
    "Electromaker",
    "AliExpress",
    "Mouser",
    "Seeed Studio",
    "Others",
]

# "Close to budget" when approved spend is at least this fraction of team budget (and not over).
_CLOSE_TO_BUDGET_RATIO = 0.85

EMAIL_FAIL_TOAST = "Order updated, but notification email could not be sent"


def _admin_order_visible_for_dashboard(o: Any) -> bool:
    """Non-archived rows, plus received rows pending Workday verification (archived until verified)."""
    if not o["archived"]:
        return True
    rk = o.keys()
    ra = o["received_at"] if "received_at" in rk else None
    if ra is None:
        return False
    wv = o["workday_verified"] if "workday_verified" in rk else 0
    return not int(wv or 0)


def _format_workday_verified_at(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        raw = str(ts).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        else:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.strftime("%b %d, %Y at %I:%M %p")
    except Exception:
        return str(ts)


def _is_individual_project_type(project_type: str | None) -> bool:
    return (project_type or "").strip().lower() == PROJECT_TYPE_INDIVIDUAL


def _order_party_display_label(project_type: str | None, team_snapshot, cfo_snapshot) -> str:
    """Order row / student column: group -> Team {n} · {cfo}; individual -> {cfo} only."""
    tn = (str(team_snapshot or "")).strip()
    cfo = (str(cfo_snapshot or "")).strip() or "(Unknown CFO)"
    if _is_individual_project_type(project_type):
        return cfo
    return f"Team {tn} · {cfo}" if tn else f"Team — · {cfo}"


def _instructor_notes_meaningful(notes_raw) -> str | None:
    s = str(notes_raw or "").strip()
    if not s or s == "/":
        return None
    return s


def _instructor_order_sort_key(o) -> tuple[int, int]:
    s = str(o["status"] or "").strip()
    rank = {
        STATUS_PENDING: 0,
        STATUS_PROCESSING: 1,
        STATUS_APPROVED: 2,
        STATUS_REJECTED: 3,
        STATUS_WITHDRAWN: 4,
    }.get(s, 9)
    return (rank, int(o["id"]))


def _instructor_dual_status_row_html(inst: str, admin_st: str) -> str:
    def pill_instructor(raw: str) -> str:
        s = (raw or "").strip()
        if s == STATUS_APPROVED:
            return (
                '<span class="prt-badge" style="background:#dcfce7;color:#15803d">'
                "✓ Instructor</span>"
            )
        if s == STATUS_REJECTED:
            return (
                '<span class="prt-badge" style="background:#fee2e2;color:#dc2626">'
                "✗ Instructor</span>"
            )
        return (
            '<span class="prt-badge" style="background:#fef3c7;color:#d97706">'
            "⋯ Instructor</span>"
        )

    def pill_admin(raw: str) -> str:
        s = (raw or "").strip()
        if s == STATUS_APPROVED:
            return (
                '<span class="prt-badge" style="background:#dcfce7;color:#15803d">'
                "✓ Admin</span>"
            )
        if s == STATUS_REJECTED:
            return (
                '<span class="prt-badge" style="background:#fee2e2;color:#dc2626">'
                "✗ Admin</span>"
            )
        return (
            '<span class="prt-badge" style="background:#fef3c7;color:#d97706">'
            "⋯ Admin</span>"
        )

    return (
        pill_instructor(inst)
        + '<span style="color:#9ca3af;margin:0 6px">·</span>'
        + pill_admin(admin_st)
    )


def _compute_course_summary_report(class_id: int) -> dict:
    """Aggregates for Summary page only; uses existing list/get helpers (no new DB API)."""
    classes = get_classes()
    meta = next((c for c in classes if int(c["id"]) == int(class_id)), None)
    project_type = (meta.get("project_type") if meta else None) or ""
    is_individual = _is_individual_project_type(project_type)
    course_budget = float(meta["total_budget"]) if meta and meta.get("total_budget") is not None else None

    budget_summary = get_budget_summary_by_team(class_id)
    total_spend = sum(r["used_amount"] for r in budget_summary)
    n_teams = len(budget_summary)
    avg_spend_per_team = (total_spend / n_teams) if n_teams else 0.0

    orders = list_orders(class_id=class_id)
    orders = [o for o in orders if not o["archived"]]

    approved_count = sum(1 for o in orders if str(o["status"]) == STATUS_APPROVED)
    rejected_count = sum(1 for o in orders if str(o["status"]) == STATUS_REJECTED)

    item_counter = Counter(
        (str(o["item_name"]) or "").strip() for o in orders if (str(o["item_name"]) or "").strip()
    )
    top_items = item_counter.most_common(3)

    provider_counter = Counter(
        (str(o["provider_name_snapshot"]) or "").strip()
        for o in orders
        if (str(o["provider_name_snapshot"]) or "").strip()
    )
    top_providers = provider_counter.most_common(3)

    cfo_by_team: dict[str, str] = {}
    for o in orders:
        tn = (str(o["team_number_snapshot"]) or "").strip()
        cfo = (str(o["cfo_name_snapshot"]) or "").strip()
        if tn and cfo and tn not in cfo_by_team:
            cfo_by_team[tn] = cfo

    exceeded_rows: list[tuple[str, float, float, float]] = []
    close_rows: list[tuple[str, float, float, float]] = []
    for r in budget_summary:
        team = str(r["team_number"])
        budget = float(r["budget_total"])
        used = float(r["used_amount"])
        remaining = float(r["remaining_amount"])
        cfo = (cfo_by_team.get(team) or "").strip()
        if is_individual:
            label = cfo or team or "—"
        else:
            label = f"Team {team} · {cfo}" if cfo else f"Team {team}"
        if budget > 0 and used > budget + 1e-9:
            exceeded_rows.append((label, used, budget, remaining))
        elif budget > 0 and used >= budget * _CLOSE_TO_BUDGET_RATIO and used <= budget + 1e-9:
            close_rows.append((label, used, budget, remaining))

    pct_of_course: float | None = None
    if course_budget is not None and course_budget > 0:
        pct_of_course = min(100.0, (total_spend / course_budget) * 100.0)

    return {
        "course_budget": course_budget,
        "total_spend": total_spend,
        "pct_of_course": pct_of_course,
        "n_teams": n_teams,
        "avg_spend_per_team": avg_spend_per_team,
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "top_items": top_items,
        "top_providers": top_providers,
        "exceeded_rows": exceeded_rows,
        "close_rows": close_rows,
    }


def _require_non_empty(value: str, field_name: str) -> None:
    if value is None:
        raise ValueError(f"{field_name} is required.")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{field_name} is required.")


def _email_order_detail_html(o: Any) -> str:
    item = html.escape(str(o["item_name"] or ""))
    qty_raw = o["quantity"]
    try:
        qf = float(qty_raw)
        qty_disp = str(int(qf)) if qf == int(qf) else str(qty_raw)
    except (TypeError, ValueError):
        qty_disp = str(qty_raw if qty_raw is not None else "")
    qty = html.escape(qty_disp)
    try:
        unit = float(o["unit_price"] or 0)
    except (TypeError, ValueError):
        unit = 0.0
    try:
        tot = float(o["total_price"] or 0)
    except (TypeError, ValueError):
        tot = 0.0
    sup = html.escape(str(o["provider_name_snapshot"] or ""))
    link = str(o["purchase_link"] or "").strip()
    link_html = (
        f'<a href="{html.escape(link, quote=True)}">Open link</a>' if link else "—"
    )
    return (
        f"<p><strong>Item:</strong> {item}</p>"
        f"<p><strong>Quantity:</strong> {qty}</p>"
        f"<p><strong>Unit price:</strong> ${unit:,.2f}</p>"
        f"<p><strong>Line total:</strong> ${tot:,.2f}</p>"
        f"<p><strong>Supplier:</strong> {sup}</p>"
        f"<p><strong>Purchase link:</strong> {link_html}</p>"
    )


def _notify_admin_new_purchase_request(order_id: int) -> bool:
    o = get_order(order_id)
    if not o:
        return True
    admin_em = get_first_admin_email()
    if not admin_em:
        return True
    student_plain = str(o["cfo_name_snapshot"] or "").strip()
    item_name = str(o["item_name"] or "").strip()
    subj = f"New purchase request from {student_plain} — {item_name}"
    student_h = html.escape(student_plain)
    wid = o["window_id"] if "window_id" in o.keys() else None
    wlab = get_submission_window_label(wid)
    win_html = (
        f"<p><strong>Submission window:</strong> {html.escape(wlab)}</p>" if wlab else ""
    )
    inner = f"<p><strong>Student:</strong> {student_h}</p>" + _email_order_detail_html(o) + win_html
    return send_notification(admin_em, subj, inner)


def _notify_student_instructor_approved(order_id: int) -> bool:
    o = get_order(order_id)
    if not o:
        return True
    to = get_user_email_by_full_name(str(o["cfo_name_snapshot"] or ""))
    if not to:
        return True
    item_name = str(o["item_name"] or "").strip()
    subj = f"Your purchase request was approved by instructor — {item_name}"
    inner = (
        _email_order_detail_html(o)
        + "<p>Your instructor approved this request. <strong>Next step:</strong> the program admin will review it.</p>"
    )
    return send_notification(to, subj, inner)


def _notify_student_instructor_rejected(order_id: int) -> bool:
    o = get_order(order_id)
    if not o:
        return True
    to = get_user_email_by_full_name(str(o["cfo_name_snapshot"] or ""))
    if not to:
        return True
    item_name = str(o["item_name"] or "").strip()
    subj = f"Your purchase request was rejected — {item_name}"
    ir = (
        str(o["instructor_rejection_reason"] or "").strip()
        if "instructor_rejection_reason" in o.keys()
        else ""
    )
    inner = (
        _email_order_detail_html(o)
        + f"<p><strong>Rejection reason:</strong> {html.escape(ir)}</p>"
        + "<p>You may submit a revised request after addressing the feedback above.</p>"
    )
    return send_notification(to, subj, inner)


def _notify_student_admin_approved(order_id: int) -> bool:
    o = get_order(order_id)
    if not o:
        return True
    to = get_user_email_by_full_name(str(o["cfo_name_snapshot"] or ""))
    if not to:
        return True
    item_name = str(o["item_name"] or "").strip()
    subj = f"Your purchase request is fully approved — {item_name}"
    inner = (
        _email_order_detail_html(o)
        + "<p>Your purchase request is <strong>fully approved</strong>. Dorothy will place the order on behalf of the class.</p>"
    )
    return send_notification(to, subj, inner)


def _notify_student_admin_rejected(order_id: int) -> bool:
    o = get_order(order_id)
    if not o:
        return True
    to = get_user_email_by_full_name(str(o["cfo_name_snapshot"] or ""))
    if not to:
        return True
    item_name = str(o["item_name"] or "").strip()
    subj = f"Your purchase request was rejected by admin — {item_name}"
    rr = str(o["rejection_reason"] or "").strip() if "rejection_reason" in o.keys() else ""
    inner = _email_order_detail_html(o) + f"<p><strong>Rejection reason:</strong> {html.escape(rr)}</p>"
    return send_notification(to, subj, inner)


def _submit_status_tracker_html(order_row) -> str:
    status = str(order_row["status"])
    inst = str(order_row["instructor_status"]) if "instructor_status" in order_row.keys() else STATUS_PENDING
    adm = str(order_row["admin_status"]) if "admin_status" in order_row.keys() else STATUS_PENDING

    admin_reason_raw = order_row["rejection_reason"] if "rejection_reason" in order_row.keys() else None
    inst_reason_raw = (
        order_row["instructor_rejection_reason"] if "instructor_rejection_reason" in order_row.keys() else None
    )
    if status == STATUS_REJECTED and inst == STATUS_REJECTED:
        reason = (inst_reason_raw or "").strip() or None
    else:
        reason = (admin_reason_raw or "").strip() or None
    reason_esc = html.escape(reason) if reason else ""

    step_labels = ["Instructor review", "Admin review", "Fully approved"]

    def pill_html(label: str, kind: str) -> str:
        if kind == "done":
            style = "background:#dcfce7;color:#15803d;font-weight:600"
        elif kind == "current":
            style = "background:#fef3c7;color:#d97706;font-weight:600"
        elif kind == "rejected":
            style = "background:#fee2e2;color:#dc2626;font-weight:600"
        else:
            style = "background:#f3f4f6;color:#6b7280;font-weight:600"
        return f'<span class="prt-badge" style="{style}">{label}</span>'

    if status == STATUS_REJECTED:
        rej_at_inst = inst == STATUS_REJECTED
        rej_at_adm = adm == STATUS_REJECTED
        if rej_at_inst:
            parts = [
                pill_html(step_labels[0], "rejected"),
                pill_html(step_labels[1], "todo"),
                pill_html(step_labels[2], "todo"),
            ]
        elif rej_at_adm:
            p0 = "done" if inst == STATUS_APPROVED else "current"
            parts = [
                pill_html(step_labels[0], p0),
                pill_html(step_labels[1], "rejected"),
                pill_html(step_labels[2], "todo"),
            ]
        else:
            parts = [pill_html(lab, "todo") for lab in step_labels]
        body = (
            '<div style="padding:12px;border:1px solid #e5e7eb;border-radius:12px;background:#fff">'
            + '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            + " → ".join(parts)
            + "</div>"
            + '<div style="margin-top:10px;color:#dc2626;font-weight:600">Rejected</div>'
        )
        if reason_esc:
            body += f'<div style="margin-top:4px;font-size:0.95rem;color:#1f2937">{reason_esc}</div>'
        else:
            body += '<div style="margin-top:4px;font-size:0.82rem;color:#6b7280">No reason recorded.</div>'
        body += "</div>"
        return body

    if status == STATUS_APPROVED:
        parts = [pill_html(lab, "done") for lab in step_labels]
    elif status == STATUS_PROCESSING:
        parts = [
            pill_html(step_labels[0], "done"),
            pill_html(step_labels[1], "current"),
            pill_html(step_labels[2], "todo"),
        ]
    else:
        parts = [
            pill_html(step_labels[0], "current"),
            pill_html(step_labels[1], "todo"),
            pill_html(step_labels[2], "todo"),
        ]

    return (
        '<div style="padding:12px;border:1px solid #e5e7eb;border-radius:12px;background:#fff">'
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        + " → ".join(parts)
        + "</div></div>"
    )


def _render_submit_budget_panel(
    is_individual: bool,
    team_for_budget: str,
    preview: tuple[float, float, float] | None,
    total_budget: float | None,
    used_approved: float | None,
    remaining_budget: float | None,
    requested_total: Decimal,
    remaining_after: Decimal | None,
) -> None:
    """Student submit page only: budget summary (metrics, progress, request cost)."""
    with st.container():
        st.markdown('<span class="prt-flat-panel"></span>', unsafe_allow_html=True)
        acc_col, main_col = st.columns([0.04, 0.96], gap="small")
        with acc_col:
            st.markdown(
                '<div class="prt-budget-card"><div class="prt-budget-accent" aria-hidden="true"></div></div>',
                unsafe_allow_html=True,
            )
        with main_col:
            st.markdown(
                '<p class="prt-budget-panel-title">Budget check (real-time)</p>',
                unsafe_allow_html=True,
            )

            if team_for_budget and preview is None:
                st.warning(
                    "This class has no per-group/student budget configured. Ask your instructor to check course settings."
                )
                return
            if not team_for_budget:
                st.info(
                    "Signed-in name is missing; budget preview cannot load. Sign out and sign in again, or contact your instructor."
                    if is_individual
                    else "Enter your team number in Team details below to load budget figures."
                )
                return
            if total_budget is None or used_approved is None or remaining_budget is None:
                return

            tb = float(total_budget)
            ua = float(used_approved)
            rem = float(remaining_budget)
            req_f = float(requested_total)

            rem_value_color = "#15803d" if rem > 0 else "#dc2626"
            st.markdown(
                f"""
<style>
section.main div[data-testid="stHorizontalBlock"]:has(> div[data-testid="column"]:nth-child(3))
  > div[data-testid="column"]:nth-child(2) [data-testid="stMetricValue"] {{
  color: #d97706 !important;
  font-weight: 700 !important;
}}
section.main div[data-testid="stHorizontalBlock"]:has(> div[data-testid="column"]:nth-child(3))
  > div[data-testid="column"]:nth-child(3) [data-testid="stMetricValue"] {{
  color: {rem_value_color} !important;
  font-weight: 700 !important;
}}
</style>
""",
                unsafe_allow_html=True,
            )

            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("💰 Total Budget", f"${tb:,.2f}")
            with c2:
                st.metric("📦 Already Used", f"${ua:,.2f}")
            with c3:
                rem_label = "Remaining" if rem > 0 else "Remaining"
                st.metric(rem_label, f"${rem:,.2f}")

            pct_used = (ua / tb * 100.0) if tb > 0 else 0.0
            pct_used = min(100.0, max(0.0, pct_used))
            if pct_used < 70:
                bar_color = "#15803d"
            elif pct_used <= 90:
                bar_color = "#d97706"
            else:
                bar_color = "#dc2626"

            st.markdown(
                f"""
<div style="margin:14px 0 10px 0">
<div style="font-size:0.82rem;color:#6b7280;margin-bottom:6px">Budget used ({pct_used:.0f}% of total)</div>
<div style="background:#e5e7eb;border-radius:8px;height:16px;overflow:hidden;max-width:100%">
<div style="width:{pct_used:.2f}%;background:{bar_color};height:100%;border-radius:8px"></div>
</div>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown(
                f'<p class="prt-budget-cost-line">This request will cost: ${req_f:,.2f}</p>',
                unsafe_allow_html=True,
            )

            if remaining_after is None:
                st.info("Enter valid quantities and unit prices on every line to total this request.")
            elif remaining_after < 0:
                over = abs(float(remaining_after))
                st.markdown(
                    f"""
<div style="background:#fee2e2;border:2px solid #fecaca;border-radius:10px;padding:14px 16px;margin-top:10px;
color:#991b1b;font-weight:700;font-size:1rem">
⚠️ Over budget by ${over:,.2f} — please reduce your order
</div>
""",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<p style="color:#15803d;font-size:0.95rem;margin:10px 0 0 0;font-weight:600">Within budget</p>',
                    unsafe_allow_html=True,
                )


def render_submit_request() -> None:
    active_class_id = st.session_state.active_class_id
    prev_banner_class = st.session_state.get("_prt_submit_banner_class_id")
    if prev_banner_class is not None and int(prev_banner_class) != int(active_class_id):
        st.session_state.pop("prt_just_submitted", None)
        st.session_state.pop("prt_snapshot_after_submit", None)
        st.session_state.pop("prt_submit_cooldown_end", None)
    st.session_state["_prt_submit_banner_class_id"] = int(active_class_id)

    class_meta = get_class_by_id(int(active_class_id)) or {}
    is_individual = class_meta.get("project_type") == PROJECT_TYPE_INDIVIDUAL

    if st.session_state.get("prt_just_submitted") and "prt_snapshot_after_submit" in st.session_state:
        if _p1_form_state_snapshot(is_individual) != st.session_state.prt_snapshot_after_submit:
            st.session_state.prt_just_submitted = False
            st.session_state.pop("prt_snapshot_after_submit", None)

    if st.session_state.get("prt_just_submitted"):
        st.success(
            "✅ Your request has been submitted successfully! "
            "Check the My Orders tab to track its status."
        )

    section_header(
        "Submit Purchase Request",
        "CFOs — enter team details, line items, and submit for instructor and admin review.",
    )
    if st.session_state.pop("prt_email_toast", False):
        st.toast(EMAIL_FAIL_TOAST, icon="⚠️")
    closed_msg = st.session_state.pop("prt_submit_closed_msg", None)
    if closed_msg:
        st.info(closed_msg)
    hr_divider()

    if "p1_item_count" not in st.session_state:
        st.session_state.p1_item_count = 1

    if is_individual:
        team_for_budget = (st.session_state.get("user_name") or "").strip()
    else:
        team_for_budget = (st.session_state.get("p1_team_number") or "").strip()

    total_budget = None
    used_approved = None
    remaining_budget = None
    preview = get_submission_budget_preview(int(active_class_id), team_for_budget) if team_for_budget else None
    if preview is not None:
        total_budget, used_approved, remaining_budget = preview

    requested_total = Decimal("0")
    for i in range(st.session_state.p1_item_count):
        q = parse_decimal(st.session_state.get(f"p1_quantity_{i}", "1"))
        p = parse_decimal(st.session_state.get(f"p1_unit_price_{i}", "0"))
        line = compute_amount(q, p)
        if line is not None:
            requested_total += line

    remaining_after = None
    if remaining_budget is not None:
        remaining_after = Decimal(str(remaining_budget)) - requested_total

    _render_submit_budget_panel(
        is_individual=is_individual,
        team_for_budget=team_for_budget,
        preview=preview,
        total_budget=total_budget,
        used_approved=used_approved,
        remaining_budget=remaining_budget,
        requested_total=requested_total,
        remaining_after=remaining_after,
    )

    hr_divider()

    ui_state = get_student_submission_window_ui_state(int(active_class_id))
    now_local = datetime.now().astimezone()
    end_local = ui_state["deadline_end_local"]
    with st.container():
        st.markdown('<span class="prt-flat-panel"></span>', unsafe_allow_html=True)
        st.markdown("##### Submission window")
        if ui_state["has_open_window"]:
            win_line = _format_window_datetime_line(end_local)
            st.markdown(
                f'<p style="font-size:1rem;font-weight:700;color:#111827;margin:0 0 0.35rem 0">'
                f"Current window: {html.escape(win_line)}</p>",
                unsafe_allow_html=True,
            )
            remaining_str = _format_countdown_remaining(end_local, now_local)
            delta = end_local - now_local
            hours_left = max(0.0, delta.total_seconds() / 3600.0)
            countdown_color = "#d97706" if hours_left < 24.0 else "#15803d"
            st.markdown(
                '<p style="font-size:0.82rem;color:#6b7280;margin:0.25rem 0 0.1rem 0;font-weight:500">'
                "Time remaining</p>"
                f'<p style="font-size:1.1rem;font-weight:600;color:{countdown_color};margin:0;line-height:1.35">'
                f"{html.escape(remaining_str)}</p>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="background:#fee2e2;border:1px solid #fecaca;border-radius:10px;padding:12px 14px;color:#991b1b;font-weight:600">'
                "Submission window closed. Your request will be added to next week's list."
                "</div>",
                unsafe_allow_html=True,
            )
            st.caption(
                f"Next weekly deadline: {_format_window_datetime_line(end_local)}"
            )

    hr_divider()

    submitter_display = (st.session_state.get("user_name") or "").strip()
    name_esc = html.escape(submitter_display or "—")
    submit_as_html = (
        f'<p class="prt-sidebar-line" style="margin:0.35rem 0 0.5rem 0;color:#1f2937">'
        f"Submitting as: <strong>{name_esc}</strong></p>"
    )
    team_number = ""
    if is_individual:
        st.markdown(submit_as_html, unsafe_allow_html=True)
    else:
        with st.container(border=True):
            st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
            st.markdown(
                '<div class="prt-student-form-labels" style="display:none"></div>',
                unsafe_allow_html=True,
            )
            st.markdown("##### Team details")
            team_number = st.text_input("Team Number", key="p1_team_number", placeholder="e.g. 1")
            st.markdown(submit_as_html, unsafe_allow_html=True)

    hr_divider()

    with st.container(border=True):
        st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
        st.markdown(
            '<div class="prt-student-form-labels" style="display:none"></div>',
            unsafe_allow_html=True,
        )
        st.markdown("##### Line items")
        st.caption("Each block is one order line. Add more lines if you are ordering multiple parts.")
        for i in range(st.session_state.p1_item_count):
            with st.container(border=True):
                st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
                st.markdown(
                    f'<p class="prt-student-item-idx">Item {i + 1}</p>',
                    unsafe_allow_html=True,
                )
                st.selectbox(
                    "Provider / Supplier",
                    options=SUBMIT_PROVIDER_OPTIONS,
                    index=0,
                    key=f"p1_provider_choice_{i}",
                )
                if st.session_state.get(f"p1_provider_choice_{i}") == "Others":
                    st.text_input(
                        "Custom website / provider name",
                        key=f"p1_provider_other_{i}",
                    )
                st.text_input("Item Name", key=f"p1_item_name_{i}")
                st.text_input("Quantity", key=f"p1_quantity_{i}", value="1")
                st.text_input("Unit Price ($)", key=f"p1_unit_price_{i}", value="0")
                st.text_input("Purchase Link", key=f"p1_purchase_link_{i}")
                st.text_area("Notes", key=f"p1_notes_{i}", height=80)

        if st.button("＋ Add Another Item", key="p1_add_item"):
            st.session_state.p1_item_count += 1
            st.rerun()

    submit_disabled = False
    if not team_for_budget:
        submit_disabled = True
    for i in range(st.session_state.p1_item_count):
        ch = st.session_state.get(f"p1_provider_choice_{i}", SUBMIT_PROVIDER_OPTIONS[0])
        if ch == "Others" and not (st.session_state.get(f"p1_provider_other_{i}") or "").strip():
            submit_disabled = True
            break
    if team_for_budget and preview is None:
        submit_disabled = True
    if remaining_budget is not None and remaining_after is not None and remaining_after < 0:
        submit_disabled = True

    hr_divider()

    cooldown_end = st.session_state.get("prt_submit_cooldown_end")
    in_cooldown = False
    if cooldown_end is not None and not st.session_state.get("prt_just_submitted"):
        in_cooldown = stdlib_time.time() < float(cooldown_end)

    _, submit_col2 = st.columns([1, 3])
    with submit_col2:
        if st.session_state.get("prt_just_submitted"):
            st.button(
                "✅ Submitted — switch to My Orders to track status",
                disabled=True,
                type="secondary",
                key="p1_submit_ack",
                use_container_width=True,
            )
        elif st.button(
            "Submit Request",
            type="primary",
            disabled=submit_disabled or in_cooldown,
            key="p1_submit_btn",
            use_container_width=True,
        ):
            try:
                cfo_name = (st.session_state.get("user_name") or "").strip()
                if is_individual:
                    _require_non_empty(cfo_name, "Your name")
                else:
                    _require_non_empty(team_number, "Team Number")
                    _require_non_empty(cfo_name, "Your name")

                items_payload = []
                for i in range(st.session_state.p1_item_count):
                    pch = st.session_state.get(f"p1_provider_choice_{i}", SUBMIT_PROVIDER_OPTIONS[0])
                    if pch == "Others":
                        pother = st.session_state.get(f"p1_provider_other_{i}", "")
                        _require_non_empty(pother, f"Item {i + 1} — Custom website / provider name")
                        provider_resolved = (pother or "").strip()
                    else:
                        provider_resolved = pch
                    _require_non_empty(provider_resolved, f"Item {i + 1} — Provider / Supplier")

                    iname = st.session_state.get(f"p1_item_name_{i}", "")
                    plink = st.session_state.get(f"p1_purchase_link_{i}", "")
                    nts = st.session_state.get(f"p1_notes_{i}", "")
                    qty = parse_decimal(st.session_state.get(f"p1_quantity_{i}", "1"))
                    price = parse_decimal(st.session_state.get(f"p1_unit_price_{i}", "0"))
                    _require_non_empty(iname, f"Item {i + 1} — Item Name")
                    _require_non_empty(plink, f"Item {i + 1} — Purchase Link")
                    if qty is None or qty <= 0:
                        raise ValueError(f"Item {i + 1}: quantity must be a number greater than 0.")
                    if price is None or price <= 0:
                        raise ValueError(f"Item {i + 1}: unit price must be a number greater than 0.")
                    items_payload.append(
                        {
                            "item_name": iname.strip(),
                            "quantity": float(qty),
                            "unit_price": float(price),
                            "purchase_link": plink.strip(),
                            "notes": nts.strip(),
                            "provider_name": provider_resolved,
                        }
                    )

                resolved = resolve_window_for_order_submission(int(active_class_id))
                order_ids = create_orders_batch(
                    class_id=active_class_id,
                    team_number=team_number.strip() if not is_individual else "",
                    cfo_name=cfo_name.strip(),
                    items=items_payload,
                    deadline=resolved["deadline"],
                    window_id=int(resolved["window_id"]),
                    individual_budget_key=st.session_state.get("user_name")
                    if is_individual
                    else None,
                )

                email_ok = True
                for oid in order_ids:
                    if not _notify_admin_new_purchase_request(oid):
                        email_ok = False

                _clear_p1_form_keys()
                st.session_state.prt_just_submitted = True
                st.session_state.prt_submit_cooldown_end = stdlib_time.time() + 3.0
                st.session_state.pop("prt_snapshot_after_submit", None)
                if not email_ok:
                    st.session_state.prt_email_toast = True
                if resolved.get("closed_message"):
                    st.session_state.prt_submit_closed_msg = resolved["closed_message"]
                st.rerun()
            except Exception as e:
                st.error(str(e))

    if st.session_state.get("prt_just_submitted") and "prt_snapshot_after_submit" not in st.session_state:
        st.session_state.prt_snapshot_after_submit = _p1_form_state_snapshot(is_individual)


def render_instructor_page() -> None:
    if st.session_state.pop("prt_email_toast", False):
        st.toast(EMAIL_FAIL_TOAST, icon="⚠️")
    section_header(
        "Instructor",
        "Register courses and review purchase requests for the selected class.",
    )
    hr_divider()

    active_class_id = st.session_state.active_class_id

    with st.container(border=True):
        st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
        st.markdown('<span class="prt-ins-section-register"></span>', unsafe_allow_html=True)
        st.markdown("##### Register a new course")
        with st.form("instructor_new_course"):
            cr1, cr2 = st.columns(2)
            with cr1:
                st.text_input("Course name", key="ins_course_name", placeholder="e.g. TECHIN 514 Winter 2026")
            with cr2:
                st.number_input(
                    "Budget per group/student",
                    min_value=0.01,
                    value=500.0,
                    step=50.0,
                    key="ins_budget_per_group",
                )
                st.caption("This amount applies equally to every student or group")
            st.radio(
                "Project type",
                options=["Group project", "Individual project"],
                index=0,
                horizontal=True,
                key="ins_project_type",
            )
            reg = st.form_submit_button("Register course", type="primary")
            if reg:
                try:
                    name = (st.session_state.get("ins_course_name") or "").strip()
                    budget = float(st.session_state.get("ins_budget_per_group") or 0.0)
                    pt_choice = (st.session_state.get("ins_project_type") or "Group project").strip()
                    pt = "group" if pt_choice == "Group project" else "individual"
                    new_id = create_class(name, budget, pt)
                    st.session_state.active_class_id = new_id
                    st.success("Course registered. It is now selected in the sidebar.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    hr_divider()

    with st.container(border=True):
        st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
        st.markdown('<span class="prt-ins-section-manage"></span>', unsafe_allow_html=True)
        st.markdown("##### Manage existing courses")

        classes = get_classes()
        if not classes:
            st.info("No courses registered yet.")
        else:
            if "ins_delete_show_flow" not in st.session_state:
                st.session_state.ins_delete_show_flow = False

            def _reset_ins_delete_flow() -> None:
                st.session_state.ins_delete_show_flow = False

            options_labels = [c["name"] for c in classes]
            st.selectbox(
                "Course",
                options=options_labels,
                key="ins_mgmt_course_label",
                on_change=_reset_ins_delete_flow,
            )
            selected_label = str(st.session_state.get("ins_mgmt_course_label") or options_labels[0])
            selected_delete_id = int(next(c["id"] for c in classes if c["name"] == selected_label))

            if st.button("Delete this course", key="ins_btn_delete_course", type="secondary"):
                st.session_state.ins_delete_show_flow = True
                st.rerun()

            if st.session_state.ins_delete_show_flow:
                confirm = st.checkbox(
                    "I confirm I want to permanently delete this course and all its orders",
                    key="ins_delete_confirm_chk",
                )
                if confirm:
                    if st.button("Confirm Delete", key="ins_btn_confirm_delete", type="secondary"):
                        if int(selected_delete_id) == int(active_class_id):
                            st.error("Switch to a different class before deleting this one")
                        else:
                            try:
                                delete_class_by_id(int(selected_delete_id))
                                st.session_state.ins_delete_show_flow = False
                                for k in ("ins_mgmt_course_label", "ins_delete_confirm_chk"):
                                    if k in st.session_state:
                                        del st.session_state[k]
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

    hr_divider()

    with st.container(border=True):
        st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
        st.markdown('<span class="prt-ins-section-requests"></span>', unsafe_allow_html=True)
        st.markdown("##### Purchase requests (selected class)")
        classes = get_classes()
        meta = next((c for c in classes if int(c["id"]) == int(active_class_id)), None)
        if meta and meta.get("budget_per_group") is not None:
            pt_label = "Group" if meta.get("project_type") != PROJECT_TYPE_INDIVIDUAL else "Individual"
            st.caption(
                f"Per-group/student budget (this class): **{meta['budget_per_group']:,.2f}** · "
                f"Project type: **{pt_label}**"
            )

        try:
            orders = list_orders(class_id=active_class_id)
        except Exception:
            orders = []

        orders = [o for o in orders if not o["archived"]]

        if not orders:
            st.info("No purchase requests for this class yet.")
            return

        by_cfo: dict[str, list] = defaultdict(list)
        for o in orders:
            cfo_key = (o["cfo_name_snapshot"] or "").strip() or "(Unknown CFO)"
            by_cfo[cfo_key].append(o)

        n_items = len(orders)
        n_pending = sum(1 for o in orders if str(o["instructor_status"]) == STATUS_PENDING)
        n_inst_apr = sum(1 for o in orders if str(o["instructor_status"]) == STATUS_APPROVED)
        n_inst_rej = sum(1 for o in orders if str(o["instructor_status"]) == STATUS_REJECTED)
        if n_pending > 0:
            pending_seg = (
                f'<span style="color:#d97706;font-weight:700;font-size:1.1rem">'
                f"{n_pending} pending your review</span>"
            )
        else:
            pending_seg = (
                f'<span style="color:#9ca3af;font-weight:500">{n_pending} pending your review</span>'
            )
        appr_seg = (
            f'<span style="color:#16a34a;font-weight:600">{n_inst_apr} approved</span>'
        )
        if n_inst_rej > 0:
            rej_seg = (
                f'<span style="color:#dc2626;font-weight:600">{n_inst_rej} rejected</span>'
            )
        else:
            rej_seg = (
                f'<span style="color:#9ca3af;font-weight:500">{n_inst_rej} rejected</span>'
            )
        st.markdown(
            '<p style="font-size:1rem;margin:0.5rem 0 1rem 0;line-height:1.65">'
            f'<span style="color:#111827;font-weight:500">{n_items} items total</span>'
            '<span style="color:#d1d5db"> · </span>'
            f"{pending_seg}"
            '<span style="color:#d1d5db"> · </span>'
            f"{appr_seg}"
            '<span style="color:#d1d5db"> · </span>'
            f"{rej_seg}"
            "</p>",
            unsafe_allow_html=True,
        )

        st.session_state.setdefault("ins_batch_approve_confirm", False)
        pending_ids = [int(o["id"]) for o in orders if str(o["instructor_status"]) == STATUS_PENDING]
        if not pending_ids:
            st.session_state.ins_batch_approve_confirm = False

        st.text_input(
            "Search by student name",
            key="ins_student_search",
            placeholder="Type to filter by student / CFO name",
        )
        q = (st.session_state.get("ins_student_search") or "").strip().lower()

        if not st.session_state.get("ins_batch_approve_confirm"):
            if st.button(
                "Approve all pending items",
                key="ins_btn_apr_all",
                type="primary",
                disabled=len(pending_ids) == 0,
            ):
                st.session_state.ins_batch_approve_confirm = True
                st.rerun()
        else:
            st.warning(f"This will approve {len(pending_ids)} pending items. Confirm?")
            bac1, bac2 = st.columns(2)
            with bac1:
                if st.button(
                    "Confirm",
                    type="primary",
                    key="ins_btn_apr_all_yes",
                    use_container_width=True,
                ):
                    errs: list[str] = []
                    for oid in pending_ids:
                        try:
                            set_instructor_order_status(oid, STATUS_APPROVED)
                            if not _notify_student_instructor_approved(oid):
                                st.session_state.prt_email_toast = True
                        except Exception as e:
                            errs.append(f"Order {oid}: {e}")
                    st.session_state.ins_batch_approve_confirm = False
                    if errs:
                        st.error("\n".join(errs))
                    else:
                        st.toast(f"Approved {len(pending_ids)} item(s).", icon="✅")
                    st.rerun()
            with bac2:
                if st.button("Cancel", key="ins_btn_apr_all_no", use_container_width=True):
                    st.session_state.ins_batch_approve_confirm = False
                    st.rerun()

        ins_pt = meta.get("project_type") if meta else None
        cfo_names_sorted = sorted(by_cfo.keys())
        if q:
            cfo_names_sorted = [k for k in cfo_names_sorted if q in k.lower()]
            if not cfo_names_sorted:
                st.caption("No students match your search.")
                return

        for cfo_name in cfo_names_sorted:
            cfo_orders = by_cfo[cfo_name]
            cfo_orders_sorted = sorted(cfo_orders, key=_instructor_order_sort_key)
            tn_first = (str(cfo_orders[0]["team_number_snapshot"] or "")).strip()
            n_cfo = len(cfo_orders)
            n_pend_cfo = sum(
                1 for o in cfo_orders if str(o["instructor_status"]) == STATUS_PENDING
            )
            item_word = "1 item" if n_cfo == 1 else f"{n_cfo} items"
            if _is_individual_project_type(ins_pt):
                exp_title = f"{cfo_name}  ·  {item_word}"
            else:
                exp_title = (
                    f"Team {tn_first or '—'} · {cfo_name}  ·  {item_word}"
                )
            if n_pend_cfo > 0:
                exp_title += f" · ⚠️ {n_pend_cfo} pending"
            with st.expander(exp_title, expanded=False):
                for o in cfo_orders_sorted:
                    row_id = int(o["id"])
                    inst = str(o["instructor_status"])
                    party = _order_party_display_label(
                        ins_pt, o["team_number_snapshot"], o["cfo_name_snapshot"]
                    )
                    st.markdown(
                        '<div style="border-left: 3px solid #4b2e83; padding: 12px 16px; margin: 8px 0; '
                        'background: #fafafa; border-radius: 0 8px 8px 0;">',
                        unsafe_allow_html=True,
                    )
                    st.markdown(status_badge_html(str(o["status"])), unsafe_allow_html=True)
                    st.markdown(
                        f"**{party}** · "
                        f"{o['provider_name_snapshot']} · Deadline **{o['deadline']}**"
                    )
                    st.markdown(
                        f"**{o['item_name']}** · Qty **{o['quantity']}** × **{o['unit_price']}** "
                        f"= **{float(o['total_price']):,.2f}**"
                    )
                    link_raw = (str(o["purchase_link"] or "")).strip()
                    if link_raw:
                        esc_href = html.escape(link_raw, quote=True)
                        st.markdown(
                            f'<a class="prt-link-pill" href="{esc_href}" target="_blank" rel="noopener noreferrer">'
                            "View link</a>",
                            unsafe_allow_html=True,
                        )
                    notes_show = _instructor_notes_meaningful(o["notes"])
                    if notes_show:
                        st.markdown(
                            f"**Notes:** {html.escape(notes_show)}",
                            unsafe_allow_html=True,
                        )

                    if inst == STATUS_PENDING:
                        reject_key = f"ins_rej_reason_{row_id}"
                        st.text_input(
                            "Rejection reason (required if you reject)",
                            key=reject_key,
                            placeholder="Explain why this line is rejected",
                        )
                        ca, cr = st.columns(2)
                        with ca:
                            if st.button(
                                "Approve",
                                key=f"ins_apr_{row_id}",
                                type="primary",
                                use_container_width=True,
                            ):
                                try:
                                    set_instructor_order_status(row_id, STATUS_APPROVED)
                                    if not _notify_student_instructor_approved(row_id):
                                        st.session_state.prt_email_toast = True
                                    st.success(f"Order {row_id} approved by instructor.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(str(e))
                        with cr:
                            if st.button(
                                "Reject",
                                key=f"ins_rej_{row_id}",
                                type="secondary",
                                use_container_width=True,
                            ):
                                reason = (st.session_state.get(reject_key) or "").strip()
                                if not reason:
                                    st.error("Enter a rejection reason before rejecting.")
                                else:
                                    try:
                                        set_instructor_order_status(
                                            row_id, STATUS_REJECTED, rejection_reason=reason
                                        )
                                        if not _notify_student_instructor_rejected(row_id):
                                            st.session_state.prt_email_toast = True
                                        st.warning(f"Order {row_id} rejected.")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(str(e))
                    else:
                        st.markdown(
                            _instructor_dual_status_row_html(inst, str(o["admin_status"])),
                            unsafe_allow_html=True,
                        )
                    st.markdown("</div>", unsafe_allow_html=True)


def _parse_iso_datetime(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _instructor_is_legacy_order(o) -> bool:
    """Orders submitted before instructor workflow: NULL or empty instructor_status."""
    rk = o.keys()
    if "instructor_status" not in rk:
        return True
    v = o["instructor_status"]
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def _clear_p1_form_keys() -> None:
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("p1_"):
            del st.session_state[k]
    st.session_state.p1_item_count = 1


def _p1_form_state_snapshot(is_individual: bool) -> tuple[Any, ...]:
    """Stable snapshot of student submit form for detecting edits after a successful submit."""
    n = int(st.session_state.get("p1_item_count") or 1)
    parts: list[Any] = [n]
    if not is_individual:
        parts.append(st.session_state.get("p1_team_number"))
    for i in range(n):
        parts.extend(
            [
                st.session_state.get(f"p1_provider_choice_{i}"),
                st.session_state.get(f"p1_provider_other_{i}"),
                st.session_state.get(f"p1_item_name_{i}"),
                st.session_state.get(f"p1_quantity_{i}"),
                st.session_state.get(f"p1_unit_price_{i}"),
                st.session_state.get(f"p1_purchase_link_{i}"),
                st.session_state.get(f"p1_notes_{i}"),
            ]
        )
    return tuple(parts)


def _student_class_orders_for_user(class_id: int, user_name: str) -> list[Any]:
    un = (user_name or "").strip()
    if not un:
        return []
    try:
        rows = list_orders(class_id=class_id, exclude_withdrawn=False)
    except Exception:
        return []
    out: list[Any] = []
    for o in rows:
        if "archived" in o.keys() and o["archived"]:
            continue
        cfo = (o["cfo_name_snapshot"] or "").strip() if "cfo_name_snapshot" in o.keys() else ""
        if cfo == un:
            out.append(o)
    return out


def _student_history_window_sort_value(o: Any) -> str:
    wdt = o["window_deadline_datetime"] if "window_deadline_datetime" in o.keys() else None
    dl = o["deadline"] if "deadline" in o.keys() else None
    cand = wdt or dl or ""
    return str(cand) if cand else ""


def _render_student_order_status_detail(o: Any) -> None:
    """Status messages below the badge (rejection reasons, pending info, success)."""
    rk = o.keys()
    legacy = _instructor_is_legacy_order(o)
    inst_raw = o["instructor_status"] if "instructor_status" in rk else None
    adm_raw = o["admin_status"] if "admin_status" in rk else None
    inst = (str(inst_raw).strip() if inst_raw is not None else "") if not legacy else ""
    adm = str(adm_raw).strip() if adm_raw is not None else ""
    stt = str(o["status"]) if "status" in rk else STATUS_PENDING

    if stt == STATUS_WITHDRAWN:
        st.caption("You withdrew this request.")
        return

    if stt == STATUS_LOST:
        return

    if not legacy and inst == STATUS_REJECTED:
        ir = ""
        if "instructor_rejection_reason" in rk and o["instructor_rejection_reason"]:
            ir = str(o["instructor_rejection_reason"]).strip()
        msg = "Instructor rejected this request."
        if ir:
            msg += f" Reason: {ir}"
        st.error(msg)
        return

    if adm == STATUS_REJECTED:
        rr = ""
        if "rejection_reason" in rk and o["rejection_reason"]:
            rr = str(o["rejection_reason"]).strip()
        msg = "Admin rejected this request."
        if rr:
            msg += f" Reason: {rr}"
        st.error(msg)
        return

    if stt == STATUS_APPROVED:
        st.success("✅ Fully approved")
        return

    if legacy:
        if stt == STATUS_PENDING:
            st.info("⏳ Awaiting admin review")
        return

    if inst == STATUS_PENDING or inst == "":
        st.info("⏳ Awaiting instructor review")
        return

    if inst == STATUS_APPROVED and adm == STATUS_PENDING:
        st.info("⏳ Instructor approved — awaiting admin review")
        return

    st.markdown(
        f'<p style="font-size:0.8rem;color:#6b7280;margin:0">Status: {html.escape(stt)}</p>',
        unsafe_allow_html=True,
    )


def _render_student_order_card(o: Any, class_id: int, user_name: str) -> None:
    rk = o.keys()
    oid = int(o["id"])
    item_nm = str(o["item_name"] or "")
    prov = str(o["provider_name_snapshot"] or "")
    qty = o["quantity"]
    unit_p = float(o["unit_price"])
    tot = float(o["total_price"])
    wlab = (o["window_label"] if "window_label" in rk and o["window_label"] else None) or ""
    wlab = wlab.strip() if wlab else "Legacy (no submission window)"
    link_raw = (str(o["purchase_link"] or "")).strip()
    stt = str(o["status"]) if "status" in rk else STATUS_PENDING

    qty_disp = html.escape(str(qty))
    un = (user_name or "").strip()
    cfo = (o["cfo_name_snapshot"] or "").strip() if "cfo_name_snapshot" in rk else ""
    mine = bool(un and cfo == un)

    with st.container(border=True):
        st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
        st.markdown(
            f'<p class="prt-order-card-title">{html.escape(item_nm)}</p>',
            unsafe_allow_html=True,
        )
        rep_id = None
        if "replacement_for_order_id" in rk and o["replacement_for_order_id"] is not None:
            try:
                rep_id = int(o["replacement_for_order_id"])
            except (TypeError, ValueError):
                rep_id = None
        if rep_id is not None:
            st.markdown(
                f'<p style="margin:0.15rem 0 0.35rem 0;font-size:0.82rem;color:#2563eb;font-weight:600">'
                f"🔄 Replacement for order #{rep_id}</p>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f'<p class="prt-order-card-meta">{html.escape(prov) if prov else "—"}</p>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<p class="prt-order-card-line">Qty {qty_disp} · '
            f'<span style="font-weight:600">${unit_p:,.2f}</span> each · Total: '
            f'<span style="font-weight:600">${tot:,.2f}</span></p>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<p class="prt-order-card-window">{html.escape(wlab)}</p>',
            unsafe_allow_html=True,
        )
        if link_raw:
            esc_href = html.escape(link_raw, quote=True)
            st.markdown(
                f'<a class="prt-link-pill" href="{esc_href}" target="_blank" rel="noopener noreferrer">'
                "View link</a>",
                unsafe_allow_html=True,
            )
        st.markdown(status_badge_html(stt), unsafe_allow_html=True)
        _render_student_order_status_detail(o)

        if mine and order_eligible_for_student_withdraw_or_edit(o):
            bc1, bc2 = st.columns(2)
            with bc1:
                if st.button("Edit", key=f"prt_stu_edit_{class_id}_{oid}"):
                    st.session_state[f"prt_open_edit_{oid}"] = True
            with bc2:
                if st.button("Withdraw Request", key=f"prt_stu_wd_{class_id}_{oid}"):
                    st.session_state[f"prt_confirm_wd_{oid}"] = True

        if mine and st.session_state.get(f"prt_confirm_wd_{oid}"):
            st.warning(
                "Are you sure you want to withdraw this request? This cannot be undone."
            )
            w1, w2 = st.columns(2)
            with w1:
                if st.button("Yes, withdraw", key=f"prt_stu_wdy_{class_id}_{oid}"):
                    try:
                        set_order_withdrawn(oid)
                        st.session_state[f"prt_confirm_wd_{oid}"] = False
                        st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
            with w2:
                if st.button("No", key=f"prt_stu_wdn_{class_id}_{oid}"):
                    st.session_state[f"prt_confirm_wd_{oid}"] = False
                    st.rerun()

        if (
            mine
            and st.session_state.get(f"prt_open_edit_{oid}")
            and order_eligible_for_student_withdraw_or_edit(o)
        ):
            prov_opts = sorted(set(SUBMIT_PROVIDER_OPTIONS + get_providers(class_id)))
            cur_prov = str(o["provider_name_snapshot"] or "").strip()
            if cur_prov and cur_prov not in prov_opts:
                prov_opts = [cur_prov] + prov_opts
            try:
                pidx = prov_opts.index(cur_prov) if cur_prov else 0
            except ValueError:
                pidx = 0
            with st.form(f"prt_stu_edit_form_{class_id}_{oid}"):
                st.caption("Edit your request (only before instructor review).")
                f_item = st.text_input(
                    "Item Name",
                    value=str(o["item_name"] or ""),
                    key=f"prt_ei_{class_id}_{oid}_name",
                )
                f_qty = st.number_input(
                    "Quantity",
                    min_value=0.0001,
                    value=float(o["quantity"] or 1.0),
                    format="%.4f",
                    key=f"prt_ei_{class_id}_{oid}_qty",
                )
                f_unit = st.number_input(
                    "Unit Price ($)",
                    min_value=0.0,
                    value=float(o["unit_price"] or 0.0),
                    format="%.2f",
                    key=f"prt_ei_{class_id}_{oid}_unit",
                )
                f_link = st.text_input(
                    "Purchase Link",
                    value=str(o["purchase_link"] or ""),
                    key=f"prt_ei_{class_id}_{oid}_link",
                )
                f_notes = st.text_area(
                    "Notes",
                    value=str(o["notes"] or ""),
                    key=f"prt_ei_{class_id}_{oid}_notes",
                    height=90,
                )
                f_prov = st.selectbox(
                    "Provider / Supplier",
                    options=prov_opts,
                    index=min(pidx, len(prov_opts) - 1) if prov_opts else 0,
                    key=f"prt_ei_{class_id}_{oid}_prov",
                )
                s1, s2 = st.columns(2)
                with s1:
                    save = st.form_submit_button("Save Changes")
                with s2:
                    cancel = st.form_submit_button("Cancel")
            if save:
                try:
                    update_order_details(
                        oid,
                        item_name=f_item,
                        quantity=float(f_qty),
                        unit_price=float(f_unit),
                        purchase_link=f_link,
                        notes=f_notes,
                        provider_name=str(f_prov or "").strip(),
                    )
                    st.session_state[f"prt_open_edit_{oid}"] = False
                    st.session_state["prt_my_order_updated_ok"] = True
                    st.rerun()
                except Exception as ex:
                    st.error(str(ex))
            if cancel:
                st.session_state[f"prt_open_edit_{oid}"] = False
                st.rerun()


def render_student_my_orders() -> None:
    section_header(
        "My Orders",
        "Purchase requests you submitted for the selected class, grouped by submission week.",
    )
    hr_divider()
    if st.session_state.pop("prt_my_order_updated_ok", False):
        st.success("Your request has been updated.")
    user_name = (st.session_state.get("user_name") or "").strip()
    active_class_id = int(st.session_state.active_class_id)
    orders = _student_class_orders_for_user(active_class_id, user_name)
    if not orders:
        st.info("You have not submitted any requests for this class yet.")
        return

    by_win: dict[str, list[Any]] = defaultdict(list)
    for o in orders:
        rk = o.keys()
        wlab = (o["window_label"] if "window_label" in rk and o["window_label"] else None) or ""
        wlab = wlab.strip() if wlab else "Legacy (no submission window)"
        by_win[wlab].append(o)

    def _grp_newest_key(label: str) -> str:
        grp = by_win[label]
        best = ""
        for x in grp:
            sv = _student_history_window_sort_value(x)
            if sv > best:
                best = sv
        return best

    sorted_labels = sorted(by_win.keys(), key=_grp_newest_key, reverse=True)
    first_week = True
    for lab in sorted_labels:
        if not first_week:
            st.markdown("<div style='height:0.75rem'></div>", unsafe_allow_html=True)
        first_week = False
        st.markdown(
            f'<p class="prt-week-heading">{html.escape(lab)}</p>',
            unsafe_allow_html=True,
        )
        for idx, o in enumerate(by_win[lab]):
            if idx:
                st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
            _render_student_order_card(o, active_class_id, user_name)


def _render_instructor_approval_cell(o, rk) -> None:
    legacy = _instructor_is_legacy_order(o)
    if legacy:
        st.markdown(
            '<span class="prt-badge prt-badge-legacy">N/A</span>',
            unsafe_allow_html=True,
        )
        return
    inst_raw = o["instructor_status"]
    inst = str(inst_raw).strip() if inst_raw is not None else ""
    if inst == STATUS_APPROVED:
        st.markdown(status_badge_html(STATUS_APPROVED), unsafe_allow_html=True)
    elif inst == STATUS_REJECTED:
        st.markdown(status_badge_html(STATUS_REJECTED), unsafe_allow_html=True)
        ir = ""
        if "instructor_rejection_reason" in rk and o["instructor_rejection_reason"]:
            ir = str(o["instructor_rejection_reason"]).strip()
        if ir:
            st.caption(ir)
    else:
        st.markdown(status_badge_html(STATUS_PENDING), unsafe_allow_html=True)


def _admin_format_link_cell(link: str) -> str:
    s = (link or "").strip()
    if not s:
        return "—"
    if s.lower().startswith(("http://", "https://")):
        esc = html.escape(s, quote=True)
        return f'<a href="{esc}" target="_blank" rel="noopener noreferrer">link</a>'
    return html.escape(s)


def _admin_notes_cell_display(notes_raw: str | None) -> str:
    s = (notes_raw or "").strip()
    if not s or s in ("/", "@@"):
        return "—"
    return html.escape(s)


_RECEIPT_FILE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".pdf"})


def _admin_sanitize_receipt_filename(name: str) -> str:
    raw = Path(name).name
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", raw) or "receipt"
    return safe


def _admin_store_receipt_file(order_id: int, uploaded_file: Any) -> str:
    safe = _admin_sanitize_receipt_filename(uploaded_file.name)
    suf = Path(safe).suffix.lower()
    if suf not in _RECEIPT_FILE_EXTS:
        raise ValueError("File must be .jpg, .jpeg, .png, or .pdf.")
    dest = get_receipts_dir() / f"{order_id}_{safe}"
    dest.write_bytes(uploaded_file.getvalue())
    return f"prt_data/receipts/{dest.name}"


def _admin_receipt_path_on_disk(stored: str | None) -> Path | None:
    if not stored or not str(stored).strip():
        return None
    p = Path(__file__).resolve().parent / str(stored).strip().replace("\\", "/")
    return p if p.is_file() else None


def _admin_resolve_provider_from_edit_widgets(
    key_prefix: str,
    row_id: int,
    *,
    choice_key: str,
    other_key: str,
) -> str:
    ch = st.session_state.get(choice_key, SUBMIT_PROVIDER_OPTIONS[0])
    if ch == "Others":
        return (st.session_state.get(other_key) or "").strip()
    return str(ch or "").strip()


def _admin_render_order_edit_form(
    key_prefix: str,
    row_id: int,
    o: Any,
) -> None:
    """Inline admin edit form; caller sets session edit_open flag."""
    supplier = str(o["provider_name_snapshot"] or "").strip()
    if supplier in SUBMIT_PROVIDER_OPTIONS:
        pidx = SUBMIT_PROVIDER_OPTIONS.index(supplier)
        other_default = ""
    else:
        pidx = SUBMIT_PROVIDER_OPTIONS.index("Others")
        other_default = supplier
    ck = f"{key_prefix}_adm_edit_pc_{row_id}"
    ok = f"{key_prefix}_adm_edit_po_{row_id}"
    with st.form(f"{key_prefix}_adm_edit_form_{row_id}"):
        st.caption(f"Edit order #{row_id}")
        st.text_input(
            "Item Name",
            value=str(o["item_name"] or ""),
            key=f"{key_prefix}_adm_edit_item_{row_id}",
        )
        st.text_input(
            "Quantity",
            value=str(o["quantity"] or ""),
            key=f"{key_prefix}_adm_edit_qty_{row_id}",
        )
        st.text_input(
            "Unit Price ($)",
            value=str(o["unit_price"] or ""),
            key=f"{key_prefix}_adm_edit_unit_{row_id}",
        )
        st.text_input(
            "Purchase Link",
            value=str(o["purchase_link"] or ""),
            key=f"{key_prefix}_adm_edit_link_{row_id}",
        )
        st.selectbox(
            "Provider / Supplier",
            options=SUBMIT_PROVIDER_OPTIONS,
            index=pidx,
            key=ck,
        )
        _prov_choice = st.session_state.get(ck, SUBMIT_PROVIDER_OPTIONS[pidx])
        if _prov_choice == "Others":
            st.text_input(
                "Custom provider name",
                value=other_default,
                key=ok,
            )
        st.text_area(
            "Notes",
            value=str(o["notes"] or ""),
            key=f"{key_prefix}_adm_edit_notes_{row_id}",
            height=72,
        )
        c1, c2 = st.columns(2)
        with c1:
            save = st.form_submit_button("Save Changes", type="primary", use_container_width=True)
        with c2:
            cancel = st.form_submit_button("Cancel", type="secondary", use_container_width=True)
    edit_key = f"{key_prefix}_edit_open_{row_id}"
    if save:
        try:
            prov = _admin_resolve_provider_from_edit_widgets(
                key_prefix,
                row_id,
                choice_key=ck,
                other_key=ok,
            )
            if st.session_state.get(ck, SUBMIT_PROVIDER_OPTIONS[pidx]) == "Others":
                _require_non_empty(prov, "Custom provider name")
            qty_d = parse_decimal(st.session_state.get(f"{key_prefix}_adm_edit_qty_{row_id}", "1"))
            price_d = parse_decimal(st.session_state.get(f"{key_prefix}_adm_edit_unit_{row_id}", "0"))
            if qty_d is None or qty_d <= 0:
                raise ValueError("Quantity must be a number greater than 0.")
            if price_d is None or price_d <= 0:
                raise ValueError("Unit price must be a number greater than 0.")
            admin_update_order_details(
                row_id,
                item_name=str(st.session_state.get(f"{key_prefix}_adm_edit_item_{row_id}") or ""),
                quantity=float(qty_d),
                unit_price=float(price_d),
                purchase_link=str(st.session_state.get(f"{key_prefix}_adm_edit_link_{row_id}") or ""),
                notes=str(st.session_state.get(f"{key_prefix}_adm_edit_notes_{row_id}") or ""),
                provider_name=prov,
            )
            st.session_state[edit_key] = False
            st.toast("Order updated successfully.")
            st.rerun()
        except Exception as e:
            st.error(str(e))
    if cancel:
        st.session_state[edit_key] = False
        st.rerun()


def _admin_render_receipt_upload_panel(key_prefix: str, row_id: int) -> None:
    up_key = f"{key_prefix}_receipt_up_{row_id}"
    st.caption("Upload a receipt (JPG, PNG, or PDF)")
    f = st.file_uploader(
        "Receipt file",
        type=["jpg", "jpeg", "png", "pdf"],
        key=f"{key_prefix}_rcpt_file_{row_id}",
    )
    b1, b2 = st.columns(2)
    with b1:
        if st.button("Save upload", key=f"{key_prefix}_rcpt_save_{row_id}", type="primary", use_container_width=True):
            if f is None:
                st.error("Choose a file first.")
            else:
                try:
                    rel = _admin_store_receipt_file(row_id, f)
                    save_receipt_path(row_id, rel)
                    st.session_state[up_key] = False
                    st.toast(f"Receipt uploaded for order {row_id}.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
    with b2:
        if st.button("Cancel", key=f"{key_prefix}_rcpt_cancel_{row_id}", use_container_width=True):
            st.session_state[up_key] = False
            st.rerun()


def _admin_resolve_current_window_label(class_id: int) -> str | None:
    try:
        ui = get_student_submission_window_ui_state(class_id)
    except Exception:
        ui = {}
    if ui.get("has_open_window") and ui.get("label"):
        lab = str(ui["label"]).strip()
        if lab:
            return lab
    try:
        all_w = list_submission_windows(int(class_id))
    except Exception:
        return None
    if not all_w:
        return None
    now = datetime.now().astimezone()
    future = [w for w in all_w if _parse_iso_datetime(w["deadline_datetime"]) >= now]
    if future:
        lab = str(future[0]["label"]).strip()
        return lab or None
    past = sorted(all_w, key=lambda w: w["deadline_datetime"], reverse=True)
    lab = str(past[0]["label"]).strip()
    return lab or None


def _admin_current_window_metrics(orders: list, current_label: str | None) -> dict[str, int | float]:
    total = 0
    pending = 0
    approved = 0
    spend = 0.0
    if not current_label:
        return {"total": 0, "pending": 0, "approved": 0, "spend": 0.0}
    target = current_label.strip()
    for o in orders:
        wlab = ""
        if "window_label" in o.keys() and o["window_label"] is not None:
            wlab = str(o["window_label"]).strip()
        if wlab != target:
            continue
        total += 1
        stt = str(o["status"]) if "status" in o.keys() else ""
        if stt == STATUS_APPROVED:
            approved += 1
            try:
                spend += float(o["total_price"] or 0)
            except (TypeError, ValueError):
                pass
        elif _admin_awaiting_admin_review(o):
            pending += 1
    return {"total": total, "pending": pending, "approved": approved, "spend": spend}


def _admin_orders_to_csv_bytes(orders: list, class_name: str) -> bytes:
    fieldnames = [
        "order_id",
        "class_name",
        "team_number",
        "cfo_name",
        "provider_name",
        "item_name",
        "quantity",
        "unit_price",
        "total_price",
        "purchase_link",
        "notes",
        "deadline",
        "status",
        "created_at",
        "approved_at",
        "rejected_at",
        "received_at",
        "return_flag",
        "return_reason",
        "archived",
    ]
    rows_out: list[dict[str, Any]] = []
    if not orders:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        return buf.getvalue().encode("utf-8")
    for o in orders:
        rows_out.append(
            {
                "order_id": o["id"],
                "class_name": class_name,
                "team_number": o["team_number_snapshot"],
                "cfo_name": o["cfo_name_snapshot"],
                "provider_name": o["provider_name_snapshot"],
                "item_name": o["item_name"],
                "quantity": o["quantity"],
                "unit_price": o["unit_price"],
                "total_price": o["total_price"],
                "purchase_link": o["purchase_link"],
                "notes": o["notes"],
                "deadline": o["deadline"],
                "status": o["status"],
                "created_at": o["created_at"],
                "approved_at": o["approved_at"],
                "rejected_at": o["rejected_at"],
                "received_at": o["received_at"],
                "return_flag": o["return_flag"],
                "return_reason": o["return_reason"],
                "archived": o["archived"],
            }
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows_out:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


def _admin_budget_summary_display_rows(budget_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in budget_summary:
        out.append(
            {
                "Student / Team": r["team_number"],
                "Total Budget ($)": f"${float(r['budget_total']):,.2f}",
                "Used ($)": f"${float(r['used_amount']):,.2f}",
                "Remaining ($)": f"${float(r['remaining_amount']):,.2f}",
                "Orders": int(r["total_orders"]),
                "Pending": int(r["pending_count"]),
            }
        )
    return out


def _admin_scroll_to_anchor_script(anchor_id: str) -> None:
    """Scroll main document to an element id (Admin Dashboard jump control)."""
    components.html(
        f"""
<script>
(function() {{
  const root = window.parent.document;
  const el = root.querySelector("#{anchor_id}");
  if (el) el.scrollIntoView({{ behavior: "smooth", block: "start" }});
}})();
</script>
        """,
        height=0,
        width=0,
    )


_HAS_ST_DIALOG = hasattr(st, "dialog") and callable(getattr(st, "dialog", None))


def _lost_package_modal_inner(order_id: int, item_name: str, team_name: str) -> None:
    st.warning("⚠️ You are about to mark this order as lost:")
    st.markdown(f"**Item:** {html.escape(item_name)}")
    st.markdown(f"**Team:** {html.escape(team_name)}")
    st.markdown(f"**Order ID:** #{order_id}")
    st.divider()
    st.markdown("This will:")
    st.markdown("- Mark the original order as **Lost**")
    st.markdown("- Create a **new replacement order** starting from pending approval")
    if st.button(
        "✅ Yes, mark as lost & reorder",
        type="primary",
        use_container_width=True,
        key=f"prt_lost_dlg_y_{order_id}",
    ):
        try:
            new_id = mark_lost_and_create_replacement(order_id)
            st.session_state.pop("prt_modal_lost", None)
            st.session_state["prt_lost_toast_payload"] = (order_id, new_id)
            st.rerun()
        except Exception as e:
            st.error(str(e))
    if st.button("Cancel", use_container_width=True, key=f"prt_lost_dlg_n_{order_id}"):
        st.session_state.pop("prt_modal_lost", None)
        st.rerun()


if _HAS_ST_DIALOG:

    @st.dialog("Lost Package Confirmation")
    def open_lost_package_dialog(order_id: int, item_name: str, team_name: str) -> None:
        _lost_package_modal_inner(order_id, item_name, team_name)

else:
    open_lost_package_dialog = None  # type: ignore[misc, assignment]


def _sw_deactivate_modal_inner(window_id: int, label: str, deadline_display: str) -> None:
    st.warning("Are you sure? Students won't be able to submit to this window.")
    st.markdown(f"**Window:** {html.escape(label)}")
    st.markdown(f"**Deadline:** {html.escape(deadline_display)}")
    st.divider()
    if st.button(
        "Confirm Deactivate",
        type="primary",
        use_container_width=True,
        key=f"prt_sw_deact_y_{window_id}",
    ):
        try:
            set_submission_window_active(window_id, False)
            st.session_state.pop("prt_modal_sw_deact", None)
            st.rerun()
        except Exception as e:
            st.error(str(e))
    if st.button("Cancel", use_container_width=True, key=f"prt_sw_deact_n_{window_id}"):
        st.session_state.pop("prt_modal_sw_deact", None)
        st.rerun()


if _HAS_ST_DIALOG:

    @st.dialog("Deactivate submission window?")
    def open_deactivate_window_dialog(window_id: int, label: str, deadline_display: str) -> None:
        _sw_deactivate_modal_inner(window_id, label, deadline_display)

else:
    open_deactivate_window_dialog = None  # type: ignore[misc, assignment]


def _render_admin_fallback_modal_overlays() -> None:
    """When st.dialog is unavailable, show centered card-style confirmations at full width."""
    if _HAS_ST_DIALOG:
        return
    lost = st.session_state.get("prt_modal_lost")
    sw = st.session_state.get("prt_modal_sw_deact")
    if not lost and not sw:
        return
    st.markdown(
        """
<style>
section[data-testid="stMain"] div[data-testid="stVerticalBlock"]:has(.prt-admin-modal-fallback-mark) {
  position: relative;
  z-index: 1000;
  padding: 1rem 0 1.25rem 0;
  margin-bottom: 0.5rem;
  background: linear-gradient(180deg, rgba(15,23,42,0.06) 0%, rgba(15,23,42,0) 100%);
  border-radius: 12px;
}
section[data-testid="stMain"] div[data-testid="stVerticalBlock"]:has(.prt-admin-modal-fallback-mark)
  div[data-testid="stVerticalBlockBorderWrapper"] > div {
  border-radius: 12px !important;
  box-shadow: 0 18px 40px rgba(15, 23, 42, 0.12) !important;
  border: 1px solid #e5e7eb !important;
  max-width: 480px;
  margin-left: auto !important;
  margin-right: auto !important;
}
</style>
""",
        unsafe_allow_html=True,
    )
    modal_ph = st.empty()
    with modal_ph.container():
        st.markdown('<span class="prt-admin-modal-fallback-mark"></span>', unsafe_allow_html=True)
        if lost:
            with st.container(border=True):
                st.markdown("##### Lost Package Confirmation")
                _lost_package_modal_inner(
                    int(lost["order_id"]),
                    str(lost["item_name"]),
                    str(lost["team_name"]),
                )
        elif sw:
            with st.container(border=True):
                st.markdown("##### Deactivate submission window?")
                _sw_deactivate_modal_inner(
                    int(sw["window_id"]),
                    str(sw["label"]),
                    str(sw["deadline_display"]),
                )


def _render_admin_orders_table(
    orders_list: list,
    key_prefix: str,
    project_type: str | None,
    *,
    show_column_headers: bool = True,
) -> None:
    if not orders_list:
        st.caption("No orders in this group.")
        return

    col_weights = [1.2, 1.4, 0.8, 1.0, 1.0, 0.5, 1.0, 0.8, 0.8, 0.75, 1.25]
    headers = [
        "Student Name",
        "Item Name",
        "Quantity",
        "Price",
        "Supplier",
        "Link",
        "Notes",
        "Instructor",
        "Status",
        "Workday",
        "Actions",
    ]
    with st.container():
        st.markdown('<span class="prt-admin-table-region"></span>', unsafe_allow_html=True)
        if show_column_headers:
            header_cols = st.columns(col_weights)
            for i, label in enumerate(headers):
                with header_cols[i]:
                    st.markdown(
                        f'<p class="prt-admin-th">{html.escape(label)}</p>',
                        unsafe_allow_html=True,
                    )
            st.markdown(
                '<hr class="prt-admin-table-head-rule" />',
                unsafe_allow_html=True,
            )

        for o in orders_list:
            rk = o.keys()
            row_id = int(o["id"])
            status = str(o["status"])
            received_at = o["received_at"] if "received_at" in rk else None
            legacy_inst = _instructor_is_legacy_order(o)
            inst_raw = o["instructor_status"] if "instructor_status" in rk else None
            inst = (str(inst_raw).strip() if inst_raw is not None else "") if not legacy_inst else ""
            party = _order_party_display_label(
                project_type, o["team_number_snapshot"], o["cfo_name_snapshot"]
            )
            item_nm = str(o["item_name"] or "")
            qty = o["quantity"]
            unit_p = float(o["unit_price"])
            tot_p = float(o["total_price"])
            supplier = str(o["provider_name_snapshot"] or "")
            link_raw = str(o["purchase_link"] or "")
            notes = str(o["notes"] or "")
            needs_instructor_before_admin = (
                not legacy_inst and (inst or "") == STATUS_PENDING
            )
            recv_open_key = f"{key_prefix}_recv_open_{row_id}"
            edit_open_key = f"{key_prefix}_edit_open_{row_id}"
            receipt_up_key = f"{key_prefix}_receipt_up_{row_id}"
            receipt_stored = ""
            if "receipt_path" in rk and o["receipt_path"]:
                receipt_stored = str(o["receipt_path"]).strip()

            cols = st.columns(col_weights)
            qty_s = html.escape(str(qty))
            qty_int = int(qty) if float(str(qty)) == int(float(str(qty))) else qty
            price_line = (
                f'<p class="prt-admin-td" style="margin:0;font-size:0.82rem;color:#6b7280">'
                f"${unit_p:,.2f} × {qty_int}</p>"
                f'<p class="prt-admin-td" style="margin:0;font-weight:700;color:#111827">'
                f"${tot_p:,.2f}</p>"
            )
            with cols[0]:
                st.markdown(
                    f'<p class="prt-admin-td" style="margin:0">{html.escape(party)}</p>',
                    unsafe_allow_html=True,
                )
            with cols[1]:
                st.markdown(
                    f'<p class="prt-admin-td" style="margin:0">{html.escape(item_nm)}</p>',
                    unsafe_allow_html=True,
                )
            with cols[2]:
                st.markdown(
                    f'<p class="prt-admin-td" style="margin:0">{qty_s}</p>',
                    unsafe_allow_html=True,
                )
            with cols[3]:
                st.markdown(price_line, unsafe_allow_html=True)
            with cols[4]:
                st.markdown(
                    f'<p class="prt-admin-td" style="margin:0">{html.escape(supplier)}</p>',
                    unsafe_allow_html=True,
                )
            with cols[5]:
                st.markdown(
                    f'<div class="prt-admin-td">{_admin_format_link_cell(link_raw)}</div>',
                    unsafe_allow_html=True,
                )
            with cols[6]:
                st.markdown(
                    f'<div class="prt-admin-td">{_admin_notes_cell_display(notes)}</div>',
                    unsafe_allow_html=True,
                )
            with cols[7]:
                _render_instructor_approval_cell(o, rk)
            with cols[8]:
                st.markdown(status_badge_html(status), unsafe_allow_html=True)
            wv_done = int(o["workday_verified"] or 0) if "workday_verified" in rk else 0
            with cols[9]:
                if received_at:
                    if wv_done:
                        st.markdown(
                            '<p class="prt-admin-td" style="margin:0;color:#15803d;font-weight:600;font-size:0.82rem">'
                            "✅ Verified</p>",
                            unsafe_allow_html=True,
                        )
                        wva = o["workday_verified_at"] if "workday_verified_at" in rk else None
                        if wva:
                            st.caption(
                                f"✅ Workday verified on {_format_workday_verified_at(str(wva))}"
                            )
                    else:
                        st.markdown(
                            '<p class="prt-admin-td" style="margin:0;color:#d97706;font-weight:600;font-size:0.82rem">'
                            "⏳ Pending</p>",
                            unsafe_allow_html=True,
                        )
                else:
                    st.markdown(
                        '<p class="prt-admin-td" style="margin:0;color:#9ca3af">—</p>',
                        unsafe_allow_html=True,
                    )
            with cols[10]:
                if status in (STATUS_PENDING, STATUS_PROCESSING):
                    appr_disabled = needs_instructor_before_admin
                    if st.button(
                        "Approve",
                        key=f"{key_prefix}_ap_{row_id}",
                        type="primary",
                        use_container_width=True,
                        disabled=appr_disabled,
                        help=(
                            "Waiting for instructor approval"
                            if appr_disabled
                            else None
                        ),
                    ):
                        st.session_state.pop(f"{key_prefix}_reject_open_{row_id}", None)
                        try:
                            set_order_status(row_id, STATUS_APPROVED)
                            if not _notify_student_admin_approved(row_id):
                                st.session_state.prt_email_toast = True
                            st.success(f"Order {row_id} approved.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
                    if st.button(
                        "Reject",
                        key=f"{key_prefix}_rj_{row_id}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        st.session_state[f"{key_prefix}_reject_open_{row_id}"] = True
                        st.rerun()
                elif status == STATUS_APPROVED and not received_at:
                    if not st.session_state.get(recv_open_key, False):
                        if st.button(
                            "Mark Received",
                            key=f"{key_prefix}_recv_{row_id}",
                            type="secondary",
                            use_container_width=True,
                        ):
                            st.session_state[recv_open_key] = True
                            st.rerun()
                    else:
                        st.caption("Continue below ↓")
                else:
                    if not (status == STATUS_APPROVED and received_at):
                        st.markdown(
                            '<span style="color:#9ca3af;font-weight:500">No action</span>',
                            unsafe_allow_html=True,
                        )

                if status == STATUS_APPROVED and not received_at:
                    rp_disk = _admin_receipt_path_on_disk(receipt_stored or None)
                    if rp_disk is not None:
                        st.markdown(
                            '<p style="color:#15803d;font-size:0.78rem;margin:6px 0 2px 0;font-weight:600">'
                            "📎 Receipt on file</p>",
                            unsafe_allow_html=True,
                        )
                        with open(rp_disk, "rb") as _rf:
                            _rdata = _rf.read()
                        st.download_button(
                            label="Download receipt",
                            data=_rdata,
                            file_name=rp_disk.name,
                            mime="application/octet-stream",
                            key=f"{key_prefix}_rcpt_dl_{row_id}",
                            use_container_width=True,
                        )
                    if st.button(
                        "Upload Receipt",
                        key=f"{key_prefix}_receipt_up_btn_{row_id}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        st.session_state[receipt_up_key] = True
                        st.rerun()
                    if _HAS_ST_DIALOG and open_lost_package_dialog is not None:
                        if st.button(
                            "📦 Lost Package",
                            key=f"{key_prefix}_lost_pkg_{row_id}",
                            type="secondary",
                            use_container_width=True,
                        ):
                            open_lost_package_dialog(row_id, item_nm, party)
                    else:
                        if st.button(
                            "📦 Lost Package",
                            key=f"{key_prefix}_lost_pkg_{row_id}",
                            type="secondary",
                            use_container_width=True,
                        ):
                            st.session_state["prt_modal_lost"] = {
                                "order_id": row_id,
                                "item_name": item_nm,
                                "team_name": party,
                            }
                            st.rerun()

                if received_at and not wv_done:
                    if st.button(
                        "✓ Mark Workday Verified",
                        key=f"{key_prefix}_wdy_{row_id}",
                        type="primary",
                        use_container_width=True,
                    ):
                        try:
                            dname = (st.session_state.get("user_name") or "").strip() or "Admin"
                            set_workday_verified(row_id, dname)
                            st.success(f"Order {row_id} marked Workday verified.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

                if not received_at and status != STATUS_LOST:
                    if st.button(
                        "Edit",
                        key=f"{key_prefix}_edit_btn_{row_id}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        st.session_state[edit_open_key] = True
                        st.rerun()

            rej_open_key = f"{key_prefix}_reject_open_{row_id}"
            if st.session_state.get(rej_open_key, False):
                st.text_input(
                    "Rejection reason (required)",
                    key=f"{key_prefix}_rej_reason_{row_id}",
                    placeholder="Explain why this order is rejected",
                )
                if st.button(
                    "Confirm reject",
                    key=f"{key_prefix}_rej_go_{row_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    reason = (st.session_state.get(f"{key_prefix}_rej_reason_{row_id}") or "").strip()
                    if not reason:
                        st.error("Enter a rejection reason.")
                    else:
                        try:
                            set_order_status(row_id, STATUS_REJECTED, rejection_reason=reason)
                            if not _notify_student_admin_rejected(row_id):
                                st.session_state.prt_email_toast = True
                            st.session_state[rej_open_key] = False
                            st.warning(f"Order {row_id} rejected.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
                if st.button(
                    "Cancel",
                    key=f"{key_prefix}_rej_x_{row_id}",
                    use_container_width=True,
                ):
                    st.session_state[rej_open_key] = False
                    st.rerun()

            if (
                st.session_state.get(edit_open_key)
                and not received_at
                and status != STATUS_LOST
            ):
                with st.container(border=True):
                    _admin_render_order_edit_form(key_prefix, row_id, o)
            elif st.session_state.get(edit_open_key):
                st.session_state[edit_open_key] = False

            if (
                status == STATUS_APPROVED
                and not received_at
                and st.session_state.get(receipt_up_key)
            ):
                with st.container(border=True):
                    _admin_render_receipt_upload_panel(key_prefix, row_id)

            if status == STATUS_APPROVED and not received_at and st.session_state.get(recv_open_key, False):
                with st.expander("Receive this order — optional return flag", expanded=True):
                    st.file_uploader(
                        "Attach receipt (optional)",
                        type=["jpg", "jpeg", "png", "pdf"],
                        key=f"{key_prefix}_recv_rcpt_{row_id}",
                    )
                    st.checkbox(
                        "Flag for return?",
                        key=f"{key_prefix}_ret_flag_{row_id}",
                    )
                    if st.session_state.get(f"{key_prefix}_ret_flag_{row_id}", False):
                        st.text_input(
                            "Return reason (required if flagged)",
                            key=f"{key_prefix}_ret_reason_{row_id}",
                        )
                    b1, b2 = st.columns(2)
                    with b1:
                        if st.button(
                            "Confirm received",
                            key=f"{key_prefix}_recv_confirm_{row_id}",
                            type="primary",
                            use_container_width=True,
                        ):
                            rf = bool(st.session_state.get(f"{key_prefix}_ret_flag_{row_id}", False))
                            rr = st.session_state.get(f"{key_prefix}_ret_reason_{row_id}")
                            recv_file = st.session_state.get(f"{key_prefix}_recv_rcpt_{row_id}")
                            if rf and (rr is None or not str(rr).strip()):
                                st.error("Please provide a return reason when flagging for return.")
                            else:
                                try:
                                    if recv_file is not None:
                                        rel = _admin_store_receipt_file(row_id, recv_file)
                                        save_receipt_path(row_id, rel)
                                    mark_received(
                                        order_id=row_id,
                                        return_flag=rf,
                                        return_reason=str(rr).strip() if rr else None,
                                    )
                                    st.session_state[recv_open_key] = False
                                    st.success(f"Order {row_id} marked as received.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(str(e))
                    with b2:
                        if st.button(
                            "Cancel",
                            key=f"{key_prefix}_recv_cancel_{row_id}",
                            use_container_width=True,
                        ):
                            st.session_state[recv_open_key] = False
                            st.rerun()


def render_admin_dashboard() -> None:
    if st.session_state.pop("prt_email_toast", False):
        st.toast(EMAIL_FAIL_TOAST, icon="⚠️")
    lost_toast = st.session_state.pop("prt_lost_toast_payload", None)
    if lost_toast is not None:
        _oid, _nid = lost_toast
        st.toast(
            f"Order {_oid} marked as lost. Replacement order #{_nid} created and pending approval."
        )
    _render_admin_fallback_modal_overlays()
    section_header(
        "Admin Dashboard",
        "Dorothy — review weekly orders, approve or reject, and mark items as received.",
    )
    hr_divider()

    anchor_id = "prt-admin-current-week"
    do_scroll = st.session_state.pop("_prt_admin_scroll_week", False)

    active_class_id = st.session_state.active_class_id

    try:
        orders = list_orders(
            class_id=active_class_id,
            team_number=None,
            status=None,
            provider_name=None,
            deadline_start=None,
            deadline_end=None,
            window_label=None,
        )
    except Exception:
        orders = []

    orders = [o for o in orders if _admin_order_visible_for_dashboard(o)]
    admin_pt = get_class_project_type(int(active_class_id))
    current_label = _admin_resolve_current_window_label(int(active_class_id))
    metrics = _admin_current_window_metrics(orders, current_label)

    pnd = int(metrics["pending"])
    st.markdown(
        f"""
<style>
section[data-testid="stMain"] div[data-testid="stHorizontalBlock"]:has(.prt-admin-metrics-anchor)
  div[data-testid="column"]:nth-child(2) [data-testid="stMetricValue"] {{
  color: {'#d97706' if pnd > 0 else '#9ca3af'} !important;
  font-size: {'1.45rem' if pnd > 0 else '1.05rem'} !important;
  font-weight: {'700' if pnd > 0 else '500'} !important;
}}
section[data-testid="stMain"] div[data-testid="stHorizontalBlock"]:has(.prt-admin-metrics-anchor)
  div[data-testid="column"]:nth-child(2) [data-testid="stMetricLabel"] {{
  color: {'#b45309' if pnd > 0 else '#9ca3af'} !important;
  font-size: {'0.95rem' if pnd > 0 else '0.78rem'} !important;
}}
section[data-testid="stMain"] div[data-testid="stHorizontalBlock"]:has(.prt-admin-metrics-anchor)
  div[data-testid="column"]:nth-child(2) [data-testid="stMetricLabel"] p {{
  color: inherit !important;
}}
</style>
""",
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(
            '<span class="prt-admin-metrics-anchor" style="display:none"></span>',
            unsafe_allow_html=True,
        )
        st.metric("📋 This Week's Orders", metrics["total"])
    with m2:
        st.metric(
            "⚠️ Pending Approval" if pnd > 0 else "Pending Approval",
            metrics["pending"],
        )
    with m3:
        st.metric("✅ Approved", metrics["approved"])
    with m4:
        st.metric("💰 Total Spend", f"${metrics['spend']:,.2f}")

    if st.button("Jump to this week ↓", type="primary", key="prt_admin_jump_week"):
        st.session_state["_prt_admin_scroll_week"] = True

    with st.expander("⚙️ Manage Submission Windows (click to expand)", expanded=False):
        st.markdown("##### Submission Windows")
        st.caption(
            "Weekly deadlines are every Monday at 1:00 PM (local time). "
            "Deactivate a window to stop submissions toward it; students then roll to the next Monday."
        )
        try:
            all_windows = list_submission_windows(int(active_class_id))
        except Exception:
            all_windows = []
        now_local = datetime.now().astimezone()
        upcoming = [
            w
            for w in all_windows
            if _parse_iso_datetime(w["deadline_datetime"]) >= now_local
        ]
        upcoming.sort(key=lambda w: w["deadline_datetime"])

        if not upcoming:
            st.info(
                "No upcoming submission windows. Add a custom window below or register windows when creating the class."
            )
        else:
            for idx, w in enumerate(upcoming):
                if idx > 0:
                    st.markdown(
                        '<div style="border-top:1px solid #f3f4f6;margin:0.35rem 0 0.5rem 0"></div>',
                        unsafe_allow_html=True,
                    )
                ddl = _parse_iso_datetime(w["deadline_datetime"])
                ddl_line = _format_window_datetime_line(ddl)
                c1, c2, c3 = st.columns([2, 3, 2])
                with c1:
                    st.markdown(f"**{w['label']}**")
                with c2:
                    st.markdown(
                        f'<p style="margin:0;color:#6b7280;font-size:0.82rem">{html.escape(ddl_line)}</p>',
                        unsafe_allow_html=True,
                    )
                with c3:
                    active = bool(w["is_active"])
                    if active:
                        if _HAS_ST_DIALOG and open_deactivate_window_dialog is not None:
                            if st.button(
                                "Deactivate",
                                key=f"sw_deact_{w['id']}",
                                type="secondary",
                            ):
                                open_deactivate_window_dialog(
                                    int(w["id"]),
                                    str(w["label"]),
                                    ddl_line,
                                )
                        else:
                            if st.button(
                                "Deactivate",
                                key=f"sw_deact_{w['id']}",
                                type="secondary",
                            ):
                                st.session_state["prt_modal_sw_deact"] = {
                                    "window_id": int(w["id"]),
                                    "label": str(w["label"]),
                                    "deadline_display": ddl_line,
                                }
                                st.rerun()
                    else:
                        if st.button("Activate", key=f"sw_act_{w['id']}", type="primary"):
                            try:
                                set_submission_window_active(int(w["id"]), True)
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

        with st.form("admin_add_custom_window"):
            st.markdown("**Add custom deadline**")
            ac1, ac2 = st.columns(2)
            with ac1:
                ad = st.date_input("Date", key="adm_sw_date")
            with ac2:
                at = st.time_input("Time", value=time(13, 0), key="adm_sw_time")
            alab = st.text_input(
                "Label (optional)", key="adm_sw_label", placeholder='e.g. "Week of Apr 6"'
            )
            add_sub = st.form_submit_button("Add submission window", type="primary")
            if add_sub:
                try:
                    tz = datetime.now().astimezone().tzinfo
                    adt = datetime.combine(ad, at).replace(tzinfo=tz)
                    add_custom_submission_window(
                        int(active_class_id),
                        adt,
                        alab.strip() or None,
                    )
                    st.success("Submission window added.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.markdown("##### Email Settings")
        st.caption(
            "Use a Gmail **App Password**, not your regular Gmail password. "
            "App passwords require 2-Step Verification on the Google account. "
            "Create one here: https://myaccount.google.com/apppasswords"
        )
        if "adm_email_settings_loaded" not in st.session_state:
            _es0 = get_email_settings()
            st.session_state.adm_email_user = _es0["smtp_user"]
            st.session_state.adm_email_sender = _es0["sender_name"]
            st.session_state.adm_email_enabled = _es0["enabled"]
            st.session_state.adm_email_settings_loaded = True
        st.text_input("Gmail address", key="adm_email_user", autocomplete="email")
        st.text_input(
            "Gmail App Password",
            type="password",
            key="adm_email_pw",
            help="16-character app password from Google Account → Security → App passwords",
        )
        st.caption(
            "Leave the app password field blank to keep the saved password unchanged when you click Save settings."
        )
        st.text_input("Sender name", key="adm_email_sender", placeholder="e.g. GIX Purchase Requests")
        st.toggle("Enable email notifications", key="adm_email_enabled")
        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("Save settings", type="primary", key="adm_email_save"):
                try:
                    pw_raw = st.session_state.get("adm_email_pw") or ""
                    save_email_settings(
                        str(st.session_state.get("adm_email_user") or ""),
                        str(st.session_state.get("adm_email_sender") or ""),
                        bool(st.session_state.get("adm_email_enabled")),
                        smtp_password=str(pw_raw).strip() or None,
                    )
                    _es2 = get_email_settings()
                    st.session_state.adm_email_user = _es2["smtp_user"]
                    st.session_state.adm_email_sender = _es2["sender_name"]
                    st.session_state.adm_email_enabled = _es2["enabled"]
                    st.session_state.adm_email_pw = ""
                    st.success("Email settings saved.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        with bc2:
            if st.button("Send test email", key="adm_email_test"):
                u = (st.session_state.get("adm_email_user") or "").strip()
                pw_typed = (st.session_state.get("adm_email_pw") or "").strip()
                sn = (st.session_state.get("adm_email_sender") or "").strip()
                if not u:
                    st.error("Enter a Gmail address before sending a test.")
                else:
                    ok = send_test_email(
                        u,
                        smtp_user=u,
                        app_password=pw_typed,
                        sender_name=sn,
                    )
                    if ok:
                        st.success(f"Test email sent to {u}. Check your inbox.")
                    else:
                        st.error("Could not send test email. Check address, app password, and network.")

    hr_divider()

    budget_summary = get_budget_summary_by_team(active_class_id)
    budget_display = _admin_budget_summary_display_rows(budget_summary)
    with st.expander("Budget summary per team", expanded=False):
        st.dataframe(budget_display, use_container_width=True)

    hr_divider()

    st.markdown("##### Orders by submission week")

    if not orders:
        st.info("No purchase requests for this class yet.")
        if do_scroll:
            _admin_scroll_to_anchor_script(anchor_id)
        return

    meta_class = get_class_by_id(int(active_class_id)) or {}
    class_name_export = str(meta_class.get("name") or "class")

    by_window: dict[str, list] = defaultdict(list)
    for o in orders:
        wlab = (o["window_label"] if "window_label" in o.keys() and o["window_label"] else None) or ""
        wlab = wlab.strip() if wlab else ""
        group_key = wlab if wlab else "Legacy (no submission window)"
        by_window[group_key].append(o)

    def _window_group_sort_key(k: str) -> str:
        grp = by_window[k]
        best: str | None = None
        for o in grp:
            wdt = o["window_deadline_datetime"] if "window_deadline_datetime" in o.keys() else None
            cand = (wdt or o["deadline"]) or ""
            if isinstance(cand, str) and (best is None or cand < best):
                best = cand
        return best or ""

    def _admin_label_match(a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        return a.strip() == b.strip()

    legacy_key = "Legacy (no submission window)"
    non_legacy_keys = [k for k in by_window.keys() if k != legacy_key]
    sorted_week_labels = sorted(non_legacy_keys, key=_window_group_sort_key)
    has_legacy = bool(legacy_key in by_window and by_window[legacy_key])

    anchor_done = False
    if current_label and not any(_admin_label_match(current_label, k) for k in by_window.keys()):
        st.markdown(
            f'<div id="{anchor_id}" style="scroll-margin-top:72px;"></div>',
            unsafe_allow_html=True,
        )
        anchor_done = True
        st.markdown(f"**{html.escape(current_label)}**")
        st.caption("No orders for this submission window yet.")
        csv_bytes = _admin_orders_to_csv_bytes([], class_name_export)
        st.download_button(
            label="📥 Export this week as CSV",
            data=csv_bytes,
            file_name=f"purchase_requests_week_{date.today().isoformat()}.csv",
            mime="text/csv",
            type="secondary",
        )
        hr_divider()

    for idx, win_label in enumerate(sorted_week_labels):
        win_orders = by_window[win_label]
        match_current = bool(current_label and _admin_label_match(current_label, win_label))
        fallback_first = current_label is None and idx == 0
        if not anchor_done and (match_current or fallback_first):
            st.markdown(
                f'<div id="{anchor_id}" style="scroll-margin-top:72px;"></div>',
                unsafe_allow_html=True,
            )
            anchor_done = True
        st.markdown(f"**{win_label}**")
        _render_admin_orders_table(win_orders, f"adm_wk_{idx}", admin_pt)
        if match_current:
            csv_b = _admin_orders_to_csv_bytes(win_orders, class_name_export)
            st.download_button(
                label="📥 Export this week as CSV",
                data=csv_b,
                file_name=f"purchase_requests_week_{date.today().isoformat()}.csv",
                mime="text/csv",
                type="secondary",
            )
        if idx < len(sorted_week_labels) - 1 or has_legacy:
            hr_divider()

    if legacy_key in by_window and by_window[legacy_key]:
        leg_orders = by_window[legacy_key]
        st.caption("Legacy orders (submitted before window system)")
        _render_admin_orders_table(
            leg_orders, "adm_legacy", admin_pt, show_column_headers=False
        )

    if do_scroll:
        _admin_scroll_to_anchor_script(anchor_id)


def render_summary_report() -> None:
    section_header(
        "Summary & Report",
        "Course-level totals, exports, and archived history for the selected class.",
    )
    hr_divider()

    active_class_id = st.session_state.active_class_id

    inner_tabs = st.tabs(["Summary & Export", "Archive View"])

    with inner_tabs[0]:
        rep = _compute_course_summary_report(int(active_class_id))

        with st.container(border=True):
            st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
            st.markdown("##### Course summary report")
            st.caption(
                "Based on non-archived orders for this class. "
                f"“Close to budget” means ≥ {_CLOSE_TO_BUDGET_RATIO:.0%} of team budget (approved spend)."
            )

            m1, m2, m3 = st.columns(3)
            m1.metric(
                "Average spend per student",
                f"{rep['avg_spend_per_team']:,.2f}",
                help="Approved spend divided by number of teams (each team has one CFO/student contact).",
            )
            m2.metric("Approved requests (lines)", f"{rep['approved_count']}")
            m3.metric("Rejected requests (lines)", f"{rep['rejected_count']}")

            pwk = count_pending_workday_verification(int(active_class_id))
            st.markdown(
                f"""
<style>
section[data-testid="stMain"] div[data-testid="stHorizontalBlock"]:has(.prt-summary-pwk-anchor)
  div[data-testid="column"]:only-child [data-testid="stMetricValue"] {{
  color: {'#d97706' if pwk > 0 else '#1f2937'} !important;
  font-weight: {'700' if pwk > 0 else '400'} !important;
}}
section[data-testid="stMain"] div[data-testid="stHorizontalBlock"]:has(.prt-summary-pwk-anchor)
  div[data-testid="column"]:only-child [data-testid="stMetricLabel"] p {{
  color: {'#b45309' if pwk > 0 else '#6b7280'} !important;
}}
</style>
""",
                unsafe_allow_html=True,
            )
            pw_col = st.columns(1)
            with pw_col[0]:
                st.markdown(
                    '<span class="prt-summary-pwk-anchor" style="display:none"></span>',
                    unsafe_allow_html=True,
                )
                st.metric("Pending Workday verification", f"{pwk} orders")

            st.markdown("**Total approved spend vs course budget**")
            cb = rep["course_budget"]
            ts = rep["total_spend"]
            if cb is not None and cb > 0:
                ratio = min(1.0, max(0.0, ts / cb))
                st.progress(ratio)
                pct = rep["pct_of_course"]
                st.caption(
                    f"{ts:,.2f} of {cb:,.2f} "
                    f"({pct:.1f}% of course budget)" if pct is not None else f"{ts:,.2f} of {cb:,.2f}"
                )
            else:
                st.info(
                    "No course-level budget is set for this class, or budget is zero. "
                    "Set **Total course budget** when registering the course (Instructor view) to see the bar."
                )

            st.markdown("**Teams over budget (approved spend)**")
            if rep["exceeded_rows"]:
                for label, used, budget, remaining in rep["exceeded_rows"]:
                    st.markdown(
                        f'<p style="color:#b91c1c;margin:0.25rem 0;font-weight:600">'
                        f"{html.escape(label)} — used {used:,.2f} / budget {budget:,.2f} "
                        f"(remaining {remaining:,.2f})</p>",
                        unsafe_allow_html=True,
                    )
            else:
                st.success("No teams are over their team budget.")

            st.markdown("**Teams close to budget**")
            if rep["close_rows"]:
                for label, used, budget, remaining in rep["close_rows"]:
                    st.markdown(
                        f'<p style="color:#a16207;margin:0.25rem 0;font-weight:600">'
                        f"{html.escape(label)} — used {used:,.2f} / budget {budget:,.2f} "
                        f"(remaining {remaining:,.2f})</p>",
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("No teams within the “close” threshold (or none with a positive team budget).")

            st.markdown("**Most commonly ordered items (top 3)**")
            if rep["top_items"]:
                for i, (name, cnt) in enumerate(rep["top_items"], start=1):
                    st.markdown(f"{i}. **{name}** — {cnt} line(s)")
            else:
                st.caption("No items yet.")

            st.markdown("**Most used suppliers (top 3)**")
            if rep["top_providers"]:
                for i, (name, cnt) in enumerate(rep["top_providers"], start=1):
                    st.markdown(f"{i}. **{name}** — {cnt} line(s)")
            else:
                st.caption("No supplier data yet.")

            hr_divider()

            st.markdown("##### Aggregated view")
            budget_summary = get_budget_summary_by_team(active_class_id)
            total_spend = sum(r["used_amount"] for r in budget_summary)
            total_orders = sum(r["total_orders"] for r in budget_summary)
            pending_count = sum(r["pending_count"] for r in budget_summary)

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Spend (approved only)", f"{total_spend:,.2f}")
            c2.metric("Total Orders", f"{total_orders}")
            c3.metric("Pending Count", f"{pending_count}")

            st.markdown("##### Spend per team")
            st.dataframe(budget_summary, use_container_width=True)

            hr_divider()

            st.markdown("##### Export all data as CSV")
            include_all_classes = st.checkbox("Include all classes", value=True)
            include_archived = st.checkbox("Include archived history", value=True)

            export_class_id = None if include_all_classes else int(active_class_id)

            rows = export_orders_csv_rows(
                include_all_classes=include_all_classes,
                include_archived=include_archived,
                class_id=export_class_id,
            )
            if not rows:
                st.download_button(
                    label="Download CSV (no data)",
                    data=b"",
                    file_name=f"purchase_requests_export_{date.today().isoformat()}.csv",
                    mime="text/csv",
                    disabled=True,
                    use_container_width=True,
                )
            else:
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for r in rows:
                    writer.writerow(r)
                csv_bytes = buf.getvalue().encode("utf-8")
                st.download_button(
                    label="Download CSV",
                    data=csv_bytes,
                    file_name=f"purchase_requests_export_{date.today().isoformat()}.csv",
                    mime="text/csv",
                    use_container_width=True,
                    type="primary",
                )

    with inner_tabs[1]:
        with st.container(border=True):
            st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
            st.markdown("##### Archived historical orders")
            archived_orders = list_archived_orders(class_id=int(active_class_id), archived=True)
            arch_pt = get_class_project_type(int(active_class_id))
            if not archived_orders:
                st.info("No archived orders for this class yet.")
            else:
                st.caption(f"Showing {len(archived_orders)} archived record(s).")
                for aidx, o in enumerate(archived_orders):
                    if aidx > 0:
                        st.markdown(
                            '<div style="border-top:1px solid #f3f4f6;margin:0.65rem 0"></div>',
                            unsafe_allow_html=True,
                        )
                    order_id = int(o["id"])
                    arch_party = _order_party_display_label(
                        arch_pt, o["team_number_snapshot"], o["cfo_name_snapshot"]
                    )
                    with st.container():
                        st.markdown('<span class="prt-archive-order-mark"></span>', unsafe_allow_html=True)
                        st.markdown(status_badge_html(o["status"]), unsafe_allow_html=True)
                        st.markdown(
                            f"**Order {order_id}** · **{arch_party}** · "
                            f"{o['provider_name_snapshot']} · Deadline {o['deadline']}"
                        )
                        st.markdown(
                            f"{o['item_name']} · Qty {o['quantity']} × {o['unit_price']} = "
                            f"**{float(o['total_price']):,.2f}**"
                        )
                        if o["received_at"]:
                            st.caption(f"Received at: {o['received_at']}")
                        if o["return_flag"]:
                            st.caption(f"Return flagged: Yes ({o['return_reason'] or 'No reason provided'})")


def render_login_page() -> None:
    st.markdown(
        """
<style>
  section[data-testid="stSidebar"] { display: none !important; }
  div[data-testid="collapsedControl"] { display: none !important; }
  section[data-testid="stMain"] .main .block-container {
    max-width: 440px !important;
    margin-left: auto !important;
    margin-right: auto !important;
  }
</style>
        """,
        unsafe_allow_html=True,
    )
    if st.session_state.pop("show_reg_success", False):
        st.success("Account created successfully. Please sign in.")

    with st.container(border=True):
        st.markdown('<span class="prt-card-surface"></span>', unsafe_allow_html=True)
        st.markdown(
            "<h1>Purchase Request Tracker — UW</h1>",
            unsafe_allow_html=True,
        )
        tab_sign, tab_create = st.tabs(["Sign In", "Create Account"])

        with tab_sign:
            st.text_input("Email", key="login_email", autocomplete="email")
            st.text_input("Password", type="password", key="login_pw")
            if st.button("Sign In", type="primary", use_container_width=True, key="login_btn"):
                em = (st.session_state.get("login_email") or "").strip()
                pw = st.session_state.get("login_pw") or ""
                user = authenticate_user(em, pw)
                if user is None:
                    st.session_state.login_err = True
                else:
                    st.session_state.user_id = user["id"]
                    st.session_state.user_name = user["full_name"]
                    st.session_state.user_email = user["email"]
                    st.session_state.user_role = user["role"]
                    st.session_state.pop("login_err", None)
                    st.rerun()
            if st.session_state.get("login_err"):
                st.error("Invalid email or password.")

        with tab_create:
            st.text_input("Full name", key="reg_full_name")
            st.text_input("Email", key="reg_email", autocomplete="email")
            st.text_input("Password", type="password", key="reg_pw")
            st.text_input("Confirm password", type="password", key="reg_pw2")
            st.selectbox(
                "Role",
                options=[ROLE_STUDENT_CFO, ROLE_INSTRUCTOR],
                key="reg_role",
            )
            st.caption("Admin accounts are managed by the system administrator.")
            if st.button("Create Account", type="primary", use_container_width=True, key="reg_btn"):
                fn = (st.session_state.get("reg_full_name") or "").strip()
                em = (st.session_state.get("reg_email") or "").strip()
                pw = st.session_state.get("reg_pw") or ""
                pw2 = st.session_state.get("reg_pw2") or ""
                rl = st.session_state.get("reg_role") or ROLE_STUDENT_CFO
                if pw != pw2:
                    st.error("Passwords do not match.")
                else:
                    try:
                        create_user(fn, em, pw, rl)
                        st.session_state.show_reg_success = True
                        st.session_state.pop("login_err", None)
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
                    except sqlite3.IntegrityError:
                        st.error("An account with this email already exists.")


st.set_page_config(
    page_title="Purchase Request Tracker — UW",
    page_icon="🎓",
    layout="wide",
)

inject_prt_styles()

init_db()

if "user_id" not in st.session_state:
    render_uw_banner()
    render_login_page()
    st.stop()

render_uw_banner()
st.title("Purchase Request Tracker")
st.caption("Submit and track purchase requests for your GIX class.")
render_sidebar()

role = st.session_state.user_role

if role == ROLE_STUDENT_CFO:
    tab_submit, tab_my_orders = st.tabs(["📝 Submit Request", "📋 My Orders"])
    with tab_submit:
        render_submit_request()
    with tab_my_orders:
        render_student_my_orders()
elif role == ROLE_INSTRUCTOR:
    tab_ins, = st.tabs(["Instructor"])
    with tab_ins:
        render_instructor_page()
elif role == ROLE_ADMIN:
    tab_admin, tab_summary = st.tabs(["Admin Dashboard", "Summary & Report"])
    with tab_admin:
        render_admin_dashboard()
    with tab_summary:
        render_summary_report()
else:
    st.error(f"Unsupported account role: {role!r}. Contact an administrator.")
