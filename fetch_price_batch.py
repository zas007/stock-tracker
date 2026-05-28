"""
fetch_price_batch.py — 概念驗證
用 STOCK_DAY_ALL 一次抓全市場當日行情，
建立 code → (avg, close, volume, change_pct) 查表，
取代逐支打 STOCK_DAY 的做法。

使用方式（獨立測試）：
    python3 fetch_price_batch.py 20260527
"""

import json, sys, subprocess, time
from datetime import datetime, timedelta

# ── 沿用主程式的 curl_get ──
def curl_get(url, retries=3, wait=10):
    for attempt in range(retries):
        result = subprocess.run(
            ["curl", "-s", "--max-time", "30", url,
             "-H", "User-Agent: Mozilla/5.0",
             "-H", "Referer: https://www.twse.com.tw/zh/"],
            capture_output=True
        )
        text = result.stdout.decode("utf-8", errors="replace").strip()
        if text and not text.startswith("<"):
            return text
        if attempt < retries - 1:
            print(f"  retry {attempt+1}...")
            time.sleep(wait)
    return None


def fetch_price_map_batch(date_str):
    """
    一次抓全市場當日行情，回傳：
    { code: (avg_price, close, volume_lots, change_pct) }

    API: STOCK_DAY_ALL
    欄位（預期）: 證券代號, 證券名稱, 成交股數, 成交金額, 開盤價,
                  最高價, 最低價, 收盤價, 漲跌(+/-), 漲跌價差, 本益比
    """
    url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"
           f"?date={date_str}&response=json")
    print(f"  抓取全市場行情（STOCK_DAY_ALL {date_str}）...")
    text = curl_get(url)
    if not text:
        print("  ❌ API 無回應")
        return {}

    try:
        data = json.loads(text)
    except Exception as e:
        print(f"  ❌ JSON 解析失敗：{e}")
        return {}

    if data.get("stat") != "OK":
        print(f"  ❌ stat={data.get('stat')}")
        return {}

    rows = data.get("data", [])
    fields = data.get("fields", [])
    print(f"  ✅ 取得 {len(rows)} 支，欄位：{fields}")

    price_map = {}
    for row in rows:
        try:
            code   = str(row[0]).strip()
            shares = float(str(row[2]).replace(",", ""))
            amount = float(str(row[3]).replace(",", ""))
            close  = float(str(row[7]).replace(",", ""))
            # row[8] = 漲跌價差，含正負號，如 "+5.00" 或 "-3.00" 或 "0.00"
            diff_str = str(row[8]).replace(",", "").strip()
            diff = float(diff_str) if diff_str not in ("", "--") else 0.0
            vol    = int(shares / 1000)
            avg    = round(amount / shares, 2) if shares > 0 else 0.0
            # 還原前日收盤，計算漲跌幅
            prev_close = close - diff
            pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
            pct_str = f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"
            price_map[code] = (avg, close, vol, pct_str)
        except Exception:
            continue

    print(f"  ✅ 解析完成，{len(price_map)} 支有效")
    return price_map


def benchmark(date_str, sample_codes):
    """
    比較兩種方式的時間：
    A) STOCK_DAY_ALL 一次抓 → 查表
    B) 逐支打 STOCK_DAY（模擬）
    """
    print(f"\n=== Benchmark date={date_str}, {len(sample_codes)} 支 ===\n")

    # A: batch
    t0 = time.time()
    price_map = fetch_price_map_batch(date_str)
    t_batch = time.time() - t0
    hits = sum(1 for c in sample_codes if c in price_map)
    print(f"\n[A] STOCK_DAY_ALL: {t_batch:.1f}s，命中 {hits}/{len(sample_codes)} 支")

    # B: 估算逐支時間（不實際打，避免 rate limit）
    t_per_call = 1.0   # 保守估計含 sleep 每支約 1 秒
    t_serial = len(sample_codes) * t_per_call
    print(f"[B] 逐支 STOCK_DAY（估算）: ~{t_serial:.0f}s（每支約 {t_per_call}s）")
    print(f"\n速度提升：{t_serial / t_batch:.0f}x（若 batch 成功）")

    # 印出幾筆確認資料正確
    print("\n--- 抽查資料 ---")
    for code in sample_codes[:5]:
        if code in price_map:
            avg, close, vol, pct = price_map[code]
            print(f"  {code}: 均價={avg} 收盤={close} 量={vol:,}張 漲跌={pct}")
        else:
            print(f"  {code}: 不在結果中")

    return price_map


if __name__ == "__main__":
    date_str = sys.argv[1] if len(sys.argv) > 1 else "20260526"

    # 用 T86 買超榜前50的常見代號來測試命中率
    sample_codes = [
        "2330", "2317", "2303", "2881", "2882", "2886", "2891",
        "3711", "2344", "2356", "6669", "3231", "2603", "2618",
        "3037", "8046", "6269", "2382", "4966", "3491",
        "6230", "3017", "2059", "6510", "6223", "6515",
        "3260", "5347", "2337", "2409", "2880", "2887",
        "1303", "6770", "2883", "3481", "2324", "2449",
        "2892", "2801", "1216", "2313", "4958", "3706",
        "2312", "3006", "3042", "2327", "6409", "5483",
    ]

    price_map = benchmark(date_str, sample_codes)
