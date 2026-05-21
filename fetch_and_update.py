"""
台灣股市三大法人買超/賣超追蹤 v10

新增：
1. 獨立設定檔 config.py（所有參數、族群表、名稱對照集中管理）
2. 族群聯動改為每日累積（新資料插最上面，舊的加分隔線）
3. 族群聯動名稱從 CODE_NAME_MAP 補全，不再依賴買超榜
"""

import subprocess, json, gspread, sys, os, time
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# ★ v10：從獨立設定檔載入所有參數
try:
    import config as _cfg
    SPREADSHEET_ID = _cfg.SPREADSHEET_ID
    _cf = _cfg.CREDENTIALS_FILE
    CREDENTIALS_FILE = _cf if _cf else os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
    RISK_LOW = _cfg.RISK_LOW
    RISK_MID = _cfg.RISK_MID
    CHIP_HIGH = _cfg.CHIP_HIGH
    CHIP_MID = _cfg.CHIP_MID
    TRUST_STAR = _cfg.TRUST_STAR
    TRUST_FIRE = _cfg.TRUST_FIRE
    MARGIN_WARN = _cfg.MARGIN_WARN
    SECTOR_MAP = _cfg.SECTOR_MAP
    CODE_NAME_MAP = _cfg.CODE_NAME_MAP
    print("✅ 已載入 config.py")
except ImportError:
    print("⚠️ 找不到 config.py，使用主程式內建預設值")
    CODE_NAME_MAP = {}

# ═══════════════════════════════════════════════
# ★ 固定系統設定（不透過 config.py 控制）
# ═══════════════════════════════════════════════
COOKIE_FILE = "/tmp/twse_cookie.txt"

# ═══════════════════════════════════════════════
# curl 工具
# ═══════════════════════════════════════════════
def curl_get(url):
    result = subprocess.run([
        "curl", "-s", "--max-time", "20",
        "-c", COOKIE_FILE, "-b", COOKIE_FILE,
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "-H", "Referer: https://www.twse.com.tw/zh/trading/foreign/t86.html",
        "-H", "Accept: application/json, text/plain, */*",
        url
    ], capture_output=True, text=True)
    return result.stdout.strip()

def warm_up_cookie():
    subprocess.run([
        "curl", "-s", "-c", COOKIE_FILE, "-b", COOKIE_FILE,
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "https://www.twse.com.tw/zh/trading/foreign/t86.html",
        "-o", "/dev/null"
    ], capture_output=True)

def to_int(s):
    try: return int(str(s).replace(",","").strip())
    except: return 0

def fmt_date(date_str):
    return f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"

# ═══════════════════════════════════════════════
# 抓三大法人買超 + 賣超
# ═══════════════════════════════════════════════
def fetch_institutional(date_str):
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json"
    text = curl_get(url)
    if not text or text.startswith("<"):
        raise ValueError("TWSE 回傳非 JSON")
    data = json.loads(text)
    if data.get("stat") != "OK":
        raise ValueError(f"無資料：{data.get('stat','')}")

    def top10_buy(buy_col, sell_col, net_col):
        result = [
            {"code": r[0].strip(), "name": r[1].strip(),
             "buy": to_int(r[buy_col]), "sell": to_int(r[sell_col]),
             "net": to_int(r[net_col]), "avg_price": 0.0}
            for r in data["data"] if to_int(r[net_col]) > 0
        ]
        return sorted(result, key=lambda x: x["net"], reverse=True)[:10]

    def top10_sell(buy_col, sell_col, net_col):
        result = [
            {"code": r[0].strip(), "name": r[1].strip(),
             "buy": to_int(r[buy_col]), "sell": to_int(r[sell_col]),
             "net": to_int(r[net_col]), "avg_price": 0.0}
            for r in data["data"] if to_int(r[net_col]) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:10]

    return (
        top10_buy(2,3,4), top10_buy(5,6,7), top10_buy(8,9,10),
        top10_sell(2,3,4), top10_sell(5,6,7), top10_sell(8,9,10)
    )

# ═══════════════════════════════════════════════
# 抓個股日資料（均價 + 收盤價 + 成交量）
# ═══════════════════════════════════════════════
def fetch_stock_day(code, date_str):
    """回傳 (avg_price, close_price, volume_lots, pct_str)
    volume_lots = 成交股數 → 張數（1張=1千股）
    pct_str     = 漲跌幅字串，如 "+3.52%" / "-1.20%"，無法計算時為 "N/A"
    """
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={date_str}&stockNo={code}&response=json"
    text = curl_get(url)
    if not text or text.startswith("<"):
        return 0.0, 0.0, 0, "N/A"
    try:
        data = json.loads(text)
        if data.get("stat") != "OK" or not data.get("data"):
            return 0.0, 0.0, 0, "N/A"
        year   = int(date_str[:4]) - 1911
        target = f"{year}/{date_str[4:6]}/{date_str[6:]}"
        rows   = data["data"]
        for idx, row in enumerate(rows):
            if row[0].strip() == target:
                shares = float(str(row[1]).replace(",",""))
                amount = float(str(row[2]).replace(",",""))
                close  = float(str(row[6]).replace(",",""))
                volume_lots = int(shares / 1000)
                avg    = round(amount / shares, 2) if shares > 0 else 0.0
                # 漲跌幅：用前一行收盤計算（API 回傳整月資料）
                if idx > 0:
                    prev    = float(str(rows[idx-1][6]).replace(",",""))
                    pct     = (close - prev) / prev * 100 if prev > 0 else 0.0
                    sign    = "+" if pct >= 0 else ""
                    pct_str = f"{sign}{pct:.2f}%"
                else:
                    pct_str = "N/A"
                return avg, close, volume_lots, pct_str
    except Exception:
        pass
    return 0.0, 0.0, 0, "N/A"

# ═══════════════════════════════════════════════
# ★ 新增 v7：抓融資融券資料
# ═══════════════════════════════════════════════
def fetch_margin(code, date_str):
    """
    回傳 (margin_balance, margin_change, short_balance)
    margin_balance = 融資餘額(張)
    margin_change  = 當日融資增減(張)，正=增加, 負=減少
    short_balance  = 融券餘額(張)
    來源：TWSE MI_MARGN API
    """
    url = (f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
           f"?date={date_str}&selectType=STOCK&response=json")
    text = curl_get(url)
    if not text or text.startswith("<"):
        return 0, 0, 0
    try:
        data = json.loads(text)
        if data.get("stat") != "OK" or not data.get("data"):
            return 0, 0, 0
        for row in data["data"]:
            if len(row) < 20:
                continue
            row_code = str(row[0]).strip()
            if row_code != code:
                continue
            # 欄位說明（MI_MARGN 格式）：
            # [0]代號 [1]名稱
            # 融資: [2]買進 [3]賣出 [4]現金償還 [5]今日餘額 [6]前日餘額 [7]增減
            # 融券: [8]賣出 [9]買進 [10]現券償還 [11]今日餘額 [12]前日餘額 [13]增減
            margin_balance = to_int(row[5])
            margin_prev    = to_int(row[6])
            margin_change  = margin_balance - margin_prev   # 正=增, 負=減
            short_balance  = to_int(row[11])
            return margin_balance, margin_change, short_balance
    except Exception:
        pass
    return 0, 0, 0

def enrich_with_prices(groups, date_str):
    """批次抓均價、收盤價、成交量、漲跌幅，去重複"""
    all_codes = {}
    for group in groups:
        for stock in group:
            all_codes[stock["code"]] = (0.0, 0.0, 0, "N/A")

    total = len(all_codes)
    print(f"  抓取 {total} 支股票價格（每支間隔 0.5 秒）...")
    for i, code in enumerate(all_codes):
        avg, close, vol, pct_str = fetch_stock_day(code, date_str)
        all_codes[code] = (avg, close, vol, pct_str)
        print(f"  [{i+1}/{total}] {code}: 均價={avg:.2f} 收盤={close:.2f} 成交={vol:,}張 漲跌={pct_str}")
        if i < total - 1:
            time.sleep(0.5)

    for group in groups:
        for stock in group:
            avg, close, vol, pct_str = all_codes.get(stock["code"], (0.0, 0.0, 0, "N/A"))
            stock["avg_price"] = avg
            stock["close"]     = close
            stock["volume"]    = vol
            stock["pct_str"]   = pct_str   # ★ 新增漲跌幅

def enrich_with_margin(groups, date_str):
    """★ v7 新增：批次抓融資融券資料"""
    all_codes = {}
    for group in groups:
        for stock in group:
            all_codes[stock["code"]] = (0, 0, 0)

    total = len(all_codes)
    print(f"  抓取 {total} 支股票融資融券（每支間隔 0.5 秒）...")

    # MI_MARGN 一次回傳全市場，只需抓一次
    url = (f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
           f"?date={date_str}&selectType=STOCK&response=json")
    text = curl_get(url)
    margin_map = {}
    if text and not text.startswith("<"):
        try:
            data = json.loads(text)
            if data.get("stat") == "OK" and data.get("data"):
                for row in data["data"]:
                    if len(row) < 14:
                        continue
                    row_code = str(row[0]).strip()
                    mb = to_int(row[5])
                    mp = to_int(row[6])
                    sb = to_int(row[11])
                    margin_map[row_code] = (mb, mb - mp, sb)
        except Exception as e:
            print(f"  ⚠️ 融資融券解析失敗：{e}")

    for group in groups:
        for stock in group:
            code = stock["code"]
            mb, mc, sb = margin_map.get(code, (0, 0, 0))
            stock["margin_balance"] = mb   # 融資餘額
            stock["margin_change"]  = mc   # 融資增減（正=增加）
            stock["short_balance"]  = sb   # 融券餘額

    found = sum(1 for c in all_codes if c in margin_map)
    print(f"  ✅ 融資融券 找到 {found}/{total} 支")

# ═══════════════════════════════════════════════
# 分析工具
# ═══════════════════════════════════════════════
def weighted_avg(entries):
    total_net  = sum(e[1] for e in entries if e[2] > 0)
    total_cost = sum(e[1] * e[2] for e in entries if e[2] > 0)
    return round(total_cost / total_net, 2) if total_net > 0 else 0.0

def unique_days(entries):
    return len(set(e[0] for e in entries))

def calc_risk(current_price, w_avg):
    if current_price <= 0 or w_avg <= 0:
        return "", ""
    pct = (current_price - w_avg) / w_avg
    pct_str = f"{pct*100:.1f}%"
    if pct < RISK_LOW:
        return pct_str, "🟢 低"
    elif pct < RISK_MID:
        return pct_str, "🟡 中"
    else:
        return pct_str, "🔴 高"

def calc_trend(entries, n=3):
    sorted_entries = sorted(entries, key=lambda e: e[0], reverse=True)
    recent = [e[1] for e in sorted_entries[:n]]
    if len(recent) < 2:
        return "-"
    if all(recent[i] >= recent[i+1] for i in range(len(recent)-1)):
        return "📈 遞增中"
    elif all(recent[i] <= recent[i+1] for i in range(len(recent)-1)):
        return "📉 遞減中"
    else:
        return "➡️ 持平"

def calc_signal(code, buy_entries, sell_hist, disp):
    signals = []
    recent_sell_dates = [e[0] for e in sell_hist.get(code, [])]
    if disp in recent_sell_dates:
        signals.append("🔴 今日賣超")
    buy_dates = sorted(set(e[0] for e in buy_entries), reverse=True)
    if buy_dates and buy_dates[0] != disp:
        signals.append("⚠️ 已停止買入")
    return " ".join(signals) if signals else "✅ 持續買入"

# ★ v7 新增：籌碼集中度
def calc_chip_concentration(total_net_lots, volume_lots):
    """
    total_net_lots: 三法人合計買超張數（當日）
    volume_lots:    當日成交量（張）
    回傳 (pct_str, label)
    """
    if volume_lots <= 0 or total_net_lots <= 0:
        return "", ""
    pct = total_net_lots / volume_lots
    pct_str = f"{pct*100:.1f}%"
    if pct >= CHIP_HIGH:
        return pct_str, "🔵 高度集中"
    elif pct >= CHIP_MID:
        return pct_str, "🟦 中度集中"
    else:
        return pct_str, "⬜ 偏低"

# ★ v7 新增：投信連續買超標記
def calc_trust_label(t_consec):
    if t_consec >= TRUST_FIRE:
        return f"🔥 連續{t_consec}天"
    elif t_consec >= TRUST_STAR:
        return f"⭐ 連續{t_consec}天"
    elif t_consec > 0:
        return f"{t_consec}天"
    return ""

# ★ v7 新增：融資健康度判斷
def calc_margin_health(margin_change, total_net_lots):
    """
    margin_change:   當日融資增減（正=增加）
    total_net_lots:  三法人合計買超張數
    回傳說明字串
    """
    if margin_change == 0 and total_net_lots == 0:
        return ""
    if total_net_lots > 0 and margin_change <= 0:
        return "✅ 籌碼乾淨"      # 法人買、散戶沒有跟（甚至在還券）
    elif total_net_lots > 0 and 0 < margin_change < MARGIN_WARN:
        return "🟡 小幅跟進"      # 法人買、少量散戶跟
    elif total_net_lots > 0 and margin_change >= MARGIN_WARN:
        return "⚠️ 散戶大量跟進"  # 法人買、大量散戶跟，籌碼偏雜
    elif total_net_lots <= 0 and margin_change > 0:
        return "🔴 法人不買散戶買" # 危險訊號
    return ""

# ═══════════════════════════════════════════════
# Google Sheets 工具
# ═══════════════════════════════════════════════
def connect_sheets():
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

def get_or_create(ss, name, cols=15):
    try: return ss.worksheet(name)
    except: return ss.add_worksheet(title=name, rows=300, cols=cols)

def prepend_block(ws, new_block, disp, date_marker_prefix, sep_cols):
    existing = ws.get_all_values()
    if existing and existing[0] and existing[0][0].startswith(f"{date_marker_prefix}{disp}"):
        end_row = len(existing)
        for i in range(1, len(existing)):
            cell = existing[i][0] if existing[i] else ""
            if cell.startswith(date_marker_prefix) and disp not in cell:
                end_row = i
                break
            if cell.startswith("─") and disp in (existing[i][1] if len(existing[i]) > 1 else ""):
                end_row = i + 1
                break
        ws.delete_rows(1, end_row)
        existing = ws.get_all_values()

    if existing and any(any(c for c in row) for row in existing):
        sep = [["─" * 20, f"以上為 {disp}"] + [""] * (sep_cols - 2)]
        full_data = new_block + sep + existing
    else:
        full_data = new_block

    ws.clear()
    if ws.row_count < len(full_data) + 10:
        ws.add_rows(len(full_data) + 10 - ws.row_count)
    ws.update("A1", full_data)

# ═══════════════════════════════════════════════
# 更新各工作表
# ═══════════════════════════════════════════════
def update_buy_sheet(ss, date_str, foreign, trust, dealer):
    ws   = get_or_create(ss, "今日買超排行", 10)
    disp = fmt_date(date_str)
    hdrs = ["名次","代號","股票名稱","買進(張)","賣出(張)","買超(張)",
            "當日均價(元)","收盤價(元)","成交量(張)","籌碼集中度"]   # ★ v7 新增後兩欄
    block = [[f"資料日期：{disp}"] + [""]*9]
    for label, data in [("外資及陸資買超前十名", foreign),
                        ("投信買超前十名",        trust),
                        ("自營商買超前十名",      dealer)]:
        block += [[f"【{label}】"]+[""]*9, hdrs]
        for i, r in enumerate(data):
            vol = r.get("volume", 0)
            chip_pct, chip_lbl = calc_chip_concentration(r["net"], vol)
            chip_str = f"{chip_pct} {chip_lbl}".strip() if chip_pct else ""
            block.append([i+1, r["code"], r["name"], r["buy"], r["sell"],
                          r["net"], r["avg_price"] or "", r.get("close") or "",
                          vol or "", chip_str])
        block += [[""]*10]
    prepend_block(ws, block, disp, "資料日期：", 10)
    print("  ✅ 今日買超排行 更新完成")

def update_sell_sheet(ss, date_str, f_sell, t_sell, d_sell):
    ws   = get_or_create(ss, "今日賣超排行", 8)
    disp = fmt_date(date_str)
    hdrs = ["名次","代號","股票名稱","買進(張)","賣出(張)","賣超(張)","當日均價(元)","收盤價(元)"]
    block = [[f"資料日期：{disp}"] + [""]*7]
    for label, data in [("外資及陸資賣超前十名", f_sell),
                        ("投信賣超前十名",        t_sell),
                        ("自營商賣超前十名",      d_sell)]:
        block += [[f"【{label}】"]+[""]*7, hdrs]
        for i, r in enumerate(data):
            net_abs = abs(r["net"])
            block.append([i+1, r["code"], r["name"], r["buy"], r["sell"],
                          net_abs, r["avg_price"] or "", r.get("close") or ""])
        block += [[""]*8]
    prepend_block(ws, block, disp, "資料日期：", 8)
    print("  ✅ 今日賣超排行 更新完成")

def append_history(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell):
    ws   = get_or_create(ss, "歷史紀錄", 10)
    disp = fmt_date(date_str)
    if not ws.get_all_values():
        ws.append_row(["日期","名次","代號","股票名稱","法人類別","張數",
                       "當日均價(元)","買/賣","成交量(張)","籌碼集中度"])   # ★ v7 新增後兩欄
    existing = ws.get_all_values()
    for i in reversed(range(len(existing))):
        if existing[i] and existing[i][0] == disp:
            ws.delete_rows(i+1)
    new_rows = []
    for label, data in [("外資",foreign),("投信",trust),("自營商",dealer)]:
        for i, r in enumerate(data):
            vol = r.get("volume", 0)
            chip_pct, chip_lbl = calc_chip_concentration(r["net"], vol)
            chip_str = f"{chip_pct} {chip_lbl}".strip() if chip_pct else ""
            new_rows.append([disp, i+1, r["code"], r["name"], label,
                             r["net"], r["avg_price"] or "", "買超",
                             vol or "", chip_str])
    for label, data in [("外資",f_sell),("投信",t_sell),("自營商",d_sell)]:
        for i, r in enumerate(data):
            new_rows.append([disp, i+1, r["code"], r["name"], label,
                             abs(r["net"]), r["avg_price"] or "", "賣超", "", ""])
    ws.append_rows(new_rows)
    print(f"  ✅ 歷史紀錄 新增 {len(new_rows)} 筆")

def update_analysis(ss, date_str, current_prices, current_margin):
    """
    current_prices: {code: (avg_price, close, volume)} 今日價格快取
    current_margin: {code: (margin_balance, margin_change, short_balance)} 今日融資快取
    """
    hist = get_or_create(ss, "歷史紀錄")
    disp = fmt_date(date_str)
    rows = hist.get_all_values()[1:]

    # ★ 修正4：對照分析統計的是「歷史上所有出現過的股票」，
    # 但 current_prices 只有今日買賣超榜代號。
    # 先收集歷史代號，補抓快取缺少的股票現價，避免現價/籌碼集中度欄位空白。
    all_hist_codes = set(row[2] for row in rows if len(row) > 2 and row[2])
    missing = [c for c in all_hist_codes if c not in current_prices]
    if missing:
        print(f"  📡 補抓歷史股票現價（{len(missing)} 支，快取缺少）...")
        for i, code in enumerate(missing):
            avg, close, vol, pct_str = fetch_stock_day(code, date_str)
            current_prices[code] = (avg, close, vol, pct_str)
            if close > 0:
                print(f"    [{i+1}/{len(missing)}] {code}: 收盤={close:.2f} 成交={vol:,}張")
            if i < len(missing) - 1:
                time.sleep(0.5)
        print(f"  ✅ 補抓完成")

    buy_map   = {}
    sell_hist = {}

    for row in rows:
        if len(row) < 7 or not row[2]: continue
        date, _, code, name, type_, net_s, price_s = row[0],row[1],row[2],row[3],row[4],row[5],row[6]
        buy_sell = row[7] if len(row) > 7 else "買超"
        try: net   = int(str(net_s).replace(",",""))
        except: net = 0
        try: price = float(price_s) if price_s else 0.0
        except: price = 0.0

        k = code or name
        if buy_sell == "買超":
            if k not in buy_map:
                buy_map[k] = {"code":code,"name":name,"f":[],"t":[],"d":[],"last":""}
            entry = (date, net, price)
            if type_=="外資":   buy_map[k]["f"].append(entry)
            if type_=="投信":   buy_map[k]["t"].append(entry)
            if type_=="自營商": buy_map[k]["d"].append(entry)
            if date > buy_map[k]["last"]: buy_map[k]["last"] = date
        else:
            if k not in sell_hist:
                sell_hist[k] = []
            sell_hist[k].append((date, net))

    # ★ v7：欄位擴充（新增 投信標記、籌碼集中度、融資增減、融資健康度）
    headers = [
        "代號","股票名稱",
        "外資累計天數","外資連續天數","外資買超(張)","外資加權均價","外資趨勢",
        "投信累計天數","投信連續天數","投信買超(張)","投信加權均價","投信趨勢","投信標記",   # ★ +投信標記
        "自營商累計天數","自營商連續天數","自營商買超(張)","自營商加權均價","自營商趨勢",
        "合計累計天數","現價","漲幅%","出貨風險","訊號",
        "籌碼集中度%","籌碼集中度評級",   # ★ 新增
        "融資餘額(張)","融資增減(張)","融券餘額(張)","融資健康度",   # ★ 新增
        "最近出現日"
    ]

    all_dates = sorted(set(row[0] for row in rows if row and row[0]))

    # ★ v9：建立每支股票每日三法人合計淨買超 dict
    # net_by_date[code][date] = 合計淨張數（買超+、賣超-）
    net_by_date = {}
    for row in rows:
        if len(row) < 8 or not row[2]: continue
        date, code_r, net_s = row[0], row[2], row[5]
        buy_sell = row[7] if len(row) > 7 else "買超"
        try: net = int(str(net_s).replace(",",""))
        except: net = 0
        sign = 1 if buy_sell == "買超" else -1
        net_by_date.setdefault(code_r, {})
        net_by_date[code_r][date] = net_by_date[code_r].get(date, 0) + sign * net

    def consecutive_days(entries, code):
        """
        ★ v9 新邏輯（方案 A+C）：
        以「三法人合計淨買超 > 0」判斷連續，賣超當天即中斷歸零。
        從最近一次出現在買超榜的日期往回數，只要任一天合計淨 <= 0 就停。
        """
        if not entries: return 0
        last_buy_date = max(e[0] for e in entries)
        if last_buy_date not in all_dates: return 1
        start_idx  = all_dates.index(last_buy_date)
        daily_net  = net_by_date.get(code, {})
        count = 0
        for i in range(start_idx, -1, -1):
            d       = all_dates[i]
            day_net = daily_net.get(d, 0)
            if day_net > 0:
                count += 1
            else:
                break   # 合計淨買超 <= 0（包含賣超）→ 中斷
        return count

    def build_row(s, current_prices, current_margin, sell_hist, disp):
        code = s["code"]
        f_total  = unique_days(s["f"])
        t_total  = unique_days(s["t"])
        d_total  = unique_days(s["d"])
        f_consec = consecutive_days(s["f"], code)
        t_consec = consecutive_days(s["t"], code)
        d_consec = consecutive_days(s["d"], code)
        f_net    = sum(e[1] for e in s["f"])
        t_net    = sum(e[1] for e in s["t"])
        d_net    = sum(e[1] for e in s["d"])
        f_wavg   = weighted_avg(s["f"])
        t_wavg   = weighted_avg(s["t"])
        d_wavg   = weighted_avg(s["d"])
        f_trend  = calc_trend(s["f"])
        t_trend  = calc_trend(s["t"])
        d_trend  = calc_trend(s["d"])

        _, close, volume, price_pct = current_prices.get(code, (0.0, 0.0, 0, "N/A"))
        all_entries = s["f"] + s["t"] + s["d"]
        all_wavg    = weighted_avg(all_entries)
        _, risk = calc_risk(close, all_wavg)
        signal  = calc_signal(code, all_entries, sell_hist, disp)

        # ★ v7 新欄位
        trust_label = calc_trust_label(t_consec)
        today_net   = sum(e[1] for e in all_entries if e[0] == disp)   # 今日三法人合計買超
        chip_pct, chip_lbl = calc_chip_concentration(today_net, volume)

        mb, mc, sb    = current_margin.get(code, (0, 0, 0))
        margin_health = calc_margin_health(mc, today_net)

        return [
            code, s["name"],
            f_total, f_consec, f_net, f_wavg or "", f_trend,
            t_total, t_consec, t_net, t_wavg or "", t_trend, trust_label,   # ★
            d_total, d_consec, d_net, d_wavg or "", d_trend,
            f_total+t_total+d_total,
            close or "", price_pct, risk, signal,
            chip_pct, chip_lbl,   # ★
            mb or "", mc if mb else "", sb or "", margin_health,   # ★
            s["last"]
        ]

    # ★ 修正排序：最近出現日降序（新的在上），同日再按合計累積天數升序
    sorted_stocks = sorted(
        buy_map.values(),
        key=lambda x: (
            x["last"],
            -(unique_days(x["f"]) + unique_days(x["t"]) + unique_days(x["d"]))
        ),
        reverse=True
    )

    # ── 對照分析 ──
    n_cols   = len(headers)
    ws_ana   = get_or_create(ss, "對照分析", n_cols)
    ana_data = [[f"統計截至：{disp}（每次執行自動更新至最新）"] + [""]*(n_cols-1), headers]
    for s in sorted_stocks:
        ana_data.append(build_row(s, current_prices, current_margin, sell_hist, disp))
    ws_ana.clear()
    if ws_ana.row_count < len(ana_data) + 5:
        ws_ana.add_rows(len(ana_data) + 5 - ws_ana.row_count)
    ws_ana.update("A1", ana_data)
    print(f"  ✅ 對照分析 更新完成（{len(ana_data)-2} 支，完整累積統計）")

    # ── 每日快照 ──
    ws_snap    = get_or_create(ss, "每日快照", n_cols)
    snap_block = [[f"統計截至：{disp}"] + [""]*(n_cols-1), headers]
    for s in sorted_stocks:
        snap_block.append(build_row(s, current_prices, current_margin, sell_hist, disp))
    prepend_block(ws_snap, snap_block, disp, "統計截至：", n_cols)
    print(f"  ✅ 每日快照 更新完成（最新快照插入最上方）")

# ═══════════════════════════════════════════════
# ★ v8 族群聯動工作表
# ═══════════════════════════════════════════════
def fetch_stock_quote(code, date_str):
    """
    抓個股當日收盤價、漲跌幅、成交量。
    回傳 (close, change_pct, volume_lots, prev_close)
    change_pct 單位：% 字串，如 "+3.52%" / "-1.20%"
    """
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={date_str}&stockNo={code}&response=json")
    text = curl_get(url)
    if not text or text.startswith("<"):
        return None
    try:
        data = json.loads(text)
        if data.get("stat") != "OK" or not data.get("data"):
            return None
        year   = int(date_str[:4]) - 1911
        target = f"{year}/{date_str[4:6]}/{date_str[6:]}"
        rows   = data["data"]
        for idx, row in enumerate(rows):
            if row[0].strip() != target:
                continue
            shares      = float(str(row[1]).replace(",",""))
            close       = float(str(row[6]).replace(",",""))
            volume_lots = int(shares / 1000)
            # 漲跌幅：用收盤和前一行收盤計算
            if idx > 0:
                prev    = float(str(rows[idx-1][6]).replace(",",""))
                pct     = (close - prev) / prev * 100 if prev > 0 else 0.0
                sign    = "+" if pct >= 0 else ""
                pct_str = f"{sign}{pct:.2f}%"
            else:
                pct_str = "N/A"
            return close, pct_str, volume_lots
    except Exception:
        pass
    return None

def build_sector_in_buy(all_buy_codes):
    """
    回傳當日買超榜出現的所有族群名稱 set。
    all_buy_codes: set of 代號字串
    """
    triggered = {}
    for sector, members in SECTOR_MAP.items():
        hit = [c for c in members if c in all_buy_codes]
        if hit:
            triggered[sector] = hit
    return triggered   # {族群名: [觸發代號, ...]}

def update_sector_sheet(ss, date_str, all_buy_codes, all_buy_names, current_prices):
    """
    ★ v10：族群聯動工作表改為每日累積
    - 新資料插最上面，舊的加分隔線往下（同今日買超排行邏輯）
    - 只列出「今日買超榜有成員」的族群
    - 名稱優先從 CODE_NAME_MAP 取得，其次買超榜，確保完整顯示
    current_prices: {code: (avg, close, volume)} — fallback 用
    """
    disp      = fmt_date(date_str)
    ws        = get_or_create(ss, "族群聯動", 8)
    triggered = build_sector_in_buy(all_buy_codes)

    if not triggered:
        # 今日無觸發：插入一筆無觸發紀錄（仍累積，不覆蓋舊資料）
        no_trigger_block = [
            [f"資料日期：{disp} ｜ 族群聯動分析（有法人買超成員的族群）"] + [""]*7,
            ["今日買超榜無任何族群成員出現。"] + [""]*7,
            [""]*8,
        ]
        prepend_block(ws, no_trigger_block, disp, "資料日期：", 8)
        print("  ✅ 族群聯動 今日無觸發（已記錄）")
        return

    # 收集所有觸發族群的成員
    all_member_codes = set()
    for sector in triggered:
        for c in SECTOR_MAP[sector]:
            all_member_codes.add(c)

    # ★ 修正2：優先用 current_prices 快取，只補抓快取沒有的股票
    # 快取命中的股票不再打 API（Step 2 已抓過），節省時間和降低被限流風險
    member_quote  = {}   # code -> (name, close, pct_str, vol)
    cached_codes  = [c for c in all_member_codes if c in current_prices]
    missing_codes = [c for c in all_member_codes if c not in current_prices]

    # 快取命中：直接取用（含漲跌幅，Step 2 已一併計算）
    for code in cached_codes:
        _, close, vol, pct_str = current_prices[code]
        name = CODE_NAME_MAP.get(code) or all_buy_names.get(code, "")
        member_quote[code] = (name, close, pct_str, vol)

    # 只對快取缺少的補抓（含漲跌幅）
    if missing_codes:
        print(f"  補抓族群成員行情（{len(missing_codes)} 支，快取缺少）...")
        for i, code in enumerate(missing_codes):
            avg, close, vol, pct_str = fetch_stock_day(code, date_str)
            current_prices[code] = (avg, close, vol, pct_str)   # 同步更新快取
            name = CODE_NAME_MAP.get(code) or all_buy_names.get(code, "")
            member_quote[code] = (name, close, pct_str, vol)
            if i < len(missing_codes) - 1:
                time.sleep(0.4)
    else:
        print(f"  族群成員行情全部命中快取（{len(cached_codes)} 支），跳過補抓")

    # 組裝今日區塊
    hdrs = ["代號", "股票名稱", "收盤價", "漲跌幅%", "成交量(張)", "是否在買超榜", "觸發族群", "備註"]
    new_block = [
        [f"資料日期：{disp} ｜ 族群聯動分析（有法人買超成員的族群）"] + [""]*7,
        [""]*8,
    ]

    for sector, hit_codes in triggered.items():
        members      = SECTOR_MAP[sector]
        hit_set      = set(hit_codes)
        trigger_names = [CODE_NAME_MAP.get(c) or all_buy_names.get(c, c) for c in hit_codes]
        new_block.append([f"【{sector}】 觸發成員：{'、'.join(trigger_names)}"] + [""]*7)
        new_block.append(hdrs)
        for code in members:
            name, close, pct_str, vol = member_quote.get(code, ("", 0.0, "N/A", 0))
            in_buy = "✅ 買超榜" if code in hit_set else ""
            new_block.append([
                code,
                name,
                close if close else "",
                pct_str,
                vol if vol else "",
                in_buy,
                sector,
                ""
            ])
        new_block.append([""]*8)   # 族群間空一行

    # ★ v10：使用 prepend_block 累積（新資料在上，舊資料加分隔線往下）
    prepend_block(ws, new_block, disp, "資料日期：", 8)
    print(f"  ✅ 族群聯動 更新完成（{len(triggered)} 個族群觸發，累積至工作表頂部）")

# ═══════════════════════════════════════════════
# 找最近交易日
# ═══════════════════════════════════════════════
def find_trading_day():
    warm_up_cookie()
    d = datetime.now()
    for _ in range(7):
        if d.weekday() < 5:
            date_str = d.strftime("%Y%m%d")
            try:
                print(f"  嘗試 {date_str}...")
                result = fetch_institutional(date_str)
                return (date_str,) + result
            except ValueError as e:
                print(f"  → {e}")
        d -= timedelta(days=1)
    raise RuntimeError("找不到有效交易日")

# ═══════════════════════════════════════════════
# 主程式
# ═══════════════════════════════════════════════
def main():
    print("="*50)
    print(" 台灣股市三大法人買超/賣超追蹤 v10")
    print(" 獨立設定檔 / 族群聯動累積 / 補抓最佳化")
    print("="*50)

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"\n❌ 找不到 credentials.json")
        input("按 Enter 關閉..."); sys.exit(1)

    # Step 1: 抓法人資料
    print("\n📡 Step 1/5 抓取三大法人資料...")
    try:
        date_str, foreign, trust, dealer, f_sell, t_sell, d_sell = find_trading_day()
        disp = fmt_date(date_str)
        print(f"\n  ✅ 資料日期：{disp}")
        if foreign: print(f"  外資買超第1：{foreign[0]['name']} ({foreign[0]['net']:,} 張)")
        if f_sell:  print(f"  外資賣超第1：{f_sell[0]['name']} ({abs(f_sell[0]['net']):,} 張)")
        if trust:   print(f"  投信買超第1：{trust[0]['name']} ({trust[0]['net']:,} 張)")
        if dealer:  print(f"  自營商買超第1：{dealer[0]['name']} ({dealer[0]['net']:,} 張)")
    except Exception as e:
        print(f"\n❌ 抓取失敗：{e}")
        input("按 Enter 關閉..."); sys.exit(1)

    # Step 2: 抓個股價格（含成交量）
    print(f"\n💹 Step 2/5 抓取個股價格與成交量...")
    all_groups = [foreign, trust, dealer, f_sell, t_sell, d_sell]
    try:
        enrich_with_prices(all_groups, date_str)
    except Exception as e:
        print(f"  ⚠️ 價格抓取部分失敗：{e}")

    # Step 3: 抓融資融券
    print(f"\n📋 Step 3/5 抓取融資融券資料...")
    buy_groups = [foreign, trust, dealer]
    try:
        enrich_with_margin(buy_groups, date_str)
    except Exception as e:
        print(f"  ⚠️ 融資融券抓取部分失敗：{e}")

    # 建立快取（含漲跌幅）
    current_prices = {}
    current_margin = {}
    for group in all_groups:
        for stock in group:
            current_prices[stock["code"]] = (
                stock.get("avg_price", 0.0),
                stock.get("close",     0.0),
                stock.get("volume",    0),
                stock.get("pct_str",   "N/A")   # ★ 新增漲跌幅
            )
    for group in buy_groups:
        for stock in group:
            current_margin[stock["code"]] = (
                stock.get("margin_balance", 0),
                stock.get("margin_change",  0),
                stock.get("short_balance",  0)
            )

    # ★ v8：建立今日買超榜代號集合（供族群聯動使用）
    all_buy_codes = set()
    all_buy_names = {}
    for group in buy_groups:
        for stock in group:
            all_buy_codes.add(stock["code"])
            all_buy_names[stock["code"]] = stock["name"]

    # Step 4: 寫入 Sheets
    print("\n📊 Step 4/5 寫入 Google Sheets...")
    try:
        ss = connect_sheets()
        update_buy_sheet(ss, date_str, foreign, trust, dealer)
        update_sell_sheet(ss, date_str, f_sell, t_sell, d_sell)
        append_history(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell)
        update_analysis(ss, date_str, current_prices, current_margin)
    except Exception as e:
        print(f"\n❌ 寫入失敗：{e}")
        input("按 Enter 關閉..."); sys.exit(1)

    # Step 5: ★ v8 族群聯動
    print("\n🔗 Step 5/5 更新族群聯動...")
    try:
        update_sector_sheet(ss, date_str, all_buy_codes, all_buy_names, current_prices)
    except Exception as e:
        print(f"  ⚠️ 族群聯動更新失敗：{e}")

    print(f"\n🎉 完成！")
    print(f"  https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
    input("按 Enter 關閉...")

if __name__ == "__main__":
    main()
