"""
Summit Wealth v4 - STANDALONE COMPLETE VERSION
No external file dependencies!
"""
import os, sqlite3, hashlib, secrets, datetime, uuid, logging
from flask import Flask, request, jsonify, session, redirect, render_template
from flask_cors import CORS
from functools import wraps

try:
    from binance.client import Client
    BINANCE_AVAILABLE = True
except:
    BINANCE_AVAILABLE = False
    Client = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "summit-2025")
CORS(app, supports_credentials=True)
logging.basicConfig(level=logging.INFO)

api_key = os.environ.get("BINANCE_API_KEY")
api_secret = os.environ.get("BINANCE_API_SECRET")
try:
    bnb = Client(api_key, api_secret) if (BINANCE_AVAILABLE and api_key and api_secret) else None
except Exception as e:
    logging.warning(f"Binance connection failed: {e}")
    bnb = None

DB_PATH = os.path.join(os.path.dirname(__file__), "summit.db")
NETWORKS = {"TRC20": {"network": "TRX"}, "BEP20": {"network": "BSC"}, "ERC20": {"network": "ETH"}}

# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT,
    email TEXT UNIQUE,
    phone TEXT,
    password_hash TEXT,
    pin_hash TEXT,
    role TEXT DEFAULT 'client',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    balance REAL DEFAULT 0,
    equity REAL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    account_id TEXT,
    type TEXT,
    method TEXT,
    amount_usd REAL,
    reference TEXT,
    status TEXT DEFAULT 'PENDING',
    note TEXT,
    created_at TEXT,
    completed_at TEXT,
    binance_tx_id TEXT,
    deposit_address TEXT
);
""")
        db.commit()
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            uid  = str(uuid.uuid4())
            uid2 = str(uuid.uuid4())
            aid2 = str(uuid.uuid4())
            now  = datetime.datetime.utcnow().isoformat()
            db.execute(
                "INSERT INTO users(id,name,email,password_hash,pin_hash,role,created_at) VALUES(?,?,?,?,?,'admin',?)",
                (uid, "Admin", "admin@test.com",
                 hashlib.sha256(b"admin1234").hexdigest(),
                 hashlib.sha256(b"000000").hexdigest(), now)
            )
            db.execute(
                "INSERT INTO users(id,name,email,password_hash,pin_hash,role,created_at) VALUES(?,?,?,?,?,'client',?)",
                (uid2, "John Trader", "john@test.com",
                 hashlib.sha256(b"demo1234").hexdigest(),
                 hashlib.sha256(b"123456").hexdigest(), now)
            )
            db.execute(
                "INSERT INTO accounts(id,user_id,balance,equity,created_at) VALUES(?,?,5000,5000,?)",
                (aid2, uid2, now)
            )
            db.commit()
            print("Demo: john@test.com / demo1234  |  Admin: admin@test.com / admin1234")

# ── HELPERS ───────────────────────────────────────────────────────────────────

def _hash(s):  return hashlib.sha256(str(s).encode()).hexdigest()
def _uid():    return str(uuid.uuid4())
def _now():    return datetime.datetime.utcnow().isoformat()

def ok(data=None, **kw):
    p = {"success": True}
    if data:
        p["data"] = data
    p.update(kw)
    return jsonify(p)

def err(msg, code=400):
    return jsonify({"success": False, "error": msg}), code

def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session:
            return err("Not logged in", 401)
        return f(*a, **kw)
    return dec

# ── PAGE ROUTES ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("login.html")

@app.route("/register")
def register_page():
    return render_template("register.html")

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session or session.get("role") != "client":
        return redirect("/")
    return render_template("dashboard.html")

@app.route("/admin")
def admin_index():
    return render_template("admin_login.html")

@app.route("/admin/dashboard")
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect("/admin")
    return render_template("admin_dashboard.html")

# ── AUTH API ──────────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d     = request.json
    name  = d.get("name", "").strip()
    email = d.get("email", "").lower().strip()
    phone = d.get("phone", "").strip()
    pw    = d.get("password", "")
    pin   = d.get("pin", "000000")
    if not name or not email or len(pw) < 6:
        return err("Invalid input")
    try:
        with get_db() as db:
            uid, aid, now = _uid(), _uid(), _now()
            db.execute(
                "INSERT INTO users(id,name,email,phone,password_hash,pin_hash,role,created_at) VALUES(?,?,?,?,?,?,'client',?)",
                (uid, name, email, phone, _hash(pw), _hash(pin), now)
            )
            db.execute(
                "INSERT INTO accounts(id,user_id,balance,equity,created_at) VALUES(?,?,0,0,?)",
                (aid, uid, now)
            )
            db.commit()
        session["user_id"] = uid
        session["role"]    = "client"
        return ok({"redirect": "/dashboard"})
    except:
        return err("Email already registered", 409)

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d     = request.json
    email = d.get("email", "").lower()
    pw    = d.get("password", "")
    admin = d.get("admin", False)
    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or u["password_hash"] != _hash(pw):
            return err("Invalid credentials", 401)
        if admin and u["role"] != "admin":
            return err("Not admin", 403)
        if not admin and u["role"] == "admin":
            return err("Use admin login", 403)
        session["user_id"] = u["id"]
        session["role"]    = u["role"]
    return ok({"role": u["role"], "name": u["name"],
               "redirect": "/admin/dashboard" if admin else "/dashboard"})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return ok()

# ── CLIENT API ────────────────────────────────────────────────────────────────

@app.route("/api/client/summary")
@login_required
def client_summary():
    uid = session["user_id"]
    with get_db() as db:
        u   = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        a   = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        dep = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) as s FROM transactions WHERE user_id=? AND type='DEPOSIT' AND status='COMPLETED'",
            (uid,)).fetchone()
        wdr = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) as s FROM transactions WHERE user_id=? AND type='WITHDRAWAL' AND status='COMPLETED'",
            (uid,)).fetchone()
    return ok({"name": u["name"], "balance": a["balance"], "equity": a["equity"],
               "total_deposits": dep["s"], "total_withdrawals": wdr["s"]})

@app.route("/api/client/transactions")
@login_required
def client_transactions():
    uid = session["user_id"]
    with get_db() as db:
        txs = db.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC", (uid,)
        ).fetchall()
    return ok([dict(t) for t in txs])

@app.route("/api/client/deposit/address")
@login_required
def client_deposit_address():
    if not bnb:
        return err("Binance not configured", 502)
    net = request.args.get("network", "TRC20").upper()
    if net not in NETWORKS:
        return err("Invalid network")
    try:
        addr = bnb.get_deposit_address(coin="USDT", network=NETWORKS[net]["network"])
        return ok({"address": addr["address"], "network": net})
    except Exception as e:
        return err(f"Error: {str(e)}", 502)

@app.route("/api/client/deposit/pending", methods=["POST"])
@login_required
def client_deposit_pending():
    d    = request.json
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network", "TRC20").upper()
    addr = d.get("address", "")
    if amt < 100:
        return err("Min $100")
    with get_db() as db:
        acct = db.execute("SELECT id FROM accounts WHERE user_id=?", (uid,)).fetchone()
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,reference,status,deposit_address,created_at) "
            "VALUES(?,?,?,'DEPOSIT',?,?,?,'PENDING',?,?)",
            (_uid(), uid, acct[0] if acct else None, net, amt,
             "SWC-" + secrets.token_hex(4).upper(), addr, _now())
        )
        db.commit()
    return ok()

@app.route("/api/client/withdraw", methods=["POST"])
@login_required
def client_withdraw():
    if not bnb:
        return err("Binance not configured", 502)
    d   = request.json
    uid = session["user_id"]
    amt = float(d.get("amount", 0))
    net = d.get("network", "TRC20").upper()
    addr = d.get("address", "")
    pin  = d.get("pin", "")
    if amt < 1000:
        return err("Min $1,000")
    if not addr:
        return err("Enter address")
    with get_db() as db:
        u = db.execute("SELECT pin_hash FROM users WHERE id=?", (uid,)).fetchone()
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if a["balance"] < amt:
            return err("Insufficient balance")
        if u["pin_hash"] and u["pin_hash"] != _hash(pin):
            return err("Invalid PIN", 403)
        try:
            result = bnb.withdraw(
                coin="USDT", address=addr, amount=amt,
                network=NETWORKS[net]["network"], name="Summit"
            )
            bid = result.get("id", "")
        except Exception as e:
            return err(f"Binance error: {str(e)}", 502)
        db.execute("UPDATE accounts SET balance=balance-? WHERE user_id=?", (amt, uid))
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,reference,status,binance_tx_id,created_at,completed_at) "
            "VALUES(?,?,?,'WITHDRAWAL',?,?,?,'COMPLETED',?,?,?)",
            (_uid(), uid, a["id"], net, amt,
             "WD-" + secrets.token_hex(4).upper(), bid, _now(), _now())
        )
        db.commit()
    return ok({"binance_id": bid})

# ── ADMIN API ─────────────────────────────────────────────────────────────────

@app.route("/api/admin/stats")
@login_required
def admin_stats():
    if session.get("role") != "admin":
        return err("Not admin", 403)
    with get_db() as db:
        clients     = db.execute("SELECT COUNT(*) as c FROM users WHERE role='client'").fetchone()["c"]
        deposits    = db.execute("SELECT COALESCE(SUM(amount_usd),0) as s FROM transactions WHERE type='DEPOSIT' AND status='COMPLETED'").fetchone()["s"]
        withdrawals = db.execute("SELECT COALESCE(SUM(amount_usd),0) as s FROM transactions WHERE type='WITHDRAWAL' AND status='COMPLETED'").fetchone()["s"]
    return ok({"clients": clients, "deposits": deposits, "withdrawals": withdrawals})

@app.route("/api/admin/transactions")
@login_required
def admin_transactions():
    if session.get("role") != "admin":
        return err("Not admin", 403)
    with get_db() as db:
        txs = db.execute(
            "SELECT t.*, u.name as user_name FROM transactions t "
            "JOIN users u ON t.user_id=u.id ORDER BY t.created_at DESC"
        ).fetchall()
    return ok([dict(t) for t in txs])

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("\n Summit Wealth v4 - COMPLETE & STANDALONE")
    print("   http://127.0.0.1:8080")
    print("   Client: john@test.com / demo1234")
    print("   Admin:  admin@test.com / admin1234")
    if bnb:
        print("   Binance Connected\n")
    else:
        print("   Binance: set BINANCE_API_KEY & BINANCE_API_SECRET\n")
    app.run(debug=False, port=8080, host="0.0.0.0")