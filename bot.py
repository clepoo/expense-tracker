"""
FinBot — Telegram Bot + Web Dashboard
Features: expense logging, edit, recurring management, miles tracker, web dashboard.
"""

import os, json, sqlite3, logging, threading
try:
    import libsql_experimental as libsql
    USE_TURSO = bool(os.environ.get("TURSO_URL"))
except ImportError:
    USE_TURSO = False
from datetime import datetime, date
from anthropic import Anthropic
from flask import Flask, request, session, redirect, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── CONFIG ────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID    = int(os.environ["ALLOWED_USER_ID"])
DB_PATH            = os.environ.get("DB_PATH", "finbot.db")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "finbot123")
FLASK_SECRET       = os.environ.get("FLASK_SECRET", "change-me")
PORT               = int(os.environ.get("PORT", 8080))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── CONSTANTS ─────────────────────────────────────────────────────
CARDS = [
    "CITI REWARDS","HSBC REVO","UOB PPV Contactless","UOB PPV Online",
    "DBS WWMC","OCBC REWARDS","UOB PRIVI","UOB VS SGD","UOB VS FCY","TRUST","Cash",
]
CATEGORIES = [
    "Food","Groceries","Shopping","Transport","Travel",
    "Health, Beauty & Wellness","Entertainment","Bills","Investments","Misc",
]
CAT_EMOJI = {
    "Food":"🍜","Groceries":"🛒","Shopping":"🛍️","Transport":"🚌","Travel":"✈️",
    "Health, Beauty & Wellness":"💊","Entertainment":"🎬","Bills":"📄",
    "Investments":"📈","Misc":"📌","Income":"💰",
}
CARD_EMOJI = {
    "CITI REWARDS":"🔵","HSBC REVO":"🟢","UOB PPV Contactless":"🟣","UOB PPV Online":"🟣",
    "DBS WWMC":"🔴","OCBC REWARDS":"🟡","UOB PRIVI":"🔷","UOB VS SGD":"🟤",
    "UOB VS FCY":"🟤","TRUST":"⬜","Cash":"💵",
}
CARD_COLORS = {
    "CITI REWARDS":"#378ADD","HSBC REVO":"#1D9E75","UOB PPV Contactless":"#7F77DD",
    "UOB PPV Online":"#7F77DD","DBS WWMC":"#E24B4A","OCBC REWARDS":"#BA7517",
    "UOB PRIVI":"#5B8FD4","UOB VS SGD":"#8B6914","UOB VS FCY":"#8B6914",
    "TRUST":"#888780","Cash":"#4A4A45",
}
CAT_COLORS = {
    "Food":"#1D9E75","Groceries":"#0F6E56","Shopping":"#7F77DD","Transport":"#378ADD",
    "Travel":"#D85A30","Health, Beauty & Wellness":"#D4537E","Entertainment":"#BA7517",
    "Bills":"#E24B4A","Investments":"#639922","Misc":"#888780",
}
CARD_CAPS = {
    "CITI REWARDS":        (1000, 4,   0.4,  "Online · statement month"),
    "HSBC REVO":           (1000, 4,   0.4,  "Contactless · calendar month"),
    "UOB PPV Contactless": (600,  4,   2.0,  "$600 cap contactless"),
    "UOB PPV Online":      (600,  4,   2.0,  "$600 cap online"),
    "DBS WWMC":            (1000, 4,   2.0,  "Online only · no Amaze"),
    "OCBC REWARDS":        (1110, 4,   0.4,  "Online MCC only"),
    "UOB PRIVI":           (None, 1.4, 1.4,  "All spend · no cap"),
    "UOB VS SGD":          (1200, 4,   2.0,  "SGD contactless · min $1K"),
    "UOB VS FCY":          (1200, 4,   2.0,  "FCY spend · min $1K"),
}
EDITABLE_FIELDS = {"amount","desc","category","card","date","qualifying","my_amt"}

# ── DATABASE ──────────────────────────────────────────────────────
# libsql_experimental (Turso) quirks vs sqlite3:
#   - row_factory not supported → use cursor.description to build dicts
#   - executescript not supported → use individual execute statements
#   - no sync() needed for remote-only connections

def _rows_to_dicts(cursor, rows):
    """Convert rows to dicts using cursor.description column names."""
    if not rows:
        return []
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in rows]

def _row_to_dict(cursor, row):
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))

def get_conn():
    if USE_TURSO:
        conn = libsql.connect(
            database=os.environ["TURSO_URL"],
            auth_token=os.environ["TURSO_TOKEN"],
        )
    else:
        conn = sqlite3.connect(DB_PATH)
    return conn

def db_commit(conn):
    conn.commit()

def init_db():
    conn = get_conn()
    for stmt in [
        """CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
            desc TEXT NOT NULL, category TEXT NOT NULL, total REAL NOT NULL,
            my_amt REAL NOT NULL, card TEXT NOT NULL,
            qualifying TEXT NOT NULL DEFAULT 'Yes',
            type TEXT NOT NULL DEFAULT 'expense', created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
            desc TEXT NOT NULL, revenue REAL NOT NULL, cost REAL NOT NULL DEFAULT 0,
            profit REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS recurring (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""",
    ]:
        conn.execute(stmt)
    db_commit(conn)
    conn.close()

def insert_transaction(date_, desc, category, total, my_amt, card, qualifying="Yes", typ="expense"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO transactions (date,desc,category,total,my_amt,card,qualifying,type,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (date_, desc, category, round(total,2), round(my_amt,2), card, qualifying, typ, datetime.now().isoformat())
    )
    tid = cur.lastrowid
    db_commit(conn); conn.close()
    return tid

def update_transaction(tid, field, value):
    allowed = {"date","desc","category","total","my_amt","card","qualifying"}
    if field not in allowed:
        return False, f"Cannot edit field '{field}'"
    if field == "card" and value not in CARDS:
        return False, f"Unknown card. Options: {', '.join(CARDS)}"
    if field == "category" and value not in CATEGORIES:
        return False, f"Unknown category. Options: {', '.join(CATEGORIES)}"
    if field in ("total","my_amt"):
        try: value = round(float(value),2)
        except: return False, "Amount must be a number"
    conn = get_conn()
    conn.execute(f"UPDATE transactions SET {field}=? WHERE id=?", (value, tid))
    db_commit(conn); conn.close()
    return True, "Updated"

def fetch_transactions(year=None, month=None, limit=None, typ=None):
    conn = get_conn()
    where, params = [], []
    if year and month:
        where.append("strftime('%Y-%m', date) = ?")
        params.append(f"{year:04d}-{month:02d}")
    if typ:
        where.append("type = ?"); params.append(typ)
    ws = ("WHERE " + " AND ".join(where)) if where else ""
    lim = f"LIMIT {limit}" if limit else ""
    cur = conn.execute(f"SELECT * FROM transactions {ws} ORDER BY date DESC, id DESC {lim}", tuple(params))
    rows = _rows_to_dicts(cur, cur.fetchall())
    conn.close()
    return rows

def get_transaction(tid):
    conn = get_conn()
    cur = conn.execute("SELECT * FROM transactions WHERE id=?", (tid,))
    row = _row_to_dict(cur, cur.fetchone())
    conn.close()
    return row

def delete_transaction(tid):
    conn = get_conn()
    conn.execute("DELETE FROM transactions WHERE id=?", (tid,))
    db_commit(conn); conn.close()
    return True

def get_monthly_summary(year, month):
    conn = get_conn()
    ym = f"{year:04d}-{month:02d}"
    cur = conn.execute("""
        SELECT category, SUM(my_amt) as total FROM transactions
        WHERE strftime('%Y-%m',date)=? AND type='expense'
        GROUP BY category ORDER BY total DESC
    """, (ym,))
    cats = _rows_to_dicts(cur, cur.fetchall())
    cur2 = conn.execute("SELECT SUM(my_amt) FROM transactions WHERE strftime('%Y-%m',date)=? AND type='expense'", (ym,))
    total_exp = (cur2.fetchone()[0] or 0)
    cur3 = conn.execute("SELECT COUNT(*) FROM transactions WHERE strftime('%Y-%m',date)=? AND type='expense'", (ym,))
    count = (cur3.fetchone()[0] or 0)
    cur4 = conn.execute("""
        SELECT card, SUM(total) as total FROM transactions
        WHERE strftime('%Y-%m',date)=? AND type='expense' AND qualifying='Yes'
        GROUP BY card ORDER BY total DESC
    """, (ym,))
    card_spend = _rows_to_dicts(cur4, cur4.fetchall())
    conn.close()
    return cats, total_exp, count, card_spend

def get_available_months():
    conn = get_conn()
    cur = conn.execute("SELECT DISTINCT strftime('%Y-%m',date) as ym FROM transactions ORDER BY ym DESC LIMIT 24")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ── RECURRING DB ──────────────────────────────────────────────────
def get_recurring():
    conn = get_conn()
    cur = conn.execute("SELECT * FROM recurring WHERE active=1 ORDER BY amount DESC")
    rows = _rows_to_dicts(cur, cur.fetchall())
    conn.close()
    return rows

def update_recurring(rid, amount):
    conn = get_conn()
    conn.execute("UPDATE recurring SET amount=? WHERE id=?", (round(float(amount),2), rid))
    db_commit(conn); conn.close()

def toggle_recurring(rid, active):
    conn = get_conn()
    conn.execute("UPDATE recurring SET active=? WHERE id=?", (1 if active else 0, rid))
    db_commit(conn); conn.close()

def add_recurring(name, amount, category):
    conn = get_conn()
    conn.execute("INSERT INTO recurring (name,amount,category,active) VALUES (?,?,?,1)", (name, round(float(amount),2), category))
    db_commit(conn); conn.close()

def delete_recurring(rid):
    conn = get_conn()
    conn.execute("DELETE FROM recurring WHERE id=?", (rid,))
    db_commit(conn); conn.close()

# ── SALES DB ──────────────────────────────────────────────────────
def get_sales(year=None, month=None):
    conn = get_conn()
    where, params = [], []
    if year and month:
        where.append("strftime('%Y-%m',date)=?")
        params.append(f"{year:04d}-{month:02d}")
    ws = ("WHERE " + " AND ".join(where)) if where else ""
    cur = conn.execute(f"SELECT * FROM sales {ws} ORDER BY date DESC, id DESC", tuple(params))
    rows = _rows_to_dicts(cur, cur.fetchall())
    conn.close()
    return rows

def insert_sale(date_, desc, revenue, cost):
    conn = get_conn()
    profit = round(revenue-cost,2)
    conn.execute("INSERT INTO sales (date,desc,revenue,cost,profit,created_at) VALUES (?,?,?,?,?,?)",
                 (date_, desc, round(revenue,2), round(cost,2), profit, datetime.now().isoformat()))
    db_commit(conn); conn.close()

def delete_sale(sid):
    conn = get_conn()
    conn.execute("DELETE FROM sales WHERE id=?", (sid,))
    db_commit(conn); conn.close()
    return True

# ── CLAUDE PARSER ─────────────────────────────────────────────────
PARSE_SYSTEM = f"""You are a finance expense parser for a Singapore user.
Parse the user's message into a JSON expense entry.

Available cards (match case-insensitively): {", ".join(CARDS)}
Available categories: {", ".join(CATEGORIES)}

Card matching — the user will often write abbreviated/lowercase card names, match them:
- "hsbc revo" or "hsbc" → HSBC REVO
- "citi rewards" or "citi" → CITI REWARDS
- "dbs wwmc" or "dbs" or "wwmc" → DBS WWMC
- "ocbc rewards" or "ocbc" → OCBC REWARDS
- "uob ppv contactless" or "ppv contactless" or "ppv" → UOB PPV Contactless
- "uob ppv online" or "ppv online" → UOB PPV Online
- "uob privi" or "privi" → UOB PRIVI
- "uob vs sgd" or "vs sgd" → UOB VS SGD
- "uob vs fcy" or "vs fcy" → UOB VS FCY
- "trust" → TRUST

Amount parsing — message format is: [total] [my_share?] [description] [card?] [yes/no?]
- If ONE number at the start: that is total, and my_amt = total (full amount is yours)
- If TWO numbers at the start: first is total, second is your share (my_amt)
  e.g. "14.1 1 grab to kallang hsbc revo" → total=14.1, my_amt=1.00
  e.g. "39.15 los tacos citi rewards" → total=39.15, my_amt=39.15
- Numbers with decimals like 14.1 are valid amounts (do not require two decimal places)
- "split N" or "my share N" or "i pay N" → my_amt = N
- "split half" / "split equally" / "half" → my_amt = total / 2
- "split" alone → my_amt = total / 2

Qualifying charge:
- "yes" at the end → qualifying = "Yes"
- "no" at the end → qualifying = "No"
- Cash transactions → qualifying = "No" by default
- All other cards → qualifying = "Yes" by default

Other rules:
- If no card mentioned → default to "Cash"
- Infer category from description (grab/uber = Transport, food/coffee/restaurant = Food, shopee/taobao = Shopping, etc.)
- Dates: if not mentioned, use today ({date.today().isoformat()})

Respond ONLY with a JSON object, no other text:
{{
  "date": "YYYY-MM-DD",
  "desc": "merchant/description (title case, clean)",
  "category": "one of the categories",
  "total": 0.00,
  "my_amt": 0.00,
  "card": "exact card name from the list",
  "qualifying": "Yes or No",
  "confidence": "high/medium/low",
  "note": "any clarification needed or empty string"
}}

If you cannot parse an expense at all, return: {{"error": "not an expense"}}
"""

def parse_expense_with_claude(text):
    resp = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=400,
        system=PARSE_SYSTEM, messages=[{"role":"user","content":text}]
    )
    raw = resp.content[0].text.strip()
    # Strip markdown fences
    raw = raw.replace("```json","").replace("```","").strip()
    # Extract JSON object if wrapped in other text
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
    return json.loads(raw)

# ── FLASK DASHBOARD ───────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0F0F0D;--surface:#161614;--surface2:#1E1E1A;--surface3:#252520;
  --border:#2A2A25;--border2:#333330;--text:#F0EDE6;--muted:#7A7870;--hint:#4A4A45;
  --green:#4CAF7D;--green-dim:#1A3327;--green-mid:#2D6B4A;
  --amber:#D4A843;--amber-dim:#2D2410;--red:#E05252;--red-dim:#2D1010;
  --serif:'Instrument Serif',Georgia,serif;--sans:'Inter',system-ui,sans-serif;
  --r:10px;--r-lg:14px;
}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;line-height:1.5}
a{color:var(--green);text-decoration:none}
nav{display:flex;align-items:center;gap:4px;padding:14px 24px 0;border-bottom:1px solid var(--border);background:var(--surface);flex-wrap:wrap}
.brand{font-family:var(--serif);font-size:1.2rem;color:var(--green);font-style:italic;margin-right:12px}
.nav-tab{border:none;background:transparent;color:var(--muted);font-family:var(--sans);font-size:13px;font-weight:500;padding:8px 14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;text-decoration:none;display:inline-block;transition:all .15s}
.nav-tab:hover{color:var(--text)}.nav-tab.active{color:var(--green);border-bottom-color:var(--green)}
.nav-right{margin-left:auto}
.logout-btn{font-size:12px;color:var(--muted);border:1px solid var(--border2);padding:4px 12px;border-radius:20px;background:transparent;cursor:pointer;font-family:var(--sans)}
.logout-btn:hover{color:var(--red);border-color:var(--red)}
main{max-width:960px;margin:0 auto;padding:24px 20px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{background:var(--surface2);border:1px solid var(--border);border-radius:var(--r-lg);padding:14px 16px}
.stat-label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
.stat-value{font-family:var(--serif);font-size:1.7rem;line-height:1}
.stat-sub{font-size:11px;color:var(--muted);margin-top:4px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:18px 20px;margin-bottom:14px}
.card-title{font-family:var(--serif);font-size:1.05rem;font-weight:400;margin-bottom:14px}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
input,select,textarea{font-family:var(--sans);font-size:13px;background:var(--surface2);border:1px solid var(--border2);border-radius:var(--r);padding:9px 12px;color:var(--text);outline:none;width:100%;transition:border-color .15s}
input:focus,select:focus,textarea:focus{border-color:var(--green-mid)}
select option{background:var(--surface2)}
.btn{border:none;border-radius:var(--r);padding:9px 18px;font-family:var(--sans);font-size:13px;font-weight:500;cursor:pointer;transition:all .15s}
.btn-primary{background:var(--green);color:#fff}.btn-primary:hover{background:#3D9B68}
.btn-sm{padding:5px 10px;font-size:12px;border-radius:var(--r);cursor:pointer;font-family:var(--sans);font-weight:500;border:none}
.btn-del{background:var(--red-dim);color:var(--red);border:1px solid #4A1A1A}.btn-del:hover{background:#3D1515}
.btn-edit{background:var(--surface3);color:var(--muted);border:1px solid var(--border2)}.btn-edit:hover{color:var(--text)}
.btn-save{background:var(--green);color:#fff}.btn-save:hover{background:#3D9B68}
.row{display:flex;gap:10px}.row .field{flex:1}
.entry{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--r);background:var(--surface2);margin-bottom:6px;border:1px solid transparent}
.entry:hover{border-color:var(--border2)}
.eicon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;background:var(--surface3)}
.einfo{flex:1;min-width:0}
.ename{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.emeta{font-size:11px;color:var(--muted);margin-top:2px}
.eamt{font-family:var(--serif);font-size:1rem;flex-shrink:0}
.edit-form{background:var(--surface3);border:1px solid var(--border2);border-radius:var(--r);padding:12px 14px;margin-bottom:6px;display:none}
.edit-form.open{display:block}
.edit-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:10px}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.bar-label{font-size:12px;width:160px;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{flex:1;height:6px;background:var(--surface3);border-radius:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px}
.bar-val{font-size:12px;color:var(--muted);width:72px;text-align:right;flex-shrink:0}
.month-nav{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.month-nav a{background:var(--surface2);border:1px solid var(--border);color:var(--muted);padding:5px 12px;border-radius:6px;font-size:13px}
.month-nav a:hover{border-color:var(--border2);color:var(--text)}
.month-nav .curr{font-size:14px;font-weight:500;min-width:120px;text-align:center}
.tag{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--surface3);color:var(--muted)}
.flash{padding:10px 16px;border-radius:var(--r);font-size:13px;margin-bottom:16px}
.flash-ok{background:var(--green-dim);color:var(--green);border:1px solid var(--green-mid)}
.flash-err{background:var(--red-dim);color:var(--red);border:1px solid #4A1A1A}
.empty{text-align:center;padding:2.5rem;color:var(--hint)}
.rec-row{display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--surface2);border-radius:var(--r);margin-bottom:6px;border:1px solid transparent}
.rec-row:hover{border-color:var(--border2)}
.rec-info{flex:1}.rec-name{font-size:13px;font-weight:500}.rec-cat{font-size:11px;color:var(--muted)}
.rec-amt{font-family:var(--serif);font-size:1rem;margin-right:8px}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
@media(max-width:600px){.grid4,.grid2,.grid3{grid-template-columns:1fr 1fr}.bar-label{width:100px}}
"""

SHELL = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinBot</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>{css}</style></head><body>{nav}<main>{content}</main>
<script>
function toggleEdit(id){{
  var f=document.getElementById('ef-'+id);
  f.classList.toggle('open');
}}
</script>
</body></html>"""

def make_nav(active):
    tabs=[("/","dashboard","Dashboard"),("/add","add","Add"),("/history","history","History"),
          ("/sales","sales","Sales"),("/recurring","recurring","Recurring")]
    t="".join(f'<a href="{h}" class="nav-tab{" active" if a==active else ""}">{l}</a>' for h,a,l in tabs)
    return (f'<nav><span class="brand">FinBot</span>{t}'
            f'<div class="nav-right"><form method="post" action="/logout" style="margin:0">'
            f'<button class="logout-btn">Sign out</button></form></div></nav>')

def render(content, active="dashboard"):
    return SHELL.format(css=CSS, nav=make_nav(active), content=content)

def require_auth():
    return not session.get("authed")

# ── FLASK ROUTES ──────────────────────────────────────────────────
@flask_app.route("/login", methods=["GET","POST"])
def login():
    err=""
    if request.method=="POST":
        if request.form.get("password")==DASHBOARD_PASSWORD:
            session["authed"]=True; return redirect("/")
        err='<div class="flash flash-err">Incorrect password.</div>'
    return SHELL.format(css=CSS, nav="", content=f"""
    <div style="max-width:360px;margin:80px auto"><div class="card">
      <div style="font-family:var(--serif);font-size:1.4rem;margin-bottom:20px;text-align:center">FinBot 🔒</div>
      {err}<form method="post">
        <div class="field"><label>Password</label><input type="password" name="password" autofocus></div>
        <button class="btn btn-primary" style="width:100%">Sign in</button>
      </form></div></div>""")

@flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear(); return redirect("/login")

@flask_app.route("/")
def dashboard():
    if require_auth(): return redirect("/login")
    now=datetime.now()
    y=int(request.args.get("y",now.year)); m=int(request.args.get("m",now.month))
    label=datetime(y,m,1).strftime("%B %Y")
    pv,pm=(y-1,12) if m==1 else (y,m-1)
    nv,nm=(y+1,1)  if m==12 else (y,m+1)
    cats,total_exp,count,card_spend=get_monthly_summary(y,m)
    txns=fetch_transactions(year=y,month=m,limit=20,typ="expense")
    rec=get_recurring()
    rec_total=sum(r["amount"] for r in rec)
    SALARY=6050
    bal=SALARY-total_exp-rec_total
    bc="var(--green)" if bal>=0 else "var(--red)"

    stats=f"""<div class="grid4" style="margin-bottom:16px">
      <div class="stat"><div class="stat-label">Income</div><div class="stat-value" style="color:var(--green)">${SALARY:,.2f}</div></div>
      <div class="stat"><div class="stat-label">Variable expenses</div><div class="stat-value" style="color:var(--red)">${total_exp:,.2f}</div><div class="stat-sub">{count} transactions</div></div>
      <div class="stat"><div class="stat-label">Recurring</div><div class="stat-value" style="color:var(--amber)">${rec_total:,.2f}</div></div>
      <div class="stat"><div class="stat-label">Balance</div><div class="stat-value" style="color:{bc}">${abs(bal):,.2f}</div><div class="stat-sub">{"surplus" if bal>=0 else "deficit"}</div></div>
    </div>"""

    mx=cats[0]["total"] if cats else 1
    cat_bars="".join(
        f'<div class="bar-row"><div class="bar-label">{CAT_EMOJI.get(r["category"],"📌")} {r["category"]}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{r["total"]/mx*100:.1f}%;background:{CAT_COLORS.get(r["category"],"#888")}"></div></div>'
        f'<div class="bar-val">${r["total"]:,.2f}</div></div>' for r in cats
    ) if cats else '<div class="empty">No expenses</div>'

    mx2=card_spend[0]["total"] if card_spend else 1
    card_bars="".join(
        f'<div class="bar-row"><div class="bar-label">{CARD_EMOJI.get(r["card"],"💳")} {r["card"]}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{r["total"]/mx2*100:.1f}%;background:{CARD_COLORS.get(r["card"],"#888")}"></div></div>'
        f'<div class="bar-val">${r["total"]:,.2f}</div></div>' for r in card_spend
    ) if card_spend else '<div class="empty">No card spend</div>'

    def trow(t):
        sp=f' <span class="tag">you: ${t["my_amt"]:.2f}</span>' if abs(t["my_amt"]-t["total"])>0.01 else ""
        copts="".join(f'<option {"selected" if c==t["category"] else ""}>{c}</option>' for c in CATEGORIES)
        kopts="".join(f'<option {"selected" if c==t["card"] else ""}>{c}</option>' for c in CARDS)
        qopts=f'<option {"selected" if t["qualifying"]=="Yes" else ""}>Yes</option><option {"selected" if t["qualifying"]=="No" else ""}>No</option>'
        return (
            f'<div class="entry">'
            f'<div class="eicon">{CAT_EMOJI.get(t["category"],"📌")}</div>'
            f'<div class="einfo"><div class="ename">{t["desc"]}{sp}</div>'
            f'<div class="emeta">{t["category"]} · {CARD_EMOJI.get(t["card"],"💳")} {t["card"]} · {t["date"]}</div></div>'
            f'<div class="eamt" style="color:var(--red)">-${t["total"]:.2f}</div>'
            f'<button class="btn-sm btn-edit" type="button" onclick="toggleEdit({t["id"]})">Edit</button>'
            f'<form method="post" action="/delete/{t["id"]}" style="margin:0">'
            f'<input type="hidden" name="back" value="/?y={y}&m={m}">'
            f'<button class="btn-sm btn-del">✕</button></form></div>'
            f'<div class="edit-form" id="ef-{t["id"]}">'
            f'<form method="post" action="/edit/{t["id"]}">'
            f'<input type="hidden" name="back" value="/?y={y}&m={m}">'
            f'<div class="edit-row">'
            f'<div class="field" style="margin:0"><label>Description</label><input name="desc" value="{t["desc"]}"></div>'
            f'<div class="field" style="margin:0"><label>Date</label><input type="date" name="date" value="{t["date"]}"></div>'
            f'<div class="field" style="margin:0"><label>Total</label><input type="number" name="total" value="{t["total"]}" step="0.01"></div>'
            f'<div class="field" style="margin:0"><label>My share</label><input type="number" name="my_amt" value="{t["my_amt"]}" step="0.01"></div>'
            f'<div class="field" style="margin:0"><label>Category</label><select name="category">{copts}</select></div>'
            f'<div class="field" style="margin:0"><label>Card</label><select name="card">{kopts}</select></div>'
            f'<div class="field" style="margin:0"><label>Qualifying</label><select name="qualifying">{qopts}</select></div>'
            f'</div><button class="btn-sm btn-save" type="submit">Save changes</button>'
            f'</form></div>'
        )

    txn_html="".join(trow(t) for t in txns) if txns else '<div class="empty">No transactions this month</div>'
    content=f"""
    <div class="month-nav">
      <a href="/?y={pv}&m={pm}">‹</a><span class="curr">{label}</span><a href="/?y={nv}&m={nm}">›</a>
    </div>
    {stats}
    <div class="grid2">
      <div class="card"><div class="card-title">Spending by category</div>{cat_bars}</div>
      <div class="card"><div class="card-title">Card spend (qualifying)</div>{card_bars}</div>
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div class="card-title" style="margin:0">Transactions</div>
        <a href="/add" style="font-size:12px;border:1px solid var(--green-mid);padding:5px 12px;border-radius:20px">+ Add</a>
      </div>{txn_html}</div>"""
    return render(content,"dashboard")

@flask_app.route("/add", methods=["GET","POST"])
def add_entry():
    if require_auth(): return redirect("/login")
    flash=""
    if request.method=="POST":
        try:
            d=request.form["date"]; desc=request.form["desc"].strip()
            cat=request.form["category"]; tot=float(request.form["total"])
            my=float(request.form.get("my_amt") or tot)
            card=request.form["card"]; qual=request.form.get("qualifying","Yes")
            if not d or not desc or tot<=0:
                flash='<div class="flash flash-err">Please fill in all required fields.</div>'
            else:
                tid=insert_transaction(d,desc,cat,tot,my,card,qual)
                flash=f'<div class="flash flash-ok">✅ Saved #{tid} — {desc} ${tot:.2f}</div>'
        except Exception as e:
            flash=f'<div class="flash flash-err">Error: {e}</div>'
    today=date.today().isoformat()
    copts="".join(f"<option>{c}</option>" for c in CATEGORIES)
    kopts="".join(f"<option>{c}</option>" for c in CARDS)
    content=f"""<div style="max-width:560px;margin:0 auto"><div class="card">
      <div class="card-title">Add expense</div>{flash}
      <form method="post">
        <div class="row">
          <div class="field"><label>Date</label><input type="date" name="date" value="{today}" required></div>
          <div class="field"><label>Category</label><select name="category">{copts}</select></div>
        </div>
        <div class="field"><label>Description</label><input type="text" name="desc" placeholder="e.g. Luckin Coffee" required></div>
        <div class="row">
          <div class="field"><label>Total (SGD)</label><input type="number" name="total" placeholder="0.00" step="0.01" min="0" required></div>
          <div class="field"><label>My share</label><input type="number" name="my_amt" placeholder="Leave blank = full amount" step="0.01" min="0"></div>
        </div>
        <div class="row">
          <div class="field"><label>Card</label><select name="card">{kopts}</select></div>
          <div class="field"><label>Qualifying?</label><select name="qualifying"><option>Yes</option><option>No</option></select></div>
        </div>
        <button class="btn btn-primary" style="width:100%;margin-top:4px">Add expense</button>
      </form></div></div>"""
    return render(content,"add")

@flask_app.route("/edit/<int:tid>", methods=["POST"])
def edit_entry(tid):
    if require_auth(): return redirect("/login")
    try:
        t=get_transaction(tid)
        if not t: return redirect(request.form.get("back","/"))
        for field in ["desc","date","total","my_amt","category","card","qualifying"]:
            if field in request.form:
                val=request.form[field]
                if field in ("total","my_amt"): val=round(float(val),2)
                conn=get_conn()
                conn.execute(f"UPDATE transactions SET {field}=? WHERE id=?", (val,tid))
                db_commit(conn); conn.close()
    except Exception as e:
        log.error(f"Edit error: {e}")
    return redirect(request.form.get("back","/"))

@flask_app.route("/history")
def history():
    if require_auth(): return redirect("/login")
    months=get_available_months()
    sel=request.args.get("ym", months[0] if months else datetime.now().strftime("%Y-%m"))
    try: y,m=int(sel[:4]),int(sel[5:])
    except: y,m=datetime.now().year,datetime.now().month
    label=datetime(y,m,1).strftime("%B %Y")
    txns=fetch_transactions(year=y,month=m)
    mopts="".join(
        f'<option value="{mo}" {"selected" if mo==sel else ""}>{datetime(int(mo[:4]),int(mo[5:]),1).strftime("%B %Y")}</option>'
        for mo in months)

    def trow(t):
        ac="var(--green)" if t["type"]=="income" else "var(--red)"
        sg="+" if t["type"]=="income" else "-"
        sp=f' <span class="tag">you: ${t["my_amt"]:.2f}</span>' if abs(t["my_amt"]-t["total"])>0.01 else ""
        copts="".join(f'<option {"selected" if c==t["category"] else ""}>{c}</option>' for c in CATEGORIES)
        kopts="".join(f'<option {"selected" if c==t["card"] else ""}>{c}</option>' for c in CARDS)
        qopts=f'<option {"selected" if t["qualifying"]=="Yes" else ""}>Yes</option><option {"selected" if t["qualifying"]=="No" else ""}>No</option>'
        return (
            f'<div class="entry"><div class="eicon">{CAT_EMOJI.get(t["category"],"📌")}</div>'
            f'<div class="einfo"><div class="ename">{t["desc"]}{sp}</div>'
            f'<div class="emeta">{t["category"]} · {CARD_EMOJI.get(t["card"],"💳")} {t["card"]} · {t["date"]}</div></div>'
            f'<div class="eamt" style="color:{ac}">{sg}${t["total"]:.2f}</div>'
            f'<button class="btn-sm btn-edit" type="button" onclick="toggleEdit({t["id"]})">Edit</button>'
            f'<form method="post" action="/delete/{t["id"]}" style="margin:0">'
            f'<input type="hidden" name="back" value="/history?ym={sel}">'
            f'<button class="btn-sm btn-del">✕</button></form></div>'
            f'<div class="edit-form" id="ef-{t["id"]}">'
            f'<form method="post" action="/edit/{t["id"]}">'
            f'<input type="hidden" name="back" value="/history?ym={sel}">'
            f'<div class="edit-row">'
            f'<div class="field" style="margin:0"><label>Description</label><input name="desc" value="{t["desc"]}"></div>'
            f'<div class="field" style="margin:0"><label>Date</label><input type="date" name="date" value="{t["date"]}"></div>'
            f'<div class="field" style="margin:0"><label>Total</label><input type="number" name="total" value="{t["total"]}" step="0.01"></div>'
            f'<div class="field" style="margin:0"><label>My share</label><input type="number" name="my_amt" value="{t["my_amt"]}" step="0.01"></div>'
            f'<div class="field" style="margin:0"><label>Category</label><select name="category">{copts}</select></div>'
            f'<div class="field" style="margin:0"><label>Card</label><select name="card">{kopts}</select></div>'
            f'<div class="field" style="margin:0"><label>Qualifying</label><select name="qualifying">{qopts}</select></div>'
            f'</div><button class="btn-sm btn-save" type="submit">Save changes</button>'
            f'</form></div>'
        )

    txn_html="".join(trow(t) for t in txns) if txns else '<div class="empty">No transactions</div>'
    content=f"""
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h2 style="font-family:var(--serif);font-size:1.3rem;font-weight:400">{label} — {len(txns)} entries</h2>
      <form method="get"><select name="ym" onchange="this.form.submit()" style="width:auto;padding:6px 10px;font-size:13px">{mopts}</select></form>
    </div>
    <div class="card">{txn_html}</div>"""
    return render(content,"history")

@flask_app.route("/sales")
def sales_page():
    if require_auth(): return redirect("/login")
    all_sales=get_sales()
    rev=sum(s["revenue"] for s in all_sales)
    cost=sum(s["cost"] for s in all_sales)
    profit=rev-cost
    pc="var(--green)" if profit>=0 else "var(--red)"
    rows="".join(
        f'<div class="entry"><div class="eicon">🏷️</div>'
        f'<div class="einfo"><div class="ename">{s["desc"]}</div>'
        f'<div class="emeta">{s["date"]} · cost: ${s["cost"]:.2f} · profit: ${s["profit"]:.2f}</div></div>'
        f'<div class="eamt" style="color:var(--green)">+${s["revenue"]:.2f}</div>'
        f'<form method="post" action="/sales/delete/{s["id"]}" style="margin:0">'
        f'<button class="btn-sm btn-del">✕</button></form></div>'
        for s in all_sales
    ) if all_sales else '<div class="empty">No sales recorded</div>'
    content=f"""
    <div class="grid3" style="margin-bottom:16px">
      <div class="stat"><div class="stat-label">Revenue</div><div class="stat-value" style="color:var(--green)">${rev:,.2f}</div></div>
      <div class="stat"><div class="stat-label">Cost</div><div class="stat-value" style="color:var(--red)">${cost:,.2f}</div></div>
      <div class="stat"><div class="stat-label">Profit</div><div class="stat-value" style="color:{pc}">${profit:,.2f}</div></div>
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div class="card-title" style="margin:0">All sales</div>
      </div>
      <form method="post" action="/sales/add" style="margin-bottom:16px">
        <div class="row">
          <div class="field"><label>Date</label><input type="date" name="date" value="{date.today().isoformat()}"></div>
          <div class="field"><label>Item</label><input name="desc" placeholder="Item sold"></div>
          <div class="field"><label>Revenue</label><input type="number" name="revenue" placeholder="0.00" step="0.01" min="0"></div>
          <div class="field"><label>Cost</label><input type="number" name="cost" placeholder="0.00" step="0.01" min="0"></div>
          <div class="field" style="justify-content:flex-end"><label>&nbsp;</label><button class="btn btn-primary">Add</button></div>
        </div>
      </form>
      {rows}</div>"""
    return render(content,"sales")

@flask_app.route("/sales/add", methods=["POST"])
def sales_add():
    if require_auth(): return redirect("/login")
    try:
        insert_sale(request.form["date"],request.form["desc"],
                    float(request.form.get("revenue",0)),float(request.form.get("cost",0)))
    except Exception as e: log.error(f"Sales add error: {e}")
    return redirect("/sales")

@flask_app.route("/sales/delete/<int:sid>", methods=["POST"])
def sales_delete(sid):
    if require_auth(): return redirect("/login")
    delete_sale(sid); return redirect("/sales")

@flask_app.route("/recurring")
def recurring_page():
    if require_auth(): return redirect("/login")
    flash=request.args.get("flash","")
    rec=get_recurring()
    total=sum(r["amount"] for r in rec)
    flash_html=f'<div class="flash flash-ok">{flash}</div>' if flash else ""
    copts="".join(f"<option>{c}</option>" for c in CATEGORIES)
    rows="".join(
        f'<div class="rec-row">'
        f'<div class="eicon">{CAT_EMOJI.get(r["category"],"📌")}</div>'
        f'<div class="rec-info"><div class="rec-name">{r["name"]}</div><div class="rec-cat">{r["category"]}</div></div>'
        f'<form method="post" action="/recurring/update/{r["id"]}" style="display:flex;align-items:center;gap:8px;margin:0">'
        f'<input type="number" name="amount" value="{r["amount"]}" step="0.01" style="width:90px;padding:5px 8px;font-size:13px">'
        f'<button class="btn-sm btn-save">Save</button>'
        f'</form>'
        f'<form method="post" action="/recurring/delete/{r["id"]}" style="margin:0">'
        f'<button class="btn-sm btn-del">✕</button></form></div>'
        for r in rec
    ) if rec else '<div class="empty">No recurring items</div>'

    content=f"""
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="font-family:var(--serif);font-size:1.3rem;font-weight:400">Recurring Expenses</h2>
      <div style="font-family:var(--serif);font-size:1.1rem;color:var(--amber)">Total: ${total:,.2f}/mo</div>
    </div>
    {flash_html}
    <div class="card">
      <div class="card-title">Monthly items</div>
      {rows}
    </div>
    <div class="card">
      <div class="card-title">Add new recurring item</div>
      <form method="post" action="/recurring/add">
        <div class="row">
          <div class="field"><label>Name</label><input name="name" placeholder="e.g. Netflix, Gym"></div>
          <div class="field"><label>Amount</label><input type="number" name="amount" placeholder="0.00" step="0.01" min="0"></div>
          <div class="field"><label>Category</label><select name="category">{copts}</select></div>
        </div>
        <button class="btn btn-primary">Add recurring item</button>
      </form>
    </div>"""
    return render(content,"recurring")

@flask_app.route("/recurring/update/<int:rid>", methods=["POST"])
def recurring_update(rid):
    if require_auth(): return redirect("/login")
    try: update_recurring(rid, request.form["amount"])
    except Exception as e: log.error(f"Recurring update error: {e}")
    return redirect("/recurring?flash=Updated+successfully")

@flask_app.route("/recurring/add", methods=["POST"])
def recurring_add():
    if require_auth(): return redirect("/login")
    try:
        add_recurring(request.form["name"], float(request.form["amount"]), request.form["category"])
    except Exception as e: log.error(f"Recurring add error: {e}")
    return redirect("/recurring?flash=Added+successfully")

@flask_app.route("/recurring/delete/<int:rid>", methods=["POST"])
def recurring_delete(rid):
    if require_auth(): return redirect("/login")
    delete_recurring(rid); return redirect("/recurring")

@flask_app.route("/delete/<int:tid>", methods=["POST"])
def delete_entry(tid):
    if require_auth(): return redirect("/login")
    delete_transaction(tid); return redirect(request.form.get("back","/"))

@flask_app.route("/health")
def health():
    return jsonify({"status":"ok"})

# ── TELEGRAM BOT ──────────────────────────────────────────────────
def is_allowed(update): return update.effective_user.id == ALLOWED_USER_ID
async def reject(update): await update.message.reply_text("⛔ Unauthorised.")

pending: dict[int,dict] = {}

def esc(text):
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    await update.message.reply_text(
        "👋 *FinBot* — your personal expense logger\n\n"
        "Just type your expense naturally:\n"
        "• `45 luckin coffee citi rewards`\n"
        "• `16.70 foodpanda hsbc revo`\n"
        "• `8 starbucks split 3 citi rewards`\n"
        "• `120 watsons split equally ocbc`\n\n"
        "*Commands:*\n"
        "/recent — last 10 transactions\n"
        "/summary — this month's breakdown\n"
        "/miles — card spend & cap status\n"
        "/recurring — view & update recurring items\n"
        "/edit \\<id\\> \\<field\\> \\<value\\> — edit a transaction\n"
        "/delete \\<id\\> — remove a transaction\n"
        "/help — show this message",
        parse_mode="MarkdownV2")

async def cmd_help(update, ctx): await cmd_start(update, ctx)

async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    today = date.today().isoformat()
    conn = get_conn()
    cur = conn.execute(
        "SELECT * FROM transactions WHERE date<=? ORDER BY date DESC, id DESC LIMIT 10",
        (today,)
    )
    rows = _rows_to_dicts(cur, cur.fetchall())
    conn.close()
    if not rows:
        return await update.message.reply_text("No transactions yet.")
    lines = ["Recent transactions (up to today):\n"]
    for t in rows:
        sp = f" (you: ${t['my_amt']:.2f})" if abs(t["my_amt"]-t["total"])>0.01 else ""
        lines.append(f"#{t['id']} {t['desc']}\n  ${t['total']:.2f}{sp} | {t['card']} | {t['date']}\n  {t['category']}")
    await update.message.reply_text("\n\n".join(lines))

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    now = datetime.now()
    today = date.today().isoformat()
    ym = f"{now.year:04d}-{now.month:02d}"
    conn = get_conn()
    cur = conn.execute(
        "SELECT category, SUM(my_amt) as total FROM transactions"
        " WHERE strftime('%Y-%m',date)=? AND type='expense' AND date<=?"
        " GROUP BY category ORDER BY total DESC",
        (ym, today))
    cats = _rows_to_dicts(cur, cur.fetchall())
    r2 = conn.execute("SELECT SUM(my_amt) FROM transactions WHERE strftime('%Y-%m',date)=? AND type='expense' AND date<=?", (ym, today)).fetchone()
    r3 = conn.execute("SELECT COUNT(*) FROM transactions WHERE strftime('%Y-%m',date)=? AND type='expense' AND date<=?", (ym, today)).fetchone()
    conn.close()
    total_exp = (r2[0] or 0)
    count = (r3[0] or 0)
    rec = get_recurring()
    rec_total = sum(r["amount"] for r in rec)
    bal = 6050 - total_exp - rec_total
    lines = [f"📊 {now.strftime('%B %Y')} — {count} transactions\n"]
    for r in cats:
        bar_len = int((r["total"]/total_exp)*12) if total_exp else 0
        bar = "█"*bar_len + "░"*(12-bar_len)
        lines.append(f"{CAT_EMOJI.get(r['category'],'📌')} {r['category']}\n[{bar}] ${r['total']:.2f}")
    lines.append(f"\n💸 Variable: ${total_exp:.2f}")
    lines.append(f"🔁 Recurring: ${rec_total:.2f}")
    lines.append(f"💰 Balance: ${bal:.2f} ({'surplus' if bal>=0 else 'deficit'})")
    await update.message.reply_text("\n\n".join(lines))

async def cmd_miles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    import calendar as cal
    now = datetime.now()
    today = date.today().isoformat()
    def card_window(card_id):
        if card_id == "CITI REWARDS":
            if now.day >= 6:
                s = now.replace(day=6)
                em, ey = (now.month+1, now.year) if now.month<12 else (1, now.year+1)
                e = now.replace(year=ey, month=em, day=5)
            else:
                pm, py = (now.month-1, now.year) if now.month>1 else (12, now.year-1)
                s = now.replace(year=py, month=pm, day=6)
                e = now.replace(day=5)
            return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"), f"{s.strftime('%d %b')} - {e.strftime('%d %b')}"
        else:
            last = cal.monthrange(now.year, now.month)[1]
            return now.replace(day=1).strftime("%Y-%m-%d"), now.replace(day=last).strftime("%Y-%m-%d"), now.strftime("%B %Y")
    lines = ["✈️ Miles tracker\n"]
    total_miles = 0
    conn = get_conn()
    for card, (cap, mpd, mult, note) in CARD_CAPS.items():
        start, end, label = card_window(card)
        cur = conn.execute(
            "SELECT SUM(total) as spent FROM transactions"
            " WHERE card=? AND date>=? AND date<=? AND type='expense' AND qualifying='Yes'",
            (card, start, end))
        row = cur.fetchone()
        spent = (row[0] or 0) if row else 0
        if spent == 0 and cap is not None:
            continue
        emoji = CARD_EMOJI.get(card, "💳")
        if cap is None:
            miles = round(spent * mpd)
            cap_line = "No cap"
            status = "🟢"
            bar = "∞"
        else:
            pct = min(spent/cap, 1.0)
            filled = int(pct*10)
            bar = "█"*filled + "░"*(10-filled)
            rem = max(0, cap-spent)
            status = "🔴" if spent>=cap else ("🟡" if pct>=0.8 else "🟢")
            cap_line = f"CAP REACHED (${spent:.0f}/${cap:.0f})" if spent>=cap else f"${rem:.0f} to cap"
            miles = round(min(spent,cap)*mpd*mult)
        total_miles += miles
        lines.append(f"{status} {emoji} {card}\n  [{bar}] ${spent:.0f} ({label})\n  {cap_line} | ~{miles:,} miles")
    conn.close()
    lines.append(f"\nTotal est. miles: {total_miles:,}")
    await update.message.reply_text("\n\n".join(lines))

async def cmd_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    rec = get_recurring()
    if not rec:
        return await update.message.reply_text("No recurring items. Add them on the dashboard.")
    total = sum(r["amount"] for r in rec)
    lines = ["🔁 Recurring expenses\n"]
    for r in rec:
        lines.append(f"#{r['id']} {r['name']}\n  ${r['amount']:.2f}/mo | {r['category']}")
    lines.append(f"\nTotal: ${total:.2f}/mo")
    lines.append("\nTo update: /recurring set <id> <amount>")
    await update.message.reply_text("\n\n".join(lines))

async def cmd_recurring_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    args = [a for a in ctx.args if a.lower() != "set"]
    if len(args) < 2:
        return await update.message.reply_text("Usage: /recurring set <id> <amount>\nExample: /recurring set 1 500")
    try:
        rid = int(args[0]); amount = float(args[1])
        update_recurring(rid, amount)
        await update.message.reply_text(f"✅ Recurring #{rid} updated to ${amount:.2f}/mo")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    args = ctx.args
    if len(args) < 3:
        return await update.message.reply_text(
            "Usage: /edit <id> <field> <value>\n\n"
            "Fields: desc  date  total  my_amt  category  card  qualifying\n\n"
            "Examples:\n"
            "/edit 42 total 8.50\n"
            "/edit 42 card HSBC REVO\n"
            "/edit 42 category Groceries\n"
            "/edit 42 desc Starbucks Suntec\n"
            "/edit 42 qualifying No")
    try:
        tid = int(args[0])
        field = args[1].lower()
        value = " ".join(args[2:])
        t = get_transaction(tid)
        if not t:
            return await update.message.reply_text(f"❌ No transaction #{tid}. Check /recent for IDs.")
        ok, msg = update_transaction(tid, field, value)
        if ok:
            t = get_transaction(tid)
            await update.message.reply_text(
                f"✅ Updated #{tid}\n\n"
                f"{CAT_EMOJI.get(t['category'],'📌')} {t['desc']}\n"
                f"  ${t['total']:.2f} | {t['card']} | {t['date']}\n"
                f"  {t['category']}")
        else:
            await update.message.reply_text(f"❌ {msg}")
    except ValueError:
        await update.message.reply_text("❌ ID must be a number. Example: /edit 42 total 8.50")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Usage: /delete <id>  (get IDs from /recent)")
    ok = delete_transaction(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Deleted #{ctx.args[0]}." if ok else f"❌ No transaction #{ctx.args[0]}.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    text=update.message.text.strip()
    uid=update.effective_user.id

    # Handle /recurring set inline
    if text.lower().startswith("/recurring set") or text.lower().startswith("/recurring"):
        parts=text.split()
        if len(parts)>=4 and parts[1].lower()=="set":
            ctx.args=parts[1:]
            return await cmd_recurring_set(update,ctx)

    if uid in pending:
        exp = pending[uid]
        tl = text.lower().strip()
        if tl in ("yes","y","✅","ok","yep","yeah","confirm"):
            tid = insert_transaction(exp["date"],exp["desc"],exp["category"],
                                    exp["total"],exp["my_amt"],exp["card"],exp["qualifying"])
            del pending[uid]
            sp = f" (you: ${exp['my_amt']:.2f})" if abs(exp["my_amt"]-exp["total"])>0.01 else ""
            await update.message.reply_text(
                f"✅ Saved #{tid}\n\n"
                f"{CAT_EMOJI.get(exp['category'],'📌')} {exp['desc']}\n"
                f"  ${exp['total']:.2f}{sp} | {exp['card']} | {exp['date']}\n"
                f"  {exp['category']} | Qualifying: {exp['qualifying']}"
            ); return
        elif tl in ("no","n","❌","cancel","nope"):
            del pending[uid]
            await update.message.reply_text("❌ Cancelled."); return
        elif tl in ("qualifying","q","y qualifying","yes qualifying"):
            pending[uid]["qualifying"] = "Yes"
            await update.message.reply_text(
                f"✅ Marked as qualifying. Reply yes to save or no to cancel.\n"
                f"{exp['desc']} | ${exp['total']:.2f} | {exp['card']}"
            ); return
        elif tl in ("not qualifying","nq","n qualifying","no qualifying","not"):
            pending[uid]["qualifying"] = "No"
            await update.message.reply_text(
                f"✅ Marked as NOT qualifying. Reply yes to save or no to cancel.\n"
                f"{exp['desc']} | ${exp['total']:.2f} | {exp['card']}"
            ); return
        else:
            exp = pending[uid]
            sp = f" (you: ${exp['my_amt']:.2f})" if abs(exp["my_amt"]-exp["total"])>0.01 else ""
            msg = (
                "⚠️ Still waiting for confirmation:\n\n"
                + f"{CAT_EMOJI.get(exp['category'],'📌')} {exp['desc']}\n"
                + f"  ${exp['total']:.2f}{sp} | {exp['card']} | {exp['date']}\n\n"
                + "Reply yes to save, no to cancel."
            )
            await update.message.reply_text(msg)
            return

    thinking=await update.message.reply_text("⏳ Parsing…")
    try:
        parsed = parse_expense_with_claude(text)
    except Exception as e:
        log.error(f"Parse error: {e!r}")
        await thinking.delete()
        await update.message.reply_text(
            f"⚠️ Parse failed: {e}\n\n"
            "Try: 14.1 grab to kallang hsbc revo yes"
        )
        return

    await thinking.delete()
    if "error" in parsed:
        await update.message.reply_text("🤔 I couldn't find an expense\\. Try: `39 los tacos citi rewards`", parse_mode="MarkdownV2")
        return

    pending[uid] = parsed
    sp = f" (you: ${parsed['my_amt']:.2f})" if abs(parsed.get("my_amt",0)-parsed.get("total",0))>0.01 else ""
    qual = parsed.get("qualifying","Yes")
    qual_line = f"  Qualifying: {qual}"
    # Flag if qualifying was not explicitly stated by user (Claude inferred it)
    user_text_lower = text.lower()
    qual_explicit = any(w in user_text_lower for w in ("yes","no","qualifying","not qualifying"))
    if not qual_explicit and parsed.get("card","") != "Cash":
        qual_line = "  Qualifying? — reply YES or NO (or confirm/cancel)"
    msg = (
        "Got it — confirm?\n\n"
        + f"{CAT_EMOJI.get(parsed.get('category',''),'📌')} {parsed.get('desc','')}\n"
        + f"  ${parsed.get('total',0):.2f}{sp} | {parsed.get('card','')} | {parsed.get('date','')}\n"
        + f"  {parsed.get('category','')}\n"
        + qual_line + "\n\n"
        + "yes = save  |  no = cancel"
    )
    await update.message.reply_text(msg)

# ── MAIN ──────────────────────────────────────────────────────────
def run_flask():
    log.info(f"Dashboard starting on port {PORT}…")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask dashboard thread started.")
    tg=Application.builder().token(TELEGRAM_TOKEN).build()
    tg.add_handler(CommandHandler("start",     cmd_start))
    tg.add_handler(CommandHandler("help",      cmd_help))
    tg.add_handler(CommandHandler("recent",    cmd_recent))
    tg.add_handler(CommandHandler("summary",   cmd_summary))
    tg.add_handler(CommandHandler("miles",     cmd_miles))
    tg.add_handler(CommandHandler("recurring", cmd_recurring))
    tg.add_handler(CommandHandler("edit",      cmd_edit))
    tg.add_handler(CommandHandler("delete",    cmd_delete))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Telegram bot polling…")
    tg.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
