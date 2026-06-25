"""
台灣股市三大法人買超推薦回測腳本 — backtest.py
版本：v1.0（架子版）

用途：
  對歷史推薦重建評分，比對 T+1/T+2/T+3 實際漲跌，
  輸出「回測明細」與「回測勝率矩陣」兩張工作表到 Google Sheets。

執行方式：
  python3 backtest.py              # 跑全部可用歷史
  python3 backtest.py --days 30    # 只跑最近 30 天
  python3 backtest.py --dry-run    # 只印結果，不寫 Sheets

架子狀態（v1.0）：
  ✅ 資料讀取（Sheets 歷史紀錄 + 推薦歷史）
  ✅ 評分特徵重建邏輯（連續天數、籌碼集中度、加速度）
  ✅ 輸出格式（明細 + 勝率矩陣）
  🚧 T+1/T+2/T+3 股價抓取（TODO：資料累積足夠後補上）
  🚧 融資健康度/出貨風險/融券趨勢重建（TODO：需打 MI_MARGN API）
  ⚠️  樣本 < 20 筆時勝率標注「樣本不足」

注意：
  credentials.json 需放在同目錄下（與主程式共用）。
  資料來源為「推薦歷史」工作表（由主程式 _archive_performance 自動寫入）。
"""

import os, sys, json, re, argparse
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

    result = []
    date_pat = re.compile(r"^\d{4}/\d{2}/\d{2}$")
    for row in rows[1:]:
        if not row or not date_pat.match(str(row[0]).strip()):
            continue
        result.append({
            "rec_date":     row[0].strip(),
            "code":         row[1].strip() if len(row) > 1 else "",
            "name":         row[2].strip() if len(row) > 2 else "",
            "score":        _f(row[3])     if len(row) > 3 else None,
            "base_close":   _f(row[4])     if len(row) > 4 else None,
            "t1":           _f(row[5])     if len(row) > 5 else None,
            "t2":           _f(row[6])     if len(row) > 6 else None,
            "t3":           _f(row[7])     if len(row) > 7 else None,
            "t4":           _f(row[8])     if len(row) > 8 else None,
            "t5":           _f(row[9])     if len(row) > 9 else None,
            "group":        row[10].strip() if len(row) > 10 else "",
            "risk":         row[11].strip() if len(row) > 11 else "",   # ★ v11.23
            "margin_health":row[12].strip() if len(row) > 12 else "", # ★ v11.23
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
    從歷史紀錄重建推薦日當天的評分特徵。
    回傳 dict（特徵值），資料不足時用空字串填充（不回傳 None，確保明細完整）。
    """
    code     = rec["code"]
    rec_date = rec["rec_date"]   # YYYY/MM/DD
    entries  = hist_map.get(code, {})

    # ── 連續天數 ──
    all_dates  = sorted(entries.keys())
    net_by_day = {d: entries[d].get("total_net", 0) for d in all_dates}
    consec = 0
    for d in reversed(all_dates):
        if d > rec_date: continue
        if net_by_day.get(d, 0) > 0: consec += 1
        else: break

    # ── 籌碼集中度 ──
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

    # ── 加速度 ──
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
        "chip_pct":      chip_pct_disp,
        "chip_lbl":      chip_lbl,
        "accel_lbl":     accel_lbl,
        "margin_health": rec.get("margin_health", ""),  # ★ v11.23 從推薦歷史直接取
        "risk":          rec.get("risk", ""),            # ★ v11.23 從推薦歷史直接取
        "short_trend":   "TODO",   # 仍待補
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
            rate = round(wins / n * 100, 1) if n else None
            suffix = "" if n >= MIN_SAMPLE else f" ⚠️樣本不足({n})"
            result.append({
                "key":  k, "n": n, "wins": wins,
                "rate": f"{rate}%{suffix}" if rate is not None else "N/A",
                "avg":  avg,
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
        rate = round(wins/n*100, 1) if n else None
        suffix = "" if n >= MIN_SAMPLE else f" ⚠️樣本不足({n})"
        overall.append({
            "key":  label, "n": n, "wins": wins,
            "rate": f"{rate}%{suffix}" if rate is not None else "N/A",
            "avg":  avg,
        })
    sections.append(("【整體 T+1 / T+2 / T+3 / T+4 / T+5 勝率（總覽）】", overall))

    return sections


# ── 輸出到 Sheets ──────────────────────────────────────────────

DETAIL_HEADERS = [
    "推薦日", "推薦星期", "代號", "股票名稱", "推薦評分",
    "連續天數", "籌碼集中度%", "籌碼集中度評級", "買超加速度",
    "推薦收盤",
    "T+1收盤", "T+1漲跌%", "T+2收盤", "T+2漲跌%", "T+3收盤", "T+3漲跌%",
    "T+4收盤", "T+4漲跌%", "T+5收盤", "T+5漲跌%",
    "T+1勝負", "T+2勝負", "T+3勝負", "T+4勝負", "T+5勝負",
    "融資健康度", "出貨風險", "融券趨勢",
]

def _win_label(pnl):
    if pnl is None: return "待補"
    return "✅ 勝" if pnl > 0 else ("➡ 平" if pnl == 0 else "❌ 負")

def write_detail_sheet(ss, detail_rows, dry_run=False):
    now  = datetime.now().strftime("%Y/%m/%d %H:%M")
    n    = len(DETAIL_HEADERS)
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
            r.get("rec_score",""),  r.get("consec",""),
            r.get("chip_pct",""),   r.get("chip_lbl",""),  r.get("accel_lbl",""),
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
        ["切面", "分類", "樣本數", "勝出數", "勝率（T+1）", "平均漲跌幅(%)"],
    ]
    for title, rows in sections:
        data.append([title] + [""]*5)
        for r in rows:
            data.append(["", r["key"], r["n"], r["wins"], r["rate"],
                         r["avg"] if r["avg"] is not None else "N/A"])
        data.append([""]*6)

    if dry_run:
        print(f"  [dry-run] 回測勝率矩陣（前15行預覽）：")
        for row in data[:15]:
            if any(row): print(f"    {row}")
        return

    ws = get_or_create(ss, BACKTEST_SHEET_SUMMARY, 6)
    ws.clear()
    if ws.row_count < len(data) + 5:
        ws.add_rows(len(data) + 5 - ws.row_count)
    ws.update(range_name="A1", values=data)
    print(f"  ✅ 回測勝率 寫入完成（{len(sections)} 個切面）")


# ── 主流程 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="台灣股市推薦回測腳本 v1.0")
    parser.add_argument("--days",    type=int, default=0,
                        help="只回測最近 N 天的推薦（0 = 全部）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只印結果，不寫 Google Sheets")
    args = parser.parse_args()

    print("=" * 50)
    print("  台灣股市推薦回測腳本 v1.0（架子版）")
    print("=" * 50)

    # ── 連線 ──
    if args.dry_run:
        ss = None
        print("\n[dry-run 模式：不連接 Sheets，使用假資料驗證框架]")
        _demo_dry_run()
        return

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

    # ── 讀取資料 ──
    print("\n📂 讀取資料...")
    perf_records = load_perf_history(ss)
    hist_records = load_hist_records(ss)

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
        row = {
            **features,
            "base_close": base,
            "t1": rec.get("t1"), "t1_pnl": calc_pnl(base, rec.get("t1")),
            "t2": rec.get("t2"), "t2_pnl": calc_pnl(base, rec.get("t2")),
            "t3": rec.get("t3"), "t3_pnl": calc_pnl(base, rec.get("t3")),
            "t4": rec.get("t4"), "t4_pnl": calc_pnl(base, rec.get("t4")),
            "t5": rec.get("t5"), "t5_pnl": calc_pnl(base, rec.get("t5")),
            "group": rec.get("group", ""),  # ★ v11.23
        }
        detail_rows.append(row)

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


def _demo_dry_run():
    """dry-run：用假資料驗證完整框架"""
    fake = [
        {"rec_date":"2026/05/20","code":"2330","name":"台積電","rec_score":85,
         "consec":8,"chip_pct":25.3,"chip_lbl":"🔵 高度集中","accel_lbl":"🚀 加速",
         "base_close":950.0,
         "t1":960.0,"t1_pnl":1.05, "t2":945.0,"t2_pnl":-0.53,
         "t3":970.0,"t3_pnl":2.11, "t4":975.0,"t4_pnl":2.63,
         "t5":980.0,"t5_pnl":3.16,
         "margin_health":"TODO","risk":"TODO","short_trend":"TODO"},
        {"rec_date":"2026/05/20","code":"6669","name":"緯穎","rec_score":72,
         "consec":4,"chip_pct":15.1,"chip_lbl":"🟦 中度集中","accel_lbl":"📈 溫和加速",
         "base_close":2500.0,
         "t1":2480.0,"t1_pnl":-0.80, "t2":2530.0,"t2_pnl":1.20,
         "t3":2550.0,"t3_pnl":2.00,  "t4":2540.0,"t4_pnl":1.60,
         "t5":2560.0,"t5_pnl":2.40,
         "margin_health":"TODO","risk":"TODO","short_trend":"TODO"},
        {"rec_date":"2026/05/21","code":"3037","name":"欣興","rec_score":65,
         "consec":3,"chip_pct":11.2,"chip_lbl":"🟦 中度集中","accel_lbl":"➡ 持平",
         "base_close":180.0,
         "t1":None,"t1_pnl":None, "t2":None,"t2_pnl":None,
         "t3":None,"t3_pnl":None, "t4":None,"t4_pnl":None,
         "t5":None,"t5_pnl":None,
         "margin_health":"TODO","risk":"TODO","short_trend":"TODO"},
    ]
    sections = calc_win_rate_matrix(fake)
    write_detail_sheet(None, fake, dry_run=True)
    write_summary_sheet(None, sections, dry_run=True)
    print("\n✅ 框架驗證完成")
    print("   T+N 欄位標注「待補」為正常狀態，等資料累積後補實作 fetch_close_price。")


if __name__ == "__main__":
    main()
