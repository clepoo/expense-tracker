"""
FinBot — Telegram Bot + Web Dashboard
Telegram bot for logging expenses, Flask for the dashboard.
Both share the same SQLite database.
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime, date
from anthropic import Anthropic
from flask import Flask, request, session, redirect, url_for, jsonify
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

# ── CONFIG ────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID    = int(os.environ["ALLOWED_USER_ID"])
DB_PATH            = os.environ.get("DB_PATH", "finbot.db")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "finbot123")
FLASK_SECRET       = os.environ.get("FLASK_SECRET", "change-me-in-railway")
PORT               = int(os.environ.get("PORT", 8080))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── CARDS & CATEGORIES ────────────────────────────────────────────
CARDS = [
    "CITI REWARDS", "HSBC REVO", "UOB PPV Contactless", "UOB PPV Online",
    "DBS WWMC", "OCBC REWARDS", "UOB PRIVI", "UOB VS SGD", "UOB VS FCY",
    "TRUST", "Cash",
]

CATEGORIES = [
    "Food", "Groceries", "Shopping", "Transport", "Travel",
    "Health, Beauty & Wellness", "Entertainment", "Bills", "Investments", "Misc",
]

CAT_EMOJI = {
    "Food": "🍜", "Groceries": "🛒", "Shopping": "🛍️", "Transport": "🚌",
    "Travel": "✈️", "Health, Beauty & Wellness": "💊", "Entertainment": "🎬",
    "Bills": "📄", "Investments": "📈", "Misc": "📌", "Income": "💰",
}

CARD_EMOJI = {
    "CITI REWARDS": "🔵", "HSBC REVO": "🟢", "UOB PPV Contactless": "🟣",
    "UOB PPV Online": "🟣", "DBS WWMC": "🔴", "OCBC REWARDS": "🟡",
    "UOB PRIVI": "🔷", "UOB VS SGD": "🟤", "UOB VS FCY": "🟤",
    "TRUST": "⬜", "Cash": "💵",
}

CARD_COLORS = {
    "CITI REWARDS": "#378ADD", "HSBC REVO": "#1D9E75",
    "UOB PPV Contactless": "#7F77DD", "UOB PPV Online": "#7F77DD",
    "DBS WWMC": "#E24B4A", "OCBC REWARDS": "#BA7517",
    "UOB PRIVI": "#5B8FD4", "UOB VS SGD": "#8B6914", "UOB VS FCY": "#8B6914",
    "TRUST": "#888780", "Cash": "#4A4A45",
}

CAT_COLORS = {
    "Food": "#1D9E75", "Groceries": "#0F6E56", "Shopping": "#7F77DD",
    "Transport": "#378ADD", "Travel": "#D85A30", "Health, Beauty & Wellness": "#D4537E",
    "Entertainment": "#BA7517", "Bills": "#E24B4A", "Investments": "#639922", "Misc": "#888780",
}
# Card cap rules for /miles command
# (cap_sgd, mpd, miles_multiplier, note)  — cap_sgd=None means no cap
CARD_CAPS = {
    "CITI REWARDS":        (1000,  4,   0.4,  "Online · statement month"),
    "HSBC REVO":           (1000,  4,   0.4,  "Contactless · calendar month"),
    "UOB PPV Contactless": (600,   4,   2.0,  "$600 cap contactless"),
    "UOB PPV Online":      (600,   4,   2.0,  "$600 cap online"),
    "DBS WWMC":            (1000,  4,   2.0,  "Online only · no Amaze"),
    "OCBC REWARDS":        (1110,  4,   0.4,  "Online MCC only"),
    "UOB PRIVI":           (None,  1.4, 1.4,  "All spend · no cap"),
    "UOB VS SGD":          (1200,  4,   2.0,  "SGD contactless · min $1K"),
    "UOB VS FCY":          (1200,  4,   2.0,  "FCY spend · min $1K"),
}


# ── DATABASE ──────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            desc        TEXT NOT NULL,
            category    TEXT NOT NULL,
            total       REAL NOT NULL,
            my_amt      REAL NOT NULL,
            card        TEXT NOT NULL,
            qualifying  TEXT NOT NULL DEFAULT 'Yes',
            type        TEXT NOT NULL DEFAULT 'expense',
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def insert_transaction(date_, desc, category, total, my_amt, card, qualifying="Yes", typ="expense"):
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO transactions (date, desc, category, total, my_amt, card, qualifying, type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date_, desc, category, round(total,2), round(my_amt,2), card, qualifying, typ, datetime.now().isoformat()))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid

def fetch_transactions(year=None, month=None, limit=None, typ=None):
    conn = get_conn()
    where, params = [], []
    if year and month:
        where.append("strftime('%Y-%m', date) = ?")
        params.append(f"{year:04d}-{month:02d}")
    if typ:
        where.append("type = ?")
        params.append(typ)
    where_str = ("WHERE " + " AND ".join(where)) if where else ""
    lim = f"LIMIT {limit}" if limit else ""
    rows = conn.execute(
        f"SELECT * FROM transactions {where_str} ORDER BY date DESC, id DESC {lim}", params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_transaction(tid):
    conn = get_conn()
    affected = conn.execute("DELETE FROM transactions WHERE id = ?", (tid,)).rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_monthly_summary(year, month):
    conn = get_conn()
    ym = f"{year:04d}-{month:02d}"
    cats = conn.execute("""
        SELECT category, SUM(my_amt) as total FROM transactions
        WHERE strftime('%Y-%m', date) = ? AND type = 'expense'
        GROUP BY category ORDER BY total DESC
    """, (ym,)).fetchall()
    total_row  = conn.execute("SELECT SUM(my_amt) FROM transactions WHERE strftime('%Y-%m', date) = ? AND type = 'expense'", (ym,)).fetchone()
    count_row  = conn.execute("SELECT COUNT(*) FROM transactions WHERE strftime('%Y-%m', date) = ? AND type = 'expense'", (ym,)).fetchone()
    card_spend = conn.execute("""
        SELECT card, SUM(total) as total FROM transactions
        WHERE strftime('%Y-%m', date) = ? AND type = 'expense' AND qualifying = 'Yes'
        GROUP BY card ORDER BY total DESC
    """, (ym,)).fetchall()
    conn.close()
    return [dict(r) for r in cats], (total_row[0] or 0), (count_row[0] or 0), [dict(r) for r in card_spend]

def get_available_months():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT strftime('%Y-%m', date) as ym FROM transactions ORDER BY ym DESC LIMIT 24").fetchall()
    conn.close()
    return [r[0] for r in rows]

# ── CLAUDE PARSER ─────────────────────────────────────────────────
PARSE_SYSTEM = f"""You are a finance expense parser for a Singapore user.
Parse the user's message into a JSON expense entry.

Available cards: {", ".join(CARDS)}
Available categories: {", ".join(CATEGORIES)}

Card notes:
- UOB PRIVI: 1.4 MPD, no cap, earns on all spend
- CITI REWARDS: 4 MPD online, $1000 cap per statement month
- HSBC REVO: 4 MPD contactless, $1000 cap per calendar month
- UOB PPV Contactless/Online: 4 MPD, $600 cap each
- DBS WWMC: 4 MPD online only, $1000 cap
- OCBC REWARDS: 4 MPD online MCC, $1110 cap
- UOB VS SGD/FCY: 4 MPD, $1200 cap each, min $1K spend to activate

Rules:
- If no card is mentioned, default to "Cash"
- If no category is clear, infer from the description
- The first standalone number is the total amount
- Split rules (apply in this order):
    1. "split 3" or "my share 3" or "i pay 3" — my_amt = that specific number (e.g. split 3 means you pay $3)
    2. "split half", "split equally", "half" — my_amt = total / 2
    3. "split" with no amount — my_amt = total / 2
    4. no split mentioned — my_amt = total
- "yes" or "no" at the end of the message = qualifying charge override
- Dates: if not mentioned, use today ({date.today().isoformat()})
- qualifying: "Yes" unless it's a cash transaction or user says "no"

Respond ONLY with a JSON object, no other text:
{{
  "date": "YYYY-MM-DD",
  "desc": "merchant/description",
  "category": "one of the categories",
  "total": 0.00,
  "my_amt": 0.00,
  "card": "card name",
  "qualifying": "Yes or No",
  "confidence": "high/medium/low",
  "note": "any clarification needed or empty string"
}}

If you cannot parse an expense at all, return: {{"error": "not an expense"}}
"""

def parse_expense_with_claude(text: str) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": text}]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
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
nav{display:flex;align-items:center;gap:4px;padding:14px 24px 0;border-bottom:1px solid var(--border);background:var(--surface)}
.brand{font-family:var(--serif);font-size:1.2rem;color:var(--green);font-style:italic;margin-right:16px}
.nav-tab{border:none;background:transparent;color:var(--muted);font-family:var(--sans);font-size:13px;font-weight:500;padding:8px 16px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;text-decoration:none;display:inline-block;transition:all .15s}
.nav-tab:hover{color:var(--text)}.nav-tab.active{color:var(--green);border-bottom-color:var(--green)}
.nav-right{margin-left:auto}
.logout-btn{font-size:12px;color:var(--muted);border:1px solid var(--border2);padding:4px 12px;border-radius:20px;background:transparent;cursor:pointer;font-family:var(--sans)}
.logout-btn:hover{color:var(--red);border-color:var(--red)}
main{max-width:960px;margin:0 auto;padding:24px 20px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{background:var(--surface2);border:1px solid var(--border);border-radius:var(--r-lg);padding:14px 16px}
.stat-label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
.stat-value{font-family:var(--serif);font-size:1.7rem;line-height:1}
.stat-sub{font-size:11px;color:var(--muted);margin-top:4px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:18px 20px;margin-bottom:14px}
.card-title{font-family:var(--serif);font-size:1.05rem;font-weight:400;margin-bottom:14px}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
input,select{font-family:var(--sans);font-size:13px;background:var(--surface2);border:1px solid var(--border2);border-radius:var(--r);padding:9px 12px;color:var(--text);outline:none;width:100%;transition:border-color .15s}
input:focus,select:focus{border-color:var(--green-mid)}
select option{background:var(--surface2)}
.btn{border:none;border-radius:var(--r);padding:9px 18px;font-family:var(--sans);font-size:13px;font-weight:500;cursor:pointer;transition:all .15s}
.btn-primary{background:var(--green);color:#fff}.btn-primary:hover{background:#3D9B68}
.btn-sm{padding:5px 10px;font-size:12px;background:var(--red-dim);color:var(--red);border:1px solid #4A1A1A;border-radius:var(--r)}
.btn-sm:hover{background:#3D1515}
.row{display:flex;gap:10px}.row .field{flex:1}
.entry{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--r);background:var(--surface2);margin-bottom:6px;border:1px solid transparent}
.entry:hover{border-color:var(--border2)}
.eicon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;background:var(--surface3)}
.einfo{flex:1;min-width:0}
.ename{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.emeta{font-size:11px;color:var(--muted);margin-top:2px}
.eamt{font-family:var(--serif);font-size:1rem;flex-shrink:0}
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
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
@media(max-width:600px){.grid4,.grid2{grid-template-columns:1fr 1fr}.bar-label{width:100px}}
"""

HTML_SHELL = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinBot</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>{css}</style></head><body>{nav}<main>{content}</main></body></html>"""

def make_nav(active):
    tabs = [("/","dashboard","Dashboard"),("/add","add","Add Entry"),("/history","history","History")]
    t = "".join(f'<a href="{h}" class="nav-tab{" active" if a==active else ""}">{l}</a>' for h,a,l in tabs)
    return (f'<nav><span class="brand">FinBot</span>{t}'
            f'<div class="nav-right"><form method="post" action="/logout" style="margin:0">'
            f'<button class="logout-btn">Sign out</button></form></div></nav>')

def render(content, active="dashboard"):
    return HTML_SHELL.format(css=CSS, nav=make_nav(active), content=content)

def require_auth():
    return not session.get("authed")

@flask_app.route("/login", methods=["GET","POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect("/")
        err = '<div class="flash flash-err">Incorrect password.</div>'
    return HTML_SHELL.format(css=CSS, nav="", content=f"""
    <div style="max-width:360px;margin:80px auto">
      <div class="card">
        <div style="font-family:var(--serif);font-size:1.4rem;margin-bottom:20px;text-align:center">FinBot 🔒</div>
        {err}
        <form method="post">
          <div class="field"><label>Password</label>
            <input type="password" name="password" autofocus placeholder="Dashboard password"></div>
          <button class="btn btn-primary" style="width:100%">Sign in</button>
        </form>
      </div>
    </div>""")

@flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")

@flask_app.route("/")
def dashboard():
    if require_auth(): return redirect("/login")
    now = datetime.now()
    y = int(request.args.get("y", now.year))
    m = int(request.args.get("m", now.month))
    label = datetime(y,m,1).strftime("%B %Y")
    pv,pm = (y-1,12) if m==1 else (y,m-1)
    nv,nm = (y+1,1)  if m==12 else (y,m+1)

    cats, total_exp, count, card_spend = get_monthly_summary(y, m)
    txns = fetch_transactions(year=y, month=m, limit=20, typ="expense")

    SALARY = 6050; RECURRING = 888.35
    bal = SALARY - total_exp - RECURRING
    bal_color = "var(--green)" if bal>=0 else "var(--red)"

    stats = f"""<div class="grid4" style="margin-bottom:16px">
      <div class="stat"><div class="stat-label">Income</div><div class="stat-value" style="color:var(--green)">${SALARY:,.2f}</div></div>
      <div class="stat"><div class="stat-label">Variable expenses</div><div class="stat-value" style="color:var(--red)">${total_exp:,.2f}</div><div class="stat-sub">{count} transactions</div></div>
      <div class="stat"><div class="stat-label">Recurring</div><div class="stat-value" style="color:var(--amber)">${RECURRING:,.2f}</div></div>
      <div class="stat"><div class="stat-label">Balance</div><div class="stat-value" style="color:{bal_color}">${abs(bal):,.2f}</div><div class="stat-sub">{"surplus" if bal>=0 else "deficit"}</div></div>
    </div>"""

    mx = cats[0]["total"] if cats else 1
    cat_bars = "".join(
        f'<div class="bar-row"><div class="bar-label">{CAT_EMOJI.get(r["category"],"📌")} {r["category"]}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{r["total"]/mx*100:.1f}%;background:{CAT_COLORS.get(r["category"],"#888")}"></div></div>'
        f'<div class="bar-val">${r["total"]:,.2f}</div></div>' for r in cats
    ) if cats else '<div class="empty">No expenses</div>'

    mx2 = card_spend[0]["total"] if card_spend else 1
    card_bars = "".join(
        f'<div class="bar-row"><div class="bar-label">{CARD_EMOJI.get(r["card"],"💳")} {r["card"]}</div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{r["total"]/mx2*100:.1f}%;background:{CARD_COLORS.get(r["card"],"#888")}"></div></div>'
        f'<div class="bar-val">${r["total"]:,.2f}</div></div>' for r in card_spend
    ) if card_spend else '<div class="empty">No card spend</div>'

    def trow(t):
        sp = f' <span class="tag">you: ${t["my_amt"]:.2f}</span>' if abs(t["my_amt"]-t["total"])>0.01 else ""
        return (f'<div class="entry"><div class="eicon">{CAT_EMOJI.get(t["category"],"📌")}</div>'
                f'<div class="einfo"><div class="ename">{t["desc"]}{sp}</div>'
                f'<div class="emeta">{t["category"]} · {CARD_EMOJI.get(t["card"],"💳")} {t["card"]} · {t["date"]}</div></div>'
                f'<div class="eamt" style="color:var(--red)">-${t["total"]:.2f}</div>'
                f'<form method="post" action="/delete/{t["id"]}" style="margin:0">'
                f'<input type="hidden" name="back" value="/?y={y}&m={m}">'
                f'<button class="btn btn-sm">✕</button></form></div>')

    txn_html = "".join(trow(t) for t in txns) if txns else '<div class="empty">No transactions this month</div>'

    content = f"""
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
      </div>
      {txn_html}
    </div>"""
    return render(content, "dashboard")

@flask_app.route("/add", methods=["GET","POST"])
def add_entry():
    if require_auth(): return redirect("/login")
    flash = ""
    if request.method == "POST":
        try:
            d    = request.form["date"]
            desc = request.form["desc"].strip()
            cat  = request.form["category"]
            tot  = float(request.form["total"])
            my   = float(request.form.get("my_amt") or tot)
            card = request.form["card"]
            qual = request.form.get("qualifying","Yes")
            if not d or not desc or tot<=0:
                flash = '<div class="flash flash-err">Please fill in all required fields.</div>'
            else:
                tid = insert_transaction(d, desc, cat, tot, my, card, qual)
                flash = f'<div class="flash flash-ok">✅ Saved #{tid} — {desc} ${tot:.2f}</div>'
        except Exception as e:
            flash = f'<div class="flash flash-err">Error: {e}</div>'

    today = date.today().isoformat()
    copts = "".join(f"<option>{c}</option>" for c in CATEGORIES)
    kopts = "".join(f"<option>{c}</option>" for c in CARDS)
    content = f"""<div style="max-width:560px;margin:0 auto"><div class="card">
      <div class="card-title">Add expense</div>{flash}
      <form method="post">
        <div class="row">
          <div class="field"><label>Date</label><input type="date" name="date" value="{today}" required></div>
          <div class="field"><label>Category</label><select name="category">{copts}</select></div>
        </div>
        <div class="field"><label>Description</label><input type="text" name="desc" placeholder="e.g. Luckin Coffee, Grab…" required></div>
        <div class="row">
          <div class="field"><label>Total (SGD)</label><input type="number" name="total" placeholder="0.00" step="0.01" min="0" required></div>
          <div class="field"><label>My share</label><input type="number" name="my_amt" placeholder="Leave blank = full amount" step="0.01" min="0"></div>
        </div>
        <div class="row">
          <div class="field"><label>Card</label><select name="card">{kopts}</select></div>
          <div class="field"><label>Qualifying?</label><select name="qualifying"><option value="Yes">Yes</option><option value="No">No</option></select></div>
        </div>
        <button class="btn btn-primary" style="width:100%;margin-top:4px">Add expense</button>
      </form>
    </div></div>"""
    return render(content, "add")

@flask_app.route("/history")
def history():
    if require_auth(): return redirect("/login")
    months = get_available_months()
    sel = request.args.get("ym", months[0] if months else datetime.now().strftime("%Y-%m"))
    try: y,m = int(sel[:4]),int(sel[5:])
    except: y,m = datetime.now().year,datetime.now().month
    label = datetime(y,m,1).strftime("%B %Y")
    txns = fetch_transactions(year=y, month=m)

    mopts = "".join(
        f'<option value="{mo}" {"selected" if mo==sel else ""}>{datetime(int(mo[:4]),int(mo[5:]),1).strftime("%B %Y")}</option>'
        for mo in months)

    def trow(t):
        acolor = "var(--green)" if t["type"]=="income" else "var(--red)"
        sign = "+" if t["type"]=="income" else "-"
        sp = f' <span class="tag">you: ${t["my_amt"]:.2f}</span>' if abs(t["my_amt"]-t["total"])>0.01 else ""
        return (f'<div class="entry"><div class="eicon">{CAT_EMOJI.get(t["category"],"📌")}</div>'
                f'<div class="einfo"><div class="ename">{t["desc"]}{sp}</div>'
                f'<div class="emeta">{t["category"]} · {CARD_EMOJI.get(t["card"],"💳")} {t["card"]} · {t["date"]}</div></div>'
                f'<div class="eamt" style="color:{acolor}">{sign}${t["total"]:.2f}</div>'
                f'<form method="post" action="/delete/{t["id"]}" style="margin:0">'
                f'<input type="hidden" name="back" value="/history?ym={sel}">'
                f'<button class="btn btn-sm">✕</button></form></div>')

    txn_html = "".join(trow(t) for t in txns) if txns else '<div class="empty">No transactions</div>'
    content = f"""
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h2 style="font-family:var(--serif);font-size:1.3rem;font-weight:400">{label}</h2>
      <form method="get"><select name="ym" onchange="this.form.submit()" style="width:auto;padding:6px 10px;font-size:13px">{mopts}</select></form>
    </div>
    <div class="card"><div class="card-title">All transactions — {len(txns)} entries</div>{txn_html}</div>"""
    return render(content, "history")

@flask_app.route("/delete/<int:tid>", methods=["POST"])
def delete_entry(tid):
    if require_auth(): return redirect("/login")
    delete_transaction(tid)
    return redirect(request.form.get("back", "/"))

@flask_app.route("/health")
def health():
    return jsonify({"status":"ok"})

# ── TELEGRAM BOT ──────────────────────────────────────────────────
def is_allowed(update): return update.effective_user.id == ALLOWED_USER_ID
async def reject(update): await update.message.reply_text("⛔ Unauthorised.")

pending: dict[int,dict] = {}

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    await update.message.reply_text(
        "👋 *FinBot* — your personal expense logger\n\n"
        "Just type your expense naturally:\n"
        "• `45 luckin coffee citi rewards`\n"
        "• `16.70 foodpanda hsbc revo`\n"
        "• `120 watsons split equally ocbc`\n\n"
        "*Commands:*\n"
        "/recent — last 10 transactions\n"
        "/summary — this month's breakdown\n"
        "/miles — card spend & cap status\n"
        "/delete \\<id\\> — remove a transaction\n"
        "/help — show this message",
        parse_mode="MarkdownV2")

async def cmd_help(update, ctx): await cmd_start(update, ctx)

async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    rows = fetch_transactions(limit=10)
    if not rows: return await update.message.reply_text("No transactions yet.")
    lines = ["*Recent transactions:*\n"]
    for t in rows:
        sp = f" \\(you: ${t['my_amt']:.2f}\\)" if abs(t["my_amt"]-t["total"])>0.01 else ""
        lines.append(f"`#{t['id']}` {CAT_EMOJI.get(t['category'],'📌')} *{esc(t['desc'])}*\n"
                     f"   ${t['total']:.2f}{sp} · {esc(t['card'])} · {t['date']}\n"
                     f"   _{esc(t['category'])}_")
    await update.message.reply_text("\n\n".join(lines), parse_mode="MarkdownV2")

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    now = datetime.now()
    cats, total_exp, count, _ = get_monthly_summary(now.year, now.month)
    if not cats: return await update.message.reply_text(f"No expenses for {now.strftime('%B %Y')} yet.")
    lines = [f"📊 *{esc(now.strftime('%B %Y'))}* — {count} transactions\n"]
    for r in cats:
        bar_len = int((r["total"]/total_exp)*12) if total_exp else 0
        lines.append(f"{CAT_EMOJI.get(r['category'],'📌')} {esc(r['category'])}\n"
                     f"`{'█'*bar_len+'░'*(12-bar_len)}` ${r['total']:.2f}")
    lines.append(f"\n💸 *Total: ${total_exp:.2f}*")
    await update.message.reply_text("\n\n".join(lines), parse_mode="MarkdownV2")


async def cmd_miles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    now = datetime.now()
    y, m = now.year, now.month
    ym = f"{y:04d}-{m:02d}"

    conn = get_conn()
    # Get qualifying spend per card this month
    rows = conn.execute("""
        SELECT card, SUM(total) as spent
        FROM transactions
        WHERE strftime('%Y-%m', date) = ? AND type = 'expense' AND qualifying = 'Yes'
        GROUP BY card
    """, (ym,)).fetchall()
    conn.close()

    spend_map = {r["card"]: r["spent"] for r in rows}

    lines = [f"✈️ *Miles tracker — {esc(now.strftime('%B %Y'))}*\n"]

    total_miles = 0
    for card, (cap, mpd, mult, note) in CARD_CAPS.items():
        spent = spend_map.get(card, 0)
        if spent == 0 and cap is not None:
            continue  # skip cards with no spend this month

        emoji = CARD_EMOJI.get(card, "💳")

        if cap is None:
            # No cap cards — just calc miles
            miles = round(spent * mpd * mult / mpd)  # base pts * multiplier
            bar = "∞"
            status = "🟢"
            cap_line = "No cap"
        else:
            pct = min(spent / cap, 1.0)
            filled = int(pct * 10)
            bar = "█" * filled + "░" * (10 - filled)
            remaining = max(0, cap - spent)
            if spent >= cap:
                status = "🔴"  # at/over cap
                cap_line = f"CAP REACHED (${spent:.0f} / ${cap:.0f})"
            elif pct >= 0.8:
                status = "🟡"  # close to cap
                cap_line = f"${remaining:.0f} to cap"
            else:
                status = "🟢"
                cap_line = f"${remaining:.0f} to cap"
            # Miles = base pts from capped spend * multiplier
            capped_spend = min(spent, cap)
            base_pts = capped_spend  # 1pt per $1 base (simplified)
            miles = round(capped_spend * mpd * mult)

        total_miles += miles
        lines.append(
            f"{status} {emoji} *{esc(card)}*\n"
            f"   `{bar}` ${spent:.0f} spent\n"
            f"   {esc(cap_line)} · ~{miles:,} miles"
        )

    lines.append("\n🌏 *Est\\. miles this month: {:,}*".format(total_miles))
    await update.message.reply_text("\n\n".join(lines), parse_mode="MarkdownV2")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Usage: /delete <id>  (get IDs from /recent)")
    ok = delete_transaction(int(ctx.args[0]))
    await update.message.reply_text(f"✅ Deleted #{ctx.args[0]}." if ok else f"❌ No transaction #{ctx.args[0]}.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    text = update.message.text.strip()
    uid  = update.effective_user.id

    if uid in pending:
        exp = pending[uid]
        if text.lower() in ("yes","y","✅","ok","yep","yeah","confirm"):
            tid = insert_transaction(exp["date"],exp["desc"],exp["category"],
                                     exp["total"],exp["my_amt"],exp["card"],exp["qualifying"])
            del pending[uid]
            sp = f"\n   You pay: *${exp['my_amt']:.2f}*" if abs(exp["my_amt"]-exp["total"])>0.01 else ""
            await update.message.reply_text(
                f"✅ Saved \\#{tid}\n\n"
                f"{CAT_EMOJI.get(exp['category'],'📌')} *{esc(exp['desc'])}*\n"
                f"   ${exp['total']:.2f}{sp}\n"
                f"   {CARD_EMOJI.get(exp['card'],'💳')} {esc(exp['card'])} · {esc(exp['category'])} · {exp['date']}",
                parse_mode="MarkdownV2")
            return
        elif text.lower() in ("no","n","❌","cancel","nope"):
            del pending[uid]
            await update.message.reply_text("❌ Cancelled.")
            return

    thinking = await update.message.reply_text("⏳ Parsing…")
    try:
        parsed = parse_expense_with_claude(text)
    except Exception as e:
        log.error(f"Parse error: {e}")
        await thinking.delete()
        await update.message.reply_text("⚠️ Couldn't parse that. Try: `45.50 luckin coffee citi rewards`")
        return

    await thinking.delete()
    if "error" in parsed:
        await update.message.reply_text("🤔 I couldn't find an expense\\. Try: `39 los tacos citi rewards`", parse_mode="MarkdownV2")
        return

    pending[uid] = parsed
    sp = f"\n   Your share: *${parsed['my_amt']:.2f}*" if abs(parsed.get("my_amt",0)-parsed.get("total",0))>0.01 else ""
    note = f"\n   _{esc(parsed['note'])}_" if parsed.get("note") else ""
    conf = {"high":"✅","medium":"🟡","low":"⚠️"}.get(parsed.get("confidence","high"),"✅")
    await update.message.reply_text(
        f"{conf} *Got it — confirm?*\n\n"
        f"{CAT_EMOJI.get(parsed.get('category',''),'📌')} *{esc(parsed.get('desc',''))}*\n"
        f"   ${parsed.get('total',0):.2f}{sp}\n"
        f"   {CARD_EMOJI.get(parsed.get('card',''),'💳')} {esc(parsed.get('card',''))} · {esc(parsed.get('category',''))}\n"
        f"   📅 {parsed.get('date','')}{note}\n\n"
        "Reply *yes* to save or *no* to cancel",
        parse_mode="MarkdownV2")

def esc(text):
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

# ── MAIN ──────────────────────────────────────────────────────────
def run_flask():
    log.info(f"Dashboard starting on port {PORT}…")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask dashboard thread started.")

    tg = Application.builder().token(TELEGRAM_TOKEN).build()
    tg.add_handler(CommandHandler("start",   cmd_start))
    tg.add_handler(CommandHandler("help",    cmd_help))
    tg.add_handler(CommandHandler("recent",  cmd_recent))
    tg.add_handler(CommandHandler("summary", cmd_summary))
    tg.add_handler(CommandHandler("miles",   cmd_miles))
    tg.add_handler(CommandHandler("delete",  cmd_delete))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram bot polling…")
    tg.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
