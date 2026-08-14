"""
台灣股市三大法人買超推薦回測腳本 — backtest.py
版本：v1.5

用途：
  對歷史推薦重建評分，比對 T+1/T+2/T+3 實際漲跌，
  輸出「回測明細」與「回測勝率矩陣」兩張工作表到 Google Sheets。

執行方式：
  python3 backtest.py              # 跑全部可用歷史
  python3 backtest.py --days 30    # 只跑最近 30 天
  python3 backtest.py --dry-run    # 只印結果，不寫 Sheets
  python3 backtest.py --single     # 單股回測模式（讀「回測設定」工作表）

單股回測模式（--single）：
  1. 讀取 Google Sheets「回測設定」工作表中的股票代號清單
  2. 從「歷史紀錄」撈每支股票所有法人買超訊號
  3. 進場：法人出現買超紀錄當日收盤
  4. 出場：T+3 收盤 或 法人當日轉賣超，取先到者
  5. 結果輸出至「單股回測」工作表

回測設定工作表格式：
  A欄 = 代號（如 2330），B欄 = 備註（可空）
  第一列為標題列，從第二列開始填代號

架子狀態（v1.5）：
  ✅ 資料讀取（Sheets 歷史紀錄 + 推薦歷史）
  ✅ 評分特徵重建邏輯（連續天數、籌碼集中度、加速度）
  ✅ 輸出格式（明細 + 勝率矩陣）
  ✅ 單股回測模式（--single）★ v1.1 新增
  ✅ 大盤警訊等級切面、主榜排名切面、獨立代號數、標準差 ★ v1.2 新增
  ✅ 回測明細改依推薦日降序排列（新資料在最上面）★ v1.2 新增
  ✅ 「推薦收盤買 vs 建議買進低點買」勝率比較切面 ★ v1.3 新增（對應 fetch_and_update.py v11.42）
     └ v11.42 之前累積的推薦成效資料沒有建議買進價位欄，該切面樣本數會偏低，
       需累積 v11.42 上線後的新資料（建議2~3週）才有參考價值，見備忘錄 T38
  ✅ 連續天數／籌碼集中度%／籌碼集中度評級改優先讀真值 ★ v1.4 新增（對應 fetch_and_update.py v11.44）
     └ v11.44 之前累積的舊資料沒有這幾欄，自動 fallback 用舊版重建邏輯（明細欄「來源」標示真值/重建近似值）
  ✅ 振幅%、自營商標記(含📢利多/利空)兩個新切面 ★ v1.4 新增（同對應 v11.44，舊資料歸類「未知」）
  ✅ 量比、融資趨勢、融券趨勢三個新切面 ★ v1.5 新增（對應 fetch_and_update.py v11.45，T39）
     └ 融券趨勢過去誤以為需要另打 MI_MARGN API 才能重建，其實 calc_short_trend() 早就算好了，
       只是沒往下傳，v11.45 補上後直接讀真值即可；v11.45 之前累積的舊資料沒有這幾欄，歸類「未知」
  🚧 買超加速度仍為重建近似值（尚未接真值，fetch_and_update.py 未存這欄，見備忘錄待處理清單 T41）
  🚧 相對強弱%、集保大戶、5日線尚未接進回測（見備忘錄待處理清單 T40）
  ⚠️  樣本 < 20 筆時勝率標注「樣本不足」

注意：
  credentials.json 需放在同目錄下（與主程式共用）。
  資料來源為「推薦歷史」工作表（由主程式 _archive_performance 自動寫入）。
"""

import os, sys, json, re, argparse, statistics
from datetime import datetime, timedelta

# ── 載入設定 ──────────────────────────────────────────────────
try:
    import config as _cfg
    SPREADSHEET_ID   = _cfg.SPREADSHEET_ID
    _cf              = _cfg.CREDENTIALS_FILE
    CREDENTIALS_FILE = _cf if _cf else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "credentials.json"
    )
    SECTOR_MAP    = _cfg.SECTOR_MAP
    CODE_NAME_MAP = _cfg.CODE_NAME_MAP
    RISK_LOW      = _cfg.RISK_LOW
    RISK_MID      = _cfg.RISK_MID
    CHIP_HIGH     = _cfg.CHIP_HIGH
    CHIP_MID      = _cfg.CHIP_MID
    MARGIN_WARN   = _cfg.MARGIN_WARN
    print("✅ 已載入 config.py")
except ImportError:
    print("❌ 找不到 config.py，請確認 backtest.py 和 config.py 在同一目錄")
    sys.exit(1)

# ── 常數 ──────────────────────────────────────────────────────
MIN_SAMPLE             = 20        # 勝率統計最低樣本數（低於此數標注「樣本不足」）
BACKTEST_SHEET_DETAIL  = "回測明細"
BACKTEST_SHEET_SUMMARY = "回測勝率"
SINGLE_STOCK_SETTING   = "回測設定"   # ★ v1.1 單股回測設定工作表
SINGLE_STOCK_RESULT    = "單股回測"   # ★ v1.1 單股回測結果工作表

COOKIE_FILE = "/tmp/twse_cookie_bt.txt"

# ── 網路工具（單股回測用）─────────────────────────────────────
import subprocess

def curl_get(url):
    result = subprocess.run([
        "curl", "-s", "--max-time", "20",
        "-c", COOKIE_FILE, "-b", COOKIE_FILE,
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "-H", "Referer: https://www.twse.com.tw/zh/trading/foreign/t86.html",
        "-H", "Accept: application/json, text/plain, */*",
        url
    ], capture_output=True, text=True)
    return result.stdout.strip()

_price_cache = {}   # { (code, YYYYMMDD): close_price }

def fetch_close_price_single(code, date_str):
    """
    抓取個股指定日期收盤價（YYYYMMDD）。
    先查快取，再打 TWSE STOCK_DAY API。
    """
    key = (code, date_str)
    if key in _price_cache:
        return _price_cache[key]

    import json as _json
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={date_str}&stockNo={code}&response=json")
    try:
        text = curl_get(url)
        if not text or text.startswith("<"):
            _price_cache[key] = None
            return None
        data = _json.loads(text)
        if data.get("stat") != "OK" or not data.get("data"):
            _price_cache[key] = None
            return None
        year   = int(date_str[:4]) - 1911
        target = f"{year}/{date_str[4:6]}/{date_str[6:]}"
        for row in data["data"]:
            if row[0].strip() == target:
                close = float(str(row[6]).replace(",", ""))
                _price_cache[key] = close
                return close
    except Exception:
        pass
    _price_cache[key] = None
    return None

def _next_trading_days(date_str, n=3):
    """
    給定日期字串 YYYY/MM/DD，回傳後 n 個交易日（跳週末，未處理國定假日）的 YYYYMMDD list。
    """
    try:
        d = datetime.strptime(date_str, "%Y/%m/%d")
    except Exception:
        return []
    result, count = [], 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.strftime("%Y%m%d"))
            count += 1
    return result


# ── Google Sheets 連線 ─────────────────────────────────────────
def connect_sheets():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

def get_or_create(ss, name, cols=10):
    try:
        return ss.worksheet(name)
    except Exception:
        return ss.add_worksheet(title=name, rows=500, cols=cols)


# ── 「推薦成效／推薦歷史」欄位對照 ──────────────────────────────
# ★ v1.6 backtest.py 是獨立腳本（不 import fetch_and_update.py），
#   所以這裡自己保留一份欄名順序，必須跟 fetch_and_update.py 的
#   PERFORMANCE_HEADERS 手動保持一致（兩邊改動時記得互相對照）。
PERFORMANCE_HEADERS = [
    "推薦日", "代號", "股票名稱", "推薦評分",
    "推薦收盤", "T+1收盤", "T+2收盤", "T+3收盤", "T+4收盤", "T+5收盤",
    "組別", "出貨風險", "融資健康度",
    "建議買進價位", "建議買進低", "建議買進高",
    "連續天數", "籌碼集中度%", "籌碼集中度評級", "振幅%", "自營商標記",
    "量比", "融資趨勢", "融券趨勢",
]
PERF_IDX = {name: i for i, name in enumerate(PERFORMANCE_HEADERS)}


def _perf_cell(row, key, default=""):
    """安全讀取「推薦成效／推薦歷史」列的某一欄（依 PERF_IDX 對照，越界或空值回傳 default）。"""
    idx = PERF_IDX[key]
    if len(row) > idx and row[idx] not in (None, ""):
        return str(row[idx]).strip()
    return default


# ── 資料讀取 ───────────────────────────────────────────────────

def load_perf_history(ss):
    """
    從「推薦歷史」工作表讀取所有封存的推薦成效。
    回傳 list of dict：rec_date, code, name, score, base_close, t1, t2, t3
    """
    try:
        ws   = ss.worksheet("推薦歷史")
        rows = ws.get_all_values()
    except Exception as e:
        print(f"  ⚠️ 讀取推薦歷史失敗：{e}")
        return []

    if len(rows) < 2:
        print("  ℹ️ 推薦歷史尚無資料（需先累積推薦成效後才有）")
        return []

    def _f(v):
        s = str(v).strip().replace("▲","").replace("▼","").replace("－","")
        try: return float(s)
        except: return None

    def _fk(row, key):
        """數值欄：依 PERF_IDX 取值後轉 float（取不到回傳 None）。"""
        idx = PERF_IDX[key]
        return _f(row[idx]) if len(row) > idx else None

    result = []
    date_pat = re.compile(r"^\d{4}/\d{2}/\d{2}$")
    for row in rows[1:]:
        if not row or not date_pat.match(str(row[0]).strip()):
            continue
        result.append({
            "rec_date":     _perf_cell(row, "推薦日"),
            "code":         _perf_cell(row, "代號"),
            "name":         _perf_cell(row, "股票名稱"),
            "score":        _fk(row, "推薦評分"),
            "base_close":   _fk(row, "推薦收盤"),
            "t1":           _fk(row, "T+1收盤"),
            "t2":           _fk(row, "T+2收盤"),
            "t3":           _fk(row, "T+3收盤"),
            "t4":           _fk(row, "T+4收盤"),
            "t5":           _fk(row, "T+5收盤"),
            "group":        _perf_cell(row, "組別"),
            "risk":         _perf_cell(row, "出貨風險"),          # ★ v11.23
            "margin_health":_perf_cell(row, "融資健康度"),        # ★ v11.23
            # ★ v1.3 對應 fetch_and_update.py v11.42 新增的三欄（法人成本價／5日均線組成的買進參考）
            "buy_label":    _perf_cell(row, "建議買進價位"),
            "buy_low":      _fk(row, "建議買進低"),
            "buy_high":     _fk(row, "建議買進高"),
            # ★ v1.4 對應 fetch_and_update.py v11.44 新增的五欄：推薦當日的真值（不再由 backtest 自己重建近似值）
            # v11.44 之前封存的舊資料沒有這幾欄，會是空字串，_rebuild_features() 會 fallback 到舊的重建邏輯
            "consec_real":   _perf_cell(row, "連續天數"),
            "chip_pct_real": _perf_cell(row, "籌碼集中度%"),
            "chip_lbl_real": _perf_cell(row, "籌碼集中度評級"),
            "amp":           _perf_cell(row, "振幅%"),
            "dealer":        _perf_cell(row, "自營商標記"),
            # ★ v1.5 對應 fetch_and_update.py v11.45 新增的三欄：量比/融資趨勢/融券趨勢（T39）
            # v11.45 之前封存的舊資料沒有這幾欄，會是空字串
            "vol_ratio_real":    _perf_cell(row, "量比"),
            "margin_trend_real": _perf_cell(row, "融資趨勢"),
            "short_trend_real":  _perf_cell(row, "融券趨勢"),
        })
    print(f"  ✅ 推薦歷史讀取 {len(result)} 筆")
    return result


def load_hist_records(ss):
    """
    從「歷史紀錄」工作表讀取所有買超記錄。
    回傳 list of dict（對應歷史紀錄欄位結構）。
    """
    try:
        ws   = ss.worksheet("歷史紀錄")
        rows = ws.get_all_values()
    except Exception as e:
        print(f"  ⚠️ 讀取歷史紀錄失敗：{e}")
        return []

    result = []
    for row in rows[1:]:
        if not row or not row[0]: continue
        result.append({
            "date":      row[0].strip(),
            "code":      row[2].strip() if len(row) > 2 else "",
            "name":      row[3].strip() if len(row) > 3 else "",
            "inst_type": row[4].strip() if len(row) > 4 else "",
            "net":       row[5].strip() if len(row) > 5 else "",
            "avg_price": row[6].strip() if len(row) > 6 else "",
            "buy_sell":  row[7].strip() if len(row) > 7 else "買超",
            "volume":    row[8].strip() if len(row) > 8 else "",
        })
    print(f"  ✅ 歷史紀錄讀取 {len(result)} 筆")
    return result


def load_alert_history(ss):
    """
    ★ v1.2 從「警訊」工作表讀取每日大盤警戒等級，供回測依警訊等級切面分析。
    回傳 {日期(YYYY/MM/DD): level("red"/"yellow"/"green")}
    「警訊」是 v11.34 才新增的工作表，該日期之前的推薦查不到資料屬正常現象。
    """
    try:
        ws   = ss.worksheet("警訊")
        rows = ws.get_all_values()
    except Exception:
        print("  ℹ️ 尚無「警訊」工作表（v11.34 才新增），大盤警訊切面將全部標「未知」")
        return {}

    result = {}
    date_pat = re.compile(r"^\d{4}/\d{2}/\d{2}$")
    for row in rows[1:]:
        if not row or not date_pat.match(str(row[0]).strip()):
            continue
        result[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
    print(f"  ✅ 警訊 讀取 {len(result)} 筆")
    return result


def build_hist_map(hist_records):
    """
    將歷史紀錄轉為快速查詢結構：
    { code: { date(YYYY/MM/DD): {total_net, volume, f_net, t_net, d_net} } }
    """
    result = {}
    for r in hist_records:
        if r["buy_sell"] != "買超": continue
        code, date = r["code"], r["date"]
        try:   net    = int(str(r["net"]).replace(",", ""))
        except: net   = 0
        try:   volume = int(str(r["volume"]).replace(",", ""))
        except: volume = 0

        result.setdefault(code, {}).setdefault(date, {
            "total_net": 0, "volume": 0, "f_net": 0, "t_net": 0, "d_net": 0
        })
        result[code][date]["total_net"] += net
        if r["inst_type"] == "外資":   result[code][date]["f_net"]    += net
        if r["inst_type"] == "投信":   result[code][date]["t_net"]    += net
        if r["inst_type"] == "自營商": result[code][date]["d_net"]    += net
        if volume > 0:
            result[code][date]["volume"] = volume
    return result


# ── T+N 股價抓取（TODO 區）────────────────────────────────────

def fetch_close_price(code, date_str):
    """
    抓取指定股票在指定日期的收盤價。
    date_str 格式：YYYYMMDD

    TODO（資料累積後補上）：
      實作時複製主程式的 fetch_price_map_batch / fetch_stock_day_full 邏輯。
      建議加一個 module-level _price_cache = {} 避免重複打 API。
    """
    return None   # 回傳 None 表示待補


def fetch_tn_prices(code, base_date_disp):
    """
    抓取 T+1、T+2、T+3 收盤價（跳週末）。
    base_date_disp 格式：YYYY/MM/DD
    回傳 {1: price_or_None, 2: price_or_None, 3: price_or_None}
    """
    result = {}
    try:
        d = datetime.strptime(base_date_disp, "%Y/%m/%d")
    except Exception:
        return {1: None, 2: None, 3: None}
    count = 0
    while count < 3:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
            result[count] = fetch_close_price(code, d.strftime("%Y%m%d"))   # TODO
    return result


# ── 特徵重建 ──────────────────────────────────────────────────

def _rebuild_features(rec, hist_map):
    """
    組裝推薦日當天的評分特徵。
    ★ v1.4：連續天數／籌碼集中度%／籌碼集中度評級／振幅%／自營商標記，
      優先直接採用 fetch_and_update.py v11.44 起存進「推薦歷史」的真值（推薦當時評分實際用的數字），
      不再由 backtest 自己從「歷史紀錄」重建近似值——舊版重建的「連續天數」用的是三大法人合計連續天數，
      跟評分實際用的 max(外資/投信/自營商連續天數) 定義不同，兜不起來；籌碼集中度%同理是重算，有精度誤差風險。
      只有 v11.44 之前封存的舊資料（沒有這幾欄，會是空字串）才 fallback 用舊的重建邏輯，資料來源標記在
      "*_source" 欄位方便回測明細分辨這筆是真值還是重建值。
    「買超加速度」目前仍是重建近似值（fetch_and_update.py 尚未把這欄存進「推薦歷史」，見備忘錄待處理清單 T41）。
    量比/融資趨勢/融券趨勢已改讀 v11.45 存的真值（T39，見下方）。
    回傳 dict（特徵值），資料不足時用空字串填充（不回傳 None，確保明細完整）。
    """
    code     = rec["code"]
    rec_date = rec["rec_date"]   # YYYY/MM/DD
    entries  = hist_map.get(code, {})
    all_dates  = sorted(entries.keys())
    net_by_day = {d: entries[d].get("total_net", 0) for d in all_dates}

    # ── 連續天數：v11.44 真值優先，否則 fallback 重建（合計買超連續天數，僅為近似值）──
    consec_real = rec.get("consec_real", "")
    if str(consec_real).strip().isdigit():
        consec, consec_source = int(consec_real), "真值"
    else:
        consec = 0
        for d in reversed(all_dates):
            if d > rec_date: continue
            if net_by_day.get(d, 0) > 0: consec += 1
            else: break
        consec_source = "重建近似值"

    # ── 籌碼集中度：v11.44 真值優先，否則 fallback 重算（僅為近似值）──
    chip_pct_real = rec.get("chip_pct_real", "")
    chip_lbl_real = rec.get("chip_lbl_real", "")
    if chip_pct_real or chip_lbl_real:
        chip_pct_disp, chip_lbl, chip_source = chip_pct_real, chip_lbl_real, "真值"
    else:
        day_data  = entries.get(rec_date, {})
        total_net = day_data.get("total_net", 0)
        volume    = day_data.get("volume", 0)
        if volume > 0 and total_net > 0:
            chip_pct = total_net / volume
            if chip_pct >= CHIP_HIGH:  chip_lbl = "🔵 高度集中"
            elif chip_pct >= CHIP_MID: chip_lbl = "🟦 中度集中"
            else:                      chip_lbl = "⬜ 偏低"
            chip_pct_disp = round(chip_pct * 100, 1)
        else:
            chip_pct_disp, chip_lbl = "", ""
        chip_source = "重建近似值"

    # ── 加速度（仍為重建近似值，尚未接真值）──
    buy_dates = sorted(
        [d for d in all_dates if d <= rec_date and net_by_day.get(d, 0) > 0],
        reverse=True
    )
    if len(buy_dates) >= 2:
        recent   = net_by_day[buy_dates[0]]
        prev_avg = sum(net_by_day[d] for d in buy_dates[1:3]) / min(len(buy_dates)-1, 2)
        if prev_avg > 0:
            r = round(recent / prev_avg, 2)
            if r >= 1.5:   accel_lbl = "🚀 加速"
            elif r >= 1.2: accel_lbl = "📈 溫和加速"
            elif r >= 0.8: accel_lbl = "➡ 持平"
            else:          accel_lbl = "📉 減速"
        else:
            accel_lbl = ""
    else:
        accel_lbl = ""

    return {
        "code":          code,
        "name":          rec.get("name", ""),
        "rec_date":      rec_date,
        "rec_score":     rec.get("score", ""),
        "consec":        consec,
        "consec_source": consec_source,      # ★ v1.4
        "chip_pct":      chip_pct_disp,
        "chip_lbl":      chip_lbl,
        "chip_source":   chip_source,        # ★ v1.4
        "accel_lbl":     accel_lbl,
        "margin_health": rec.get("margin_health", ""),  # ★ v11.23 從推薦歷史直接取
        "risk":          rec.get("risk", ""),            # ★ v11.23 從推薦歷史直接取
        "amp":           rec.get("amp", ""),              # ★ v1.4 振幅%（真值，v11.44 前無資料）
        "dealer":        rec.get("dealer", ""),           # ★ v1.4 自營商標記，含📢利多/利空（真值，v11.44 前無資料）
        # ★ v1.5 量比/融資趨勢/融券趨勢，直接讀真值（v11.45 前無資料時為空字串，切面會歸類「未知」）
        "vol_ratio":     rec.get("vol_ratio_real", ""),
        "margin_trend":  rec.get("margin_trend_real", ""),
        "short_trend":   rec.get("short_trend_real", ""),
    }


# ── 損益計算 ──────────────────────────────────────────────────

def calc_pnl(base_close, tn_close):
    if base_close and tn_close and base_close > 0:
        return round((tn_close - base_close) / base_close * 100, 2)
    return None


# ── 勝率矩陣 ──────────────────────────────────────────────────

def calc_win_rate_matrix(detail_rows):
    """
    計算多個特徵切面的勝率矩陣。
    回傳 list of (section_title, rows) 供寫入工作表。
    """
    def _stats(rows, key_fn, t_key):
        groups = {}
        for r in rows:
            groups.setdefault(key_fn(r), []).append(r)
        result = []
        for k, grp in sorted(groups.items(), key=lambda x: -len(x[1])):
            pnls = [r[t_key] for r in grp if r.get(t_key) is not None]
            n    = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            avg  = round(sum(pnls) / n, 2) if n else None
            std  = round(statistics.pstdev(pnls), 2) if n >= 2 else None   # ★ v1.2 母體標準差
            rate = round(wins / n * 100, 1) if n else None
            n_codes = len({r.get("code") for r in grp if r.get(t_key) is not None and r.get("code")})   # ★ v1.2 去重後獨立代號數（與樣本數 n 同一母體）
            suffix = "" if n >= MIN_SAMPLE else f" ⚠️樣本不足({n})"
            result.append({
                "key":  k, "n": n, "wins": wins,
                "rate": f"{rate}%{suffix}" if rate is not None else "N/A",
                "avg":  avg, "std": std, "n_codes": n_codes,
            })
        return result

    sections = []

    # 切面 1：籌碼集中度
    sections.append(("【籌碼集中度 × T+1 勝率】",
        _stats(detail_rows, lambda r: r.get("chip_lbl") or "未知", "t1_pnl")))

    # 切面 2：連續天數分桶
    def _bucket(r):
        c = r.get("consec", 0)
        try: c = int(c)
        except: return "未知"
        if c <= 2:    return "1~2天"
        elif c <= 5:  return "3~5天"
        elif c <= 10: return "6~10天"
        else:         return "11天以上"
    sections.append(("【連續天數 × T+1 勝率】",
        _stats(detail_rows, _bucket, "t1_pnl")))

    # 切面 3：買超加速度
    sections.append(("【買超加速度 × T+1 勝率】",
        _stats(detail_rows, lambda r: r.get("accel_lbl") or "資料不足", "t1_pnl")))

    # 切面 4：推薦評分分桶
    def _score_bucket(r):
        s = r.get("rec_score")
        try: s = float(s)
        except: return "未知"
        if s >= 80:   return "80~100分"
        elif s >= 60: return "60~79分"
        elif s >= 40: return "40~59分"
        else:         return "40分以下"
    sections.append(("【推薦評分分桶 × T+1 勝率】",
        _stats(detail_rows, _score_bucket, "t1_pnl")))

    # 切面 5：出貨風險
    def _risk_lbl(r):
        v = str(r.get("risk", "")).strip()
        return v if v and v != "TODO" else "未知"
    sections.append(("【出貨風險 × T+1 勝率】",
        _stats(detail_rows, _risk_lbl, "t1_pnl")))

    # 切面 6：融資健康度
    def _margin_lbl(r):
        v = str(r.get("margin_health", "")).strip()
        return v if v and v != "TODO" else "未知"
    sections.append(("【融資健康度 × T+1 勝率】",
        _stats(detail_rows, _margin_lbl, "t1_pnl")))

    # 切面 7：主榜 vs 觀察組
    def _group_lbl(r):
        v = str(r.get("group", "")).strip()
        return v if v else "未知"
    sections.append(("【主榜 vs 觀察組 × T+1 勝率】",
        _stats(detail_rows, _group_lbl, "t1_pnl")))

    # 切面 8：推薦日星期幾
    _WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    def _weekday_lbl(r):
        try:
            return _WEEKDAY_ZH[datetime.strptime(r["rec_date"], "%Y/%m/%d").weekday()]
        except Exception:
            return "未知"
    sections.append(("【推薦日星期幾 × T+1 勝率】",
        _stats(detail_rows, _weekday_lbl, "t1_pnl")))

    # 切面 9：整體 T+1/T+2/T+3/T+4/T+5 勝率
    overall = []
    for t_key, label in [("t1_pnl","T+1"),("t2_pnl","T+2"),("t3_pnl","T+3"),
                          ("t4_pnl","T+4"),("t5_pnl","T+5")]:
        pnls = [r[t_key] for r in detail_rows if r.get(t_key) is not None]
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        avg  = round(sum(pnls)/n, 2) if n else None
        std  = round(statistics.pstdev(pnls), 2) if n >= 2 else None
        rate = round(wins/n*100, 1) if n else None
        n_codes = len({r.get("code") for r in detail_rows
                        if r.get("code") and r.get(t_key) is not None})
        suffix = "" if n >= MIN_SAMPLE else f" ⚠️樣本不足({n})"
        overall.append({
            "key":  label, "n": n, "wins": wins,
            "rate": f"{rate}%{suffix}" if rate is not None else "N/A",
            "avg":  avg, "std": std, "n_codes": n_codes,
        })
    sections.append(("【整體 T+1 / T+2 / T+3 / T+4 / T+5 勝率（總覽）】", overall))

    # 切面 10：★ v1.2 大盤警訊等級 × T+1 勝率
    # 驗證 fetch_and_update.py v11.35 的情境降權有沒有效：紅/黃警訊日 vs 正常日的推薦表現
    _ALERT_LABEL = {"red": "🔴 高風險", "yellow": "🟡 觀察", "green": "🟢 正常"}
    def _alert_lbl(r):
        lv = str(r.get("alert_level", "")).strip()
        return _ALERT_LABEL.get(lv, "未知（v11.34前無資料）")
    sections.append(("【大盤警訊等級 × T+1 勝率】",
        _stats(detail_rows, _alert_lbl, "t1_pnl")))

    # 切面 11：★ v1.2 主榜排名 Top1~5 × T+1 勝率（驗證評分排序能力，只看主榜）
    def _rank_lbl(r):
        rk = r.get("rank")
        return f"第{rk}名" if rk else "非主榜/無排名"
    main_board_rows = [r for r in detail_rows if r.get("rank")]
    if main_board_rows:
        sections.append(("【主榜排名 Top1~5 × T+1 勝率】",
            _stats(main_board_rows, _rank_lbl, "t1_pnl")))

    # 切面 12：★ v1.4 振幅% × T+1 勝率（v11.44 前無資料，歸類「未知」）
    def _amp_bucket(r):
        v = str(r.get("amp", "")).strip()
        if not v:
            return "未知"
        try:
            amp = float(v.replace("⚡", "").replace("%", ""))
        except ValueError:
            return "未知"
        if amp < 2:   return "<2%"
        elif amp < 5: return "2~5%"
        else:         return "≥5%⚡"
    sections.append(("【振幅% × T+1 勝率】",
        _stats(detail_rows, _amp_bucket, "t1_pnl")))

    # 切面 13：★ v1.4 自營商標記／重大訊息 × T+1 勝率（v11.44 前無資料，歸類「未知」）
    # 主要想看：📢利多/📢利空 標記出現時，勝率是否有明顯差異
    def _dealer_lbl(r):
        v = str(r.get("dealer", "")).strip()
        if not v:
            return "未知"
        if "📢利多" in v: return "📢 利多標記"
        if "📢利空" in v: return "📢 利空標記"
        return "無重大訊息標記"
    sections.append(("【自營商標記/重大訊息 × T+1 勝率】",
        _stats(detail_rows, _dealer_lbl, "t1_pnl")))

    # 切面 15：★ v1.5 量比 × T+1 勝率（T39，v11.45 前無資料，歸類「未知」）
    def _vol_ratio_bucket(r):
        v = str(r.get("vol_ratio", "")).strip()
        if not v:
            return "未知"
        try:
            vr = float(v)
        except ValueError:
            return "未知"
        if vr < 1:    return "<1倍（量縮）"
        elif vr < 2:  return "1~2倍"
        elif vr < 3:  return "2~3倍"
        else:         return "≥3倍（爆量）"
    sections.append(("【量比 × T+1 勝率】",
        _stats(detail_rows, _vol_ratio_bucket, "t1_pnl")))

    # 切面 16：★ v1.5 融資趨勢 × T+1 勝率（T39，v11.45 前無資料，歸類「未知」）
    def _margin_trend_lbl(r):
        v = str(r.get("margin_trend", "")).strip()
        return v if v else "未知"
    sections.append(("【融資趨勢 × T+1 勝率】",
        _stats(detail_rows, _margin_trend_lbl, "t1_pnl")))

    # 切面 17：★ v1.5 融券趨勢 × T+1 勝率（T39，v11.45 前無資料，歸類「未知」；
    #   backtest.py 舊版誤以為需要另打 MI_MARGN API 才能重建，其實 fetch_and_update.py
    #   的 calc_short_trend() 早就算好了，v11.45 只是把這個現成標籤存進「推薦歷史」）
    def _short_trend_lbl(r):
        v = str(r.get("short_trend", "")).strip()
        return v if v else "未知"
    sections.append(("【融券趨勢 × T+1 勝率】",
        _stats(detail_rows, _short_trend_lbl, "t1_pnl")))

    # 切面 18：★ v1.3 推薦收盤買 vs 建議買進低點買 × T+1~T+5 勝率
    # 直接回答「照建議買進價位買，勝率比照推薦收盤價買高多少%」
    # ★ 對應 fetch_and_update.py v11.42；v11.42 之前累積的推薦成效資料沒有這三欄，
    #   「建議買進低點買」那幾列的樣本數會明顯偏低（甚至 N/A），屬於資料還在累積中的正常現象，
    #   不是計算錯誤。建議累積至少 2~3 週新資料後再參考本切面的結論（見備忘錄 T38）。
    buy_vs_close = []
    for t_key_close, t_key_buy, label in [
        ("t1_pnl", "t1_pnl_buy", "T+1"),
        ("t2_pnl", "t2_pnl_buy", "T+2"),
        ("t3_pnl", "t3_pnl_buy", "T+3"),
        ("t4_pnl", "t4_pnl_buy", "T+4"),
        ("t5_pnl", "t5_pnl_buy", "T+5"),
    ]:
        for t_key, method in [(t_key_close, "推薦收盤買"), (t_key_buy, "建議買進低點買")]:
            pnls = [r[t_key] for r in detail_rows if r.get(t_key) is not None]
            n    = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            avg  = round(sum(pnls) / n, 2) if n else None
            std  = round(statistics.pstdev(pnls), 2) if n >= 2 else None
            rate = round(wins / n * 100, 1) if n else None
            n_codes = len({r.get("code") for r in detail_rows
                            if r.get("code") and r.get(t_key) is not None})
            suffix = "" if n >= MIN_SAMPLE else f" ⚠️樣本不足({n})"
            buy_vs_close.append({
                "key":  f"{label}（{method}）", "n": n, "wins": wins,
                "rate": f"{rate}%{suffix}" if rate is not None else "N/A",
                "avg":  avg, "std": std, "n_codes": n_codes,
            })
    sections.append(("【★推薦收盤買 vs 建議買進低點買 × T+1~T+5 勝率比較】", buy_vs_close))

    return sections


# ── 輸出到 Sheets ──────────────────────────────────────────────

DETAIL_HEADERS = [
    "推薦日", "推薦星期", "代號", "股票名稱", "推薦評分",
    "連續天數", "連續天數來源", "籌碼集中度%", "籌碼集中度評級", "籌碼集中度來源",   # ★ v1.4 加「來源」欄，方便分辨真值/重建近似值
    "買超加速度",
    "推薦收盤",
    "T+1收盤", "T+1漲跌%", "T+2收盤", "T+2漲跌%", "T+3收盤", "T+3漲跌%",
    "T+4收盤", "T+4漲跌%", "T+5收盤", "T+5漲跌%",
    "T+1勝負", "T+2勝負", "T+3勝負", "T+4勝負", "T+5勝負",
    "融資健康度", "出貨風險", "融券趨勢",
    "振幅%", "自營商標記",   # ★ v1.4
    "量比", "融資趨勢",   # ★ v1.5
    "組別", "主榜排名", "大盤警訊等級",   # ★ v1.2
    "建議買進價位", "建議買進低", "建議買進高",   # ★ v1.3
    "T+1漲跌%(建議買進)", "T+2漲跌%(建議買進)", "T+3漲跌%(建議買進)",
    "T+4漲跌%(建議買進)", "T+5漲跌%(建議買進)",   # ★ v1.3
]

def _win_label(pnl):
    if pnl is None: return "待補"
    return "✅ 勝" if pnl > 0 else ("➡ 平" if pnl == 0 else "❌ 負")

def write_detail_sheet(ss, detail_rows, dry_run=False):
    now  = datetime.now().strftime("%Y/%m/%d %H:%M")
    n    = len(DETAIL_HEADERS)
    # ★ v1.2 依推薦日降序排列（新資料在最上面），符合專案「沒特別說明一律降序」的預設
    detail_rows = sorted(detail_rows, key=lambda r: r.get("rec_date", ""), reverse=True)
    data = [
        [f"回測明細（產出時間：{now}，共 {len(detail_rows)} 筆）"] + [""]*(n-1),
        DETAIL_HEADERS,
    ]
    _WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    for r in detail_rows:
        rec_date = r.get("rec_date", "")
        try:
            weekday_zh = _WEEKDAY_ZH[datetime.strptime(rec_date, "%Y/%m/%d").weekday()]
        except Exception:
            weekday_zh = ""
        data.append([
            rec_date,               weekday_zh,
            r.get("code",""),       r.get("name",""),
            r.get("rec_score",""),  r.get("consec",""),  r.get("consec_source",""),
            r.get("chip_pct",""),   r.get("chip_lbl",""),  r.get("chip_source",""),
            r.get("accel_lbl",""),
            r.get("base_close",""),
            r.get("t1",""),         r.get("t1_pnl","待補"),
            r.get("t2",""),         r.get("t2_pnl","待補"),
            r.get("t3",""),         r.get("t3_pnl","待補"),
            r.get("t4",""),         r.get("t4_pnl","待補"),
            r.get("t5",""),         r.get("t5_pnl","待補"),
            _win_label(r.get("t1_pnl")),
            _win_label(r.get("t2_pnl")),
            _win_label(r.get("t3_pnl")),
            _win_label(r.get("t4_pnl")),
            _win_label(r.get("t5_pnl")),
            r.get("margin_health",""), r.get("risk",""), r.get("short_trend",""),
            r.get("amp",""), r.get("dealer",""),
            r.get("vol_ratio",""), r.get("margin_trend",""),
            r.get("group",""), r.get("rank","") or "", r.get("alert_level","") or "",
            r.get("buy_label",""), r.get("buy_low","") or "", r.get("buy_high","") or "",
            r.get("t1_pnl_buy","待補"), r.get("t2_pnl_buy","待補"), r.get("t3_pnl_buy","待補"),
            r.get("t4_pnl_buy","待補"), r.get("t5_pnl_buy","待補"),
        ])

    if dry_run:
        print(f"  [dry-run] 回測明細 {len(detail_rows)} 筆（前3筆預覽）：")
        for row in data[2:5]:
            print(f"    {row[:8]}")
        return

    ws = get_or_create(ss, BACKTEST_SHEET_DETAIL, n)
    ws.clear()
    if ws.row_count < len(data) + 5:
        ws.add_rows(len(data) + 5 - ws.row_count)
    ws.update(range_name="A1", values=data)
    print(f"  ✅ 回測明細 寫入 {len(detail_rows)} 筆")


def write_summary_sheet(ss, sections, dry_run=False):
    now  = datetime.now().strftime("%Y/%m/%d %H:%M")
    data = [
        [f"回測勝率矩陣（產出時間：{now}）"],
        ["切面", "分類", "樣本數", "獨立代號數", "勝出數", "勝率（T+1）", "平均漲跌幅(%)", "標準差(%)"],
    ]
    for title, rows in sections:
        data.append([title] + [""]*7)
        for r in rows:
            data.append(["", r["key"], r["n"], r.get("n_codes", ""), r["wins"], r["rate"],
                         r["avg"] if r["avg"] is not None else "N/A",
                         r.get("std") if r.get("std") is not None else "N/A"])
        data.append([""]*8)

    if dry_run:
        print(f"  [dry-run] 回測勝率矩陣（前15行預覽）：")
        for row in data[:15]:
            if any(row): print(f"    {row}")
        return

    ws = get_or_create(ss, BACKTEST_SHEET_SUMMARY, 8)
    ws.clear()
    if ws.row_count < len(data) + 5:
        ws.add_rows(len(data) + 5 - ws.row_count)
    ws.update(range_name="A1", values=data)
    print(f"  ✅ 回測勝率 寫入完成（{len(sections)} 個切面）")


# ── 主流程 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="台灣股市推薦回測腳本 v1.5")
    parser.add_argument("--days",    type=int, default=0,
                        help="只回測最近 N 天的推薦（0 = 全部）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印結果，不寫 Google Sheets")
    parser.add_argument("--single",  action="store_true",
                        help="★ v1.1 單股回測模式（讀「回測設定」工作表）")
    args = parser.parse_args()

    print("=" * 50)
    print("  台灣股市推薦回測腳本 v1.5")
    print("=" * 50)

    # ── dry-run 快速驗證 ──
    if args.dry_run and not args.single:
        print("\n[dry-run 模式：不連接 Sheets，使用假資料驗證框架]")
        _demo_dry_run()
        return

    # ── 連線 ──
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"❌ 找不到 credentials.json（路徑：{CREDENTIALS_FILE}）")
        sys.exit(1)
    print("\n🔌 連接 Google Sheets...")
    try:
        ss = connect_sheets()
        print("  ✅ 連接成功")
    except Exception as e:
        print(f"❌ 連接失敗：{e}")
        sys.exit(1)

    # ── 單股回測模式 ──
    if args.single:
        main_single(ss, dry_run=args.dry_run)
        print(f"\n🎉 完成！")
        print(f"  https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
        return

    # ── 一般推薦回測模式 ──
    print("\n📂 讀取資料...")
    perf_records = load_perf_history(ss)
    hist_records = load_hist_records(ss)
    alert_map    = load_alert_history(ss)   # ★ v1.2

    if not perf_records:
        print("\n⚠️ 推薦歷史無資料，架子驗證完成。")
        print("   等主程式跑幾週後，「推薦歷史」工作表有資料，再執行回測。")
        return

    # ── 日期過濾 ──
    if args.days > 0:
        cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y/%m/%d")
        before = len(perf_records)
        perf_records = [r for r in perf_records if r["rec_date"] >= cutoff]
        print(f"  過濾近 {args.days} 天：{before} → {len(perf_records)} 筆")

    # ── 建立歷史查詢 map ──
    hist_map = build_hist_map(hist_records)

    # ── 組裝明細 ──
    print(f"\n📊 組裝回測明細（{len(perf_records)} 筆）...")
    detail_rows = []
    for rec in perf_records:
        features = _rebuild_features(rec, hist_map)
        base     = rec.get("base_close")
        buy_low  = rec.get("buy_low")    # ★ v1.3
        buy_high = rec.get("buy_high")   # ★ v1.3
        row = {
            **features,
            "base_close": base,
            "t1": rec.get("t1"), "t1_pnl": calc_pnl(base, rec.get("t1")),
            "t2": rec.get("t2"), "t2_pnl": calc_pnl(base, rec.get("t2")),
            "t3": rec.get("t3"), "t3_pnl": calc_pnl(base, rec.get("t3")),
            "t4": rec.get("t4"), "t4_pnl": calc_pnl(base, rec.get("t4")),
            "t5": rec.get("t5"), "t5_pnl": calc_pnl(base, rec.get("t5")),
            "group": rec.get("group", ""),  # ★ v11.23
            "alert_level": alert_map.get(rec["rec_date"], ""),   # ★ v1.2
            # ★ v1.3 以「建議買進低點」為進場價的假設性損益（回答：照建議價位買，勝率高多少%）
            #   出場價維持用同一組 T+N 收盤，只換「進場價」這個變數，其餘條件不變才能公平比較
            "buy_label":    rec.get("buy_label", ""),
            "buy_low":      buy_low,
            "buy_high":     buy_high,
            "t1_pnl_buy":   calc_pnl(buy_low, rec.get("t1")),
            "t2_pnl_buy":   calc_pnl(buy_low, rec.get("t2")),
            "t3_pnl_buy":   calc_pnl(buy_low, rec.get("t3")),
            "t4_pnl_buy":   calc_pnl(buy_low, rec.get("t4")),
            "t5_pnl_buy":   calc_pnl(buy_low, rec.get("t5")),
        }
        detail_rows.append(row)

    # ── ★ v1.2 主榜排名：同一推薦日的主榜股票依推薦評分由高到低排 1~N ──
    from collections import defaultdict as _defaultdict
    by_date_board = _defaultdict(list)
    for r in detail_rows:
        if str(r.get("group", "")).strip() == "主榜":
            by_date_board[r["rec_date"]].append(r)
    for day_rows in by_date_board.values():
        day_rows.sort(key=lambda r: (r.get("rec_score") if isinstance(r.get("rec_score"), (int, float)) else -1),
                       reverse=True)
        for i, r in enumerate(day_rows, start=1):
            r["rank"] = i

    valid_t1 = sum(1 for r in detail_rows if r.get("t1_pnl") is not None)
    print(f"  T+1 有效樣本：{valid_t1}/{len(detail_rows)} 筆"
          + ("（T+N 待補，需實作 fetch_close_price）" if valid_t1 == 0 else ""))

    # ── 計算勝率矩陣 ──
    print("\n📈 計算勝率矩陣...")
    sections = calc_win_rate_matrix(detail_rows)

    # ── 輸出 ──
    print("\n💾 輸出結果...")
    write_detail_sheet(ss, detail_rows)
    write_summary_sheet(ss, sections)

    print(f"\n🎉 完成！")
    print(f"  https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


# ══════════════════════════════════════════════════════════════
# ★ v1.1 單股回測模式（--single）
# ══════════════════════════════════════════════════════════════

def load_single_stock_setting(ss):
    """
    讀取「回測設定」工作表，回傳 list of (code, remark)。
    格式：A欄=代號、B欄=備註（可空），第一列為標題。
    若工作表不存在則自動建立並填入範例。
    """
    try:
        ws = ss.worksheet(SINGLE_STOCK_SETTING)
        rows = ws.get_all_values()
        if len(rows) < 2:
            print(f"  ℹ️ 「{SINGLE_STOCK_SETTING}」工作表無資料，請填入股票代號後再執行")
            return []
        result = []
        for row in rows[1:]:
            code = row[0].strip() if row else ""
            remark = row[1].strip() if len(row) > 1 else ""
            if code and code.isdigit():
                result.append((code, remark))
        print(f"  ✅ 讀取回測設定：{len(result)} 支股票")
        return result
    except Exception:
        # 工作表不存在，自動建立
        print(f"  ℹ️ 找不到「{SINGLE_STOCK_SETTING}」工作表，自動建立...")
        ws = ss.add_worksheet(title=SINGLE_STOCK_SETTING, rows=100, cols=3)
        ws.update(range_name="A1", values=[
            ["代號", "備註（可空）"],
            ["2330", "台積電"],
            ["2317", "鴻海"],
        ])
        print(f"  ✅ 已建立「{SINGLE_STOCK_SETTING}」工作表，請填入要回測的股票代號後再執行")
        return []


def run_single_stock_backtest(ss, hist_records, codes_remarks):
    """
    單股回測主邏輯。
    進場：法人出現買超當日收盤
    出場：T+3 收盤 or 法人當日轉賣超（取先到者）
    回傳 list of dict（每個進出場訊號一筆）
    """
    hist_map = build_hist_map(hist_records)

    # 建立「賣超日期」查詢結構：{ code: set of dates }
    sell_dates = {}
    for r in hist_records:
        if r.get("buy_sell") == "賣超":
            sell_dates.setdefault(r["code"], set()).add(r["date"])

    # 讀取「歷史紀錄」工作表取得代號對應名稱
    name_map = {}
    for r in hist_records:
        if r["code"] not in name_map and r.get("name"):
            name_map[r["code"]] = r["name"]

    all_results = []
    for code, remark in codes_remarks:
        name = name_map.get(code, CODE_NAME_MAP.get(code, code))
        entries = hist_map.get(code, {})
        if not entries:
            print(f"  ⚠️ {code} {name}：歷史紀錄無買超資料，略過")
            continue

        signals = sorted(entries.keys())   # 買超日期清單，升序
        print(f"  🔍 {code} {name}：{len(signals)} 個買超訊號")

        for entry_date in signals:
            # ── 進場收盤價 ──
            entry_date_yyyymmdd = entry_date.replace("/", "")
            entry_close = fetch_close_price_single(code, entry_date_yyyymmdd)
            if not entry_close:
                continue

            # ── 往後找 T+1 T+2 T+3（跳週末）──
            future_days = _next_trading_days(entry_date, n=3)

            # ── 出場判斷：法人轉賣超或 T+3 ──
            exit_day_idx = None   # 0=T+1, 1=T+2, 2=T+3
            exit_reason  = "T+3"
            code_sell = sell_dates.get(code, set())
            for i, fday_yyyymmdd in enumerate(future_days):
                fday_disp = f"{fday_yyyymmdd[:4]}/{fday_yyyymmdd[4:6]}/{fday_yyyymmdd[6:]}"
                if fday_disp in code_sell:
                    exit_day_idx = i
                    exit_reason  = f"T+{i+1} 法人轉賣"
                    break
            if exit_day_idx is None:
                exit_day_idx = 2   # T+3

            exit_date_yyyymmdd = future_days[exit_day_idx]
            exit_close = fetch_close_price_single(code, exit_date_yyyymmdd)

            pnl = None
            if entry_close and exit_close and entry_close > 0:
                pnl = round((exit_close - entry_close) / entry_close * 100, 2)

            # ── 抓 T+1/T+2/T+3 收盤（供參考）──
            t_closes = {}
            for i, fday in enumerate(future_days):
                t_closes[f"t{i+1}"] = fetch_close_price_single(code, fday)

            # ── 當日法人資料 ──
            day_data   = entries[entry_date]
            total_net  = day_data.get("total_net", 0)
            volume     = day_data.get("volume", 0)
            chip_pct   = round(total_net / volume * 100, 1) if volume > 0 and total_net > 0 else None

            all_results.append({
                "code":        code,
                "name":        name,
                "remark":      remark,
                "entry_date":  entry_date,
                "entry_close": entry_close,
                "exit_date":   f"{exit_date_yyyymmdd[:4]}/{exit_date_yyyymmdd[4:6]}/{exit_date_yyyymmdd[6:]}",
                "exit_reason": exit_reason,
                "exit_close":  exit_close,
                "pnl":         pnl,
                "t1":          t_closes.get("t1"),
                "t2":          t_closes.get("t2"),
                "t3":          t_closes.get("t3"),
                "total_net":   total_net,
                "chip_pct":    chip_pct,
                "f_net":       day_data.get("f_net", 0),
                "t_net":       day_data.get("t_net", 0),
                "d_net":       day_data.get("d_net", 0),
            })

    return all_results


def _single_stock_summary(results):
    """
    依股票彙整勝率統計。
    回傳 list of dict（每支股票一列）。
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for r in results:
        groups[(r["code"], r["name"])].append(r)

    summary = []
    for (code, name), rows in sorted(groups.items()):
        pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        avg  = round(sum(pnls) / n, 2) if n else None
        rate = round(wins / n * 100, 1) if n else None
        best  = round(max(pnls), 2) if pnls else None
        worst = round(min(pnls), 2) if pnls else None
        summary.append({
            "code": code, "name": name,
            "signals": len(rows), "valid": n,
            "wins": wins, "rate": rate, "avg_pnl": avg,
            "best": best, "worst": worst,
        })
    return summary


def write_single_stock_sheet(ss, results, summary, dry_run=False):
    """
    輸出單股回測結果到「單股回測」工作表。
    上半部：彙整摘要；下半部：逐筆明細。
    """
    now = datetime.now().strftime("%Y/%m/%d %H:%M")

    SUMMARY_HEADERS = [
        "代號", "股票名稱", "訊號數", "有效樣本", "勝出次數",
        "勝率%", "平均損益%", "最佳%", "最差%",
    ]
    DETAIL_HDRS = [
        "代號", "股票名稱", "進場日", "進場收盤",
        "出場日", "出場原因", "出場收盤", "損益%",
        "T+1收盤", "T+2收盤", "T+3收盤",
        "法人合計(張)", "外資(張)", "投信(張)", "自營(張)", "籌碼集中度%",
    ]

    def _pnl_label(p):
        if p is None: return "N/A"
        return f"+{p}%" if p > 0 else f"{p}%"

    # 摘要區
    data = [
        [f"單股回測結果（{now}，共 {len(summary)} 支股票，{len(results)} 個訊號）"],
        [],
        ["═" * 30 + "  彙整摘要  " + "═" * 30],
        SUMMARY_HEADERS,
    ]
    for s in summary:
        rate_str = f"{s['rate']}%" if s['rate'] is not None else "N/A"
        if s['valid'] < MIN_SAMPLE:
            rate_str += f" ⚠️樣本不足({s['valid']})"
        data.append([
            s["code"], s["name"], s["signals"], s["valid"],
            s["wins"], rate_str,
            _pnl_label(s["avg_pnl"]),
            _pnl_label(s["best"]),
            _pnl_label(s["worst"]),
        ])

    data += [[], ["═" * 30 + "  逐筆明細  " + "═" * 30], DETAIL_HDRS]
    for r in results:
        data.append([
            r["code"], r["name"], r["entry_date"], r["entry_close"],
            r["exit_date"], r["exit_reason"], r["exit_close"],
            _pnl_label(r["pnl"]),
            r["t1"] or "", r["t2"] or "", r["t3"] or "",
            r["total_net"], r["f_net"], r["t_net"], r["d_net"],
            r["chip_pct"] if r["chip_pct"] is not None else "",
        ])

    if dry_run:
        print(f"  [dry-run] 單股回測：{len(summary)} 支股票，{len(results)} 個訊號")
        for s in summary:
            print(f"    {s['code']} {s['name']}: 勝率 {s['rate']}%（{s['valid']} 筆）")
        return

    ws = get_or_create(ss, SINGLE_STOCK_RESULT, max(len(DETAIL_HDRS), len(SUMMARY_HEADERS)))
    ws.clear()
    if ws.row_count < len(data) + 5:
        ws.add_rows(len(data) + 5 - ws.row_count)
    ws.update(range_name="A1", values=data)
    print(f"  ✅ 單股回測 寫入完成（{len(summary)} 支股票，{len(results)} 個訊號）")


def main_single(ss, dry_run=False):
    """單股回測主流程"""
    print("\n📋 讀取回測設定...")
    codes_remarks = load_single_stock_setting(ss)
    if not codes_remarks:
        return

    print("\n📂 讀取歷史紀錄...")
    hist_records = load_hist_records(ss)
    if not hist_records:
        print("  ⚠️ 歷史紀錄無資料")
        return

    print(f"\n🔍 開始回測（{len(codes_remarks)} 支股票）...")
    results = run_single_stock_backtest(ss, hist_records, codes_remarks)

    if not results:
        print("  ⚠️ 無有效回測訊號（歷史資料可能不足，或代號有誤）")
        return

    summary = _single_stock_summary(results)

    print(f"\n📊 彙整結果：")
    for s in summary:
        rate_str = f"{s['rate']}%" if s['rate'] is not None else "N/A"
        print(f"  {s['code']} {s['name']}：{s['valid']} 筆有效，勝率 {rate_str}，平均 {s['avg_pnl']}%")

    print("\n💾 輸出結果...")
    write_single_stock_sheet(ss, results, summary, dry_run=dry_run)


def _demo_dry_run():
    """dry-run：用假資料驗證完整框架"""
    fake = [
        {"rec_date":"2026/05/20","code":"2330","name":"台積電","rec_score":85,
         "consec":8,"consec_source":"真值","chip_pct":25.3,"chip_lbl":"🔵 高度集中","chip_source":"真值",
         "accel_lbl":"🚀 加速","amp":"4.2%","dealer":"⭐連續3天 📢利多",
         "vol_ratio":"2.4","margin_trend":"↗ 大增","short_trend":"回補 3天",
         "base_close":950.0,
         "t1":960.0,"t1_pnl":1.05, "t2":945.0,"t2_pnl":-0.53,
         "t3":970.0,"t3_pnl":2.11, "t4":975.0,"t4_pnl":2.63,
         "t5":980.0,"t5_pnl":3.16,
         "margin_health":"TODO","risk":"TODO"},
        {"rec_date":"2026/05/20","code":"6669","name":"緯穎","rec_score":72,
         "consec":4,"consec_source":"真值","chip_pct":15.1,"chip_lbl":"🟦 中度集中","chip_source":"真值",
         "accel_lbl":"📈 溫和加速","amp":"1.8%","dealer":"",
         "vol_ratio":"1.3","margin_trend":"➡ 持平","short_trend":"持平",
         "base_close":2500.0,
         "t1":2480.0,"t1_pnl":-0.80, "t2":2530.0,"t2_pnl":1.20,
         "t3":2550.0,"t3_pnl":2.00,  "t4":2540.0,"t4_pnl":1.60,
         "t5":2560.0,"t5_pnl":2.40,
         "margin_health":"TODO","risk":"TODO"},
        {"rec_date":"2026/05/21","code":"3037","name":"欣興","rec_score":65,
         "consec":3,"consec_source":"重建近似值","chip_pct":11.2,"chip_lbl":"🟦 中度集中","chip_source":"重建近似值",
         "accel_lbl":"➡ 持平","amp":"","dealer":"",
         "vol_ratio":"","margin_trend":"","short_trend":"",
         "base_close":180.0,
         "t1":None,"t1_pnl":None, "t2":None,"t2_pnl":None,
         "t3":None,"t3_pnl":None, "t4":None,"t4_pnl":None,
         "t5":None,"t5_pnl":None,
         "margin_health":"TODO","risk":"TODO"},
    ]
    sections = calc_win_rate_matrix(fake)
    write_detail_sheet(None, fake, dry_run=True)
    write_summary_sheet(None, sections, dry_run=True)
    print("\n✅ 框架驗證完成")
    print("   T+N 欄位標注「待補」為正常狀態，等資料累積後補實作 fetch_close_price。")


if __name__ == "__main__":
    main()
