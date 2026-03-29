"""
FinBot — Personal Finance Telegram Bot
Logs expenses conversationally using Claude to parse natural language.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, date
from anthropic import Anthropic
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ── CONFIG ────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID  = int(os.environ["ALLOWED_USER_ID"])   # your Telegram user ID
DB_PATH          = os.environ.get("DB_PATH", "finbot.db")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
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

# ── DATABASE ──────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
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

def insert_transaction(date, desc, category, total, my_amt, card, qualifying="Yes", typ="expense"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO transactions (date, desc, category, total, my_amt, card, qualifying, type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, desc, category, round(total,2), round(my_amt,2), card, qualifying, typ, datetime.now().isoformat()))
    tid = c.lastrowid
    conn.commit()
    conn.close()
    return tid

def get_transactions(year=None, month=None, limit=10, typ=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    where = []
    params = []
    if year and month:
        where.append("strftime('%Y-%m', date) = ?")
        params.append(f"{year:04d}-{month:02d}")
    if typ:
        where.append("type = ?")
        params.append(typ)
    where_str = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ORDER BY date DESC, id DESC"
    lim = f"LIMIT {limit}" if limit else ""
    c.execute(f"SELECT id, date, desc, category, total, my_amt, card, qualifying, type FROM transactions {where_str} {order} {lim}", params)
    rows = c.fetchall()
    conn.close()
    return rows

def delete_transaction(tid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM transactions WHERE id = ?", (tid,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_summary(year, month):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    ym = f"{year:04d}-{month:02d}"
    c.execute("""
        SELECT category, SUM(my_amt)
        FROM transactions
        WHERE strftime('%Y-%m', date) = ? AND type = 'expense'
        GROUP BY category
        ORDER BY SUM(my_amt) DESC
    """, (ym,))
    cats = c.fetchall()
    c.execute("""
        SELECT SUM(my_amt) FROM transactions
        WHERE strftime('%Y-%m', date) = ? AND type = 'expense'
    """, (ym,))
    total_exp = (c.fetchone()[0] or 0)
    c.execute("""
        SELECT COUNT(*) FROM transactions
        WHERE strftime('%Y-%m', date) = ? AND type = 'expense'
    """, (ym,))
    count = c.fetchone()[0]
    conn.close()
    return cats, total_exp, count

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
- If amount has $ sign or is just a number, that's the total
- "split with X" or "half" means my_amt = total / 2
- Dates: if not mentioned, use today ({date.today().isoformat()})
- qualifying: "Yes" unless it's a cash transaction or clearly ineligible

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

If you cannot parse an expense at all (not a finance message), return:
{{"error": "not an expense"}}
"""

def parse_expense_with_claude(text: str) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": text}]
    )
    raw = resp.content[0].text.strip()
    # strip any accidental markdown fences
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ── SECURITY ──────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID

async def reject(update: Update):
    await update.message.reply_text("⛔ Unauthorised.")

# ── PENDING STATE ─────────────────────────────────────────────────
# Stores parsed expense awaiting confirmation per user
pending: dict[int, dict] = {}

# ── HANDLERS ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    msg = (
        "👋 *FinBot* — your personal expense logger\n\n"
        "Just type your expense naturally:\n"
        "• `45 luckin coffee citi rewards`\n"
        "• `grabbed food panda 16.70`\n"
        "• `120 watsons for mom split equally ocbc`\n"
        "• `beauty and the beast 174.80 split dbs wwmc`\n\n"
        "*Commands:*\n"
        "/recent — last 10 transactions\n"
        "/summary — this month's P\\&L\n"
        "/delete \\<id\\> — remove a transaction\n"
        "/help — show this message"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    rows = get_transactions(limit=10)
    if not rows:
        return await update.message.reply_text("No transactions yet.")
    lines = ["*Recent transactions:*\n"]
    for r in rows:
        tid, date_, desc, cat, total, my_amt, card, qual, typ = r
        emoji = CAT_EMOJI.get(cat, "📌")
        split_note = f" \\(you: ${my_amt:.2f}\\)" if abs(my_amt - total) > 0.01 else ""
        lines.append(
            f"`#{tid}` {emoji} *{esc(desc)}*\n"
            f"   ${total:.2f}{split_note} · {esc(card)} · {date_}\n"
            f"   _{esc(cat)}_"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="MarkdownV2")

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    now = datetime.now()
    cats, total_exp, count = get_summary(now.year, now.month)
    month_name = now.strftime("%B %Y")

    if not cats:
        return await update.message.reply_text(f"No expenses logged for {month_name} yet.")

    lines = [f"📊 *{esc(month_name)}* — {count} transactions\n"]
    for cat, amt in cats:
        bar_len = int((amt / total_exp) * 12) if total_exp else 0
        bar = "█" * bar_len + "░" * (12 - bar_len)
        emoji = CAT_EMOJI.get(cat, "📌")
        lines.append(f"{emoji} {esc(cat)}\n`{bar}` ${amt:.2f}")

    lines.append(f"\n💸 *Total variable: ${total_exp:.2f}*")
    await update.message.reply_text("\n\n".join(lines), parse_mode="MarkdownV2")

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    args = ctx.args
    if not args or not args[0].isdigit():
        return await update.message.reply_text("Usage: /delete <id>  (get IDs from /recent)")
    tid = int(args[0])
    ok = delete_transaction(tid)
    if ok:
        await update.message.reply_text(f"✅ Transaction #{tid} deleted.")
    else:
        await update.message.reply_text(f"❌ No transaction found with ID #{tid}.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return await reject(update)
    text = update.message.text.strip()
    uid = update.effective_user.id

    # ── Confirmation replies ──────────────────────────────────────
    if uid in pending:
        exp = pending[uid]
        if text.lower() in ("yes", "y", "✅", "ok", "yep", "yeah", "confirm"):
            tid = insert_transaction(
                exp["date"], exp["desc"], exp["category"],
                exp["total"], exp["my_amt"], exp["card"], exp["qualifying"]
            )
            del pending[uid]
            emoji = CAT_EMOJI.get(exp["category"], "📌")
            card_e = CARD_EMOJI.get(exp["card"], "💳")
            split_note = f"\n   You pay: *${exp['my_amt']:.2f}*" if abs(exp["my_amt"] - exp["total"]) > 0.01 else ""
            await update.message.reply_text(
                f"✅ Saved \\#{tid}\n\n"
                f"{emoji} *{esc(exp['desc'])}*\n"
                f"   ${exp['total']:.2f}{split_note}\n"
                f"   {card_e} {esc(exp['card'])} · {esc(exp['category'])} · {exp['date']}",
                parse_mode="MarkdownV2"
            )
            return
        elif text.lower() in ("no", "n", "❌", "cancel", "nope"):
            del pending[uid]
            await update.message.reply_text("❌ Cancelled. Send the expense again if you want to re-enter it.")
            return
        # else fall through and try to parse as new expense

    # ── Parse as expense ──────────────────────────────────────────
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
        await update.message.reply_text(
            "🤔 I couldn't find an expense in that message\\.\n\n"
            "Try something like:\n"
            "`39.15 los tacos citi rewards`\n"
            "`28 grab hsbc revo`",
            parse_mode="MarkdownV2"
        )
        return

    # Store pending and ask for confirmation
    pending[uid] = parsed
    emoji = CAT_EMOJI.get(parsed.get("category",""), "📌")
    card_e = CARD_EMOJI.get(parsed.get("card",""), "💳")
    split_note = f"\n   Your share: *${parsed['my_amt']:.2f}*" if abs(parsed.get("my_amt",0) - parsed.get("total",0)) > 0.01 else ""
    note = f"\n   _{esc(parsed['note'])}_" if parsed.get("note") else ""
    conf_emoji = {"high":"✅","medium":"🟡","low":"⚠️"}.get(parsed.get("confidence","high"),"✅")

    msg = (
        f"{conf_emoji} *Got it — confirm?*\n\n"
        f"{emoji} *{esc(parsed.get('desc',''))}*\n"
        f"   ${parsed.get('total',0):.2f}{split_note}\n"
        f"   {card_e} {esc(parsed.get('card',''))} · {esc(parsed.get('category',''))}\n"
        f"   📅 {parsed.get('date','')}"
        f"{note}\n\n"
        "Reply *yes* to save or *no* to cancel"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

def esc(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

# ── MAIN ──────────────────────────────────────────────────────────
def main():
    init_db()
    log.info("Database initialised.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("recent",  cmd_recent))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("delete",  cmd_delete))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting — polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
