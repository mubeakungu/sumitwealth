"""
Summit Wealth v5.4 - $8 PROFIT PER $100 BALANCE (8% daily)
- Scheduler starts at module level (works with gunicorn on Render)
- One controlled trade per client per day
- Trade profit scales: $8 per $100 of balance
- Realistic trade using real Binance prices
- Referral system: 16% commission
- Manual wallet for deposits
- Min deposit: $100
- Withdrawal deducted only on admin approval
- Database: PostgreSQL (psycopg2)
"""

import os, hashlib, secrets, datetime, uuid, logging, threading, random
import psycopg2
import psycopg2.extras
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

def make_binance_client():
    if not (BINANCE_AVAILABLE and api_key and api_secret):
        log.warning("Binance: API keys not configured")
        return None
    try:
        client = Client(api_key, api_secret)
        log.info("Binance connected ✓")
        return client
    except Exception as e:
        log.warning(f"Binance connection failed: {e}")
        return None

bnb = make_binance_client()

# ── CONFIG ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set! Add it in Render → your web service → Environment.")

# $8 profit per $100 balance — this is the rate, not a flat fee
DAILY_PROFIT_PER_100 = float(os.environ.get("DAILY_PROFIT_USD", "8.0"))
MIN_BALANCE          = float(os.environ.get("MIN_BALANCE", "100.0"))
TRADE_HOUR           = int(os.environ.get("TRADE_HOUR", "5"))   # 5am UTC = 8am EAT
TRADE_SYMBOL         = os.environ.get("TRADE_SYMBOL", "BTCUSDT")
CHECK_INTERVAL       = 60   # seconds between scheduler ticks

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

REFERRAL_COMMISSION_PCT = 0.16
REFERRAL_MIN_DEPOSIT    = 100.0

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    """Return a new PostgreSQL connection with dict-like row access."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
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
CREATE TABLE IF NOT EXISTS daily_trade_log (
    id         TEXT PRIMARY KEY,
    user_id    TEXT,
    account_id TEXT,
    trade_id   TEXT,
    profit     REAL,
    date       TEXT,
    created_at TEXT
);
""")
    conn.commit()

    # Safe migrations — add columns if they don't exist
    for col, tbl, defval in [
        ("referral_code", "users",    "''"),
        ("referred_by",   "users",    "NULL"),
        ("ref_balance",   "accounts", "0"),
    ]:
        try:
            cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {defval}")
        except Exception:
            conn.rollback()

    conn.commit()

    # Assign referral codes to users missing one
    cur.execute("SELECT id FROM users WHERE referral_code IS NULL OR referral_code=''")
    users = cur.fetchall()
    for u in users:
        cur.execute(
            "UPDATE users SET referral_code=%s WHERE id=%s",
            (secrets.token_hex(4).upper(), u["id"])
        )
    conn.commit()

    # Create demo users if no users exist
    cur.execute("SELECT 1 FROM users LIMIT 1")
    if not cur.fetchone():
        uid  = str(uuid.uuid4())
        uid2 = str(uuid.uuid4())
        aid2 = str(uuid.uuid4())
        now  = datetime.datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO users(id,name,email,password_hash,pin_hash,role,"
            "referral_code,created_at) VALUES(%s,%s,%s,%s,%s,'admin',%s,%s)",
            (uid, "Admin", "admin@test.com",
             hashlib.sha256(b"admin1234").hexdigest(),
             hashlib.sha256(b"000000").hexdigest(),
             secrets.token_hex(4).upper(), now)
        )
        cur.execute(
            "INSERT INTO users(id,name,email,password_hash,pin_hash,role,"
            "referral_code,created_at) VALUES(%s,%s,%s,%s,%s,'client',%s,%s)",
            (uid2, "John Trader", "john@test.com",
             hashlib.sha256(b"demo1234").hexdigest(),
             hashlib.sha256(b"123456").hexdigest(),
             secrets.token_hex(4).upper(), now)
        )
        cur.execute(
            "INSERT INTO accounts(id,user_id,balance,equity,ref_balance,created_at) "
            "VALUES(%s,%s,1000,1000,0,%s)",
            (aid2, uid2, now)
        )
        conn.commit()
        log.info("Demo users created: john@test.com / demo1234  |  admin@test.com / admin1234")

    cur.close()
    conn.close()
    log.info("Database initialised ✓")

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _hash(s):  return hashlib.sha256(str(s).encode()).hexdigest()
def _uid():    return str(uuid.uuid4())
def _now():    return datetime.datetime.utcnow().isoformat()
def _today():  return datetime.datetime.utcnow().strftime("%Y-%m-%d")

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

# ── DAILY TRADE ENGINE ────────────────────────────────────────────────────────
def get_live_price(symbol):
    if bnb:
        try:
            ticker = bnb.get_symbol_ticker(symbol=symbol)
            price = float(ticker["price"])
            log.info(f"Live Binance price {symbol}: ${price:,.2f}")
            return price
        except Exception as e:
            log.warning(f"Could not get live price for {symbol}: {e}")
    fallback = {"BTCUSDT": 67500.0, "ETHUSDT": 3450.0, "BNBUSDT": 580.0}
    price = fallback.get(symbol, 100.0)
    log.info(f"Using fallback price {symbol}: ${price:,.2f}")
    return price

def run_daily_trades():
    today = _today()
    log.info(f"=== Daily trade run: {today} — ${DAILY_PROFIT_PER_100} profit per $100 balance ===")

    price = get_live_price(TRADE_SYMBOL)
    pct_gain    = random.uniform(0.003, 0.005)
    close_price = round(price * (1 + pct_gain), 2)
    price_diff  = close_price - price

    if price_diff <= 0:
        log.error("Price diff is zero — cannot compute quantity. Aborting.")
        return

    log.info(f"  Entry: ${price:,.2f} | Close: ${close_price:,.2f} | "
             f"Rate: ${DAILY_PROFIT_PER_100} per $100 balance")

    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT u.id, u.name, a.id AS account_id, a.balance "
        "FROM users u JOIN accounts a ON u.id=a.user_id "
        "WHERE u.role='client' AND a.balance >= %s",
        (MIN_BALANCE,)
    )
    clients = cur.fetchall()
    log.info(f"  Eligible clients: {len(clients)}")
    paid = 0

    for c in clients:
        cur.execute(
            "SELECT id FROM daily_trade_log WHERE user_id=%s AND date=%s",
            (c["id"], today)
        )
        if cur.fetchone():
            log.info(f"  Skipping {c['name']} — already traded today")
            continue

        # Scale profit: $8 per $100 of balance
        client_profit   = round((c["balance"] / 100.0) * DAILY_PROFIT_PER_100, 2)
        client_quantity = round(client_profit / price_diff, 6)

        now = datetime.datetime.utcnow()
        open_minutes_ago = random.randint(30, 90)
        opened_at = (now - datetime.timedelta(minutes=open_minutes_ago)).isoformat()
        closed_at = now.isoformat()
        trade_id  = _uid()
        sl = round(price * 0.985, 2)
        tp = close_price

        cur.execute(
            "INSERT INTO trades(id,user_id,account_id,symbol,direction,"
            "entry_price,quantity,stop_loss,take_profit,close_price,"
            "pnl,status,close_reason,opened_at,closed_at) "
            "VALUES(%s,%s,%s,%s,'BUY',%s,%s,%s,%s,%s,%s,'CLOSED','TAKE_PROFIT',%s,%s)",
            (trade_id, c["id"], c["account_id"],
             TRADE_SYMBOL, price, client_quantity, sl, tp,
             close_price, client_profit, opened_at, closed_at)
        )
        cur.execute(
            "UPDATE accounts SET balance=balance+%s, equity=equity+%s WHERE user_id=%s",
            (client_profit, client_profit, c["id"])
        )
        cur.execute(
            "INSERT INTO daily_trade_log(id,user_id,account_id,trade_id,"
            "profit,date,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (_uid(), c["id"], c["account_id"],
             trade_id, client_profit, today, _now())
        )
        paid += 1
        log.info(f"  ✓ {c['name']}: BUY {client_quantity} {TRADE_SYMBOL} "
                 f"@ ${price:,.2f} → ${close_price:,.2f} = +${client_profit} "
                 f"(balance: ${c['balance']:,.2f})")

    conn.commit()
    cur.close()
    conn.close()
    log.info(f"=== Daily trade complete: {paid}/{len(clients)} clients credited ===")

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
_scheduler_lock    = threading.Lock()
_scheduler_started = False
_scheduler_stop    = threading.Event()

def trade_scheduler(stop_event):
    log.info(
        f"Trade scheduler started — fires daily at "
        f"{TRADE_HOUR:02d}:00 UTC ({TRADE_HOUR+3:02d}:00 EAT)"
    )
    last_run_date = None
    while not stop_event.is_set():
        try:
            now   = datetime.datetime.utcnow()
            today = now.strftime("%Y-%m-%d")
            if now.hour == TRADE_HOUR and last_run_date != today:
                last_run_date = today
                try:
                    run_daily_trades()
                except Exception as e:
                    log.error(f"Trade run error: {e}")
        except Exception as e:
            log.error(f"Scheduler tick error: {e}")
        stop_event.wait(CHECK_INTERVAL)
    log.info("Trade scheduler stopped")

def start_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        t = threading.Thread(
            target=trade_scheduler,
            args=(_scheduler_stop,),
            daemon=True,
            name="TradeScheduler"
        )
        t.start()
        log.info("TradeScheduler thread launched ✓")

# ── REFERRAL ENGINE ───────────────────────────────────────────────────────────
def process_referral_commission(tx_id, user_id, amount_usd):
    if amount_usd < REFERRAL_MIN_DEPOSIT:
        return
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    if not user or not user["referred_by"]:
        cur.close(); conn.close(); return

    cur.execute("SELECT id FROM referrals WHERE referred_id=%s", (user_id,))
    if cur.fetchone():
        cur.close(); conn.close(); return

    cur.execute("SELECT * FROM users WHERE id=%s", (user["referred_by"],))
    referrer = cur.fetchone()
    if not referrer:
        cur.close(); conn.close(); return

    commission = round(amount_usd * REFERRAL_COMMISSION_PCT, 2)
    cur.execute(
        "INSERT INTO referrals(id,referrer_id,referred_id,commission_usd,"
        "status,triggered_by,created_at) VALUES(%s,%s,%s,%s,'CREDITED',%s,%s)",
        (_uid(), referrer["id"], user_id, commission, tx_id, _now())
    )
    cur.execute(
        "UPDATE accounts SET ref_balance=ref_balance+%s WHERE user_id=%s",
        (commission, referrer["id"])
    )
    conn.commit()
    cur.close()
    conn.close()
    log.info(f"Referral commission: {referrer['name']} +${commission}")

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
    name  = d.get("name","").strip()
    email = d.get("email","").lower().strip()
    phone = d.get("phone","").strip()
    pw    = d.get("password","")
    pin   = d.get("pin","000000")
    ref   = d.get("ref_code","").strip().upper()

    if not name or not email or len(pw) < 6:
        return err("Name, email and password (min 6 chars) required")
    if len(pin) != 6 or not pin.isdigit():
        return err("PIN must be exactly 6 digits")

    conn = get_db()
    cur  = conn.cursor()
    try:
        referred_by = None
        if ref:
            cur.execute("SELECT id FROM users WHERE referral_code=%s", (ref,))
            referrer = cur.fetchone()
            if referrer: referred_by = referrer["id"]

        uid, aid, now = _uid(), _uid(), _now()
        cur.execute(
            "INSERT INTO users(id,name,email,phone,password_hash,pin_hash,"
            "role,referral_code,referred_by,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'client',%s,%s,%s)",
            (uid, name, email, phone, _hash(pw), _hash(pin),
             secrets.token_hex(4).upper(), referred_by, now)
        )
        cur.execute(
            "INSERT INTO accounts(id,user_id,balance,equity,ref_balance,"
            "created_at) VALUES(%s,%s,0,0,0,%s)",
            (aid, uid, now)
        )
        conn.commit()
        session["user_id"] = uid
        session["role"]    = "client"
        return ok({"redirect": "/dashboard"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return err("Email already registered", 409)
    finally:
        cur.close()
        conn.close()

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d     = request.json or {}
    email = d.get("email","").lower().strip()
    pw    = d.get("password","")
    admin = d.get("admin", False)

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=%s", (email,))
    u = cur.fetchone()
    cur.close()
    conn.close()

    if not u or u["password_hash"] != _hash(pw):
        return err("Invalid credentials", 401)
    if admin and u["role"] != "admin":
        return err("Not an admin account", 403)
    if not admin and u["role"] == "admin":
        return err("Please use admin login", 403)

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
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT * FROM accounts WHERE user_id=%s", (uid,))
    a = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
        "WHERE user_id=%s AND type='DEPOSIT' AND status='COMPLETED'", (uid,)
    )
    dep = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
        "WHERE user_id=%s AND type='WITHDRAWAL' AND status='COMPLETED'", (uid,)
    )
    wdr = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s", (uid,))
    ref_count = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(commission_usd),0) AS s FROM referrals WHERE referrer_id=%s", (uid,)
    )
    ref_earned = cur.fetchone()
    cur.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE user_id=%s AND status='OPEN'", (uid,)
    )
    open_trades = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(SUM(profit),0) AS s FROM daily_trade_log WHERE user_id=%s", (uid,)
    )
    total_profit = cur.fetchone()
    cur.execute(
        "SELECT COUNT(*) AS c FROM daily_trade_log WHERE user_id=%s", (uid,)
    )
    days_traded = cur.fetchone()

    cur.close()
    conn.close()

    balance = a["balance"] if a else 0
    expected_daily = round((balance / 100.0) * DAILY_PROFIT_PER_100, 2)

    return ok({
        "name":              u["name"],
        "balance":           balance,
        "equity":            a["equity"]        if a else 0,
        "total_deposits":    dep["s"],
        "total_withdrawals": wdr["s"],
        "ref_balance":       a["ref_balance"]   if a else 0,
        "ref_code":          u["referral_code"] or "",
        "ref_count":         ref_count["c"],
        "ref_earned":        ref_earned["s"],
        "open_trades":       open_trades["c"],
        "total_profit":      total_profit["s"],
        "days_traded":       days_traded["c"],
        "daily_profit":      expected_daily,
        "daily_profit_rate": DAILY_PROFIT_PER_100,
    })

@app.route("/api/client/referrals")
@login_required
def client_referrals():
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT r.*, u.name AS referred_name, u.email AS referred_email "
        "FROM referrals r JOIN users u ON r.referred_id=u.id "
        "WHERE r.referrer_id=%s ORDER BY r.created_at DESC", (uid,)
    )
    refs = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(r) for r in refs])

@app.route("/api/client/referral/withdraw", methods=["POST"])
@login_required
def client_referral_withdraw():
    d    = request.json or {}
    uid  = session["user_id"]
    pin  = d.get("pin","")
    addr = d.get("address","").strip()
    net  = d.get("network","TRC20").upper()

    if not addr:            return err("Enter your USDT wallet address")
    if net not in NETWORKS: return err("Invalid network")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT * FROM accounts WHERE user_id=%s", (uid,))
    a = cur.fetchone()

    if u["pin_hash"] and u["pin_hash"] != _hash(pin):
        cur.close(); conn.close()
        return err("Invalid PIN", 403)

    ref_bal = a["ref_balance"] if a else 0
    if ref_bal < 16:
        cur.close(); conn.close()
        return err("Minimum referral withdrawal is $16")

    cur.execute("UPDATE accounts SET ref_balance=0 WHERE user_id=%s", (uid,))
    ref = "REF-" + secrets.token_hex(4).upper()
    cur.execute(
        "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
        "reference,status,note,deposit_address,created_at) "
        "VALUES(%s,%s,%s,'REFERRAL_WITHDRAWAL',%s,%s,%s,'PENDING',%s,%s,%s)",
        (_uid(), uid, a["id"], net, ref_bal, ref,
         f"Referral to {addr[:20]}...", addr, _now())
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"reference": ref,
               "message": "Withdrawal submitted. Admin will process within 24hrs."})

@app.route("/api/client/transactions")
@login_required
def client_transactions():
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id=%s ORDER BY created_at DESC", (uid,)
    )
    txs = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(t) for t in txs])

@app.route("/api/client/trades")
@login_required
def client_trades():
    uid    = session["user_id"]
    status = request.args.get("status","")
    conn   = get_db()
    cur    = conn.cursor()
    if status:
        cur.execute(
            "SELECT * FROM trades WHERE user_id=%s AND status=%s "
            "ORDER BY opened_at DESC LIMIT 50",
            (uid, status.upper())
        )
    else:
        cur.execute(
            "SELECT * FROM trades WHERE user_id=%s ORDER BY opened_at DESC LIMIT 50",
            (uid,)
        )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(r) for r in rows])

@app.route("/api/client/profit/history")
@login_required
def client_profit_history():
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM daily_trade_log WHERE user_id=%s "
        "ORDER BY date DESC LIMIT 30", (uid,)
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(r) for r in rows])

# ── DEPOSIT ───────────────────────────────────────────────────────────────────
@app.route("/api/client/deposit/address")
@login_required
def client_deposit_address():
    net = request.args.get("network","TRC20").upper()
    if net not in NETWORKS: return err("Invalid network")
    wallet = MANUAL_WALLETS.get(net,"")
    if not wallet: return err("Deposit address not configured. Contact support.")
    return ok({"address": wallet, "network": net, "mode": "manual"})

@app.route("/api/client/deposit/pending", methods=["POST"])
@login_required
def client_deposit_pending():
    d    = request.json or {}
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network","TRC20").upper()
    addr = d.get("address","").strip()

    if amt < 100: return err("Minimum deposit is $100")
    if not addr:  return err("Deposit address is required")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT id FROM accounts WHERE user_id=%s", (uid,))
    acct = cur.fetchone()
    ref  = "SWC-" + secrets.token_hex(4).upper()
    cur.execute(
        "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
        "reference,status,deposit_address,created_at) "
        "VALUES(%s,%s,%s,'DEPOSIT',%s,%s,%s,'PENDING',%s,%s)",
        (_uid(), uid, acct["id"] if acct else None, net, amt, ref, addr, _now())
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"reference": ref,
               "message": "Deposit submitted. Awaiting admin confirmation."})

# ── WITHDRAWAL ────────────────────────────────────────────────────────────────
@app.route("/api/client/withdraw", methods=["POST"])
@login_required
def client_withdraw():
    d    = request.json or {}
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network","TRC20").upper()
    addr = d.get("address","").strip()
    pin  = d.get("pin","")

    if amt < 1000:          return err("Minimum withdrawal is $1,000")
    if not addr:            return err("Enter withdrawal address")
    if net not in NETWORKS: return err("Invalid network")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT pin_hash FROM users WHERE id=%s", (uid,))
    u = cur.fetchone()
    cur.execute("SELECT * FROM accounts WHERE user_id=%s", (uid,))
    a = cur.fetchone()

    if not a or a["balance"] < amt:
        cur.close(); conn.close()
        return err("Insufficient balance")
    if u["pin_hash"] and u["pin_hash"] != _hash(pin):
        cur.close(); conn.close()
        return err("Invalid PIN", 403)

    ref = "WD-" + secrets.token_hex(4).upper()
    cur.execute(
        "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
        "reference,status,deposit_address,created_at) "
        "VALUES(%s,%s,%s,'WITHDRAWAL',%s,%s,%s,'PENDING',%s,%s)",
        (_uid(), uid, a["id"], net, amt, ref, addr, _now())
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"reference": ref,
               "message": "Withdrawal submitted. Admin will process within 24hrs."})

# ── ADMIN API ─────────────────────────────────────────────────────────────────
@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE role='client'")
    clients = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE balance >= %s", (MIN_BALANCE,))
    active = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type='DEPOSIT' AND status='COMPLETED'")
    deposits = cur.fetchone()["s"]
    cur.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type='WITHDRAWAL' AND status='COMPLETED'")
    withdrawals = cur.fetchone()["s"]
    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE type='DEPOSIT' AND status='PENDING'")
    pending_dep = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM transactions WHERE type IN ('WITHDRAWAL','REFERRAL_WITHDRAWAL') AND status='PENDING'")
    pending_wdr = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM referrals")
    total_refs = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(commission_usd),0) AS s FROM referrals")
    ref_paid = cur.fetchone()["s"]
    cur.execute("SELECT COALESCE(SUM(profit),0) AS s FROM daily_trade_log")
    profit_paid = cur.fetchone()["s"]
    cur.execute("SELECT COUNT(*) AS c FROM daily_trade_log WHERE date=%s", (_today(),))
    trades_today = cur.fetchone()["c"]
    cur.close(); conn.close()

    return ok({
        "clients":             clients,
        "active_clients":      active,
        "deposits":            deposits,
        "withdrawals":         withdrawals,
        "pending_deposits":    pending_dep,
        "pending_withdrawals": pending_wdr,
        "total_referrals":     total_refs,
        "ref_commissions":     ref_paid,
        "total_profit_paid":   profit_paid,
        "trades_today":        trades_today,
        "daily_profit_rate":   DAILY_PROFIT_PER_100,
        "trade_symbol":        TRADE_SYMBOL,
        "scheduler_running":   _scheduler_started,
    })

@app.route("/api/admin/transactions")
@admin_required
def admin_transactions():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT t.*, u.name AS user_name, u.email AS user_email "
        "FROM transactions t JOIN users u ON t.user_id=u.id "
        "ORDER BY t.created_at DESC"
    )
    txs = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(t) for t in txs])

@app.route("/api/admin/deposit/approve", methods=["POST"])
@admin_required
def admin_approve_deposit():
    d    = request.json or {}
    txid = d.get("tx_id","").strip()
    if not txid: return err("tx_id required")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=%s", (txid,))
    tx = cur.fetchone()
    if not tx:                    cur.close(); conn.close(); return err("Transaction not found")
    if tx["type"] != "DEPOSIT":   cur.close(); conn.close(); return err("Not a deposit")
    if tx["status"] != "PENDING": cur.close(); conn.close(); return err(f"Already {tx['status']}")

    cur.execute(
        "UPDATE transactions SET status='COMPLETED',completed_at=%s WHERE id=%s",
        (_now(), txid)
    )
    cur.execute(
        "UPDATE accounts SET balance=balance+%s,equity=equity+%s WHERE user_id=%s",
        (tx["amount_usd"], tx["amount_usd"], tx["user_id"])
    )
    conn.commit()
    cur.close(); conn.close()

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
    txid   = d.get("tx_id","").strip()
    reason = d.get("reason","Rejected by admin")
    conn   = get_db()
    cur    = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=%s", (txid,))
    tx = cur.fetchone()
    if not tx or tx["status"] != "PENDING":
        cur.close(); conn.close()
        return err("Pending transaction not found")
    cur.execute(
        "UPDATE transactions SET status='REJECTED',note=%s,completed_at=%s WHERE id=%s",
        (reason, _now(), txid)
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"message": "Deposit rejected"})

@app.route("/api/admin/withdrawal/approve", methods=["POST"])
@admin_required
def admin_approve_withdrawal():
    d    = request.json or {}
    txid = d.get("tx_id","").strip()
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=%s", (txid,))
    tx = cur.fetchone()
    if (not tx
            or tx["type"] not in ("WITHDRAWAL","REFERRAL_WITHDRAWAL")
            or tx["status"] != "PENDING"):
        cur.close(); conn.close()
        return err("Pending withdrawal not found")
    
    # Only deduct from balance for regular WITHDRAWAL
    # REFERRAL_WITHDRAWAL already had ref_balance zeroed when requested
    if tx["type"] == "WITHDRAWAL":
        cur.execute(
            "UPDATE accounts SET balance=balance-%s,equity=equity-%s WHERE user_id=%s",
            (tx["amount_usd"], tx["amount_usd"], tx["user_id"])
        )
    
    cur.execute(
        "UPDATE transactions SET status='COMPLETED',completed_at=%s WHERE id=%s",
        (_now(), txid)
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"message": "Withdrawal marked complete"})

@app.route("/api/admin/withdrawal/reject", methods=["POST"])
@admin_required
def admin_reject_withdrawal():
    d      = request.json or {}
    txid   = d.get("tx_id","").strip()
    reason = d.get("reason","Rejected by admin")
    conn   = get_db()
    cur    = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=%s", (txid,))
    tx = cur.fetchone()
    if (not tx
            or tx["type"] not in ("WITHDRAWAL","REFERRAL_WITHDRAWAL")
            or tx["status"] != "PENDING"):
        cur.close(); conn.close()
        return err("Pending withdrawal not found")
    cur.execute(
        "UPDATE transactions SET status='REJECTED',note=%s,completed_at=%s WHERE id=%s",
        (reason, _now(), txid)
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"message": "Withdrawal rejected — client balance unchanged"})

@app.route("/api/admin/clients")
@admin_required
def admin_clients():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT u.id, u.name, u.email, u.phone, u.referral_code, u.created_at, "
        "a.balance, a.equity, a.ref_balance, "
        "(SELECT COUNT(*) FROM referrals WHERE referrer_id=u.id) AS ref_count, "
        "(SELECT COALESCE(SUM(profit),0) FROM daily_trade_log WHERE user_id=u.id) AS total_profit, "
        "(SELECT COUNT(*) FROM daily_trade_log WHERE user_id=u.id) AS days_active "
        "FROM users u LEFT JOIN accounts a ON u.id=a.user_id "
        "WHERE u.role='client' ORDER BY u.created_at DESC"
    )
    clients = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(c) for c in clients])

@app.route("/api/admin/client/<uid>/adjust", methods=["POST"])
@admin_required
def admin_adjust_balance(uid):
    d      = request.json or {}
    amount = float(d.get("amount", 0))
    note   = d.get("note","Admin adjustment")
    if amount == 0: return err("Amount cannot be zero")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE user_id=%s", (uid,))
    a = cur.fetchone()
    if not a:
        cur.close(); conn.close()
        return err("Account not found")

    cur.execute(
        "UPDATE accounts SET balance=balance+%s,equity=equity+%s WHERE user_id=%s",
        (amount, amount, uid)
    )
    cur.execute(
        "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
        "reference,status,note,created_at,completed_at) "
        "VALUES(%s,%s,%s,'ADJUSTMENT','MANUAL',%s,%s,'COMPLETED',%s,%s,%s)",
        (_uid(), uid, a["id"], abs(amount),
         "ADJ-"+secrets.token_hex(4).upper(), note, _now(), _now())
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"message": f"Balance adjusted by {amount:+.2f}"})

@app.route("/api/admin/trade/run", methods=["POST"])
@admin_required
def admin_run_trades():
    threading.Thread(target=run_daily_trades, daemon=True).start()
    return ok({"message": f"Daily trades triggered — ${DAILY_PROFIT_PER_100} per $100 balance per active client"})

@app.route("/api/admin/trade/log")
@admin_required
def admin_trade_log():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT d.*, u.name AS user_name FROM daily_trade_log d "
        "JOIN users u ON d.user_id=u.id ORDER BY d.created_at DESC LIMIT 100"
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(r) for r in rows])

@app.route("/api/admin/trades")
@admin_required
def admin_trades():
    status = request.args.get("status", "").upper()
    conn   = get_db()
    cur    = conn.cursor()
    if status:
        cur.execute(
            "SELECT t.*, u.name AS user_name FROM trades t "
            "JOIN users u ON t.user_id=u.id "
            "WHERE t.status=%s ORDER BY t.opened_at DESC",
            (status,)
        )
    else:
        cur.execute(
            "SELECT t.*, u.name AS user_name FROM trades t "
            "JOIN users u ON t.user_id=u.id ORDER BY t.opened_at DESC"
        )
    trades = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(t) for t in trades])

@app.route("/api/admin/referrals")
@admin_required
def admin_referrals():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT r.*, u1.name AS referrer_name, u1.email AS referrer_email, "
        "u2.name AS referred_name, u2.email AS referred_email "
        "FROM referrals r JOIN users u1 ON r.referrer_id=u1.id "
        "JOIN users u2 ON r.referred_id=u2.id ORDER BY r.created_at DESC"
    )
    refs = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(r) for r in refs])

# ── SCHEDULER STATUS ──────────────────────────────────────────────────────────
@app.route("/api/admin/scheduler/status")
@admin_required
def scheduler_status():
    now      = datetime.datetime.utcnow()
    next_run = now.replace(hour=TRADE_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += datetime.timedelta(days=1)
    hours_left = round((next_run - now).total_seconds() / 3600, 1)
    return ok({
        "running":           _scheduler_started,
        "trade_hour_utc":    TRADE_HOUR,
        "trade_hour_eat":    TRADE_HOUR + 3,
        "next_run_utc":      next_run.isoformat(),
        "hours_until_run":   hours_left,
        "daily_profit_rate": DAILY_PROFIT_PER_100,
        "profit_basis":      "per $100 balance",
        "min_balance":       MIN_BALANCE,
        "symbol":            TRADE_SYMBOL,
    })

# ── STARTUP ───────────────────────────────────────────────────────────────────
init_db()
start_scheduler()

if __name__ == "__main__":
    print("\n" + "="*60)
    print("   Summit Wealth v5.4 — $8 PROFIT PER $100 BALANCE")
    print("="*60)
    print(f"   URL    : http://127.0.0.1:8080")
    print(f"   Client : john@test.com  / demo1234")
    print(f"   Admin  : admin@test.com / admin1234")
    print(f"   Rate   : ${DAILY_PROFIT_PER_100} per $100 balance/day at {TRADE_HOUR:02d}:00 UTC ({TRADE_HOUR+3:02d}:00 EAT)")
    print(f"   Symbol : {TRADE_SYMBOL}")
    print(f"   Min Dep: $100")
    print(f"   Binance: {'CONNECTED ✓' if bnb else 'fallback prices'}")
    print(f"   TRC20  : {'SET ✓' if MANUAL_WALLETS['TRC20'] else 'NOT SET ✗'}")
    print("="*60 + "\n")
    app.run(debug=False, port=8080, host="0.0.0.0")
