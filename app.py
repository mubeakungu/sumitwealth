"""
Summit Wealth v5 - BINANCE ONLY + REFERRAL SYSTEM
- Referral code per client (20% commission on first $100+ deposit)
- Referral earnings withdrawable as USDT via Binance
- No M-Pesa
"""

import os, sqlite3, hashlib, secrets, datetime, uuid, logging, threading
from flask import Flask, request, jsonify, session, redirect, render_template
from flask_cors import CORS
from functools import wraps

try:
    from binance.client import Client
    BINANCE_AVAILABLE = True
except ImportError:
    BINANCE_AVAILABLE = False
    Client = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "summit-2025")
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE']   = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
CORS(app, supports_credentials=True)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("summit")

# ── BINANCE ───────────────────────────────────────────────────────────────────
api_key    = os.environ.get("BINANCE_API_KEY")
api_secret = os.environ.get("BINANCE_API_SECRET")
try:
    bnb = Client(api_key, api_secret) if (BINANCE_AVAILABLE and api_key and api_secret) else None
except Exception as e:
    logging.warning(f"Binance connection failed: {e}")
    bnb = None

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_PATH  = os.path.join(os.path.dirname(__file__), "summit.db")
NETWORKS = {
    "TRC20": {"network": "TRX"},
    "BEP20": {"network": "BSC"},
    "ERC20": {"network": "ETH"},
}
MANUAL_WALLETS = {
    "TRC20": os.environ.get("WALLET_TRC20", ""),
    "BEP20": os.environ.get("WALLET_BEP20", ""),
    "ERC20": os.environ.get("WALLET_ERC20", ""),
}

REFERRAL_COMMISSION_PCT = 0.20
REFERRAL_MIN_DEPOSIT    = 100.0

EMA_FAST        = 9
EMA_SLOW        = 21
CHECK_INTERVAL  = 30
STOP_LOSS_PCT   = 0.015
TAKE_PROFIT_PCT = 0.030

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    email         TEXT UNIQUE,
    phone         TEXT,
    password_hash TEXT,
    pin_hash      TEXT,
    role          TEXT DEFAULT 'client',
    referral_code TEXT UNIQUE,
    referred_by   TEXT,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT,
    balance     REAL DEFAULT 0,
    equity      REAL DEFAULT 0,
    ref_balance REAL DEFAULT 0,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT,
    account_id      TEXT,
    type            TEXT,
    method          TEXT,
    amount_usd      REAL,
    reference       TEXT,
    status          TEXT DEFAULT 'PENDING',
    note            TEXT,
    created_at      TEXT,
    completed_at    TEXT,
    binance_tx_id   TEXT,
    deposit_address TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    id             TEXT PRIMARY KEY,
    referrer_id    TEXT,
    referred_id    TEXT,
    commission_usd REAL DEFAULT 0,
    status         TEXT DEFAULT 'CREDITED',
    triggered_by   TEXT,
    created_at     TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id           TEXT PRIMARY KEY,
    user_id      TEXT,
    account_id   TEXT,
    symbol       TEXT,
    direction    TEXT,
    entry_price  REAL,
    quantity     REAL,
    stop_loss    REAL,
    take_profit  REAL,
    close_price  REAL,
    pnl          REAL DEFAULT 0,
    status       TEXT DEFAULT 'OPEN',
    close_reason TEXT,
    opened_at    TEXT,
    closed_at    TEXT
);
""")
        db.commit()

        # Safe migrations
        for col, tbl, defval in [
            ("referral_code", "users",    "''"),
            ("referred_by",   "users",    "NULL"),
            ("ref_balance",   "accounts", "0"),
        ]:
            try:
                db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {defval}")
                db.commit()
            except:
                pass

        # Generate referral codes for users that don't have one
        users = db.execute(
            "SELECT id FROM users WHERE referral_code IS NULL OR referral_code=''"
        ).fetchall()
        for u in users:
            db.execute(
                "UPDATE users SET referral_code=? WHERE id=?",
                (secrets.token_hex(4).upper(), u["id"])
            )
        db.commit()

        # Seed demo data if empty
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            uid  = str(uuid.uuid4())
            uid2 = str(uuid.uuid4())
            aid2 = str(uuid.uuid4())
            now  = datetime.datetime.utcnow().isoformat()
            db.execute(
                "INSERT INTO users(id,name,email,password_hash,pin_hash,role,referral_code,created_at) "
                "VALUES(?,?,?,?,?,'admin',?,?)",
                (uid, "Admin", "admin@test.com",
                 hashlib.sha256(b"admin1234").hexdigest(),
                 hashlib.sha256(b"000000").hexdigest(),
                 secrets.token_hex(4).upper(), now)
            )
            db.execute(
                "INSERT INTO users(id,name,email,password_hash,pin_hash,role,referral_code,created_at) "
                "VALUES(?,?,?,?,?,'client',?,?)",
                (uid2, "John Trader", "john@test.com",
                 hashlib.sha256(b"demo1234").hexdigest(),
                 hashlib.sha256(b"123456").hexdigest(),
                 secrets.token_hex(4).upper(), now)
            )
            db.execute(
                "INSERT INTO accounts(id,user_id,balance,equity,ref_balance,created_at) "
                "VALUES(?,?,5000,5000,0,?)",
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
    if data is not None: p["data"] = data
    p.update(kw)
    return jsonify(p)

def err(msg, code=400):
    return jsonify({"success": False, "error": msg}), code

def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session: return err("Not logged in", 401)
        return f(*a, **kw)
    return dec

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session: return err("Not logged in", 401)
        if session.get("role") != "admin": return err("Admin access required", 403)
        return f(*a, **kw)
    return dec

# ── REFERRAL ENGINE ───────────────────────────────────────────────────────────
def process_referral_commission(tx_id: str, user_id: str, amount_usd: float):
    if amount_usd < REFERRAL_MIN_DEPOSIT:
        return
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["referred_by"]:
            return
        already_paid = db.execute(
            "SELECT id FROM referrals WHERE referred_id=?", (user_id,)
        ).fetchone()
        if already_paid:
            return
        referrer = db.execute(
            "SELECT * FROM users WHERE id=?", (user["referred_by"],)
        ).fetchone()
        if not referrer:
            return
        commission = round(amount_usd * REFERRAL_COMMISSION_PCT, 2)
        db.execute(
            "INSERT INTO referrals(id,referrer_id,referred_id,commission_usd,"
            "status,triggered_by,created_at) VALUES(?,?,?,?,'CREDITED',?,?)",
            (_uid(), referrer["id"], user_id, commission, tx_id, _now())
        )
        db.execute(
            "UPDATE accounts SET ref_balance=ref_balance+? WHERE user_id=?",
            (commission, referrer["id"])
        )
        db.commit()
        log.info(
            f"Referral commission: {referrer['name']} credited "
            f"${commission} for referring {user['name']}"
        )

# ── TRADE BOT ─────────────────────────────────────────────────────────────────
def _ema(prices, period):
    if len(prices) < period: return prices[-1]
    k = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val

def get_trend(client, symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval="1h", limit=50)
        closes = [float(k[4]) for k in klines]
        fast = _ema(closes, EMA_FAST)
        slow = _ema(closes, EMA_SLOW)
        if fast > slow * 1.001:   return "UP"
        elif fast < slow * 0.999: return "DOWN"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def _open_trade(db, user_id, account_id, symbol, direction, entry_price, quantity):
    sl = entry_price * (1 - STOP_LOSS_PCT)   if direction == "BUY" else entry_price * (1 + STOP_LOSS_PCT)
    tp = entry_price * (1 + TAKE_PROFIT_PCT) if direction == "BUY" else entry_price * (1 - TAKE_PROFIT_PCT)
    db.execute(
        "INSERT INTO trades(id,user_id,account_id,symbol,direction,entry_price,"
        "quantity,stop_loss,take_profit,status,opened_at) VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?)",
        (_uid(), user_id, account_id, symbol, direction, entry_price, quantity, sl, tp, _now())
    )

def _close_trade(db, trade, close_price, reason):
    pnl = (
        (close_price - trade["entry_price"]) * trade["quantity"]
        if trade["direction"] == "BUY"
        else (trade["entry_price"] - close_price) * trade["quantity"]
    )
    db.execute(
        "UPDATE trades SET status='CLOSED',close_price=?,pnl=?,close_reason=?,closed_at=? WHERE id=?",
        (close_price, pnl, reason, _now(), trade["id"])
    )
    db.execute(
        "UPDATE accounts SET balance=balance+?,equity=equity+? WHERE user_id=?",
        (pnl, pnl, trade["user_id"])
    )
    db.commit()
    return pnl

def trade_bot_loop(stop_event):
    log.info("Trade bot started")
    while not stop_event.is_set():
        if bnb:
            try:
                with get_db() as db:
                    open_trades = db.execute(
                        "SELECT * FROM trades WHERE status='OPEN'"
                    ).fetchall()
                if open_trades:
                    syms = set(t["symbol"] for t in open_trades)
                    prices, trends = {}, {}
                    for sym in syms:
                        try:
                            prices[sym] = float(bnb.get_symbol_ticker(symbol=sym)["price"])
                            trends[sym] = get_trend(bnb, sym)
                        except:
                            pass
                    with get_db() as db:
                        for trade in open_trades:
                            sym   = trade["symbol"]
                            price = prices.get(sym)
                            trend = trends.get(sym, "NEUTRAL")
                            if not price: continue
                            d  = trade["direction"]
                            sl = trade["stop_loss"]
                            tp = trade["take_profit"]
                            if (d == "BUY" and price <= sl) or (d == "SELL" and price >= sl):
                                _close_trade(db, trade, price, "STOP_LOSS")
                                new_dir = None
                                if d == "BUY"  and trend == "DOWN": new_dir = "SELL"
                                if d == "SELL" and trend == "UP":   new_dir = "BUY"
                                if new_dir:
                                    _open_trade(db, trade["user_id"], trade["account_id"],
                                                sym, new_dir, price, trade["quantity"])
                                db.commit()
                                continue
                            if (d == "BUY" and price >= tp) or (d == "SELL" and price <= tp):
                                _close_trade(db, trade, price, "TAKE_PROFIT")
                                if (d == "BUY" and trend == "UP") or (d == "SELL" and trend == "DOWN"):
                                    _open_trade(db, trade["user_id"], trade["account_id"],
                                                sym, d, price, trade["quantity"])
                                db.commit()
                                continue
                            if (d == "BUY" and trend == "DOWN") or (d == "SELL" and trend == "UP"):
                                _close_trade(db, trade, price, "TREND_REVERSAL")
                                _open_trade(db, trade["user_id"], trade["account_id"],
                                            sym, "SELL" if d == "BUY" else "BUY",
                                            price, trade["quantity"])
                                db.commit()
            except Exception as e:
                log.error(f"Trade bot error: {e}")
        stop_event.wait(CHECK_INTERVAL)
    log.info("Trade bot stopped")

# ── PAGE ROUTES ───────────────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("login.html")

@app.route("/register")
def register_page(): return render_template("register.html")

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session or session.get("role") != "client":
        return redirect("/")
    return render_template("dashboard.html")

@app.route("/admin")
def admin_index(): return render_template("admin_login.html")

@app.route("/admin/dashboard")
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect("/admin")
    return render_template("admin_dashboard.html")

# ── AUTH ──────────────────────────────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d     = request.json or {}
    name  = d.get("name", "").strip()
    email = d.get("email", "").lower().strip()
    phone = d.get("phone", "").strip()
    pw    = d.get("password", "")
    pin   = d.get("pin", "000000")
    ref   = d.get("ref_code", "").strip().upper()

    if not name or not email or len(pw) < 6:
        return err("Name, email and password (min 6 chars) required")
    if len(pin) != 6 or not pin.isdigit():
        return err("PIN must be exactly 6 digits")

    try:
        with get_db() as db:
            referred_by = None
            if ref:
                referrer = db.execute(
                    "SELECT id FROM users WHERE referral_code=?", (ref,)
                ).fetchone()
                if referrer:
                    referred_by = referrer["id"]

            uid, aid, now = _uid(), _uid(), _now()
            my_code = secrets.token_hex(4).upper()

            db.execute(
                "INSERT INTO users(id,name,email,phone,password_hash,pin_hash,"
                "role,referral_code,referred_by,created_at) VALUES(?,?,?,?,?,?,'client',?,?,?)",
                (uid, name, email, phone, _hash(pw), _hash(pin), my_code, referred_by, now)
            )
            db.execute(
                "INSERT INTO accounts(id,user_id,balance,equity,ref_balance,created_at) "
                "VALUES(?,?,0,0,0,?)",
                (aid, uid, now)
            )
            db.commit()

        session["user_id"] = uid
        session["role"]    = "client"
        return ok({"redirect": "/dashboard"})
    except sqlite3.IntegrityError:
        return err("Email already registered", 409)

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d     = request.json or {}
    email = d.get("email", "").lower().strip()
    pw    = d.get("password", "")
    admin = d.get("admin", False)

    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or u["password_hash"] != _hash(pw):
            return err("Invalid credentials", 401)
        if admin and u["role"] != "admin":
            return err("Not an admin account", 403)
        if not admin and u["role"] == "admin":
            return err("Please use admin login", 403)
        session["user_id"] = u["id"]
        session["role"]    = u["role"]

    return ok({
        "role":     u["role"],
        "name":     u["name"],
        "redirect": "/admin/dashboard" if admin else "/dashboard"
    })

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
            "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
            "WHERE user_id=? AND type='DEPOSIT' AND status='COMPLETED'", (uid,)
        ).fetchone()
        wdr = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
            "WHERE user_id=? AND type='WITHDRAWAL' AND status='COMPLETED'", (uid,)
        ).fetchone()
        ref_count  = db.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?", (uid,)
        ).fetchone()
        ref_earned = db.execute(
            "SELECT COALESCE(SUM(commission_usd),0) AS s FROM referrals WHERE referrer_id=?", (uid,)
        ).fetchone()
        open_trades = db.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE user_id=? AND status='OPEN'", (uid,)
        ).fetchone()

    return ok({
        "name":              u["name"],
        "balance":           a["balance"]       if a else 0,
        "equity":            a["equity"]        if a else 0,
        "total_deposits":    dep["s"],
        "total_withdrawals": wdr["s"],
        "ref_balance":       a["ref_balance"]   if a else 0,
        "ref_code":          u["referral_code"] or "",
        "ref_count":         ref_count["c"],
        "ref_earned":        ref_earned["s"],
        "open_trades":       open_trades["c"],
    })

@app.route("/api/client/referrals")
@login_required
def client_referrals():
    uid = session["user_id"]
    with get_db() as db:
        refs = db.execute(
            "SELECT r.*, u.name AS referred_name, u.email AS referred_email "
            "FROM referrals r JOIN users u ON r.referred_id=u.id "
            "WHERE r.referrer_id=? ORDER BY r.created_at DESC",
            (uid,)
        ).fetchall()
    return ok([dict(r) for r in refs])

@app.route("/api/client/referral/withdraw", methods=["POST"])
@login_required
def client_referral_withdraw():
    d   = request.json or {}
    uid = session["user_id"]
    pin = d.get("pin", "")
    addr = d.get("address", "").strip()
    net  = d.get("network", "TRC20").upper()

    if not addr:            return err("Enter your USDT wallet address")
    if net not in NETWORKS: return err("Invalid network")

    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()

        if u["pin_hash"] and u["pin_hash"] != _hash(pin):
            return err("Invalid PIN", 403)

        ref_bal = a["ref_balance"] if a else 0
        if ref_bal < 10:
            return err("Minimum referral withdrawal is $10")

        bid    = ""
        status = "PENDING"
        if bnb:
            try:
                result = bnb.withdraw(
                    coin="USDT",
                    address=addr,
                    amount=ref_bal,
                    network=NETWORKS[net]["network"],
                    name="SummitReferral"
                )
                bid    = result.get("id", "")
                status = "COMPLETED"
            except Exception as e:
                return err(f"Binance withdrawal error: {str(e)}", 502)

        db.execute(
            "UPDATE accounts SET ref_balance=0 WHERE user_id=?", (uid,)
        )
        ref = "REF-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,binance_tx_id,deposit_address,created_at,completed_at) "
            "VALUES(?,?,?,'REFERRAL_WITHDRAWAL',?,?,?,?,?,?,?,?,?)",
            (_uid(), uid, a["id"], net, ref_bal, ref, status,
             f"Referral commission to {addr[:20]}...", bid,
             addr, _now(), _now() if status == "COMPLETED" else None)
        )
        db.commit()

    msg = f"${ref_bal:.2f} sent to your wallet." if bid else "Withdrawal submitted. Admin will process."
    return ok({"reference": ref, "binance_id": bid, "message": msg})

@app.route("/api/client/transactions")
@login_required
def client_transactions():
    uid = session["user_id"]
    with get_db() as db:
        txs = db.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC", (uid,)
        ).fetchall()
    return ok([dict(t) for t in txs])

@app.route("/api/client/trades")
@login_required
def client_trades():
    uid    = session["user_id"]
    status = request.args.get("status", "")
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM trades WHERE user_id=? AND status=? ORDER BY opened_at DESC",
                (uid, status.upper())
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM trades WHERE user_id=? ORDER BY opened_at DESC", (uid,)
            ).fetchall()
    return ok([dict(r) for r in rows])

# ── DEPOSIT ───────────────────────────────────────────────────────────────────
@app.route("/api/client/deposit/address")
@login_required
def client_deposit_address():
    net = request.args.get("network", "TRC20").upper()
    if net not in NETWORKS: return err("Invalid network")

    if bnb:
        try:
            addr = bnb.get_deposit_address(coin="USDT", network=NETWORKS[net]["network"])
            return ok({"address": addr["address"], "network": net, "mode": "auto"})
        except Exception as e:
            log.warning(f"Binance deposit address failed: {e}")

    wallet = MANUAL_WALLETS.get(net, "")
    if not wallet:
        return err("Deposit address not configured. Contact support.")
    return ok({"address": wallet, "network": net, "mode": "manual"})

@app.route("/api/client/deposit/pending", methods=["POST"])
@login_required
def client_deposit_pending():
    d    = request.json or {}
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network", "TRC20").upper()
    addr = d.get("address", "").strip()

    if amt < 100:  return err("Minimum deposit is $100")
    if not addr:   return err("Deposit address is required")

    with get_db() as db:
        acct = db.execute("SELECT id FROM accounts WHERE user_id=?", (uid,)).fetchone()
        ref  = "SWC-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,deposit_address,created_at) "
            "VALUES(?,?,?,'DEPOSIT',?,?,?,'PENDING',?,?)",
            (_uid(), uid, acct["id"] if acct else None, net, amt, ref, addr, _now())
        )
        db.commit()
    return ok({"reference": ref, "message": "Deposit submitted. Awaiting admin confirmation."})

# ── WITHDRAWAL ────────────────────────────────────────────────────────────────
@app.route("/api/client/withdraw", methods=["POST"])
@login_required
def client_withdraw():
    d    = request.json or {}
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network", "TRC20").upper()
    addr = d.get("address", "").strip()
    pin  = d.get("pin", "")

    if amt < 1000:          return err("Minimum withdrawal is $1,000")
    if not addr:            return err("Enter withdrawal address")
    if net not in NETWORKS: return err("Invalid network")

    with get_db() as db:
        u = db.execute("SELECT pin_hash FROM users WHERE id=?", (uid,)).fetchone()
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()

        if not a or a["balance"] < amt: return err("Insufficient balance")
        if u["pin_hash"] and u["pin_hash"] != _hash(pin): return err("Invalid PIN", 403)

        bid = ""; status = "PENDING"
        if bnb:
            try:
                result = bnb.withdraw(
                    coin="USDT", address=addr, amount=amt,
                    network=NETWORKS[net]["network"], name="Summit"
                )
                bid    = result.get("id", "")
                status = "COMPLETED"
            except Exception as e:
                return err(f"Binance error: {str(e)}", 502)

        db.execute(
            "UPDATE accounts SET balance=balance-?,equity=equity-? WHERE user_id=?",
            (amt, amt, uid)
        )
        ref = "WD-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,binance_tx_id,deposit_address,created_at,completed_at) "
            "VALUES(?,?,?,'WITHDRAWAL',?,?,?,?,?,?,?,?)",
            (_uid(), uid, a["id"], net, amt, ref, status, bid,
             addr, _now(), _now() if status == "COMPLETED" else None)
        )
        db.commit()

    msg = "Withdrawal processed via Binance." if bid else "Withdrawal submitted. Awaiting admin processing."
    return ok({"reference": ref, "binance_id": bid, "message": msg})

# ── ADMIN API ─────────────────────────────────────────────────────────────────
@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    with get_db() as db:
        clients     = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='client'").fetchone()["c"]
        deposits    = db.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type='DEPOSIT' AND status='COMPLETED'").fetchone()["s"]
        withdrawals = db.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type='WITHDRAWAL' AND status='COMPLETED'").fetchone()["s"]
        pending_dep = db.execute("SELECT COUNT(*) AS c FROM transactions WHERE type='DEPOSIT' AND status='PENDING'").fetchone()["c"]
        open_trades = db.execute("SELECT COUNT(*) AS c FROM trades WHERE status='OPEN'").fetchone()["c"]
        total_refs  = db.execute("SELECT COUNT(*) AS c FROM referrals").fetchone()["c"]
        ref_paid    = db.execute("SELECT COALESCE(SUM(commission_usd),0) AS s FROM referrals").fetchone()["s"]
    return ok({
        "clients":          clients,
        "deposits":         deposits,
        "withdrawals":      withdrawals,
        "pending_deposits": pending_dep,
        "open_trades":      open_trades,
        "total_referrals":  total_refs,
        "ref_commissions":  ref_paid,
    })

@app.route("/api/admin/transactions")
@admin_required
def admin_transactions():
    with get_db() as db:
        txs = db.execute(
            "SELECT t.*, u.name AS user_name, u.email AS user_email "
            "FROM transactions t JOIN users u ON t.user_id=u.id "
            "ORDER BY t.created_at DESC"
        ).fetchall()
    return ok([dict(t) for t in txs])

@app.route("/api/admin/deposit/approve", methods=["POST"])
@admin_required
def admin_approve_deposit():
    d    = request.json or {}
    txid = d.get("tx_id", "").strip()
    if not txid: return err("tx_id required")

    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx:                    return err("Transaction not found")
        if tx["type"] != "DEPOSIT":   return err("Not a deposit")
        if tx["status"] != "PENDING": return err(f"Already {tx['status']}")

        db.execute(
            "UPDATE transactions SET status='COMPLETED',completed_at=? WHERE id=?",
            (_now(), txid)
        )
        db.execute(
            "UPDATE accounts SET balance=balance+?,equity=equity+? WHERE user_id=?",
            (tx["amount_usd"], tx["amount_usd"], tx["user_id"])
        )
        db.commit()

    threading.Thread(
        target=process_referral_commission,
        args=(txid, tx["user_id"], tx["amount_usd"]),
        daemon=True
    ).start()

    return ok({"message": f"Deposit of ${tx['amount_usd']:,.2f} approved"})

@app.route("/api/admin/deposit/reject", methods=["POST"])
@admin_required
def admin_reject_deposit():
    d      = request.json or {}
    txid   = d.get("tx_id", "").strip()
    reason = d.get("reason", "Rejected by admin")
    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx or tx["status"] != "PENDING": return err("Pending transaction not found")
        db.execute(
            "UPDATE transactions SET status='REJECTED',note=?,completed_at=? WHERE id=?",
            (reason, _now(), txid)
        )
        db.commit()
    return ok({"message": "Deposit rejected"})

@app.route("/api/admin/withdrawal/approve", methods=["POST"])
@admin_required
def admin_approve_withdrawal():
    d    = request.json or {}
    txid = d.get("tx_id", "").strip()
    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx or tx["type"] != "WITHDRAWAL" or tx["status"] != "PENDING":
            return err("Pending withdrawal not found")
        db.execute(
            "UPDATE transactions SET status='COMPLETED',completed_at=? WHERE id=?",
            (_now(), txid)
        )
        db.commit()
    return ok({"message": "Withdrawal marked complete"})

@app.route("/api/admin/clients")
@admin_required
def admin_clients():
    with get_db() as db:
        clients = db.execute(
            "SELECT u.id, u.name, u.email, u.phone, u.referral_code, u.created_at, "
            "a.balance, a.equity, a.ref_balance, "
            "(SELECT COUNT(*) FROM referrals WHERE referrer_id=u.id) AS ref_count "
            "FROM users u LEFT JOIN accounts a ON u.id=a.user_id "
            "WHERE u.role='client' ORDER BY u.created_at DESC"
        ).fetchall()
    return ok([dict(c) for c in clients])

@app.route("/api/admin/client/<uid>/adjust", methods=["POST"])
@admin_required
def admin_adjust_balance(uid):
    d      = request.json or {}
    amount = float(d.get("amount", 0))
    note   = d.get("note", "Admin adjustment")
    if amount == 0: return err("Amount cannot be zero")
    with get_db() as db:
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if not a: return err("Account not found")
        db.execute(
            "UPDATE accounts SET balance=balance+?,equity=equity+? WHERE user_id=?",
            (amount, amount, uid)
        )
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,created_at,completed_at) "
            "VALUES(?,?,?,'ADJUSTMENT','MANUAL',?,?,'COMPLETED',?,?,?)",
            (_uid(), uid, a["id"], abs(amount),
             "ADJ-" + secrets.token_hex(4).upper(), note, _now(), _now())
        )
        db.commit()
    return ok({"message": f"Balance adjusted by {amount:+.2f}"})

@app.route("/api/admin/referrals")
@admin_required
def admin_referrals():
    with get_db() as db:
        refs = db.execute(
            "SELECT r.*, "
            "u1.name AS referrer_name, u1.email AS referrer_email, "
            "u2.name AS referred_name, u2.email AS referred_email "
            "FROM referrals r "
            "JOIN users u1 ON r.referrer_id=u1.id "
            "JOIN users u2 ON r.referred_id=u2.id "
            "ORDER BY r.created_at DESC"
        ).fetchall()
    return ok([dict(r) for r in refs])

@app.route("/api/admin/trades")
@admin_required
def admin_trades():
    status = request.args.get("status", "")
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT t.*, u.name AS user_name FROM trades t "
                "JOIN users u ON t.user_id=u.id WHERE t.status=? ORDER BY t.opened_at DESC",
                (status.upper(),)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT t.*, u.name AS user_name FROM trades t "
                "JOIN users u ON t.user_id=u.id ORDER BY t.opened_at DESC"
            ).fetchall()
    return ok([dict(r) for r in rows])

# ── INIT & ENTRY POINT ────────────────────────────────────────────────────────
init_db()  # Always runs — works with both gunicorn and python app.py

if __name__ == "__main__":
    _bot_stop   = threading.Event()
    _bot_thread = threading.Thread(
        target=trade_bot_loop, args=(_bot_stop,),
        daemon=True, name="TradeBotThread"
    )
    _bot_thread.start()

    print("\n" + "="*55)
    print("   Summit Wealth v5 — BINANCE + REFERRAL EDITION")
    print("="*55)
    print(f"   http://127.0.0.1:8080")
    print(f"   Client : john@test.com  / demo1234")
    print(f"   Admin  : admin@test.com / admin1234")
    print(f"   Binance: {'CONNECTED ✓' if bnb else 'NOT configured (set BINANCE_API_KEY)'}")
    print(f"   Referral: 20% commission, paid as USDT via Binance")
    print("="*55 + "\n")

    try:
        app.run(debug=False, port=8080, host="0.0.0.0")
    finally:
        _bot_stop.set()
        _bot_thread.join(timeout=5)
        print("Stopped.")
