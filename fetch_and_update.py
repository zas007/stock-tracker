"""
台灣股市三大法人買超/賣超追蹤 v10
優化重點：
1. 修正版本標題（v8 → v10）
2. 合併 fetch_stock_quote / fetch_stock_day → fetch_stock_day_full
3. 族群聯動優先使用 current_prices 快取，不重複打 API
4. build_row 獨立為模組層函式（不再是 inner function）
5. update_analysis 拆分為 _build_history_maps / _build_analysis_rows / update_analysis
"""

import subprocess, json, gspread, sys, os, time, re
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# ★ v10：從獨立設定檔載入所有參數
try:
    import config as _cfg
    SPREADSHEET_ID  = _cfg.SPREADSHEET_ID
    _cf             = _cfg.CREDENTIALS_FILE
    CREDENTIALS_FILE = _cf if _cf else os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
    RISK_LOW    = _cfg.RISK_LOW
    RISK_MID    = _cfg.RISK_MID
    CHIP_HIGH   = _cfg.CHIP_HIGH
    CHIP_MID    = _cfg.CHIP_MID
    TRUST_STAR  = _cfg.TRUST_STAR
    TRUST_FIRE  = _cfg.TRUST_FIRE
    MARGIN_WARN = _cfg.MARGIN_WARN
    SECTOR_MAP  = _cfg.SECTOR_MAP
    CODE_NAME_MAP = _cfg.CODE_NAME_MAP
    print("✅ 已載入 config.py")
except ImportError:
    print("⚠️ 找不到 config.py，使用主程式內建預設值")
    CODE_NAME_MAP = {}

# ═══════════════════════════════════════════════
# ★ 固定系統設定
# ═══════════════════════════════════════════════
COOKIE_FILE = "/tmp/twse_cookie.txt"

# 啟動時從 SECTOR_MAP 建立「代號 → 族群」反查表（程式內自動維護，不需手動）
def _build_code_to_sector():
    return {
        code: sector
        for sector, members in SECTOR_MAP.items()
        for code in members
    }
CODE_TO_SECTOR = {}   # 在 main() 初始化後重建


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
    """支援空字串、dash(-)、全形破折號、逗號數字"""
    s = str(s).replace(",", "").strip()
    if s in ("", "-", "--", "－"):
        return 0
    try: return int(s)
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

    # 診斷：印出實際欄位數，方便日後排查
    sample_len = len(data["data"][0]) if data.get("data") else 0
    print(f"  T86 欄位數：{sample_len}，資料筆數：{len(data.get('data', []))}")
    if sample_len < 19:
        raise ValueError(
            f"T86 欄位數不足（{sample_len} 欄，需要 ≥19）。"
            "可能尚未收盤，三大法人資料通常 16:30 後才有。"
        )

    # 實際欄位（19欄）：
    # [2][3][4]   外陸資(不含外資自營商) 買進/賣出/買超
    # [5][6][7]   外資自營商             買進/賣出/買超
    # [8][9][10]  投信                   買進/賣出/買超
    # [11]        自營商合計買超（直接用）
    # [12][13][14] 自營商自行買賣        買進/賣出/買超
    # [15][16][17] 自營商避險            買進/賣出/買超
    # [18]        三大法人合計買超

    def make_stock(r, buy, sell, net):
        """單位：股 → 張（//1000）"""
        return {
            "code": r[0].strip(), "name": r[1].strip(),
            "buy":  buy  // 1000,
            "sell": sell // 1000,
            "net":  net  // 1000,
            "avg_price": 0.0
        }

    def foreign_buy():
        # 外資 = 外陸資 + 外資自營商
        result = [
            make_stock(r,
                to_int(r[2]) + to_int(r[5]),
                to_int(r[3]) + to_int(r[6]),
                to_int(r[4]) + to_int(r[7]))
            for r in data["data"]
            if (to_int(r[4]) + to_int(r[7])) > 0
        ]
        return sorted(result, key=lambda x: x["net"], reverse=True)[:10]

    def foreign_sell():
        result = [
            make_stock(r,
                to_int(r[2]) + to_int(r[5]),
                to_int(r[3]) + to_int(r[6]),
                to_int(r[4]) + to_int(r[7]))
            for r in data["data"]
            if (to_int(r[4]) + to_int(r[7])) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:10]

    def trust_buy():
        result = [
            make_stock(r, to_int(r[8]), to_int(r[9]), to_int(r[10]))
            for r in data["data"] if to_int(r[10]) > 0
        ]
        return sorted(result, key=lambda x: x["net"], reverse=True)[:10]

    def trust_sell():
        result = [
            make_stock(r, to_int(r[8]), to_int(r[9]), to_int(r[10]))
            for r in data["data"] if to_int(r[10]) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:10]

    def dealer_buy():
        # 自營商合計買超 = [11]，買進/賣出用自行+避險
        result = [
            make_stock(r,
                to_int(r[12]) + to_int(r[15]),
                to_int(r[13]) + to_int(r[16]),
                to_int(r[11]))
            for r in data["data"] if to_int(r[11]) > 0
        ]
        return sorted(result, key=lambda x: x["net"], reverse=True)[:10]

    def dealer_sell():
        result = [
            make_stock(r,
                to_int(r[12]) + to_int(r[15]),
                to_int(r[13]) + to_int(r[16]),
                to_int(r[11]))
            for r in data["data"] if to_int(r[11]) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:10]

    return (
        foreign_buy(), trust_buy(), dealer_buy(),
        foreign_sell(), trust_sell(), dealer_sell()
    )


# ═══════════════════════════════════════════════
# ★ 優化：合併 fetch_stock_day + fetch_stock_quote
#   回傳 (avg_price, close, volume_lots, change_pct_str)
#   change_pct_str: "+3.52%" / "-1.20%" / "N/A"
# ═══════════════════════════════════════════════

def fetch_stock_day_full(code, date_str):
    """
    單一函式取得個股當日完整資料：
    - avg_price    : 當日均價（成交金額÷成交股數）
    - close        : 收盤價
    - volume_lots  : 成交量（張）
    - change_pct   : 漲跌幅字串，如 "+3.52%" / "N/A"
    """
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={date_str}&stockNo={code}&response=json")
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
            if row[0].strip() != target:
                continue
            shares = float(str(row[1]).replace(",", ""))
            amount = float(str(row[2]).replace(",", ""))
            close  = float(str(row[6]).replace(",", ""))
            volume_lots = int(shares / 1000)
            avg    = round(amount / shares, 2) if shares > 0 else 0.0
            if idx > 0:
                prev = float(str(rows[idx-1][6]).replace(",", ""))
                pct  = (close - prev) / prev * 100 if prev > 0 else 0.0
                sign = "+" if pct >= 0 else ""
                change_pct = f"{sign}{pct:.2f}%"
            else:
                change_pct = "N/A"
            return avg, close, volume_lots, change_pct
    except Exception:
        pass
    return 0.0, 0.0, 0, "N/A"


# ═══════════════════════════════════════════════
# 批次抓均價、收盤、成交量（寫入 stock dict）
# ═══════════════════════════════════════════════

def enrich_with_prices(groups, date_str):
    """批次抓均價、收盤價、成交量，去重複"""
    all_codes = {}
    for group in groups:
        for stock in group:
            all_codes[stock["code"]] = (0.0, 0.0, 0, "N/A")

    total = len(all_codes)
    print(f"  抓取 {total} 支股票價格（每支間隔 0.5 秒）...")
    for i, code in enumerate(all_codes):
        avg, close, vol, chg = fetch_stock_day_full(code, date_str)
        all_codes[code] = (avg, close, vol, chg)
        print(f"  [{i+1}/{total}] {code}: 均價={avg:.2f} 收盤={close:.2f} 成交={vol:,}張 漲跌={chg}")
        if i < total - 1:
            time.sleep(0.5)

    for group in groups:
        for stock in group:
            avg, close, vol, chg = all_codes.get(stock["code"], (0.0, 0.0, 0, "N/A"))
            stock["avg_price"]  = avg
            stock["close"]      = close
            stock["volume"]     = vol
            stock["change_pct"] = chg

    # 回傳快取供族群聯動直接使用（含漲跌幅）
    return all_codes


# ═══════════════════════════════════════════════
# 批次抓融資融券（一次全市場）
# ═══════════════════════════════════════════════

def enrich_with_margin(groups, date_str):
    """批次抓融資融券資料（MI_MARGN 一次抓全市場）"""
    all_codes = {stock["code"] for group in groups for stock in group}
    total = len(all_codes)

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
            stock["margin_balance"] = mb
            stock["margin_change"]  = mc
            stock["short_balance"]  = sb

    found = sum(1 for c in all_codes if c in margin_map)
    print(f"  ✅ 融資融券 找到 {found}/{total} 支")
    return margin_map


# ═══════════════════════════════════════════════
# 分析工具
# ═══════════════════════════════════════════════

def weighted_avg(entries):
    """純買超加權均價（向下相容，不含賣超扣減）"""
    total_net  = sum(e[1] for e in entries if e[2] > 0)
    total_cost = sum(e[1] * e[2] for e in entries if e[2] > 0)
    return round(total_cost / total_net, 2) if total_net > 0 else 0.0

def calc_position_fifo(buy_entries, sell_entries):
    """
    以價格升序 FIFO 扣減賣超，計算剩餘持倉加權均價。
    - buy_entries : [(date, net張數, avg_price), ...]
    - sell_entries: [(date, net張數), ...]
    - 優先扣掉成本最低的買入批次（模擬實際出場行為）
    - avg_price = 0 的 entry：張數仍參與扣減，但不列入成本計算
    回傳 (remaining_lots, weighted_avg_price)
    """
    # 按價格升序建立 queue，price=0 排最後（先扣有價格的低價部位）
    queue = sorted(
        [[net, price] for _, net, price in buy_entries if net > 0],
        key=lambda x: (x[1] == 0, x[1])   # price=0 排最後
    )

    total_sell = sum(net for _, net in sell_entries)
    remaining_sell = total_sell
    while remaining_sell > 0 and queue:
        if queue[0][0] <= remaining_sell:
            remaining_sell -= queue[0][0]
            queue.pop(0)
        else:
            queue[0][0] -= remaining_sell
            remaining_sell = 0

    # 剩餘持倉（排除 price=0 的部位只算張數，不算入成本）
    remaining_lots = sum(q[0] for q in queue)
    priced         = [(q[0], q[1]) for q in queue if q[1] > 0]
    total_cost     = sum(lots * price for lots, price in priced)
    total_priced   = sum(lots for lots, _ in priced)
    w_avg = round(total_cost / total_priced, 2) if total_priced > 0 else 0.0
    return remaining_lots, w_avg

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

def calc_chip_concentration(total_net_lots, volume_lots):
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

def calc_trust_label(t_consec):
    if t_consec >= TRUST_FIRE:
        return f"🔥 連續{t_consec}天"
    elif t_consec >= TRUST_STAR:
        return f"⭐ 連續{t_consec}天"
    elif t_consec > 0:
        return f"{t_consec}天"
    return ""

def calc_margin_health(margin_change, total_net_lots):
    if margin_change == 0 and total_net_lots == 0:
        return ""
    if total_net_lots > 0 and margin_change <= 0:
        return "✅ 籌碼乾淨"
    elif total_net_lots > 0 and 0 < margin_change < MARGIN_WARN:
        return "🟡 小幅跟進"
    elif total_net_lots > 0 and margin_change >= MARGIN_WARN:
        return "⚠️ 散戶大量跟進"
    elif total_net_lots <= 0 and margin_change > 0:
        return "🔴 法人不買散戶買"
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
        sep       = [["─" * 20, f"以上為 {disp}"] + [""] * (sep_cols - 2)]
        full_data = new_block + sep + existing
    else:
        full_data = new_block

    ws.clear()
    if ws.row_count < len(full_data) + 10:
        ws.add_rows(len(full_data) + 10 - ws.row_count)
    ws.update(range_name="A1", values=full_data)


# ═══════════════════════════════════════════════
# 更新各工作表
# ═══════════════════════════════════════════════

def update_buy_sheet(ss, date_str, foreign, trust, dealer):
    ws   = get_or_create(ss, "今日買超排行", 10)
    disp = fmt_date(date_str)
    hdrs = ["名次","代號","股票名稱","買進(張)","賣出(張)","買超(張)",
            "當日均價(元)","收盤價(元)","成交量(張)","籌碼集中度"]
    block = [[f"資料日期：{disp}"] + [""]*9]
    for label, data in [("外資及陸資買超前十名", foreign),
                        ("投信買超前十名",       trust),
                        ("自營商買超前十名",     dealer)]:
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
                        ("投信賣超前十名",       t_sell),
                        ("自營商賣超前十名",     d_sell)]:
        block += [[f"【{label}】"]+[""]*7, hdrs]
        for i, r in enumerate(data):
            block.append([i+1, r["code"], r["name"], r["buy"], r["sell"],
                          abs(r["net"]), r["avg_price"] or "", r.get("close") or ""])
        block += [[""]*8]
    prepend_block(ws, block, disp, "資料日期：", 8)
    print("  ✅ 今日賣超排行 更新完成")


def append_history(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell):
    ws   = get_or_create(ss, "歷史紀錄", 10)
    disp = fmt_date(date_str)
    if not ws.get_all_values():
        ws.append_row(["日期","名次","代號","股票名稱","法人類別","張數",
                       "當日均價(元)","買/賣","成交量(張)","籌碼集中度"])
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
                             r["net"], r["avg_price"] or "", "買超", vol or "", chip_str])
    for label, data in [("外資",f_sell),("投信",t_sell),("自營商",d_sell)]:
        for i, r in enumerate(data):
            new_rows.append([disp, i+1, r["code"], r["name"], label,
                             abs(r["net"]), r["avg_price"] or "", "賣超", "", ""])
    ws.append_rows(new_rows)
    print(f"  ✅ 歷史紀錄 新增 {len(new_rows)} 筆")


# ═══════════════════════════════════════════════
# ★ 優化：update_analysis 拆分為三個函式
# ═══════════════════════════════════════════════

def _build_history_maps(rows):
    """
    從歷史紀錄工作表的原始列資料，建立：
    - buy_map    : {code: {code, name, f, t, d, last}}
    - sell_hist  : {code: [(date, net), ...]}
    - net_by_date: {code: {date: 合計淨張數}}
    - all_dates  : sorted list of all trade dates
    """
    buy_map     = {}
    sell_hist   = {}
    net_by_date = {}

    for row in rows:
        if len(row) < 7 or not row[2]:
            continue
        date, code, name, type_, net_s, price_s = row[0], row[2], row[3], row[4], row[5], row[6]
        buy_sell = row[7] if len(row) > 7 else "買超"
        try:   net   = int(str(net_s).replace(",",""))
        except: net  = 0
        try:   price = float(price_s) if price_s else 0.0
        except: price = 0.0

        k = code or name
        sign = 1 if buy_sell == "買超" else -1
        net_by_date.setdefault(k, {})
        net_by_date[k][date] = net_by_date[k].get(date, 0) + sign * net

        if buy_sell == "買超":
            if k not in buy_map:
                buy_map[k] = {"code": code, "name": name, "f": [], "t": [], "d": [], "last": ""}
            entry = (date, net, price)
            if type_ == "外資":   buy_map[k]["f"].append(entry)
            if type_ == "投信":   buy_map[k]["t"].append(entry)
            if type_ == "自營商": buy_map[k]["d"].append(entry)
            if date > buy_map[k]["last"]:
                buy_map[k]["last"] = date
        else:
            # 各法人分開記錄賣超，方便 FIFO 各自扣減
            if k not in sell_hist:
                sell_hist[k] = {"f": [], "t": [], "d": []}
            entry_s = (date, net)
            if type_ == "外資":   sell_hist[k]["f"].append(entry_s)
            if type_ == "投信":   sell_hist[k]["t"].append(entry_s)
            if type_ == "自營商": sell_hist[k]["d"].append(entry_s)

    all_dates = sorted(set(row[0] for row in rows if row and row[0]))
    return buy_map, sell_hist, net_by_date, all_dates


def _consecutive_days(entries, code, all_dates, net_by_date):
    """
    v9 邏輯：以三法人合計淨買超 > 0 判斷連續天數。
    任一日合計淨 <= 0 即中斷歸零。
    """
    if not entries:
        return 0
    last_buy_date = max(e[0] for e in entries)
    if last_buy_date not in all_dates:
        return 1
    start_idx  = all_dates.index(last_buy_date)
    daily_net  = net_by_date.get(code, {})
    count      = 0
    for i in range(start_idx, -1, -1):
        d       = all_dates[i]
        day_net = daily_net.get(d, 0)
        if day_net > 0:
            count += 1
        else:
            break
    return count


def build_row(s, current_prices, current_margin, sell_hist, disp, all_dates, net_by_date):
    """
    ★ 優化：從 inner function 獨立為模組層函式。
    建立對照分析 / 每日快照 的單列資料。
    """
    code = s["code"]

    f_total  = unique_days(s["f"])
    t_total  = unique_days(s["t"])
    d_total  = unique_days(s["d"])
    f_consec = _consecutive_days(s["f"], code, all_dates, net_by_date)
    t_consec = _consecutive_days(s["t"], code, all_dates, net_by_date)
    d_consec = _consecutive_days(s["d"], code, all_dates, net_by_date)

    # 各法人分別取自己的賣超紀錄
    sells     = sell_hist.get(code, {"f": [], "t": [], "d": []})
    f_sells   = sells.get("f", [])
    t_sells   = sells.get("t", [])
    d_sells   = sells.get("d", [])
    all_sells = f_sells + t_sells + d_sells

    # FIFO 扣減：各法人各自優先出清低價部位
    f_remaining, f_wavg = calc_position_fifo(s["f"], f_sells)
    t_remaining, t_wavg = calc_position_fifo(s["t"], t_sells)
    d_remaining, d_wavg = calc_position_fifo(s["d"], d_sells)

    f_net   = f_remaining
    t_net   = t_remaining
    d_net   = d_remaining
    f_trend = calc_trend(s["f"])
    t_trend = calc_trend(s["t"])
    d_trend = calc_trend(s["d"])

    _, close, volume, _ = current_prices.get(code, (0.0, 0.0, 0, "N/A"))
    all_entries = s["f"] + s["t"] + s["d"]
    all_remaining, all_wavg = calc_position_fifo(all_entries, all_sells)
    pct_str, risk = calc_risk(close, all_wavg)
    signal        = calc_signal(code, all_entries, sell_hist, disp)
    trust_label   = calc_trust_label(t_consec)

    today_net           = sum(e[1] for e in all_entries if e[0] == disp)
    chip_pct, chip_lbl  = calc_chip_concentration(today_net, volume)
    mb, mc, sb          = current_margin.get(code, (0, 0, 0))
    margin_health       = calc_margin_health(mc, today_net)

    return [
        code, s["name"],
        f_total, f_consec, f_net, f_wavg or "", f_trend,
        t_total, t_consec, t_net, t_wavg or "", t_trend, trust_label,
        d_total, d_consec, d_net, d_wavg or "", d_trend,
        f_total + t_total + d_total,
        close or "", pct_str, risk, signal,
        chip_pct, chip_lbl,
        mb or "", mc if mb else "", sb or "", margin_health,
        s["last"]
    ]


ANALYSIS_HEADERS = [
    "代號","股票名稱",
    "外資累計天數","外資連續天數","外資買超(張)","外資加權均價","外資趨勢",
    "投信累計天數","投信連續天數","投信買超(張)","投信加權均價","投信趨勢","投信標記",
    "自營商累計天數","自營商連續天數","自營商買超(張)","自營商加權均價","自營商趨勢",
    "合計累計天數","現價","漲幅%","出貨風險","訊號",
    "籌碼集中度%","籌碼集中度評級",
    "融資餘額(張)","融資增減(張)","融券餘額(張)","融資健康度",
    "最近出現日"
]


def update_analysis(ss, date_str, current_prices, current_margin):
    """
    更新「對照分析」與「每日快照」工作表。
    current_prices : {code: (avg, close, volume, change_pct)}
    current_margin : {code: (margin_balance, margin_change, short_balance)}
    """
    hist = get_or_create(ss, "歷史紀錄")
    disp = fmt_date(date_str)
    rows = hist.get_all_values()[1:]

    buy_map, sell_hist, net_by_date, all_dates = _build_history_maps(rows)

    sorted_stocks = sorted(
        buy_map.values(),
        key=lambda x: x["last"],
        reverse=True
    )

    n_cols = len(ANALYSIS_HEADERS)

    # ── 對照分析（每次覆蓋）──
    ws_ana   = get_or_create(ss, "對照分析", n_cols)
    ana_data = [
        [f"統計截至：{disp}（每次執行自動更新至最新）"] + [""] * (n_cols - 1),
        ANALYSIS_HEADERS
    ]
    for s in sorted_stocks:
        ana_data.append(build_row(s, current_prices, current_margin, sell_hist, disp, all_dates, net_by_date))
    ws_ana.clear()
    if ws_ana.row_count < len(ana_data) + 5:
        ws_ana.add_rows(len(ana_data) + 5 - ws_ana.row_count)
    ws_ana.update(range_name="A1", values=ana_data)
    print(f"  ✅ 對照分析 更新完成（{len(ana_data)-2} 支，完整累積統計）")

    # ── 每日快照（prepend 累積）──
    ws_snap    = get_or_create(ss, "每日快照", n_cols)
    snap_block = [
        [f"統計截至：{disp}"] + [""] * (n_cols - 1),
        ANALYSIS_HEADERS
    ]
    for s in sorted_stocks:
        snap_block.append(build_row(s, current_prices, current_margin, sell_hist, disp, all_dates, net_by_date))
    prepend_block(ws_snap, snap_block, disp, "統計截至：", n_cols)
    print(f"  ✅ 每日快照 更新完成（最新快照插入最上方）")


# ═══════════════════════════════════════════════
# ★ v8 族群聯動工作表
# ═══════════════════════════════════════════════


def fetch_industry_map():
    """
    從 TWSE 抓所有上市股票的官方產業別。
    回傳 {代號: 產業別名稱}，失敗時回傳空 dict。
    """
    url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
    result = subprocess.run([
        "curl", "-s", "--max-time", "20",
        "-H", "User-Agent: Mozilla/5.0",
        "-H", "Accept-Charset: Big5",
        url
    ], capture_output=True)
    try:
        # TWSE 這頁是 Big5 編碼
        html = result.stdout.decode("big5", errors="replace")
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
        industry_map = {}
        current_industry = ""
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if not cells:
                continue
            # 產業別標題列：只有一格且不含空格（無代號）
            if len(cells) == 1 and cells[0]:
                current_industry = cells[0]
                continue
            # 股票列：第一格是「代號　名稱」（全形空格分隔）
            if cells and "　" in cells[0]:
                parts = cells[0].split("　")
                code  = parts[0].strip()
                if code.isdigit() and len(code) == 4:
                    industry_map[code] = current_industry
        print(f"  ✅ 官方產業別 載入 {len(industry_map)} 支")
        return industry_map
    except Exception as e:
        print(f"  ⚠️ 官方產業別載入失敗：{e}")
        return {}


def auto_update_sector_map(new_codes, all_buy_names, industry_map):
    """
    把買超榜上不在 SECTOR_MAP 的股票，依官方產業別自動補入 config.py。
    回傳補入的 {代號: 族群名} dict。
    """
    if not new_codes:
        return {}

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    if not os.path.exists(config_path):
        print("  ⚠️ 找不到 config.py，無法自動補入族群")
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        config_text = f.read()

    added = {}
    for code in sorted(new_codes):
        name   = all_buy_names.get(code, code)
        sector = industry_map.get(code, "其他")
        SECTOR_MAP.setdefault(sector, [])
        if code not in SECTOR_MAP[sector]:
            SECTOR_MAP[sector].append(code)
            added[code] = sector

    if not added:
        return {}

    for code, sector in added.items():
        name = all_buy_names.get(code, code)
        # 若族群已存在，插入到該列表末尾
        def replacer_existing(m):
            inner = m.group(2).rstrip()
            sep   = ",\n        " if inner.strip() else "\n        "
            return m.group(1) + inner + sep + '"' + code + '",  # ' + name + "\n    " + m.group(3)
        pattern_existing = r'("' + re.escape(sector) + r'"\s*:\s*\[)([^\]]*?)(\])'
        new_text, n = re.subn(pattern_existing, replacer_existing, config_text, flags=re.DOTALL)
        if n:
            config_text = new_text
        else:
            # 族群不存在，在 SECTOR_MAP 最後一個 } 前插入
            new_entry = '\n    "' + sector + '": [\n        "' + code + '",  # ' + name + '\n    ],'
            pattern_map = r"(SECTOR_MAP\s*=\s*\{)(.*?)(\n\})"
            def replacer_map(m):
                return m.group(1) + m.group(2) + new_entry + m.group(3)
            config_text = re.sub(pattern_map, replacer_map, config_text, flags=re.DOTALL)

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config_text)

    print(f"  ✅ 自動補入 SECTOR_MAP：{added}")
    return added

def _build_sector_triggered(all_buy_codes):
    """
    從買超榜出發查 CODE_TO_SECTOR 反查表，
    找出觸發的族群 {族群名: [觸發代號, ...]}。
    比原本從 SECTOR_MAP 遍歷更快，且新補入的代號即時生效。
    """
    triggered = {}
    for code in all_buy_codes:
        sector = CODE_TO_SECTOR.get(code)
        if sector:
            triggered.setdefault(sector, []).append(code)
    return triggered


def update_sector_sheet(ss, date_str, all_buy_codes, all_buy_names, current_prices):
    """
    ★ v10 優化：族群成員行情優先從 current_prices 快取取得，
    只對快取中沒有的股票才呼叫 API，大幅減少重複請求。

    current_prices: {code: (avg, close, volume, change_pct)}
    """
    disp      = fmt_date(date_str)
    ws        = get_or_create(ss, "族群聯動", 8)
    # 找出買超榜中不在 SECTOR_MAP 的新股票
    unknown_codes = {c for c in all_buy_codes if c not in CODE_TO_SECTOR}
    if unknown_codes:
        print(f"  發現 {len(unknown_codes)} 支新股票不在族群表：{sorted(unknown_codes)}")
        print("  正在查詢官方產業別...")
        industry_map = fetch_industry_map()
        newly_added  = auto_update_sector_map(unknown_codes, all_buy_names, industry_map)
        if newly_added:
            # 重建反查表讓本次執行即時生效
            CODE_TO_SECTOR.update({c: s for c, s in newly_added.items()})
    else:
        print(f"  所有買超股票均已在族群表中")

    triggered = _build_sector_triggered(all_buy_codes)

    if not triggered:
        no_trigger_block = [
            [f"資料日期：{disp} ｜ 族群聯動分析（有法人買超成員的族群）"] + [""]*7,
            ["今日買超榜無任何族群成員出現。"] + [""]*7,
            [""]*8,
        ]
        prepend_block(ws, no_trigger_block, disp, "資料日期：", 8)
        print("  ✅ 族群聯動 今日無觸發（已記錄）")
        return

    # 收集所有觸發族群成員
    all_member_codes = {c for sector in triggered for c in SECTOR_MAP.get(sector, [])}
    # ★ 只補抓快取中沒有的股票
    missing = [c for c in all_member_codes if c not in current_prices]

    member_extra = {}
    if missing:
        print(f"  補抓族群成員行情（{len(missing)} 支不在快取中）...")
        for i, code in enumerate(missing):
            avg, close, vol, chg = fetch_stock_day_full(code, date_str)
            member_extra[code] = (avg, close, vol, chg)
            print(f"  [{i+1}/{len(missing)}] {code}: 收盤={close:.2f} {chg}")
            if i < len(missing) - 1:
                time.sleep(0.4)

    def get_quote(code):
        """從快取或補抓結果取得 (close, change_pct, volume)"""
        if code in current_prices:
            _, close, vol, chg = current_prices[code]
        else:
            _, close, vol, chg = member_extra.get(code, (0.0, 0.0, 0, "N/A"))
        name = CODE_NAME_MAP.get(code) or all_buy_names.get(code, "")
        return name, close, chg, vol

    # 組裝今日區塊
    hdrs = ["代號","股票名稱","收盤價","漲跌幅%","成交量(張)","是否在買超榜","觸發族群","備註"]
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
            name, close, chg, vol = get_quote(code)
            in_buy = "✅ 買超榜" if code in hit_set else ""
            new_block.append([
                code,
                name,
                close if close else "",
                chg,
                vol if vol else "",
                in_buy,
                sector,
                ""
            ])
        new_block.append([""]*8)

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

# ═══════════════════════════════════════════════
# 待處理事項（有項目時啟動優先回報）
# ═══════════════════════════════════════════════
PENDING_ITEMS = [
    {
        "id": 1,
        "priority": "🔴 高",
        "title": "歷史紀錄舊資料單位錯誤",
        "desc": "v10.2 前寫入的買超/賣出欄位單位為「股」而非「張」，需除以 1000 修正，或清除歷史紀錄從今天重新累積。"
    },
    {
        "id": 2,
        "priority": "🟡 中",
        "title": "fetch_industry_map Big5 解析驗證",
        "desc": "官方產業別 API 使用 Big5 編碼，需在真實環境執行一次確認解析正確，自動補族群功能才可靠。"
    },
    {
        "id": 3,
        "priority": "🟡 中",
        "title": "族群熱度排行",
        "desc": "統計各族群近 N 天被買超的成員數與天數，輸出輪動熱度排行，協助判斷現在輪到哪個族群。"
    },
]


def check_pending():
    """啟動時若有待處理事項，優先回報並詢問是否繼續。"""
    if not PENDING_ITEMS:
        return
    print()
    print("╔" + "═" * 48 + "╗")
    print("║  ⚠️  有待處理事項，請確認                      ║")
    print("╠" + "═" * 48 + "╣")
    for item in PENDING_ITEMS:
        print(f"║  [{item['id']}] {item['priority']}  {item['title']}")
        # 說明超過 40 字換行
        desc = item["desc"]
        while desc:
            print(f"║      {desc[:42]}")
            desc = desc[42:]
    print("╚" + "═" * 48 + "╝")
    ans = input("\n是否繼續執行？(Y/n): ").strip().lower()
    if ans == "n":
        print("已中止，請先處理待處理事項。")
        sys.exit(0)


def main():
    global CODE_TO_SECTOR
    CODE_TO_SECTOR = _build_code_to_sector()

    print("=" * 50)
    print("  台灣股市三大法人買超/賣超追蹤 v10.2")
    print("  config.py 獨立設定 ｜ 族群聯動累積")
    print("=" * 50)
    print(f"  族群反查表：{len(CODE_TO_SECTOR)} 支股票已對應族群")

    # 優先回報待處理事項
    check_pending()

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"\n❌ 找不到 credentials.json")
        input("按 Enter 關閉..."); sys.exit(1)

    # Step 1: 抓法人資料
    print("\n📡 Step 1/5 抓取三大法人資料...")
    try:
        date_str, foreign, trust, dealer, f_sell, t_sell, d_sell = find_trading_day()
        disp = fmt_date(date_str)
        print(f"\n  ✅ 資料日期：{disp}")
        print(f"  外資  買超 {len(foreign)} 筆 / 賣超 {len(f_sell)} 筆")
        print(f"  投信  買超 {len(trust)}   筆 / 賣超 {len(t_sell)} 筆")
        print(f"  自營商買超 {len(dealer)}  筆 / 賣超 {len(d_sell)} 筆")
        if foreign: print(f"  外資買超第1：{foreign[0]['name']} ({foreign[0]['net']:,} 張)")
        if f_sell:  print(f"  外資賣超第1：{f_sell[0]['name']} ({abs(f_sell[0]['net']):,} 張)")
        if trust:   print(f"  投信買超第1：{trust[0]['name']} ({trust[0]['net']:,} 張)")
        if dealer:  print(f"  自營商買超第1：{dealer[0]['name']} ({dealer[0]['net']:,} 張)")
    except Exception as e:
        print(f"\n❌ 抓取失敗：{e}")
        input("按 Enter 關閉..."); sys.exit(1)

    # Step 2: 抓個股價格（含成交量、漲跌幅）
    print(f"\n💹 Step 2/5 抓取個股價格與成交量...")
    all_groups = [foreign, trust, dealer, f_sell, t_sell, d_sell]
    try:
        price_cache = enrich_with_prices(all_groups, date_str)
    except Exception as e:
        print(f"  ⚠️ 價格抓取部分失敗：{e}")
        price_cache = {}

    # Step 3: 抓融資融券
    print(f"\n📋 Step 3/5 抓取融資融券資料...")
    buy_groups = [foreign, trust, dealer]
    try:
        enrich_with_margin(buy_groups, date_str)
    except Exception as e:
        print(f"  ⚠️ 融資融券抓取部分失敗：{e}")

    # 建立快取
    current_prices = {}
    current_margin = {}
    for group in all_groups:
        for stock in group:
            current_prices[stock["code"]] = (
                stock.get("avg_price",  0.0),
                stock.get("close",      0.0),
                stock.get("volume",     0),
                stock.get("change_pct", "N/A"),
            )
    for group in buy_groups:
        for stock in group:
            current_margin[stock["code"]] = (
                stock.get("margin_balance", 0),
                stock.get("margin_change",  0),
                stock.get("short_balance",  0),
            )

    # 今日買超榜代號集合（供族群聯動使用）
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

    # Step 5: 族群聯動
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
