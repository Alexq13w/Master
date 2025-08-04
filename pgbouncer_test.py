import os
import psycopg2

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "6432")
DB_NAME = os.environ.get("DB_NAME", "dbmaster_yehk")
DB_USER = os.environ.get("DB_USER", "dbmaster_yehk_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "FL6nmuzBXyf2EpnPQmYrHMTI0C2tc6Q0")

try:
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    print("Connected successfully.")
    with conn.cursor() as cur:
        cur.execute("SELECT now();")
        print(cur.fetchone())
    conn.close()
except Exception as e:
    print("Connection failed:", e)
