import os
import datetime
import hashlib
import hmac
import logging  # 💡 1. ログモジュールをインポート
import ipaddress
import threading
import uuid
from contextlib import contextmanager
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from sqlalchemy import case, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import (
    Conflict,
    HTTPException,
    InternalServerError,
    ServiceUnavailable,
    TooManyRequests,
)
from werkzeug.security import check_password_hash
from models import (
    db,
    Dataset,
    GuestCreationRateLimit,
    MaterialOrderItem,
    ShopMemo,
    ShopTask,
    Product,
    DailySales,
)
from google import genai
from material_orders import create_material_orders_blueprint
from prompts import build_sales_prompt
from shop_memos import create_shop_memos_blueprint
from shop_tasks import create_shop_tasks_blueprint
from product_images import available_product_images, available_product_image_assets
from catalog_repository import (
    CatalogPublishError,
    InvalidCatalogImage,
    MAX_UPLOAD_IMAGE_BYTES,
    publish_catalog_change,
    validate_image,
)


def _load_direct_run_environment(dotenv_path=None):
    """python app.py用の.envをconfig importより前に読み込む。"""
    if dotenv_path is None:
        dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    return load_dotenv(dotenv_path=dotenv_path, override=False)


if __name__ == "__main__":
    _load_direct_run_environment()


import config


# 💡 2. ログの初期設定（デジタコのセットアップ）
# フォーマットに「日時 [レベル] メッセージ」を指定し、コンテナの標準出力に出すよう設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

app.config["SQLALCHEMY_DATABASE_URI"] = config.SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = config.SQLALCHEMY_TRACK_MODIFICATIONS
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["ADMIN_USERNAME"] = config.ADMIN_USERNAME
app.config["ADMIN_PASSWORD_HASH"] = config.ADMIN_PASSWORD_HASH
app.config["LOCAL_DEVELOPMENT"] = config.LOCAL_DEVELOPMENT

app.config["SESSION_COOKIE_SECURE"] = config.SESSION_COOKIE_SECURE
app.config["SESSION_COOKIE_HTTPONLY"] = config.SESSION_COOKIE_HTTPONLY
app.config["SESSION_COOKIE_SAMESITE"] = config.SESSION_COOKIE_SAMESITE
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
app.config["ADMIN_LOGIN_RATE_LIMIT_MAX_FAILURES"] = (
    config.ADMIN_LOGIN_RATE_LIMIT_MAX_FAILURES
)
app.config["ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS"] = (
    config.ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS
)
app.config["GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS"] = (
    config.GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS
)
app.config["GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS"] = (
    config.GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS
)
app.config["GUEST_ACTIVE_DATASET_LIMIT"] = (
    config.GUEST_ACTIVE_DATASET_LIMIT
)


@app.before_request
def allow_catalog_image_request_size():
    """CSRFがmultipartを読む前に、このPOSTだけ画像とformの容量を確保する。"""
    if request.endpoint == "submit_product_catalog" and request.method == "POST":
        request.max_content_length = MAX_UPLOAD_IMAGE_BYTES + 16 * 1024


csrf = CSRFProtect(app)
db.init_app(app)

migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=86400"
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "form-action 'self'"
    )

    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"

    return response


class AdminUser(UserMixin):
    id = "admin"
    is_admin = True
    is_guest = False


class GuestUser(UserMixin):
    is_admin = False
    is_guest = True

    def __init__(self, dataset_id):
        self.dataset_id = dataset_id

    @property
    def id(self):
        return f"guest:{self.dataset_id}"


ADMIN_AUTH_FINGERPRINT_SESSION_KEY = "admin_auth_fingerprint"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Tokyo")
GEMINI_REQUEST_TIMEOUT_MILLISECONDS = 15_000
GUEST_ABSOLUTE_LIFETIME = datetime.timedelta(hours=2)
GUEST_IDLE_TIMEOUT = datetime.timedelta(minutes=30)
GUEST_AI_USAGE_LIMIT = 3
PRODUCTS_PER_POST_LIMIT = 30
GUEST_PRODUCT_LIFETIME_LIMIT = 30
PRODUCT_NAME_MAX_LENGTH = 100
PRODUCT_PRICE_MAX = 9_990
PRODUCT_PRICE_STEP = 10
PRODUCT_YEAR_MIN = 2000
PRODUCT_YEAR_MAX = 2100
SALES_PER_POST_LIMIT = 30
SALES_QUANTITY_MAX = 1_000
# 各Productは1年月に所属し、売上は1日1行・最大10,000個。
# 1商品につき最大31日、同名を全30商品で合算した数量を上限とする。
GUEST_AI_AGGREGATE_QUANTITY_MAX = (
    GUEST_PRODUCT_LIFETIME_LIMIT * 31 * SALES_QUANTITY_MAX
)
_GUEST_AI_LIMIT_REACHED = object()
GUEST_CREATION_RATE_LIMIT_HMAC_DOMAIN = (
    "guest-creation-rate-limit:v1"
)
ADMIN_LOGIN_RATE_LIMIT_HMAC_DOMAIN = "admin-login-rate-limit:v1"
GUEST_ADMISSION_LOCK_NAMESPACE = 0x47554553  # "GUES"
GUEST_ADMISSION_LOCK_KEY = 1
_GUEST_CAPACITY_NOT_LOADED = object()
_ADMIN_LOGIN_SQLITE_LOCK = threading.Lock()


class GuestCapacityFull(ServiceUnavailable):
    """満員と他の503障害を文字列比較せず区別する。"""

    def __init__(self, active_guest_count, active_dataset_limit):
        super().__init__(description="ゲストデモは現在満員です。")
        self.active_guest_count = active_guest_count
        self.active_dataset_limit = active_dataset_limit


def business_today():
    """日本の店舗業務で扱う今日の日付を返す。"""
    return datetime.datetime.now(BUSINESS_TIMEZONE).date()


def _as_utc(value):
    """DBから取得した日時をUTCのaware datetimeへそろえる。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _parse_bounded_nonnegative_integer(value, maximum):
    """巨大な数値文字列も例外にせず、指定上限内の整数へ変換する。"""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None

    normalized_value = value.lstrip("0") or "0"
    maximum_text = str(maximum)
    if (
        len(normalized_value) > len(maximum_text)
        or (
            len(normalized_value) == len(maximum_text)
            and normalized_value > maximum_text
        )
    ):
        return None

    return int(normalized_value)


def _guest_dataset_is_expired(dataset, now=None):
    """Guest Datasetが絶対期限または無操作期限切れか判定する。"""
    absolute_expires_at = _as_utc(dataset.absolute_expires_at)
    last_activity_at = _as_utc(dataset.last_activity_at)
    now = _as_utc(now) if now is not None else datetime.datetime.now(
        datetime.timezone.utc
    )

    if absolute_expires_at is None or absolute_expires_at <= now:
        return True

    if (
        last_activity_at is None
        or last_activity_at + GUEST_IDLE_TIMEOUT <= now
    ):
        return True

    return False


def _get_active_guest_dataset_count(*, now=None):
    """既存の期限判定と同じ境界で有効なGuest数を返す。"""
    active_at = _as_utc(now) if now is not None else datetime.datetime.now(
        datetime.timezone.utc
    )
    idle_cutoff = active_at - GUEST_IDLE_TIMEOUT

    return Dataset.query.filter(
        Dataset.kind == "guest",
        Dataset.system_key.is_(None),
        Dataset.absolute_expires_at > active_at,
        Dataset.last_activity_at > idle_cutoff,
    ).count()


def _cleanup_expired_guest_datasets(*, now=None):
    """現在のtransaction内で期限切れGuestと配下データを削除する。"""
    cleanup_time = _as_utc(now) if now is not None else datetime.datetime.now(
        datetime.timezone.utc
    )
    guest_datasets = Dataset.query.filter_by(
        kind="guest",
        system_key=None,
    ).all()
    expired_dataset_ids = [
        dataset.id
        for dataset in guest_datasets
        if _guest_dataset_is_expired(dataset, now=cleanup_time)
    ]

    if not expired_dataset_ids:
        return 0

    locked_guest_datasets = (
        Dataset.query
        .filter(
            Dataset.id.in_(expired_dataset_ids),
            Dataset.kind == "guest",
            Dataset.system_key.is_(None),
        )
        .order_by(Dataset.id)
        .populate_existing()
        .with_for_update()
        .all()
    )
    expired_dataset_ids = [
        dataset.id
        for dataset in locked_guest_datasets
        if _guest_dataset_is_expired(dataset, now=cleanup_time)
    ]

    if not expired_dataset_ids:
        return 0

    MaterialOrderItem.query.filter(
        MaterialOrderItem.dataset_id.in_(expired_dataset_ids)
    ).delete(synchronize_session=False)
    ShopMemo.query.filter(
        ShopMemo.dataset_id.in_(expired_dataset_ids)
    ).delete(synchronize_session=False)
    ShopTask.query.filter(
        ShopTask.dataset_id.in_(expired_dataset_ids)
    ).delete(synchronize_session=False)

    product_ids = [
        row[0]
        for row in (
            db.session.query(Product.id)
            .filter(Product.dataset_id.in_(expired_dataset_ids))
            .all()
        )
    ]

    if product_ids:
        DailySales.query.filter(
            DailySales.product_id.in_(product_ids)
        ).delete(synchronize_session=False)
        Product.query.filter(
            Product.id.in_(product_ids),
            Product.dataset_id.in_(expired_dataset_ids),
        ).delete(synchronize_session=False)

    deleted_count = Dataset.query.filter(
        Dataset.id.in_(expired_dataset_ids),
        Dataset.kind == "guest",
        Dataset.system_key.is_(None),
    ).delete(synchronize_session=False)
    db.session.flush()
    return deleted_count


def _get_admin_auth_fingerprint(password_hash):
    if not isinstance(password_hash, str) or not password_hash:
        return None

    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


@login_manager.user_loader
def load_user(user_id):
    if user_id == AdminUser.id:
        if not app.config.get("ADMIN_USERNAME"):
            return None

        current_fingerprint = _get_admin_auth_fingerprint(
            app.config.get("ADMIN_PASSWORD_HASH")
        )
        session_fingerprint = session.get(
            ADMIN_AUTH_FINGERPRINT_SESSION_KEY
        )
        if not current_fingerprint or not isinstance(session_fingerprint, str):
            return None
        if not hmac.compare_digest(current_fingerprint, session_fingerprint):
            return None

        return AdminUser()

    if not isinstance(user_id, str) or not user_id.startswith("guest:"):
        return None

    guest_dataset_id_text = user_id.removeprefix("guest:")
    try:
        guest_dataset_id = uuid.UUID(guest_dataset_id_text)
    except (ValueError, AttributeError):
        return None

    try:
        guest_dataset = Dataset.query.filter_by(
            id=guest_dataset_id,
            kind="guest",
            system_key=None,
        ).one_or_none()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to restore a Guest from its Dataset.")
        return None

    if guest_dataset is None:
        return None

    if _guest_dataset_is_expired(guest_dataset):
        return None

    guest_dataset.last_activity_at = datetime.datetime.now(
        datetime.timezone.utc
    )

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to update Guest activity.")
        return None

    return GuestUser(guest_dataset.id)


def admin_required(view_function):
    """Adminだけが業務routeへアクセスできるようにする。"""
    @wraps(view_function)
    @login_required
    def wrapped_view(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return view_function(*args, **kwargs)

    return wrapped_view


def admin_or_guest_required(view_function):
    """Adminまたは正規Guestだけが業務routeへアクセスできるようにする。"""
    @wraps(view_function)
    @login_required
    def wrapped_view(*args, **kwargs):
        principal = current_user._get_current_object()

        if not isinstance(principal, (AdminUser, GuestUser)):
            abort(403)

        return view_function(*args, **kwargs)

    return wrapped_view


def require_current_dataset():
    """現在の認証利用者が使用できるDatasetだけを返す。"""
    if not current_user.is_authenticated:
        abort(403)

    principal = current_user._get_current_object()
    if isinstance(principal, AdminUser):
        dataset_filters = {
            "kind": "admin",
            "system_key": "admin",
        }
        missing_status_code = 500
    elif isinstance(principal, GuestUser):
        dataset_filters = {
            "id": principal.dataset_id,
            "kind": "guest",
            "system_key": None,
        }
        missing_status_code = 403
    else:
        abort(403)

    try:
        dataset = Dataset.query.filter_by(**dataset_filters).one_or_none()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to resolve the current Dataset.")
        abort(503)

    if dataset is None:
        if missing_status_code == 500:
            logger.error("The Admin Dataset is missing.")
        abort(missing_status_code)

    if (
        isinstance(principal, GuestUser)
        and _guest_dataset_is_expired(dataset)
    ):
        abort(403)

    return dataset


app.register_blueprint(
    create_material_orders_blueprint(
        access_required=admin_or_guest_required,
        resolve_dataset=require_current_dataset,
    )
)
app.register_blueprint(
    create_shop_memos_blueprint(
        access_required=admin_or_guest_required,
        resolve_dataset=require_current_dataset,
    )
)
app.register_blueprint(
    create_shop_tasks_blueprint(
        access_required=admin_or_guest_required,
        resolve_dataset=require_current_dataset,
    )
)


def _reserve_guest_ai_usage(current_dataset):
    """GuestのGemini利用権をDB上でatomicに1回分確保する。"""
    principal = current_user._get_current_object()

    if isinstance(principal, AdminUser):
        if (
            current_dataset.kind != "admin"
            or current_dataset.system_key != "admin"
        ):
            abort(403)
        return True

    if not isinstance(principal, GuestUser):
        abort(403)

    if (
        principal.dataset_id != current_dataset.id
        or current_dataset.kind != "guest"
        or current_dataset.system_key is not None
    ):
        abort(403)

    reservation_time = datetime.datetime.now(datetime.timezone.utc)
    try:
        result = db.session.execute(
            update(Dataset)
            .execution_options(synchronize_session=False)
            .where(
                Dataset.id == current_dataset.id,
                Dataset.kind == "guest",
                Dataset.system_key.is_(None),
                Dataset.guest_ai_usage_count < GUEST_AI_USAGE_LIMIT,
                Dataset.absolute_expires_at > reservation_time,
                Dataset.last_activity_at > (
                    reservation_time - GUEST_IDLE_TIMEOUT
                ),
            )
            .values(
                guest_ai_usage_count=(
                    Dataset.guest_ai_usage_count + 1
                )
            )
        )

        if result.rowcount == 1:
            db.session.commit()
            return True

        db.session.rollback()
        dataset_state = (
            db.session.query(
                Dataset.guest_ai_usage_count,
                Dataset.last_activity_at,
                Dataset.absolute_expires_at,
            )
            .filter_by(
                id=current_dataset.id,
                kind="guest",
                system_key=None,
            )
            .one_or_none()
        )
        db.session.rollback()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to reserve Guest Gemini usage.")
        abort(503)

    if dataset_state is None or _guest_dataset_is_expired(dataset_state):
        abort(403)

    if dataset_state.guest_ai_usage_count >= GUEST_AI_USAGE_LIMIT:
        return False

    logger.error("Guest Gemini usage reservation affected no Dataset row.")
    abort(503)


def _guest_ai_limit_response():
    return jsonify({
        "error": "guest_ai_limit_reached",
        "message": "ゲストデモで利用できるAI機能は合計3回までです。",
        "limit": GUEST_AI_USAGE_LIMIT,
        "remaining": 0,
    }), 429


def _get_guest_creation_rate_limit_settings():
    """Guest作成rate limitの必須設定を正の整数として返す。"""
    try:
        max_attempts = int(
            app.config["GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS"]
        )
        window_seconds = int(
            app.config["GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS"]
        )
    except (KeyError, TypeError, ValueError):
        logger.error("Guest creation rate limit configuration is invalid.")
        abort(503)

    if max_attempts <= 0 or window_seconds <= 0:
        logger.error("Guest creation rate limit configuration is invalid.")
        abort(503)

    return max_attempts, window_seconds


def _get_admin_login_rate_limit_settings():
    """Adminログインrate limit設定を正の整数として返す。"""
    try:
        max_failures = int(
            app.config["ADMIN_LOGIN_RATE_LIMIT_MAX_FAILURES"]
        )
        window_seconds = int(
            app.config["ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS"]
        )
    except (KeyError, TypeError, ValueError):
        logger.error("Admin login rate limit configuration is invalid.")
        abort(503)

    if max_failures <= 0 or window_seconds <= 0:
        logger.error("Admin login rate limit configuration is invalid.")
        abort(503)

    return max_failures, window_seconds


def _get_guest_active_dataset_limit():
    """有効Guest Dataset上限を正の整数として返す。"""
    try:
        active_dataset_limit = int(
            app.config["GUEST_ACTIVE_DATASET_LIMIT"]
        )
    except (KeyError, TypeError, ValueError):
        logger.error("Guest active Dataset limit configuration is invalid.")
        abort(503)

    if active_dataset_limit <= 0:
        logger.error("Guest active Dataset limit configuration is invalid.")
        abort(503)

    return active_dataset_limit


def _acquire_guest_admission_lock():
    """PostgreSQLではGuest作成transactionを直列化する。"""
    dialect_name = db.session.get_bind().dialect.name

    if dialect_name == "sqlite":
        return

    if dialect_name != "postgresql":
        logger.error(
            "Guest admission locking does not support DB dialect: %s",
            dialect_name,
        )
        abort(503)

    db.session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            ":lock_namespace, :lock_key)"
        ),
        {
            "lock_namespace": GUEST_ADMISSION_LOCK_NAMESPACE,
            "lock_key": GUEST_ADMISSION_LOCK_KEY,
        },
    )


def _get_admin_login_advisory_lock_key(client_key_hash):
    """HMAC client keyをPostgreSQL用の符号付き64-bit keyへ変換する。"""
    try:
        client_key_bytes = bytes.fromhex(client_key_hash)
    except (TypeError, ValueError):
        logger.error("Admin login client key is invalid.")
        abort(503)

    if len(client_key_bytes) != hashlib.sha256().digest_size:
        logger.error("Admin login client key is invalid.")
        abort(503)

    return int.from_bytes(client_key_bytes[:8], "big", signed=True)


@contextmanager
def _serialize_admin_login_attempt(client_key_hash):
    """同一clientの上限確認から認証結果確定までを直列化する。"""
    dialect_name = db.session.get_bind().dialect.name

    if dialect_name == "sqlite":
        _ADMIN_LOGIN_SQLITE_LOCK.acquire()
        try:
            yield
        finally:
            db.session.rollback()
            _ADMIN_LOGIN_SQLITE_LOCK.release()
        return

    if dialect_name != "postgresql":
        logger.error(
            "Admin login locking does not support DB dialect: %s",
            dialect_name,
        )
        abort(503)

    advisory_lock_key = _get_admin_login_advisory_lock_key(
        client_key_hash
    )
    try:
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": advisory_lock_key},
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to lock an Admin login attempt.")
        abort(503)

    try:
        yield
    finally:
        db.session.rollback()


def _get_rate_limit_client_key(
    hmac_domain,
    *,
    purpose,
    test_client_ip_config_key,
):
    """検証済みclient IPを用途分離した匿名keyへ変換する。"""
    if app.testing:
        raw_ip = request.remote_addr or app.config.get(
            test_client_ip_config_key
        )
    elif app.config.get("LOCAL_DEVELOPMENT") is True:
        raw_ip = request.remote_addr
    else:
        raw_ip = request.headers.get("CF-Connecting-IP")

    try:
        parsed_ip = ipaddress.ip_address(raw_ip)
    except (TypeError, ValueError):
        logger.warning("%s client IP is missing or invalid.", purpose)
        abort(503)

    if isinstance(parsed_ip, ipaddress.IPv6Address):
        parsed_ip = parsed_ip.ipv4_mapped or parsed_ip
    normalized_ip = str(parsed_ip)

    secret_key = app.config.get("SECRET_KEY")
    if isinstance(secret_key, str):
        secret_key = secret_key.encode("utf-8")
    if not isinstance(secret_key, bytes) or not secret_key:
        logger.error("SECRET_KEY is unavailable for %s.", purpose)
        abort(503)

    message = f"{hmac_domain}:{normalized_ip}".encode("utf-8")
    return hmac.new(secret_key, message, hashlib.sha256).hexdigest()


def _get_guest_creation_client_key():
    """Guest作成用の匿名client keyを返す。"""
    return _get_rate_limit_client_key(
        GUEST_CREATION_RATE_LIMIT_HMAC_DOMAIN,
        purpose="Guest creation rate limiting",
        test_client_ip_config_key=(
            "GUEST_CREATION_RATE_LIMIT_TEST_CLIENT_IP"
        ),
    )


def _get_admin_login_client_key():
    """Adminログイン用の匿名client keyを返す。"""
    return _get_rate_limit_client_key(
        ADMIN_LOGIN_RATE_LIMIT_HMAC_DOMAIN,
        purpose="Admin login rate limiting",
        test_client_ip_config_key=(
            "ADMIN_LOGIN_RATE_LIMIT_TEST_CLIENT_IP"
        ),
    )


def _reserve_fixed_window_attempt(
    client_key_hash,
    max_attempts,
    window_seconds,
    *,
    now=None,
    purpose,
):
    """DBの単一UPSERTでfixed-windowの1回分をatomicに確保する。"""
    attempt_time = _as_utc(now) if now is not None else datetime.datetime.now(
        datetime.timezone.utc
    )
    window_cutoff = attempt_time - datetime.timedelta(
        seconds=window_seconds
    )
    table = GuestCreationRateLimit.__table__
    dialect_name = db.session.get_bind().dialect.name

    if dialect_name == "postgresql":
        insert_statement = postgresql_insert(table)
    elif dialect_name == "sqlite":
        insert_statement = sqlite_insert(table)
    else:
        logger.error(
            "Guest creation rate limiting does not support DB dialect: %s",
            dialect_name,
        )
        abort(503)

    insert_statement = insert_statement.values(
        client_key_hash=client_key_hash,
        window_started_at=attempt_time,
        request_count=1,
        updated_at=attempt_time,
    )
    window_has_expired = table.c.window_started_at <= window_cutoff
    reservation_statement = insert_statement.on_conflict_do_update(
        index_elements=[table.c.client_key_hash],
        set_={
            "window_started_at": case(
                (window_has_expired, attempt_time),
                else_=table.c.window_started_at,
            ),
            "request_count": case(
                (window_has_expired, 1),
                else_=table.c.request_count + 1,
            ),
            "updated_at": attempt_time,
        },
        where=or_(
            window_has_expired,
            table.c.request_count < max_attempts,
        ),
    )

    try:
        result = db.session.execute(reservation_statement)
        if result.rowcount == 1:
            db.session.commit()
            return True

        db.session.rollback()
        return False
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to reserve a %s attempt.", purpose)
        abort(503)


def _reserve_guest_creation_attempt(client_key_hash, *, now=None):
    """Guest作成試行をatomicに1回分確保する。"""
    max_attempts, window_seconds = (
        _get_guest_creation_rate_limit_settings()
    )
    return _reserve_fixed_window_attempt(
        client_key_hash,
        max_attempts,
        window_seconds,
        now=now,
        purpose="Guest creation",
    )


def _reserve_admin_login_failure(client_key_hash, *, now=None):
    """Adminログイン失敗をatomicに1回分記録する。"""
    max_failures, window_seconds = _get_admin_login_rate_limit_settings()
    return _reserve_fixed_window_attempt(
        client_key_hash,
        max_failures,
        window_seconds,
        now=now,
        purpose="Admin login failure",
    )


def _admin_login_rate_limit_is_reached(
    client_key_hash,
    *,
    now=None,
    end_transaction=True,
):
    """現在の時間窓でAdminログイン失敗上限に達したか確認する。"""
    max_failures, window_seconds = _get_admin_login_rate_limit_settings()
    check_time = _as_utc(now) if now is not None else datetime.datetime.now(
        datetime.timezone.utc
    )
    window_cutoff = check_time - datetime.timedelta(seconds=window_seconds)
    table = GuestCreationRateLimit.__table__

    try:
        row = db.session.execute(
            select(
                table.c.window_started_at,
                table.c.request_count,
            ).where(table.c.client_key_hash == client_key_hash)
        ).one_or_none()
        if end_transaction:
            db.session.rollback()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to check the Admin login rate limit.")
        abort(503)

    if row is None:
        return False

    return (
        _as_utc(row.window_started_at) > window_cutoff
        and row.request_count >= max_failures
    )


def _admin_login_rate_limit_response():
    logger.warning("Admin login rate limit reached.")
    return _render_login_page(
        error=(
            "ログイン試行回数が上限に達しました。"
            "時間を置いてからお試しください。"
        ),
        status_code=429,
    )


def start_guest_session():
    """Guest Datasetを作成し、対応するGuest identityを発行する。"""
    if current_user.is_authenticated:
        abort(409)

    client_key_hash = _get_guest_creation_client_key()
    if not _reserve_guest_creation_attempt(client_key_hash):
        abort(
            429,
            description=(
                "ゲストデモの開始回数が上限に達しました。"
                "時間を置いてからお試しください。"
            ),
        )

    active_dataset_limit = _get_guest_active_dataset_limit()
    capacity_is_full = False
    guest_dataset = None

    try:
        _acquire_guest_admission_lock()
        now = datetime.datetime.now(datetime.timezone.utc)
        _cleanup_expired_guest_datasets(now=now)
        active_guest_count = _get_active_guest_dataset_count(now=now)

        if active_guest_count >= active_dataset_limit:
            capacity_is_full = True
        else:
            guest_dataset = Dataset(
                kind="guest",
                system_key=None,
                created_at=now,
                last_activity_at=now,
                absolute_expires_at=now + GUEST_ABSOLUTE_LIFETIME,
            )
            db.session.add(guest_dataset)

        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception(
            "Failed to enforce Guest capacity or create a Guest Dataset."
        )
        abort(503)

    if capacity_is_full:
        raise GuestCapacityFull(
            active_guest_count,
            active_dataset_limit,
        )

    guest_user = GuestUser(guest_dataset.id)
    if not login_user(guest_user):
        logger.error("Failed to establish the Guest identity.")
        abort(500)

    return guest_dataset


def _unavailable_guest_capacity(message):
    return {
        "available": False,
        "active_count": None,
        "limit": None,
        "full": False,
        "disabled": True,
        "message": message,
    }


def _get_guest_capacity_for_display():
    """副作用なしで、ログイン画面用の参考capacityを取得する。"""
    try:
        active_dataset_limit = _get_guest_active_dataset_limit()
        active_guest_count = _get_active_guest_dataset_count()
    except ServiceUnavailable:
        db.session.rollback()
        return _unavailable_guest_capacity(
            "現在のゲストデモ利用状況を取得できません。"
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to load Guest capacity for display.")
        return _unavailable_guest_capacity(
            "現在のゲストデモ利用状況を取得できません。"
        )

    capacity_is_full = active_guest_count >= active_dataset_limit
    return {
        "available": True,
        "active_count": active_guest_count,
        "limit": active_dataset_limit,
        "full": capacity_is_full,
        "disabled": capacity_is_full,
        "message": "ただいま満員です。" if capacity_is_full else None,
    }


def _render_login_page(
    *,
    error=None,
    guest_capacity=_GUEST_CAPACITY_NOT_LOADED,
    status_code=200,
):
    if guest_capacity is _GUEST_CAPACITY_NOT_LOADED:
        guest_capacity = _get_guest_capacity_for_display()

    return render_template(
        "login.html",
        error=error,
        guest_capacity=guest_capacity,
    ), status_code


def get_admin_dataset():
    """system_keyから管理者Datasetを取得し、異常時は安全に失敗する。"""
    try:
        return Dataset.query.filter_by(
            kind="admin",
            system_key="admin",
        ).one_or_none()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to load the admin Dataset.")
        return None


def _generate_ai_advice(ranked_sales, current_dataset=None):
    if not ranked_sales:
        logger.warning("AI advice requested but ranked_sales is empty.")  # 💡 注意喚起
        return "売上データがまだないため、アドバイスを生成できません。"

    if current_dataset is not None and current_dataset.kind == "guest":
        # SQLの件数制限に加え、legacy等の不正な集計を送信前に拒否する。
        # 完成したpromptを途中で切らず、商品名・数量を完全な形で保つ。
        if len(ranked_sales) > GUEST_PRODUCT_LIFETIME_LIMIT or any(
            not isinstance(name, str)
            or not 1 <= len(name) <= PRODUCT_NAME_MAX_LENGTH
            or type(qty) is not int
            or not 0 <= qty <= GUEST_AI_AGGREGATE_QUANTITY_MAX
            for name, qty in ranked_sales
        ):
            abort(400, description="AI分析対象データが上限を超えています。")

    try:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.error("GEMINI_API_KEY is missing from environment variables.")  # 💡 設定エラーの記録
            return "現在AIアドバイスを利用できません。時間を置いてからお試しください。"

        client = genai.Client(
            api_key=api_key,
            http_options={
                "timeout": GEMINI_REQUEST_TIMEOUT_MILLISECONDS,
            },
        )
        sales_summary = ", ".join([f"{name}: {qty}個" for name, qty in ranked_sales])
        prompt = build_sales_prompt(sales_summary)

        if (
            current_dataset is not None
            and not _reserve_guest_ai_usage(current_dataset)
        ):
            return _GUEST_AI_LIMIT_REACHED

        logger.info(f"Requesting Gemini AI advice for products: {len(ranked_sales)} items.")
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
        )
        logger.info("Gemini AI advice generated successfully.")
        return response.text

    except HTTPException:
        raise
    except Exception as e:
        error_text = str(e)

        if "GenerateRequestsPerDayPerProjectPerModel-FreeTier" in error_text:
            logger.warning(
                "Gemini API daily free-tier quota reached."
            )
            return (
                "☕【本日のAI分析回数が上限に達しました】\n"
                "売上データの保存と集計は正常です。"
                "時間を置いてから、あらためてAI分析をお試しください。"
            )

        if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
            logger.warning(
                "Gemini API rate limit hit (429)."
            )
            return (
                "☕【AIが少し休憩中です】\n"
                "短時間に多くの分析を行ったため、"
                "AIの利用制限がかかりました。"
                "少し時間を置いてから、もう一度お試しください。"
            )

        if "503" in error_text or "UNAVAILABLE" in error_text:
            logger.warning(
                "Gemini API temporarily unavailable (503)."
            )
            return (
                "🥐【AIアシスタントが混み合っています】\n"
                "売上データは正常に保存・集計されています。"
                "少し時間を置いてから、もう一度お試しください。"
            )

        logger.error(
            "Unexpected error during AI advice generation."
        )
        return (
            "🚨 AIアドバイスの生成中に一時的なエラーが発生しました。"
            "時間を置いてから、もう一度お試しください。"
        )


@app.route("/guest/start", methods=["POST"])
def start_guest_demo():
    try:
        start_guest_session()
    except GuestCapacityFull as error:
        guest_capacity = {
            "available": True,
            "active_count": error.active_guest_count,
            "limit": error.active_dataset_limit,
            "full": True,
            "disabled": True,
            "message": (
                "ただいまゲストデモは満員です。"
                "時間を置いてからお試しください。"
            ),
        }
        return _render_login_page(
            guest_capacity=guest_capacity,
            status_code=503,
        )
    except TooManyRequests:
        return _render_login_page(
            guest_capacity=_unavailable_guest_capacity(
                "短時間にゲストデモの開始操作が繰り返されました。"
                "時間を置いてからお試しください。"
            ),
            status_code=429,
        )
    except Conflict:
        return _render_login_page(
            guest_capacity=_unavailable_guest_capacity(
                "すでにログインしています。現在の画面からご利用ください。"
            ),
            status_code=409,
        )
    except ServiceUnavailable:
        return _render_login_page(
            guest_capacity=_unavailable_guest_capacity(
                "現在ゲストデモを開始できません。"
                "時間を置いてからお試しください。"
            ),
            status_code=503,
        )
    except InternalServerError:
        return _render_login_page(
            guest_capacity=_unavailable_guest_capacity(
                "ゲストデモを開始できませんでした。"
                "時間を置いてからお試しください。"
            ),
            status_code=500,
        )

    return redirect(url_for("index"), code=303)


@app.get("/system-overview")
def system_overview():
    """実装を復習するための公開ページ。業務データは取得しない。"""
    return render_template("system_overview.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        client_key_hash = _get_admin_login_client_key()
        with _serialize_admin_login_attempt(client_key_hash):
            if _admin_login_rate_limit_is_reached(
                client_key_hash,
                end_transaction=False,
            ):
                return _admin_login_rate_limit_response()

            username = request.form.get("username", "")
            password = request.form.get("password", "")
            configured_username = app.config.get("ADMIN_USERNAME")
            configured_password_hash = app.config.get(
                "ADMIN_PASSWORD_HASH"
            )

            password_matches = False
            if configured_password_hash:
                try:
                    password_matches = check_password_hash(
                        configured_password_hash,
                        password,
                    )
                except (TypeError, ValueError):
                    logger.exception(
                        "Invalid administrator password hash configuration."
                    )

            if (
                configured_username
                and username == configured_username
                and password_matches
            ):
                login_user(AdminUser())
                session[ADMIN_AUTH_FINGERPRINT_SESSION_KEY] = (
                    _get_admin_auth_fingerprint(configured_password_hash)
                )
                return redirect(url_for("index"))

            if not _reserve_admin_login_failure(client_key_hash):
                return _admin_login_rate_limit_response()

            logger.warning("Administrator login failed.")
            return _render_login_page(
                error=(
                    "ユーザー名またはパスワードが"
                    "正しくありません。"
                ),
                status_code=401,
            )

    return _render_login_page()


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    """現在のAdminまたはGuestの認証Sessionだけを終了する。"""
    logout_user()
    session.pop(ADMIN_AUTH_FINGERPRINT_SESSION_KEY, None)
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@admin_or_guest_required
def index():
    try:
        current_dataset = require_current_dataset()
    except InternalServerError:
        if request.method == "POST":
            return "管理者データ領域が見つかりません。", 500
        current_dataset = None

    today = business_today()
    product_image_options = available_product_images(
        app.static_folder, is_guest=current_user.is_guest,
    )
    product_image_assets = available_product_image_assets(
        app.static_folder, is_guest=current_user.is_guest,
    )

    if request.method == "POST":
        if not current_user.is_authenticated:
            return login_manager.unauthorized()

        try:
            year = int(request.form.get("year"))
            month = int(request.form.get("month"))
        except (TypeError, ValueError):
            logger.warning("Rejected product update with invalid year or month.")
            return "年月が正しくありません。", 400

        if month < 1 or month > 12:
            logger.warning("Rejected product update with out-of-range month.")
            return "月は1から12で指定してください。", 400

        if year < PRODUCT_YEAR_MIN or year > PRODUCT_YEAR_MAX:
            logger.warning("Rejected product update with out-of-range year.")
            return "年は2000から2100で指定してください。", 400

        product_names = request.form.getlist("prod_name")
        product_prices = request.form.getlist("prod_price")
        product_ids = request.form.getlist("product_id")
        image_keys = request.form.getlist("prod_image_key")
        template_keys = request.form.getlist("prod_template_key")

        if not (
            len(product_ids) == len(product_names) == len(product_prices)
        ):
            logger.warning("Rejected product update with mismatched field lengths.")
            return "商品データの件数が一致しません。", 400

        # 画像フィールドがない旧フォームも受け入れ、既存画像を保持する。
        if image_keys and len(image_keys) != len(product_names):
            logger.warning("Rejected product update with mismatched image field lengths.")
            return "商品画像の件数が一致しません。", 400
        if not image_keys:
            image_keys = [""] * len(product_names)

        if template_keys and len(template_keys) != len(product_names):
            logger.warning(
                "Rejected product update with mismatched template field lengths."
            )
            return "商品テンプレートの件数が一致しません。", 400

        if not template_keys:
            template_keys = [""] * len(product_names)

        if current_user.is_guest and len(product_ids) > PRODUCTS_PER_POST_LIMIT:
            logger.warning("Rejected product update with too many products.")
            return "商品は1回につき30件まで登録できます。", 400

        products_data = []
        seen_product_ids = set()

        for product_id, name, price, image_key, template_key in zip(
            product_ids,
            product_names,
            product_prices,
            image_keys,
            template_keys,
        ):
            existing_product = None
            normalized_name = name.strip()

            if not 1 <= len(normalized_name) <= PRODUCT_NAME_MAX_LENGTH:
                logger.warning("Rejected product update with invalid product name.")
                return "商品名は1文字以上100文字以内で入力してください。", 400

            if product_id:
                try:
                    parsed_product_id = int(product_id)
                except ValueError:
                    logger.warning("Rejected product update with invalid product ID.")
                    return "商品IDが正しくありません。", 400

                if parsed_product_id in seen_product_ids:
                    logger.warning("Rejected product update with duplicate product ID.")
                    return "同じ商品が複数回送信されています。", 400

                existing_product = Product.query.filter_by(
                    id=parsed_product_id,
                    dataset_id=current_dataset.id,
                ).one_or_none()
                if existing_product is None:
                    logger.warning("Rejected product update with unknown product ID.")
                    return "指定された商品が見つかりません。", 400

                if (
                    existing_product.year != year
                    or existing_product.month != month
                ):
                    logger.warning("Rejected product update for another year or month.")
                    return "指定された商品は選択年月の商品ではありません。", 400

                seen_product_ids.add(parsed_product_id)
            else:
                parsed_product_id = None

            price_value = _parse_bounded_nonnegative_integer(
                price,
                PRODUCT_PRICE_MAX,
            )
            if price_value is None or price_value % PRODUCT_PRICE_STEP != 0:
                logger.warning("Rejected product update with invalid price.")
                return "売価は0〜9,990円の10円単位で入力してください。", 400

            if image_key and image_key not in product_image_assets:
                logger.warning("Rejected product update with unapproved image key.")
                return "商品画像が正しくありません。", 400

            template_option = None
            if template_key:
                template_option = product_image_options.get(template_key)
                if template_option is None:
                    logger.warning(
                        "Rejected product update with unapproved template key."
                    )
                    return "商品テンプレートが正しくありません。", 400

                if normalized_name != template_option["label"]:
                    logger.warning(
                        "Rejected product update with mismatched template name."
                    )
                    return "商品名とテンプレートが一致しません。", 400

            if current_user.is_guest:
                if template_option is not None:
                    expected_image_key = (
                        template_option.get("image_key", template_key)
                        if template_option.get("filename") else ""
                    )
                    if image_key != expected_image_key:
                        logger.warning(
                            "Rejected Guest product update with mismatched "
                            "template image."
                        )
                        return "商品名と商品画像の組み合わせが正しくありません。", 400

                elif image_key:
                    image_option = product_image_assets.get(image_key)
                    if (
                        image_option is None
                        or normalized_name != image_option["label"]
                    ):
                        logger.warning(
                            "Rejected Guest product update combining a free "
                            "product name with a template image."
                        )
                        return "自由入力の商品にはテンプレート画像を指定できません。", 400

            products_data.append({
                "id": parsed_product_id,
                "product": existing_product,
                "name": normalized_name,
                "price": price_value,
                "image_key": image_key or None,
            })

        if not products_data:
            registered_months = [
                row[0]
                for row in (
                    db.session.query(Product.month)
                    .filter_by(
                        dataset_id=current_dataset.id,
                        year=year,
                    )
                    .distinct()
                    .all()
                )
            ]

            return render_template(
                "index.html",
                error="商品を1つ以上入力してください。",
                products=[],
                selected_year=year,
                selected_month=month,
                registered_months=registered_months,
                current_year=today.year,
                product_image_options=product_image_options,
                product_image_assets=product_image_assets,
                admin_catalog_options={
                    key: option for key, option in product_image_options.items()
                    if key.startswith("admin_")
                } if current_user.is_admin else {},
            )

        # 💡既存商品の価格更新と新商品の追加をログに残す
        logger.info(f"Updating product master for {year}-{month}.")

        new_product_count = sum(
            prod["id"] is None
            for prod in products_data
        )

        try:
            if current_user.is_guest:
                locked_guest_dataset = (
                    Dataset.query
                    .filter_by(
                        id=current_dataset.id,
                        kind="guest",
                        system_key=None,
                    )
                    .populate_existing()
                    .with_for_update()
                    .one_or_none()
                )
                if locked_guest_dataset is None:
                    db.session.rollback()
                    abort(403)

                current_product_count = Product.query.filter_by(
                    dataset_id=locked_guest_dataset.id,
                ).count()
                remaining_product_slots = max(
                    GUEST_PRODUCT_LIFETIME_LIMIT - current_product_count,
                    0,
                )
                if new_product_count > remaining_product_slots:
                    db.session.rollback()
                    logger.warning(
                        "Rejected Guest product update exceeding lifetime limit."
                    )
                    return "ゲストデモの商品は合計30件まで登録できます。", 400

            existing_products = Product.query.filter_by(
                dataset_id=current_dataset.id,
                year=year,
                month=month,
            ).all()

            submitted_ids = {
                prod["id"]
                for prod in products_data
                if prod["id"] is not None
            }

            for prod in products_data:
                product_id = prod["id"]

                if product_id is not None:
                    # 既存商品の商品名と価格を更新
                    existing_product = prod["product"]
                    existing_product.name = prod["name"]
                    existing_product.price = prod["price"]
                    if prod["image_key"] is not None:
                        existing_product.image_key = prod["image_key"]
                    existing_product.is_active = True

                else:
                    # IDがない商品は新規追加
                    db.session.add(
                        Product(
                            dataset=current_dataset,
                            year=year,
                            month=month,
                            name=prod["name"],
                            price=prod["price"],
                            image_key=prod["image_key"],
                        )
                    )

            for product in existing_products:
                if product.id not in submitted_ids:
                    product.is_active = False

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save product master.")
            return "商品マスタを保存できませんでした。", 500

        logger.info(
            f"Successfully updated product master with "
            f"{len(products_data)} submitted products for {year}-{month}."
        )

        return render_template("success.html", year=year, month=month)


    year = today.year
    month = request.args.get("month", type=int)

    if month is None:
        month = today.month

    if current_dataset is None:
        products = []
        registered_months = []
    else:
        products = Product.query.filter_by(
            dataset_id=current_dataset.id,
            year=year,
            month=month,
            is_active=True
        ).all()

        registered_months = (
            db.session.query(Product.month)
            .filter_by(
                dataset_id=current_dataset.id,
                year=year,
            )
            .distinct()
            .all()
        )

    registered_months = [m[0] for m in registered_months]

    return render_template(
        "index.html",
        products=products,
        selected_year=year,
        selected_month=month,
        registered_months=registered_months,
        current_year=today.year,
        product_image_options=product_image_options,
        product_image_assets=product_image_assets,
        admin_catalog_options={
            key: option for key, option in product_image_options.items()
            if key.startswith("admin_")
        } if current_user.is_admin else {},
    )


@app.route("/admin/product-catalog", methods=["POST"])
@admin_required
def submit_product_catalog():
    """検証済みの商品申請を専用branchのPRとして公開する。"""
    require_current_dataset()
    label = request.form.get("catalog_label", "").strip()
    reading = request.form.get("catalog_reading", "").strip()
    price = _parse_bounded_nonnegative_integer(
        request.form.get("catalog_default_price"), PRODUCT_PRICE_MAX,
    )
    edit_key = request.form.get("catalog_edit_key", "").strip() or None
    guest_allowed = request.form.get("catalog_guest_allowed") == "on"
    if (
        not 1 <= len(label) <= PRODUCT_NAME_MAX_LENGTH
        or price is None or price % PRODUCT_PRICE_STEP
        or not 1 <= len(reading) <= 100
        or any(not ('\u3040' <= char <= '\u309f' or char == 'ー') for char in reading)
        or (edit_key is not None and (
            len(edit_key) != 38 or not edit_key.startswith("admin_")
            or any(char not in "0123456789abcdef" for char in edit_key[6:])
        ))
        or request.form.get("catalog_guest_allowed", "") not in ("", "on")
    ):
        return "商品カタログの入力内容が正しくありません。", 400

    upload = request.files.get("catalog_image")
    try:
        image = validate_image(upload) if upload is not None and upload.filename else None
        url = publish_catalog_change(
            label=label, default_price=price, reading=reading, image=image,
            edit_key=edit_key, guest_allowed=guest_allowed,
        )
    except InvalidCatalogImage as error:
        return str(error), 400
    except ValueError as error:
        return str(error), 400
    except CatalogPublishError:
        logger.exception("Failed to submit product catalog PR.")
        return "商品カタログの申請を保存できませんでした。", 503
    return render_template("catalog_submitted.html", pull_request_url=url)


@app.route("/products", methods=["GET"])
@admin_or_guest_required
def product_catalog():
    """今月の登録商品と、過去月の最新履歴をDataset内で表示する。"""
    current_dataset = require_current_dataset()
    today = business_today()
    products = (
        Product.query
        .filter_by(dataset_id=current_dataset.id)
        .order_by(Product.year.desc(), Product.month.desc(), Product.id.desc())
        .all()
    )
    current_products = []
    past_products = []
    current_names = set()
    for product in products:
        if (
            product.year == today.year
            and product.month == today.month
            and product.is_active
            and product.name not in current_names
        ):
            current_names.add(product.name)
            current_products.append(product)

    seen_names = set(current_names)
    for product in products:
        if (product.year, product.month) >= (today.year, today.month):
            continue
        if product.name in seen_names:
            continue
        seen_names.add(product.name)
        past_products.append(product)

    return render_template(
        "products.html",
        current_products=current_products,
        past_products=past_products,
        current_year=today.year,
        current_month=today.month,
        product_image_options=available_product_images(
            app.static_folder, is_guest=current_user.is_guest,
        ),
        product_image_assets=available_product_image_assets(
            app.static_folder, is_guest=current_user.is_guest,
        ),
    )


def _get_optional_integer_query_parameter(name):
    value = request.args.get(name)
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        abort(400)


def _get_dashboard_period():
    target_year = _get_optional_integer_query_parameter("year")
    target_month = _get_optional_integer_query_parameter("month")

    if target_month is not None and not 1 <= target_month <= 12:
        abort(400)

    if target_year is None:
        target_month = None

    return target_year, target_month


def _dashboard_period_label(target_year, target_month):
    if target_year is None:
        return "全年度・全月"
    if target_month is None:
        return f"{target_year}年・全月"
    return f"{target_year}年{target_month}月"


@app.route("/dashboard")
@admin_or_guest_required
def dashboard():
    target_year, target_month = _get_dashboard_period()
    current_dataset = require_current_dataset()
    today = business_today()
    available_years = _get_dashboard_sales_years(
        current_dataset,
        today.year,
        target_year,
    )
    sales_months = _get_dashboard_sales_months(
        current_dataset,
        target_year,
    )
    period_text = _dashboard_period_label(target_year, target_month)

    logger.info(f"Dashboard accessed for period: year={target_year}, month={target_month}")

    sales_data = _get_sales_from_db(
        current_dataset,
        target_year,
        target_month,
    )
    ranked_sales = sorted(sales_data.items(), key=lambda item: item[1], reverse=True)

    chart_labels = [name for name, qty in ranked_sales]
    chart_values = [qty for name, qty in ranked_sales]
    ai_advice = (
        "売上ランキングとグラフを確認できます。"
        "さらに詳しい改善案を知りたい場合は、"
        "「詳しいアドバイスを聞く」ボタンを押してください。"
    )

    return render_template("dashboard.html",
                           today_sales=_get_today_sales_amount(current_dataset, today),
                           business_date=today.isoformat(),
                           sales=sales_data,
                           ranked_sales=ranked_sales,
                           chart_labels=chart_labels,
                           chart_values=chart_values,
                           ai_advice=ai_advice,
                           year=target_year,
                           month=target_month,
                           available_years=available_years,
                           sales_months=sales_months,
                           period_text=period_text)


@app.route("/api/dashboard-data")
@admin_or_guest_required
def api_dashboard_data():
    target_year, target_month = _get_dashboard_period()
    current_dataset = require_current_dataset()
    sales_months = _get_dashboard_sales_months(
        current_dataset,
        target_year,
    )

    logger.info(f"API Dashboard data requested for period: year={target_year}, month={target_month}")

    sales_data = _get_sales_from_db(
        current_dataset,
        target_year,
        target_month,
    )
    ranked_sales = sorted(sales_data.items(), key=lambda item: item[1], reverse=True)

    chart_labels = [name for name, qty in ranked_sales]
    chart_values = [qty for name, qty in ranked_sales]

    today = business_today()
    return jsonify({
        "today_sales": _get_today_sales_amount(current_dataset, today),
        "business_date": today.isoformat(),
        "ranked_sales": ranked_sales,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "sales_months": sales_months,
        "ai_advice": (
            "売上ランキングとグラフを更新しました。"
            "詳しい改善案を確認する場合は、"
            "「詳しいアドバイスを聞く」ボタンを押してください。"
        ),
        "period_text": _dashboard_period_label(target_year, target_month),
    })

@app.route("/api/ai-advice", methods=["POST"])
@admin_or_guest_required
def api_ai_advice():
    target_year, target_month = _get_dashboard_period()
    current_dataset = require_current_dataset()

    logger.info(
        f"AI advice requested for period: "
        f"year={target_year}, month={target_month}"
    )

    sales_data = _get_sales_from_db(
        current_dataset,
        target_year,
        target_month,
        row_limit=(
            GUEST_PRODUCT_LIFETIME_LIMIT
            if current_dataset.kind == "guest"
            else None
        ),
    )

    ranked_sales = sorted(
        sales_data.items(),
        key=lambda item: item[1],
        reverse=True
    )

    ai_advice = _generate_ai_advice(ranked_sales, current_dataset)
    if ai_advice is _GUEST_AI_LIMIT_REACHED:
        return _guest_ai_limit_response()

    return jsonify({
        "ai_advice": ai_advice
    })

@app.route("/input", methods=["GET", "POST"])
@admin_or_guest_required
def input_sales():
    today = business_today()
    current_dataset = require_current_dataset()

    if request.method == "POST":
        if not current_user.is_authenticated:
            return login_manager.unauthorized()

        date_str = request.form.get("date")
        try:
            sale_date = datetime.date.fromisoformat(date_str)
        except (TypeError, ValueError):
            logger.warning(f"Invalid sales date rejected: {date_str}")
            return "売上日の日付形式が正しくありません。", 400

        product_ids = request.form.getlist("product_id")
        quantities = request.form.getlist("quantity")

        if not product_ids or not quantities:
            logger.warning("Empty sales submission rejected.")
            return "商品と販売数量を入力してください。", 400

        if len(product_ids) != len(quantities):
            logger.warning(
                "Mismatched sales input lengths rejected: "
                f"product_ids={len(product_ids)}, "
                f"quantities={len(quantities)}"
            )
            return "商品と販売数量の件数が一致しません。", 400

        if current_user.is_guest and len(product_ids) > SALES_PER_POST_LIMIT:
            logger.warning("Rejected sales input with too many products.")
            return "売上は1回につき30件まで入力できます。", 400

        validated_sales = []
        seen_product_ids = set()
        for product_id, quantity in zip(product_ids, quantities):
            try:
                product_id_int = int(product_id)
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid product ID rejected: product_id={product_id}"
                )
                return "商品IDが正しくありません。", 400

            if product_id_int in seen_product_ids:
                logger.warning(
                    f"Duplicate product ID rejected: product_id={product_id}"
                )
                return "同じ商品が複数回送信されています。", 400

            seen_product_ids.add(product_id_int)

            quantity_value = _parse_bounded_nonnegative_integer(
                quantity,
                SALES_QUANTITY_MAX,
            )
            if quantity_value is None:
                logger.warning("Rejected sales input with invalid quantity.")
                return "販売数量は0〜1,000個の整数で入力してください。", 400

            validated_sales.append((product_id_int, quantity_value))

        validated_product_sales = []
        for product_id, qty_int in validated_sales:
            product = Product.query.filter_by(
                id=product_id,
                dataset_id=current_dataset.id,
            ).one_or_none()

            if product is None:
                logger.warning(
                    f"Unknown product rejected: product_id={product_id}"
                )
                return "指定された商品が存在しません。", 400

            if (
                product.year != sale_date.year
                or product.month != sale_date.month
            ):
                logger.warning(
                    "Product outside sales month rejected: "
                    f"product_id={product_id}, date={sale_date}"
                )
                return "売上日と商品の対象年月が一致しません。", 400

            if not product.is_active:
                logger.warning(
                    f"Inactive product rejected: product_id={product_id}"
                )
                return "販売終了商品には売上を登録できません。", 400

            validated_product_sales.append((product, qty_int))

        logger.info(
            f"Sales data submission received for date: {sale_date}"
        )

        try:
            for product, qty_int in validated_product_sales:
                daily_sales_table = DailySales.__table__
                sale_values = {
                    "product_id": product.id,
                    "date": sale_date,
                    "quantity": qty_int,
                }
                dialect_name = db.session.get_bind().dialect.name

                if dialect_name == "postgresql":
                    insert_statement = postgresql_insert(
                        daily_sales_table
                    ).values(**sale_values)
                elif dialect_name == "sqlite":
                    insert_statement = sqlite_insert(
                        daily_sales_table
                    ).values(**sale_values)
                else:
                    insert_statement = None

                if insert_statement is not None:
                    upsert_statement = insert_statement.on_conflict_do_update(
                        index_elements=[
                            daily_sales_table.c.product_id,
                            daily_sales_table.c.date,
                        ],
                        set_={"quantity": qty_int},
                    )
                    db.session.execute(upsert_statement)
                    continue

                existing = DailySales.query.filter_by(
                    product_id=product.id,
                    date=sale_date,
                ).first()
                if existing:
                    existing.quantity = qty_int
                else:
                    db.session.add(DailySales(**sale_values))

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Failed to save sales data.")
            return "売上データを保存できませんでした。", 500

        logger.info(
            f"Sales data successfully committed for date: {sale_date}"
        )

        return render_template(
            "input.html",
            success=True,
            products=_get_current_products(current_dataset, today),
            today=today,
            today_sales=_get_today_sales_map(today, current_dataset)
        )

    return render_template(
        "input.html",
        products=_get_current_products(current_dataset, today),
        today=today,
        today_sales=_get_today_sales_map(today, current_dataset)
    )

@app.route("/api/greeting", methods=["POST"])
@admin_or_guest_required
def api_greeting():
    current_dataset = require_current_dataset()
    today = business_today()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"message": f"本日は{today.strftime('%-m月%-d日')}です。今日も一日お疲れ様でした！"})

    try:
        client = genai.Client(
            api_key=api_key,
            http_options={
                "timeout": GEMINI_REQUEST_TIMEOUT_MILLISECONDS,
            },
        )

        # 💡 どんなプロンプトでリクエストを投げようとしているかINFOで記録
        logger.info("Generating AI daily greeting...")
        
        prompt = f"""
            あなたはベーカリーのスタッフに話しかける、明るく親しみやすいアシスタントです。
            今日は{today.month}月{today.day}日（{['月','火','水','木','金','土','日'][today.weekday()]}曜日）です。
            以下のどれか1〜2個を自然に盛り込んで、スタッフへの一言挨拶を作ってください。
            - 今の季節感（食材・行事・気候など）
            - 今のSNSやトレンドで話題になっているパンや食品
            - 季節の新商品へのさりげない提案
            条件：
            - 全体で2〜3文
            - 親しみやすいが馴れ馴れしすぎない口調
            - 最後は「何か特別よく売れた商品はありましたか？」で締めくくる
            - 嘘の情報は入れない（SNSトレンドは「〜が話題のようですよ」程度の表現にする）
        """
        if not _reserve_guest_ai_usage(current_dataset):
            return _guest_ai_limit_response()

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
        )
        logger.info("AI daily greeting generated successfully.")
        return jsonify({"message": response.text})

    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to generate AI greeting.")
        return jsonify({"message": f"本日は{today.month}月{today.day}日です。今日も一日お疲れ様でした！"})


def _get_current_products(current_dataset, target_date=None):
    today = target_date if target_date is not None else business_today()

    return Product.query.filter_by(
        dataset_id=current_dataset.id,
        year=today.year,
        month=today.month,
        is_active=True
    ).all()

def _get_today_sales_map(target_date, current_dataset):
    """指定日の商品別売上個数を辞書で返す。"""
    sales = (
        DailySales.query
        .join(Product, DailySales.product_id == Product.id)
        .filter(
            DailySales.date == target_date,
            Product.dataset_id == current_dataset.id,
        )
        .all()
    )

    return {
        sale.product_id: sale.quantity
        for sale in sales
    }

def _get_today_sales_amount(current_dataset, target_date):
    """認可済みDatasetの当日売上を、登録単価×数量で集計する。"""
    amount = db.session.query(
        db.func.coalesce(
            db.func.sum(db.cast(Product.price, db.BigInteger) * DailySales.quantity),
            0,
        )
    ).select_from(Product).join(
        DailySales, Product.id == DailySales.product_id,
    ).filter(
        Product.dataset_id == current_dataset.id,
        DailySales.date == target_date,
    ).scalar()
    return int(amount)


def _get_sales_from_db(
    current_dataset,
    target_year=None,
    target_month=None,
    *,
    row_limit=None,
):
    query = db.session.query(
        Product.name,
        db.func.sum(DailySales.quantity)
    ).join(
        DailySales,
        Product.id == DailySales.product_id,
    ).filter(
        Product.dataset_id == current_dataset.id,
    )

    if target_year is not None:
        query = query.filter(db.extract("year", DailySales.date) == target_year)
    if target_month is not None:
        query = query.filter(db.extract("month", DailySales.date) == target_month)

    query = query.group_by(Product.name)
    if row_limit is not None:
        # Datasetと期間で絞った集計からのみ選ぶ。通常の画面集計は制限しない。
        query = query.order_by(
            db.func.sum(DailySales.quantity).desc(), Product.name.asc(),
        ).limit(row_limit)
    results = query.all()
    return {name: int(qty) for name, qty in results}


def _get_dashboard_sales_months(current_dataset, target_year=None):
    if target_year is None:
        return []

    query = (
        db.session.query(db.extract("month", DailySales.date))
        .join(Product, DailySales.product_id == Product.id)
        .filter(Product.dataset_id == current_dataset.id)
    )

    query = query.filter(
        db.extract("year", DailySales.date) == target_year
    )

    return sorted({int(row[0]) for row in query.distinct().all()})


def _get_dashboard_sales_years(
    current_dataset,
    current_year,
    selected_year=None,
):
    existing_years = (
        db.session.query(db.extract("year", DailySales.date))
        .join(Product, DailySales.product_id == Product.id)
        .filter(Product.dataset_id == current_dataset.id)
        .distinct()
        .all()
    )
    available_years = {current_year}
    available_years.update(int(row[0]) for row in existing_years)

    if selected_year is not None:
        available_years.add(selected_year)

    return sorted(available_years)




if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    # Renderが指定するポートを優先し、なければローカル用の5000を使う
    port = int(os.environ.get("PORT", 5000))
    
    logger.info(f"Starting Flask application with FLASK_DEBUG={debug_mode} on port {port}")
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
