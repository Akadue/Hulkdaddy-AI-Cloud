import psycopg2
import config

def get_db_connection():
    try:
        conn = psycopg2.connect(config.DATABASE_URL)
        return conn
    except Exception as e:
        print(f"資料庫連線失敗: {e}")
        return None