import datetime
import uuid

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utc_now():
    """UTCのtimezone情報を持つ現在日時を返す。"""
    return datetime.datetime.now(datetime.timezone.utc)


class Dataset(db.Model):
    """管理者またはGuestごとに分離されたデータ領域を表すテーブル。"""
    __tablename__ = "datasets"
    __table_args__ = (
        db.CheckConstraint(
            "kind IN ('admin', 'guest')",
            name="ck_datasets_kind",
        ),
        db.CheckConstraint(
            "(kind = 'admin' AND system_key = 'admin') OR "
            "(kind = 'guest' AND system_key IS NULL)",
            name="ck_datasets_system_key_by_kind",
        ),
        db.CheckConstraint(
            "(kind = 'admin' AND absolute_expires_at IS NULL) OR "
            "(kind = 'guest' AND absolute_expires_at IS NOT NULL)",
            name="ck_datasets_absolute_expiry_by_kind",
        ),
        db.CheckConstraint(
            "last_activity_at >= created_at",
            name="ck_datasets_activity_not_before_creation",
        ),
        db.CheckConstraint(
            "absolute_expires_at IS NULL OR absolute_expires_at > created_at",
            name="ck_datasets_expiry_after_creation",
        ),
        db.CheckConstraint(
            "(kind = 'admin' AND guest_ai_usage_count = 0) OR "
            "(kind = 'guest' AND guest_ai_usage_count BETWEEN 0 AND 3)",
            name="ck_datasets_guest_ai_usage_count",
        ),
    )

    id = db.Column(
        db.Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    kind = db.Column(db.String(16), nullable=False)
    system_key = db.Column(db.String(100), nullable=True, unique=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    last_activity_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    absolute_expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    guest_ai_usage_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    products = db.relationship(
        "Product",
        back_populates="dataset",
        cascade="all, delete",
        passive_deletes=True,
    )
    material_order_items = db.relationship(
        "MaterialOrderItem",
        back_populates="dataset",
        passive_deletes="all",
    )
    shop_memos = db.relationship(
        "ShopMemo",
        back_populates="dataset",
        passive_deletes="all",
    )
    shop_tasks = db.relationship(
        "ShopTask",
        back_populates="dataset",
        passive_deletes="all",
    )


class GuestCreationRateLimit(db.Model):
    """Guest Session作成回数を匿名client単位で管理する。"""
    __tablename__ = "guest_creation_rate_limits"
    __table_args__ = (
        db.CheckConstraint(
            "request_count >= 0",
            name="ck_guest_creation_rate_limits_request_count_nonnegative",
        ),
    )

    client_key_hash = db.Column(db.String(64), primary_key=True)
    window_started_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
    )
    request_count = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
    )


class MaterialOrderItem(db.Model):
    """Datasetごとの独立した材料発注チェック項目を表す。"""
    __tablename__ = "material_order_items"
    __table_args__ = (
        db.CheckConstraint(
            "(is_completed = false AND completed_at IS NULL) OR "
            "(is_completed = true AND completed_at IS NOT NULL)",
            name="ck_material_order_items_completion_timestamp",
        ),
        db.Index(
            "ix_material_order_items_dataset_status_created_id",
            "dataset_id",
            "is_completed",
            "created_at",
            "id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey(
            "datasets.id",
            name="fk_material_order_items_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    name = db.Column(db.String(100), nullable=False)
    quantity_text = db.Column(db.String(30), nullable=True)
    memo = db.Column(db.String(300), nullable=True)
    is_completed = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    completed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    dataset = db.relationship(
        "Dataset",
        back_populates="material_order_items",
    )


class ShopMemo(db.Model):
    """Datasetごとの店舗メモを表す。"""
    __tablename__ = "shop_memos"
    __table_args__ = (
        db.CheckConstraint(
            "length(trim(title)) >= 1",
            name="ck_shop_memos_title_nonblank",
        ),
        db.CheckConstraint(
            "length(title) <= 100",
            name="ck_shop_memos_title_max_length",
        ),
        db.CheckConstraint(
            "length(trim(body)) >= 1",
            name="ck_shop_memos_body_nonblank",
        ),
        db.CheckConstraint(
            "length(body) <= 2000",
            name="ck_shop_memos_body_max_length",
        ),
        db.CheckConstraint(
            "updated_at >= created_at",
            name="ck_shop_memos_updated_not_before_creation",
        ),
        db.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name="ck_shop_memos_deleted_not_before_creation",
        ),
        db.CheckConstraint(
            "deleted_at IS NULL OR updated_at >= deleted_at",
            name="ck_shop_memos_updated_not_before_deletion",
        ),
        db.CheckConstraint(
            "pinned_at IS NULL OR pinned_at >= created_at",
            name="ck_shop_memos_pinned_not_before_creation",
        ),
        db.Index(
            "ix_shop_memos_dataset_deleted_pinned_updated_id",
            "dataset_id",
            "deleted_at",
            "pinned_at",
            "updated_at",
            "id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey(
            "datasets.id",
            name="fk_shop_memos_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    title = db.Column(db.String(100), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    deleted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    pinned_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    dataset = db.relationship(
        "Dataset",
        back_populates="shop_memos",
    )


class ShopTask(db.Model):
    """Datasetごとの日常業務チェックリストを表す。"""
    __tablename__ = "shop_tasks"
    __table_args__ = (
        db.CheckConstraint(
            "length(trim(title)) >= 1",
            name="ck_shop_tasks_title_nonblank",
        ),
        db.CheckConstraint(
            "length(title) <= 100",
            name="ck_shop_tasks_title_max_length",
        ),
        db.CheckConstraint(
            "(is_completed = false AND completed_at IS NULL) OR "
            "(is_completed = true AND completed_at IS NOT NULL)",
            name="ck_shop_tasks_completion_timestamp",
        ),
        db.CheckConstraint(
            "position >= 0",
            name="ck_shop_tasks_position_nonnegative",
        ),
        db.Index(
            "ix_shop_tasks_dataset_status_position_id",
            "dataset_id",
            "is_completed",
            "position",
            "id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey(
            "datasets.id",
            name="fk_shop_tasks_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    title = db.Column(db.String(100), nullable=False)
    is_completed = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    is_starred = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    position = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    completed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    dataset = db.relationship(
        "Dataset",
        back_populates="shop_tasks",
    )


class Product(db.Model):
    """その月に登録された商品マスタ（商品名・単価）を表すテーブル。"""
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey(
            "datasets.id",
            name="fk_products_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    image_key = db.Column(db.String(100), nullable=True)
    dataset = db.relationship("Dataset", back_populates="products")
    
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true()
    )
    # 1つの商品は複数の日別実績(daily_sales)を持つ
    daily_sales = db.relationship("DailySales", backref="product", lazy=True)


class DailySales(db.Model):
    """商品ごとの日別の販売数量を記録するテーブル。"""
    __tablename__ = "daily_sales"
    __table_args__ = (
        db.UniqueConstraint(
            "product_id",
            "date",
            name="uq_daily_sales_product_date"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
