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
    shop_schedules = db.relationship(
        "ShopSchedule",
        back_populates="dataset",
        passive_deletes="all",
    )
    shop_staff = db.relationship("ShopStaff", back_populates="dataset")
    shop_shifts = db.relationship("ShopShift", back_populates="dataset")
    daily_forecasts = db.relationship(
        "DailyForecast", back_populates="dataset", passive_deletes="all",
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


class ShopSchedule(db.Model):
    """Datasetごとに分離された店舗全体の予定。日時はUTCで保存する。"""
    __tablename__ = "shop_schedules"
    __table_args__ = (
        db.CheckConstraint("length(trim(title)) >= 1", name="ck_shop_schedules_title_nonblank"),
        db.CheckConstraint("length(title) <= 100", name="ck_shop_schedules_title_max_length"),
        db.CheckConstraint("note IS NULL OR length(note) <= 1000", name="ck_shop_schedules_note_max_length"),
        db.CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="ck_shop_schedules_end_after_start"),
        db.Index("ix_shop_schedules_dataset_starts_id", "dataset_id", "starts_at", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey("datasets.id", name="fk_shop_schedules_dataset_id_datasets", ondelete="CASCADE"),
        nullable=False,
    )
    title = db.Column(db.String(100), nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=True)
    note = db.Column(db.String(1000), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    dataset = db.relationship("Dataset", back_populates="shop_schedules")




class ShopStaff(db.Model):
    """Admin Datasetのスタッフ名簿。退職後も過去のシフトから参照する。"""
    __tablename__ = "shop_staff"
    __table_args__ = (
        db.UniqueConstraint("id", "dataset_id", name="uq_shop_staff_id_dataset"),
        db.UniqueConstraint("dataset_id", "submission_key", name="uq_shop_staff_submission"),
        db.CheckConstraint("length(trim(display_name)) BETWEEN 1 AND 80", name="ck_shop_staff_name"),
        db.CheckConstraint("role IN ('production', 'sales', 'both', 'other')", name="ck_shop_staff_role"),
        db.Index("ix_shop_staff_dataset_active_id", "dataset_id", "is_active", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey("datasets.id", name="fk_shop_staff_dataset", ondelete="CASCADE"),
        nullable=False,
    )
    display_name = db.Column(db.String(80), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    hired_on = db.Column(db.Date, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    submission_key = db.Column(db.String(32), nullable=False, default=lambda: uuid.uuid4().hex)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    dataset = db.relationship("Dataset", back_populates="shop_staff")
    shifts = db.relationship("ShopShift", back_populates="staff")


class ShopShift(db.Model):
    """業務日ごとの勤務予定。日時はUTC、business_dateは勤務開始のJST日付。"""
    __tablename__ = "shop_shifts"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["staff_id", "dataset_id"], ["shop_staff.id", "shop_staff.dataset_id"],
            name="fk_shop_shifts_staff_dataset", ondelete="RESTRICT",
        ),
        db.UniqueConstraint("dataset_id", "staff_id", "business_date", name="uq_shop_shifts_staff_day"),
        db.CheckConstraint("status IN ('work', 'all_day', 'off')", name="ck_shop_shifts_status"),
        db.CheckConstraint(
            "(status = 'work' AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND ends_at > starts_at) "
            "OR (status IN ('all_day', 'off') AND starts_at IS NULL AND ends_at IS NULL)",
            name="ck_shop_shifts_times_by_status",
        ),
        db.CheckConstraint("note IS NULL OR length(note) <= 500", name="ck_shop_shifts_note"),
        db.Index("ix_shop_shifts_dataset_day_id", "dataset_id", "business_date", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Uuid(as_uuid=True),
        db.ForeignKey("datasets.id", name="fk_shop_shifts_dataset", ondelete="CASCADE"),
        nullable=False,
    )
    staff_id = db.Column(db.Integer, nullable=False)
    business_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(8), nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=True)
    note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    dataset = db.relationship("Dataset", back_populates="shop_shifts")
    staff = db.relationship("ShopStaff", back_populates="shifts")


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


class PrepRecipe(db.Model):
    """月次Productから独立した、Admin用の再利用可能な配合。"""
    __tablename__ = "prep_recipes"
    __table_args__ = (
        db.UniqueConstraint("id", "dataset_id", name="uq_prep_recipes_id_dataset"),
        db.UniqueConstraint("dataset_id", "product_key", name="uq_prep_recipes_product_key"),
        db.CheckConstraint("length(trim(display_name)) BETWEEN 1 AND 100", name="ck_prep_recipes_name"),
        db.CheckConstraint("base_quantity BETWEEN 1 AND 1000", name="ck_prep_recipes_base_quantity"),
        db.CheckConstraint("length(note) <= 2000", name="ck_prep_recipes_note"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    product_key = db.Column(db.String(128), nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    image_key = db.Column(db.String(100), nullable=True)
    base_quantity = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(2000), nullable=False, default="", server_default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    ingredients = db.relationship("PrepIngredient", back_populates="recipe", cascade="all, delete-orphan", order_by="PrepIngredient.id")
    plans = db.relationship("PrepPlan", back_populates="recipe")


class PrepIngredient(db.Model):
    __tablename__ = "prep_ingredients"
    __table_args__ = (
        db.ForeignKeyConstraint(["recipe_id", "dataset_id"], ["prep_recipes.id", "prep_recipes.dataset_id"], name="fk_prep_ingredients_recipe_dataset", ondelete="CASCADE"),
        db.UniqueConstraint("recipe_id", "name", "unit", name="uq_prep_ingredients_recipe_name_unit"),
        db.CheckConstraint("length(trim(name)) BETWEEN 1 AND 100", name="ck_prep_ingredients_name"),
        db.CheckConstraint("unit IN ('g', 'ml', '個')", name="ck_prep_ingredients_unit"),
        db.CheckConstraint("amount > 0 AND amount <= 1000000", name="ck_prep_ingredients_amount"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    recipe_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    unit = db.Column(db.String(4), nullable=False)
    amount = db.Column(db.Numeric(14, 3), nullable=False)
    recipe = db.relationship("PrepRecipe", back_populates="ingredients")


class PrepPlan(db.Model):
    """JST業務日ごとの予定数。実際の製造・在庫履歴ではない。"""
    __tablename__ = "prep_plans"
    __table_args__ = (
        db.ForeignKeyConstraint(["recipe_id", "dataset_id"], ["prep_recipes.id", "prep_recipes.dataset_id"], name="fk_prep_plans_recipe_dataset", ondelete="RESTRICT"),
        db.UniqueConstraint("dataset_id", "recipe_id", "business_date", name="uq_prep_plans_recipe_day"),
        db.CheckConstraint("quantity BETWEEN 0 AND 1000", name="ck_prep_plans_quantity"),
        db.Index("ix_prep_plans_dataset_day", "dataset_id", "business_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    recipe_id = db.Column(db.Integer, nullable=False)
    business_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    recipe = db.relationship("PrepRecipe", back_populates="plans")


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


class RegisterProduct(db.Model):
    """Datasetごとに継続利用するレジ商品。月次Productとは独立。"""
    __tablename__ = "register_products"
    __table_args__ = (
        db.UniqueConstraint("id", "dataset_id", name="uq_register_products_id_dataset"),
        db.UniqueConstraint("dataset_id", "template_key", name="uq_register_products_dataset_template"),
        db.CheckConstraint("length(trim(name)) BETWEEN 1 AND 100", name="ck_register_products_name"),
        db.CheckConstraint("template_key IS NULL OR length(trim(template_key)) BETWEEN 1 AND 100", name="ck_register_products_template_key"),
        db.CheckConstraint("image_key IS NULL OR length(trim(image_key)) BETWEEN 1 AND 100", name="ck_register_products_image_key"),
        db.CheckConstraint("price_yen BETWEEN 0 AND 100000 AND price_yen = CAST(price_yen AS INTEGER)", name="ck_register_products_price"),
        db.CheckConstraint("is_active IN (true, false)", name="ck_register_products_active"),
        db.CheckConstraint("bread_kind IN ('savory','sweet','loaf','hard','sandwich','fried','baked','other')", name="ck_register_products_bread_kind"),
        db.CheckConstraint("product_class IN ('regular','seasonal','special')", name="ck_register_products_class"),
        db.CheckConstraint("photo_webp IS NULL OR (length(photo_webp) BETWEEN 1 AND 512000 AND image_key IS NULL)", name="ck_register_products_photo"),
        db.CheckConstraint("revision >= 0", name="ck_register_products_revision"),
        db.CheckConstraint("display_order >= 0 AND display_order = CAST(display_order AS INTEGER)", name="ck_register_products_display_order"),
        db.Index("ix_register_products_dataset_active_order", "dataset_id", "is_active", "display_order", "id"),
        db.Index("ux_register_products_dataset_creation_key", "dataset_id", "creation_key", unique=True),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", name="fk_register_products_dataset", ondelete="RESTRICT"), nullable=False)
    template_key = db.Column(db.String(100), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    price_yen = db.Column(db.Integer, nullable=False)
    image_key = db.Column(db.String(100), nullable=True)
    bread_kind = db.Column(db.String(16), nullable=False, default="other", server_default="other")
    product_class = db.Column(db.String(16), nullable=False, default="regular", server_default="regular")
    photo_webp = db.Column(db.LargeBinary, nullable=True)
    revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    creation_key = db.Column(db.Uuid(as_uuid=True), nullable=True)
    creation_digest = db.Column(db.String(64), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    display_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())


class RegisterPriceHistory(db.Model):
    """導入後に観測した登録時価格と変更。導入前の価格は推測しない。"""
    __tablename__ = "register_price_history"
    __table_args__ = (
        db.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"],
                                name="fk_register_price_history_product_dataset", ondelete="RESTRICT"),
        db.CheckConstraint("event IN ('created', 'changed')", name="ck_register_price_history_event"),
        db.CheckConstraint("old_price_yen IS NULL OR old_price_yen BETWEEN 0 AND 100000",
                           name="ck_register_price_history_old_price"),
        db.CheckConstraint("new_price_yen BETWEEN 0 AND 100000", name="ck_register_price_history_new_price"),
        db.CheckConstraint("(event = 'created' AND old_price_yen IS NULL) OR "
                           "(event = 'changed' AND old_price_yen IS NOT NULL AND old_price_yen <> new_price_yen)",
                           name="ck_register_price_history_transition"),
        db.Index("ix_register_price_history_dataset_product_time", "dataset_id", "register_product_id", "recorded_at", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    register_product_id = db.Column(db.Integer, nullable=False)
    event = db.Column(db.String(16), nullable=False)
    old_price_yen = db.Column(db.Integer, nullable=True)
    new_price_yen = db.Column(db.Integer, nullable=False)
    recorded_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=utc_now, server_default=db.func.now())


class RegisterPrepLink(db.Model):
    """Adminが明示的に承認した独自レジ商品とレシピの一対一対応。"""
    __tablename__ = "register_prep_links"
    __table_args__ = (
        db.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"],
                                name="fk_register_prep_links_product_dataset", ondelete="RESTRICT"),
        db.ForeignKeyConstraint(["recipe_id", "dataset_id"],
                                ["prep_recipes.id", "prep_recipes.dataset_id"],
                                name="fk_register_prep_links_recipe_dataset", ondelete="RESTRICT"),
        db.UniqueConstraint("dataset_id", "register_product_id",
                            name="uq_register_prep_links_dataset_product"),
    )

    dataset_id = db.Column(db.Uuid(as_uuid=True), primary_key=True)
    recipe_id = db.Column(db.Integer, primary_key=True)
    register_product_id = db.Column(db.Integer, nullable=False)


class RegisterCutover(db.Model):
    """旧日次売上とレジ会計を営業日単位で切り替える境界。"""
    __tablename__ = "register_cutovers"

    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", name="fk_register_cutovers_dataset", ondelete="RESTRICT"), primary_key=True)
    first_register_date = db.Column(db.Date, nullable=False)


class RegisterCutoverApproval(db.Model):
    """切替と同じトランザクションに保存する管理者の承認記録。"""
    __tablename__ = "register_cutover_approvals"
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey(
        "register_cutovers.dataset_id", ondelete="RESTRICT"), primary_key=True)
    first_register_date = db.Column(db.Date, nullable=False)
    approved_by = db.Column(db.String(100), nullable=False)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now,
                            server_default=db.func.now())
    reason = db.Column(db.String(500), nullable=False)
    device_inventory = db.Column(db.String(1000), nullable=False)
    review_decisions = db.Column(db.Text, nullable=False, default="{}", server_default="{}")


class RegisterTransaction(db.Model):
    """販売または返金の1会計。0円の保存可否は後続サービスで決める。"""
    __tablename__ = "register_transactions"
    __table_args__ = (
        db.UniqueConstraint("id", "dataset_id", name="uq_register_transactions_id_dataset"),
        db.UniqueConstraint("dataset_id", "submission_key", name="uq_register_transactions_submission"),
        db.UniqueConstraint("recovery_case_id", name="uq_register_transactions_recovery_case"),
        db.UniqueConstraint("dataset_id", "recovery_evidence_digest", name="uq_register_transactions_recovery_evidence"),
        db.ForeignKeyConstraint(["recovery_case_id", "dataset_id"],
                                ["register_recovery_cases.id", "register_recovery_cases.dataset_id"],
                                name="fk_register_transactions_recovery_dataset", ondelete="RESTRICT"),
        db.ForeignKeyConstraint(["offline_permit_id", "dataset_id"],
                                ["register_offline_permits.id", "register_offline_permits.dataset_id"],
                                name="fk_register_transactions_offline_permit_dataset", ondelete="RESTRICT"),
        db.CheckConstraint("save_method IN ('normal','recovery','offline')", name="ck_register_transactions_save_method"),
        db.CheckConstraint("(save_method = 'normal' AND recovery_case_id IS NULL AND client_completed_at IS NULL "
                           "AND recovery_evidence_digest IS NULL AND offline_permit_id IS NULL) OR "
                           "(save_method = 'recovery' AND recovery_case_id IS NOT NULL AND client_completed_at IS NOT NULL "
                           "AND recovery_evidence_digest IS NOT NULL AND length(recovery_evidence_digest) = 64 "
                           "AND offline_permit_id IS NULL) OR "
                           "(save_method = 'offline' AND recovery_case_id IS NULL AND client_completed_at IS NOT NULL "
                           "AND recovery_evidence_digest IS NULL AND offline_permit_id IS NOT NULL)",
                           name="ck_register_transactions_recovery_fields"),
        db.CheckConstraint("length(request_digest) = 64", name="ck_register_transactions_digest"),
        db.CheckConstraint("kind IN ('sale', 'refund')", name="ck_register_transactions_kind"),
        db.CheckConstraint("total_yen BETWEEN -1000000000 AND 1000000000 AND total_yen = CAST(total_yen AS INTEGER)", name="ck_register_transactions_total"),
        db.CheckConstraint(
            "(kind = 'sale' AND total_yen >= 0 AND received_yen IS NOT NULL "
            "AND change_yen IS NOT NULL AND received_yen BETWEEN 0 AND 1000000000 "
            "AND change_yen BETWEEN 0 AND 1000000000 "
            "AND received_yen = CAST(received_yen AS INTEGER) "
            "AND change_yen = CAST(change_yen AS INTEGER) "
            "AND received_yen >= total_yen AND change_yen = received_yen - total_yen) "
            "OR (kind = 'refund' AND total_yen <= 0 "
            "AND received_yen IS NULL AND change_yen IS NULL)",
            name="ck_register_transactions_settlement",
        ),
        db.Index("ix_register_transactions_dataset_day_completed", "dataset_id", "business_date", "completed_at"),
    )

    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", name="fk_register_transactions_dataset", ondelete="RESTRICT"), nullable=False)
    submission_key = db.Column(db.Uuid(as_uuid=True), nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    business_date = db.Column(db.Date, nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    total_yen = db.Column(db.BigInteger, nullable=False)
    received_yen = db.Column(db.BigInteger, nullable=True)
    change_yen = db.Column(db.BigInteger, nullable=True)
    save_method = db.Column(db.String(16), nullable=False, default="normal", server_default="normal")
    recovery_case_id = db.Column(db.Uuid(as_uuid=True), nullable=True)
    recovery_evidence_digest = db.Column(db.String(64), nullable=True)
    client_completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    offline_permit_id = db.Column(db.Uuid(as_uuid=True), nullable=True)


class RegisterOfflinePermit(db.Model):
    """Adminがオンラインで端末と営業日を事前承認。失効と同期上限はDBが正本。"""
    __tablename__ = "register_offline_permits"
    __table_args__ = (
        db.UniqueConstraint("id", "dataset_id", name="uq_register_offline_permit_id_dataset"),
        db.CheckConstraint("length(device_id) = 36", name="ck_register_offline_device"),
        db.CheckConstraint("expires_at > issued_at", name="ck_register_offline_window"),
        db.Index("ix_register_offline_permit_device", "dataset_id", "device_id"),
    )
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False)
    device_id = db.Column(db.String(36), nullable=False)
    approved_by = db.Column(db.String(100), nullable=False)
    business_date = db.Column(db.Date, nullable=False)
    next_business_date = db.Column(db.Date, nullable=True)
    catalog_digest = db.Column(db.String(64), nullable=False)
    issued_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)


class RegisterTransactionLine(db.Model):
    """会計時点の商品名と単価を固定した明細。"""
    __tablename__ = "register_transaction_lines"
    __table_args__ = (
        db.ForeignKeyConstraint(["transaction_id", "dataset_id"], ["register_transactions.id", "register_transactions.dataset_id"], name="fk_register_lines_transaction_dataset", ondelete="RESTRICT"),
        db.ForeignKeyConstraint(["register_product_id", "dataset_id"], ["register_products.id", "register_products.dataset_id"], name="fk_register_lines_product_dataset", ondelete="RESTRICT"),
        db.UniqueConstraint("transaction_id", "register_product_id", name="uq_register_lines_transaction_product"),
        db.CheckConstraint("length(trim(product_name_snapshot)) BETWEEN 1 AND 100", name="ck_register_lines_name"),
        db.CheckConstraint("quantity BETWEEN -50 AND 50 AND quantity <> 0 AND quantity = CAST(quantity AS INTEGER)", name="ck_register_lines_quantity"),
        db.CheckConstraint("unit_price_yen BETWEEN 0 AND 100000 AND unit_price_yen = CAST(unit_price_yen AS INTEGER)", name="ck_register_lines_unit_price"),
        db.CheckConstraint("line_total_yen = unit_price_yen * quantity", name="ck_register_lines_total"),
        db.Index("ix_register_lines_dataset_product", "dataset_id", "register_product_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    dataset_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    register_product_id = db.Column(db.Integer, nullable=False)
    product_name_snapshot = db.Column(db.String(100), nullable=False)
    unit_price_yen = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    line_total_yen = db.Column(db.Integer, nullable=False)


class RegisterRecoveryCase(db.Model):
    """端末の元会計を売上とは別に保全する。snapshot_jsonは変更しない。"""
    __tablename__ = "register_recovery_cases"
    __table_args__ = (
        db.UniqueConstraint("id", "dataset_id", name="uq_register_recovery_id_dataset"),
        db.UniqueConstraint("dataset_id", "submission_key", name="uq_register_recovery_submission"),
        db.CheckConstraint("length(source_digest) = 64", name="ck_register_recovery_digest"),
        db.CheckConstraint("status IN ('unreviewed','investigating','held','saved','cancelled','additional')",
                           name="ck_register_recovery_status"),
        db.CheckConstraint("kind IN ('sale','refund')", name="ck_register_recovery_kind"),
        db.CheckConstraint("total_yen BETWEEN -1000000000 AND 1000000000", name="ck_register_recovery_total"),
    )
    id = db.Column(db.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="RESTRICT"), nullable=False)
    submission_key = db.Column(db.Uuid(as_uuid=True), nullable=False)
    source_digest = db.Column(db.String(64), nullable=False)
    snapshot_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="unreviewed")
    kind = db.Column(db.String(16), nullable=False)
    client_business_date = db.Column(db.Date, nullable=False)
    total_yen = db.Column(db.BigInteger, nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=utc_now, server_default=db.func.now())


class RegisterRecoveryLine(db.Model):
    """DB制約でも元の商品IDを同じDatasetに固定する。"""
    __tablename__ = "register_recovery_lines"
    __table_args__ = (
        db.ForeignKeyConstraint(["case_id", "dataset_id"],
                                ["register_recovery_cases.id", "register_recovery_cases.dataset_id"],
                                ondelete="RESTRICT"),
        db.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"], ondelete="RESTRICT"),
        db.UniqueConstraint("case_id", "register_product_id", name="uq_register_recovery_line_product"),
        db.CheckConstraint("quantity BETWEEN -50 AND 50 AND quantity <> 0", name="ck_register_recovery_line_qty"),
        db.CheckConstraint("unit_price_yen BETWEEN 0 AND 100000", name="ck_register_recovery_line_price"),
        db.CheckConstraint("line_total_yen = quantity * unit_price_yen", name="ck_register_recovery_line_total"),
    )
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    dataset_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    register_product_id = db.Column(db.Integer, nullable=False)
    product_name_snapshot = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price_yen = db.Column(db.Integer, nullable=False)
    line_total_yen = db.Column(db.Integer, nullable=False)


class RegisterRecoveryEvent(db.Model):
    """状態遷移と確認した根拠の追記専用監査。"""
    __tablename__ = "register_recovery_events"
    __table_args__ = (
        db.ForeignKeyConstraint(["case_id", "dataset_id"],
                                ["register_recovery_cases.id", "register_recovery_cases.dataset_id"],
                                ondelete="RESTRICT"),
        db.ForeignKeyConstraint(["transaction_id", "dataset_id"],
                                ["register_transactions.id", "register_transactions.dataset_id"],
                                name="fk_register_recovery_event_transaction_dataset", ondelete="RESTRICT"),
        db.CheckConstraint("to_status IN ('unreviewed','investigating','held','saved','cancelled','additional')",
                           name="ck_register_recovery_event_status"),
    )
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    dataset_id = db.Column(db.Uuid(as_uuid=True), nullable=False)
    from_status = db.Column(db.String(16), nullable=True)
    to_status = db.Column(db.String(16), nullable=False)
    actor = db.Column(db.String(100), nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=utc_now, server_default=db.func.now())
    reason = db.Column(db.String(500), nullable=False)
    server_result = db.Column(db.String(24), nullable=False)
    price_conflict = db.Column(db.Boolean, nullable=False)
    day_conflict = db.Column(db.Boolean, nullable=False)
    cash_result = db.Column(db.String(24), nullable=False)
    evidence_kind = db.Column(db.String(100), nullable=False, default="none")
    evidence_reference = db.Column(db.String(500), nullable=False, default="")
    transaction_id = db.Column(db.Uuid(as_uuid=True), nullable=True)
    adopted_business_date = db.Column(db.Date, nullable=True)


class DailyWeather(db.Model):
    """日別天気の保存済み観測・再解析データ。認可済みDatasetと日付で一意。"""
    __tablename__ = "daily_weather"
    __table_args__ = (
        db.UniqueConstraint("dataset_id", "date", name="uq_daily_weather_dataset_date"),
        db.CheckConstraint("weather_code BETWEEN 0 AND 99", name="ck_daily_weather_code"),
        db.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_daily_weather_latitude"),
        db.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_daily_weather_longitude"),
        db.CheckConstraint("precipitation_mm IS NULL OR precipitation_mm >= 0", name="ck_daily_weather_precipitation"),
    )
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    weather_code = db.Column(db.Integer, nullable=False)
    precipitation_mm = db.Column(db.Float, nullable=True)
    max_temperature_c = db.Column(db.Float, nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    source = db.Column(db.String(64), nullable=False)
    recorded_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class DailyForecast(db.Model):
    """ホームで取得した当日の最初のMET Norway予報。観測記録とは別。"""
    __tablename__ = "daily_forecasts"
    __table_args__ = (
        db.UniqueConstraint("dataset_id", "business_date", name="uq_daily_forecasts_dataset_date"),
        db.CheckConstraint("weather_code BETWEEN 0 AND 99", name="ck_daily_forecasts_code"),
        db.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_daily_forecasts_latitude"),
        db.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_daily_forecasts_longitude"),
    )
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(db.Uuid(as_uuid=True), db.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    business_date = db.Column(db.Date, nullable=False)
    weather_code = db.Column(db.Integer, nullable=False)
    forecast_valid_at = db.Column(db.DateTime(timezone=True), nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    location_name = db.Column(db.String(80), nullable=False)
    source = db.Column(db.String(64), nullable=False)
    dataset = db.relationship("Dataset", back_populates="daily_forecasts")

