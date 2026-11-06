"""患者目录表：把写死在 JSON 里的演示患者搬进库

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05

说明：
  1. 只新增一张 patients 表，不动旧表结构（已有数据零风险）；
  2. patient_ref 唯一索引：住院号重复时在写入口就报错，不让脏数据流到 resolve_patient；
  3. 数据由 `patient_service.ensure_seed()` 从 app/agents/data/patients.json 导入，
     不放在迁移里——迁移只管结构，数据可以随时重播（幂等）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_ref", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("dept", sa.String(64), nullable=False, server_default=""),
        sa.Column("plan_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("discharge_date", sa.Date(), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_patients_patient_ref", "patients", ["patient_ref"], unique=True)
    op.create_index("ix_patients_name", "patients", ["name"])
    op.create_index("ix_patients_status", "patients", ["status"])


def downgrade() -> None:
    op.drop_index("ix_patients_status", table_name="patients")
    op.drop_index("ix_patients_name", table_name="patients")
    op.drop_index("ix_patients_patient_ref", table_name="patients")
    op.drop_table("patients")
