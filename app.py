"""
Summit Wealth v5.13 - $4.5 PROFIT PER $100 BALANCE (4.5% daily)
- Scheduler starts at module level (works with gunicorn on Render)
- One controlled trade per client per day
- Trade profit scales: $4.5 per $100 of balance
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
- FIX v5.14: Startup banner incorrectly printed "Min Withdrawal: $1,000"
            even though /api/client/withdraw has always enforced a $10
            minimum. Banner text corrected to match actual enforced value
            (no behavior change).
- NEW v5.15: Added POST /api/admin/trade/run-single — lets an admin backfill
            today's trade for ONE specific client (e.g. if the scheduled
            run was skipped for them due to a DB outage). Uses the same
            eligibility/payout logic as run_daily_trades(), scoped to a
            single user_id, and is idempotent via the same
            UNIQUE(user_id, date) constraint.
- FIX v5.16: PROFIT BASIS DECOUPLED FROM ELIGIBILITY MINIMUM — profit rate
            is now $4.5 per $90 of net deposit (was $4.5 per $100). The
            minimum net deposit required to be eligible for daily trades
            remains $100 (MIN_BALANCE, unchanged) — these were previously
            tied together by both hardcoding "100.0"; they are now two
            independent constants (MIN_BALANCE for eligibility,
            PROFIT_BASIS_USD for the profit-per-unit divisor).
- NEW v5.17: Added GET /api/client/balance/history — a server-computed,
            chronological running balance per client, built directly from
            their own completed DEPOSIT/WITHDRAWAL/ADJUSTMENT transactions
            plus daily_trade_log profit rows (the dashboard's "Balance Over
            Time" chart now calls this instead of reconstructing balance
            client-side in JS). Also fixed admin_adjust_balance and the
            flat-profit migration endpoint, which previously stored
            ADJUSTMENT amounts as abs(amount) — losing the direction of
            negative corrections and silently breaking any balance
            reconstruction. Both now store the true signed amount.
- FIX v5.18: CRITICAL — net_deposit (deposits minus WITHDRAWAL-type
            transactions) could go negative for any long-tenured, profitable
            client, because withdrawing already-earned PROFIT is recorded
            as the same 'WITHDRAWAL' transaction type as withdrawing
            principal — there's no way to tell them apart at the database
            level. Once a client's lifetime withdrawals exceeded their
            lifetime deposits (which happens naturally as profit
            accumulates and gets cashed out), net_deposit went negative and
            STAYED negative — a fresh deposit only nudged the number
            slightly, so the client could remain permanently below
            MIN_BALANCE and locked out of daily trades no matter how much
            they kept depositing, even though their real account balance
            was healthy. net_deposit is now floored at $0 everywhere it's
            computed (run_daily_trades, client_summary,
            admin_run_single_client_trade, admin_migrate_flat_profit) via
            SQL GREATEST(0, ...) / Python max(0.0, ...), so a new deposit
            always restores eligibility as expected.
- CHANGE v5.19: SUPERSEDES v5.18's floor-at-zero approach — withdrawals no
            longer factor into the trade eligibility/profit basis AT ALL.
            The basis (still called net_deposit in code/API for backward
            compatibility) is now simply a client's GROSS total completed
            deposits. This was requested directly: eligibility should be
            based on total deposits only, full stop — the only guard is a
            defensive check that refuses to trade a client whose deposit
            total is somehow negative (which should never happen under
            normal operation, since deposit amounts are always stored
            positive), logging a warning and skipping them rather than
            crediting profit off a nonsensical number.
- CHANGE v5.20: Trade eligibility now ALSO requires the client's CURRENT
            account balance to be >= MIN_BALANCE, in addition to gross total
            deposits >= MIN_BALANCE (from v5.19). Closes the gap where a
            client could deposit once, withdraw the full principal back out,
            and keep earning daily profit indefinitely off a deposit total
            that no longer reflects any real money in their account.
            Applied in run_daily_trades(), admin_run_single_client_trade(),
            and client_summary()'s daily_profit preview. The
            admin_migrate_flat_profit() historical-correction tool is
            intentionally left on deposit-total-only logic, since it can't
            know a client's balance at each past trade date, only today's.
- CHANGE v5.21: SUPERSEDES v5.16/v5.19/v5.20's deposit-based profit basis —
            reverted to the ORIGINAL design stated at the top of this file:
            profit is $4.5 per $100 of a client's CURRENT ACCOUNT BALANCE
            (PROFIT_BASIS_USD default back to 100.0), not gross/net deposit
            total. This is intentionally compounding — as profit is credited
            to balance, the next day's profit is calculated off the new,
            larger balance. This was requested directly, reversing the
            "flat, non-compounding" rationale from v5.11. Eligibility is
            now simply CURRENT balance >= MIN_BALANCE — the separate
            deposit-total eligibility check from v5.19/v5.20 no longer
            applies, since deposit total is no longer the profit basis.
            Applied in run_daily_trades(), admin_run_single_client_trade(),
            and client_summary()'s daily_profit preview.
            admin_migrate_flat_profit() is a historical-correction tool for
            the earlier deposit-based migration and is intentionally left
            untouched — it is unrelated to this change and not expected to
            be run again under the current (balance-based) formula.
- CHANGE v5.22: Profit now only counts WHOLE $100 units of balance — a
            partial remainder below $100 earns nothing extra. Previously
            $613.11 was treated as 6.1311 units (profit = 613.11/100*4.5 =
            $27.59); now it's floor(613.11/100) = 6 whole units, so
            profit = 6*4.5 = $27.00 — the $13.11 remainder is simply not
            counted until it grows into a full $100. Applied via
            math.floor() in run_daily_trades(), admin_run_single_client_
            trade(), and client_summary()'s daily_profit preview. Trade
            eligibility (balance >= MIN_BALANCE) is unchanged.
- CHANGE v5.23: SUPERSEDES v5.21/v5.22's balance-based, compounding basis —
            profit basis is back to a client's GROSS TOTAL COMPLETED
            DEPOSITS (not current balance — balance is no longer read for
            the profit calculation at all, only for display and for the
            $10/withdrawal-amount check). The whole-$100-unit floor from
            v5.22 is kept: only complete $100 units of the deposit total
            count (e.g. $450 deposited = 4 units = $18.00/day, not
            $20.25). Eligibility is simply gross total deposits >=
            MIN_BALANCE. This was requested directly, along with fixing
            already-logged trades that were credited under the v5.21/v5.22
            balance-based formula: run POST /api/admin/migrate/flat-profit
            (dry run by default, {"apply": true} to commit) to recompute
            every client's historical daily_trade_log rows under this
            deposit-based formula and correct their account balance to
            match — this is the tool to use for a client who, e.g., has 4
            days of trades logged at the wrong amount.
- CHANGE v5.24: SUPERSEDES v5.23's gross-deposit-only basis — the profit
            basis is now (total completed DEPOSITs − total completed
            principal WITHDRAWALs), floored at $0, computed fresh from the
            database on every trade run. REFERRAL_WITHDRAWAL is still
            excluded (draws only from ref_balance). This was requested
            directly: withdrawing deposited principal should immediately
            stop that money from generating further profit. Because the
            basis is recalculated live (not cached) every time
            run_daily_trades()/admin_run_single_client_trade() executes,
            the effect is immediate — the moment a withdrawal transaction
            flips to COMPLETED (on admin approval), the very next trade
            run reflects the reduced basis, with no separate "close the
            trade" action needed. Applied in run_daily_trades(),
            admin_run_single_client_trade(), client_summary(), and
            admin_migrate_flat_profit() (for consistency, so the migration
            tool corrects historical balances under the same formula the
            live engine now uses).
- NEW v5.25: Added POST /api/admin/client/<uid>/correct-profit — a
            single-client-scoped counterpart to admin_migrate_flat_profit().
            Recomputes ONLY the given client's daily_trade_log rows and
            balance under the current live formula (v5.24). Dry run by
            default; {"apply": true} to commit. This is what the admin
            dashboard's per-client "Fix Profit" button calls — it never
            reads or writes any other client's data, unlike the bulk
            migration endpoint.
- FIX v5.26: The admin dashboard's "Run Missed Trade" modal displayed a
            misleading "Net Deposit" figure computed client-side as
            total_deposits − total_withdrawals, where total_withdrawals
            (from admin_clients()) combines WITHDRAWAL and
            REFERRAL_WITHDRAWAL together. A client who had only ever
            withdrawn referral commissions (never touching trading
            principal) would show a deeply negative figure there and
            appear ineligible, even though the actual backend eligibility
            check (run_daily_trades()/admin_run_single_client_trade(),
            both already correct since v5.24) only ever subtracts
            PRINCIPAL withdrawals and was never actually affected. Added a
            dedicated trading_basis field to admin_clients() — GREATEST(0,
            deposits − principal-only withdrawals) — so the dashboard no
            longer has to (mis)reconstruct this figure itself; it now
            reads that field directly. total_principal_withdrawals is also
            now exposed separately from the combined total_withdrawals
            figure (which stays combined, since that's correct for the
            accounting display in the Clients table).
"""

import os, hashlib, secrets, datetime, uuid, logging, threading, random, base64, math
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

DAILY_PROFIT_PER_100 = float(os.environ.get("DAILY_PROFIT_USD", "4.5"))
PROFIT_BASIS_USD     = float(os.environ.get("PROFIT_BASIS_USD", "100.0"))
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

_trade_run_lock = threading.Lock()

def run_daily_trades():
    if not _trade_run_lock.acquire(blocking=False):
        log.warning("run_daily_trades already in progress on this worker — skipping overlapping call")
        return

    try:
        today = _today()
        log.info(f"=== Daily trade run: {today} — ${DAILY_PROFIT_PER_100} profit per ${PROFIT_BASIS_USD:.0f} total deposited ===")

        price       = get_live_price(TRADE_SYMBOL)
        pct_gain    = random.uniform(0.003, 0.005)
        close_price = round(price * (1 + pct_gain), 2)
        price_diff  = close_price - price

        if price_diff <= 0:
            log.error("Price diff is zero — aborting.")
            return

        log.info(f"  Entry: ${price:,.2f} | Close: ${close_price:,.2f} | Rate: ${DAILY_PROFIT_PER_100}/${PROFIT_BASIS_USD:.0f}")

        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT u.id, u.name, a.id AS account_id, a.balance, "
            "  GREATEST(0, "
            "    COALESCE((SELECT SUM(amount_usd) FROM transactions "
            "              WHERE user_id=u.id AND type='DEPOSIT' AND status='COMPLETED'), 0) "
            "    - COALESCE((SELECT SUM(amount_usd) FROM transactions "
            "                WHERE user_id=u.id AND type='WITHDRAWAL' AND status='COMPLETED'), 0) "
            "  ) AS total_deposit "
            "FROM users u JOIN accounts a ON u.id=a.user_id "
            "WHERE u.role='client'"
        )
        all_clients = cur.fetchall()
        # Profit basis is TOTAL DEPOSITS MINUS COMPLETED WITHDRAWALS, floored
        # at $0 (v5.24). This is computed fresh from the database every time
        # a trade runs, so the moment a withdrawal is approved (status flips
        # to COMPLETED and the money leaves the account), the very next
        # trade run — scheduled or manual — immediately reflects the
        # reduced basis. A client who withdraws their full deposited amount
        # drops straight to $0 basis and stops earning profit from that
        # point on, with no lag. REFERRAL_WITHDRAWAL is intentionally
        # excluded — it only ever draws from ref_balance, never principal.
        # Current balance is still not read for the calculation itself.
        clients = [c for c in all_clients if c["total_deposit"] >= MIN_BALANCE]
        log.info(f"  Eligible clients (deposits - withdrawals >= ${MIN_BALANCE}): {len(clients)}")
        paid = 0

        for c in clients:
            # Only whole $100 units of (deposits - withdrawals) count — a
            # partial remainder below $100 does not earn a partial profit.
            client_profit   = round(math.floor(c["total_deposit"] / PROFIT_BASIS_USD) * DAILY_PROFIT_PER_100, 2)
            client_quantity = round(client_profit / price_diff, 6)

            now              = datetime.datetime.utcnow()
            open_minutes_ago = random.randint(30, 90)
            opened_at        = (now - datetime.timedelta(minutes=open_minutes_ago)).isoformat()
            closed_at        = now.isoformat()
            trade_id         = _uid()
            sl               = round(price * 0.985, 2)
            tp               = close_price

            try:
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
                log.info(f"  ✓ {c['name']}: +${client_profit} (total deposit: ${c['total_deposit']:,.2f}, bal: ${c['balance']:,.2f})")

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

    balance      = a["balance"] if a else 0
    # Profit basis is TOTAL DEPOSITS MINUS COMPLETED WITHDRAWALS, floored at
    # $0 (v5.24) — matches run_daily_trades()/admin_run_single_client_trade().
    # Current balance is not used for this calculation, only displayed above.
    net_deposit  = max(0.0, dep["s"] - principal_wdr["s"])
    # Only whole $100 units of the basis count toward profit — a
    # remainder below $100 doesn't earn a partial amount.
    expected_daily = round(math.floor(net_deposit / PROFIT_BASIS_USD) * DAILY_PROFIT_PER_100, 2) if net_deposit >= MIN_BALANCE else 0

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

@app.route("/api/client/balance/history")
@login_required
def client_balance_history():
    """
    Server-computed, chronological running balance for THIS client, built
    directly from their own completed transactions (DEPOSIT +, WITHDRAWAL -,
    ADJUSTMENT signed) plus daily_trade_log profit entries. Replaces the old
    approach of reconstructing balance client-side in the dashboard JS,
    which could drift (e.g. ADJUSTMENT used to store an unsigned amount).
    REFERRAL_WITHDRAWAL is intentionally excluded — it only ever draws from
    ref_balance, never the main trading balance.
    """
    uid  = session["user_id"]
    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT type, amount_usd, created_at, completed_at FROM transactions "
        "WHERE user_id=%s AND type IN ('DEPOSIT','WITHDRAWAL','ADJUSTMENT') "
        "AND status='COMPLETED'",
        (uid,)
    )
    txs = cur.fetchall()

    cur.execute(
        "SELECT profit, created_at FROM daily_trade_log WHERE user_id=%s",
        (uid,)
    )
    trades = cur.fetchall()

    cur.execute("SELECT balance FROM accounts WHERE user_id=%s", (uid,))
    acct = cur.fetchone()
    current_balance = acct["balance"] if acct else 0

    cur.close()
    conn.close()

    events = []
    for t in txs:
        ts = t["completed_at"] or t["created_at"]
        if t["type"] == "DEPOSIT":
            delta = t["amount_usd"]
        elif t["type"] == "WITHDRAWAL":
            delta = -t["amount_usd"]
        else:  # ADJUSTMENT — amount_usd is stored signed at source
            delta = t["amount_usd"]
        events.append((ts or "", delta))

    for p in trades:
        events.append((p["created_at"] or "", p["profit"]))

    events.sort(key=lambda e: e[0])

    running = 0.0
    points  = []
    for ts, delta in events:
        running = round(running + delta, 2)
        points.append({"date": ts[:10] if ts else "", "timestamp": ts, "balance": running})

    # Always end on the true stored balance, so any legacy rows recorded
    # before signed ADJUSTMENT amounts existed can't leave the chart's
    # final point drifting from the client's real, current balance.
    points.append({"date": "Now", "timestamp": _now(), "balance": current_balance})

    return ok(points)

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
    d          = request.json or {}
    uid        = session["user_id"]
    amount_kes = float(d.get("amount", 0))
    phone      = d.get("phone_number", "").strip()

    if amount_kes < 12952.2169:
        return err("Minimum deposit is KES 12952.2169")
    if not phone:
        return err("Phone number is required")

    phone_fmt = mpesa_format_phone(phone)
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

    cur.execute(
        "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
        "reference,status,note,deposit_address,created_at) "
        "VALUES(%s,%s,%s,'DEPOSIT','MPESA',%s,%s,'PENDING',%s,%s,%s)",
        (tx_id, uid, acct["id"] if acct else None,
         amount_usd, ref, note, phone_fmt, _now())
    )
    conn.commit()

    push_ok, result = mpesa_stk_push(
        phone       = phone,
        amount_kes  = int(amount_kes),
        account_ref = ref,
        description = "Summit Deposit"
    )

    if not push_ok:
        cur.execute(
            "UPDATE transactions SET status='REJECTED', note=%s WHERE id=%s",
            (f"STK Push failed: {result}", tx_id)
        )
        conn.commit()
        cur.close(); conn.close()
        return err(result)

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
        return ok({"status": "PENDING"})

    mpesa_code = ""
    if tx["status"] == "COMPLETED" and tx["note"] and "MpesaRef:" in tx["note"]:
        try:
            mpesa_code = tx["note"].split("MpesaRef:")[-1].strip().split(" ")[0]
        except Exception:
            pass

    return ok({
        "status":     tx["status"],
        "amount":     tx["amount_usd"],
        "mpesa_code": mpesa_code,
        "reference":  tx["reference"],
        "message":    tx["note"] or ""
    })


# ── M-PESA CALLBACK (Daraja → this endpoint) ─────────────────────────────────
@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
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

    if amt < 10:            return err("Minimum withdrawal is $10")
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
        "(SELECT COALESCE(SUM(amount_usd),0) FROM transactions "
        " WHERE user_id=u.id AND type='WITHDRAWAL' AND status='COMPLETED') AS total_principal_withdrawals, "
        "GREATEST(0, "
        "  (SELECT COALESCE(SUM(amount_usd),0) FROM transactions "
        "   WHERE user_id=u.id AND type='DEPOSIT' AND status='COMPLETED') "
        "  - (SELECT COALESCE(SUM(amount_usd),0) FROM transactions "
        "     WHERE user_id=u.id AND type='WITHDRAWAL' AND status='COMPLETED') "
        ") AS trading_basis, "
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
    """
    FIX v5.27: This endpoint previously ignored the "field" selector the
    admin dashboard's Adjust Balance form sends entirely — no matter which
    field was chosen (Balance, Total Deposits, Total Withdrawals, etc.), it
    always just bumped accounts.balance/equity and logged a generic
    ADJUSTMENT transaction. That meant "adjusting Total Deposits" silently
    did NOT create anything the deposits-based trading_basis/eligibility
    calculation could see (those are computed from SUM(type='DEPOSIT')
    transactions, not from balance) — a client could show a healthy
    balance while still having $0 in real recorded deposits, and get
    blocked from trading with no visible explanation. Now:
      - field='balance' / 'equity' : same as before (touches both, an
        ADJUSTMENT transaction is logged for the audit trail).
      - field='total_deposits'     : inserts a real COMPLETED DEPOSIT
        transaction (so it counts toward trading_basis) and credits
        balance/equity to match, since a real deposit would do both.
      - field='total_withdrawals'  : inserts a real COMPLETED WITHDRAWAL
        transaction (so it reduces trading_basis, matching a real
        withdrawal) and debits balance/equity to match.
      - field='ref_balance'        : adjusts the withdrawable referral
        balance directly (unchanged from before).
      - field='total_profit'       : inserts a daily_trade_log row dated
        today (blocked with a clear error if today's slot is already
        taken — use the "Fix Profit" tool for correcting existing days
        instead) and credits balance/equity to match.
      - field='total_ref_earned'   : rejected with a clear error — it's a
        sum of individual referral-commission records tied to a specific
        referred user, which this generic tool has no sane way to
        fabricate. Adjust ref_balance instead if the goal is to change
        withdrawable referral funds.
    Both total_deposits and total_withdrawals adjustments require a
    positive amount — use the other field to represent a decrease, same
    as how real deposits/withdrawals are always positive amounts.
    """
    d      = request.json or {}
    amount = float(d.get("amount", 0))
    note   = d.get("note", "Admin adjustment")
    field  = d.get("field", "balance")
    if amount == 0:
        return err("Amount cannot be zero")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE user_id=%s", (uid,))
    a = cur.fetchone()
    if not a:
        cur.close(); conn.close()
        return err("Account not found")

    ref = "ADJ-" + secrets.token_hex(4).upper()

    if field in ("balance", "equity"):
        cur.execute(
            "UPDATE accounts SET balance=balance+%s,equity=equity+%s WHERE user_id=%s",
            (amount, amount, uid)
        )
        cur.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,created_at,completed_at) "
            "VALUES(%s,%s,%s,'ADJUSTMENT','MANUAL',%s,%s,'COMPLETED',%s,%s,%s)",
            (_uid(), uid, a["id"], amount, ref, note, _now(), _now())
        )
        conn.commit()
        cur.close(); conn.close()
        return ok({"message": f"Balance adjusted by {amount:+.2f}"})

    if field == "ref_balance":
        cur.execute(
            "UPDATE accounts SET ref_balance=ref_balance+%s WHERE user_id=%s",
            (amount, uid)
        )
        cur.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,created_at,completed_at) "
            "VALUES(%s,%s,%s,'ADJUSTMENT','MANUAL',%s,%s,'COMPLETED',%s,%s,%s)",
            (_uid(), uid, a["id"], amount, ref, note, _now(), _now())
        )
        conn.commit()
        cur.close(); conn.close()
        return ok({"message": f"Referral balance adjusted by {amount:+.2f}"})

    if field == "total_deposits":
        if amount < 0:
            cur.close(); conn.close()
            return err("Total Deposits adjustments must be a positive amount — use Total Withdrawals to record a reduction instead")
        cur.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,created_at,completed_at) "
            "VALUES(%s,%s,%s,'DEPOSIT','MANUAL',%s,%s,'COMPLETED',%s,%s,%s)",
            (_uid(), uid, a["id"], amount, ref, note, _now(), _now())
        )
        cur.execute(
            "UPDATE accounts SET balance=balance+%s,equity=equity+%s WHERE user_id=%s",
            (amount, amount, uid)
        )
        conn.commit()
        cur.close(); conn.close()
        return ok({"message": f"Recorded a ${amount:.2f} deposit correction — now counts toward trading eligibility, and balance credited to match"})

    if field == "total_withdrawals":
        if amount < 0:
            cur.close(); conn.close()
            return err("Total Withdrawals adjustments must be a positive amount — use Total Deposits to record an increase instead")
        cur.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,created_at,completed_at) "
            "VALUES(%s,%s,%s,'WITHDRAWAL','MANUAL',%s,%s,'COMPLETED',%s,%s,%s)",
            (_uid(), uid, a["id"], amount, ref, note, _now(), _now())
        )
        cur.execute(
            "UPDATE accounts SET balance=balance-%s,equity=equity-%s WHERE user_id=%s",
            (amount, amount, uid)
        )
        conn.commit()
        cur.close(); conn.close()
        return ok({"message": f"Recorded a ${amount:.2f} withdrawal correction — now reduces trading eligibility, and balance debited to match"})

    if field == "total_profit":
        today = _today()
        cur.execute("SELECT 1 FROM daily_trade_log WHERE user_id=%s AND date=%s", (uid, today))
        if cur.fetchone():
            cur.close(); conn.close()
            return err(f"This client already has a trade logged for {today} — use the Fix Profit tool to correct existing profit history instead of adding a new day")
        cur.execute(
            "INSERT INTO daily_trade_log(id,user_id,account_id,trade_id,profit,date,created_at) "
            "VALUES(%s,%s,%s,NULL,%s,%s,%s)",
            (_uid(), uid, a["id"], amount, today, _now())
        )
        cur.execute(
            "UPDATE accounts SET balance=balance+%s,equity=equity+%s WHERE user_id=%s",
            (amount, amount, uid)
        )
        cur.execute(
            "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
            "reference,status,note,created_at,completed_at) "
            "VALUES(%s,%s,%s,'ADJUSTMENT','MANUAL',%s,%s,'COMPLETED',%s,%s,%s)",
            (_uid(), uid, a["id"], amount, ref, note, _now(), _now())
        )
        conn.commit()
        cur.close(); conn.close()
        return ok({"message": f"Recorded a ${amount:.2f} profit correction for {today}, and balance credited to match"})

    if field == "total_ref_earned":
        cur.close(); conn.close()
        return err("Total Ref Earned can't be adjusted directly — it's the sum of individual referral commission records tied to specific referred users. Adjust Referral Balance instead if the goal is to change withdrawable referral funds.")

    cur.close(); conn.close()
    return err(f"Unknown field: {field}")
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

# ── ADMIN: RUN MISSED TRADE FOR ONE CLIENT (v5.15 — NEW) ──────────────────────
@app.route("/api/admin/trade/run-single", methods=["POST"])
@admin_required
def admin_run_single_client_trade():
    """
    Backfill today's trade for ONE client who was skipped by the scheduled
    run (e.g. due to a DB outage during the trigger window). Uses the same
    eligibility/payout logic as run_daily_trades(), scoped to a single
    user_id. Idempotent via the same UNIQUE(user_id, date) constraint on
    daily_trade_log — calling it twice for the same client/day is a no-op
    with a clear error, not a double payout.

    Body: { "user_id": "<id>" }
    """
    d   = request.json or {}
    uid = d.get("user_id", "").strip()
    if not uid:
        return err("user_id required")

    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT u.id, u.name, a.id AS account_id, a.balance, "
        "  GREATEST(0, "
        "    COALESCE((SELECT SUM(amount_usd) FROM transactions "
        "              WHERE user_id=u.id AND type='DEPOSIT' AND status='COMPLETED'), 0) "
        "    - COALESCE((SELECT SUM(amount_usd) FROM transactions "
        "                WHERE user_id=u.id AND type='WITHDRAWAL' AND status='COMPLETED'), 0) "
        "  ) AS total_deposit "
        "FROM users u JOIN accounts a ON u.id=a.user_id "
        "WHERE u.id=%s AND u.role='client'", (uid,)
    )
    c = cur.fetchone()
    if not c:
        cur.close(); conn.close()
        return err("Client not found", 404)

    # Profit basis is deposits minus completed withdrawals, floored at $0
    # (v5.24) — see note at top of file.
    if c["total_deposit"] < MIN_BALANCE:
        cur.close(); conn.close()
        return err(f"Client's deposits minus withdrawals (${c['total_deposit']:.2f}) is below minimum (${MIN_BALANCE})")

    today = _today()

    cur.execute(
        "SELECT 1 FROM daily_trade_log WHERE user_id=%s AND date=%s", (uid, today)
    )
    if cur.fetchone():
        cur.close(); conn.close()
        return err(f"{c['name']} already has a trade logged for {today}")

    price       = get_live_price(TRADE_SYMBOL)
    pct_gain    = random.uniform(0.003, 0.005)
    close_price = round(price * (1 + pct_gain), 2)
    price_diff  = close_price - price
    if price_diff <= 0:
        cur.close(); conn.close()
        return err("Price diff was zero — try again")

    # Only whole $100 units of TOTAL DEPOSITS count — a partial remainder
    # below $100 does not earn a partial profit.
    client_profit   = round(math.floor(c["total_deposit"] / PROFIT_BASIS_USD) * DAILY_PROFIT_PER_100, 2)
    client_quantity = round(client_profit / price_diff, 6)

    now              = datetime.datetime.utcnow()
    open_minutes_ago = random.randint(30, 90)
    opened_at        = (now - datetime.timedelta(minutes=open_minutes_ago)).isoformat()
    closed_at        = now.isoformat()
    trade_id         = _uid()
    sl               = round(price * 0.985, 2)
    tp               = close_price

    try:
        cur.execute(
            "INSERT INTO daily_trade_log(id,user_id,account_id,trade_id,"
            "profit,date,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, date) DO NOTHING",
            (_uid(), c["id"], c["account_id"], trade_id, client_profit, today, _now())
        )
        if cur.rowcount == 0:
            conn.rollback()
            cur.close(); conn.close()
            return err(f"{c['name']} already traded today (race with another run)")

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
        log.info(f"Manual single-client trade backfill: {c['name']} +${client_profit}")
    except Exception as e:
        conn.rollback()
        cur.close(); conn.close()
        return err(f"Failed to record trade: {e}")

    cur.close(); conn.close()
    return ok({
        "message": f"Trade backfilled for {c['name']}: +${client_profit}",
        "profit": client_profit,
        "total_deposit": c["total_deposit"],
    })

@app.route("/api/admin/trade/run", methods=["POST"])
@admin_required
def admin_run_trades():
    threading.Thread(target=run_daily_trades, daemon=True).start()
    return ok({"message": f"Daily trades triggered — ${DAILY_PROFIT_PER_100} per ${PROFIT_BASIS_USD:.0f} total deposited"})

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

# ── ADMIN: FIX PROFIT — SINGLE CLIENT (v5.25) ─────────────────────────────────
@app.route("/api/admin/client/<uid>/correct-profit", methods=["POST"])
@admin_required
def admin_correct_client_profit(uid):
    """
    Single-client-scoped counterpart to admin_migrate_flat_profit() below.
    Recomputes ONLY this client's daily_trade_log entries and account
    balance under the CURRENT live profit formula (v5.24: for every day
    they have a logged trade, profit = floor((deposits - completed
    principal withdrawals) / PROFIT_BASIS_USD) * DAILY_PROFIT_PER_100).
    No other client's data is read or written by this endpoint — that is
    what distinguishes it from the bulk migration tool.

    Dry run by default (returns what WOULD change, applied=false); pass
    {"apply": true} to actually correct the balance and daily_trade_log
    rows, and notify the client in-platform. This is what the admin
    dashboard's per-client "Fix Profit" button calls.
    """
    d     = request.json or {}
    apply = bool(d.get("apply", False))

    conn = get_db()
    cur  = conn.cursor()

    cur.execute(
        "SELECT u.id, u.name, u.email, a.id AS account_id, a.balance, a.equity "
        "FROM users u JOIN accounts a ON u.id = a.user_id "
        "WHERE u.id=%s AND u.role='client'", (uid,)
    )
    c = cur.fetchone()
    if not c:
        cur.close(); conn.close()
        return err("Client not found", 404)

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

    # Profit basis is deposits minus completed principal withdrawals,
    # floored at $0 (v5.24) — matches the live trade engine.
    total_deposit = max(0.0, deposits - principal_withdrawals)

    cur.execute(
        "SELECT id, profit, date FROM daily_trade_log WHERE user_id=%s ORDER BY date", (uid,)
    )
    trade_log_rows    = cur.fetchall()
    days_traded       = len(trade_log_rows)
    old_total_profit  = round(sum(r["profit"] for r in trade_log_rows), 2)

    if total_deposit < MIN_BALANCE or days_traded == 0:
        flat_daily = 0.0
    else:
        # Only whole $100 units of (deposits - withdrawals) count — matches
        # the live formula's floor() behavior.
        flat_daily = round(math.floor(total_deposit / PROFIT_BASIS_USD) * DAILY_PROFIT_PER_100, 2)

    correct_total_profit = round(flat_daily * days_traded, 2)
    delta = round(correct_total_profit - old_total_profit, 2)

    current_balance = c["balance"]
    new_balance     = round(current_balance + delta, 2)

    per_day = [
        {"date": r["date"], "old_profit": r["profit"], "new_profit": flat_daily}
        for r in trade_log_rows
    ]

    result = {
        "user_id":               uid,
        "name":                  c["name"],
        "email":                 c["email"],
        "total_deposit":         total_deposit,
        "days_traded":           days_traded,
        "flat_daily":            flat_daily,
        "old_total_profit":      old_total_profit,
        "correct_total_profit":  correct_total_profit,
        "delta":                 delta,
        "current_balance":       current_balance,
        "new_balance":           new_balance,
        "per_day":               per_day,
        "applied":               False,
    }

    needs_correction = abs(delta) >= 0.01

    if apply and needs_correction:
        try:
            cur.execute(
                "UPDATE accounts SET balance = balance + %s, equity = equity + %s "
                "WHERE user_id = %s",
                (delta, delta, uid)
            )
            note = (
                f"Balance correction: recomputed under current profit formula "
                f"(${DAILY_PROFIT_PER_100:.1f} per ${PROFIT_BASIS_USD:.0f} of "
                f"deposits minus withdrawals, whole $100 units only)."
            )
            cur.execute(
                "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
                "reference,status,note,created_at,completed_at) "
                "VALUES(%s,%s,%s,'ADJUSTMENT','CORRECTION',%s,%s,'COMPLETED',%s,%s,%s)",
                (_uid(), uid, c["account_id"], delta,
                 "FIX-" + secrets.token_hex(4).upper(), note, _now(), _now())
            )
            for row in trade_log_rows:
                cur.execute(
                    "UPDATE daily_trade_log SET profit=%s WHERE id=%s",
                    (flat_daily, row["id"])
                )

            direction = "reduced" if delta < 0 else "increased"
            notif_msg = (
                f"We've corrected how your daily trading profit is calculated: it's "
                f"now ${DAILY_PROFIT_PER_100:.1f} per ${PROFIT_BASIS_USD:.0f} of your "
                f"deposits minus withdrawals (whole $100 units only). As part of this "
                f"correction your balance has been {direction} by ${abs(delta):,.2f}. "
                f"Your new balance is ${new_balance:,.2f}. See your Transactions tab "
                f"for the full adjustment record."
            )
            create_notification(
                cur, uid,
                "Account balance updated — profit calculation correction",
                notif_msg,
                "ALERT" if delta < 0 else "INFO"
            )
            conn.commit()
            result["applied"] = True
            log.info(f"Single-client profit correction applied: {c['name']} {delta:+.2f}")
        except Exception as e:
            conn.rollback()
            cur.close(); conn.close()
            return err(f"Failed to apply correction: {e}")
    elif apply:
        # Nothing needed — already matches the current formula. Report
        # success with applied=true so the dashboard doesn't show an error
        # for a no-op correction.
        result["applied"] = True

    cur.close(); conn.close()
    return ok(result)

@app.route("/api/admin/migrate/flat-profit", methods=["POST"])
@admin_required
def admin_migrate_flat_profit():
    """
    ⚠️ DANGEROUS — BULK, ALL-CLIENTS, WHOLE-HISTORY REWRITE. ⚠️
    Prefer POST /api/admin/client/<uid>/correct-profit for routine fixes —
    it's scoped to one client and can never touch anyone else's data.

    Recomputes EVERY client's ENTIRE historical daily_trade_log under the
    CURRENT profit formula, as if that formula had always been in effect.
    Because this platform's formula has changed multiple times, a client's
    early days were very likely credited under a DIFFERENT, legitimate
    formula at the time — this endpoint has no awareness of that and will
    overwrite those rows too, not just the ones that were actually wrong.
    Applying this against the whole client base at once can (and has)
    produced large, unwanted balance swings for long-tenured clients whose
    history spans several formula changes.

    Kept only for reference / rare full-platform resets where you
    genuinely want every client's whole history recomputed under today's
    rate. For "this one client's balance looks wrong," use the scoped
    single-client endpoint above instead.

    Dry run by default (shows what WOULD change); pass {"apply": true} to
    actually correct balances and daily_trade_log rows, and notify each
    affected client in-platform.
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

        # Profit basis is deposits minus completed principal withdrawals,
        # floored at $0 (v5.24) — matches the live trade engine.
        total_deposit = max(0.0, deposits - principal_withdrawals)

        cur.execute(
            "SELECT id, profit FROM daily_trade_log WHERE user_id=%s ORDER BY date", (uid,)
        )
        trade_log_rows    = cur.fetchall()
        days_traded        = len(trade_log_rows)
        old_total_profit   = round(sum(r["profit"] for r in trade_log_rows), 2)

        cur.execute(
            "SELECT COUNT(*) AS c FROM transactions "
            "WHERE user_id=%s AND type IN ('DEPOSIT','WITHDRAWAL') AND status='COMPLETED'", (uid,)
        )
        mixed_history = cur.fetchone()["c"] > 1

        if total_deposit < MIN_BALANCE or days_traded == 0:
            flat_daily = 0.0
        else:
            # Only whole $100 units of (deposits - withdrawals) count
            # (matches the live formula's floor behavior).
            flat_daily = round(math.floor(total_deposit / PROFIT_BASIS_USD) * DAILY_PROFIT_PER_100, 2)

        correct_total_profit = round(flat_daily * days_traded, 2)
        delta = round(correct_total_profit - old_total_profit, 2)

        entry = {
            "user_id": uid, "name": c["name"], "email": c["email"],
            "total_deposit": total_deposit, "days_traded": days_traded,
            "flat_daily_under_current_formula": flat_daily,
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
                    f"Balance correction: recomputed under current profit formula "
                    f"(${DAILY_PROFIT_PER_100:.1f} per ${PROFIT_BASIS_USD:.0f} of total "
                    f"deposits, whole $100 units only)."
                )
                cur.execute(
                    "INSERT INTO transactions(id,user_id,account_id,type,method,amount_usd,"
                    "reference,status,note,created_at,completed_at) "
                    "VALUES(%s,%s,%s,'ADJUSTMENT','MIGRATION',%s,%s,'COMPLETED',%s,%s,%s)",
                    (_uid(), uid, c["account_id"], delta,
                     "MIG-" + secrets.token_hex(4).upper(), note, _now(), _now())
                )
                for row in trade_log_rows:
                    cur.execute(
                        "UPDATE daily_trade_log SET profit=%s WHERE id=%s",
                        (flat_daily, row["id"])
                    )

                direction = "reduced" if delta < 0 else "increased"
                notif_msg = (
                    f"We've corrected how your daily trading profit is calculated: it's "
                    f"now ${DAILY_PROFIT_PER_100:.1f} per ${PROFIT_BASIS_USD:.0f} of your "
                    f"total deposits (whole $100 units only), not your account balance. "
                    f"As part of this correction your balance has been {direction} by "
                    f"${abs(delta):,.2f}. Your new balance is ${c['balance'] + delta:,.2f}. "
                    f"See your Transactions tab for the full adjustment record."
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
        "profit_basis":      f"${DAILY_PROFIT_PER_100:.1f} per ${PROFIT_BASIS_USD:.0f} of (deposits - withdrawals), whole $100 units only, non-compounding",
        "profit_basis_usd":  PROFIT_BASIS_USD,
        "min_balance":       MIN_BALANCE,
        "symbol":            TRADE_SYMBOL,
    })

# ── STARTUP ───────────────────────────────────────────────────────────────────
init_db()
start_scheduler()

if __name__ == "__main__":
    print("\n" + "="*60)
    print("   Summit Wealth v5.26 — $4.5 PROFIT PER $100 OF (DEPOSITS - WITHDRAWALS)")
    print("="*60)
    print(f"   URL    : http://127.0.0.1:8080")
    print(f"   Client : john@test.com  / demo1234")
    print(f"   Admin  : admin@test.com / admin1234")
    print(f"   Rate   : ${DAILY_PROFIT_PER_100} per ${PROFIT_BASIS_USD:.0f} of (deposits - withdrawals)/day (whole $100 units only) at {TRADE_HOUR:02d}:00 UTC ({TRADE_HOUR+3:02d}:00 EAT)")
    print(f"   Eligible min total deposit: ${MIN_BALANCE:.0f}")
    print(f"   Symbol : {TRADE_SYMBOL}")
    print(f"   Min Dep: $100  |  Min Withdrawal: $10  |  Ref Withdrawal: $16")
    print(f"   Binance: {'CONNECTED ✓' if bnb else 'fallback prices'}")
    print(f"   TRC20  : {'SET ✓' if MANUAL_WALLETS.get('TRC20') else 'NOT SET ✗'}")
    print(f"   M-Pesa : STK Push | Env: {MPESA_ENV} | Shortcode: {MPESA_SHORTCODE}")
    print(f"   KES/USD: {KES_PER_USD} | Callback: {MPESA_CALLBACK_URL}")
    print(f"   Forgot Password: /forgot-password (email+phone+PIN verification)")
    print("="*60 + "\n")
    app.run(debug=False, port=8080, host="0.0.0.0")
