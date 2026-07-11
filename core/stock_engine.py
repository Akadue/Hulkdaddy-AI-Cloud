import twstock

def get_stock_info(stock_code):
    try:
        stock = twstock.Stock(stock_code)
        if not stock.price:
            return f"找不到股票代號 {stock_code} 的資料，請確認是否輸入正確。"
        
        current_price = stock.price[-1]
        open_price = stock.open[-1]
        high_price = stock.high[-1]
        low_price = stock.low[-1]
        
        if len(stock.price) >= 2:
            prev_price = stock.price[-2]
            diff = round(current_price - prev_price, 2)
            diff_str = f"📈 漲跌: +{diff}" if diff > 0 else f"📉 漲跌: {diff}"
        else:
            diff_str = ""

        report = (
            f"📊 台灣股票代號: {stock_code}\n"
            f"💰 最新市價: {current_price} 元\n"
            f"📋 開盤價: {open_price} 元\n"
            f"🔝 最高價: {high_price} 元\n"
            f"🔙 最低價: {low_price} 元\n"
            f"{diff_str}"
        )
        return report
    except Exception as e:
        return f"查詢股票 {stock_code} 時發生錯誤: {str(e)}"