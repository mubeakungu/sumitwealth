"""
Summit Wealth v5.13 - $8 PROFIT PER $100 BALANCE (8% daily)
- Scheduler starts at module level (works with gunicorn on Render)
- One controlled trade per client per day
- Trade profit scales: $8 per $100 of balance
- Realistic trade using real Binance prices
- Referral system: 16% commission
- Manual wallet for USDT deposits
- Min deposit: $100
- Withdrawal deducted only on admin approval
- Database: PostgreSQL (psycopg2)
- FIX v5.5: total_withdrawals now correctly summed in client_summary
            both WITHDRAWAL and REFERRAL_WITHDRAWAL counted
            referral withdrawal rejection now restores ref_balance
            admin clients list now includes total_deposits/withdrawals
- FIX v5.6: referral withdrawal now requires referred user to have made a deposit
- NEW v5.7: M-Pesa manual deposit method added
- NEW v5.8: M-Pesa STK Push (Daraja API) — client enters phone + KES amount,
            receives real M-Pesa PIN prompt on their phone,
            callback auto-approves and credits account on success
- FIX v5.9: STK Push route renamed to /api/client/mpesa/stk-push (dashboard match)
            Added /api/client/mpesa/status polling endpoint
- FIX v5.10: CRITICAL — fixed double/triple daily-profit crediting caused by
            multiple gunicorn workers each running their own scheduler thread
            (and/or repeated manual "Run Trades" clicks) racing on a
            SELECT-then-INSERT check. daily_trade_log now has a UNIQUE
            (user_id, date) constraint and the "already traded today" check
            is now a single atomic INSERT ... ON CONFLICT DO NOTHING. Only
            the worker that successfully claims the slot credits the
            balance — every other concurrent attempt is a guaranteed no-op.
            Added a process-local lock so manual admin trade-runs can't
            overlap a scheduled run either.
- FIX v5.11: CHANGED PROFIT BASIS — profit is now a flat $8 per $100 of a
            client's NET DEPOSITS (completed deposits minus completed
            principal WITHDRAWALs), not 8% of their live/compounding
            balance. Previously the formula was (balance/100)*8, which
            compounds daily (8% of an ever-growing number), so long-tenure
            clients silently earned far more than "$8 per $100 deposited"
            implies (e.g. 12 days of compounding ≈ 2.5x the flat amount).
            Now profit is (net_deposit/100)*8 every day — the daily payout
            stays constant unless the client deposits more or withdraws
            principal. Referral withdrawals are NOT counted against net
            deposit (they only ever draw from ref_balance, never principal).
- NEW v5.12: Free-tier Render has no shell access, so the balance-correction
            migration (compounded → flat 8%/100 net deposit) now runs
            in-process via a protected admin endpoint instead of a standalone
            script: POST /api/admin/migrate/flat-profit (dry run by default,
            {"apply": true} to commit). Added a notifications table + client
            endpoints so affected clients see an in-platform message
            explaining any balance correction — the migration endpoint
            auto-creates one for every client whose balance changes. Also
            added a general-purpose admin broadcast/direct-message endpoint.
- NEW v5.13: Password recovery added.
            - /forgot-password page + POST /api/auth/forgot-password:
              self-service reset for clients. No SMTP is configured for
              Summit Wealth, so identity is verified with email + the
              client's registered phone + their 6-digit PIN (the same PIN
              already used for withdrawals) instead of an email link.
            - POST /api/admin/client/<uid>/reset-password: lets an admin
              set a new password directly for any client from the admin
              dashboard's client-edit view, with an optional in-platform
              notification (reuses the existing notifications table).
"""

import os, hashlib, secrets, datetime, uuid, logging, threading, random, base64
import psycopg2
import psycopg2.extras
import requests as http_requests
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
    raise RuntimeError("DATABASE_URL environment variable is not set!")

DAILY_PROFIT_PER_100 = float(os.environ.get("DAILY_PROFIT_USD", "8.0"))
MIN_BALANCE          = float(os.environ.get("MIN_BALANCE", "100.0"))
TRADE_HOUR           = int(os.environ.get("TRADE_HOUR", "5"))
TRADE_SYMBOL         = os.environ.get("TRADE_SYMBOL", "BTCUSDT")
CHECK_INTERVAL       = 60

# ── NETWORKS & WALLETS ────────────────────────────────────────────────────────
NETWORKS = {
    "TRC20": {"network": "TRX"},
    "BEP20": {"network": "BSC"},
    "ERC20": {"network": "ETH"},
    "MPESA": {"network": "MPESA"},
}
MANUAL_WALLETS = {
    "TRC20": os.environ.get("WALLET_TRC20", ""),
    "BEP20": os.environ.get("WALLET_BEP20", ""),
    "ERC20": os.environ.get("WALLET_ERC20", ""),
}

# ── DARAJA / M-PESA STK PUSH ──────────────────────────────────────────────────
MPESA_ENV             = os.environ.get("MPESA_ENV", "sandbox")
MPESA_CONSUMER_KEY    = os.environ.get("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE       = os.environ.get("MPESA_SHORTCODE", "174379")
MPESA_PASSKEY         = os.environ.get("MPESA_PASSKEY", "")
MPESA_CALLBACK_URL    = os.environ.get("MPESA_CALLBACK_URL", "https://sumitwealthfx.space/mpesa/callback")

# Exchange rate: KES per USD
KES_PER_USD = float(os.environ.get("KES_PER_USD", "129.0"))

if MPESA_ENV == "production":
    MPESA_BASE_URL = "https://api.safaricom.co.ke"
else:
    MPESA_BASE_URL = "https://sandbox.safaricom.co.ke"

REFERRAL_COMMISSION_PCT = 0.16
REFERRAL_MIN_DEPOSIT    = 100.0

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
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
CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    user_id    TEXT,
    title      TEXT,
    message    TEXT,
    type       TEXT DEFAULT 'INFO',
    read_at    TEXT,
    created_at TEXT
);
""")
    conn.commit()

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

    # ── FIX v5.10: enforce one daily_trade_log row per (user_id, date) ──────
    # This is what actually stops double-crediting. Without this constraint,
    # two concurrent workers (or two manual "Run Trades" clicks) can both
    # pass the old SELECT check before either INSERT lands, and both credit
    # the account. First, deduplicate any existing double-paid rows, then
    # add the constraint so it can never happen again.
    try:
        cur.execute("""
            DELETE FROM daily_trade_log a USING daily_trade_log b
            WHERE a.id > b.id
              AND a.user_id = b.user_id
              AND a.date = b.date
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.warning(f"daily_trade_log dedupe skipped: {e}")

    try:
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_trade_log_user_date
            ON daily_trade_log(user_id, date)
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.warning(f"daily_trade_log unique index skipped: {e}")

    cur.execute("SELECT id FROM users WHERE referral_code IS NULL OR referral_code=''")
    users = cur.fetchall()
    for u in users:
        cur.execute("UPDATE users SET referral_code=%s WHERE id=%s",
                    (secrets.token_hex(4).upper(), u["id"]))
    conn.commit()

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

def create_notification(cur, user_id, title, message, ntype="INFO"):
    """Insert a notification row. Caller is responsible for conn.commit()."""
    cur.execute(
        "INSERT INTO notifications(id,user_id,title,message,type,created_at) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (_uid(), user_id, title, message, ntype, _now())
    )

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
            price  = float(ticker["price"])
            log.info(f"Live Binance price {symbol}: ${price:,.2f}")
            return price
        except Exception as e:
            log.warning(f"Could not get live price for {symbol}: {e}")
    fallback = {"BTCUSDT": 67500.0, "ETHUSDT": 3450.0, "BNBUSDT": 580.0}
    price = fallback.get(symbol, 100.0)
    log.info(f"Using fallback price {symbol}: ${price:,.2f}")
    return price

# FIX v5.10: process-local lock so a manual admin trade-run can't overlap
# a concurrently firing scheduled run within the SAME worker process.
_trade_run_lock = threading.Lock()

def run_daily_trades():
    if not _trade_run_lock.acquire(blocking=False):
        log.warning("run_daily_trades already in progress on this worker — skipping overlapping call")
        return

    try:
        today = _today()
        log.info(f"=== Daily trade run: {today} — ${DAILY_PROFIT_PER_100} profit per $100 balance ===")

        price       = get_live_price(TRADE_SYMBOL)
        pct_gain    = random.uniform(0.003, 0.005)
        close_price = round(price * (1 + pct_gain), 2)
        price_diff  = close_price - price

        if price_diff <= 0:
            log.error("Price diff is zero — aborting.")
            return

        log.info(f"  Entry: ${price:,.2f} | Close: ${close_price:,.2f} | Rate: ${DAILY_PROFIT_PER_100}/$100")

        conn = get_db()
        cur  = conn.cursor()
        # FIX v5.11: eligibility and profit are based on NET DEPOSITS
        # (completed deposits minus completed *principal* withdrawals),
        # not live/compounding balance. Referral withdrawals are excluded
        # since they only ever draw from ref_balance, never principal.
        cur.execute(
            "SELECT u.id, u.name, a.id AS account_id, a.balance, "
            "  COALESCE((SELECT SUM(amount_usd) FROM transactions "
            "            WHERE user_id=u.id AND type='DEPOSIT' AND status='COMPLETED'), 0) "
            "  - COALESCE((SELECT SUM(amount_usd) FROM transactions "
            "              WHERE user_id=u.id AND type='WITHDRAWAL' AND status='COMPLETED'), 0) "
            "  AS net_deposit "
            "FROM users u JOIN accounts a ON u.id=a.user_id "
            "WHERE u.role='client'"
        )
        all_clients = cur.fetchall()
        clients = [c for c in all_clients if c["net_deposit"] >= MIN_BALANCE]
        log.info(f"  Eligible clients (net deposit >= ${MIN_BALANCE}): {len(clients)}")
        paid = 0

        for c in clients:
            # Flat $8 per $100 of net deposit — does NOT compound off balance.
            client_profit   = round((c["net_deposit"] / 100.0) * DAILY_PROFIT_PER_100, 2)
            client_quantity = round(client_profit / price_diff, 6)

            now              = datetime.datetime.utcnow()
            open_minutes_ago = random.randint(30, 90)
            opened_at        = (now - datetime.timedelta(minutes=open_minutes_ago)).isoformat()
            closed_at        = now.isoformat()
            trade_id         = _uid()
            sl               = round(price * 0.985, 2)
            tp               = close_price

            try:
                # ── FIX v5.10: atomic claim ──────────────────────────────
                # This single INSERT is the ONLY thing that decides whether
                # this client gets paid today. If another worker/thread has
                # already inserted a row for (user_id, date), the unique
                # index makes this a guaranteed no-op (rowcount 0) instead
                # of a duplicate payout. No SELECT-then-INSERT race window.
                cur.execute(
                    "INSERT INTO daily_trade_log(id,user_id,account_id,trade_id,"
                    "profit,date,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (user_id, date) DO NOTHING",
                    (_uid(), c["id"], c["account_id"], trade_id, client_profit, today, _now())
                )

                if cur.rowcount == 0:
                    conn.commit()
                    log.info(f"  Skipping {c['name']} — already traded today")
                    continue

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
                conn.commit()
                paid += 1
                log.info(f"  ✓ {c['name']}: +${client_profit} (net deposit: ${c['net_deposit']:,.2f}, bal: ${c['balance']:,.2f})")

            except Exception as e:
                conn.rollback()
                log.error(f"  Trade failed for {c['name']}: {e}")

        cur.close()
        conn.close()
        log.info(f"=== Done: {paid}/{len(clients)} clients credited ===")
    finally:
        _trade_run_lock.release()

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
_scheduler_lock    = threading.Lock()
_scheduler_started = False
_scheduler_stop    = threading.Event()

def trade_scheduler(stop_event):
    log.info(f"Scheduler started — fires at {TRADE_HOUR:02d}:00 UTC ({TRADE_HOUR+3:02d}:00 EAT)")
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
    log.info("Scheduler stopped")

def start_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
        t = threading.Thread(target=trade_scheduler, args=(_scheduler_stop,),
                             daemon=True, name="TradeScheduler")
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

# ── DARAJA STK PUSH ENGINE ────────────────────────────────────────────────────

def mpesa_get_token():
    """Get OAuth access token from Daraja."""
    try:
        creds = base64.b64encode(
            f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()
        ).decode()
        r = http_requests.get(
            f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}"},
            timeout=15
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        log.error(f"M-Pesa token error: {e}")
        return None

def mpesa_format_phone(phone):
    """Normalise phone to 2547XXXXXXXX format."""
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not phone.startswith("254"):
        phone = "254" + phone
    return phone

def mpesa_stk_push(phone, amount_kes, account_ref, description):
    """
    Initiate STK Push.
    Returns (True, checkout_request_id) or (False, error_msg).
    amount_kes must be an integer (minimum 1).
    """
    token = mpesa_get_token()
    if not token:
        return False, "Could not connect to M-Pesa. Please try again."

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    password  = base64.b64encode(
        f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
    ).decode()

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password":          password,
        "Timestamp":         timestamp,
        "TransactionType":   "CustomerPayBillOnline",
        "Amount":            int(amount_kes),
        "PartyA":            mpesa_format_phone(phone),
        "PartyB":            MPESA_SHORTCODE,
        "PhoneNumber":       mpesa_format_phone(phone),
        "CallBackURL":       MPESA_CALLBACK_URL,
        "AccountReference":  account_ref[:12],
        "TransactionDesc":   description[:13],
    }

    try:
        r = http_requests.post(
            f"{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        data = r.json()
        log.info(f"STK Push response: {data}")

        if data.get("ResponseCode") == "0":
            return True, data.get("CheckoutRequestID", "")
        else:
            msg = data.get("errorMessage") or data.get("ResponseDescription") or "M-Pesa request failed"
            return False, msg
    except Exception as e:
        log.error(f"STK Push error: {e}")
        return False, "M-Pesa service unavailable. Please try again."

# ── PAGE ROUTES ───────────────────────────────────────────────────────────────
@app.route("/")
def index(): return render_template("login.html")

@app.route("/register")
def register_page(): return render_template("register.html")

@app.route("/forgot-password")
def forgot_password_page():
    return render_template("forgot_password.html")

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

# ── AUTH: FORGOT PASSWORD (v5.13) ─────────────────────────────────────────────
@app.route("/api/auth/forgot-password", methods=["POST"])
def api_forgot_password():
    """
    Self-service password reset for clients.

    There's no SMTP configured for Summit Wealth (unlike Zappest), so
    instead of an emailed reset link, identity is verified using three
    things the client already has on file:
        - their account email
        - their registered phone number
        - their 6-digit PIN (the same PIN used for withdrawals)
    All three must match before the password is changed. Admin accounts
    are excluded — admins are reset by another admin via the admin panel.

    Body: { email, phone, pin, new_password }
    """
    d            = request.json or {}
    email        = d.get("email","").lower().strip()
    phone        = d.get("phone","").strip()
    pin          = d.get("pin","").strip()
    new_password = d.get("new_password","")

    if not email or not phone or not pin:
        return err("Email, phone and PIN are required")
    if len(pin) != 6 or not pin.isdigit():
        return err("PIN must be exactly 6 digits")
    if len(new_password) < 6:
        return err("New password must be at least 6 characters")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=%s AND role='client'", (email,))
    u = cur.fetchone()

    if not u:
        cur.close(); conn.close()
        return err("No matching account found", 404)

    stored_phone = mpesa_format_phone(u["phone"] or "")
    given_phone  = mpesa_format_phone(phone)

    if not u["phone"] or stored_phone != given_phone or u["pin_hash"] != _hash(pin):
        cur.close(); conn.close()
        return err("Details do not match our records", 403)

    cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                (_hash(new_password), u["id"]))
    conn.commit()
    cur.close(); conn.close()

    log.info(f"Password reset via forgot-password flow: {u['email']}")
    return ok({"message": "Password updated. You can now sign in with your new password."})

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
        """
        SELECT COALESCE(SUM(amount_usd), 0) AS s
        FROM transactions
        WHERE user_id = %s
          AND type IN ('WITHDRAWAL', 'REFERRAL_WITHDRAWAL')
          AND status = 'COMPLETED'
        """,
        (uid,)
    )
    wdr = cur.fetchone()

    # FIX v5.11: principal withdrawals only (excludes REFERRAL_WITHDRAWAL,
    # which draws from ref_balance and never affects the profit-earning
    # principal). Used to compute the flat, non-compounding net deposit.
    cur.execute(
        """
        SELECT COALESCE(SUM(amount_usd), 0) AS s
        FROM transactions
        WHERE user_id = %s
          AND type = 'WITHDRAWAL'
          AND status = 'COMPLETED'
        """,
        (uid,)
    )
    principal_wdr = cur.fetchone()

    cur.execute(
        """
        SELECT COALESCE(SUM(amount_usd), 0) AS s
        FROM transactions
        WHERE user_id = %s
          AND type IN ('WITHDRAWAL', 'REFERRAL_WITHDRAWAL')
          AND status = 'PENDING'
        """,
        (uid,)
    )
    wdr_pending = cur.fetchone()

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

    balance        = a["balance"] if a else 0
    # FIX v5.11: flat basis — net_deposit stays constant unless the client
    # deposits more or withdraws principal, so this no longer compounds.
    net_deposit    = round(dep["s"] - principal_wdr["s"], 2)
    expected_daily = round((net_deposit / 100.0) * DAILY_PROFIT_PER_100, 2) if net_deposit >= MIN_BALANCE else 0

    return ok({
        "name":                u["name"],
        "phone":               u["phone"] or "",
        "balance":             balance,
        "equity":              a["equity"]        if a else 0,
        "total_deposits":      dep["s"],
        "total_withdrawals":   wdr["s"],
        "net_deposit":         net_deposit,
        "pending_withdrawals": wdr_pending["s"],
        "ref_balance":         a["ref_balance"]   if a else 0,
        "ref_code":            u["referral_code"] or "",
        "ref_count":           ref_count["c"],
        "ref_earned":          ref_earned["s"],
        "open_trades":         open_trades["c"],
        "total_profit":        total_profit["s"],
        "days_traded":         days_traded["c"],
        "daily_profit":        expected_daily,
        "daily_profit_rate":   DAILY_PROFIT_PER_100,
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

    cur.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s AND status='CREDITED'", (uid,)
    )
    referral_count = cur.fetchone()["c"]
    if referral_count == 0:
        cur.close(); conn.close()
        return err("You must have at least one referred user who made a deposit")

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
        "SELECT * FROM daily_trade_log WHERE user_id=%s ORDER BY date DESC LIMIT 30", (uid,)
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(r) for r in rows])

# ── NOTIFICATIONS (v5.12) ─────────────────────────────────────────────────────
@app.route("/api/client/notifications")
@login_required
def client_notifications():
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
        (uid,)
    )
    rows = cur.fetchall()
    cur.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id=%s AND read_at IS NULL",
        (uid,)
    )
    unread = cur.fetchone()["c"]
    cur.close(); conn.close()
    return ok({"notifications": [dict(r) for r in rows], "unread_count": unread})

@app.route("/api/client/notifications/<nid>/read", methods=["POST"])
@login_required
def client_notification_read(nid):
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE notifications SET read_at=%s WHERE id=%s AND user_id=%s AND read_at IS NULL",
        (_now(), nid, uid)
    )
    conn.commit()
    cur.close(); conn.close()
    return ok()

@app.route("/api/client/notifications/read-all", methods=["POST"])
@login_required
def client_notifications_read_all():
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE notifications SET read_at=%s WHERE user_id=%s AND read_at IS NULL",
        (_now(), uid)
    )
    conn.commit()
    cur.close(); conn.close()
    return ok()

# ── DEPOSIT ───────────────────────────────────────────────────────────────────
@app.route("/api/client/deposit/address")
@login_required
def client_deposit_address():
    net = request.args.get("network","TRC20").upper()
    if net not in NETWORKS: return err("Invalid network")
    if net == "MPESA":
        return ok({"network": "MPESA", "mode": "stk_push"})
    wallet = MANUAL_WALLETS.get(net,"")
    if not wallet: return err("Deposit address not configured. Contact support.")
    return ok({"address": wallet, "network": net, "mode": "manual"})


# ── M-PESA STK PUSH (v5.9 — route fixed to match dashboard) ─────────────────
@app.route("/api/client/mpesa/stk-push", methods=["POST"])
@login_required
def client_mpesa_stk_push():
    """
    Initiate M-Pesa STK Push.
    Body: { phone_number: "0712345678", amount: <KES int> }
    Converts amount KES → USD, creates PENDING transaction,
    fires STK Push, stores CheckoutRequestID for callback matching.
    """
    d          = request.json or {}
    uid        = session["user_id"]
    amount_kes = float(d.get("amount", 0))
    phone      = d.get("phone_number", "").strip()

    if amount_kes < 12952.2169:
        return err("Minimum deposit is KES 12952.2169")
    if not phone:
        return err("Phone number is required")

    # Normalise phone
    phone_fmt = mpesa_format_phone(phone)

    # Convert KES → USD
    amount_usd = round(amount_kes / KES_PER_USD, 2)
    if amount_usd < 1:
        return err("Amount too low after conversion")

    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT id FROM accounts WHERE user_id=%s", (uid,))
    acct  = cur.fetchone()
    tx_id = _uid()
    ref   = "SWC-" + secrets.token_hex(4).upper()
    note  = f"STK Push | Phone: {phone_fmt} | KES: {int(amount_kes)}"

    # Create PENDING transaction first
    cur.execute(
        "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
        "reference,status,note,deposit_address,created_at) "
        "VALUES(%s,%s,%s,'DEPOSIT','MPESA',%s,%s,'PENDING',%s,%s,%s)",
        (tx_id, uid, acct["id"] if acct else None,
         amount_usd, ref, note, phone_fmt, _now())
    )
    conn.commit()

    # Fire STK Push to Daraja
    push_ok, result = mpesa_stk_push(
        phone       = phone,
        amount_kes  = int(amount_kes),
        account_ref = ref,
        description = "Summit Deposit"
    )

    if not push_ok:
        # Mark as rejected so it doesn't linger as pending
        cur.execute(
            "UPDATE transactions SET status='REJECTED', note=%s WHERE id=%s",
            (f"STK Push failed: {result}", tx_id)
        )
        conn.commit()
        cur.close(); conn.close()
        return err(result)

    # Store Daraja CheckoutRequestID in binance_tx_id column for callback lookup
    cur.execute(
        "UPDATE transactions SET binance_tx_id=%s WHERE id=%s",
        (result, tx_id)
    )
    conn.commit()
    cur.close(); conn.close()

    log.info(f"STK Push sent: {ref} | {phone_fmt} | KES {int(amount_kes)} | CheckoutID: {result}")
    return ok({
        "reference":           ref,
        "checkout_request_id": result,
        "amount_kes":          int(amount_kes),
        "amount_usd":          amount_usd,
        "message":             f"M-Pesa prompt sent to {phone_fmt}. Enter your PIN to complete."
    })


# ── M-PESA STATUS POLLING (v5.9 — NEW) ───────────────────────────────────────
@app.route("/api/client/mpesa/status")
@login_required
def client_mpesa_status():
    """
    Poll STK Push payment status by CheckoutRequestID.
    Dashboard calls this every 5s while waiting screen is shown.
    Returns: { status: PENDING|COMPLETED|REJECTED, amount, mpesa_code, reference }
    """
    checkout_id = request.args.get("checkout_request_id", "").strip()
    if not checkout_id:
        return err("checkout_request_id is required")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT status, amount_usd, note, reference FROM transactions "
        "WHERE binance_tx_id=%s AND type='DEPOSIT'",
        (checkout_id,)
    )
    tx = cur.fetchone()
    cur.close(); conn.close()

    if not tx:
        # Not found yet — still pending
        return ok({"status": "PENDING"})

    # Extract M-Pesa receipt number from note if payment completed
    mpesa_code = ""
    if tx["status"] == "COMPLETED" and tx["note"] and "MpesaRef:" in tx["note"]:
        try:
            mpesa_code = tx["note"].split("MpesaRef:")[-1].strip().split(" ")[0]
        except Exception:
            pass

    return ok({
        "status":     tx["status"],       # PENDING / COMPLETED / REJECTED
        "amount":     tx["amount_usd"],
        "mpesa_code": mpesa_code,
        "reference":  tx["reference"],
        "message":    tx["note"] or ""
    })


# ── M-PESA CALLBACK (Daraja → this endpoint) ─────────────────────────────────
@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    """
    Daraja STK Push result callback.
    ResultCode 0 = success → credit account.
    Any other code = failed/cancelled → reject transaction.
    """
    try:
        data = request.json or {}
        log.info(f"M-Pesa callback received: {data}")

        body        = data.get("Body", {})
        stk         = body.get("stkCallback", {})
        result_code = stk.get("ResultCode")
        checkout_id = stk.get("CheckoutRequestID", "")

        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT * FROM transactions "
            "WHERE binance_tx_id=%s AND type='DEPOSIT' AND status='PENDING'",
            (checkout_id,)
        )
        tx = cur.fetchone()

        if not tx:
            log.warning(f"M-Pesa callback: no pending tx for CheckoutRequestID={checkout_id}")
            cur.close(); conn.close()
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

        if result_code == 0:
            # Payment successful — extract metadata and credit account
            meta       = stk.get("CallbackMetadata", {}).get("Item", [])
            mpesa_ref  = next((i["Value"] for i in meta if i.get("Name") == "MpesaReceiptNumber"), "")
            amount_kes = next((i["Value"] for i in meta if i.get("Name") == "Amount"), "")

            cur.execute(
                "UPDATE transactions SET status='COMPLETED', completed_at=%s, "
                "note=note||%s WHERE id=%s",
                (_now(), f" | MpesaRef: {mpesa_ref} | KES: {amount_kes}", tx["id"])
            )
            cur.execute(
                "UPDATE accounts SET balance=balance+%s, equity=equity+%s "
                "WHERE user_id=%s",
                (tx["amount_usd"], tx["amount_usd"], tx["user_id"])
            )
            conn.commit()
            log.info(
                f"M-Pesa payment confirmed: {tx['reference']} "
                f"+${tx['amount_usd']} | MpesaRef: {mpesa_ref}"
            )

            # Process referral commission in background
            threading.Thread(
                target=process_referral_commission,
                args=(tx["id"], tx["user_id"], tx["amount_usd"]),
                daemon=True
            ).start()

        else:
            result_desc = stk.get("ResultDesc", "Cancelled or failed")
            cur.execute(
                "UPDATE transactions SET status='REJECTED', "
                "note=note||%s, completed_at=%s WHERE id=%s",
                (f" | Failed: {result_desc}", _now(), tx["id"])
            )
            conn.commit()
            log.info(f"M-Pesa payment failed: {tx['reference']} | {result_desc}")

        cur.close(); conn.close()

    except Exception as e:
        log.error(f"M-Pesa callback error: {e}")

    # Always return 200 to Safaricom
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/api/client/deposit/pending", methods=["POST"])
@login_required
def client_deposit_pending():
    """USDT manual deposit only — M-Pesa uses /api/client/mpesa/stk-push."""
    d    = request.json or {}
    uid  = session["user_id"]
    amt  = float(d.get("amount", 0))
    net  = d.get("network","TRC20").upper()
    addr = d.get("address","").strip()

    if net == "MPESA":
        return err("Use the M-Pesa deposit flow")
    if amt < 100:  return err("Minimum deposit is $100")
    if not addr:   return err("Deposit address is required")

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

    if amt < 10:          return err("Minimum withdrawal is $10")
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
    cur.execute("SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions WHERE type IN ('WITHDRAWAL','REFERRAL_WITHDRAWAL') AND status='COMPLETED'")
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

    if tx["type"] == "WITHDRAWAL":
        cur.execute(
            "UPDATE accounts SET balance=balance-%s, equity=equity-%s WHERE user_id=%s",
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

    if tx["type"] == "REFERRAL_WITHDRAWAL":
        cur.execute(
            "UPDATE accounts SET ref_balance=ref_balance+%s WHERE user_id=%s",
            (tx["amount_usd"], tx["user_id"])
        )

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
        "(SELECT COUNT(*) FROM daily_trade_log WHERE user_id=u.id) AS days_active, "
        "(SELECT COALESCE(SUM(amount_usd),0) FROM transactions "
        " WHERE user_id=u.id AND type='DEPOSIT' AND status='COMPLETED') AS total_deposits, "
        "(SELECT COALESCE(SUM(amount_usd),0) FROM transactions "
        " WHERE user_id=u.id AND type IN ('WITHDRAWAL','REFERRAL_WITHDRAWAL') "
        " AND status='COMPLETED') AS total_withdrawals, "
        "(SELECT COALESCE(SUM(commission_usd),0) FROM referrals "
        " WHERE referrer_id=u.id) AS total_ref_earned "
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
         "ADJ-"+secrets.token_hex(4).upper(), note, _now(), _now(), _now())
    )
    conn.commit()
    cur.close(); conn.close()
    return ok({"message": f"Balance adjusted by {amount:+.2f}"})

@app.route("/api/admin/client/<uid>/edit", methods=["POST"])
@admin_required
def admin_edit_client(uid):
    d     = request.json or {}
    name  = d.get("name","").strip()
    email = d.get("email","").lower().strip()
    phone = d.get("phone","").strip()
    if not name:  return err("Name is required")
    if not email: return err("Email is required")
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE id=%s", (uid,))
        if not cur.fetchone():
            cur.close(); conn.close()
            return err("Client not found", 404)
        cur.execute(
            "UPDATE users SET name=%s, email=%s, phone=%s WHERE id=%s",
            (name, email, phone, uid)
        )
        conn.commit()
        cur.close(); conn.close()
        return ok({"message": "Client updated successfully"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close(); conn.close()
        return err("That email is already used by another account", 409)

# ── ADMIN: RESET CLIENT PASSWORD (v5.13) ──────────────────────────────────────
@app.route("/api/admin/client/<uid>/reset-password", methods=["POST"])
@admin_required
def admin_reset_client_password(uid):
    """
    Admin sets a new password directly for a client — no PIN or old
    password required. Called from the client Edit view in the admin
    dashboard. Optionally creates an in-platform notification so the
    client knows their password changed.

    Body: { new_password: str, notify: bool (default true) }
    """
    d            = request.json or {}
    new_password = d.get("new_password","")
    notify       = bool(d.get("notify", True))

    if len(new_password) < 6:
        return err("New password must be at least 6 characters")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT id, name, email FROM users WHERE id=%s AND role='client'", (uid,))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        return err("Client not found", 404)

    cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                (_hash(new_password), uid))

    if notify:
        create_notification(
            cur, uid,
            "Your password was reset",
            "An administrator has reset your account password. If you did not "
            "request this, please contact support immediately.",
            "ALERT"
        )

    conn.commit()
    cur.close(); conn.close()

    log.info(f"Admin reset password for client {u['email']}")
    return ok({"message": f"Password reset for {u['name']}"})

@app.route("/api/admin/trade/run", methods=["POST"])
@admin_required
def admin_run_trades():
    threading.Thread(target=run_daily_trades, daemon=True).start()
    return ok({"message": f"Daily trades triggered — ${DAILY_PROFIT_PER_100} per $100 balance"})

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
    status = request.args.get("status","").upper()
    conn   = get_db()
    cur    = conn.cursor()
    if status:
        cur.execute(
            "SELECT t.*, u.name AS user_name FROM trades t "
            "JOIN users u ON t.user_id=u.id "
            "WHERE t.status=%s ORDER BY t.opened_at DESC", (status,)
        )
    else:
        cur.execute(
            "SELECT t.*, u.name AS user_name FROM trades t "
            "JOIN users u ON t.user_id=u.id ORDER BY t.opened_at DESC"
        )
    trades = cur.fetchall()
    cur.close(); conn.close()
    return ok([dict(t) for t in trades])

@app.route("/api/admin/migrate/flat-profit", methods=["POST"])
@admin_required
def admin_migrate_flat_profit():
    """
    ONE-OFF MIGRATION (v5.12) — runs in-process since free-tier Render has
    no shell access. Corrects balances that were paid under the OLD
    compounding formula (balance/100*8 daily) to match the NEW flat formula
    (net_deposit/100*8, same amount every day).

    Body: { "apply": false }  (default) -> dry run, computes and returns
                                            deltas, writes NOTHING to the DB
          { "apply": true }             -> commits the corrections:
                                            - adjusts accounts.balance/equity
                                            - logs a visible ADJUSTMENT/MIGRATION
                                              transaction per affected client
                                            - rewrites daily_trade_log.profit
                                              rows to the flat amount
                                            - creates an in-platform
                                              notification for every client
                                              whose balance changed
    Always call with apply=false first and review the response before
    calling again with apply=true.
    """
    d     = request.json or {}
    apply = bool(d.get("apply", False))

    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT u.id, u.name, u.email, a.id AS account_id, a.balance, a.equity "
        "FROM users u JOIN accounts a ON u.id = a.user_id "
        "WHERE u.role = 'client' ORDER BY u.created_at"
    )
    clients = cur.fetchall()

    results      = []
    total_delta  = 0.0

    for c in clients:
        uid = c["id"]

        cur.execute(
            "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
            "WHERE user_id=%s AND type='DEPOSIT' AND status='COMPLETED'", (uid,)
        )
        deposits = cur.fetchone()["s"]

        cur.execute(
            "SELECT COALESCE(SUM(amount_usd),0) AS s FROM transactions "
            "WHERE user_id=%s AND type='WITHDRAWAL' AND status='COMPLETED'", (uid,)
        )
        principal_withdrawals = cur.fetchone()["s"]

        net_deposit = round(deposits - principal_withdrawals, 2)

        cur.execute(
            "SELECT id, profit FROM daily_trade_log WHERE user_id=%s ORDER BY date", (uid,)
        )
        trade_log_rows    = cur.fetchall()
        days_traded        = len(trade_log_rows)
        old_total_profit   = round(sum(r["profit"] for r in trade_log_rows), 2)

        # Flag clients with more than one deposit/withdrawal event — net
        # deposit may not have been constant across their whole history, so
        # the correction nets out the *total* correctly but isn't a perfect
        # day-by-day reconstruction. Worth a manual look for these.
        cur.execute(
            "SELECT COUNT(*) AS c FROM transactions "
            "WHERE user_id=%s AND type IN ('DEPOSIT','WITHDRAWAL') AND status='COMPLETED'", (uid,)
        )
        mixed_history = cur.fetchone()["c"] > 1

        if net_deposit < MIN_BALANCE or days_traded == 0:
            flat_daily = 0.0
        else:
            flat_daily = round((net_deposit / 100.0) * DAILY_PROFIT_PER_100, 2)

        correct_total_profit = round(flat_daily * days_traded, 2)
        delta = round(correct_total_profit - old_total_profit, 2)

        entry = {
            "user_id": uid, "name": c["name"], "email": c["email"],
            "net_deposit": net_deposit, "days_traded": days_traded,
            "old_total_profit": old_total_profit,
            "correct_total_profit": correct_total_profit,
            "delta": delta, "mixed_history": mixed_history,
        }
        results.append(entry)

        if abs(delta) < 0.01:
            continue
        total_delta += delta

        if apply:
            try:
                cur.execute(
                    "UPDATE accounts SET balance = balance + %s, equity = equity + %s "
                    "WHERE user_id = %s",
                    (delta, delta, uid)
                )
                note = (
                    "Balance correction: migrated to flat $8 per $100 net-deposit "
                    "profit basis (previously compounded daily)."
                )
                cur.execute(
                    "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
                    "reference,status,note,created_at,completed_at) "
                    "VALUES(%s,%s,%s,'ADJUSTMENT','MIGRATION',%s,%s,'COMPLETED',%s,%s,%s)",
                    (_uid(), uid, c["account_id"], abs(delta),
                     "MIG-" + secrets.token_hex(4).upper(), note, _now(), _now())
                )
                for row in trade_log_rows:
                    cur.execute(
                        "UPDATE daily_trade_log SET profit=%s WHERE id=%s",
                        (flat_daily, row["id"])
                    )

                direction = "reduced" if delta < 0 else "increased"
                notif_msg = (
                    f"We've updated how daily profit is calculated: it's now a flat "
                    f"${DAILY_PROFIT_PER_100:.0f} per $100 you've deposited each day, "
                    f"instead of compounding on your growing balance. As part of this "
                    f"change your balance has been {direction} by ${abs(delta):,.2f} "
                    f"to reflect the corrected amount. Your new balance is "
                    f"${c['balance'] + delta:,.2f}. See your Transactions tab for the "
                    f"full adjustment record."
                )
                create_notification(
                    cur, uid,
                    "Account balance updated — profit calculation correction",
                    notif_msg,
                    "ALERT" if delta < 0 else "INFO"
                )
                conn.commit()
                entry["applied"] = True
            except Exception as e:
                conn.rollback()
                entry["applied"] = False
                entry["error"] = str(e)

    cur.close(); conn.close()

    return ok({
        "mode": "APPLIED" if apply else "DRY_RUN",
        "clients_needing_correction": len([r for r in results if abs(r["delta"]) >= 0.01]),
        "total_delta": round(total_delta, 2),
        "results": results,
    })

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

# ── ADMIN NOTIFICATIONS (v5.12) ───────────────────────────────────────────────
@app.route("/api/admin/notifications/send", methods=["POST"])
@admin_required
def admin_send_notification():
    """
    Send a notification either to one client or broadcast to all clients.
    Body: { "user_id": "<id>" }  -> single client
          { "broadcast": true }  -> every client
          "title": str, "message": str, "type": "INFO"|"WARNING"|"ALERT" (optional)
    """
    d       = request.json or {}
    title   = d.get("title","").strip()
    message = d.get("message","").strip()
    ntype   = d.get("type","INFO").upper()
    user_id = d.get("user_id","").strip()
    broadcast = bool(d.get("broadcast", False))

    if not title or not message:
        return err("title and message are required")
    if not broadcast and not user_id:
        return err("Provide either user_id or broadcast=true")

    conn = get_db()
    cur  = conn.cursor()

    if broadcast:
        cur.execute("SELECT id FROM users WHERE role='client'")
        targets = [r["id"] for r in cur.fetchall()]
    else:
        cur.execute("SELECT id FROM users WHERE id=%s AND role='client'", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return err("Client not found", 404)
        targets = [row["id"]]

    for uid in targets:
        create_notification(cur, uid, title, message, ntype)
    conn.commit()
    cur.close(); conn.close()
    return ok({"message": f"Notification sent to {len(targets)} client(s)"})

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
        "profit_basis":      "flat $8 per $100 net deposited (non-compounding)",
        "min_balance":       MIN_BALANCE,
        "symbol":            TRADE_SYMBOL,
    })

# ── STARTUP ───────────────────────────────────────────────────────────────────
init_db()
start_scheduler()

if __name__ == "__main__":
    print("\n" + "="*60)
    print("   Summit Wealth v5.13 — $8 FLAT PROFIT PER $100 NET DEPOSITED")
    print("="*60)
    print(f"   URL    : http://127.0.0.1:8080")
    print(f"   Client : john@test.com  / demo1234")
    print(f"   Admin  : admin@test.com / admin1234")
    print(f"   Rate   : ${DAILY_PROFIT_PER_100} per $100 net deposited/day (flat, non-compounding) at {TRADE_HOUR:02d}:00 UTC ({TRADE_HOUR+3:02d}:00 EAT)")
    print(f"   Symbol : {TRADE_SYMBOL}")
    print(f"   Min Dep: $100  |  Min Withdrawal: $1,000  |  Ref Withdrawal: $16")
    print(f"   Binance: {'CONNECTED ✓' if bnb else 'fallback prices'}")
    print(f"   TRC20  : {'SET ✓' if MANUAL_WALLETS.get('TRC20') else 'NOT SET ✗'}")
    print(f"   M-Pesa : STK Push | Env: {MPESA_ENV} | Shortcode: {MPESA_SHORTCODE}")
    print(f"   KES/USD: {KES_PER_USD} | Callback: {MPESA_CALLBACK_URL}")
    print(f"   Forgot Password: /forgot-password (email+phone+PIN verification)")
    print("="*60 + "\n")
    app.run(debug=False, port=8080, host="0.0.0.0")
