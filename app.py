import os
from datetime import date, datetime
from collections import defaultdict

from flask import Flask, render_template, request, redirect, url_for, flash, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, logout_user, login_required,
    current_user, UserMixin
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text

# ---------- アプリ設定 ----------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")  # 本番は環境変数に
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///giftlog.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

#（任意：本番のCookieを少し堅く）
if os.getenv("FLASK_ENV") == "production":
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ---------- モデル ----------
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    gifts = db.relationship("Gift", backref="user", lazy=True)
    givers = db.relationship("Giver", backref="user", lazy=True)
    categories = db.relationship("Category", backref="user", lazy=True)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

class Giver(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    contact = db.Column(db.String(255))
    gifts = db.relationship("Gift", backref="giver", lazy=True)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    gifts = db.relationship("Gift", backref="category", lazy=True)

class Gift(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    title = db.Column(db.String(200), nullable=False)
    memo = db.Column(db.Text)

    giver_id = db.Column(db.Integer, db.ForeignKey("giver.id"))
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))

    received_date = db.Column(db.Date, default=date.today)

    thank_you_sent = db.Column(db.Boolean, default=False)  # お礼連絡
    return_due_date = db.Column(db.Date)                   # 返礼ToDo期日
    return_done = db.Column(db.Boolean, default=False)     # 返礼OK
    amount_yen = db.Column(db.Integer)                     # 任意の金額

@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))

# 既存DBに金額カラムが無い場合だけ追加（SQLite向け簡易マイグレーション）
def ensure_amount_column():
    insp = inspect(db.engine)
    cols = [c["name"] for c in insp.get_columns("gift")]
    if "amount_yen" not in cols:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE gift ADD COLUMN amount_yen INTEGER"))

# ---------- ヘルパ ----------
def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None

@app.template_filter("yen")
def yen(n):
    if n is None:
        return ""
    try:
        return f"¥{int(n):,}"
    except Exception:
        return ""

@app.template_filter("datejp")
def datejp(d):
    return d.strftime("%Y-%m-%d") if d else ""

def ensure_initial_master(user: "User"):
    """初回 or ログイン時、足りないマスタだけ追加する（重複なし）"""
    base_categories = ["食品", "化粧品", "日用品", "その他"]
    base_givers = ["父", "母"]

    existing_cat = {c.name for c in Category.query.filter_by(user_id=user.id).all()}
    existing_giver = {g.name for g in Giver.query.filter_by(user_id=user.id).all()}

    added = False
    for name in base_categories:
        if name not in existing_cat:
            db.session.add(Category(user_id=user.id, name=name))
            added = True
    for name in base_givers:
        if name not in existing_giver:
            db.session.add(Giver(user_id=user.id, name=name))
            added = True
    if added:
        db.session.commit()

def escape_ics(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
    )

# ---------- ルート ----------
@app.route("/")
@login_required
def home():
    count = Gift.query.filter_by(user_id=current_user.id).count()
    return render_template("index.html", title="GiftLog", count=count)

#  プレゼント追加ページ（/gifts/new）
@app.route("/gifts/new", methods=["GET", "POST"])
@login_required
def gift_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("タイトルは必須だよ！", "error")
            givers = Giver.query.filter_by(user_id=current_user.id).order_by(Giver.name).all()
            categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
            # 入力値を渡して再描画（必要なら request.form をテンプレ側で参照）
            return render_template("gift_new.html", title="ギフト追加",
                                   givers=givers, categories=categories), 400


        g = Gift(
            user_id=current_user.id,
            title=title,
            memo=request.form.get("memo", "").strip(),
            giver_id=request.form.get("giver_id", type=int),
            category_id=request.form.get("category_id", type=int),
            received_date=parse_date(request.form.get("received_date")) or date.today(),
            thank_you_sent=(request.form.get("thank_you_sent") == "on"),
            return_due_date=parse_date(request.form.get("return_due_date")),
            return_done=(request.form.get("return_done") == "on"),
            amount_yen=request.form.get("amount_yen", type=int)
        )
        db.session.add(g)
        db.session.commit()
        flash("ギフトを追加したよ！", "success")
        return redirect(url_for("gifts"))

    givers = Giver.query.filter_by(user_id=current_user.id).order_by(Giver.name).all()
    categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    return render_template("gift_new.html", title="ギフト追加", givers=givers, categories=categories)

# 一覧ページ（GET専用）
@app.route("/gifts", methods=["GET"])
@login_required
def gifts():
    # 検索パラメータ
    q = request.args.get("q", "").strip()
    selected_giver_id = request.args.get("giver_id", type=int)
    selected_category_id = request.args.get("category_id", type=int)
    only_todo = request.args.get("todo") == "1"
    min_amount = request.args.get("min_amount", type=int)
    max_amount = request.args.get("max_amount", type=int)
    amount_only = request.args.get("amount_only") == "1"

    qset = Gift.query.filter_by(user_id=current_user.id)
    if q:
        qset = qset.filter(Gift.title.contains(q))
    if selected_giver_id:
        qset = qset.filter_by(giver_id=selected_giver_id)
    if selected_category_id:
        qset = qset.filter_by(category_id=selected_category_id)
    if only_todo:
        qset = qset.filter((Gift.return_done == False) & (Gift.return_due_date != None))

    if amount_only or (min_amount is not None or max_amount is not None):
        qset = qset.filter(Gift.amount_yen != None)
    if min_amount is not None:
        qset = qset.filter(Gift.amount_yen >= min_amount)
    if max_amount is not None:
        qset = qset.filter(Gift.amount_yen <= max_amount)

    gifts = qset.order_by(Gift.received_date.desc()).all()
    givers = Giver.query.filter_by(user_id=current_user.id).order_by(Giver.name).all()
    categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()

    # 集計
    amounts = [g.amount_yen for g in gifts if g.amount_yen is not None]
    total_amount = sum(amounts) if amounts else 0
    avg_amount = round(sum(amounts) / len(amounts)) if amounts else None

    category_totals = defaultdict(int)
    for g in gifts:
        if g.amount_yen is not None and g.category:
            category_totals[g.category.name] += g.amount_yen

    return render_template(
        "gifts.html",
        title="Gifts",
        gifts=gifts,
        givers=givers,
        categories=categories,
        q=q,
        only_todo=only_todo,
        selected_giver_id=selected_giver_id,
        selected_category_id=selected_category_id,
        min_amount=min_amount,
        max_amount=max_amount,
        amount_only=amount_only,
        total_amount=total_amount,
        avg_amount=avg_amount,
        category_totals=category_totals,
    )

# 編集
@app.route("/gifts/<int:gift_id>/edit", methods=["GET", "POST"])
@login_required
def gift_edit(gift_id: int):
    gift = Gift.query.filter_by(id=gift_id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        gift.title = request.form.get("title", "").strip() or gift.title
        gift.memo = request.form.get("memo", "").strip()
        gift.giver_id = request.form.get("giver_id", type=int)
        gift.category_id = request.form.get("category_id", type=int)
        gift.received_date = parse_date(request.form.get("received_date")) or gift.received_date
        gift.thank_you_sent = (request.form.get("thank_you_sent") == "on")
        gift.return_due_date = parse_date(request.form.get("return_due_date"))
        gift.return_done = (request.form.get("return_done") == "on")
        gift.amount_yen = request.form.get("amount_yen", type=int)
        db.session.commit()
        flash("更新したよ！", "success")
        return redirect(url_for("gifts"))

    givers = Giver.query.filter_by(user_id=current_user.id).order_by(Giver.name).all()
    categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    return render_template("gift_form.html", title="編集", mode="edit", gift=gift, givers=givers, categories=categories)

# 贈り主 & カテゴリ 管理
@app.route("/givers", methods=["GET", "POST"])
@login_required
def givers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        contact = request.form.get("contact", "").strip()
        if name:
            db.session.add(Giver(user_id=current_user.id, name=name, contact=contact))
            db.session.commit()
            flash("贈り主を追加したよ！", "success")
        return redirect(url_for("givers"))
    items = Giver.query.filter_by(user_id=current_user.id).order_by(Giver.name).all()
    return render_template("givers.html", title="贈り主", givers=items)

@app.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            db.session.add(Category(user_id=current_user.id, name=name))
            db.session.commit()
            flash("カテゴリを追加したよ！", "success")
        return redirect(url_for("categories"))
    items = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    return render_template("categories.html", title="カテゴリ", categories=items)

# 認証
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("メールとパスワードを入力してね。", "error")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("このメールはすでに登録されています。", "error")
            return redirect(url_for("register"))
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        ensure_initial_master(user)
        flash("登録完了！ようこそ🎉", "success")
        return redirect(url_for("home"))
    return render_template("register.html", title="新規登録")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            ensure_initial_master(user)
            flash("ログインしました。", "success")
            return redirect(url_for("home"))
        flash("メールまたはパスワードが違います。", "error")
    return render_template("login.html", title="ログイン")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("ログアウトしました。", "success")
    return redirect(url_for("login"))

# カレンダー（返礼ToDo）
@app.route("/calendar.ics")
@login_required
def calendar_feed():
    gifts = (
        Gift.query.filter_by(user_id=current_user.id, return_done=False)
        .filter(Gift.return_due_date != None)
        .all()
    )

    def fmt(d: date) -> str:
        return d.strftime("%Y%m%d")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//GiftLog//JP",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for g in gifts:
        uid = f"giftlog-{g.id}@local"
        summary = f"返礼ToDo: {g.title}"
        desc = f"贈り主: {g.giver.name if g.giver else ''} / カテゴリ: {g.category.name if g.category else ''}"
        due = fmt(g.return_due_date)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"SUMMARY:{escape_ics(summary)}",
            f"DESCRIPTION:{escape_ics(desc)}",
            f"DTSTART;VALUE=DATE:{due}",
            f"DTEND;VALUE=DATE:{due}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")

    ics = "\r\n".join(lines)  # ICSはCRLF
    resp = make_response(ics)
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=giftlog.ics"
    return resp

# ---------- エラーハンドラ（簡易） ----------
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", title="Not Found"), 404

@app.errorhandler(500)
def server_error(e):
    # ログや通知を入れる場合はここに
    return render_template("500.html", title="Error"), 500

# ---------- 起動 ----------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        ensure_amount_column()

    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    # 一時的にルートを表示（デバッグ用。提出時は消してOK）
    print("=== ROUTES ===")
    for r in app.url_map.iter_rules():
        print(" ", r)
    app.run(debug=debug)
