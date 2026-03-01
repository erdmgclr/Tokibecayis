import json
import re
import sqlite3
import os
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, abort, g, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash


BASE_DIR = Path(__file__).resolve().parent
APP_DB = str(BASE_DIR / "tokibecayis.db")
ADRES_JSON_PATH = BASE_DIR / "static" / "adres.json"

INACTIVITY_DAYS = 7  # 7 gün giriş yapmayan kullanıcı ilanları pasife alınır

# performans için global temizlik periyodu (saniye)
STALE_CLEANUP_INTERVAL_SEC = 300
_LAST_STALE_CLEANUP_TS = 0.0

app = Flask(__name__)
app.secret_key = "CHANGE_ME_TO_A_RANDOM_SECRET"  # üretimde değiştir


# -----------------------------
# DB helpers
# -----------------------------
def is_postgres() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _convert_qmarks_to_psycopg(sql: str) -> str:
    """Convert SQLite-style ? placeholders to psycopg2 %s placeholders.
    Avoids touching question marks inside quoted strings.
    """
    out = []
    in_single = False
    in_double = False
    esc = False
    for ch in sql:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":  # escape next char
            out.append(ch)
            esc = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            continue
        if ch == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class DB:
    """Small compatibility wrapper so the codebase can stay mostly unchanged.

    - SQLite: uses sqlite3 connection/cursor directly
    - Postgres: uses psycopg2 connection/cursor, exposing .execute() that returns a cursor
    """
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql: str, params=()):
        if is_postgres():
            sql = _convert_qmarks_to_psycopg(sql)
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq_of_params):
        if is_postgres():
            sql = _convert_qmarks_to_psycopg(sql)
            cur = self.conn.cursor()
            cur.executemany(sql, seq_of_params)
            return cur
        return self.conn.executemany(sql, seq_of_params)

    def cursor(self):
        return self.conn.cursor()

    def commit(self):
        return self.conn.commit()

    def close(self):
        return self.conn.close()

def get_db():
    if not is_postgres():
        conn = sqlite3.connect(APP_DB)
        conn.row_factory = sqlite3.Row
        return DB(conn)

    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
        sslmode="require",
    )
    return DB(conn)


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def table_cols(cur, table_name: str) -> set[str]:
    if is_postgres():
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
            (table_name,),
        )
        rows = cur.fetchall()
        cols = set()
        for r in rows:
            if isinstance(r, dict):
                cols.add(r.get("column_name"))
            else:
                cols.add(r[0])
        return {c for c in cols if c}
    rows = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r[1] for r in rows}


def ensure_schema():
    conn = get_db()
    cur = conn.cursor()

    if is_postgres():
        # --- users ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login_at TEXT,
            failed_login_attempts INTEGER NOT NULL DEFAULT 0,
            seen_intro INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL DEFAULT 'user',
            lock_until TEXT
        )
        """)
        # --- listings ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),

            mevcut_il TEXT NOT NULL,
            mevcut_ilce TEXT NOT NULL,
            mevcut_mahalle TEXT NOT NULL,
            mevcut_bolge TEXT,
            mevcut_etap TEXT,
            mevcut_kat TEXT NOT NULL,
            mevcut_oda TEXT NOT NULL,
            mevcut_not TEXT,

            hedef_il TEXT NOT NULL DEFAULT 'any',
            hedef_ilce_json TEXT NOT NULL DEFAULT '["any"]',
            hedef_mahalle_json TEXT NOT NULL DEFAULT '["any"]',
            hedef_bolge TEXT,
            hedef_etap TEXT,
            hedef_kat_json TEXT NOT NULL DEFAULT '["any"]',
            hedef_oda TEXT NOT NULL DEFAULT 'any',
            ucret TEXT NOT NULL DEFAULT 'any',
            hedef_not TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """)
        # --- threads ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            id SERIAL PRIMARY KEY,
            user1_id INTEGER NOT NULL REFERENCES users(id),
            user2_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            last_message_at TEXT,
            UNIQUE(user1_id, user2_id)
        )
        """)
        # --- messages ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            thread_id INTEGER NOT NULL REFERENCES threads(id),
            sender_id INTEGER NOT NULL REFERENCES users(id),
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        # --- blocks ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS blocks (
            id SERIAL PRIMARY KEY,
            blocker_id INTEGER NOT NULL REFERENCES users(id),
            blocked_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            UNIQUE(blocker_id, blocked_id)
        )
        """)
        # --- reports ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            reporter_user_id INTEGER NOT NULL REFERENCES users(id),
            target_user_id INTEGER,
            listing_id INTEGER,
            thread_id INTEGER,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        conn.commit()
        conn.close()
        return

    # ---------------- users ----------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_login_at TEXT,
        failed_login_attempts INTEGER NOT NULL DEFAULT 0,
        seen_intro INTEGER NOT NULL DEFAULT 0
    )
    """)
    ucols = table_cols(cur, "users")
    if "role" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

    if "lock_until" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN lock_until TEXT")

    # ---------------- listings ----------------
    # Şema: form template'leri mevcut/hedef alanlarını kullanıyor.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,

        mevcut_il TEXT NOT NULL,
        mevcut_ilce TEXT NOT NULL,
        mevcut_mahalle TEXT NOT NULL,
        mevcut_bolge TEXT,
        mevcut_etap TEXT,
        mevcut_kat TEXT NOT NULL,
        mevcut_oda TEXT NOT NULL,
        mevcut_not TEXT,

        hedef_il TEXT NOT NULL DEFAULT 'any',
        hedef_ilce_json TEXT NOT NULL DEFAULT '["any"]',
        hedef_mahalle_json TEXT NOT NULL DEFAULT '["any"]',
        hedef_bolge TEXT,
        hedef_etap TEXT,
        hedef_kat_json TEXT NOT NULL DEFAULT '["any"]',
        hedef_oda TEXT NOT NULL DEFAULT 'any',
        ucret TEXT NOT NULL DEFAULT 'any',
        hedef_not TEXT,

        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,

        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # Eski sürümlerden kalma kolonlar olabilir; dokunmuyoruz.

    # ---------------- threads/messages/blocks/reports ----------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1_id INTEGER NOT NULL,
        user2_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_message_at TEXT,
        UNIQUE(user1_id, user2_id),
        FOREIGN KEY(user1_id) REFERENCES users(id),
        FOREIGN KEY(user2_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(thread_id) REFERENCES threads(id),
        FOREIGN KEY(sender_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        blocker_id INTEGER NOT NULL,
        blocked_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(blocker_id, blocked_id),
        FOREIGN KEY(blocker_id) REFERENCES users(id),
        FOREIGN KEY(blocked_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_user_id INTEGER NOT NULL,
        target_user_id INTEGER,
        listing_id INTEGER,
        thread_id INTEGER,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        FOREIGN KEY(reporter_user_id) REFERENCES users(id)
    )
    """)

    conn.commit()
    conn.close()


with app.app_context():
    ensure_schema()


# -----------------------------
# Decorators / g user
# -----------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        conn = get_db()
        u = conn.execute("SELECT id, role FROM users WHERE id=?", (session["user_id"],)).fetchone()
        conn.close()
        if not u:
            session.clear()
            return redirect(url_for("login"))
        if (u["role"] or "user") != "admin":
            return abort(403)
        g.is_admin = True
        return f(*args, **kwargs)
    return wrapper





def active_listings_count_value() -> int:
    """Sistemde görünen (etkin) aktif ilan sayısı.
    Kural: listing.is_active=1 VE (sahibi admin OR son login <= INACTIVITY_DAYS gün).
    """
    cutoff = (datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)).isoformat(timespec="seconds")
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM listings l
            JOIN users u ON u.id = l.user_id
            WHERE COALESCE(l.is_active,1)=1
              AND (
                    COALESCE(u.role,'user')='admin'
                 OR (COALESCE(u.last_login_at,'')<>'' AND u.last_login_at >= ?)
              )
            """,
            (cutoff,),
        ).fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


@app.context_processor
def inject_global_counts():
    try:
        return {"active_listings_count": active_listings_count_value()}
    except Exception:
        return {"active_listings_count": 0}


@app.context_processor
def inject_unread_flag():
    """Navbar için minimal 'okunmamış mesaj' rozeti.

    DB şemasını değiştirmeden şu kuralı kullanır:
    Kullanıcının dahil olduğu herhangi bir konuşmada en son mesajı karşı taraf gönderdiyse
    has_unread=True döner.
    """
    if not session.get("user_id"):
        return {"has_unread": False}

    me = session["user_id"]
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT t.id
            FROM threads t
            JOIN messages m ON m.thread_id = t.id
            WHERE (t.user1_id = ? OR t.user2_id = ?)
              AND m.id = (
                SELECT id
                FROM messages
                WHERE thread_id = t.id
                ORDER BY created_at DESC
                LIMIT 1
              )
              AND m.sender_id != ?
            LIMIT 1
            """,
            (me, me, me),
        ).fetchone()
        return {"has_unread": bool(row)}
    except Exception:
        return {"has_unread": False}
    finally:
        conn.close()

@app.before_request
def load_current_user():
    # periyodik "stale ilan" temizliği (admin hariç)
    global _LAST_STALE_CLEANUP_TS
    try:
        now_ts = datetime.utcnow().timestamp()
        if (now_ts - float(_LAST_STALE_CLEANUP_TS)) >= STALE_CLEANUP_INTERVAL_SEC:
            conn = get_db()
            deactivate_stale_listings_global(conn)
            conn.commit()
            conn.close()
            _LAST_STALE_CLEANUP_TS = now_ts
    except Exception:
        # temizlik hata verirse uygulamayı bozma
        pass

    g.user = None
    g.is_admin = False
    uid = session.get("user_id")
    if not uid:
        return
    conn = get_db()
    u = conn.execute("SELECT id, username, role, seen_intro FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if u:
        g.user = u
        g.is_admin = ((u["role"] or "user") == "admin")


# -----------------------------
# -----------------------------
# Helpers
# -----------------------------
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,30}$")


def username_is_valid(username: str) -> tuple[bool, str]:
    if not username or len(username) < 3:
        return False, "en az 3 karakter"
    if len(username) > 30:
        return False, "en fazla 30 karakter"
    if not _USERNAME_RE.match(username):
        return False, "sadece harf/rakam/._- kullan"
    return True, "ok"


def parse_multiselect(values):
    # form getlist boş gelirse any yap
    vals = [v for v in (values or []) if (v or "").strip() != ""]
    if not vals:
        return ["any"]
    # any varsa tek başına kalsın
    if "any" in vals:
        return ["any"]
    # uniq
    out = []
    s = set()
    for v in vals:
        if v not in s:
            out.append(v)
            s.add(v)
    return out


def listing_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # json kolonlarını list olarak ekle (JS edit doldurma için)
    try:
        d["hedef_ilce"] = json.loads(d.get("hedef_ilce_json") or '["any"]')
    except Exception:
        d["hedef_ilce"] = ["any"]
    try:
        d["hedef_mahalle"] = json.loads(d.get("hedef_mahalle_json") or '["any"]')
    except Exception:
        d["hedef_mahalle"] = ["any"]
    try:
        d["hedef_kat"] = json.loads(d.get("hedef_kat_json") or '["any"]')
    except Exception:
        d["hedef_kat"] = ["any"]
    return d


def normalize_pair(a, b):
    return (a, b) if a < b else (b, a)


def is_blocked(conn, viewer_id, other_id):
    r = conn.execute("""
        SELECT 1 FROM blocks
        WHERE (blocker_id=? AND blocked_id=?)
           OR (blocker_id=? AND blocked_id=?)
        LIMIT 1
    """, (viewer_id, other_id, other_id, viewer_id)).fetchone()
    return bool(r)


def hard_delete_user(conn, user_id: int):
    tids = [r["id"] for r in conn.execute(
        "SELECT id FROM threads WHERE user1_id=? OR user2_id=?",
        (user_id, user_id)
    ).fetchall()]

    if tids:
        conn.executemany("DELETE FROM messages WHERE thread_id=?", [(tid,) for tid in tids])

    conn.execute("DELETE FROM threads WHERE user1_id=? OR user2_id=?", (user_id, user_id))
    conn.execute("DELETE FROM listings WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM blocks WHERE blocker_id=? OR blocked_id=?", (user_id, user_id))
    conn.execute("DELETE FROM reports WHERE reporter_user_id=? OR target_user_id=?", (user_id, user_id))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def auto_deactivate_stale_listings(conn, user_id):
    u = conn.execute("SELECT last_login_at FROM users WHERE id=?", (user_id,)).fetchone()
    if not u or not u["last_login_at"]:
        return
    try:
        last = datetime.fromisoformat(u["last_login_at"])
    except Exception:
        return
    if datetime.utcnow() - last >= timedelta(days=INACTIVITY_DAYS):
        conn.execute("UPDATE listings SET is_active=0, updated_at=? WHERE user_id=?", (now_iso(), user_id))


def deactivate_stale_listings_global(conn):
    """7+ gündür giriş yapmayan (admin olmayan) kullanıcıların ilanlarını DB'de pasife çeker.
    Not: Arka plan job olmadan 'tam 8. gün' garanti edilemez; bu fonksiyon bir istek geldiğinde tetiklenir.
    """
    cutoff = (datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE listings
        SET is_active=0, updated_at=?
        WHERE is_active=1
          AND user_id IN (
              SELECT id FROM users
              WHERE COALESCE(role,'user') <> 'admin'
                AND COALESCE(last_login_at,'') <> ''
                AND last_login_at < ?
          )
        """,
        (now_iso(), cutoff),
    )



def matches_one_way(a: dict, b: dict) -> bool:
    # b.mevcut must satisfy a.hedef
    def any_or_contains(arr, value):
        return ("any" in arr) or (value in arr)

    if a.get("ucret") not in (None, "", "any"):
        # a ücret filtresi varsa b'nin ucret'ini dikkate almayız; bu alan talep tarafı
        pass

    if a.get("hedef_il") not in (None, "", "any") and b.get("mevcut_il") != a.get("hedef_il"):
        return False

    hedef_ilce = a.get("hedef_ilce") or ["any"]
    hedef_mahalle = a.get("hedef_mahalle") or ["any"]
    hedef_kat = a.get("hedef_kat") or ["any"]

    if not any_or_contains(hedef_ilce, b.get("mevcut_ilce")):
        return False
    if not any_or_contains(hedef_mahalle, b.get("mevcut_mahalle")):
        return False
    if not any_or_contains(hedef_kat, str(b.get("mevcut_kat"))):
        return False

    if a.get("hedef_oda") not in (None, "", "any") and b.get("mevcut_oda") != a.get("hedef_oda"):
        return False

    # bolge/etap: boş = farketmez; doluysa eşit olmalı
    ab = (a.get("hedef_bolge") or "").strip()
    ae = (a.get("hedef_etap") or "").strip()
    if ab and (str(b.get("mevcut_bolge") or "").strip() != ab):
        return False
    if ae and (str(b.get("mevcut_etap") or "").strip() != ae):
        return False

    # ücret: boş/any = farketmez, aksi eşit olmalı (b'nin ucret alanı yok; listing ucret talep alanı)
    # Burada ücret filtresi "karşı taraftan ücret ister mi?" şeklinde okunuyor.
    # Yani a.ucret != any ise b.ucret ile eşleşmeli.
    au = (a.get("ucret") or "any")
    bu = (b.get("ucret") or "any")
    if au != "any" and bu != "any" and bu != au:
        return False

    return True


def is_mutual_match(a: dict, b: dict) -> bool:
    return matches_one_way(a, b) and matches_one_way(b, a)


# -----------------------------
# API
# -----------------------------
@app.route("/api/adres")
def api_adres():
    try:
        data = json.loads(ADRES_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = []
    return jsonify(data)


@app.route("/api/username_check")
def api_username_check():
    username = (request.args.get("username") or "").strip()
    ok, reason = username_is_valid(username)
    if not ok:
        return jsonify({"ok": False, "reason": reason})
    conn = get_db()
    exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if exists:
        return jsonify({"ok": False, "reason": "kullanımda"})
    return jsonify({"ok": True, "reason": "ok"})


# -----------------------------
# Public
# -----------------------------
@app.route("/")
def home():
    if session.get("user_id"):
        if g.user and int(g.user["seen_intro"] or 0) == 0:
            return redirect(url_for("intro"))
        return redirect(url_for("dashboard"))
    return render_template("intro.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# -----------------------------
# Auth
# -----------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        ok, reason = username_is_valid(username)
        if not ok:
            flash(f"Kullanıcı adı uygun değil: {reason}", "error")
            return render_template("register.html")
        if len(password) < 5:
            flash("Şifre en az 5 karakter olmalı.", "error")
            return render_template("register.html")

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at, last_login_at, failed_login_attempts, seen_intro, role) "
                "VALUES (?, ?, ?, ?, 0, 0, 'user')",
                (username, generate_password_hash(password), now_iso(), now_iso())
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash("Bu kullanıcı adı zaten alınmış.", "error")
            return render_template("register.html")

        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        session["user_id"] = user["id"]
        flash("Kayıt başarılı.", "success")
        return redirect(url_for("intro"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    # Zaten giriş yaptıysa
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        conn = get_db()
        user = conn.execute(
            "SELECT id, username, password_hash, failed_login_attempts, role, lock_until "
            "FROM users WHERE username=?",
            (username,)
        ).fetchone()

        # Kullanıcı yoksa
        if not user:
            conn.close()
            flash("Kullanıcı adı veya şifre hatalı.", "error")
            return render_template("login.html")

        role = (user["role"] or "user")

        # Kilit kontrolü
        if user["lock_until"]:
            try:
                until_dt = datetime.fromisoformat(user["lock_until"])
            except Exception:
                until_dt = None

            if until_dt and datetime.utcnow() < until_dt:
                kalan = until_dt - datetime.utcnow()
                dk = int(kalan.total_seconds() // 60) + 1
                if dk >= 60 * 24:
                    gun = (dk // (60 * 24)) + 1
                    msg = f"Hesap kilitli. {gun} gün sonra tekrar deneyin."
                elif dk >= 60:
                    saat = (dk // 60) + 1
                    msg = f"Hesap kilitli. {saat} saat sonra tekrar deneyin."
                else:
                    msg = f"Hesap kilitli. {dk} dk sonra tekrar deneyin."
                conn.close()
                flash(msg, "error")
                return render_template("login.html")

        # Şifre yanlışsa: hesap SİLME YOK, kademeli kilit var
        if not check_password_hash(user["password_hash"], password):
            attempts = int(user["failed_login_attempts"] or 0) + 1

            lock_until = None
            if attempts == 1:
                msg = "Kullanıcı adı veya şifre hatalı."
            elif attempts == 2:
                lock_until = (datetime.utcnow() + timedelta(minutes=5)).isoformat(timespec="seconds")
                msg = "2. hatalı giriş: 5 dakika kilit."
            elif attempts == 3:
                lock_until = (datetime.utcnow() + timedelta(minutes=10)).isoformat(timespec="seconds")
                msg = "3. hatalı giriş: 10 dakika kilit."
            else:
                # 4. yanlış: 1 gün, sonraki her yanlışta +1 gün (doğru şifre girene kadar)
                base = datetime.utcnow()
                if user["lock_until"]:
                    try:
                        prev = datetime.fromisoformat(user["lock_until"])
                        if prev and prev > base:
                            base = prev
                    except Exception:
                        pass
                lock_until = (base + timedelta(days=1)).isoformat(timespec="seconds")
                msg = "Hatalı giriş: 1 gün kilit (doğru şifre girene kadar her yanlışta +1 gün)."

            conn.execute(
                "UPDATE users SET failed_login_attempts=?, lock_until=? WHERE id=?",
                (attempts, lock_until, user["id"])
            )
            conn.commit()
            conn.close()

            flash(msg, "error")
            return render_template("login.html")

        # Şifre doğru: sayaç/kilit sıfırla
        conn.execute(
            "UPDATE users SET last_login_at=?, failed_login_attempts=0, lock_until=NULL WHERE id=?",
            (now_iso(), user["id"])
        )

        # 7 gün pasif kuralı (admin hariç)
        if role != "admin":
            auto_deactivate_stale_listings(conn, user["id"])

        conn.commit()
        conn.close()

        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/intro")
@login_required
def intro():
    return render_template("intro.html")


@app.route("/intro/accept", methods=["POST"])
@login_required
def intro_accept():
    conn = get_db()
    conn.execute("UPDATE users SET seen_intro=1 WHERE id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


# -----------------------------
# Dashboard
# -----------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    auto_deactivate_stale_listings(conn, session["user_id"])
    conn.commit()

    my_listings = conn.execute(
        "SELECT * FROM listings WHERE user_id=? ORDER BY id DESC",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return render_template("dashboard.html", my_listings=my_listings)


# -----------------------------
# Listings CRUD
# -----------------------------
@app.route("/listing/new", methods=["GET", "POST"])
@login_required
def listing_new():
    if request.method == "POST":
        f = request.form

        mevcut_il = (f.get("mevcut_il") or "").strip()
        mevcut_ilce = (f.get("mevcut_ilce") or "").strip()
        mevcut_mahalle = (f.get("mevcut_mahalle") or "").strip()
        mevcut_kat = (f.get("mevcut_kat") or "").strip()
        mevcut_oda = (f.get("mevcut_oda") or "").strip()

        if not all([mevcut_il, mevcut_ilce, mevcut_mahalle, mevcut_kat, mevcut_oda]):
            flash("Mevcut daire alanları eksik.", "error")
            return render_template("listing_form.html", mode="new", listing=None, inactivity_days=INACTIVITY_DAYS)

        hedef_il = (f.get("hedef_il") or "any").strip() or "any"
        hedef_ilce = parse_multiselect(request.form.getlist("hedef_ilce"))
        hedef_mahalle = parse_multiselect(request.form.getlist("hedef_mahalle"))
        hedef_kat = parse_multiselect(request.form.getlist("hedef_kat"))
        hedef_oda = (f.get("hedef_oda") or "any").strip() or "any"
        ucret = (f.get("ucret") or "any").strip() or "any"

        conn = get_db()
        conn.execute("""
            INSERT INTO listings (
              user_id,
              mevcut_il, mevcut_ilce, mevcut_mahalle, mevcut_bolge, mevcut_etap, mevcut_kat, mevcut_oda, mevcut_not,
              hedef_il, hedef_ilce_json, hedef_mahalle_json, hedef_bolge, hedef_etap, hedef_kat_json, hedef_oda, ucret, hedef_not,
              created_at, updated_at, is_active
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session["user_id"],
            mevcut_il, mevcut_ilce, mevcut_mahalle,
            (f.get("mevcut_bolge") or "").strip() or None,
            (f.get("mevcut_etap") or "").strip() or None,
            mevcut_kat, mevcut_oda,
            (f.get("mevcut_not") or "").strip() or None,

            hedef_il,
            json.dumps(hedef_ilce, ensure_ascii=False),
            json.dumps(hedef_mahalle, ensure_ascii=False),
            (f.get("hedef_bolge") or "").strip() or None,
            (f.get("hedef_etap") or "").strip() or None,
            json.dumps(hedef_kat, ensure_ascii=False),
            hedef_oda,
            ucret,
            (f.get("hedef_not") or "").strip() or None,

            now_iso(), now_iso(), 1
        ))
        conn.commit()
        conn.close()
        flash("İlan oluşturuldu.", "success")
        return redirect(url_for("dashboard"))

    return render_template("listing_form.html", mode="new", listing=None, inactivity_days=INACTIVITY_DAYS)


@app.route("/listing/<int:listing_id>/edit", methods=["GET", "POST"])
@login_required
def listing_edit(listing_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM listings WHERE id=? AND user_id=?",
        (listing_id, session["user_id"])
    ).fetchone()
    if not row:
        conn.close()
        abort(404)

    if request.method == "POST":
        f = request.form

        mevcut_il = (f.get("mevcut_il") or "").strip()
        mevcut_ilce = (f.get("mevcut_ilce") or "").strip()
        mevcut_mahalle = (f.get("mevcut_mahalle") or "").strip()
        mevcut_kat = (f.get("mevcut_kat") or "").strip()
        mevcut_oda = (f.get("mevcut_oda") or "").strip()

        if not all([mevcut_il, mevcut_ilce, mevcut_mahalle, mevcut_kat, mevcut_oda]):
            flash("Mevcut daire alanları eksik.", "error")
            listing = listing_row_to_dict(row)
            conn.close()
            return render_template("listing_form.html", mode="edit", listing=listing, inactivity_days=INACTIVITY_DAYS)

        hedef_il = (f.get("hedef_il") or "any").strip() or "any"
        hedef_ilce = parse_multiselect(request.form.getlist("hedef_ilce"))
        hedef_mahalle = parse_multiselect(request.form.getlist("hedef_mahalle"))
        hedef_kat = parse_multiselect(request.form.getlist("hedef_kat"))
        hedef_oda = (f.get("hedef_oda") or "any").strip() or "any"
        ucret = (f.get("ucret") or "any").strip() or "any"

        conn.execute("""
            UPDATE listings
            SET
              mevcut_il=?, mevcut_ilce=?, mevcut_mahalle=?, mevcut_bolge=?, mevcut_etap=?, mevcut_kat=?, mevcut_oda=?, mevcut_not=?,
              hedef_il=?, hedef_ilce_json=?, hedef_mahalle_json=?, hedef_bolge=?, hedef_etap=?, hedef_kat_json=?, hedef_oda=?, ucret=?, hedef_not=?,
              updated_at=?
            WHERE id=? AND user_id=?
        """, (
            mevcut_il, mevcut_ilce, mevcut_mahalle,
            (f.get("mevcut_bolge") or "").strip() or None,
            (f.get("mevcut_etap") or "").strip() or None,
            mevcut_kat, mevcut_oda,
            (f.get("mevcut_not") or "").strip() or None,

            hedef_il,
            json.dumps(hedef_ilce, ensure_ascii=False),
            json.dumps(hedef_mahalle, ensure_ascii=False),
            (f.get("hedef_bolge") or "").strip() or None,
            (f.get("hedef_etap") or "").strip() or None,
            json.dumps(hedef_kat, ensure_ascii=False),
            hedef_oda,
            ucret,
            (f.get("hedef_not") or "").strip() or None,

            now_iso(),
            listing_id, session["user_id"]
        ))
        conn.commit()
        conn.close()
        flash("İlan güncellendi.", "success")
        return redirect(url_for("dashboard"))

    listing = listing_row_to_dict(row)
    conn.close()
    return render_template("listing_form.html", mode="edit", listing=listing, inactivity_days=INACTIVITY_DAYS)


@app.route("/listing/<int:listing_id>/delete", methods=["POST"])
@login_required
def listing_delete(listing_id):
    conn = get_db()
    conn.execute("DELETE FROM listings WHERE id=? AND user_id=?", (listing_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("İlan silindi.", "success")
    return redirect(url_for("dashboard"))


@app.route("/listing/<int:listing_id>/toggle-active", methods=["POST"])
@login_required
def listing_toggle_active(listing_id):
    conn = get_db()
    l = conn.execute(
        "SELECT id, is_active FROM listings WHERE id=? AND user_id=?",
        (listing_id, session["user_id"])
    ).fetchone()
    if not l:
        conn.close()
        abort(404)

    new_val = 0 if int(l["is_active"] or 0) == 1 else 1
    conn.execute(
        "UPDATE listings SET is_active=?, updated_at=? WHERE id=? AND user_id=?",
        (new_val, now_iso(), listing_id, session["user_id"])
    )
    conn.commit()
    conn.close()
    flash("İlan durumu güncellendi.", "success")
    return redirect(url_for("dashboard"))


@app.route("/listings")
@login_required
def listings_all():
    # filtreler
    il = (request.args.get("il") or "").strip()
    ilce = (request.args.get("ilce") or "").strip()
    mahalle = (request.args.get("mahalle") or "").strip()
    oda = (request.args.get("oda") or "").strip()
    kat = (request.args.get("kat") or "").strip()
    ucret = (request.args.get("ucret") or "").strip()
    where = ["l.user_id <> ?"]
    params = [session["user_id"]]

    # Pasif ilanlar kullanıcıya gösterilmez (seçenek iptal)
    where.append("l.is_active=1")

    # 7+ gündür giriş yapmayan (admin olmayan) kullanıcıların ilanlarını gizle
    cutoff = (datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)).isoformat(timespec="seconds")
    where.append("(COALESCE(u.role,'user')='admin' OR COALESCE(u.last_login_at,'') >= ?)")
    params.append(cutoff)

    if il:
        where.append("l.mevcut_il=?"); params.append(il)
    if ilce:
        where.append("l.mevcut_ilce=?"); params.append(ilce)
    if mahalle:
        where.append("l.mevcut_mahalle=?"); params.append(mahalle)
    if oda:
        where.append("l.mevcut_oda=?"); params.append(oda)
    if kat:
        where.append("l.mevcut_kat=?"); params.append(kat)
    if ucret:
        where.append("l.ucret=?"); params.append(ucret)

    conn = get_db()
    rows = conn.execute(f"""
        SELECT l.*, u.username
        FROM listings l
        JOIN users u ON u.id = l.user_id
        WHERE {' AND '.join(where)}
        ORDER BY l.id DESC
        LIMIT 500
    """, tuple(params)).fetchall()
    conn.close()

    return render_template("listings_all.html", listings=rows)
@app.route("/matches")
@login_required
def matches():
    # Kullanıcının ilk aktif ilanını baz al (yoksa boş)
    conn = get_db()
    base_row = conn.execute("""
        SELECT * FROM listings
        WHERE user_id=? AND is_active=1
        ORDER BY id DESC
        LIMIT 1
    """, (session["user_id"],)).fetchone()

    if not base_row:
        conn.close()
        # template base/matches_list bekliyor, boş verelim
        base = {"id": None, "mevcut_il": "-", "mevcut_ilce": "-", "mevcut_mahalle": "-"}
        return render_template("matches.html", base=base, matches_list=[])

    base = listing_row_to_dict(base_row)

    # filtreler (matches.html querystring)
    f_ucret = (request.args.get("ucret") or "").strip()
    f_oda = (request.args.get("oda") or "").strip()
    f_kat = (request.args.get("kat") or "").strip()

    # 7+ gündür giriş yapmayan (admin olmayan) kullanıcıların ilanlarını gizle
    cutoff = (datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)).isoformat(timespec="seconds")

    others = conn.execute("""
        SELECT l.*
        FROM listings l
        JOIN users u ON u.id = l.user_id
        WHERE l.user_id <> ? AND l.is_active=1
          AND (COALESCE(u.role,'user')='admin' OR COALESCE(u.last_login_at,'') >= ?)
        ORDER BY l.id DESC
        LIMIT 1000
    """, (session["user_id"], cutoff)).fetchall()
    out = []
    for row in others:
        b = listing_row_to_dict(row)
        if not is_mutual_match(base, b):
            continue
        # extra filtreler
        if f_ucret and (b.get("ucret") != f_ucret):
            continue
        if f_oda and (b.get("mevcut_oda") != f_oda):
            continue
        if f_kat and (str(b.get("mevcut_kat")) != f_kat):
            continue

        # karşı kullanıcı adı
        uname = conn.execute("SELECT username FROM users WHERE id=?", (b["user_id"],)).fetchone()
        b["owner_username"] = uname["username"] if uname else "?"
        out.append(b)

    conn.close()
    return render_template("matches.html", base=base, matches_list=out)


@app.route("/matches/<int:listing_id>")
@login_required
def show_matches(listing_id):
    conn = get_db()
    base_row = conn.execute(
        "SELECT * FROM listings WHERE id=? AND user_id=?",
        (listing_id, session["user_id"])
    ).fetchone()

    if not base_row:
        conn.close()
        abort(404)

    base = listing_row_to_dict(base_row)

    # filtreler (matches.html querystring)
    f_ucret = (request.args.get("ucret") or "").strip()
    f_oda = (request.args.get("oda") or "").strip()
    f_kat = (request.args.get("kat") or "").strip()

    cutoff = (datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)).isoformat(timespec="seconds")

    others = conn.execute("""
        SELECT l.*
        FROM listings l
        JOIN users u ON u.id = l.user_id
        WHERE l.user_id <> ? AND l.is_active=1
          AND (COALESCE(u.role,'user')='admin' OR COALESCE(u.last_login_at,'') >= ?)
        ORDER BY l.id DESC
        LIMIT 1000
    """, (session["user_id"], cutoff)).fetchall()
    out = []
    for row in others:
        b = listing_row_to_dict(row)
        if not is_mutual_match(base, b):
            continue
        if f_ucret and (b.get("ucret") != f_ucret):
            continue
        if f_oda and (b.get("mevcut_oda") != f_oda):
            continue
        if f_kat and (str(b.get("mevcut_kat")) != f_kat):
            continue

        uname = conn.execute("SELECT username FROM users WHERE id=?", (b["user_id"],)).fetchone()
        b["owner_username"] = uname["username"] if uname else "?"
        out.append(b)

    conn.close()
    return render_template("matches.html", base=base, matches_list=out)


@app.route("/inbox")
@login_required
def inbox():
    conn = get_db()
    rows = conn.execute("""
        SELECT t.*,
               CASE WHEN t.user1_id=? THEN u2.username ELSE u1.username END AS other_name,
               (
                 SELECT body FROM messages m
                 WHERE m.thread_id=t.id
                 ORDER BY m.id DESC
                 LIMIT 1
               ) AS last_body
        FROM threads t
        JOIN users u1 ON u1.id = t.user1_id
        JOIN users u2 ON u2.id = t.user2_id
        WHERE t.user1_id=? OR t.user2_id=?
        ORDER BY COALESCE(t.last_message_at, t.created_at) DESC
        LIMIT 300
    """, (session["user_id"], session["user_id"], session["user_id"])).fetchall()
    conn.close()
    return render_template("inbox.html", threads=rows)


@app.route("/start_thread", methods=["POST"])
@login_required
def start_thread():
    other_user_id = request.form.get("other_user_id")
    try:
        other_user_id = int(other_user_id)
    except Exception:
        abort(400)

    if other_user_id == session["user_id"]:
        abort(400)

    conn = get_db()
    if is_blocked(conn, session["user_id"], other_user_id):
        conn.close()
        flash("Bu kullanıcı ile mesajlaşamazsın (engelleme var).", "error")
        return redirect(url_for("matches"))

    a, b = normalize_pair(session["user_id"], other_user_id)
    existing = conn.execute(
        "SELECT id FROM threads WHERE user1_id=? AND user2_id=?",
        (a, b)
    ).fetchone()
    if existing:
        tid = existing["id"]
    else:
        conn.execute(
            "INSERT INTO threads (user1_id, user2_id, created_at, last_message_at) VALUES (?, ?, ?, ?)",
            (a, b, now_iso(), None)
        )
        conn.commit()
        tid = conn.execute("SELECT id FROM threads WHERE user1_id=? AND user2_id=?", (a, b)).fetchone()["id"]

    conn.close()
    return redirect(url_for("thread_view", thread_id=tid))


@app.route("/thread/<int:thread_id>")
@login_required
def thread_view(thread_id):
    conn = get_db()
    t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not t:
        conn.close()
        abort(404)

    if session["user_id"] not in (t["user1_id"], t["user2_id"]):
        conn.close()
        abort(403)

    other_id = t["user2_id"] if session["user_id"] == t["user1_id"] else t["user1_id"]

    if is_blocked(conn, session["user_id"], other_id):
        conn.close()
        flash("Bu konuşmaya erişim yok (engelleme var).", "error")
        return redirect(url_for("inbox"))

    msgs = conn.execute("""
        SELECT m.id,
               m.thread_id,
               m.sender_id AS from_user_id,
               u.username AS from_username,
               m.body,
               m.created_at
        FROM messages m
        JOIN users u ON u.id = m.sender_id
        WHERE m.thread_id=?
        ORDER BY m.id ASC
        LIMIT 1000
    """, (thread_id,)).fetchall()

    conn.close()
    return render_template("thread.html", thread=t, messages=msgs, me_id=session["user_id"])


@app.route("/thread/<int:thread_id>/send", methods=["POST"])
@login_required
def thread_send(thread_id):
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Mesaj boş olamaz.", "error")
        return redirect(url_for("thread_view", thread_id=thread_id))

    conn = get_db()
    t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not t:
        conn.close()
        abort(404)
    if session["user_id"] not in (t["user1_id"], t["user2_id"]):
        conn.close()
        abort(403)

    other_id = t["user2_id"] if session["user_id"] == t["user1_id"] else t["user1_id"]
    if is_blocked(conn, session["user_id"], other_id):
        conn.close()
        flash("Bu kullanıcı ile mesajlaşamazsın (engelleme var).", "error")
        return redirect(url_for("inbox"))

    conn.execute(
        "INSERT INTO messages (thread_id, sender_id, body, created_at) VALUES (?, ?, ?, ?)",
        (thread_id, session["user_id"], body, now_iso())
    )
    conn.execute("UPDATE threads SET last_message_at=? WHERE id=?", (now_iso(), thread_id))
    conn.commit()
    conn.close()
    return redirect(url_for("thread_view", thread_id=thread_id))


@app.route("/user_block", methods=["POST"])
@login_required
def user_block():
    thread_id = request.form.get("thread_id")
    try:
        thread_id = int(thread_id)
    except Exception:
        abort(400)

    conn = get_db()
    t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not t:
        conn.close()
        abort(404)
    if session["user_id"] not in (t["user1_id"], t["user2_id"]):
        conn.close()
        abort(403)

    other_id = t["user2_id"] if session["user_id"] == t["user1_id"] else t["user1_id"]

    try:
        conn.execute(
            "INSERT INTO blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
            (session["user_id"], other_id, now_iso())
        )
        conn.commit()
        flash("Kullanıcı engellendi.", "success")
    except sqlite3.IntegrityError:
        flash("Kullanıcı zaten engellenmiş.", "info")
    finally:
        conn.close()

    return redirect(url_for("inbox"))


@app.route("/user_report", methods=["POST"])
@login_required
def user_report():
    reason = (request.form.get("reason") or "").strip()
    thread_id = request.form.get("thread_id")
    try:
        thread_id = int(thread_id) if thread_id else None
    except Exception:
        thread_id = None

    if not reason:
        flash("Şikayet nedeni boş olamaz.", "error")
        return redirect(request.referrer or url_for("inbox"))

    conn = get_db()
    target_user_id = None
    if thread_id:
        t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if t and session["user_id"] in (t["user1_id"], t["user2_id"]):
            target_user_id = t["user2_id"] if session["user_id"] == t["user1_id"] else t["user1_id"]

    conn.execute("""
        INSERT INTO reports
        (reporter_user_id, target_user_id, listing_id, thread_id, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
    """, (
        session["user_id"],
        target_user_id,
        None,
        thread_id,
        reason,
        now_iso()
    ))
    conn.commit()
    conn.close()
    flash("Şikayet iletildi.", "success")
    return redirect(request.referrer or url_for("inbox"))


# -----------------------------
# Admin
# -----------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    open_reports = conn.execute("SELECT COUNT(*) c FROM reports WHERE status='open'").fetchone()["c"]
    users_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    listings_count = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    threads_count = conn.execute("SELECT COUNT(*) c FROM threads").fetchone()["c"]
    conn.close()
    return render_template(
        "admin_dashboard.html",
        open_reports=open_reports,
        users_count=users_count,
        listings_count=listings_count,
        threads_count=threads_count
    )


@app.route("/admin/reports")
@admin_required
def admin_reports():
    status = (request.args.get("status") or "open").strip()
    conn = get_db()

    if status == "all":
        rows = conn.execute("""
            SELECT r.*,
                   u1.username AS reporter_name,
                   u2.username AS target_name
            FROM reports r
            LEFT JOIN users u1 ON u1.id = r.reporter_user_id
            LEFT JOIN users u2 ON u2.id = r.target_user_id
            ORDER BY r.id DESC
            LIMIT 500
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT r.*,
                   u1.username AS reporter_name,
                   u2.username AS target_name
            FROM reports r
            LEFT JOIN users u1 ON u1.id = r.reporter_user_id
            LEFT JOIN users u2 ON u2.id = r.target_user_id
            WHERE r.status=?
            ORDER BY r.id DESC
            LIMIT 500
        """, (status,)).fetchall()

    conn.close()
    return render_template("admin_reports.html", rows=rows, status=status)


@app.route("/admin/reports/<int:report_id>/set", methods=["POST"])
@admin_required
def admin_report_set(report_id):
    new_status = (request.form.get("status") or "").strip()
    if new_status not in ("open", "closed"):
        abort(400)
    conn = get_db()
    conn.execute("UPDATE reports SET status=? WHERE id=?", (new_status, report_id))
    conn.commit()
    conn.close()
    flash("Rapor durumu güncellendi.", "success")
    return redirect(url_for("admin_reports"))


@app.route("/admin/users")
@admin_required
def admin_users():
    q = (request.args.get("q") or "").strip()
    conn = get_db()
    if q:
        rows = conn.execute("""
            SELECT id, username, role, created_at, last_login_at
            FROM users
            WHERE username LIKE ?
            ORDER BY id DESC
            LIMIT 500
        """, (f"%{q}%",)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, username, role, created_at, last_login_at
            FROM users
            ORDER BY id DESC
            LIMIT 500
        """).fetchall()
    conn.close()
    return render_template("admin_users.html", rows=rows, q=q)


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    if user_id == session.get("user_id"):
        flash("Kendi admin hesabını silemezsin.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    u = conn.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
    if not u:
        conn.close()
        flash("Kullanıcı bulunamadı.", "error")
        return redirect(url_for("admin_users"))

    hard_delete_user(conn, user_id)
    conn.commit()
    conn.close()
    flash(f"Kullanıcı silindi: {u['username']}", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/messages")
@admin_required
def admin_messages():
    conn = get_db()
    rows = conn.execute("""
        SELECT t.*,
               u1.username AS user1_name,
               u2.username AS user2_name,
               (
                 SELECT body FROM messages m
                 WHERE m.thread_id=t.id
                 ORDER BY m.id DESC
                 LIMIT 1
               ) AS last_body
        FROM threads t
        JOIN users u1 ON u1.id = t.user1_id
        JOIN users u2 ON u2.id = t.user2_id
        ORDER BY COALESCE(t.last_message_at, t.created_at) DESC
        LIMIT 300
    """).fetchall()
    conn.close()
    return render_template("admin_messages.html", rows=rows)


@app.route("/admin/thread/<int:thread_id>")
@admin_required
def admin_thread_view(thread_id):
    conn = get_db()
    t = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not t:
        conn.close()
        abort(404)
    u1 = conn.execute("SELECT id, username FROM users WHERE id=?", (t["user1_id"],)).fetchone()
    u2 = conn.execute("SELECT id, username FROM users WHERE id=?", (t["user2_id"],)).fetchone()
    msgs = conn.execute("""
        SELECT m.id,
               m.thread_id,
               m.sender_id AS from_user_id,
               u.username AS from_username,
               m.body,
               m.created_at
        FROM messages m
        JOIN users u ON u.id = m.sender_id
        WHERE m.thread_id=?
        ORDER BY m.id ASC
        LIMIT 2000
    """, (thread_id,)).fetchall()
    conn.close()
    return render_template("admin_thread.html", thread=t, u1=u1, u2=u2, msgs=msgs)


if __name__ == "__main__":
    app.run(debug=True)
