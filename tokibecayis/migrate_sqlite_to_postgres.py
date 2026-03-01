#!/usr/bin/env python3
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SQLITE_DB = str(BASE_DIR / "tokibecayis.db")

def fetch_all_sqlite(conn, table):
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return cols, rows

def main():
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL ortam değişkeni yok. Railway'den DATABASE_URL ekleyip tekrar deneyin.")

    import app as tokapp
    tokapp.ensure_schema()

    pg = tokapp.get_db()
    pgcur = pg.cursor()

    sconn = sqlite3.connect(SQLITE_DB)
    sconn.row_factory = sqlite3.Row

    tables = ["users", "listings", "threads", "messages", "blocks", "reports"]

    for table in tables:
        cols, rows = fetch_all_sqlite(sconn, table)
        if not rows:
            continue

        col_list = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

        values = []
        for r in rows:
            values.append(tuple(r[c] for c in cols))

        pg.executemany(sql, values)
        pg.commit()
        print(f"{table}: {len(values)} satır taşındı")

    # Fix sequences
    for table in tables:
        pgcur.execute(f"SELECT COALESCE(MAX(id), 0) AS mx FROM {table}")
        mx = pgcur.fetchone()
        mxv = mx.get("mx", 0) if isinstance(mx, dict) else (mx[0] if mx else 0)
        pgcur.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), %s, true)",
            (table, int(mxv) if mxv else 1),
        )

    pg.commit()
    pg.close()
    sconn.close()
    print("Tamamlandı. PostgreSQL'e veri taşıma bitti.")

if __name__ == "__main__":
    main()
