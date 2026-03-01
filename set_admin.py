import os
import psycopg2

print("SCRIPT BASLADI")

db_url = os.getenv("DATABASE_URL")
if not db_url:
    raise SystemExit("HATA: DATABASE_URL set degil. PowerShell'de once `$env:DATABASE_URL=...` yap.")

conn = psycopg2.connect(db_url, sslmode="require")
cur = conn.cursor()

cur.execute("UPDATE users SET role=%s WHERE username=%s", ("admin", "erdmgclr"))
conn.commit()

print("ADMIN YAPILDI")

cur.close()
conn.close()
