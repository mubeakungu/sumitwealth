"""
Summit Wealth v4 - COMPLETE STANDALONE VERSION
Includes:
  - Manual deposit fallback (no Binance required)
  - Admin deposit approval
  - Trade bot integration (EMA trend-following)
  - Full trades table support
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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "summit-2025")
CORS(app, supports_credentials=True)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("summit")

# ── BINANCE CLIENT ────────────────────────────────────────────────────────────

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

# Set your actual USDT wallet addresses here
MANUAL_WALLETS = {
    "TRC20": os.environ.get("WALLET_TRC20", ""),   # Tron USDT address
    "BEP20": os.environ.get("WALLET_BEP20", ""),   # BNB Smart Chain USDT address
    "ERC20": os.environ.get("WALLET_ERC20", ""),   # Ethereum USDT address
}

# Trade bot settings
EMA_FAST        = 9
EMA_SLOW        = 21
CHECK_INTERVAL  = 30      # seconds between trade checks
STOP_LOSS_PCT   = 0.015   # 1.5%
TAKE_PROFIT_PCT = 0.030   # 3.0%

# ── DATABASE ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    email        TEXT UNIQUE,
    phone        TEXT,
    password_hash TEXT,
    pin_hash     TEXT,
    role         TEXT DEFAULT 'client',
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    user_id    TEXT,
    balance    REAL DEFAULT 0,
    equity     REAL DEFAULT 0,
    created_at TEXT
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

        # Seed demo data if empty
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

def _hash(s):   return hashlib.sha256(str(s).encode()).hexdigest()
def _uid():     return str(uuid.uuid4())
def _now():     return datetime.datetime.utcnow().isoformat()

def ok(data=None, **kw):
    p = {"success": True}
    if data is not None:
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

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session:
            return err("Not logged in", 401)
        if session.get("role") != "admin":
            return err("Admin access required", 403)
        return f(*a, **kw)
    return dec

# ── TRADE BOT ─────────────────────────────────────────────────────────────────

def _ema(prices: list, period: int) -> float:
    """Calculate EMA and return the latest value."""
    if len(prices) < period:
        return prices[-1]
    k   = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for price in prices[period:]:
        val = price * k + val * (1 - k)
    return val

def get_trend(client, symbol: str) -> str:
    """
    Returns 'UP', 'DOWN', or 'NEUTRAL' using EMA 9/21 crossover on 1h candles.
    """
    try:
        klines = client.get_klines(symbol=symbol, interval="1h", limit=50)
        closes = [float(k[4]) for k in klines]
        fast   = _ema(closes, EMA_FAST)
        slow   = _ema(closes, EMA_SLOW)
        if fast > slow * 1.001:
            return "UP"
        elif fast < slow * 0.999:
            return "DOWN"
        return "NEUTRAL"
    except Exception as e:
        log.error(f"get_trend error [{symbol}]: {e}")
        return "NEUTRAL"

def _open_trade_record(db, user_id, account_id, symbol, direction, entry_price, quantity):
    """Insert a new OPEN trade into the DB."""
    sl = (
        entry_price * (1 - STOP_LOSS_PCT)   if direction == "BUY"
        else entry_price * (1 + STOP_LOSS_PCT)
    )
    tp = (
        entry_price * (1 + TAKE_PROFIT_PCT) if direction == "BUY"
        else entry_price * (1 - TAKE_PROFIT_PCT)
    )
    db.execute(
        """INSERT INTO trades
           (id,user_id,account_id,symbol,direction,entry_price,
            quantity,stop_loss,take_profit,status,opened_at)
           VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?)""",
        (_uid(), user_id, account_id, symbol, direction,
         entry_price, quantity, sl, tp, _now())
    )
    log.info(f"Trade opened | {symbol} {direction} @ {entry_price:.4f} | SL={sl:.4f} TP={tp:.4f}")

def _close_trade_record(db, trade, close_price, reason):
    """Close a trade and update account balance with PnL."""
    entry     = trade["entry_price"]
    qty       = trade["quantity"]
    direction = trade["direction"]
    pnl       = (close_price - entry) * qty if direction == "BUY" else (entry - close_price) * qty

    db.execute(
        "UPDATE trades SET status='CLOSED', close_price=?, pnl=?, "
        "close_reason=?, closed_at=? WHERE id=?",
        (close_price, pnl, reason, _now(), trade["id"])
    )
    db.execute(
        "UPDATE accounts SET balance=balance+?, equity=equity+? WHERE user_id=?",
        (pnl, pnl, trade["user_id"])
    )
    db.commit()
    log.info(f"Trade closed | {trade['symbol']} | Reason: {reason} | PnL: {pnl:+.2f}")
    return pnl

def trade_bot_loop(stop_event: threading.Event):
    """
    Background thread: checks all OPEN trades every CHECK_INTERVAL seconds.
    Logic:
      1. Fetch current price + EMA trend for each open trade's symbol
      2. Hit stop loss  → close, flip direction if trend confirms
      3. Hit take profit → close, re-enter same direction if trend holds
      4. Trend reversed  → close immediately, open in new direction
    """
    log.info("Trade bot started")
    while not stop_event.is_set():
        if bnb:
            try:
                with get_db() as db:
                    open_trades = db.execute(
                        "SELECT * FROM trades WHERE status='OPEN'"
                    ).fetchall()

                if open_trades:
                    symbols_needed = set(t["symbol"] for t in open_trades)
                    prices, trends = {}, {}

                    for sym in symbols_needed:
                        try:
                            ticker      = bnb.get_symbol_ticker(symbol=sym)
                            prices[sym] = float(ticker["price"])
                            trends[sym] = get_trend(bnb, sym)
                        except Exception as e:
                            log.error(f"Price fetch failed [{sym}]: {e}")

                    with get_db() as db:
                        for trade in open_trades:
                            sym       = trade["symbol"]
                            price     = prices.get(sym)
                            trend     = trends.get(sym, "NEUTRAL")
                            direction = trade["direction"]
                            sl        = trade["stop_loss"]
                            tp        = trade["take_profit"]

                            if not price:
                                continue

                            # ── Stop Loss ─────────────────────────────────
                            if (direction == "BUY" and price <= sl) or \
                               (direction == "SELL" and price >= sl):
                                _close_trade_record(db, trade, price, "STOP_LOSS")
                                # Flip if trend confirms new direction
                                new_dir = None
                                if direction == "BUY"  and trend == "DOWN": new_dir = "SELL"
                                if direction == "SELL" and trend == "UP":   new_dir = "BUY"
                                if new_dir:
                                    _open_trade_record(
                                        db, trade["user_id"], trade["account_id"],
                                        sym, new_dir, price, trade["quantity"]
                                    )
                                db.commit()
                                continue

                            # ── Take Profit ───────────────────────────────
                            if (direction == "BUY" and price >= tp) or \
                               (direction == "SELL" and price <= tp):
                                _close_trade_record(db, trade, price, "TAKE_PROFIT")
                                # Re-enter same direction if trend still holds
                                re_enter = False
                                if direction == "BUY"  and trend == "UP":   re_enter = True
                                if direction == "SELL" and trend == "DOWN": re_enter = True
                                if re_enter:
                                    _open_trade_record(
                                        db, trade["user_id"], trade["account_id"],
                                        sym, direction, price, trade["quantity"]
                                    )
                                db.commit()
                                continue

                            # ── Trend Reversal (trade still open) ─────────
                            reversed_against = (
                                (direction == "BUY"  and trend == "DOWN") or
                                (direction == "SELL" and trend == "UP")
                            )
                            if reversed_against:
                                _close_trade_record(db, trade, price, "TREND_REVERSAL")
                                new_dir = "SELL" if direction == "BUY" else "BUY"
                                _open_trade_record(
                                    db, trade["user_id"], trade["account_id"],
                                    sym, new_dir, price, trade["quantity"]
                                )
                                db.commit()

            except Exception as e:
                log.error(f"Trade bot loop error: {e}")

        stop_event.wait(CHECK_INTERVAL)

    log.info("Trade bot stopped")

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
    d     = request.json or {}
    name  = d.get("name", "").strip()
    email = d.get("email", "").lower().strip()
    phone = d.get("phone", "").strip()
    pw    = d.get("password", "")
    pin   = d.get("pin", "000000")

    if not name or not email or len(pw) < 6:
        return err("Invalid input — name, email and password (6+ chars) required")
    if len(pin) != 6 or not pin.isdigit():
        return err("PIN must be exactly 6 digits")

    try:
        with get_db() as db:
            uid, aid, now = _uid(), _uid(), _now()
            db.execute(
                "INSERT INTO users(id,name,email,phone,password_hash,pin_hash,role,created_at) "
                "VALUES(?,?,?,?,?,?,'client',?)",
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
            return err("Please use the admin login page", 403)
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
        open_trades  = db.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE user_id=? AND status='OPEN'", (uid,)
        ).fetchone()
        total_pnl    = db.execute(
            "SELECT COALESCE(SUM(pnl),0) AS s FROM trades WHERE user_id=? AND status='CLOSED'", (uid,)
        ).fetchone()

    return ok({
        "name":              u["name"],
        "balance":           a["balance"]      if a else 0,
        "equity":            a["equity"]       if a else 0,
        "total_deposits":    dep["s"],
        "total_withdrawals": wdr["s"],
        "open_trades":       open_trades["c"],
        "total_pnl":         total_pnl["s"],
    })

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
    status = request.args.get("status", "")   # 'OPEN', 'CLOSED', or '' for all
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
    if net not in NETWORKS:
        return err("Invalid network — choose TRC20, BEP20, or ERC20")

    # Try live Binance address first
    if bnb:
        try:
            addr = bnb.get_deposit_address(coin="USDT", network=NETWORKS[net]["network"])
            return ok({"address": addr["address"], "network": net, "mode": "auto"})
        except Exception as e:
            log.warning(f"Binance deposit address failed: {e}")

    # Manual wallet fallback
    wallet = MANUAL_WALLETS.get(net, "")
    if not wallet:
        return err(
            "Deposit address not configured for this network. "
            "Please contact support or try another network."
        )
    return ok({"address": wallet, "network": net, "mode": "manual"})

@app.route("/api/client/deposit/pending", methods=["POST"])
@login_required
def client_deposit_pending():
    d    = request.json or {}
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network", "TRC20").upper()
    addr = d.get("address", "").strip()

    if amt < 100:
        return err("Minimum deposit is $100")
    if not addr:
        return err("Deposit address is required")

    with get_db() as db:
        acct = db.execute("SELECT id FROM accounts WHERE user_id=?", (uid,)).fetchone()
        ref  = "SWC-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions"
            "(id,user_id,account_id,type,method,amount_usd,reference,status,deposit_address,created_at) "
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

    if amt < 1000:
        return err("Minimum withdrawal is $1,000")
    if not addr:
        return err("Please enter a withdrawal address")
    if net not in NETWORKS:
        return err("Invalid network")

    with get_db() as db:
        u = db.execute("SELECT pin_hash FROM users WHERE id=?", (uid,)).fetchone()
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()

        if not a or a["balance"] < amt:
            return err("Insufficient balance")
        if u["pin_hash"] and u["pin_hash"] != _hash(pin):
            return err("Invalid PIN", 403)

        # Try live Binance withdrawal
        bid = ""
        if bnb:
            try:
                result = bnb.withdraw(
                    coin="USDT", address=addr, amount=amt,
                    network=NETWORKS[net]["network"], name="Summit"
                )
                bid = result.get("id", "")
                status = "COMPLETED"
            except Exception as e:
                return err(f"Binance withdrawal error: {str(e)}", 502)
        else:
            # Manual — admin will process
            status = "PENDING"

        db.execute(
            "UPDATE accounts SET balance=balance-?, equity=equity-? WHERE user_id=?",
            (amt, amt, uid)
        )
        ref = "WD-" + secrets.token_hex(4).upper()
        db.execute(
            "INSERT INTO transactions"
            "(id,user_id,account_id,type,method,amount_usd,reference,status,binance_tx_id,"
            "deposit_address,created_at,completed_at) "
            "VALUES(?,?,?,'WITHDRAWAL',?,?,?,?,?,?,?,?)",
            (_uid(), uid, a["id"], net, amt, ref, status, bid,
             addr, _now(), _now() if status == "COMPLETED" else None)
        )
        db.commit()

    msg = "Withdrawal processed via Binance." if bid else "Withdrawal submitted. Awaiting admin processing."
    return ok({"reference": ref, "binance_id": bid, "message": msg})

# ── MANUAL TRADE OPEN/CLOSE (CLIENT) ─────────────────────────────────────────

@app.route("/api/client/trade/open", methods=["POST"])
@login_required
def client_open_trade():
    """Allow client to manually open a trade."""
    d         = request.json or {}
    uid       = session["user_id"]
    symbol    = d.get("symbol", "BTCUSDT").upper()
    direction = d.get("direction", "BUY").upper()
    quantity  = float(d.get("quantity", 0))

    if direction not in ("BUY", "SELL"):
        return err("direction must be BUY or SELL")
    if quantity <= 0:
        return err("quantity must be greater than 0")

    # Get current price
    entry_price = None
    if bnb:
        try:
            ticker      = bnb.get_symbol_ticker(symbol=symbol)
            entry_price = float(ticker["price"])
        except Exception as e:
            return err(f"Could not fetch price: {e}", 502)
    else:
        entry_price = float(d.get("entry_price", 0))
        if not entry_price:
            return err("Binance not configured — provide entry_price manually")

    with get_db() as db:
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if not a:
            return err("Account not found")
        _open_trade_record(db, uid, a["id"], symbol, direction, entry_price, quantity)
        db.commit()

    return ok({"message": f"Trade opened: {direction} {symbol} @ {entry_price}"})

@app.route("/api/client/trade/close", methods=["POST"])
@login_required
def client_close_trade():
    """Allow client to manually close an open trade."""
    d        = request.json or {}
    uid      = session["user_id"]
    trade_id = d.get("trade_id", "")

    with get_db() as db:
        trade = db.execute(
            "SELECT * FROM trades WHERE id=? AND user_id=? AND status='OPEN'",
            (trade_id, uid)
        ).fetchone()
        if not trade:
            return err("Open trade not found")

        # Fetch close price
        close_price = None
        if bnb:
            try:
                ticker      = bnb.get_symbol_ticker(symbol=trade["symbol"])
                close_price = float(ticker["price"])
            except Exception as e:
                return err(f"Could not fetch price: {e}", 502)
        else:
            close_price = float(d.get("close_price", 0))
            if not close_price:
                return err("Provide close_price (Binance not configured)")

        pnl = _close_trade_record(db, trade, close_price, "MANUAL")

    return ok({"message": f"Trade closed | PnL: {pnl:+.2f} USDT"})

# ── ADMIN API ─────────────────────────────────────────────────────────────────

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    with get_db() as db:
        clients     = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='client'").fetchone()["c"]
        deposits    = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
            "WHERE type='DEPOSIT' AND status='COMPLETED'"
        ).fetchone()["s"]
        withdrawals = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
            "WHERE type='WITHDRAWAL' AND status='COMPLETED'"
        ).fetchone()["s"]
        pending_dep = db.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE type='DEPOSIT' AND status='PENDING'"
        ).fetchone()["c"]
        open_trades = db.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status='OPEN'"
        ).fetchone()["c"]

    return ok({
        "clients":          clients,
        "deposits":         deposits,
        "withdrawals":      withdrawals,
        "pending_deposits": pending_dep,
        "open_trades":      open_trades,
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
    """Admin approves a pending deposit — credits the client account."""
    d    = request.json or {}
    txid = d.get("tx_id", "").strip()

    if not txid:
        return err("tx_id is required")

    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx:
            return err("Transaction not found")
        if tx["type"] != "DEPOSIT":
            return err("Not a deposit transaction")
        if tx["status"] != "PENDING":
            return err(f"Transaction already {tx['status']}")

        db.execute(
            "UPDATE transactions SET status='COMPLETED', completed_at=? WHERE id=?",
            (_now(), txid)
        )
        db.execute(
            "UPDATE accounts SET balance=balance+?, equity=equity+? WHERE user_id=?",
            (tx["amount_usd"], tx["amount_usd"], tx["user_id"])
        )
        db.commit()

    return ok({"message": f"Deposit of ${tx['amount_usd']:,.2f} approved for user {tx['user_id']}"})

@app.route("/api/admin/deposit/reject", methods=["POST"])
@admin_required
def admin_reject_deposit():
    """Admin rejects a pending deposit."""
    d      = request.json or {}
    txid   = d.get("tx_id", "").strip()
    reason = d.get("reason", "Rejected by admin")

    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx or tx["status"] != "PENDING":
            return err("Pending transaction not found")
        db.execute(
            "UPDATE transactions SET status='REJECTED', note=?, completed_at=? WHERE id=?",
            (reason, _now(), txid)
        )
        db.commit()

    return ok({"message": "Deposit rejected"})

@app.route("/api/admin/withdrawal/approve", methods=["POST"])
@admin_required
def admin_approve_withdrawal():
    """Admin marks a pending manual withdrawal as completed."""
    d    = request.json or {}
    txid = d.get("tx_id", "").strip()

    with get_db() as db:
        tx = db.execute("SELECT * FROM transactions WHERE id=?", (txid,)).fetchone()
        if not tx or tx["type"] != "WITHDRAWAL" or tx["status"] != "PENDING":
            return err("Pending withdrawal not found")
        db.execute(
            "UPDATE transactions SET status='COMPLETED', completed_at=? WHERE id=?",
            (_now(), txid)
        )
        db.commit()

    return ok({"message": "Withdrawal marked as completed"})

@app.route("/api/admin/clients")
@admin_required
def admin_clients():
    with get_db() as db:
        clients = db.execute(
            "SELECT u.id, u.name, u.email, u.phone, u.created_at, "
            "a.balance, a.equity "
            "FROM users u LEFT JOIN accounts a ON u.id=a.user_id "
            "WHERE u.role='client' ORDER BY u.created_at DESC"
        ).fetchall()
    return ok([dict(c) for c in clients])

@app.route("/api/admin/client/<uid>/adjust", methods=["POST"])
@admin_required
def admin_adjust_balance(uid):
    """Admin manually adjusts a client's balance (e.g. profit credit, bonus)."""
    d      = request.json or {}
    amount = float(d.get("amount", 0))
    note   = d.get("note", "Admin adjustment")

    if amount == 0:
        return err("Amount cannot be zero")

    with get_db() as db:
        a = db.execute("SELECT * FROM accounts WHERE user_id=?", (uid,)).fetchone()
        if not a:
            return err("Client account not found")
        db.execute(
            "UPDATE accounts SET balance=balance+?, equity=equity+? WHERE user_id=?",
            (amount, amount, uid)
        )
        db.execute(
            "INSERT INTO transactions"
            "(id,user_id,account_id,type,method,amount_usd,reference,status,note,created_at,completed_at) "
            "VALUES(?,?,?,'ADJUSTMENT','MANUAL',?,?,'COMPLETED',?,?,?)",
            (_uid(), uid, a["id"], abs(amount),
             "ADJ-" + secrets.token_hex(4).upper(), note, _now(), _now())
        )
        db.commit()

    return ok({"message": f"Balance adjusted by {amount:+.2f} for user {uid}"})

@app.route("/api/admin/trades")
@admin_required
def admin_trades():
    status = request.args.get("status", "")
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT t.*, u.name AS user_name FROM trades t "
                "JOIN users u ON t.user_id=u.id "
                "WHERE t.status=? ORDER BY t.opened_at DESC",
                (status.upper(),)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT t.*, u.name AS user_name FROM trades t "
                "JOIN users u ON t.user_id=u.id ORDER BY t.opened_at DESC"
            ).fetchall()
    return ok([dict(r) for r in rows])

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    # Start trade bot background thread
    _bot_stop = threading.Event()
    _bot_thread = threading.Thread(
        target=trade_bot_loop,
        args=(_bot_stop,),
        daemon=True,
        name="TradeBotThread"
    )
    _bot_thread.start()

    print("\n" + "="*52)
    print("   Summit Wealth v4 — COMPLETE STANDALONE")
    print("="*52)
    print("   http://127.0.0.1:8080")
    print("   Client : john@test.com  / demo1234")
    print("   Admin  : admin@test.com / admin1234")
    print(f"   Binance: {'CONNECTED ✓' if bnb else 'NOT configured (manual mode)'}")
    print(f"   Trade bot: RUNNING (checks every {CHECK_INTERVAL}s)")
    print("="*52 + "\n")

    try:
        app.run(debug=False, port=8080, host="0.0.0.0")
    finally:
        _bot_stop.set()
        _bot_thread.join(timeout=5)
        print("Trade bot stopped.")
