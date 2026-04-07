from __future__ import annotations

from dataclasses import dataclass


STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_WITHDRAWN = "WITHDRAWN"
STATUS_LOST = "LOST"


@dataclass(frozen=True)
class StatusStyle:
    label: str
    color: str  # text
    background: str  # pill background


STATUS_STYLES: dict[str, StatusStyle] = {
    STATUS_PENDING: StatusStyle(label="Pending", color="#d97706", background="#fef3c7"),
    STATUS_PROCESSING: StatusStyle(label="Processing", color="#d97706", background="#fef3c7"),
    STATUS_APPROVED: StatusStyle(label="Approved", color="#15803d", background="#dcfce7"),
    STATUS_REJECTED: StatusStyle(label="Rejected", color="#dc2626", background="#fee2e2"),
    STATUS_WITHDRAWN: StatusStyle(label="Withdrawn", color="#6b7280", background="#f3f4f6"),
    STATUS_LOST: StatusStyle(label="📦 Lost", color="#4b5563", background="#e5e7eb"),
}


def compute_combined_status(instructor_status: str, admin_status: str) -> str:
    """Derives legacy `status` from instructor + admin approval (both must approve for APPROVED)."""
    inst = (instructor_status or "").strip()
    adm = (admin_status or "").strip()

    # Legacy orders: no instructor review (NULL/empty before instructor workflow existed).
    if not inst:
        if adm == STATUS_REJECTED:
            return STATUS_REJECTED
        if adm == STATUS_APPROVED:
            return STATUS_APPROVED
        return STATUS_PENDING

    if inst == STATUS_REJECTED or adm == STATUS_REJECTED:
        return STATUS_REJECTED
    if inst == STATUS_APPROVED and adm == STATUS_APPROVED:
        return STATUS_APPROVED
    if inst == STATUS_APPROVED and adm == STATUS_PENDING:
        return STATUS_PROCESSING
    return STATUS_PENDING

