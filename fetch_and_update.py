"""
台灣股市三大法人買超/賣超追蹤（版本見 VERSION 常數）
優化重點：
1. 修正版本標題（v8 → v10）
2. 合併 fetch_stock_quote / fetch_stock_day → fetch_stock_day_full
3. 族群聯動優先使用 current_prices 快取，不重複打 API
4. build_row 獨立為模組層函式（不再是 inner function）
5. update_analysis 拆分為 _build_history_maps / _build_analysis_rows / update_analysis
6. 分模式執行（--fetch-only / --sheet-only / --debug-margin）
7. 雲端快取（Google Sheets「快取」工作表）
8. 買超/賣超榜上限從 10 改 50，enrich_with_prices 改兩段式（STOCK_DAY_ALL batch 查表 → 查不到才逐支補抓）
9. 保底補抓三段式（batch→OTC→skip）、賣超移出 enrich_with_prices、Step 1.5 快取命中跳過 Step 2/3
"""

import subprocess, json, gspread, sys, os, time, re
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

VERSION = "v11.9"  # ← 每次 commit 只改這裡

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
    LARGE_BUY_DAYS  = getattr(_cfg, "LARGE_BUY_DAYS",  3)
    LARGE_BUY_RATIO = getattr(_cfg, "LARGE_BUY_RATIO", 1.5)
    SECTOR_MAP  = _cfg.SECTOR_MAP
    CODE_NAME_MAP = _cfg.CODE_NAME_MAP
    print("✅ 已載入 config.py")
except ImportError:
    print("⚠️ 找不到 config.py，使用主程式內建預設值")
    CODE_NAME_MAP   = {}
    LARGE_BUY_DAYS  = 3
    LARGE_BUY_RATIO = 1.5

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
    # ★ v10.8 過濾欄位數不足的列，避免 list index out of range
    data["data"] = [r for r in data["data"] if len(r) >= 19]

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
        return sorted(result, key=lambda x: x["net"], reverse=True)[:50]

    def foreign_sell():
        result = [
            make_stock(r,
                to_int(r[2]) + to_int(r[5]),
                to_int(r[3]) + to_int(r[6]),
                to_int(r[4]) + to_int(r[7]))
            for r in data["data"]
            if (to_int(r[4]) + to_int(r[7])) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:50]

    def trust_buy():
        result = [
            make_stock(r, to_int(r[8]), to_int(r[9]), to_int(r[10]))
            for r in data["data"] if to_int(r[10]) > 0
        ]
        return sorted(result, key=lambda x: x["net"], reverse=True)[:50]

    def trust_sell():
        result = [
            make_stock(r, to_int(r[8]), to_int(r[9]), to_int(r[10]))
            for r in data["data"] if to_int(r[10]) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:50]

    def dealer_buy():
        # 自營商合計買超 = [11]，買進/賣出用自行+避險
        result = [
            make_stock(r,
                to_int(r[12]) + to_int(r[15]),
                to_int(r[13]) + to_int(r[16]),
                to_int(r[11]))
            for r in data["data"] if to_int(r[11]) > 0
        ]
        return sorted(result, key=lambda x: x["net"], reverse=True)[:50]

    def dealer_sell():
        result = [
            make_stock(r,
                to_int(r[12]) + to_int(r[15]),
                to_int(r[13]) + to_int(r[16]),
                to_int(r[11]))
            for r in data["data"] if to_int(r[11]) < 0
        ]
        return sorted(result, key=lambda x: x["net"])[:50]

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
    - volume_ratio : 今日量 ÷ 前10日均量（無法計算時為 None）
    """
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={date_str}&stockNo={code}&response=json")
    text = curl_get(url)
    if not text or text.startswith("<"):
        return 0.0, 0.0, 0, "N/A", None
    try:
        data = json.loads(text)
        if data.get("stat") != "OK" or not data.get("data"):
            return 0.0, 0.0, 0, "N/A", None
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
            # ★ v10.7 量比：今日量 ÷ 前10日均量
            prev_rows = rows[max(0, idx-10):idx]
            if prev_rows:
                prev_vols = []
                for pr in prev_rows:
                    try:
                        prev_vols.append(int(float(str(pr[1]).replace(",", "")) / 1000))
                    except Exception:
                        pass
                avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0
                volume_ratio = round(volume_lots / avg_vol, 2) if avg_vol > 0 else None
            else:
                volume_ratio = None
            return avg, close, volume_lots, change_pct, volume_ratio
    except Exception:
        pass
    return 0.0, 0.0, 0, "N/A", None


# ═══════════════════════════════════════════════
# ★ v10.8 STOCK_DAY_ALL 一次抓全市場行情
# ═══════════════════════════════════════════════

def fetch_price_map_batch(date_str):
    """
    用 STOCK_DAY_ALL 一次抓全市場當日行情，回傳查表：
    { code: (avg_price, close, volume_lots, change_pct_str, None) }
    量比固定回傳 None（無前10日資料，只有當日）。
    失敗時回傳空 dict，由 enrich_with_prices 回退逐支補抓。
    """
    # 不帶 date 參數，抓最新一日；帶日期時歷史資料為空
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
    print(f"  [batch] STOCK_DAY_ALL {date_str}...")
    text = curl_get(url)
    if not text or text.startswith("<"):
        print("  [batch] ❌ 無回應，改逐支抓取")
        return {}
    try:
        data = json.loads(text)
    except Exception as e:
        print(f"  [batch] ❌ JSON 解析失敗：{e}，改逐支抓取")
        return {}

    if data.get("stat") != "OK":
        print(f"  [batch] ❌ stat={data.get('stat')}，改逐支抓取")
        return {}

    # 驗證回傳日期是否吻合目標日期
    api_date = str(data.get("date", ""))
    if api_date and api_date != date_str:
        print(f"  [batch] ⚠️ 回傳日期 {api_date} ≠ 目標 {date_str}，batch 不適用")
        return {}

    rows = data.get("data", [])
    price_map = {}
    for row in rows:
        try:
            code   = str(row[0]).strip()
            shares = float(str(row[2]).replace(",", ""))
            amount = float(str(row[3]).replace(",", ""))
            close  = float(str(row[7]).replace(",", ""))
            diff_str = str(row[8]).replace(",", "").strip()
            diff = float(diff_str) if diff_str not in ("", "--", "X0.00") else 0.0
            vol    = int(shares / 1000)
            avg    = round(amount / shares, 2) if shares > 0 else 0.0
            prev_close = close - diff
            pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
            sign = "+" if pct >= 0 else ""
            pct_str = f"{sign}{pct:.2f}%"
            price_map[code] = (avg, close, vol, pct_str, None)  # 量比 None
        except Exception:
            continue

    print(f"  [batch] ✅ {len(price_map)} 支")
    return price_map


def fetch_price_map_otc(date_str):
    """
    ★ v10.8 上櫃股行情（OTC/櫃買中心）
    一次抓全上櫃，回傳 { code: (avg, close, vol, pct_str, None) }
    API: tpex.org.tw daily_close_quotes
    欄位：[0]代號 [1]名稱 [2]收盤 [3]漲跌 [7]均價 [8]成交股數
    """
    year = int(date_str[:4]) - 1911
    d    = f"{year}/{date_str[4:6]}/{date_str[6:]}"
    url  = (f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes"
            f"/stk_quote_result.php?d={d}&output=json")
    print(f"  [otc] 抓取上櫃行情（{date_str}）...")
    text = curl_get(url)
    if not text or text.startswith("<") or text.startswith("Host"):
        print("  [otc] ❌ 無回應")
        return {}
    try:
        data  = json.loads(text)
        rows  = data["tables"][0].get("data", data["tables"][0].get("aaData", []))
    except Exception as e:
        print(f"  [otc] ❌ 解析失敗：{e}")
        return {}

    otc_map = {}
    for row in rows:
        try:
            code    = str(row[0]).strip()
            close   = float(str(row[2]).replace(",", "").strip())
            diff    = float(str(row[3]).replace(",", "").strip().replace("+", ""))
            avg     = float(str(row[7]).replace(",", "").strip())
            shares  = float(str(row[8]).replace(",", "").strip())
            vol     = int(shares / 1000)
            prev_close = close - diff
            pct    = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
            sign   = "+" if pct >= 0 else ""
            pct_str = f"{sign}{pct:.2f}%"
            otc_map[code] = (avg, close, vol, pct_str, None)
        except Exception:
            continue
    print(f"  [otc] ✅ {len(otc_map)} 支")
    return otc_map


# ═══════════════════════════════════════════════
# ★ v11.6 外資期貨未平倉燈號（期交所）
# ═══════════════════════════════════════════════

def _futures_dots(value, per_dot, max_dots=5):
    """
    依絕對值換算燈號顆數（最多 max_dots 顆，無條件捨去）。
    正值 → 🟢，負值 → 🔴，未滿一顆 → 🟡
    口數：每 3,000 口一顆；變化量：每 1,500 口一顆
    """
    dots = min(int(abs(value) // per_dot), max_dots)
    if dots == 0:
        return "🟡"
    emoji = "🟢" if value > 0 else "🔴"
    return emoji * dots


def fetch_futures_signal(date_str):
    """
    從期交所抓外資大台指未平倉淨口數，回傳 (口數, 燈號) 或 None（失敗時）。
    API: https://www.taifex.com.tw/cht/3/futContractsDateDown（CSV 格式，Big5 編碼）

    燈號邏輯（每 3,000 口一顆，最多 5 顆）：
      🟢🟢🟢🟢🟢 > +15,000 口
      🟢🟢🟢🟢   +12,001 ~ +15,000
      ...
      🟡          -3,000 ~ +3,000（未達一顆）
      ...
      🔴🔴🔴🔴🔴 < -15,000 口
    """
    d = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
    url = (f"https://www.taifex.com.tw/cht/3/futContractsDateDown"
           f"?queryStartDate={d}&queryEndDate={d}&commodityId=TXF")

    # ★ 期交所回傳 Big5 編碼，需用 bytes 模式讀取再 decode
    result = subprocess.run([
        "curl", "-s", "--max-time", "20",
        "-H", "User-Agent: Mozilla/5.0",
        "-H", "Referer: https://www.taifex.com.tw/",
        url
    ], capture_output=True)   # 不帶 text=True，取 bytes

    if not result.stdout:
        print("  ⚠️ 期交所 API 無回應")
        return None

    try:
        text = result.stdout.decode("big5", errors="replace")
    except Exception as e:
        print(f"  ⚠️ 期交所編碼解析失敗：{e}")
        return None

    if text.startswith("<"):
        print("  ⚠️ 期交所 API 回傳 HTML（可能無當日資料）")
        return None

    try:
        # CSV 格式：日期,商品名稱,身份別,多方口數,多方契約金額,空方口數,空方契約金額,多空淨額口數,...
        lines = [l for l in text.strip().splitlines() if l.strip()]
        for line in lines:
            cols = line.split(",")
            # 找外資那列（身份別欄含「外資」）
            if len(cols) >= 8 and "外資" in cols[2]:
                net    = to_int(cols[7])   # 多空淨額口數
                signal = _futures_dots(net, per_dot=3000)
                return net, signal
        print("  ⚠️ 期交所資料找不到外資欄位")
        return None
    except Exception as e:
        print(f"  ⚠️ 期交所資料解析失敗：{e}")
        return None


def fetch_futures_two_days(date_str):
    """
    抓今日和前一交易日的期貨資料，計算增減。
    回傳 (net: int, signal: str, delta_str: str) 或 None。
    delta 燈號：每 1,500 口一顆，最多 5 顆。
    """
    today_result = fetch_futures_signal(date_str)
    if today_result is None:
        return None
    net, signal = today_result

    prev_str = prev_trading_date(date_str)
    prev_result = fetch_futures_signal(prev_str)

    if prev_result is not None:
        prev_net, _ = prev_result
        delta = net - prev_net
        delta_signal = _futures_dots(delta, per_dot=1500)
        sign = "+" if delta >= 0 else ""
        delta_str = f"較前日 {delta_signal} {sign}{delta:,}"
    else:
        delta_str = ""

    return net, signal, delta_str


# ═══════════════════════════════════════════════
# ★ v11.9 大盤指數（MI_INDEX）
# ═══════════════════════════════════════════════

def fetch_market_index(date_str):
    """
    從 TWSE MI_INDEX API 抓加權指數當日漲跌幅。
    回傳 float（如 1.23 或 -0.45），失敗時回傳 None。
    API 欄位（Y9999）：[0]指數名稱 [1]收盤 [2]漲跌 [3]漲跌幅(%)
    """
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
           f"?date={date_str}&response=json")
    text = curl_get(url)
    if not text or text.startswith("<"):
        print("  ⚠️ MI_INDEX 無回應")
        return None
    try:
        data = json.loads(text)
        if data.get("stat") != "OK":
            print(f"  ⚠️ MI_INDEX stat={data.get('stat')}")
            return None
        # tables[8] 是「各類指數」，找「發行量加權股價指數」
        for table in data.get("tables", []):
            for row in table.get("data", []):
                if not row:
                    continue
                name = str(row[0]).strip()
                if "加權股價指數" in name or name == "發行量加權股價指數":
                    # 漲跌幅欄：去除 % 符號和正負符號後轉 float
                    pct_raw = str(row[3]).replace("%", "").replace("+", "").replace(",", "").strip()
                    # 漲跌方向從漲跌欄判斷（含 ▲▼ 或正負）
                    diff_raw = str(row[2]).strip()
                    try:
                        pct = float(pct_raw)
                        if "▼" in diff_raw or (pct > 0 and "-" in diff_raw):
                            pct = -abs(pct)
                        return round(pct, 2)
                    except ValueError:
                        continue
        print("  ⚠️ MI_INDEX 找不到加權指數列")
        return None
    except Exception as e:
        print(f"  ⚠️ MI_INDEX 解析失敗：{e}")
        return None


def calc_relative_strength(change_pct_str, market_pct):
    """
    計算個股相對大盤強弱。
    change_pct_str: 個股當日漲跌幅字串，如 "+2.30%" 或 "-1.20%"
    market_pct: 大盤漲跌幅 float，如 1.23 或 -0.45
    回傳字串，如 "+1.07%" / "-0.45%" / ""（無法計算）
    """
    if market_pct is None:
        return ""
    try:
        stock_pct = float(str(change_pct_str).replace("%", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return ""
    diff = round(stock_pct - market_pct, 2)
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff}%"


# ═══════════════════════════════════════════════
# 批次抓均價、收盤、成交量（寫入 stock dict）
# ═══════════════════════════════════════════════

def enrich_with_prices(groups, date_str, prefetched_map=None):
    """
    ★ v10.8 兩段式：
    1. STOCK_DAY_ALL batch 查表（0.3 秒抓全市場）
    2. batch 查不到的代號才逐支打 STOCK_DAY（保留量比計算）
    prefetched_map: 外部已抓好的 batch map，避免重複打 API
    """
    all_codes = {}
    for group in groups:
        for stock in group:
            all_codes[stock["code"]] = (0.0, 0.0, 0, "N/A", None)

    total = len(all_codes)

    # 第一段：TWSE batch（若外部已抓則直接用）
    batch_map = prefetched_map if prefetched_map is not None else fetch_price_map_batch(date_str)
    hit, miss_codes = 0, []
    for code in all_codes:
        if code in batch_map:
            all_codes[code] = batch_map[code]
            hit += 1
        else:
            miss_codes.append(code)

    print(f"  [batch] 命中 {hit}/{total}，剩餘 {len(miss_codes)} 支...")

    # 第二段：OTC batch（上櫃股）
    if miss_codes:
        otc_map = fetch_price_map_otc(date_str)
        otc_hit, still_miss = 0, []
        for code in miss_codes:
            if code in otc_map:
                all_codes[code] = otc_map[code]
                otc_hit += 1
            else:
                still_miss.append(code)
        print(f"  [otc] 命中 {otc_hit}/{len(miss_codes)}，補抓 {len(still_miss)} 支...")
        miss_codes = still_miss

    # 第三段：逐支補抓（仍未命中者，保留量比計算）
    for i, code in enumerate(miss_codes):
        avg, close, vol, chg, vr = fetch_stock_day_full(code, date_str)
        if close > 0:
            all_codes[code] = (avg, close, vol, chg, vr)
            print(f"  [補抓 {i+1}/{len(miss_codes)}] {code}: 均價={avg:.2f} 收盤={close:.2f} 量比={vr}")
        else:
            print(f"  [補抓 {i+1}/{len(miss_codes)}] {code}: 收盤=0，跳過（資料未就緒）")
        if i < len(miss_codes) - 1:
            time.sleep(0.5)

    for group in groups:
        for stock in group:
            avg, close, vol, chg, vr = all_codes.get(stock["code"], (0.0, 0.0, 0, "N/A", None))
            stock["avg_price"]     = avg
            stock["close"]         = close
            stock["volume"]        = vol
            stock["change_pct"]    = chg
            stock["volume_ratio"]  = vr

    # 回傳快取供族群聯動直接使用（含漲跌幅）
    return all_codes


# ═══════════════════════════════════════════════
# 批次抓融資融券（一次全市場）
# ═══════════════════════════════════════════════

def prev_trading_date(date_str):
    """回傳 date_str 的前一個交易日（跳過週末）"""
    d = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

def _fetch_margin_map(date_str):
    """
    往前最多找 7 個交易日，回傳第一個有資料的融資融券 dict。
    MI_MARGN 為 T+1，需查前一個交易日。
    API 結構：tables[1] 為個股資料
    欄位：[0]代號 [5]融資前日餘額 [6]融資今日餘額 [12]融券今日餘額
    """
    d = datetime.strptime(prev_trading_date(date_str), "%Y%m%d")
    for _ in range(7):
        margin_date = d.strftime("%Y%m%d")
        url = (f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
               f"?date={margin_date}&selectType=ALL&response=json")
        text = curl_get(url)
        if text and not text.startswith("<"):
            try:
                data = json.loads(text)
                tables = data.get("tables", [])
                if data.get("stat") == "OK" and len(tables) >= 2:
                    result = {}
                    for row in tables[1].get("data", []):
                        if len(row) < 13:
                            continue
                        rc  = str(row[0]).strip()
                        mb  = to_int(row[6])   # 融資今日餘額
                        mp  = to_int(row[5])   # 融資前日餘額
                        sb  = to_int(row[12])  # 融券今日餘額
                        result[rc] = (mb, mb - mp, sb)
                    if result:
                        print(f"  融資融券日期：{margin_date}（全市場 {len(result)} 支）")
                        return result
            except Exception as e:
                print(f"  ⚠️ 融資融券解析失敗：{e}")
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    print("  ⚠️ 往前 7 天都找不到融資融券資料")
    return {}

def enrich_with_margin(groups, date_str):
    """批次抓融資融券資料（MI_MARGN 一次抓全市場，使用前一個交易日）"""
    all_codes = {stock["code"] for group in groups for stock in group}
    total = len(all_codes)

    margin_map = _fetch_margin_map(date_str)

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
# ★ v11.6 融券歷史工作表（獨立工作表，方案B）
# ═══════════════════════════════════════════════

def update_short_history(ss, date_str, current_margin):
    """
    將本次執行中的融券餘額寫入「融券歷史」工作表（每日一批）。
    格式：日期, 代號, 融券餘額(張)
    保留 31 天，超過自動清除。
    只寫 current_margin 中有融券資料的股票（sb > 0）。
    """
    disp = fmt_date(date_str)
    ws   = get_or_create(ss, "融券歷史", 3)

    from datetime import datetime, timedelta
    _cutoff = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=31)).strftime("%Y/%m/%d")
    purge_old_rows(ws, _cutoff)

    existing = ws.get_all_values()
    headers  = ["日期", "代號", "融券餘額(張)"]
    if not existing:
        existing = [headers]

    # 過濾掉今天已寫入的列（重跑時覆蓋）
    if existing and existing[0] == headers:
        kept = [existing[0]] + [r for r in existing[1:] if not (r and r[0] == disp)]
    else:
        kept = [r for r in existing if not (r and r[0] == disp)]
        kept = [headers] + kept

    new_rows = [
        [disp, code, sb]
        for code, (mb, mc, sb) in current_margin.items()
        if sb > 0
    ]
    new_rows.sort(key=lambda r: r[1])   # 依代號排序

    full = kept + new_rows
    ws.clear()
    if ws.row_count < len(full) + 10:
        ws.add_rows(len(full) + 10 - ws.row_count)
    ws.update(range_name="A1", values=full)
    print(f"  ✅ 融券歷史 寫入 {len(new_rows)} 筆（{disp}）")


def load_short_history(ss, date_str):
    """
    從「融券歷史」工作表讀取近期資料，建立趨勢查表。
    回傳 {code: [(disp, sb), ...]}，按日期升序。
    """
    try:
        ws   = ss.worksheet("融券歷史")
        rows = ws.get_all_values()
    except Exception:
        return {}

    result = {}
    for row in rows[1:]:   # 跳標題
        if len(row) < 3 or not row[1]:
            continue
        d_str, code = row[0].strip(), row[1].strip()
        try:
            sb = int(str(row[2]).replace(",", ""))
        except ValueError:
            continue
        result.setdefault(code, []).append((d_str, sb))

    # 每支股票按日期升序
    for code in result:
        result[code].sort(key=lambda x: x[0])
    return result


def calc_short_trend(short_hist, code):
    """
    計算融券連增/連減天數。
    回傳 (連增天數, 連減天數, 趨勢標記)。
    趨勢標記：「↗ 連增N天」/ 「↘ 連減N天」/ 「➡ 持平」/ 「」（資料不足）
    """
    entries = short_hist.get(code, [])
    if len(entries) < 2:
        return 0, 0, ""

    # 從最新往回看
    vals = [sb for _, sb in entries]
    up_count   = 0
    down_count = 0

    # 連增：從最後一天往前，每天都比前一天多
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] > vals[i - 1]:
            up_count += 1
        else:
            break

    # 連減：從最後一天往前，每天都比前一天少
    if up_count == 0:
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] < vals[i - 1]:
                down_count += 1
            else:
                break

    if up_count >= 2:
        label = f"↗ 連增{up_count}天"
    elif down_count >= 2:
        label = f"↘ 連減{down_count}天"
    elif up_count == 1:
        label = "↗ 增"
    elif down_count == 1:
        label = "↘ 減"
    else:
        label = "➡ 持平"

    return up_count, down_count, label


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

def calc_signal(code, buy_entries, sell_hist, disp,
                f_entries=None, t_entries=None, d_entries=None,
                all_dates=None, net_by_date=None):
    """
    訊號判斷（v11.1）：
    第一層：今日買賣狀態
      🔴 今日賣超   — 三法人合計今日為賣超
      🔥 大量買超   — 今日買超 ≥ 近3日平均 × 1.5（且近3日有資料）
      ✅ 持續買入   — 今日有買超，量正常
      ⚠️ 已停止買入 — 歷史有買超但今日未出現

    第二層：各法人今日狀態（附加在後）
      外資/投信/自營商各自顯示：
        🟢 今日買超  🔴 今日賣超  ⚪ 未出現
    """
    sells   = sell_hist.get(code, {})
    f_sells = sells.get("f", []) if isinstance(sells, dict) else sells
    t_sells = sells.get("t", []) if isinstance(sells, dict) else []
    d_sells = sells.get("d", []) if isinstance(sells, dict) else []

    f_entries = f_entries or []
    t_entries = t_entries or []
    d_entries = d_entries or []

    # ── 第一層：今日整體狀態 ──
    today_f_buy  = sum(e[1] for e in f_entries if e[0] == disp)
    today_t_buy  = sum(e[1] for e in t_entries if e[0] == disp)
    today_d_buy  = sum(e[1] for e in d_entries if e[0] == disp)
    today_f_sell = sum(e[1] for e in f_sells   if e[0] == disp)
    today_t_sell = sum(e[1] for e in t_sells   if e[0] == disp)
    today_d_sell = sum(e[1] for e in d_sells   if e[0] == disp)
    today_net    = today_f_buy + today_t_buy + today_d_buy \
                 - today_f_sell - today_t_sell - today_d_sell

    buy_dates = sorted(set(e[0] for e in buy_entries), reverse=True)
    active_today = buy_dates and buy_dates[0] == disp

    if today_net < 0:
        main_signal = "🔴 今日賣超"
    elif not active_today:
        main_signal = "⚠️ 已停止買入"
    else:
        # 大量判斷：今日買超 ≥ 近3日均量 × 1.5
        if all_dates and net_by_date and today_net > 0:
            daily_net = net_by_date.get(code, {})
            # 找今天以前的最近3個交易日（只計買超 > 0 的天）
            past_dates = [d for d in reversed(all_dates) if d < disp][:LARGE_BUY_DAYS]
            past_nets  = [max(daily_net.get(d, 0), 0) for d in past_dates]
            if past_nets and sum(past_nets) > 0:
                avg_n = sum(past_nets) / len(past_nets)
                main_signal = "🔥 大量買超" if today_net >= avg_n * LARGE_BUY_RATIO else "✅ 持續買入"
            else:
                main_signal = "✅ 持續買入"
        else:
            main_signal = "✅ 持續買入"

    # ── 第二層：各法人今日標記 ──
    def inst_label(buy_today, sell_today):
        if buy_today > 0 and sell_today == 0:
            return "🟢"
        if sell_today > 0 and buy_today == 0:
            return "🔴"
        if buy_today > 0 and sell_today > 0:
            return "🟡"   # 同日既買又賣（少見）
        return "⚪"

    f_lbl = inst_label(today_f_buy, today_f_sell)
    t_lbl = inst_label(today_t_buy, today_t_sell)
    d_lbl = inst_label(today_d_buy, today_d_sell)

    inst_part = f"外{f_lbl}投{t_lbl}自{d_lbl}"
    return f"{main_signal} {inst_part}"

def is_etf_code(code):
    """
    ETF 判斷：
    1. 代號含字母（如 00631L、00403A）
    2. 純數字但以 '00' 開頭（如 00891、009819、00919）
    """
    s = str(code).strip()
    if any(c.isalpha() for c in s):
        return True
    if s.startswith("00"):
        return True
    return False

def calc_chip_concentration(total_net_lots, volume_lots, code=""):
    if is_etf_code(code):
        return "", ""   # ETF 單位不同，不計算
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

def purge_old_rows(ws, cutoff_date_str, date_col=0, header_rows=1):
    """
    清除工作表中日期超過保留期限的列。
    - cutoff_date_str: 截止日期字串，格式 YYYY/MM/DD，早於此日期的列會被刪除
    - date_col: 日期欄位的索引（預設0，即A欄）
    - header_rows: 要保留的標題列數（預設1）
    只處理格式為 YYYY/MM/DD 的日期欄位，分隔線列（空列或標記列）一律保留。
    回傳刪除筆數。
    """
    from datetime import datetime
    try:
        cutoff = datetime.strptime(cutoff_date_str, "%Y/%m/%d")
    except ValueError:
        return 0

    all_rows = ws.get_all_values()
    if not all_rows:
        return 0

    kept = all_rows[:header_rows]
    removed = 0
    for row in all_rows[header_rows:]:
        date_val = row[date_col].strip() if len(row) > date_col else ""
        # 嘗試解析日期，無法解析（分隔線/標記列）一律保留
        try:
            row_date = datetime.strptime(date_val, "%Y/%m/%d")
            if row_date < cutoff:
                removed += 1
                continue
        except ValueError:
            pass  # 非日期列，保留
        kept.append(row)

    if removed > 0:
        ws.clear()
        if ws.row_count < len(kept) + 10:
            ws.add_rows(len(kept) + 10 - ws.row_count)
        ws.update(range_name="A1", values=kept)
        print(f"  🗑️  {ws.title}：清除 {removed} 筆超過1個月的舊資料")
    return removed

def prepend_block(ws, new_block, disp, date_marker_prefix, sep_cols):
    """
    ★ v10.8 batch 寫入：
    舊邏輯：delete_rows → get_all_values → clear → add_rows → update（4~5次呼叫）
    新邏輯：get_all_values → Python切片過濾 → clear → update（2次呼叫，add_rows 只在必要時）
    """
    existing = ws.get_all_values()
    # 今天已有資料 → 在 Python 裡直接切掉，不用 delete_rows
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
        existing = existing[end_row:]   # ★ Python 切片，不打 API

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
    for label, data in [("外資及陸資買超前五十名", foreign),
                        ("投信買超前五十名",       trust),
                        ("自營商買超前五十名",     dealer)]:
        block += [[f"【{label}】"]+[""]*9, hdrs]
        for i, r in enumerate(data):
            vol = r.get("volume", 0)
            chip_pct, chip_lbl = calc_chip_concentration(r["net"], vol, r["code"])
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
    for label, data in [("外資及陸資賣超前五十名", f_sell),
                        ("投信賣超前五十名",       t_sell),
                        ("自營商賣超前五十名",     d_sell)]:
        block += [[f"【{label}】"]+[""]*7, hdrs]
        for i, r in enumerate(data):
            block.append([i+1, r["code"], r["name"], r["buy"], r["sell"],
                          abs(r["net"]), r["avg_price"] or "", r.get("close") or ""])
        block += [[""]*8]
    prepend_block(ws, block, disp, "資料日期：", 8)
    print("  ✅ 今日賣超排行 更新完成")


def append_history(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell):
    """
    ★ v10.8 batch 寫入：
    避免逐行 delete_rows（300次寫入），改為讀出→過濾今天→整批寫回，
    只用 2 次 API 呼叫（clear + update）。
    """
    ws   = get_or_create(ss, "歷史紀錄", 10)
    disp = fmt_date(date_str)
    headers = ["日期","名次","代號","股票名稱","法人類別","張數",
               "當日均價(元)","買/賣","成交量(張)","籌碼集中度"]

    # 清除超過1個月的舊資料
    from datetime import datetime, timedelta
    _cutoff = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=31)).strftime("%Y/%m/%d")
    purge_old_rows(ws, _cutoff)

    existing = ws.get_all_values()
    if not existing:
        existing = [headers]

    # 如果第一列是標題，保留；過濾掉今天已有的資料
    if existing and existing[0] == headers:
        kept = [existing[0]] + [r for r in existing[1:] if not (r and r[0] == disp)]
    else:
        kept = [r for r in existing if not (r and r[0] == disp)]
        kept = [headers] + kept

    new_rows = []
    for label, data in [("外資",foreign),("投信",trust),("自營商",dealer)]:
        for i, r in enumerate(data):
            vol = r.get("volume", 0)
            chip_pct, chip_lbl = calc_chip_concentration(r["net"], vol, r["code"])
            chip_str = f"{chip_pct} {chip_lbl}".strip() if chip_pct else ""
            new_rows.append([disp, i+1, r["code"], r["name"], label,
                             r["net"], r["avg_price"] or "", "買超", vol or "", chip_str])
    for label, data in [("外資",f_sell),("投信",t_sell),("自營商",d_sell)]:
        for i, r in enumerate(data):
            new_rows.append([disp, i+1, r["code"], r["name"], label,
                             abs(r["net"]), r["avg_price"] or "", "賣超", "", ""])

    full = kept + new_rows
    ws.clear()
    if ws.row_count < len(full) + 10:
        ws.add_rows(len(full) + 10 - ws.row_count)
    ws.update(range_name="A1", values=full)
    print(f"  ✅ 歷史紀錄 新增 {len(new_rows)} 筆（合計 {len(full)-1} 筆）")


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


def build_row(s, current_prices, current_margin, sell_hist, disp, all_dates, net_by_date,
              short_hist=None, market_pct=None):
    """
    ★ 優化：從 inner function 獨立為模組層函式。
    建立對照分析 / 每日快照 的單列資料。
    ★ v11.6：新增 short_hist 參數，輸出融券趨勢欄。
    ★ v11.9：新增 market_pct 參數，輸出相對強弱欄。
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

    _, close, volume, change_pct, volume_ratio = current_prices.get(code, (0.0, 0.0, 0, "N/A", None))
    all_entries = s["f"] + s["t"] + s["d"]
    all_remaining, all_wavg = calc_position_fifo(all_entries, all_sells)
    pct_str, risk = calc_risk(close, all_wavg)
    signal        = calc_signal(code, all_entries, sell_hist, disp,
                               f_entries=s["f"], t_entries=s["t"], d_entries=s["d"],
                               all_dates=all_dates, net_by_date=net_by_date)
    trust_label   = calc_trust_label(t_consec)
    accel_ratio, accel_label = _calc_buy_accel(all_entries, all_dates)

    today_net           = sum(e[1] for e in all_entries if e[0] == disp)
    chip_pct, chip_lbl  = calc_chip_concentration(today_net, volume, code)
    mb, mc, sb          = current_margin.get(code, (0, 0, 0))
    margin_health       = calc_margin_health(mc, today_net)

    # ★ v11.6 融券趨勢
    _, _, short_trend = calc_short_trend(short_hist or {}, code)

    # ★ v11.9 大盤相對強弱
    relative_strength = calc_relative_strength(change_pct, market_pct)

    return [
        code, s["name"],
        f_total, f_consec, f_net, f_wavg or "", f_trend,
        t_total, t_consec, t_net, t_wavg or "", t_trend, trust_label,
        d_total, d_consec, d_net, d_wavg or "", d_trend,
        f_total + t_total + d_total,
        close or "", change_pct or "", pct_str, risk, signal, accel_label,
        chip_pct, chip_lbl,
        mb or "", mc if (mb or mc) else "", sb or "", margin_health,
        volume_ratio,
        short_trend,
        relative_strength,
        s["last"]
    ]


ANALYSIS_HEADERS = [
    "代號","股票名稱",
    "外資累計天數","外資連續天數","外資買超(張)","外資加權均價","外資趨勢",
    "投信累計天數","投信連續天數","投信買超(張)","投信加權均價","投信趨勢","投信標記",
    "自營商累計天數","自營商連續天數","自營商買超(張)","自營商加權均價","自營商趨勢",
    "合計累計天數","現價","當日漲跌%","漲幅%","出貨風險","訊號","買超加速度",
    "籌碼集中度%","籌碼集中度評級",
    "融資餘額(張)","融資增減(張)","融券餘額(張)","融資健康度",
    "量比",
    "融券趨勢",
    "相對強弱%",   # ★ v11.9 [32]
    "最近出現日"   # [33]
]


# ═══════════════════════════════════════════════
# ★ v11.8 族群熱度排行工作表
# ═══════════════════════════════════════════════

SECTOR_HEAT_DAYS = 5   # 近幾個交易日（交易日剛好一週）

def _last_n_trading_dates(all_dates, n):
    """從 all_dates（已排序）取最後 n 個交易日"""
    return set(all_dates[-n:]) if len(all_dates) >= n else set(all_dates)


def update_sector_heatmap(ss, date_str, ss_hist_rows=None):
    """
    計算近 SECTOR_HEAT_DAYS 個交易日各族群熱度，寫入「族群熱度」工作表。
    熱度分 = 不同成員數 × 2 + 觸發天數 × 1
    趨勢：近 3 天 vs 近 5 天每日不同成員數均值比較
    主力成員：近 5 天買超張數前 3 名
    ss_hist_rows: 外部傳入歷史紀錄列表（避免重複抓 Sheets），None 時自行抓取。
    """
    disp = fmt_date(date_str)
    ws   = get_or_create(ss, "族群熱度", 7)

    # ── 取得歷史紀錄 ──
    if ss_hist_rows is None:
        hist = get_or_create(ss, "歷史紀錄")
        rows = hist.get_all_values()[1:]
    else:
        rows = ss_hist_rows

    if not rows:
        print("  ⚠️ 歷史紀錄無資料，跳過族群熱度")
        return

    # ── 取所有交易日清單，找近 5 個 ──
    all_dates = sorted(set(r[0] for r in rows if r and r[0]))
    recent_dates  = _last_n_trading_dates(all_dates, SECTOR_HEAT_DAYS)
    recent3_dates = _last_n_trading_dates(all_dates, 3)

    # ── 建立 {代號: [法人類別, 日期, 張數]} 近期資料 ──
    # 格式：code_day_map[code][date] = total_net（三法人合計，只算買超）
    code_day_map = {}   # {code: {date: net}}
    for row in rows:
        if len(row) < 8 or not row[2]: continue
        date, code, net_s, buy_sell = row[0], row[2], row[5], row[7] if len(row) > 7 else "買超"
        if date not in recent_dates: continue
        if buy_sell != "買超": continue
        try:   net = int(str(net_s).replace(",", ""))
        except: continue
        code_day_map.setdefault(code, {})
        code_day_map[code][date] = code_day_map[code].get(date, 0) + net

    # ── 計算各族群熱度 ──
    results = []
    for sector, members in SECTOR_MAP.items():
        if not members: continue
        member_set = set(members)

        # 各日出現的不同成員數
        day_members = {}   # {date: set(codes)}
        for code in member_set:
            for date in code_day_map.get(code, {}):
                if date in recent_dates:
                    day_members.setdefault(date, set()).add(code)

        if not day_members: continue   # 近 5 天完全沒出現

        trigger_days   = len(day_members)
        unique_members = len(set(c for codes in day_members.values() for c in codes))
        heat_score     = unique_members * 2 + trigger_days

        # 趨勢：近 3 天 vs 近 5 天每日成員數均值
        avg5 = sum(len(v) for v in day_members.values()) / SECTOR_HEAT_DAYS
        avg3_days = {d: v for d, v in day_members.items() if d in recent3_dates}
        avg3 = sum(len(v) for v in avg3_days.values()) / 3 if avg3_days else 0
        if avg3 > avg5 * 1.2:   trend = "↗ 升溫"
        elif avg3 < avg5 * 0.8: trend = "↘ 降溫"
        else:                   trend = "➡ 持平"

        # 主力成員：近 5 天買超張數前 3 名
        member_total = {}
        for code in member_set:
            total = sum(code_day_map.get(code, {}).values())
            if total > 0:
                name = CODE_NAME_MAP.get(code, code)
                member_total[f"{name}({code})"] = total
        top3 = sorted(member_total.items(), key=lambda x: x[1], reverse=True)[:3]
        top3_str = "  ".join(f"{n} {v:,}張" for n, v in top3)

        results.append({
            "sector":         sector,
            "trigger_days":   trigger_days,
            "unique_members": unique_members,
            "heat_score":     heat_score,
            "trend":          trend,
            "top3":           top3_str,
        })

    # 依熱度分降冪排
    results.sort(key=lambda x: x["heat_score"], reverse=True)

    # ── 寫入工作表 ──
    headers = ["族群", f"近{SECTOR_HEAT_DAYS}天觸發天數", "不同成員數", "熱度分", "趨勢", "主力成員（近5天買超）", "統計截至"]
    data = [
        [f"族群熱度排行（近 {SECTOR_HEAT_DAYS} 個交易日，統計截至 {disp}）"] + [""] * 6,
        headers,
    ]
    for r in results:
        data.append([
            r["sector"],
            r["trigger_days"],
            r["unique_members"],
            r["heat_score"],
            r["trend"],
            r["top3"],
            disp,
        ])

    ws.clear()
    if ws.row_count < len(data) + 5:
        ws.add_rows(len(data) + 5 - ws.row_count)
    ws.update(range_name="A1", values=data)
    print(f"  ✅ 族群熱度 更新完成（{len(results)} 個族群有近期活動）")



def _calc_analysis_rows(ss, date_str, current_prices, current_margin, cache_prices=None, fast_mode=False):
    """
    從歷史紀錄計算 all_rows（純計算，不寫 Sheets）。
    選4（對照分析）和選6（明日關注）共用此函式。
    cache_prices: 快取載入的原始 current_prices，batch 不適用時作為保底來源。
    fast_mode: True 時跳過保底補抓 API，只用 current_prices/current_margin 內已有的資料。
               適用於只選明日關注推薦、不需要完整歷史股票資料的情境。
    """
    hist = get_or_create(ss, "歷史紀錄")
    disp = fmt_date(date_str)
    rows = hist.get_all_values()[1:]
    if not rows:
        return []

    all_hist_codes = set(row[2] for row in rows if len(row) > 2 and row[2])

    # ── 現價保底：fast_mode 時跳過，只用快取內已有的資料 ──
    missing_price = [c for c in all_hist_codes if c not in current_prices]
    if missing_price and not fast_mode:
        print(f"  📡 保底補抓現價（{len(missing_price)} 支，快取未涵蓋）...")
        # 第一段：TWSE batch
        batch_map = fetch_price_map_batch(date_str)
        still_missing = []
        for code in missing_price:
            if code in batch_map:
                current_prices[code] = batch_map[code]
            else:
                still_missing.append(code)
        if batch_map:
            print(f"  [batch] 保底命中 {len(missing_price)-len(still_missing)}/{len(missing_price)} 支")
        # 第二段：OTC batch
        if still_missing:
            otc_map = fetch_price_map_otc(date_str)
            remain = []
            for code in still_missing:
                if code in otc_map:
                    current_prices[code] = otc_map[code]
                else:
                    remain.append(code)
            if otc_map:
                print(f"  [otc] 保底命中 {len(still_missing)-len(remain)}/{len(still_missing)} 支")
            still_missing = remain
        # 第三段：batch/OTC 日期不吻合（補跑歷史日期），從快取 current_prices 補
        # ★ v11.3 修正：batch 不適用時改查快取，而非直接跳過
        if still_missing and cache_prices:  # cache_prices 由呼叫端傳入
            from_cache = []
            truly_missing = []
            for code in still_missing:
                if code in cache_prices:
                    current_prices[code] = cache_prices[code]
                    from_cache.append(code)
                else:
                    truly_missing.append(code)
            if from_cache:
                print(f"  [cache] 快取補底命中 {len(from_cache)}/{len(still_missing)} 支")
            still_missing = truly_missing
        # 第四段：興櫃/停牌等真正無資料，跳過
        if still_missing:
            print(f"  [skip] {len(still_missing)} 支 batch/OTC/快取均未命中，跳過（量比 None）")
        print(f"  ✅ 現價保底完成")

    # ── 融資券保底：快取缺少才補抓（fast_mode 時跳過）──
    missing_margin = [c for c in all_hist_codes if c not in current_margin]
    if missing_margin and not fast_mode:
        print(f"  📡 保底補抓融資融券（{len(missing_margin)} 支，快取未涵蓋）...")
        margin_map = _fetch_margin_map(date_str)
        for rc, val in margin_map.items():
            if rc in missing_margin:
                current_margin[rc] = val
        found_m = sum(1 for c in missing_margin if c in current_margin)
        print(f"  ✅ 融資融券保底完成（找到 {found_m}/{len(missing_margin)} 支）")

    buy_map, sell_hist, net_by_date, all_dates = _build_history_maps(rows)

    # ★ v11.6 載入融券歷史，供 build_row 計算趨勢
    short_hist = load_short_history(ss, date_str)

    # ★ v11.9 抓大盤漲跌幅，供 build_row 計算相對強弱
    print("  📡 抓取大盤指數...")
    market_pct = fetch_market_index(date_str)
    if market_pct is not None:
        sign = "+" if market_pct >= 0 else ""
        print(f"  加權指數：{sign}{market_pct}%")
    else:
        print("  ⚠️ 大盤指數取得失敗，相對強弱欄留空")

    all_rows = [
        build_row(s, current_prices, current_margin, sell_hist, disp, all_dates, net_by_date,
                  short_hist=short_hist, market_pct=market_pct)
        for s in buy_map.values()
    ]

    # ★ v10.7 過濾：最近出現日距今超過 5 個交易日的股票不列入
    def _trading_days_diff(disp_a, disp_b):
        """計算兩個 YYYY/MM/DD 之間的交易日天數差（跳週末）"""
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(disp_a, "%Y/%m/%d")
            end = datetime.strptime(disp_b, "%Y/%m/%d")
        except Exception:
            return 999
        count = 0
        step = timedelta(days=1)
        while d < end:
            d += step
            if d.weekday() < 5:
                count += 1
        return count

    all_rows = [
        r for r in all_rows
        if r[-1] and _trading_days_diff(r[-1], disp) <= 5
    ]

    all_rows.sort(key=lambda r: r[18])
    all_rows.sort(key=lambda r: r[-1] if r[-1] else "", reverse=True)
    return all_rows


def update_analysis(ss, date_str, current_prices, current_margin):
    """
    更新「對照分析」與「每日快照」工作表。
    計算部分委由 _calc_analysis_rows()，本函式只負責寫入 Sheets。
    """
    disp     = fmt_date(date_str)
    all_rows = _calc_analysis_rows(ss, date_str, current_prices, current_margin, cache_prices=current_prices)
    if not all_rows:
        print("  ⚠️ 歷史紀錄無資料，跳過對照分析")
        return []

    n_cols = len(ANALYSIS_HEADERS)

    # ── 對照分析（每次覆蓋）──
    ws_ana   = get_or_create(ss, "對照分析", n_cols)
    ana_data = [
        [f"統計截至：{disp}（每次執行自動更新至最新）"] + [""] * (n_cols - 1),
        ANALYSIS_HEADERS
    ] + all_rows
    ws_ana.clear()
    if ws_ana.row_count < len(ana_data) + 5:
        ws_ana.add_rows(len(ana_data) + 5 - ws_ana.row_count)
    ws_ana.update(range_name="A1", values=ana_data)
    print(f"  ✅ 對照分析 更新完成（{len(ana_data)-2} 支，合計累計天數由低至高）")

    # ── 每日快照（prepend 累積）──
    ws_snap    = get_or_create(ss, "每日快照", n_cols)
    from datetime import datetime, timedelta
    _cutoff = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=31)).strftime("%Y/%m/%d")
    purge_old_rows(ws_snap, _cutoff)
    snap_block = [
        [f"統計截至：{disp}"] + [""] * (n_cols - 1),
        ANALYSIS_HEADERS
    ] + all_rows
    prepend_block(ws_snap, snap_block, disp, "統計截至：", n_cols)
    print(f"  ✅ 每日快照 更新完成（最新快照插入最上方）")
    return all_rows


# ═══════════════════════════════════════════════
# ★ v10.6 明日關注推薦
# ═══════════════════════════════════════════════

RECOMMEND_HEADERS = [
    "排名", "代號", "股票名稱", "評分",
    "連續天數", "籌碼集中度%", "籌碼集中度評級",
    "出貨風險", "融資健康度",
    "現價", "當日漲跌%", "自營商標記",
]

PERFORMANCE_HEADERS = [
    "推薦日", "代號", "股票名稱", "推薦評分",
    "推薦收盤", "T+1收盤", "T+2收盤", "T+3收盤",
]


def _score_matrix(consec, chip_lbl):
    """
    連續天數 × 籌碼集中度 矩陣評分（40分）
    天數越長代表法人持續買進，分數越高。
    """
    if chip_lbl == "🔵 高度集中":
        if consec <= 3:   return 26
        elif consec <= 7: return 33
        else:             return 40
    elif chip_lbl == "🟦 中度集中":
        if consec <= 3:   return 15
        elif consec <= 7: return 21
        else:             return 27
    else:  # 偏低
        if consec <= 3:   return 4
        elif consec <= 7: return 7
        else:             return 10


def _score_margin(health):
    """融資健康度評分（25分）"""
    return {"✅ 籌碼乾淨": 25, "🟡 小幅跟進": 15,
            "⚠️ 散戶大量跟進": 5, "🔴 法人不買散戶買": 0}.get(health, 0)


def _score_risk(risk):
    """出貨風險評分（15分）：🔴高風險已在 score_stock 過濾"""
    return {"🟢 低": 15, "🟡 中": 7, "🔴 高": 0}.get(risk, 0)


def _score_volume_ratio(vr):
    """量比評分（7分）：今日量 ÷ 近10日均量"""
    if vr is None or vr == "": return 2   # ★ 無資料給基礎分，不全部歸零
    try:
        vr = float(vr)
    except (ValueError, TypeError):
        return 2
    if vr >= 3.0:   return 7    # 大爆量
    if vr >= 2.0:   return 5    # 明顯放量
    if vr >= 1.5:   return 4    # 溫和放量
    if vr >= 1.0:   return 2    # 正常量
    return 1                    # 縮量


def _score_short_trend(short_trend):
    """
    融券趨勢評分（-8 ~ +8，中性 0）：
    融券回補（↘）→ 空頭退場，加分；融券增加（↗）→ 空頭加碼，扣分。
    最終 score 會在 score_stock 內 clamp 到 [0, 100]。
    """
    s = str(short_trend).strip()
    if not s:
        return 0
    # 連減/連增：取天數判斷強度
    m = re.search(r"連[增減](\d+)天", s)
    days = int(m.group(1)) if m else 0
    if "↘" in s:   # 融券減少 → 利多
        return 8 if days >= 3 else 4
    if "↗" in s:   # 融券增加 → 利空
        return -8 if days >= 2 else -4
    return 0       # 持平或資料不足


def _calc_buy_accel(all_entries, all_dates):
    """
    計算三法人合計買超「加速度」：最近1日買超 vs 前2日平均的比值。
    回傳 (ratio, label)：
      ratio: float（無法計算時 None）
      label: "🚀 加速" / "📈 溫和加速" / "➡ 持平" / "📉 減速" / ""
    """
    if not all_entries or not all_dates:
        return None, ""
    # 從 all_entries 組出每日合計買超（只取買超 > 0 的日期）
    daily = {}
    for date, net, price in all_entries:
        daily[date] = daily.get(date, 0) + net
    # 取最近有買超紀錄的3個交易日（在 all_dates 裡，由新到舊）
    buy_dates = sorted([d for d in daily if daily[d] > 0], reverse=True)
    if len(buy_dates) < 2:
        return None, ""
    recent   = daily[buy_dates[0]]          # 最近1日
    prev_avg = sum(daily[d] for d in buy_dates[1:3]) / min(len(buy_dates) - 1, 2)  # 前1~2日均
    if prev_avg <= 0:
        return None, ""
    ratio = round(recent / prev_avg, 2)
    if ratio >= 1.5:   label = "🚀 加速"
    elif ratio >= 1.2: label = "📈 溫和加速"
    elif ratio >= 0.8: label = "➡ 持平"
    else:              label = "📉 減速"
    return ratio, label


def _score_accel(accel_label):
    """買超加速度評分（0~3分），輸入 _calc_buy_accel 回傳的 label 字串"""
    s = str(accel_label).strip()
    if not s:           return 1   # 資料不足給中性分
    if "🚀" in s:       return 3
    if "📈" in s:       return 2
    if "➡" in s:        return 1
    if "📉" in s:       return 0
    return 1


def _dealer_label(d_consec):
    """自營商連續買超標記"""
    if d_consec >= 5:  return f"🔥 自營{d_consec}天"
    elif d_consec >= 3: return f"⭐ 自營{d_consec}天"
    elif d_consec > 0:  return f"自營{d_consec}天"
    return ""


def score_stock(row):
    """
    輸入 build_row 產出的 row，回傳綜合評分（0~100）。
    row index 對照 ANALYSIS_HEADERS：
      [3]=外資連續, [8]=投信連續, [13]=自營連續
      [19]=現價, [20]=當日漲跌%, [21]=漲幅%, [22]=出貨風險, [23]=訊號
      [24]=買超加速度, [25]=籌碼集中度%, [26]=籌碼集中度評級
      [29]=融資健康度, [30]=量比, [31]=融券趨勢
      [32]=相對強弱%（v11.9）, [33]=最近出現日

    v11.2 過濾邏輯：
      移除「漲幅 ≤2%」門檻（避免錯殺法人剛開始佈局的股票）
      改為「漲幅 >8%」上限（避免追高）
      新增「出貨風險🔴」過濾
    """
    code         = row[0]
    signal       = row[23]
    risk         = row[22]
    accel_label  = row[24] if len(row) > 24 else ""
    chip_lbl     = row[26]
    health       = row[29]
    vr_raw = row[30] if len(row) > 30 else None
    short_trend  = row[31] if len(row) > 31 else ""
    try:
        volume_ratio = float(vr_raw) if (vr_raw is not None and vr_raw != "") else None
    except (ValueError, TypeError):
        volume_ratio = None

    # ── 過濾條件 ──────────────────────────────────────
    _finance_codes = set(SECTOR_MAP.get("金融", []))
    if is_etf_code(code):               return None   # ETF 不推
    if code in _finance_codes:          return None   # 金融股不推（波動小、法人長期持有）
    if "🔴 今日賣超" in str(signal):     return None   # 今日賣超
    if not chip_lbl:                     return None   # 無集中度資料
    if risk == "🔴 高":                  return None   # 出貨風險高，不推

    # 現價上限：> 400 元不推（價格過高，散戶參與度低）
    try:
        close_val = float(str(row[19]).replace(",", ""))
        if close_val > 400:              return None
    except (ValueError, TypeError):
        pass

    # 漲幅上限：當日已漲超過 8% 視為追高，不推
    chg_str = str(row[20]).replace("%", "").replace("+", "").strip()
    try:
        chg = float(chg_str)
        if chg > 8.0:                    return None
    except (ValueError, TypeError):
        pass   # 無法解析漲幅（N/A等）→ 不過濾，讓評分決定

    # 取三法人最大連續天數作為代表
    consec = max(
        int(row[3])  if str(row[3]).isdigit()  else 0,
        int(row[8])  if str(row[8]).isdigit()  else 0,
        int(row[13]) if str(row[13]).isdigit() else 0,
    )
    if consec == 0: return None

    score = (
        _score_matrix(consec, chip_lbl) +      # 40分
        _score_margin(health) +                # 25分
        _score_risk(risk) +                    # 15分
        _score_volume_ratio(volume_ratio) +    # 7分
        _score_accel(accel_label) +            # 3分
        _score_short_trend(short_trend)        # -8 ~ +8分
    )
    return max(0, min(score, 100))


def update_recommendation(ss, date_str, all_rows, cached_futures=""):
    """
    從 all_rows（build_row 產出）計算評分，
    取前5名寫入「明日關注」工作表（prepend 累積）。
    ★ v11.6：區塊頂部加入外資期貨未平倉燈號。
    cached_futures: 快取內的期貨燈號字串，有值就直接用，否則重打 API。
    回傳 futures_line（供 main 存回快取）。
    """
    disp = fmt_date(date_str)

    # ★ v11.6 期貨燈號：快取有值就直接用，否則打 API
    if cached_futures:
        futures_line = cached_futures
        print(f"  {futures_line}（快取）")
    else:
        print("  📡 抓取外資期貨未平倉...")
        futures_info = fetch_futures_two_days(date_str)
        if futures_info:
            net, signal, delta_str = futures_info
            sign = "+" if net >= 0 else ""
            futures_line = f"外資大台指淨部位：{signal} {sign}{net:,} 口　{delta_str}"
        else:
            futures_line = "外資大台指淨部位：⚠️ 資料取得失敗"
        print(f"  {futures_line}")

    # 計算評分，過濾不合格
    scored = []
    for row in all_rows:
        s = score_stock(row)
        if s is None:
            continue
        code     = row[0]
        name     = row[1]
        consec   = max(
            int(row[3])  if str(row[3]).isdigit()  else 0,
            int(row[8])  if str(row[8]).isdigit()  else 0,
            int(row[13]) if str(row[13]).isdigit() else 0,
        )
        d_consec = int(row[13]) if str(row[13]).isdigit() else 0
        chip_pct = row[24]
        chip_lbl = row[25]
        risk     = row[22]
        health   = row[28]
        close    = row[19]
        chg_pct  = row[20]
        dealer   = _dealer_label(d_consec)
        # 出貨風險高時加標記
        risk_disp = f"{risk} ⚠️" if risk == "🔴 高" else risk
        scored.append((s, [code, name, s, consec, chip_pct, chip_lbl,
                           risk_disp, health, close, chg_pct, dealer]))

    # 依評分降冪，取前5
    scored.sort(key=lambda x: x[0], reverse=True)
    top5 = scored[:5]

    n_cols = len(RECOMMEND_HEADERS)
    ws = get_or_create(ss, "明日關注", n_cols)
    from datetime import datetime, timedelta
    _cutoff = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=31)).strftime("%Y/%m/%d")
    purge_old_rows(ws, _cutoff)

    rec_rows = []
    for rank, (score, r) in enumerate(top5, 1):
        rec_rows.append([rank] + r)

    block = [
        [f"資料日期：{disp} ｜ 明日關注推薦（綜合評分前5名）"] + [""] * (n_cols - 1),
        [futures_line] + [""] * (n_cols - 1),
        RECOMMEND_HEADERS,
    ] + rec_rows

    prepend_block(ws, block, disp, "資料日期：", n_cols)
    print(f"  ✅ 明日關注 更新完成（Top5：{', '.join(r[1] for _, r in top5)}）")
    return futures_line


# ═══════════════════════════════════════════════
# ★ v10.7 推薦成效追蹤
# ═══════════════════════════════════════════════

def _parse_rec_sheet(all_vals, disp_today):
    """
    解析「明日關注」工作表，回傳所有 block：
    { disp: [(代號, 名稱, 評分, 推薦收盤), ...] }
    跳過今日 block。
    """
    result = {}
    i = 0
    while i < len(all_vals):
        cell = all_vals[i][0] if all_vals[i] else ""
        if cell.startswith("資料日期：") and disp_today not in cell:
            disp = cell.replace("資料日期：", "").split("｜")[0].strip()
            stocks = []
            i += 1
            if i < len(all_vals) and all_vals[i] and all_vals[i][0] == "排名":
                i += 1
            while i < len(all_vals):
                row = all_vals[i]
                c0  = row[0] if row else ""
                if c0.startswith("─") or c0.startswith("資料日期："):
                    break
                if len(row) >= 4 and row[1] and row[1].strip().isdigit():
                    try:
                        close = float(row[9].strip()) if len(row) > 9 and row[9].strip() else 0.0
                    except ValueError:
                        close = 0.0
                    stocks.append((row[1].strip(), row[2].strip(), row[3].strip(), close))
                i += 1
            if stocks:
                result[disp] = stocks
        else:
            i += 1
    return result


def _n_trading_days_after(base_disp, n):
    """
    回傳 base_disp（YYYY/MM/DD）之後第 n 個交易日的 disp 字串（跳週末）。
    """
    from datetime import datetime, timedelta
    d = datetime.strptime(base_disp, "%Y/%m/%d")
    count = 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d.strftime("%Y/%m/%d")


def _fmt_close(today_close, base_close):
    """今日收盤加上漲跌符號（對比推薦收盤）。"""
    if today_close <= 0:
        return ""
    if base_close <= 0:
        return str(today_close)
    if today_close > base_close:
        return f"▲{today_close}"
    elif today_close < base_close:
        return f"▼{today_close}"
    else:
        return f"－{today_close}"


def _archive_performance(ss, expired_rows):
    """
    將 T+3 追蹤完畢的推薦成效列搬入「推薦歷史」工作表（永久保留）。
    - 已存在（同推薦日+代號）的筆不重複寫入
    - expired_rows：list of list，格式同 PERFORMANCE_HEADERS
    """
    if not expired_rows:
        return

    ws = get_or_create(ss, "推薦歷史", len(PERFORMANCE_HEADERS))

    try:
        existing = ws.get_all_values()
    except Exception:
        existing = []

    # 建立已存在的 (推薦日, 代號) set
    import re as _re
    date_pat = _re.compile(r"^\d{4}/\d{2}/\d{2}$")
    existing_keys = set()
    for row in existing:
        if row and date_pat.match(str(row[0]).strip()) and len(row) >= 2:
            existing_keys.add((row[0].strip(), row[1].strip()))

    new_rows = [
        r for r in expired_rows
        if (str(r[0]).strip(), str(r[1]).strip()) not in existing_keys
    ]

    if not new_rows:
        print(f"  ℹ️ 推薦歷史：{len(expired_rows)} 筆已存在，無需新增")
        return

    # 若工作表是空的，先寫標題列
    if not existing:
        existing = [PERFORMANCE_HEADERS]

    full = existing + new_rows
    ws.clear()
    if ws.row_count < len(full) + 10:
        ws.add_rows(len(full) + 10 - ws.row_count)
    ws.update(range_name="A1", values=full)
    print(f"  ✅ 推薦歷史 新增 {len(new_rows)} 筆（累計 {len(full)-1} 筆）")


def update_performance(ss, date_str, current_prices):
    """
    推薦成效追蹤（v10.7 重寫）：
    1. 從「明日關注」解析各日推薦
    2. 讀取「推薦成效」現有列表（最多保留推薦日距今 ≤ T+3 的筆數）
    3. 新增今日5筆（T+1/T+2/T+3 留空）
    4. 對所有現存筆，若今日是其 T+1/T+2/T+3，填入今日收盤
    5. 移除推薦日距今超過 T+3 的筆
    6. 整表寫回（最新在最上面）
    """
    disp_today = fmt_date(date_str)
    N_COLS     = len(PERFORMANCE_HEADERS)   # 8

    # ── 讀取「明日關注」工作表 ──
    try:
        ws_rec   = ss.worksheet("明日關注")
        rec_vals = ws_rec.get_all_values()
    except Exception:
        print("  ⚠️ 推薦成效：找不到「明日關注」工作表，略過")
        return

    rec_map = _parse_rec_sheet(rec_vals, disp_today)

    # ── 讀取「推薦成效」現有資料 ──
    ws_perf = get_or_create(ss, "推薦成效", N_COLS)
    from datetime import datetime, timedelta
    _cutoff = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=31)).strftime("%Y/%m/%d")
    purge_old_rows(ws_perf, _cutoff)
    try:
        existing = ws_perf.get_all_values()
    except Exception:
        existing = []

    # 解析現有列表（只保留資料行：col[0] 是 YYYY/MM/DD 格式）
    import re
    date_pat = re.compile(r"^\d{4}/\d{2}/\d{2}$")

    rows = []   # list of list，長度 N_COLS，col[0]=推薦日
    for row in existing:
        if row and date_pat.match(str(row[0]).strip()):
            # 補齊欄位至 N_COLS
            r = list(row) + [""] * N_COLS
            rows.append(r[:N_COLS])

    # ── 步驟1：新增今日5筆（若今日推薦不在列表中）──
    today_codes_in_rows = {r[1] for r in rows if r[0] == disp_today}
    today_recs = rec_map.get(disp_today, [])

    # 今日推薦在 rec_map 裡的 key 是 disp_today，
    # 但 _parse_rec_sheet 跳過今日，所以改從「明日關注」直接解析今日 block
    today_stocks = []
    for i, row in enumerate(rec_vals):
        cell = row[0] if row else ""
        if cell.startswith("資料日期：") and disp_today in cell:
            j = i + 1
            if j < len(rec_vals) and rec_vals[j] and rec_vals[j][0] == "排名":
                j += 1
            while j < len(rec_vals):
                r = rec_vals[j]
                c0 = r[0] if r else ""
                if c0.startswith("─") or c0.startswith("資料日期："):
                    break
                if len(r) >= 4 and r[1] and r[1].strip().isdigit():
                    try:
                        close = float(r[9].strip()) if len(r) > 9 and r[9].strip() else 0.0
                    except ValueError:
                        close = 0.0
                    today_stocks.append((r[1].strip(), r[2].strip(), r[3].strip(), close))
                j += 1
            break

    new_rows = []
    for code, name, score, base_close in today_stocks:
        if code not in today_codes_in_rows:
            new_rows.append([disp_today, code, name, score,
                             base_close if base_close > 0 else "",
                             "", "", ""])

    rows = new_rows + rows

    # ── 步驟2：填入今日收盤到 T+1 / T+2 / T+3 欄 ──
    # col index: 推薦日[0] 代號[1] 名稱[2] 評分[3]
    #            推薦收盤[4] T+1[5] T+2[6] T+3[7]
    for r in rows:
        rec_disp   = r[0]
        code       = r[1]
        base_close_raw = r[4]
        try:
            base_close = float(str(base_close_raw)) if base_close_raw != "" else 0.0
        except ValueError:
            base_close = 0.0

        _, today_close, _, _, _ = current_prices.get(code, (0.0, 0.0, 0, "N/A", None))

        for slot, col_idx in [(1, 5), (2, 6), (3, 7)]:
            if _n_trading_days_after(rec_disp, slot) == disp_today:
                if r[col_idx] == "":   # 尚未填入才寫
                    r[col_idx] = _fmt_close(today_close, base_close)
                break

    # ── 步驟3：T+3 已填完的筆搬入「推薦歷史」，再從追蹤清單移除 ──
    from datetime import datetime
    today_dt = datetime.strptime(disp_today, "%Y/%m/%d")
    def _is_expired(rec_disp):
        try:
            t3 = datetime.strptime(_n_trading_days_after(rec_disp, 3), "%Y/%m/%d")
            return t3 < today_dt
        except Exception:
            return False

    expired = [r for r in rows if _is_expired(r[0])]
    rows    = [r for r in rows if not _is_expired(r[0])]

    # T+3 填完的筆搬入歷史工作表
    if expired:
        _archive_performance(ss, expired)

    # ── 步驟4：整表寫回 ──
    header_row = [PERFORMANCE_HEADERS]
    full_data  = header_row + rows

    ws_perf.clear()
    if ws_perf.row_count < len(full_data) + 10:
        ws_perf.add_rows(len(full_data) + 10 - ws_perf.row_count)
    ws_perf.update(range_name="A1", values=full_data)
    archived_count = len(expired)
    print(f"  ✅ 推薦成效 更新完成（追蹤中：{len(rows)} 筆，本次封存：{archived_count} 筆）")


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
        # ★ v10.8 官方產業別 → 族群名稱對照（避免歸入無意義的「股票」）
        INDUSTRY_TO_SECTOR = {
            "水泥工業":       "傳產",
            "食品工業":       "傳產",
            "塑膠工業":       "傳產",
            "紡織纖維":       "傳產",
            "電機機械":       "傳產",
            "電器電纜":       "傳產",
            "化學工業":       "傳產",
            "玻璃陶瓷":       "傳產",
            "造紙工業":       "傳產",
            "鋼鐵工業":       "傳產",
            "橡膠工業":       "傳產",
            "汽車工業":       "傳產",
            "建材營造":        "建設營造",
            "建材營造業":      "建設營造",
            "航運業":          "航運",
            "觀光餐旅":        "其他",
            "觀光餐旅業":      "其他",
            "金融保險":        "金融",
            "金融保險業":      "金融",
            "貿易百貨":        "貿易百貨",
            "貿易百貨業":      "貿易百貨",
            "綜合":           "其他",
            "其他":           "其他",
            "半導體業":       "半導體",
            "電腦及週邊設備業": "電子代工",
            "光電業":         "其他",
            "通信網路業":     "其他",
            "電子零組件業":   "其他",
            "電子通路業":     "電子代工",
            "資訊服務業":     "其他",
            "其他電子業":     "其他",
            "生技醫療業":     "其他",
            "文化創意業":     "其他",
            "農業科技業":     "其他",
            "電子商務":       "其他",
            "綠能環保":       "其他",
            "數位雲端":       "其他",
            "運動休閒":       "其他",
            "居家生活":       "其他",
        }
        SKIP_INDUSTRIES = {"股票", "上市認購(售)權證", "上市ETF", "上市ETN",
                           "上市受益證券", "上市資產支持證券", "特別股"}

        for i, row in enumerate(rows):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if not cells:
                continue
            # 股票列格式：[代號\u3000名稱, ISIN, 上市日, 市場別, 產業別, CFICode, 備註]
            # cells[0] 含 \u3000，cells[3] 是產業別
            if len(cells) >= 5 and "\u3000" in cells[0]:
                parts = cells[0].split("\u3000")
                code  = parts[0].strip()
                if not (code.isdigit() and len(code) <= 6):
                    continue
                raw_industry = cells[4].strip() if len(cells) > 4 and cells[4].strip() else ""
                if raw_industry in SKIP_INDUSTRIES:
                    continue
                sector = INDUSTRY_TO_SECTOR.get(raw_industry, raw_industry)
                industry_map[code] = sector
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
        # ★ v11.7 過濾：ETF/槓桿反向/受益憑證略過不補入族群
        # is_etf_code 涵蓋：含字母、00 開頭（如 0052、00919）、長度 > 4
        if not code.isdigit() or len(code) > 4 or is_etf_code(code):
            continue
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


def update_sector_sheet(ss, date_str, all_buy_codes, all_buy_names, current_prices, sheet_only=False, cache_ref=None):
    """
    ★ v10 優化：族群成員行情優先從 current_prices 快取取得，
    只對快取中沒有的股票才呼叫 API，大幅減少重複請求。
    sheet_only=True 時完全不打 API，快取沒有的股票留空。

    current_prices: {code: (avg, close, volume, change_pct)}
    """
    disp      = fmt_date(date_str)
    ws        = get_or_create(ss, "族群聯動", 8)
    from datetime import datetime, timedelta
    _cutoff = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=31)).strftime("%Y/%m/%d")
    purge_old_rows(ws, _cutoff)
    # 找出買超榜中不在 SECTOR_MAP 的新股票（ETF/受益憑證排除，sheet-only 時跳過 API 查詢）
    unknown_codes = {c for c in all_buy_codes
                     if c not in CODE_TO_SECTOR and c.isdigit() and len(c) <= 4
                     and not is_etf_code(c)}
    if unknown_codes and not sheet_only:
        print(f"  發現 {len(unknown_codes)} 支新股票不在族群表：{sorted(unknown_codes)}")
        print("  正在查詢官方產業別...")
        industry_map = fetch_industry_map()
        newly_added  = auto_update_sector_map(unknown_codes, all_buy_names, industry_map)
        if newly_added:
            # 重建反查表讓本次執行即時生效
            CODE_TO_SECTOR.update({c: s for c, s in newly_added.items()})
    elif unknown_codes and sheet_only:
        print(f"  ⚠️ {len(unknown_codes)} 支新股票不在族群表（sheet-only 模式，略過 API 查詢）")
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
    missing = [c for c in all_member_codes if c not in current_prices]

    if missing and not sheet_only:
        print(f"  補抓族群成員行情（{len(missing)} 支不在快取中）...")
        # ★ v11.0 三段式：TWSE batch → OTC batch → 逐支補抓
        batch_map = fetch_price_map_batch(date_str)
        still_missing = []
        for code in missing:
            if code in batch_map:
                current_prices[code] = batch_map[code]
            else:
                still_missing.append(code)
        if batch_map:
            print(f"  [batch] 族群補抓命中 {len(missing)-len(still_missing)}/{len(missing)} 支")
        if still_missing:
            otc_map = fetch_price_map_otc(date_str)
            remain = []
            for code in still_missing:
                if code in otc_map:
                    avg, close, vol, chg, vr = otc_map[code]
                    if close > 0:
                        current_prices[code] = (avg, close, vol, chg, vr)
                else:
                    remain.append(code)
            if otc_map:
                otc_hits = len(still_missing) - len(remain)
                print(f"  [otc] 族群補抓命中 {otc_hits}/{len(still_missing)} 支")
            still_missing = remain
        if still_missing:
            print(f"  [skip] {len(still_missing)} 支 batch/OTC 均未命中，跳過")
        # 族群成員行情不存快取（避免超過 50000 字元限制）
    elif missing and sheet_only:
        print(f"  ℹ️ {len(missing)} 支族群成員不在快取中，行情留空（sheet-only 模式）")

    def get_quote(code):
        """從 current_prices 取得行情（補抓結果已合併）"""
        if code in current_prices:
            _, close, vol, chg, *_ = current_prices[code]
        else:
            close, vol, chg = 0.0, 0, "N/A"
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
            # ★ v11.3 過濾：只顯示在買超榜，或收盤價在 50~130 的成員
            if not in_buy and not (close and 50 <= close <= 130):
                continue
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
    """
    ★ v11.7：16:30 前自動使用前一交易日（三大法人資料 16:30 後才釋出）；
    16:30 後先嘗試今天，沒資料再往前找。執行時間顯示於 Step 1。
    """
    warm_up_cookie()
    now = datetime.now()
    d = now
    print(f"  執行時間：{now.strftime('%Y/%m/%d %H:%M')}")
    if now.hour < 16 or (now.hour == 16 and now.minute < 30):
        print(f"  16:30 前自動使用前一交易日（三大法人資料 16:30 後才釋出）")
        d -= timedelta(days=1)
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
# 雲端快取（Google Sheets「快取」工作表）
# ═══════════════════════════════════════════════

def save_cache(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell,
               current_prices=None, current_margin=None, sector_prices=None):
    """
    把所有資料序列化後寫入 Sheets「快取」工作表。
    ★ v10.8 修正：每個 key 獨立一列，避免單 cell 超過 50000 字元限制。
    ★ v11.3 修正：current_prices（買超/賣超榜）和 sector_prices（族群成員）分格存，
                  避免合併後超過 50000 字元上限。
    ★ v11.5 修正：foreign/trust/dealer/f_sell/t_sell/d_sell 各切成 _0/_1 兩塊（25支/塊），
                  避免單格超過 50000 字元上限。
    """
    def _split_rows(key, lst, chunk=25):
        """將 list 切成 chunk 大小的塊，產生 [key_0, ...], [key_1, ...] 等列"""
        result = []
        for i in range(0, max(1, len(lst)), chunk):
            result.append([f"{key}_{i // chunk}", json.dumps(lst[i:i+chunk], ensure_ascii=False)])
        return result

    rows = [["date_str", date_str]]
    for key, lst in [("foreign", foreign), ("trust", trust), ("dealer", dealer),
                     ("f_sell", f_sell), ("t_sell", t_sell), ("d_sell", d_sell)]:
        rows.extend(_split_rows(key, lst))
    rows += [
        ["current_prices", json.dumps({k: list(v) for k, v in (current_prices or {}).items()}, ensure_ascii=False)],
        ["sector_prices",  json.dumps({k: list(v) for k, v in (sector_prices  or {}).items()}, ensure_ascii=False)],
        ["current_margin", json.dumps({k: list(v) for k, v in (current_margin or {}).items()}, ensure_ascii=False)],
        ["futures_signal", ""],   # ★ v11.6 預留，由 update_recommendation 寫入
    ]
    ws = get_or_create(ss, "快取", cols=2)
    ws.clear()
    if ws.row_count < len(rows) + 5:
        ws.add_rows(len(rows) + 5 - ws.row_count)
    ws.update(range_name="A1", values=rows)
    print(f"  💾 快取已寫入 Sheets（日期：{date_str}，共 {len(rows)} 列）")

def load_cache(ss, date_str):
    """
    從 Sheets「快取」工作表讀取。
    ★ v10.8：支援新格式（每 key 一列）與舊格式（單 payload 列）。
    ★ v11.5：支援切塊格式（foreign_0/foreign_1 等），自動合併；
             同時相容舊的單格格式（foreign）。
    """
    def _load_chunks(kv, key):
        """讀取 key_0, key_1, ... 並合併；若不存在則 fallback 讀 key"""
        if f"{key}_0" in kv:
            result = []
            i = 0
            while f"{key}_{i}" in kv:
                result.extend(json.loads(kv[f"{key}_{i}"]))
                i += 1
            return result
        elif key in kv:
            return json.loads(kv[key])
        return []

    try:
        ws = ss.worksheet("快取")
        all_rows = ws.get_all_values()
        if not all_rows:
            return None

        # 建立 key→value 查表
        kv = {row[0]: row[1] for row in all_rows if len(row) >= 2}
        cached_date = kv.get("date_str", "")
        if cached_date:
            print(f"  ℹ️ 快取日期：{cached_date}")

        # 新格式：切塊或單格（各 key 獨立一列）
        if "foreign_0" in kv or "foreign" in kv:
            foreign  = _load_chunks(kv, "foreign")
            trust    = _load_chunks(kv, "trust")
            dealer   = _load_chunks(kv, "dealer")
            f_sell   = _load_chunks(kv, "f_sell")
            t_sell   = _load_chunks(kv, "t_sell")
            d_sell   = _load_chunks(kv, "d_sell")
            current_prices = {k: tuple(v) for k, v in json.loads(kv.get("current_prices", "{}")).items()}
            # sector_prices 合併進 current_prices（族群成員保底，榜上已有的不覆蓋）
            _sector = {k: tuple(v) for k, v in json.loads(kv.get("sector_prices", "{}")).items()}
            for _k, _v in _sector.items():
                if _k not in current_prices:
                    current_prices[_k] = _v
            current_margin = {k: tuple(v) for k, v in json.loads(kv.get("current_margin", "{}")).items()}
            futures_signal = kv.get("futures_signal", "")   # ★ v11.6
        # 舊格式相容：payload 在 B2
        elif "payload" in kv:
            data = json.loads(kv["payload"])
            foreign  = data["foreign"];  trust   = data["trust"];  dealer = data["dealer"]
            f_sell   = data["f_sell"];   t_sell  = data["t_sell"]; d_sell = data["d_sell"]
            current_prices = {k: tuple(v) for k, v in data.get("current_prices", {}).items()}
            current_margin = {k: tuple(v) for k, v in data.get("current_margin", {}).items()}
            futures_signal = ""
        else:
            print("  ⚠️ 快取格式無法辨識")
            return None

        print(f"  📋 {cached_date} 快取讀取成功")
        return (cached_date, foreign, trust, dealer, f_sell, t_sell, d_sell,
                current_prices, current_margin, futures_signal)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  ⚠️ 快取工作表不存在，需重新抓取")
        return None
    except Exception as e:
        print(f"  ⚠️ 快取讀取失敗：{e}")
        return None

def prefetch_history_codes(ss, date_str, current_prices, current_margin):
    """
    從歷史紀錄收集所有出現過的股票代號，
    補抓 current_prices 和 current_margin 裡缺少的部分，
    讓快取完整，sheet-only 模式不需要再打 API。
    """
    hist = get_or_create(ss, "歷史紀錄")
    rows = hist.get_all_values()[1:]
    all_hist_codes = set(row[2] for row in rows if len(row) > 2 and row[2])

    # 補抓現價
    missing = [c for c in all_hist_codes if c not in current_prices]
    if missing:
        print(f"  📡 補抓歷史股票現價（{len(missing)} 支）...")
        # 三段式：TWSE batch → OTC batch → 逐支
        batch_map = fetch_price_map_batch(date_str)
        still_missing = []
        for code in missing:
            if code in batch_map:
                current_prices[code] = batch_map[code]
            else:
                still_missing.append(code)
        if batch_map:
            print(f"  [batch] 命中 {len(missing)-len(still_missing)}/{len(missing)} 支")
        if still_missing:
            otc_map = fetch_price_map_otc(date_str)
            remain = []
            for code in still_missing:
                if code in otc_map:
                    current_prices[code] = otc_map[code]
                else:
                    remain.append(code)
            if otc_map:
                print(f"  [otc] 命中 {len(still_missing)-len(remain)}/{len(still_missing)} 支")
            still_missing = remain
        if still_missing:
            print(f"  [skip] {len(still_missing)} 支 batch/OTC 均未命中，跳過")
        print(f"  ✅ 現價補抓完成")

    # 補抓融資融券
    missing_margin = [c for c in all_hist_codes if c not in current_margin]
    if missing_margin:
        print(f"  📡 補抓歷史股票融資融券（{len(missing_margin)} 支）...")
        margin_map = _fetch_margin_map(date_str)
        for rc, val in margin_map.items():
            if rc in missing_margin:
                current_margin[rc] = val
        found_m = sum(1 for c in missing_margin if c in current_margin)
        print(f"  ✅ 融資融券補抓完成（找到 {found_m}/{len(missing_margin)} 支）")

def debug_margin(date_str):
    """往前找最近有資料的融資融券日期，並印出原始欄位"""
    d = datetime.strptime(prev_trading_date(date_str), "%Y%m%d")
    for attempt in range(7):
        margin_date = d.strftime("%Y%m%d")
        print(f"\n🔍 嘗試融資融券日期：{margin_date}")
        url = (f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
               f"?date={margin_date}&selectType=ALL&response=json")
        text = curl_get(url)
        if not text or text.startswith("<"):
            print("  ❌ API 無回應")
        else:
            try:
                data = json.loads(text)
                stat   = data.get("stat")
                tables = data.get("tables", [])
                print(f"  stat: {stat}，tables 數量: {len(tables)}")
                if stat == "OK" and len(tables) >= 2:
                    rows = tables[1].get("data", [])
                    print(f"  ✅ 找到個股資料！筆數: {len(rows)}")
                    if rows:
                        print(f"  fields: {tables[1].get('fields')}")
                        print(f"  第1筆: {rows[0]}")
                        for target in ["2330", "2317", "2382"]:
                            found = [r for r in rows if str(r[0]).strip() == target]
                            print(f"  {target}: {found[0] if found else '找不到'}")
                    return
                else:
                    print(f"  → 無個股資料")
            except Exception as e:
                print(f"  ❌ 解析失敗：{e}")
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    print("⚠️ 往前 7 天都找不到融資融券資料")

# ═══════════════════════════════════════════════
# ═══════════════════════════════════════════════
# 主程式
# ═══════════════════════════════════════════════

def main():
    import argparse
    global CODE_TO_SECTOR
    CODE_TO_SECTOR = _build_code_to_sector()

    parser = argparse.ArgumentParser(description="台灣股市三大法人買超/賣超追蹤")
    parser.add_argument("--fetch-only",   action="store_true", help="只抓資料，存雲端快取，不寫 Sheets")
    parser.add_argument("--sheet-only",   action="store_true", help="讀雲端快取，不打 API，直接寫 Sheets")
    parser.add_argument("--debug-margin", action="store_true", help="印出融資融券 API 原始欄位")
    args = parser.parse_args()

    print("=" * 50)
    print(f"  台灣股市三大法人買超/賣超追蹤 {VERSION}")
    print("  config.py 獨立設定 ｜ 族群聯動累積 ｜ 分模式執行")
    print("=" * 50)
    print(f"  族群反查表：{len(CODE_TO_SECTOR)} 支股票已對應族群")

    if not os.path.exists(CREDENTIALS_FILE) and not args.debug_margin:
        print(f"\n❌ 找不到 credentials.json")
        input("按 Enter 關閉..."); sys.exit(1)

    # ── debug-margin 模式 ──
    if args.debug_margin:
        warm_up_cookie()
        d = datetime.now()
        for _ in range(7):
            if d.weekday() < 5:
                debug_margin(d.strftime("%Y%m%d"))
                break
            d -= timedelta(days=1)
        input("按 Enter 關閉...")
        return

    # ── 先連上 Sheets ──
    print("\n🔌 連接 Google Sheets...")
    try:
        ss = connect_sheets()
        print("  ✅ 連接成功")
    except Exception as e:
        print(f"\n❌ 連接失敗：{e}")
        input("按 Enter 關閉..."); sys.exit(1)

    # ── 找今日交易日字串（用來比對快取） ──
    warm_up_cookie()
    d = datetime.now()
    for _ in range(7):
        if d.weekday() < 5:
            today_str = d.strftime("%Y%m%d")
            break
        d -= timedelta(days=1)

    # ── 取得資料 ──
    if args.sheet_only:
        print("\n📂 sheet-only 模式：讀取雲端快取...")
        result = load_cache(ss, today_str)
        if not result:
            print("❌ 快取不存在或日期不符，請先執行完整模式或只抓資料")
            input("按 Enter 關閉..."); sys.exit(1)
        date_str, foreign, trust, dealer, f_sell, t_sell, d_sell, current_prices, current_margin, _cached_futures = result
        buy_groups = [foreign, trust, dealer]
        all_buy_codes = set()
        all_buy_names = {}
        for group in buy_groups:
            for stock in group:
                all_buy_codes.add(stock["code"])
                all_buy_names[stock["code"]] = stock["name"]
        # ★ sheet-only 也需要 _CACHE_REF，供族群聯動補抓後重存快取
        _CACHE_REF = {
            "foreign": foreign, "trust": trust, "dealer": dealer,
            "f_sell":  f_sell,  "t_sell": t_sell, "d_sell": d_sell,
        }
    else:
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

        all_groups = [foreign, trust, dealer, f_sell, t_sell, d_sell]
        buy_groups = [foreign, trust, dealer]

        # Step 1.5: 快取命中則跳過 Step 2/3，直接用快取的 prices/margin
        _cache_result = load_cache(ss, date_str)
        if _cache_result and _cache_result[0] == date_str:
            _, _cf, _ct, _cd, _cfs, _cts, _cds, current_prices, current_margin, _cached_futures = _cache_result
            sell_price_map = current_prices
            # 將快取 stock 欄位回填給 group 物件（供 Sheets 寫入用）
            for live_group, cached_group in zip(all_groups, [_cf,_ct,_cd,_cfs,_cts,_cds]):
                cached_by_code = {s["code"]: s for s in cached_group}
                for stock in live_group:
                    cached = cached_by_code.get(stock["code"], {})
                    for key in ("avg_price","close","volume","change_pct","volume_ratio",
                                "margin_balance","margin_change","short_balance"):
                        if key in cached:
                            stock[key] = cached[key]
            print("  ✅ 快取命中，跳過 Step 2/3 API 抓取")
        else:
            _cached_futures = ""   # ★ v11.6 完整執行時由 update_recommendation 填入並存快取
            # Step 2: 抓個股價格
            print(f"\n💹 Step 2/5 抓取個股價格與成交量...")
            try:
                # 先抓全市場 batch，賣超和買超共用，不重打 API
                sell_price_map = {}
                sell_price_map.update(fetch_price_map_batch(date_str))
                sell_price_map.update(fetch_price_map_otc(date_str))
                # 買超：完整三段式（含量比），直接傳入已抓好的 batch，不重打
                enrich_with_prices([foreign, trust, dealer], date_str, prefetched_map=sell_price_map)
                # 賣超：只需收盤價，從 batch 直接填，量比 None
                for group in [f_sell, t_sell, d_sell]:
                    for stock in group:
                        code = stock["code"]
                        if code in sell_price_map:
                            avg, close, vol, chg, _ = sell_price_map[code]
                            stock["avg_price"] = avg
                            stock["close"] = close
                            stock["volume"] = vol
                            stock["change_pct"] = chg
                            stock["volume_ratio"] = None
            except Exception as e:
                print(f"  ⚠️ 價格抓取部分失敗：{e}")

            # Step 3: 抓融資融券
            print(f"\n📋 Step 3/5 抓取融資融券資料...")
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
                        stock.get("avg_price",     0.0),
                        stock.get("close",         0.0),
                        stock.get("volume",        0),
                        stock.get("change_pct",    "N/A"),
                        stock.get("volume_ratio",  None),
                    )
            for group in buy_groups:
                for stock in group:
                    current_margin[stock["code"]] = (
                        stock.get("margin_balance", 0),
                        stock.get("margin_change",  0),
                        stock.get("short_balance",  0),
                    )

            # ★ v11.3 修正：current_prices（買超/賣超榜）和 sector_prices（族群成員）分開存
            #   避免合併後超過 Google Sheets 單格 50000 字元上限
            # ★ v11.5 修正：sector_prices 只存族群成員（不存全市場，否則 ~7000支超限）
            all_sector_codes = {c for members in SECTOR_MAP.values() for c in members}
            sector_prices = {}
            for code in all_sector_codes:
                if code not in current_prices and code in sell_price_map:
                    sector_prices[code] = sell_price_map[code]

            # 存快取（買超/賣超榜存 current_prices，族群成員存 sector_prices，分格避免超限）
            save_cache(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell,
                       current_prices, current_margin, sector_prices=sector_prices)

        all_buy_codes = set()
        all_buy_names = {}
        for group in buy_groups:
            for stock in group:
                all_buy_codes.add(stock["code"])
                all_buy_names[stock["code"]] = stock["name"]

        # sell_price_map 補入 current_prices（族群聯動等榜外股票用）
        # 快取命中時 sell_price_map = current_prices（僅 300 支），需重新抓全市場
        if sell_price_map is current_prices:
            _full_map = {}
            _full_map.update(fetch_price_map_batch(date_str))
            _full_map.update(fetch_price_map_otc(date_str))
            sell_price_map = _full_map
        for code, val in sell_price_map.items():
            if code not in current_prices:
                current_prices[code] = val

    # ★ 供 update_sector_sheet 補抓後重存快取用
    _CACHE_REF = {
        "foreign": foreign, "trust": trust, "dealer": dealer,
        "f_sell":  f_sell,  "t_sell": t_sell, "d_sell": d_sell,
    }

    disp = fmt_date(date_str)

    # ── fetch-only：存完就結束 ──
    if args.fetch_only:
        print(f"\n✅ fetch-only 完成，快取已儲存至 Sheets（含價格與融資券）。")
        print(f"   執行 --sheet-only 可直接寫入 Sheets，不重打 API。")
        input("按 Enter 關閉...")
        return

    # ── 選擇要寫入的工作表 ──
    _analysis_rows = []   # 暫存 update_analysis 回傳的 all_rows
    _cached_futures = ""  # ★ v11.6 期貨燈號快取

    def _run_analysis():
        nonlocal _analysis_rows
        _analysis_rows = update_analysis(ss, date_str, current_prices, current_margin)

    def _run_recommendation():
        nonlocal _analysis_rows, _cached_futures
        if not _analysis_rows:
            print("  ℹ️ 自動計算分析資料（不更新對照分析工作表）...")
            _analysis_rows = _calc_analysis_rows(ss, date_str, current_prices, current_margin,
                                                  cache_prices=current_prices, fast_mode=True)
        if not _analysis_rows:
            print("  ⚠️ 歷史紀錄無資料，無法計算推薦")
            return
        result_line = update_recommendation(ss, date_str, _analysis_rows, cached_futures=_cached_futures)
        # ★ v11.6 把期貨燈號存回快取（下次 sheet-only 不重打 API）
        if result_line and result_line != _cached_futures:
            _cached_futures = result_line
            try:
                ws_cache = ss.worksheet("快取")
                all_vals = ws_cache.get_all_values()
                for i, row in enumerate(all_vals):
                    if row and row[0] == "futures_signal":
                        ws_cache.update_cell(i + 1, 2, result_line)
                        break
                else:
                    ws_cache.append_row(["futures_signal", result_line])
            except Exception as e:
                print(f"  ⚠️ 期貨快取存回失敗：{e}")

    SHEET_OPTIONS = [
        ("1", "今日買超排行",  lambda: update_buy_sheet(ss, date_str, foreign, trust, dealer)),
        ("2", "今日賣超排行",  lambda: update_sell_sheet(ss, date_str, f_sell, t_sell, d_sell)),
        ("3", "歷史紀錄",      lambda: append_history(ss, date_str, foreign, trust, dealer, f_sell, t_sell, d_sell)),
        ("4", "對照分析+快照", _run_analysis),
        ("5", "族群聯動",      lambda: update_sector_sheet(ss, date_str, all_buy_codes, all_buy_names, current_prices, sheet_only=args.sheet_only, cache_ref=_CACHE_REF)),
        ("6", "明日關注推薦",  _run_recommendation),
        ("7", "推薦成效追蹤",  lambda: update_performance(ss, date_str, current_prices)),
        ("8", "融券歷史",      lambda: update_short_history(ss, date_str, current_margin)),
        ("9", "族群熱度排行",   lambda: update_sector_heatmap(ss, date_str)),
    ]

    if args.sheet_only:
        print()
        print("┌──────────────────────────────────────┐")
        print("│  選擇要寫入的工作表（可複選）          │")
        print("│  輸入編號，以逗號分隔，Enter 全選      │")
        print("├──────────────────────────────────────┤")
        for key, name, _ in SHEET_OPTIONS:
            print(f"│  {key}) {name:<30}│")
        print("└─────────────────────────────────────┘")
        choice = input("請輸入（例如 1,3,4）：").strip()
        if not choice:
            selected_keys = {o[0] for o in SHEET_OPTIONS}
        else:
            selected_keys = {k.strip() for k in choice.split(",")}
    else:
        selected_keys = {o[0] for o in SHEET_OPTIONS}

    def write_with_retry(name, fn, max_retries=3):
        """寫入 Sheets，遇到 429 自動等待後重試"""
        for attempt in range(1, max_retries + 1):
            try:
                fn()
                time.sleep(5)   # 每張工作表寫完後固定等 5 秒（v10.8 調高避免 429）
                return True
            except Exception as e:
                err = str(e)
                if "429" in err and attempt < max_retries:
                    wait = 30 * attempt
                    print(f"  ⏳ 429 Quota，等待 {wait} 秒後重試（{attempt}/{max_retries}）...")
                    time.sleep(wait)
                else:
                    print(f"\n❌ 寫入失敗（{name}）：{e}")
                    return False
        return False

    # Step 4: 寫入 Sheets
    print("\n📊 Step 4/5 寫入 Google Sheets...")
    for key, name, fn in SHEET_OPTIONS:
        if key not in selected_keys:
            continue
        if not write_with_retry(name, fn):
            ans = input("繼續其他工作表？(Y/n): ").strip().lower()
            if ans == "n":
                input("按 Enter 關閉..."); sys.exit(1)

    print(f"\n🎉 完成！")
    print(f"  https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
    input("按 Enter 關閉...")


if __name__ == "__main__":
    main()
