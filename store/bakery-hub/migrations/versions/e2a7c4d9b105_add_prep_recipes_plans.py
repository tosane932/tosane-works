"""Add independent Admin prep recipes, ingredients, and daily plans.

Revision ID: e2a7c4d9b105
Revises: d5a9f1b7c204
"""
from alembic import op
import sqlalchemy as sa

revision = "e2a7c4d9b105"
down_revision = "d5a9f1b7c204"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "prep_recipes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_key", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("image_key", sa.String(100), nullable=True),
        sa.Column("base_quantity", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(2000), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("id", "dataset_id", name="uq_prep_recipes_id_dataset"),
        sa.UniqueConstraint("dataset_id", "product_key", name="uq_prep_recipes_product_key"),
        sa.CheckConstraint("length(trim(display_name)) BETWEEN 1 AND 100", name="ck_prep_recipes_name"),
        sa.CheckConstraint("base_quantity BETWEEN 1 AND 1000", name="ck_prep_recipes_base_quantity"),
        sa.CheckConstraint("length(note) <= 2000", name="ck_prep_recipes_note"),
    )
    op.create_table(
        "prep_ingredients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipe_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("unit", sa.String(4), nullable=False),
        sa.Column("amount", sa.Numeric(14, 3), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id", "dataset_id"], ["prep_recipes.id", "prep_recipes.dataset_id"], name="fk_prep_ingredients_recipe_dataset", ondelete="CASCADE"),
        sa.UniqueConstraint("recipe_id", "name", "unit", name="uq_prep_ingredients_recipe_name_unit"),
        sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 100", name="ck_prep_ingredients_name"),
        sa.CheckConstraint("unit IN ('g', 'ml', '個')", name="ck_prep_ingredients_unit"),
        sa.CheckConstraint("amount > 0 AND amount <= 1000000", name="ck_prep_ingredients_amount"),
    )
    op.create_table(
        "prep_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipe_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["recipe_id", "dataset_id"], ["prep_recipes.id", "prep_recipes.dataset_id"], name="fk_prep_plans_recipe_dataset", ondelete="RESTRICT"),
        sa.UniqueConstraint("dataset_id", "recipe_id", "business_date", name="uq_prep_plans_recipe_day"),
        sa.CheckConstraint("quantity BETWEEN 0 AND 1000", name="ck_prep_plans_quantity"),
    )
    op.create_index("ix_prep_plans_dataset_day", "prep_plans", ["dataset_id", "business_date"])


def downgrade():
    op.drop_index("ix_prep_plans_dataset_day", table_name="prep_plans")
    op.drop_table("prep_plans")
    op.drop_table("prep_ingredients")
    op.drop_table("prep_recipes")
