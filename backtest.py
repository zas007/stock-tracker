"""
台灣股市三大法人買超推薦回測腳本 — backtest.py
版本：v1.9

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

架子狀態（v1.9）：
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
  ✅ 新舊評分公式(v1舊/v2新)對照比較 ★ v1.8 新增（T42，對應 2026/09/17 回測分析出的5項改進方案）
     └ 「回測勝率」新增【新舊評分公式比較｜舊公式(v1)分組】【…新公式(v2)分組】兩個切面，
       「回測明細」新增「重算分數(舊v1)/(新v2)」「分組(舊v1)/(新v2)」四欄；
       重算分數不含集保大戶/今日買超金額/當日動能/大型股補償（回測資料重建不出來，
       v1/v2 都不計入，見程式內「新舊評分公式比較」區塊開頭說明）；
       確認 v2 高分組勝率明顯優於 v1 高分組後，才考慮把 v2 邏輯搬回
       fetch_and_update.py score_stock() 正式上線（目前正式評分邏輯尚未變動）
  ✅ ★ v1.9 新增：上面 v1.8 的比較有選股偏誤（股票池是v1選出來的舊名單，看不到v2獨有的選股），
     fetch_and_update.py v11.52 起在正式選股當下就同步用v2公式對「v1候選池」重新排名，
     把「v2會選、但v1沒選進主榜/觀察組」的股票額外寫進「明日關注」的
     「🆕 v2評分限定候選」區塊追蹤，這裡新增讀取這份真值（「另一版本評分」「評分公式版本」
     兩欄，「回測明細」可查看），並新增【評分公式版本(正式環境真值) × T+1 勝率】切面
     ——這才是真正沒有選股偏誤的比較，但需要 v11.52 後新資料累積幾週才有足夠樣本
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
    # ★ v1.7 對應 fetch_and_update.py v11.47（K21）新增的佔股本比重門檻，
    #   用 getattr 給預設值，避免舊版 config.py（還沒補上這三行）直接噴錯
    SHARES_PCT_HIGH = getattr(_cfg, "SHARES_PCT_HIGH", 3.0)
    SHARES_PCT_MID  = getattr(_cfg, "SHARES_PCT_MID", 1.5)
    SHARES_PCT_LOW  = getattr(_cfg, "SHARES_PCT_LOW", 0.5)
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
    "佔股本比重%",   # ★ v1.7 對應 fetch_and_update.py v11.47（K21）
    "另一版本評分", "評分公式版本",   # ★ v1.9 對應 fetch_and_update.py v11.52（T42 新舊評分公式並行）
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
            # ★ v1.7 對應 fetch_and_update.py v11.47 新增的一欄：佔股本比重%（K21）
            # v11.47 之前封存的舊資料沒有這欄，會是空字串
            "shares_pct_real":   _perf_cell(row, "佔股本比重%"),
            # ★ v1.9 對應 fetch_and_update.py v11.52 新增的兩欄：新舊評分公式並行比較（T42）
            # v11.52 之前封存的舊資料沒有這兩欄，會是空字串（"評分公式版本"一律歸類「未知(v11.52前)」）
            "other_score_real":  _fk(row, "另一版本評分"),
            "formula_ver_real":  _perf_cell(row, "評分公式版本"),
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


# ── ★ v1.8 新舊評分公式比較（T42）───────────────────────────────
# 背景：2026/09/17 用「回測勝率」矩陣分析後，發現正式評分公式（fetch_and_update.py
# score_stock()）裡有幾個因子的評分方向跟回測結果對不起來（例如爆量給最高分，但回測
# 顯示爆量表現最差）。這裡先在 backtest.py 用同一批歷史資料分別套用「舊公式(v1，
# 目前正式上線的邏輯)」與「新公式(v2，本次改進方案)」重算一次分數，各自依分數切三等分
# （高/中/低分組）比較 T+1 勝率，用來驗證新公式的排序能力是否真的比舊公式好——
# 確認有效後才會把 v2 的邏輯搬回 fetch_and_update.py 正式上線，避免憑感覺改分數。
#
# ⚠️ 限制：這裡重算的分數「不等於」正式推薦當時的完整評分，因為以下因子在
# 「推薦歷史」工作表沒有存原始數值，回測階段補不回來，v1/v2 兩邊都不計入
# （兩邊一起拿掉，比較還是公平，只是絕對分數會跟 Sheets「推薦評分」欄位對不上，
# 這是預期中的落差，不是算錯）：
#   - 集保大戶（-5~+5分）
#   - 今日法人買超金額（0~8分）
#   - 當日漲跌%動能（-2~+5分）
#   - 連續天數矩陣的「大型股補償」（買超金額≥1億/3億時偏低分×1.5~2）
#
# 改動對照（對應 2026/09/17 分析出的 5 項改進方案）：
#   1. 量比：舊公式爆量(≥3倍)給最高分，回測顯示爆量表現最差 → 新公式改 1~2倍最高分，爆量降分
#   2. 融資趨勢：舊公式「大減」比「減」分高，回測顯示「大減」勝率反而較低 → 新公式縮小差距並反轉排序
#   3. 連續天數矩陣：回測顯示 3~5天是甜蜜點、6~10天勝率反而下滑（倒U型），
#      舊公式卻是單調遞增 → 新公式改倒U型
#   4. 買超加速度：回測顯示是樣本數夠大、訊號最強的因子之一，舊公式只佔0~3分 → 新公式加大到-2~+8分
#   5. 振幅%：舊公式沒有這個因子，回測顯示2~5%最好、≥5%最差 → 新公式新增，範圍-5~+5分
# ─────────────────────────────────────────────────────────────

def _bt_score_margin(health):
    """融資健康度評分（25分）— v1/v2 共用，非本次改進項目"""
    return {"✅ 籌碼乾淨": 25, "🟡 小幅跟進": 15,
            "⚠️ 散戶大量跟進": 5, "🔴 法人不買散戶買": 0}.get(str(health).strip(), 0)


def _bt_score_risk(risk):
    """出貨風險評分（15分）— v1/v2 共用，非本次改進項目"""
    return {"🟢 低": 15, "🟡 中": 7, "🔴 高": 0}.get(str(risk).strip(), 0)


def _bt_score_short_trend(short_trend):
    """融券趨勢評分（-8~+8分）— v1/v2 共用，非本次改進項目"""
    s = str(short_trend).strip()
    if not s:
        return 0
    m = re.search(r"連[增減](\d+)天", s)
    days = int(m.group(1)) if m else 0
    if "↘" in s: return 8 if days >= 3 else 4
    if "↗" in s: return -8 if days >= 2 else -4
    return 0


def _bt_score_shares_pct(pct):
    """佔股本比重評分（0~+8分）— v1/v2 共用，非本次改進項目"""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return 0
    if pct >= SHARES_PCT_HIGH: return 8
    elif pct >= SHARES_PCT_MID: return 5
    elif pct >= SHARES_PCT_LOW: return 2
    return 0


def _bt_score_matrix(consec, chip_lbl, version="v1"):
    """
    連續天數 × 籌碼集中度 矩陣評分。
    v1（舊公式，目前正式上線）：天數越長分越高（單調遞增）。
    v2（新公式，本次改進方案③）：改成倒U型，3~5天是甜蜜點，6~10天分數回落。
    不含大型股補償（回測重建不出今日買超金額，v1/v2 都略過，見上方限制說明）。
    """
    try:
        consec = int(consec)
    except (TypeError, ValueError):
        consec = 0
    if version == "v1":
        rows = {
            "🔵 高度集中": [(3, 26), (7, 33), (999, 40)],
            "🟦 中度集中": [(3, 15), (7, 21), (999, 27)],
        }.get(chip_lbl, [(3, 4), (7, 7), (999, 10)])
        for limit, score in rows:
            if consec <= limit:
                return score
        return rows[-1][1]
    # v2：倒U型
    if chip_lbl == "🔵 高度集中":
        if consec <= 2:    return 20
        elif consec <= 5:  return 40
        elif consec <= 10: return 24
        else:              return 33
    elif chip_lbl == "🟦 中度集中":
        if consec <= 2:    return 12
        elif consec <= 5:  return 27
        elif consec <= 10: return 16
        else:              return 22
    else:  # 偏低
        if consec <= 2:    return 3
        elif consec <= 5:  return 10
        elif consec <= 10: return 5
        else:              return 8


def _bt_score_volume_ratio(vr, version="v1"):
    """
    量比評分（改進方案①）。
    v1（舊公式）：爆量(≥3倍)給最高 7 分。
    v2（新公式）：1~2倍溫和放量給最高 7 分，爆量降到 1 分（回測顯示爆量表現最差）。
    """
    if vr is None or vr == "":
        return 3
    try:
        vr = float(vr)
    except (ValueError, TypeError):
        return 2
    if version == "v1":
        if vr >= 3.0: return 7
        if vr >= 2.0: return 5
        if vr >= 1.5: return 4
        if vr >= 1.0: return 2
        return 1
    if vr >= 3.0: return 1     # 爆量：回測表現最差，大幅降分
    if vr >= 2.0: return 4     # 明顯放量：普通
    if vr >= 1.0: return 7     # 1~2倍：回測表現最好，最高分
    if vr >= 0.5: return 4     # 溫和縮量
    return 2                    # 極度縮量


def _bt_score_margin_trend(margin_trend, version="v1"):
    """
    融資趨勢評分（改進方案②）。
    v1（舊公式）：「大減」給 +6 分，比「減」的 +3 分高。
    v2（新公式）：回測顯示「大減」勝率(37.0%)反而低於「減」(57.1%)，縮小差距並反轉排序。
    """
    s = re.sub(r"\d+張\s*$", "", str(margin_trend).strip()).strip()  # 去掉真值字串的張數（如「↘ 減37張」）
    if not s or s == "➡ 持平":
        return 0
    if version == "v1":
        if "↘" in s: return 6 if "大減" in s else 3
        if "↗" in s: return -6 if "大增" in s else -3
        return 0
    if "↘" in s: return 3 if "大減" in s else 6
    if "↗" in s: return -6 if "大增" in s else -3
    return 0


def _bt_score_accel(accel_label, version="v1"):
    """
    買超加速度評分（改進方案④）。
    v1（舊公式）：範圍只有 0~3 分。
    v2（新公式）：回測顯示🚀加速是樣本夠大(n=31)且訊號最強的因子(58.1%勝率)，
      加大到 -2~+8 分；📉減速回測勝率偏弱(38.9%)，額外給負分。
    """
    s = str(accel_label).strip()
    if version == "v1":
        if not s: return 1
        if "🚀" in s: return 3
        if "📈" in s: return 2
        if "➡" in s: return 1
        if "📉" in s: return 0
        return 1
    if not s: return 1        # 資料不足：中性分
    if "🚀" in s: return 8
    if "📈" in s: return 4
    if "➡" in s: return 1
    if "📉" in s: return -2
    return 1


def _bt_score_amplitude(amp, version="v1"):
    """
    振幅%評分（改進方案⑤，v1舊公式沒有這個因子）。
    v2（新公式）：回測顯示 2~5% 表現最好(58.1%)，≥5%表現最差(26.7%)，範圍 -5~+5 分。
    """
    if version == "v1":
        return 0
    v = str(amp).strip()
    if not v:
        return 0
    try:
        a = float(v.replace("⚡", "").replace("%", ""))
    except ValueError:
        return 0
    if a < 2:   return 2
    elif a < 5: return 5
    else:       return -5


def _bt_recalc_score(feat, version="v1"):
    """
    用回測重建出的特徵，套用指定版本(v1舊/v2新)的評分公式重算一次總分。
    僅供 v1/v2 對照比較用，不等於正式推薦當時的完整評分（見上方限制說明）。
    """
    return (
        _bt_score_matrix(feat.get("consec", 0), feat.get("chip_lbl", ""), version) +
        _bt_score_margin(feat.get("margin_health", "")) +
        _bt_score_risk(feat.get("risk", "")) +
        _bt_score_volume_ratio(feat.get("vol_ratio", ""), version) +
        _bt_score_accel(feat.get("accel_lbl", ""), version) +
        _bt_score_short_trend(feat.get("short_trend", "")) +
        _bt_score_margin_trend(feat.get("margin_trend", ""), version) +
        _bt_score_shares_pct(feat.get("shares_pct", "")) +
        _bt_score_amplitude(feat.get("amp", ""), version)
    )


def _assign_tercile_buckets(detail_rows, score_key, bucket_key):
    """
    依 score_key 數值由小到大排序後三等分，把分組標籤寫回每筆 row 的 bucket_key 欄位。
    用排名（等筆數）三等分而非固定分數門檻，因為 v1/v2 兩個公式的總分尺度不同，
    用排名才能公平比較「同樣抓前1/3」時，兩個公式各自抓到的股票勝率誰比較好。
    """
    scored = [(i, r[score_key]) for i, r in enumerate(detail_rows) if r.get(score_key) is not None]
    if not scored:
        for r in detail_rows:
            r[bucket_key] = "未知"
        return
    scored.sort(key=lambda x: x[1])
    n = len(scored)
    idx_1, idx_2 = n // 3, 2 * n // 3
    labels = {}
    for rank, (i, _) in enumerate(scored):
        if rank < idx_1:      labels[i] = "低分組（後1/3）"
        elif rank < idx_2:    labels[i] = "中分組（中1/3）"
        else:                 labels[i] = "高分組（前1/3）"
    for i, r in enumerate(detail_rows):
        r[bucket_key] = labels.get(i, "未知")


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

    feat = {
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
        # ★ v1.7 佔股本比重%，直接讀真值（無法重建，只有 v11.47 起才有資料，v11.47 前為空字串→切面歸類「未知」）
        "shares_pct":    rec.get("shares_pct_real", ""),
        # ★ v1.9 對應 fetch_and_update.py v11.52（T42）：正式環境當時實際記錄的「另一版本分數」與
        # 「評分公式版本」真值（v11.52 前無資料，為空字串）。跟上面 recalc_v1/v2（用回測重建特徵事後算的
        # 近似分數）不同，這兩欄是正式選股當下用完整資訊（含集保大戶/今日買超金額/當日動能/大型股補償）
        # 算出來的真值，之後資料累積夠了，應該優先用這組真值分析，recalc_v1/v2 只是資料不足時的替代方案。
        "other_score":   rec.get("other_score_real", ""),
        "formula_ver":   rec.get("formula_ver_real", ""),
    }
    # ★ v1.8 新舊評分公式(v1舊/v2新)重算，供對照比較用（見上方「新舊評分公式比較」區塊限制說明）
    feat["recalc_v1"] = _bt_recalc_score(feat, "v1")
    feat["recalc_v2"] = _bt_recalc_score(feat, "v2")
    return feat


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

    # ★ v1.8 先依 v1(舊)/v2(新) 重算分數各自三等分分組，供下面的比較切面使用
    _assign_tercile_buckets(detail_rows, "recalc_v1", "recalc_v1_bucket")
    _assign_tercile_buckets(detail_rows, "recalc_v2", "recalc_v2_bucket")

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

    # 切面 4b：★ v1.8 新舊評分公式比較（T42，2026/09/17 改進方案①②③④⑤）
    # 用同一批資料分別套 v1(舊，目前正式上線)/v2(新，本次改進方案) 重算分數，各自三等分，
    # 比較「高分組」的 T+1 勝率誰比較高——若 v2 高分組明顯贏 v1 高分組，代表新公式排序能力較好。
    # ⚠️ 重要限制（選股偏誤）：這裡的股票池是「推薦歷史」裡既有的名單，而這份名單本身就是用
    #   v1(舊公式)選出來的——v2 版可能會選出完全不同的股票，那些股票根本不會出現在這份名單裡。
    #   所以本切面只能回答「用新公式幫v1選過的舊名單重新排序，效果如何」，
    #   不能回答「如果一開始就用新公式選股，結果會怎樣」。
    #   真正解決這個問題的方法見下面切面 4c：v11.52 起 fetch_and_update.py 會在正式選股當下，
    #   對「v1主榜+觀察組通過篩選的整批候選股」同步套用v2公式重新排名，
    #   找出「v2會選、但v1完全沒選進主榜/觀察組」的股票，額外寫入「明日關注」的
    #   「🆕 v2評分限定候選」區塊並照樣追蹤 T+1~T+5 表現——累積幾週後，切面4c才是
    #   真正沒有選股偏誤的 v1 vs v2 比較，本切面(4b)屆時只當輔助參考。
    # ⚠️ 重算分數也不含集保大戶/今日買超金額/當日動能/大型股補償（回測資料重建不出來，
    #    v1/v2 都不計入，見上方「新舊評分公式比較」程式區塊開頭的限制說明）。
    sections.append(("【新舊評分公式比較｜舊公式(v1)分組 × T+1 勝率】",
        _stats(detail_rows, lambda r: r.get("recalc_v1_bucket") or "未知", "t1_pnl")))
    sections.append(("【新舊評分公式比較｜新公式(v2)分組 × T+1 勝率】",
        _stats(detail_rows, lambda r: r.get("recalc_v2_bucket") or "未知", "t1_pnl")))

    # 切面 4c：★ v1.9 對應 fetch_and_update.py v11.52（T42）
    # 用正式環境「評分公式版本」真值分組（不是事後重算），才是真正公平的 v1/v2 對照：
    #   "v1"：目前正式選股邏輯選中的股票（主榜/觀察組）
    #   "v2限定候選"：v2公式選中、但v1完全沒選進主榜/觀察組的股票——這組資料才能回答
    #     「v2真正選股（不是幫v1選過的股票重新排序）勝率如何」，解決上面 4b 用回測重建特徵
    #     事後重算所受的先天限制（4b只能在v1已選過的股票池裡重新排序，看不到v2獨有的選股）。
    # ⚠️ v11.52 之前的舊資料沒有這個欄位，只有這次改版之後累積的新資料才會有標記，
    #    樣本會需要幾週才夠，這段期間本切面多半只會看到「未知(v11.52前)」。
    def _formula_ver_lbl(r):
        v = str(r.get("formula_ver", "")).strip()
        if not v:
            return "未知(v11.52前)"
        return v
    sections.append(("【評分公式版本(正式環境真值) × T+1 勝率】★需v11.52後新資料累積",
        _stats(detail_rows, _formula_ver_lbl, "t1_pnl")))

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
    # ★ v1.8 修正：margin_trend_real 原始字串含精確張數（如「↘ 減37張」「↘ 大減1974張」），
    #   若直接當 key 分組，幾乎每個張數都是獨立一組（n=1），勝率完全失真、全部樣本不足。
    #   改成只留方向類別（大增/增/大減/減/持平），對應 calc_margin_trend() 本來就只有的 4+1 種分類。
    def _margin_trend_lbl(r):
        v = str(r.get("margin_trend", "")).strip()
        if not v:
            return "未知"
        v = re.sub(r"\d+張\s*$", "", v).strip()
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

    # 切面 19：★ v1.7 佔股本比重% × T+1 勝率（K21，對應 fetch_and_update.py v11.47，
    #   v11.47 前無資料，歸類「未知」；門檻跟評分公式 _score_shares_pct 的分級一致，
    #   直接對照就能看出「連續買超吃下越多股本」是否真的跟後續勝率正相關）
    def _shares_pct_bucket(r):
        v = str(r.get("shares_pct", "")).strip()
        if not v:
            return "未知"
        try:
            sp = float(v)
        except ValueError:
            return "未知"
        if sp >= SHARES_PCT_HIGH:  return f"≥{SHARES_PCT_HIGH}%（重倉吃貨）"
        elif sp >= SHARES_PCT_MID: return f"{SHARES_PCT_MID}~{SHARES_PCT_HIGH}%"
        elif sp >= SHARES_PCT_LOW: return f"{SHARES_PCT_LOW}~{SHARES_PCT_MID}%"
        else:                      return f"<{SHARES_PCT_LOW}%"
    sections.append(("【佔股本比重% × T+1 勝率】",
        _stats(detail_rows, _shares_pct_bucket, "t1_pnl")))

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
    "佔股本比重%",   # ★ v1.7 對應 fetch_and_update.py v11.47（K21）
    "重算分數(舊v1)", "分組(舊v1)", "重算分數(新v2)", "分組(新v2)",   # ★ v1.8 T42 新舊評分公式比較
    "另一版本評分(真值)", "評分公式版本(真值)",   # ★ v1.9 對應 fetch_and_update.py v11.52
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
            r.get("shares_pct",""),
            r.get("recalc_v1",""), r.get("recalc_v1_bucket",""),
            r.get("recalc_v2",""), r.get("recalc_v2_bucket",""),
            r.get("other_score",""), r.get("formula_ver",""),
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
    parser = argparse.ArgumentParser(description="台灣股市推薦回測腳本 v1.9")
    parser.add_argument("--days",    type=int, default=0,
                        help="只回測最近 N 天的推薦（0 = 全部）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印結果，不寫 Google Sheets")
    parser.add_argument("--single",  action="store_true",
                        help="★ v1.1 單股回測模式（讀「回測設定」工作表）")
    args = parser.parse_args()

    print("=" * 50)
    print("  台灣股市推薦回測腳本 v1.9")
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
         "shares_pct":"3.5",
         "base_close":950.0,
         "t1":960.0,"t1_pnl":1.05, "t2":945.0,"t2_pnl":-0.53,
         "t3":970.0,"t3_pnl":2.11, "t4":975.0,"t4_pnl":2.63,
         "t5":980.0,"t5_pnl":3.16,
         "margin_health":"TODO","risk":"TODO"},
        {"rec_date":"2026/05/20","code":"6669","name":"緯穎","rec_score":72,
         "consec":4,"consec_source":"真值","chip_pct":15.1,"chip_lbl":"🟦 中度集中","chip_source":"真值",
         "accel_lbl":"📈 溫和加速","amp":"1.8%","dealer":"",
         "vol_ratio":"1.3","margin_trend":"➡ 持平","short_trend":"持平",
         "shares_pct":"1.1",
         "base_close":2500.0,
         "t1":2480.0,"t1_pnl":-0.80, "t2":2530.0,"t2_pnl":1.20,
         "t3":2550.0,"t3_pnl":2.00,  "t4":2540.0,"t4_pnl":1.60,
         "t5":2560.0,"t5_pnl":2.40,
         "margin_health":"TODO","risk":"TODO"},
        {"rec_date":"2026/05/21","code":"3037","name":"欣興","rec_score":65,
         "consec":3,"consec_source":"重建近似值","chip_pct":11.2,"chip_lbl":"🟦 中度集中","chip_source":"重建近似值",
         "accel_lbl":"➡ 持平","amp":"","dealer":"",
         "vol_ratio":"","margin_trend":"","short_trend":"",
         "shares_pct":"",
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
