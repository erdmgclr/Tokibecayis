import os

username = "erdmgclr"   # buraya admin yapmak istediğin kullanıcı adını yaz

def is_postgres():
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith("postgres://") or url.startswith("postgresql://")

if not is_postgres():
    import sqlite3
    conn = sqlite3.connect("tokibecayis.db")
    cur = conn.cursor()
    cur.execute("UPDATE users SET role='admin' WHERE username=?", (username,))
    conn.commit()
    if cur.rowcount == 0:
        print("Bu kullanıcı adı bulunamadı!")
    else:
        print("Admin yetkisi verildi.")
    conn.close()
else:
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")
    cur = conn.cursor()
    cur.execute("UPDATE users SET role='admin' WHERE username=%s", (username,))
    conn.commit()
    if cur.rowcount == 0:
        print("Bu kullanıcı adı bulunamadı!")
    else:
        print("Admin yetkisi verildi.")
    conn.close()
