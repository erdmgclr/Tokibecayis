import os
import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")
cur = conn.cursor()

cur.execute("UPDATE users SET role=%s WHERE username=%s", ("user", "erdmgclr"))
cur.execute("UPDATE users SET role=%s WHERE username=%s", ("admin", "erdmgclradmin"))

conn.commit()
conn.close()

print("ADMIN DEGISTIRILDI")