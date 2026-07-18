from core.stock_engine import get_stock_info

def handle_text_message(user_message)
    msg = user_message.strip()
    
    # 如果使用者輸入的是純數字（代表可能是股票代號）
    if msg.isdigit()
        return get_stock_info(msg)
    
    # 一般問候
    if 嗨 in msg or hello in msg.lower()
        return 您好！我是您的專屬 AI 投顧秘書。請輸入台灣股票代號（例如：2330），我幫您查詢最新股價！
    
    return f收到訊息：'{msg}'。請輸入 4 位數股票代號讓我為您查詢個股資訊喔！