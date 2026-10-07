"""chuyển chi phí/ngân sách sang VND (đổi tên cột)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07

Trước bản này chưa có đơn giá nào được cấu hình nên mọi giá trị chi phí đều bằng 0 -> chỉ cần đổi tên cột.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = [
    ("llm_calls", "cost_usd", "cost_vnd"),
    ("extractions", "cost_usd", "cost_vnd"),
    ("tenants", "monthly_budget_usd", "monthly_budget_vnd"),
]


def upgrade() -> None:
    for table, old, new in RENAMES:
        op.alter_column(table, old, new_column_name=new)


def downgrade() -> None:
    for table, old, new in RENAMES:
        op.alter_column(table, new, new_column_name=old)
