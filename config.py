import os

# 系統會自動去雲端環境變數抓取金鑰，不需要在這裡寫死密碼！
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

PORT = int(os.environ.get("PORT", 5000))