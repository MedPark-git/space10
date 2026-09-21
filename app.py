import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import CheckConstraint, Index, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
db = SQLAlchemy(session_options={"expire_on_commit": False})
login_manager = LoginManager()
csrf = CSRFProtect()


def utcnow():
    return datetime.now(UTC)


def uuid_str():
    return str(uuid.uuid4())


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    updated_by = db.Column(db.String(36), nullable=True)


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.String(36), primary_key=True, default=uuid_str)
    login_id = db.Column(db.String(80), nullable=False, unique=True)
    name = db.Column(db.String(80), nullable=False)
    department = db.Column(db.String(100), nullable=True)
    position = db.Column(db.String(100), nullable=True)
    role = db.Column(db.String(20), nullable=False, default="viewer")
    password_hash = db.Column(db.String(255), nullable=False)
    is_active_account = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    __table_args__ = (CheckConstraint("role IN ('admin','editor','viewer')", name="ck_user_role"),)

    @property
    def is_active(self):
        return self.is_active_account

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)


class Item(TimestampMixin, db.Model):
    __tablename__ = "items"
    id = db.Column(db.String(36), primary_key=True, default=uuid_str)
    code = db.Column(db.String(80), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100), nullable=True)
    unit = db.Column(db.String(30), nullable=False, default="EA")
    location = db.Column(db.String(120), nullable=True)
    quantity = db.Column(db.Numeric(16, 3), nullable=False, default=0)
    safety_stock = db.Column(db.Numeric(16, 3), nullable=False, default=0)
    unit_cost = db.Column(db.Numeric(16, 2), nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_movement_at = db.Column(db.DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_items_name_category", "name", "category"),)


class StockMovement(TimestampMixin, db.Model):
    __tablename__ = "stock_movements"
    id = db.Column(db.String(36), primary_key=True, default=uuid_str)
    item_id = db.Column(db.String(36), db.ForeignKey("items.id"), nullable=False, index=True)
    movement_type = db.Column(db.String(10), nullable=False)
    quantity = db.Column(db.Numeric(16, 3), nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    reference = db.Column(db.String(120), nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    item = db.relationship("Item")
    creator = db.relationship("User")
    __table_args__ = (CheckConstraint("movement_type IN ('IN','OUT')", name="ck_movement_type"),)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.String(36), primary_key=True, default=uuid_str)
    event = db.Column(db.String(80), nullable=False, index=True)
    user_id = db.Column(db.String(36), nullable=True, index=True)
    login_id = db.Column(db.String(80), nullable=True)
    target_type = db.Column(db.String(80), nullable=True)
    target_id = db.Column(db.String(80), nullable=True)
    detail = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class HealthProbe(db.Model):
    __tablename__ = "health_probes"
    id = db.Column(db.String(36), primary_key=True)
    checked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


def database_uri():
    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        return None, missing
    from urllib.parse import quote_plus
    uri = "postgresql+psycopg://{}:{}@{}:{}/{}".format(
        quote_plus(os.environ["DB_USER"]), quote_plus(os.environ["DB_PASSWORD"]),
        os.environ["DB_HOST"], os.environ["DB_PORT"], quote_plus(os.environ["DB_NAME"])
    )
    return uri, []


def create_app(test_config=None):
    app = Flask(__name__)
    uri, missing = database_uri()
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=uri or "postgresql+psycopg://unconfigured:unconfigured@127.0.0.1:1/unconfigured",
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5, "pool_recycle": 300},
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=int(os.getenv("SESSION_IDLE_MINUTES", "60"))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "production") == "production",
        WTF_CSRF_TIME_LIMIT=3600,
        DB_CONFIG_MISSING=missing,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("TESTING") and not missing and not os.getenv("SECRET_KEY"):
        from runtime_security import session_secret
        app.config["SECRET_KEY"] = session_secret()
    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "로그인 후 이용해 주세요."

    @app.before_request
    def enforce_idle_session():
        session.permanent = True
        if current_user.is_authenticated:
            if current_user.must_change_password and request.endpoint not in {"force_password", "logout", "static"}:
                return redirect(url_for("force_password"))

    @app.teardown_request
    def rollback_on_error(error=None):
        if error is not None:
            db.session.rollback()
        db.session.remove()

    register_routes(app)
    register_errors(app)
    from expiry_feature import register_expiry
    register_expiry(app, db, audit, roles)

    from safe_inventory_dashboard import make_inventory_dashboard_view
    app.view_functions["expiry.dashboard"] = make_inventory_dashboard_view(app, db)

    app.jinja_env.filters["kst"] = lambda value: value.astimezone(KST).strftime("%Y-%m-%d %H:%M") if value else "-"
    app.jinja_env.filters["num"] = lambda value: f"{float(value or 0):,.0f}"
    app.jinja_env.filters["qty"] = lambda value: f"{float(value or 0):,.6f}".rstrip("0").rstrip(".")
    app.jinja_env.filters["won"] = lambda value: f"{float(value or 0):,.0f}"

    if not app.config.get("TESTING") and not missing:
        with app.app_context():
            initialize_database()
    return app


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)


@login_manager.unauthorized_handler
def require_authentication():
    if request.path.startswith("/api/"):
        return jsonify(error="authentication_required"), 401
    flash("로그인 후 이용해 주세요.", "message")
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


def initialize_database():
    try:
        with db.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(73190421)"))
            db.metadata.create_all(bind=conn)
        db.session.execute(text("SELECT pg_advisory_xact_lock(73190421)"))
        if db.session.scalar(select(func.count(User.id))) == 0:
            admin_id = os.getenv("BOOTSTRAP_ADMIN_ID")
            admin_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
            admin_name = os.getenv("BOOTSTRAP_ADMIN_NAME", "시스템관리자")
            if not admin_id or not admin_password:
                from runtime_security import bootstrap_credentials
                credentials = bootstrap_credentials()
                admin_id, admin_password = credentials['login_id'], credentials['password']
            if admin_id and admin_password:
                admin = User(login_id=admin_id.casefold(), name=admin_name, role="admin", must_change_password=True)
                admin.set_password(admin_password)
                db.session.add(admin)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()


def audit(event, user=None, target_type=None, target_id=None, detail=None, commit=True):
    actor = user or (current_user if current_user.is_authenticated else None)
    row = AuditLog(event=event, user_id=getattr(actor, "id", None), login_id=getattr(actor, "login_id", request.form.get("login_id")),
                   target_type=target_type, target_id=target_id, detail=detail, ip_address=request.headers.get("X-Forwarded-For", request.remote_addr))
    db.session.add(row)
    if commit:
        db.session.commit()


def roles(*allowed):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def form_value(name, default=""):
    return request.form.get(name, default).strip()


def register_routes(app):
    @app.get("/health")
    def health():
        result = {"status": "error", "database": False, "database_backend": "postgresql", "database_writable": False, "application_ready": False}
        if app.config.get("DB_CONFIG_MISSING"):
            return jsonify(result), 503
        try:
            db.session.execute(text("SELECT 1"))
            nested = db.session.begin_nested()
            db.session.add(HealthProbe(id=uuid_str()))
            db.session.flush()
            nested.rollback()
            db.session.rollback()
            result.update(status="ok", database=True, database_writable=True, application_ready=True)
            return jsonify(result)
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify(result), 503

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            login_id = form_value("login_id").casefold()
            user = db.session.scalar(select(User).where(func.lower(User.login_id) == login_id))
            now = utcnow()
            if user and user.locked_until and user.locked_until > now:
                audit("login_failed", user=user, detail="temporarily_locked")
                flash("로그인 실패가 반복되어 잠시 잠겼습니다. 15분 후 다시 시도해 주세요.", "error")
            elif user and user.is_active_account and check_password_hash(user.password_hash, request.form.get("password", "")):
                user.failed_login_count = 0; user.locked_until = None; user.last_login_at = now
                audit("login_success", user=user, commit=False); db.session.commit()
                login_user(user); session.permanent = True
                return redirect(url_for("force_password" if user.must_change_password else "dashboard"))
            else:
                if user:
                    user.failed_login_count += 1
                    if user.failed_login_count >= 5:
                        user.locked_until = now + timedelta(minutes=15); user.failed_login_count = 0
                audit("login_failed", user=user, detail="invalid_credentials", commit=False); db.session.commit()
                flash("아이디 또는 비밀번호를 확인해 주세요.", "error")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        audit("logout")
        logout_user(); session.clear()
        response = redirect(url_for("login")); response.delete_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
        return response

    @app.route("/password/first", methods=["GET", "POST"])
    @login_required
    def force_password():
        if request.method == "POST":
            password = request.form.get("password", "")
            if password != request.form.get("password_confirm", ""):
                flash("비밀번호 확인이 일치하지 않습니다.", "error")
            elif len(password) < 10 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
                flash("비밀번호는 영문과 숫자를 포함해 10자 이상으로 입력해 주세요.", "error")
            elif check_password_hash(current_user.password_hash, password):
                flash("기존 비밀번호와 다른 비밀번호를 사용해 주세요.", "error")
            else:
                current_user.set_password(password); current_user.must_change_password = False
                audit("password_changed", target_type="user", target_id=current_user.id, commit=False); db.session.commit()
                from runtime_security import remove_bootstrap_credentials
                remove_bootstrap_credentials(current_user.login_id)
                flash("비밀번호가 변경되었습니다.", "success"); return redirect(url_for("expiry.dashboard"))
        return render_template("password.html", first=current_user.must_change_password)

    @app.get("/")
    @login_required
    def dashboard():
        return redirect(url_for("expiry.dashboard"))

    @app.get("/items")
    @login_required
    def items():
        return redirect(url_for("expiry.master_data"))

    @app.get("/api/items")
    @login_required
    def api_items():
        rows = db.session.scalars(select(Item).where(Item.is_active.is_(True)).order_by(Item.name)).all()
        return jsonify(items=[{"id": r.id, "code": r.code, "name": r.name, "quantity": float(r.quantity), "unit": r.unit} for r in rows])

    @app.route("/items/new", methods=["GET", "POST"])
    @roles("admin", "editor")
    def item_new():
        if request.method == "POST":
            code = form_value("code").upper()
            if db.session.scalar(select(Item).where(func.lower(Item.code) == code.casefold())):
                flash("이미 등록된 품목코드입니다.", "error")
            else:
                row = Item(code=code, name=form_value("name"), category=form_value("category") or None, unit=form_value("unit", "EA"),
                           location=form_value("location") or None, quantity=0, safety_stock=float(form_value("safety_stock", "0") or 0),
                           unit_cost=float(form_value("unit_cost", "0") or 0), updated_by=current_user.id)
                db.session.add(row); db.session.flush(); audit("item_created", target_type="item", target_id=row.id, detail=row.code, commit=False); db.session.commit()
                flash("품목이 등록되었습니다.", "success"); return redirect(url_for("items"))
        return render_template("item_form.html", item=None)

    @app.route("/items/<item_id>/edit", methods=["GET", "POST"])
    @roles("admin", "editor")
    def item_edit(item_id):
        row = db.get_or_404(Item, item_id)
        if request.method == "POST":
            row.name=form_value("name"); row.category=form_value("category") or None; row.unit=form_value("unit", "EA"); row.location=form_value("location") or None
            row.safety_stock=float(form_value("safety_stock", "0") or 0); row.unit_cost=float(form_value("unit_cost", "0") or 0); row.updated_by=current_user.id
            audit("item_updated", target_type="item", target_id=row.id, detail=row.code, commit=False); db.session.commit()
            flash("품목이 수정되었습니다.", "success"); return redirect(url_for("items"))
        return render_template("item_form.html", item=row)

    @app.post("/items/<item_id>/delete")
    @roles("admin", "editor")
    def item_delete(item_id):
        row = db.get_or_404(Item, item_id)
        has_movement = db.session.scalar(select(func.count(StockMovement.id)).where(StockMovement.item_id == row.id))
        if float(row.quantity) != 0 or has_movement:
            flash("재고 또는 입출고 이력이 있는 품목은 삭제할 수 없습니다.", "error")
        else:
            row.is_active = False; row.updated_by = current_user.id
            audit("item_deleted", target_type="item", target_id=row.id, detail=row.code, commit=False); db.session.commit()
            flash("품목이 삭제되었습니다.", "success")
        return redirect(url_for("items"))

    @app.route("/movements", methods=["GET", "POST"])
    @login_required
    def movements():
        if request.method == "POST":
            abort(404)
        return redirect(url_for("expiry.stock_history"))

    @app.get("/analysis")
    @login_required
    def analysis():
        return redirect(url_for("expiry.analysis"))

    @app.get("/profile")
    @login_required
    def profile(): return render_template("profile.html")

    @app.route("/users", methods=["GET", "POST"])
    @roles("admin")
    def users():
        if request.method == "POST":
            login_id=form_value("login_id").casefold()
            if db.session.scalar(select(User).where(func.lower(User.login_id)==login_id)):
                flash("이미 사용 중인 로그인 ID입니다.", "error")
            else:
                temporary=secrets.token_urlsafe(12)
                row=User(login_id=login_id,name=form_value("name"),department=form_value("department") or None,position=form_value("position") or None,role=request.form.get("role","viewer"),must_change_password=True)
                row.set_password(temporary); db.session.add(row); db.session.flush(); audit("user_created",target_type="user",target_id=row.id,detail=row.login_id,commit=False); db.session.commit()
                flash(f"사용자가 생성되었습니다. 이번 화면에서만 확인 가능한 임시 비밀번호: {temporary}","success")
                return redirect(url_for("users"))
        rows=db.session.scalars(select(User).order_by(User.name)).all(); return render_template("users.html", users=rows)

    @app.post("/users/<user_id>/toggle")
    @roles("admin")
    def user_toggle(user_id):
        row=db.get_or_404(User,user_id)
        if row.id==current_user.id: flash("현재 로그인한 계정은 비활성화할 수 없습니다.","error")
        else:
            row.is_active_account=not row.is_active_account; row.updated_by=current_user.id
            audit("user_updated",target_type="user",target_id=row.id,detail="activated" if row.is_active_account else "deactivated",commit=False); db.session.commit(); flash("계정 상태가 변경되었습니다.","success")
        return redirect(url_for("users"))

    @app.post("/users/<user_id>/role")
    @roles("admin")
    def user_role(user_id):
        row=db.get_or_404(User,user_id); new_role=request.form.get("role")
        if new_role not in {"admin","editor","viewer"}: abort(400)
        if row.id==current_user.id and new_role!="admin":
            flash("현재 로그인한 관리자 권한은 변경할 수 없습니다.","error")
        else:
            row.role=new_role; row.updated_by=current_user.id
            audit("user_updated",target_type="user",target_id=row.id,detail=f"role:{new_role}",commit=False); db.session.commit(); flash("사용자 권한이 변경되었습니다.","success")
        return redirect(url_for("users"))

    @app.post("/users/<user_id>/reset-password")
    @roles("admin")
    def user_reset_password(user_id):
        row=db.get_or_404(User,user_id); temporary=secrets.token_urlsafe(12); row.set_password(temporary); row.must_change_password=True; row.updated_by=current_user.id
        audit("temporary_password_issued",target_type="user",target_id=row.id,commit=False); db.session.commit()
        flash(f"이번 화면에서만 확인 가능한 임시 비밀번호: {temporary}","success"); return redirect(url_for("users"))

    @app.get("/audit")
    @roles("admin")
    def audit_logs():
        event=request.args.get("event","").strip(); stmt=select(AuditLog).order_by(AuditLog.created_at.desc()).limit(500)
        if event: stmt=stmt.where(AuditLog.event.ilike(f"%{event}%"))
        return render_template("audit.html", logs=db.session.scalars(stmt).all(), event=event)


def register_errors(app):
    @app.errorhandler(401)
    def unauthorized(_):
        if request.path.startswith("/api/"): return jsonify(error="authentication_required"),401
        return redirect(url_for("login"))
    @app.errorhandler(403)
    def forbidden(_): return render_template("error.html", code=403, title="접근 권한이 없습니다", message="이 화면을 사용할 권한이 없습니다."),403
    @app.errorhandler(404)
    def not_found(_): return render_template("error.html", code=404, title="페이지를 찾을 수 없습니다", message="주소를 확인하거나 메뉴에서 다시 이동해 주세요."),404
    @app.errorhandler(500)
    def server_error(_):
        db.session.rollback(); return render_template("error.html", code=500, title="시스템 오류", message="요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."),500


app = create_app()

# Install the recovered authenticated routes and the specification-first inventory
# dashboard directly on the runtime app. This keeps the live view consistent
# whether AI Space launches app:app or the Procfile entrypoint.
from expiry_route_restore import restore_routes
from inventory_spec_view import install_inventory_spec_view
restore_routes(app, db, audit, roles)
install_inventory_spec_view(app, db)

@app.get("/health/inventory-specs-live-20260921-03")
def inventory_specs_live_readiness():
    from flask import g
    from types import SimpleNamespace
    try:
        with app.test_request_context("/expiry/dashboard", method="GET", base_url="https://localhost"):
            g._login_user = SimpleNamespace(
                is_authenticated=True, is_active=True, is_anonymous=False,
                role="admin", name="Internal render check",
                id="inventory-spec-render-check", must_change_password=False,
            )
            db.session.execute(text("SET TRANSACTION READ ONLY"))
            response = app.make_response(app.view_functions["expiry.dashboard"]())
            body = response.get_data(as_text=True)
            required = [
                'data-release="inventory-specs-20260921-03"',
                "제품별 재고 관리", "iv-category-card", "iv-category-label",
                "규격(사이즈)", "완제품", "공정중", "기타가용",
            ]
            if response.status_code != 200 or any(marker not in body for marker in required):
                raise RuntimeError("inventory specification dashboard render check failed")
            db.session.rollback()
        return jsonify(status="ok", release="inventory-specs-20260921-03", rendered=True)
    except Exception as error:
        db.session.rollback()
        app.logger.exception("INVENTORY_SPEC_RENDER_CHECK_FAILED")
        return jsonify(status="error", release="inventory-specs-20260921-03",
                       error_type=type(error).__name__), 503

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
