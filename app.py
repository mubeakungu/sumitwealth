"""
Summit Wealth v5.2 - ONE TRADE PER DAY = $8 PROFIT
- Scheduler starts at module level (works with gunicorn on Render)
- One controlled trade per client per day
- Trade targets exactly $8 profit then closes
- Realistic trade using real Binance prices
- Referral system: 20% commission
- Manual wallet for deposits
"""

import os, sqlite3, hashlib, secrets, datetime, uuid, logging, threading, random
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
DB_PATH             = os.path.join(os.path.dirname(__file__), "summit.db")
DAILY_PROFIT        = float(os.environ.get("DAILY_PROFIT_USD", "8.0"))
MIN_BALANCE         = float(os.environ.get("MIN_BALANCE", "100.0"))
TRADE_HOUR          = int(os.environ.get("TRADE_HOUR", "8"))   # 8am UTC = 11am EAT
TRADE_SYMBOL        = os.environ.get("TRADE_SYMBOL", "BTCUSDT")
CHECK_INTERVAL      = 60   # seconds between scheduler ticks

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
            except Exception:
                pass

        users = db.execute(
            "SELECT id FROM users WHERE referral_code IS NULL OR referral_code=''"
        ).fetchall()
        for u in users:
            db.execute(
                "UPDATE users SET referral_code=? WHERE id=?",
                (secrets.token_hex(4).upper(), u["id"])
            )
        db.commit()

        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            uid  = str(uuid.uuid4())
            uid2 = str(uuid.uuid4())
            aid2 = str(uuid.uuid4())
            now  = datetime.datetime.utcnow().isoformat()
            db.execute(
                "INSERT INTO users(id,name,email,password_hash,pin_hash,role,"
                "referral_code,created_at) VALUES(?,?,?,?,?,'admin',?,?)",
                (uid, "Admin", "admin@test.com",
                 hashlib.sha256(b"admin1234").hexdigest(),
                 hashlib.sha256(b"000000").hexdigest(),
                 secrets.token_hex(4).upper(), now)
            )
            db.execute(
                "INSERT INTO users(id,name,email,password_hash,pin_hash,role,"
                "referral_code,created_at) VALUES(?,?,?,?,?,'client',?,?)",
                (uid2, "John Trader", "john@test.com",
                 hashlib.sha256(b"demo1234").hexdigest(),
                 hashlib.sha256(b"123456").hexdigest(),
                 secrets.token_hex(4).upper(), now)
            )
            db.execute(
                "INSERT INTO accounts(id,user_id,balance,equity,ref_balance,created_at) "
                "VALUES(?,?,1000,1000,0,?)",
                (aid2, uid2, now)
            )
            db.commit()
            log.info("Demo users created: john@test.com / demo1234  |  admin@test.com / admin1234")

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
    """Get live price from Binance if available, else use realistic fallback."""
    if bnb:
        try:
            ticker = bnb.get_symbol_ticker(symbol=symbol)
            price = float(ticker["price"])
            log.info(f"Live Binance price {symbol}: ${price:,.2f}")
            return price
        except Exception as e:
            log.warning(f"Could not get live price for {symbol}: {e}")
    # Fallback prices when Binance unavailable
    fallback = {"BTCUSDT": 67500.0, "ETHUSDT": 3450.0, "BNBUSDT": 580.0}
    price = fallback.get(symbol, 100.0)
    log.info(f"Using fallback price {symbol}: ${price:,.2f}")
    return price

def run_daily_trades():
    """
    Run once per day automatically:
    - Open one BUY trade per active client (balance >= MIN_BALANCE)
    - Calculate quantity so that profit = exactly $8
    - Immediately close it as TAKE_PROFIT
    - Credit $8 to each client's balance
    """
    today = _today()
    log.info(f"=== Daily trade run: {today} — target ${DAILY_PROFIT} profit/client ===")

    # Get live or fallback price
    price = get_live_price(TRADE_SYMBOL)

    # Slightly vary close price (+0.3% to +0.5%) to look realistic
    pct_gain    = random.uniform(0.003, 0.005)
    close_price = round(price * (1 + pct_gain), 2)

    # quantity = desired_profit / price_difference
    price_diff = close_price - price
    if price_diff <= 0:
        log.error("Price diff is zero — cannot compute quantity. Aborting.")
        return
    quantity = round(DAILY_PROFIT / price_diff, 6)

    log.info(f"  Entry: ${price:,.2f} | Close: ${close_price:,.2f} | "
             f"Qty: {quantity} | Profit: ${DAILY_PROFIT}")

    with get_db() as db:
        clients = db.execute(
            "SELECT u.id, u.name, a.id AS account_id, a.balance "
            "FROM users u JOIN accounts a ON u.id=a.user_id "
            "WHERE u.role='client' AND a.balance >= ?",
            (MIN_BALANCE,)
        ).fetchall()

        log.info(f"  Eligible clients: {len(clients)}")
        paid = 0

        for c in clients:
            # Skip if already traded today
            already = db.execute(
                "SELECT id FROM daily_trade_log WHERE user_id=? AND date=?",
                (c["id"], today)
            ).fetchone()
            if already:
                log.info(f"  Skipping {c['name']} — already traded today")
                continue

            now = datetime.datetime.utcnow()
            # Trade opened 30-90 minutes ago, closed just now (realistic look)
            open_minutes_ago = random.randint(30, 90)
            opened_at = (now - datetime.timedelta(minutes=open_minutes_ago)).isoformat()
            closed_at = now.isoformat()

            trade_id = _uid()
            sl = round(price * 0.985, 2)   # stop loss 1.5% below entry
            tp = close_price

            # Insert the closed trade
            db.execute(
                "INSERT INTO trades(id,user_id,account_id,symbol,direction,"
                "entry_price,quantity,stop_loss,take_profit,close_price,"
                "pnl,status,close_reason,opened_at,closed_at) "
                "VALUES(?,?,?,?,'BUY',?,?,?,?,?,?,'CLOSED','TAKE_PROFIT',?,?)",
                (trade_id, c["id"], c["account_id"],
                 TRADE_SYMBOL, price, quantity, sl, tp,
                 close_price, DAILY_PROFIT,
                 opened_at, closed_at)
            )

            # Credit $8 to balance and equity
            db.execute(
                "UPDATE accounts SET balance=balance+?, equity=equity+? WHERE user_id=?",
                (DAILY_PROFIT, DAILY_PROFIT, c["id"])
            )

            # Log the daily trade
            db.execute(
                "INSERT INTO daily_trade_log(id,user_id,account_id,trade_id,"
                "profit,date,created_at) VALUES(?,?,?,?,?,?,?)",
                (_uid(), c["id"], c["account_id"],
                 trade_id, DAILY_PROFIT, today, _now())
            )

            paid += 1
            log.info(f"  ✓ {c['name']}: BUY {quantity} {TRADE_SYMBOL} "
                     f"@ ${price:,.2f} → ${close_price:,.2f} = +${DAILY_PROFIT}")

        db.commit()
        log.info(f"=== Daily trade complete: {paid}/{len(clients)} clients credited ${DAILY_PROFIT} ===")

# ── SCHEDULER (module-level — works with gunicorn) ────────────────────────────
_scheduler_lock      = threading.Lock()
_scheduler_started   = False
_scheduler_stop      = threading.Event()

def trade_scheduler(stop_event):
    """
    Background thread that fires run_daily_trades() once per day
    at TRADE_HOUR UTC. Safe to run under gunicorn workers.
    """
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
    """Start scheduler once — safe to call multiple times."""
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
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["referred_by"]: return
        already = db.execute(
            "SELECT id FROM referrals WHERE referred_id=?", (user_id,)
        ).fetchone()
        if already: return
        referrer = db.execute(
            "SELECT * FROM users WHERE id=?", (user["referred_by"],)
        ).fetchone()
        if not referrer: return
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

    try:
        with get_db() as db:
            referred_by = None
            if ref:
                referrer = db.execute(
                    "SELECT id FROM users WHERE referral_code=?", (ref,)
                ).fetchone()
                if referrer: referred_by = referrer["id"]

            uid, aid, now = _uid(), _uid(), _now()
            db.execute(
                "INSERT INTO users(id,name,email,phone,password_hash,pin_hash,"
                "role,referral_code,referred_by,created_at) "
                "VALUES(?,?,?,?,?,?,'client',?,?,?)",
                (uid, name, email, phone, _hash(pw), _hash(pin),
                 secrets.token_hex(4).upper(), referred_by, now)
            )
            db.execute(
                "INSERT INTO accounts(id,user_id,balance,equity,ref_balance,"
                "created_at) VALUES(?,?,0,0,0,?)",
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
    email = d.get("email","").lower().strip()
    pw    = d.get("password","")
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
        u  = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        a  = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
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
        total_profit = db.execute(
            "SELECT COALESCE(SUM(profit),0) AS s FROM daily_trade_log WHERE user_id=?", (uid,)
        ).fetchone()
        days_traded = db.execute(
            "SELECT COUNT(*) AS c FROM daily_trade_log WHERE user_id=?", (uid,)
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
        "total_profit":      total_profit["s"],
        "days_traded":       days_traded["c"],
        "daily_profit":      DAILY_PROFIT,
    })

@app.route("/api/client/referrals")
@login_required
def client_referrals():
    uid = session["user_id"]
    with get_db() as db:
        refs = db.execute(
            "SELECT r.*, u.name AS referred_name, u.email AS referred_email "
            "FROM referrals r JOIN users u ON r.referred_id=u.id "
            "WHERE r.referrer_id=? ORDER BY r.created_at DESC", (uid,)
        ).fetchall()
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

    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if u["pin_hash"] and u["pin_hash"] != _hash(pin):
            return err("Invalid PIN", 403)
        ref_bal = a["ref_balance"] if a else 0
        if ref_bal < 10: return err("Minimum referral withdrawal is $10")
        db.execute("UPDATE accounts SET ref_balance=0 WHERE user_id=?", (uid,))
        ref = "REF-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,deposit_address,created_at) "
            "VALUES(?,?,?,'REFERRAL_WITHDRAWAL',?,?,?,'PENDING',?,?,?)",
            (_uid(), uid, a["id"], net, ref_bal, ref,
             f"Referral to {addr[:20]}...", addr, _now())
        )
        db.commit()
    return ok({"reference": ref,
               "message": "Withdrawal submitted. Admin will process within 24hrs."})

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
    status = request.args.get("status","")
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM trades WHERE user_id=? AND status=? "
                "ORDER BY opened_at DESC LIMIT 50",
                (uid, status.upper())
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM trades WHERE user_id=? ORDER BY opened_at DESC LIMIT 50",
                (uid,)
            ).fetchall()
    return ok([dict(r) for r in rows])

@app.route("/api/client/profit/history")
@login_required
def client_profit_history():
    uid = session["user_id"]
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM daily_trade_log WHERE user_id=? "
            "ORDER BY date DESC LIMIT 30", (uid,)
        ).fetchall()
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
    with get_db() as db:
        u = db.execute("SELECT pin_hash FROM users WHERE id=?", (uid,)).fetchone()
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if not a or a["balance"] < amt: return err("Insufficient balance")
        if u["pin_hash"] and u["pin_hash"] != _hash(pin): return err("Invalid PIN", 403)
        db.execute(
            "UPDATE accounts SET balance=balance-?,equity=equity-? WHERE user_id=?",
            (amt, amt, uid)
        )
        ref = "WD-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,deposit_address,created_at) "
            "VALUES(?,?,?,'WITHDRAWAL',?,?,?,'PENDING',?,?)",
            (_uid(), uid, a["id"], net, amt, ref, addr, _now())
        )
        db.commit()
    return ok({"reference": ref,
               "message": "Withdrawal submitted. Admin will process within 24hrs."})

# ── ADMIN API ─────────────────────────────────────────────────────────────────
@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    with get_db() as db:
        clients     = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='client'").fetchone()["c"]
        active      = db.execute("SELECT COUNT(*) AS c FROM accounts WHERE balance >= ?", (MIN_BALANCE,)).fetchone()["c"]
        deposits    = db.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type='DEPOSIT' AND status='COMPLETED'").fetchone()["s"]
        withdrawals = db.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type='WITHDRAWAL' AND status='COMPLETED'").fetchone()["s"]
        pending_dep = db.execute("SELECT COUNT(*) AS c FROM transactions WHERE type='DEPOSIT' AND status='PENDING'").fetchone()["c"]
        pending_wdr = db.execute("SELECT COUNT(*) AS c FROM transactions WHERE type IN ('WITHDRAWAL','REFERRAL_WITHDRAWAL') AND status='PENDING'").fetchone()["c"]
        total_refs  = db.execute("SELECT COUNT(*) AS c FROM referrals").fetchone()["c"]
        ref_paid    = db.execute("SELECT COALESCE(SUM(commission_usd),0) AS s FROM referrals").fetchone()["s"]
        profit_paid = db.execute("SELECT COALESCE(SUM(profit),0) AS s FROM daily_trade_log").fetchone()["s"]
        trades_today = db.execute("SELECT COUNT(*) AS c FROM daily_trade_log WHERE date=?", (_today(),)).fetchone()["c"]
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
        "daily_profit_rate":   DAILY_PROFIT,
        "trade_symbol":        TRADE_SYMBOL,
        "scheduler_running":   _scheduler_started,
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
    txid = d.get("tx_id","").strip()
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
    txid   = d.get("tx_id","").strip()
    reason = d.get("reason","Rejected by admin")
    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx or tx["status"] != "PENDING":
            return err("Pending transaction not found")
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
    txid = d.get("tx_id","").strip()
    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if (not tx
                or tx["type"] not in ("WITHDRAWAL","REFERRAL_WITHDRAWAL")
                or tx["status"] != "PENDING"):
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
            "(SELECT COUNT(*) FROM referrals WHERE referrer_id=u.id) AS ref_count, "
            "(SELECT COALESCE(SUM(profit),0) FROM daily_trade_log WHERE user_id=u.id) AS total_profit, "
            "(SELECT COUNT(*) FROM daily_trade_log WHERE user_id=u.id) AS days_active "
            "FROM users u LEFT JOIN accounts a ON u.id=a.user_id "
            "WHERE u.role='client' ORDER BY u.created_at DESC"
        ).fetchall()
    return ok([dict(c) for c in clients])

@app.route("/api/admin/client/<uid>/adjust", methods=["POST"])
@admin_required
def admin_adjust_balance(uid):
    d      = request.json or {}
    amount = float(d.get("amount", 0))
    note   = d.get("note","Admin adjustment")
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
             "ADJ-"+secrets.token_hex(4).upper(), note, _now(), _now())
        )
        db.commit()
    return ok({"message": f"Balance adjusted by {amount:+.2f}"})

@app.route("/api/admin/trade/run", methods=["POST"])
@admin_required
def admin_run_trades():
    """Manually trigger daily trades from admin panel."""
    threading.Thread(target=run_daily_trades, daemon=True).start()
    return ok({"message": f"Daily trades triggered — ${DAILY_PROFIT} profit per active client"})

@app.route("/api/admin/trade/log")
@admin_required
def admin_trade_log():
    with get_db() as db:
        rows = db.execute(
            "SELECT d.*, u.name AS user_name FROM daily_trade_log d "
            "JOIN users u ON d.user_id=u.id ORDER BY d.created_at DESC LIMIT 100"
        ).fetchall()
    return ok([dict(r) for r in rows])

@app.route("/api/admin/referrals")
@admin_required
def admin_referrals():
    with get_db() as db:
        refs = db.execute(
            "SELECT r.*, u1.name AS referrer_name, u1.email AS referrer_email, "
            "u2.name AS referred_name, u2.email AS referred_email "
            "FROM referrals r JOIN users u1 ON r.referrer_id=u1.id "
            "JOIN users u2 ON r.referred_id=u2.id ORDER BY r.created_at DESC"
        ).fetchall()
    return ok([dict(r) for r in refs])

# ── SCHEDULER STATUS ──────────────────────────────────────────────────────────
@app.route("/api/admin/scheduler/status")
@admin_required
def scheduler_status():
    now       = datetime.datetime.utcnow()
    next_run  = now.replace(hour=TRADE_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += datetime.timedelta(days=1)
    hours_left = round((next_run - now).total_seconds() / 3600, 1)
    return ok({
        "running":        _scheduler_started,
        "trade_hour_utc": TRADE_HOUR,
        "trade_hour_eat": TRADE_HOUR + 3,
        "next_run_utc":   next_run.isoformat(),
        "hours_until_run": hours_left,
        "daily_profit":   DAILY_PROFIT,
        "min_balance":    MIN_BALANCE,
        "symbol":         TRADE_SYMBOL,
    })

# ── STARTUP ───────────────────────────────────────────────────────────────────
# init_db and start_scheduler are called at module level so gunicorn
# workers pick them up automatically — no __main__ guard needed.
init_db()
start_scheduler()

if __name__ == "__main__":
    print("\n" + "="*60)
    print("   Summit Wealth v5.2 — ONE TRADE/DAY = $8 PROFIT")
    print("="*60)
    print(f"   URL    : http://127.0.0.1:8080")
    print(f"   Client : john@test.com  / demo1234")
    print(f"   Admin  : admin@test.com / admin1234")
    print(f"   Profit : ${DAILY_PROFIT}/day at {TRADE_HOUR:02d}:00 UTC ({TRADE_HOUR+3:02d}:00 EAT)")
    print(f"   Symbol : {TRADE_SYMBOL}")
    print(f"   Binance: {'CONNECTED ✓' if bnb else 'fallback prices'}")
    print(f"   TRC20  : {'SET ✓' if MANUAL_WALLETS['TRC20'] else 'NOT SET ✗'}")
    print("="*60 + "\n")
    app.run(debug=False, port=8080, host="0.0.0.0")
