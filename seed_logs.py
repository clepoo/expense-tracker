"""
seed_logs.py — Run once to populate the logs table with historical data.
Safe to re-run — only clears and re-inserts logs, leaves transactions/sales/recurring untouched.
"""
import sqlite3, os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "finbot.db")
TURSO_URL = os.environ.get("TURSO_URL")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN")

try:
    import libsql_experimental as libsql
    USE_TURSO = bool(TURSO_URL)
except ImportError:
    USE_TURSO = False

LOGS_DATA = [
  {
    "id": 1,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving - CDC Driving Package",
    "amount": 96.6,
    "note": ""
  },
  {
    "id": 2,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Enrolment Fee",
    "amount": 120.0,
    "note": ""
  },
  {
    "id": 3,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 1 + 2",
    "amount": 130.0,
    "note": ""
  },
  {
    "id": 4,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 3",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 5,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 4",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 6,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 5",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 7,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 6",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 8,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving - CDC Top Up - FTT ",
    "amount": 10.0,
    "note": ""
  },
  {
    "id": 9,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Simulator",
    "amount": 80.53,
    "note": ""
  },
  {
    "id": 10,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 7",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 11,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 8",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 12,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 9",
    "amount": 60.0,
    "note": ""
  },
  {
    "id": 13,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 10 (Circuit)",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 14,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving - FTT",
    "amount": 9.44,
    "note": ""
  },
  {
    "id": 15,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 11 (Circuit)",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 16,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 12 (Circuit)",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 17,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Practical Booking",
    "amount": 36.0,
    "note": ""
  },
  {
    "id": 18,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 13 (Circuit)",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 19,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 14",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 20,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 15",
    "amount": 65.0,
    "note": ""
  },
  {
    "id": 21,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 16",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 22,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 17",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 23,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 18",
    "amount": 110.0,
    "note": ""
  },
  {
    "id": 24,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 19",
    "amount": 60.0,
    "note": ""
  },
  {
    "id": 25,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving Lesson 20 + Driving Test",
    "amount": 580.0,
    "note": ""
  },
  {
    "id": 26,
    "category": "Driving",
    "date": "2025-01-01",
    "desc": "Driving - QDL Application Fee",
    "amount": 50.0,
    "note": ""
  },
  {
    "id": 27,
    "category": "Invisalign",
    "date": "2024-01-01",
    "desc": "Q & M - Invisalign Consult 1",
    "amount": 258.95,
    "note": ""
  },
  {
    "id": 28,
    "category": "Invisalign",
    "date": "2024-01-01",
    "desc": "Q & M - Invisalign Xray",
    "amount": 91.55,
    "note": ""
  },
  {
    "id": 29,
    "category": "Invisalign",
    "date": "2024-01-01",
    "desc": "Invisalign Deposit",
    "amount": 450.0,
    "note": ""
  },
  {
    "id": 30,
    "category": "Invisalign",
    "date": "2024-01-01",
    "desc": "Invisalign Deposit",
    "amount": 1200.0,
    "note": ""
  },
  {
    "id": 31,
    "category": "Invisalign",
    "date": "2024-01-01",
    "desc": "Invisalign Deposit",
    "amount": 352.0,
    "note": ""
  },
  {
    "id": 32,
    "category": "Invisalign",
    "date": "2024-01-01",
    "desc": "Invisalign Deposit",
    "amount": 505.0,
    "note": ""
  },
  {
    "id": 33,
    "category": "Lasik",
    "date": "2021-11-19",
    "desc": "Pre-assessment",
    "amount": 288.9,
    "note": ""
  },
  {
    "id": 34,
    "category": "Lasik",
    "date": "2021-11-27",
    "desc": "ICL Assessment",
    "amount": 3630.51,
    "note": ""
  },
  {
    "id": 35,
    "category": "Lasik",
    "date": "2021-12-23",
    "desc": "Surgery and everything else",
    "amount": 5417.54,
    "note": ""
  },
  {
    "id": 36,
    "category": "Lasik",
    "date": "2021-12-24",
    "desc": "Consultation",
    "amount": 85.6,
    "note": ""
  },
  {
    "id": 37,
    "category": "Lasik",
    "date": "2021-12-30",
    "desc": "Consultation + eye drops",
    "amount": 120.91,
    "note": ""
  },
  {
    "id": 38,
    "category": "Lasik",
    "date": "2022-01-20",
    "desc": "1 Month Review",
    "amount": 118.4,
    "note": ""
  },
  {
    "id": 39,
    "category": "Lasik",
    "date": "2022-01-17",
    "desc": "Discomfort Consultation + eye lubricant",
    "amount": 104.86,
    "note": ""
  },
  {
    "id": 40,
    "category": "Lasik",
    "date": "2022-04-27",
    "desc": "Consultation",
    "amount": 203.3,
    "note": ""
  },
  {
    "id": 41,
    "category": "Lasik",
    "date": "2023-07-17",
    "desc": "Consultation",
    "amount": 165.24,
    "note": ""
  },
  {
    "id": 42,
    "category": "Lasik",
    "date": "2026-01-10",
    "desc": "Eagle Eye Consultation",
    "amount": 409.3,
    "note": "Reimbursed $353"
  }
]

def seed():
    if USE_TURSO:
        import libsql_experimental as libsql
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        print(f"Connected to Turso: {TURSO_URL}")
    else:
        conn = sqlite3.connect(DB_PATH)
        print(f"Connected to SQLite: {DB_PATH}")

    conn.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
        date TEXT NOT NULL, desc TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY, data TEXT NOT NULL)""")

    conn.execute("DELETE FROM logs")
    print("Cleared existing logs.")

    now = datetime.now().isoformat()
    for l in LOGS_DATA:
        conn.execute(
            "INSERT INTO logs (category,date,desc,amount,note,created_at) VALUES (?,?,?,?,?,?)",
            (l["category"], l["date"], l["desc"], l["amount"], l.get("note",""), now)
        )
    print(f"Inserted {len(LOGS_DATA)} log entries.")

    conn.commit()
    if USE_TURSO:
        try: conn.sync()
        except: pass
    conn.close()
    print("\n✅ Logs seed complete.")

if __name__ == "__main__":
    seed()
