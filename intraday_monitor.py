#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台灣股市盤中盯盤 — intraday_monitor.py
版本：v1.1（對應主程式 v11.57）

用途：
  拿前一晚「明日關注」的推薦清單（主榜 + 觀察組 + v2限定候選）當盯盤清單，
  盤中每分鐘查一次證交所即時報價，到點就用 Telegram 通知：
    🟢 進入買進區間   🎯 達目標價   🛑 觸及停損
    🔥 盤中爆量（累計量 ≥ 10日均量 × N 倍）   🚀 突破近20日收盤高點

  為什麼不是「盤中重新選股」：三大法人資料要收盤後才公布，盤中拿不到當天法人買賣，
  所以盤中只能盯「昨晚已經選好的清單」的價量。目標價/停損價是主程式 v11.56 在
  「明日關注」算好的（見 fetch_and_update.py 的 _target_stop_info），這裡直接讀。

執行方式：
  python3 intraday_monitor.py                 # 持續執行：等到 09:00 開盤 → 每分鐘輪詢 → 13:30 收盤後發摘要並結束
  python3 intraday_monitor.py --once          # 只查一輪就結束（不管現在幾點，收盤後會用最後成交價）
  python3 intraday_monitor.py --once --dry-run   # 只查一輪、只印在螢幕、不發通知、不記錄已通知狀態（測試用）
  python3 intraday_monitor.py --list          # 只列出今日盯盤清單（含目標價/停損價）
  python3 intraday_monitor.py --debug-quote 2330   # 印出某檔即時報價的 API 原始內容（欄位核對用）
  python3 intraday_monitor.py --setup-telegram     # 互動式設定 Telegram（寫入 telegram.json）
  python3 intraday_monitor.py --test-notify        # 發一則測試通知

設定：
  - Google Sheets 沿用同資料夾 config.py 與 credentials.json（只「讀」，不寫 Sheets，不會撞 429）
  - Telegram 金鑰放 telegram.json（{"bot_token": "...", "chat_id": "..."}）或環境變數
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。telegram.json 含金鑰，請加進 .gitignore，不要推 git。
  - 參數（輪詢間隔、爆量倍數…）可在 config.py 用 INTRADAY_* 覆寫，沒設就用下面預設值。

v1.1 新增（搭配 tg_bot.py 的 Telegram 指令）：
  - 每輪查價前讀 bot_control.json：/mute 靜音（只擋盤中事件通知；盤前清單、收盤摘要、系統警告照發）、
    /add 臨時加入的股票（只有當天有效，沒有買進區間，所以只會通知達目標/觸停損/爆量/突破）
  - 每輪把「最後查價時間」寫進 intraday_state.json，讓 bot 的 /status 能回報盯盤程式是否在跑
  - 控制檔由 bot 負責寫、盯盤程式只讀；狀態檔由盯盤程式負責寫、bot 只讀，兩邊不會互相覆蓋

時間一律以台北時間（UTC+8）判斷，不受系統時區影響。
"""

import os, sys, json, time, argparse, subprocess, statistics
from datetime import datetime, timedelta, timezone

VERSION = "v1.1"
HERE = os.path.dirname(os.path.abspath(__file__))
TW = timezone(timedelta(hours=8))

# ── 載入 config（缺項用預設值，舊版 config.py 不會壞）──────────────
try:
    sys.path.insert(0, HERE)
    import config as _cfg
except ImportError:
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


SPREADSHEET_ID = _c("SPREADSHEET_ID", "")
_cf = _c("CREDENTIALS_FILE", "")
CREDENTIALS_FILE = _cf if _cf else os.path.join(HERE, "credentials.json")
HOLIDAYS = set(_c("HOLIDAYS", set())) | set(_c("TEMP_CLOSURES", set()))

POLL_SECONDS      = _c("INTRADAY_POLL_SECONDS", 60)     # 輪詢間隔（秒）。證交所即時報價約 5 秒更新，不建議低於 30
VOL_SURGE_RATIO   = _c("INTRADAY_VOL_RATIO", 1.5)       # 盤中累計量 ≥ 近10日均量 × 此倍數 → 爆量
BREAKOUT_BUF_PCT  = _c("INTRADAY_BREAKOUT_BUFFER_PCT", 0.0)  # 突破需高過前高多少 %
REARM_PCT         = _c("INTRADAY_REARM_PCT", 1.0)       # 買進區間通知後，股價回到區間上緣 +N% 以上才重新武裝
MAX_BUY_ALERTS    = _c("INTRADAY_MAX_BUY_ALERTS", 2)    # 同一檔一天最多通知幾次「進入買進區間」
VOL_AVG_DAYS      = _c("INTRADAY_VOL_AVG_DAYS", 10)
HIGH_LOOKBACK     = _c("TARGET_HIGH_LOOKBACK", 20)
FALLBACK_GAIN_PCT = _c("TARGET_GAIN_DEFAULT_PCT", 3.0)  # 「明日關注」沒有目標價欄時的估算用
FALLBACK_STOP_PCT = _c("STOP_BELOW_PCT", 3.0)

OPEN_HM  = (9, 0)
CLOSE_HM = (13, 30)

COOKIE_FILE   = "/tmp/twse_cookie_mis.txt"
STATE_FILE    = os.path.join(HERE, "intraday_state.json")
LOCK_FILE     = os.path.join(HERE, "intraday_monitor.lock")
TELEGRAM_FILE = os.path.join(HERE, "telegram.json")
LOG_FILE      = os.path.join(HERE, "intraday_log.txt")
CONTROL_FILE  = os.path.join(HERE, "bot_control.json")   # ★ v1.1 由 tg_bot.py 寫入（靜音/臨時加入）

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ═══════════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════════

def now_tw():
    return datetime.now(TW)


def log(msg):
    print(f"[{now_tw().strftime('%H:%M:%S')}] {msg}", flush=True)


def _num(v, positive=True):
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if positive and f <= 0:
        return None
    return f


def fp(x):
    """價格格式：最多 2 位小數、去尾端 0、千分位。"""
    if x is None or x == "":
        return "-"
    s = f"{float(x):,.2f}".rstrip("0").rstrip(".")
    return s


def pct(a, b):
    """(a / b − 1) × 100，b 無效回傳 None。"""
    if a is None or not b:
        return None
    return (a / b - 1) * 100


def fpct(p, digits=1):
    return "-" if p is None else f"{p:+.{digits}f}%"


def _curl(args, stdin_text=None, timeout=20):
    cmd = ["curl", "-s", "--max-time", str(timeout)] + args
    r = subprocess.run(cmd, capture_output=True, text=True, input=stdin_text)
    return r.stdout.strip()


# ═══════════════════════════════════════════════════════════════
# Telegram
# ═══════════════════════════════════════════════════════════════

def load_telegram():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if (not token or not chat) and os.path.exists(TELEGRAM_FILE):
        try:
            with open(TELEGRAM_FILE, encoding="utf-8") as f:
                d = json.load(f)
            token = token or str(d.get("bot_token", "")).strip()
            chat = chat or str(d.get("chat_id", "")).strip()
        except Exception as e:
            log(f"⚠️ 讀取 telegram.json 失敗：{e}")
    return (token, chat) if token and chat else (None, None)


def telegram_send(text, creds):
    """發送純文字訊息（超過 4000 字自動切段）。成功回傳 True。token 走 stdin，不出現在 ps。"""
    token, chat = creds
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    ok_all = True
    for ch in chunks:
        out = _curl(
            ["--config", "-", "-X", "POST",
             "--data-urlencode", f"chat_id={chat}",
             "--data-urlencode", f"text={ch.strip()}",
             "--data-urlencode", "disable_web_page_preview=true"],
            stdin_text=f'url = "https://api.telegram.org/bot{token}/sendMessage"\n', timeout=15)
        try:
            ok = bool(json.loads(out).get("ok"))
        except Exception:
            ok = False
        if not ok:
            log(f"⚠️ Telegram 發送失敗：{out[:200] or '（無回應）'}")
            ok_all = False
    return ok_all


class Notifier:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.creds = None if dry_run else load_telegram()
        self.muted = False      # ★ v1.1 由主迴圈每輪依 bot_control.json 設定
        self.state = None       # ★ v1.1 供記錄「靜音期間略過幾則」
        if not dry_run and not self.creds:
            log("⚠️ 找不到 Telegram 設定（telegram.json），這次只印在螢幕。"
                "執行 --setup-telegram 設定。")

    def send(self, text, force=False):
        """force=True（盤前清單、收盤摘要、系統警告）不受靜音影響。"""
        if self.muted and not force:
            title = text.split("\n")[0]
            if self.state is not None:
                fl = self.state["flags"]
                fl["suppressed"] = fl.get("suppressed", 0) + 1
                lst = fl.setdefault("suppressed_titles", [])
                if len(lst) < 50:
                    lst.append(f"{now_tw().strftime('%H:%M')} {title}")
            log(f"🔕 靜音中，略過通知：{title}")
            return False
        print("─" * 40 + "\n" + text + "\n" + "─" * 40, flush=True)
        if self.dry_run or not self.creds:
            return False
        return telegram_send(text, self.creds)


def setup_telegram():
    print("Telegram 通知設定")
    print("  1. 在 Telegram 搜尋 @BotFather，傳 /newbot，照指示取得 bot token")
    print("  2. 在 Telegram 找到你剛建立的 bot，按 Start 並隨便傳一句話給它")
    token = input("請貼上 bot token：").strip()
    if not token:
        print("已取消")
        return
    out = _curl(["--config", "-"], stdin_text=f'url = "https://api.telegram.org/bot{token}/getUpdates"\n')
    chat_id = ""
    try:
        d = json.loads(out)
        if not d.get("ok"):
            print(f"❌ token 無效或 Telegram 回應異常：{out[:200]}")
            return
        for u in reversed(d.get("result", [])):
            msg = u.get("message") or u.get("channel_post") or {}
            if msg.get("chat", {}).get("id"):
                chat_id = str(msg["chat"]["id"])
                break
    except Exception as e:
        print(f"❌ 解析失敗：{e}（回應：{out[:200]}）")
        return
    if not chat_id:
        print("❌ 找不到對話。請先到 Telegram 對你的 bot 按 Start 並傳一句話，再重跑一次。")
        return
    with open(TELEGRAM_FILE, "w", encoding="utf-8") as f:
        json.dump({"bot_token": token, "chat_id": chat_id}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(TELEGRAM_FILE, 0o600)
    except Exception:
        pass
    print(f"✅ 已寫入 {TELEGRAM_FILE}（chat_id={chat_id}）")
    gi = os.path.join(HERE, ".gitignore")
    try:
        txt = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
        if "telegram.json" not in txt:
            print("⚠️ .gitignore 還沒有 telegram.json，請加上這一行，避免把金鑰推上 git：telegram.json")
    except Exception:
        pass
    ok = telegram_send("✅ 盯盤通知設定完成（測試訊息）", (token, chat_id))
    print("✅ 測試訊息已送出，請到 Telegram 確認" if ok else "❌ 測試訊息發送失敗")


# ═══════════════════════════════════════════════════════════════
# Google Sheets（只讀）
# ═══════════════════════════════════════════════════════════════

def connect_sheets():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly"])
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def load_watchlist(ss):
    """
    讀「明日關注」最上面（最新）那個區塊。回傳 (區塊日期, [watch dict, ...])。
    欄位位置從區塊內的表頭列（第一欄＝「排名」）動態對照，不寫死欄位順序；
    舊區塊沒有「目標價/停損價」欄時，用買進區間低點 × 預設比例估算並標記 est=True。
    """
    vals = ss.worksheet("明日關注").get_all_values()
    start = next((i for i, r in enumerate(vals) if r and str(r[0]).startswith("資料日期：")), None)
    if start is None:
        return "", []
    block_date = str(vals[start][0]).replace("資料日期：", "").split("｜")[0].strip()
    colmap, group, out = {}, "主榜", []
    for r in vals[start + 1:]:
        c0 = str(r[0]).strip() if r else ""
        if c0.startswith("資料日期："):
            break
        if c0 == "排名":
            colmap = {str(n).strip(): i for i, n in enumerate(r) if str(n).strip()}
            continue
        if "高風險觀察組" in c0:
            group = "觀察組"
            continue
        if "v2評分限定候選" in c0:
            group = "v2限定候選"
            continue
        if c0.startswith("──") or c0.startswith("─────"):
            continue
        if not colmap:
            continue

        def cell(name):
            i = colmap.get(name)
            return str(r[i]).strip() if i is not None and len(r) > i else ""

        code = cell("代號")
        if not code.isdigit():
            continue
        close = _num(cell("現價"))
        buy_low, buy_high = _num(cell("買進區間低")), _num(cell("買進區間高"))
        target, stop, rr = _num(cell("目標價")), _num(cell("停損價")), _num(cell("風報比"))
        est = False
        entry = buy_low or close
        if target is None and entry:
            target, est = entry * (1 + FALLBACK_GAIN_PCT / 100), True
        if stop is None and entry:
            stop, est = entry * (1 - FALLBACK_STOP_PCT / 100), True
        out.append({
            "code": code, "name": cell("股票名稱"), "group": group,
            "score": cell("評分"), "close": close,
            "buy_low": buy_low, "buy_high": buy_high,
            "target": round(target, 2) if target else None,
            "stop": round(stop, 2) if stop else None,
            "rr": rr, "est": est, "buy_label": cell("推薦買進價位"),
        })
    return block_date, out


def load_series(ss, sheet, codes, today_disp):
    """讀「收盤價歷史」/「成交量歷史」，回傳 {code: [(日期, 值), ...]}（升序、不含今天）。"""
    try:
        rows = ss.worksheet(sheet).get_all_values()[1:]
    except Exception as e:
        log(f"⚠️ 讀取「{sheet}」失敗：{e}")
        return {}
    out = {}
    for r in rows:
        if len(r) < 3 or r[1] not in codes or r[0] >= today_disp:
            continue
        v = _num(r[2])
        if v is not None:
            out.setdefault(r[1], []).append((r[0], v))
    for k in out:
        out[k].sort(key=lambda x: x[0])
    return out


def build_context(ss, watch, today_disp):
    codes = {w["code"] for w in watch}
    vol_hist = load_series(ss, "成交量歷史", codes, today_disp)
    px_hist = load_series(ss, "收盤價歷史", codes, today_disp)
    avg_vol, high_n = {}, {}
    for c in codes:
        vs = [v for _, v in vol_hist.get(c, [])][-VOL_AVG_DAYS:]
        if len(vs) >= 3:
            avg_vol[c] = statistics.mean(vs)
        ps = [v for _, v in px_hist.get(c, [])][-HIGH_LOOKBACK:]
        if len(ps) >= 5:
            high_n[c] = max(ps)
    return avg_vol, high_n


# ═══════════════════════════════════════════════════════════════
# 即時報價（證交所 MIS，約 5 秒更新；上市 tse_／上櫃 otc_）
# ═══════════════════════════════════════════════════════════════

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"


def _mis_session():
    _curl(["-c", COOKIE_FILE, "-b", COOKIE_FILE, "-H", f"User-Agent: {UA}",
           "https://mis.twse.com.tw/stock/index.jsp"])


def mis_raw(codes, ex_map):
    chans = []
    for c in codes:
        if c in ex_map:
            chans.append(f"{ex_map[c]}_{c}.tw")
        else:
            chans += [f"tse_{c}.tw", f"otc_{c}.tw"]
    url = f"{MIS_URL}?ex_ch={'|'.join(chans)}&json=1&delay=0&_={int(time.time() * 1000)}"
    txt = _curl(["-c", COOKIE_FILE, "-b", COOKIE_FILE,
                 "-H", f"User-Agent: {UA}",
                 "-H", "Referer: https://mis.twse.com.tw/stock/index.jsp",
                 url])
    return json.loads(txt)


def _first_px(s):
    for part in str(s or "").split("_"):
        v = _num(part)
        if v is not None:
            return v
    return None


def parse_quote(m, allow_prev=False):
    """
    MIS 一筆 msgArray → 標準化 dict；沒有任何可用價格回傳 None。
    allow_prev=True（僅 /add 用）：開盤前連委買賣價都沒有時，退而使用昨收當參考價。
    """
    price, src = _num(m.get("z")), "成交"
    if price is None:   # 還沒有成交（開盤前/試撮）：用最佳買賣價中間值
        b, a = _first_px(m.get("b")), _first_px(m.get("a"))
        if b and a:
            price, src = (a + b) / 2, "委買賣中間"
    if price is None and allow_prev:
        price, src = _num(m.get("y")), "昨收"
    if price is None:
        return None
    vol = _num(m.get("v"))
    return {"code": m.get("c", ""), "name": m.get("n", ""), "ex": m.get("ex", ""),
            "price": price, "src": src, "y": _num(m.get("y")),
            "h": _num(m.get("h")), "l": _num(m.get("l")),
            "v": int(vol) if vol else 0, "t": m.get("t", ""), "d": m.get("d", "")}


def fetch_quotes(codes, ex_map, allow_prev=False):
    data = mis_raw(codes, ex_map)
    out = {}
    for m in data.get("msgArray", []) or []:
        q = parse_quote(m, allow_prev=allow_prev)
        if q and q["code"]:
            out[q["code"]] = q
            if q["ex"] in ("tse", "otc"):
                ex_map[q["code"]] = q["ex"]
    return out


# ═══════════════════════════════════════════════════════════════
# 事件判斷與訊息
# ═══════════════════════════════════════════════════════════════

TITLES = {
    "buy": "🟢 進入買進區間", "target": "🎯 達目標價", "stop": "🛑 觸及停損",
    "surge": "🔥 盤中爆量", "breakout": f"🚀 突破近{HIGH_LOOKBACK}日收盤高點",
}


def _live_rr(w, p):
    t, s = w["target"], w["stop"]
    if t and s and p > s and t > p:
        return round((t - p) / (p - s), 2)
    return None


def fmt_alert(kind, w, q, extra=""):
    p = q["price"]
    chg = pct(p, q["y"])
    hm = (q["t"] or now_tw().strftime("%H:%M:%S"))[:5]
    lines = [f"{TITLES[kind]}｜{w['code']} {w['name']}",
             f"現價 {fp(p)}（{fpct(chg, 2)}）　{hm}"]
    if w["buy_low"] or w["buy_high"]:
        lo, hi = w["buy_low"], w["buy_high"]
        lines.append(f"買進區間 {fp(lo)}~{fp(hi)}" if lo != hi else f"買進價位 {fp(lo)}")
    if w["target"] and w["stop"]:
        rr = _live_rr(w, p)
        lines.append(f"🎯 目標 {fp(w['target'])}（{fpct(pct(w['target'], p))}）　"
                     f"🛑 停損 {fp(w['stop'])}（{fpct(pct(w['stop'], p))}）　"
                     f"風報比 {('>10' if rr > 10 else rr) if rr is not None else '-'}")
    if extra:
        lines.append(extra)
    tail = w["group"] + (f"｜評分 {w['score']}" if w["score"] not in ("", "-") else "")
    if w["est"]:
        tail += "｜目標/停損為預設估算"
    lines.append(tail)
    return "\n".join(lines)


def check_events(w, q, ev, avg_vol, high_n):
    """
    依報價判斷事件，回傳 [(kind, 訊息)]。ev 是這檔股票當日已觸發的狀態（會被就地更新，供去重）。
    停損後不再通知「進入買進區間」；買進區間通知後，要回到區間上緣 +REARM_PCT% 以上才會再武裝。
    """
    out = []
    p = q["price"]
    code = w["code"]

    if w["stop"] and p <= w["stop"] and not ev.get("stop"):
        ev["stop"] = 1
        out.append(("stop", fmt_alert("stop", w, q)))

    if w["target"] and p >= w["target"] and not ev.get("target"):
        ev["target"] = 1
        out.append(("target", fmt_alert("target", w, q)))

    bh = w["buy_high"]
    if bh:
        if ev.get("buy_armed") is False and p > bh * (1 + REARM_PCT / 100):
            ev["buy_armed"] = True
        in_zone = p <= bh and (w["stop"] is None or p > w["stop"])
        if (in_zone and ev.get("buy_armed", True) and not ev.get("stop")
                and ev.get("buy_n", 0) < MAX_BUY_ALERTS):
            ev["buy_n"] = ev.get("buy_n", 0) + 1
            ev["buy_armed"] = False
            out.append(("buy", fmt_alert("buy", w, q)))

    av = avg_vol.get(code)
    if av and q["v"] and q["v"] >= av * VOL_SURGE_RATIO and not ev.get("surge"):
        ev["surge"] = 1
        out.append(("surge", fmt_alert(
            "surge", w, q, f"累計量 {q['v']:,} 張＝{VOL_AVG_DAYS}日均量 {av:,.0f} 張的 {q['v'] / av:.1f} 倍")))

    hn = high_n.get(code)
    if (hn and p > hn * (1 + BREAKOUT_BUF_PCT / 100) and q["y"] and p > q["y"]
            and not ev.get("breakout")):
        ev["breakout"] = 1
        out.append(("breakout", fmt_alert("breakout", w, q, f"近{HIGH_LOOKBACK}日收盤高點 {fp(hn)}")))
    return out


def fmt_preopen(block_date, watch):
    lines = [f"📋 今日盯盤清單（依 {block_date} 推薦，共 {len(watch)} 檔）",
             "目標價為回測統計加價位結構推算的參考值，非預測；停損請自行判斷。"]
    for grp in ("主榜", "觀察組", "v2限定候選", "手動加入"):
        items = [w for w in watch if w["group"] == grp]
        if not items:
            continue
        lines.append(f"\n【{grp}】")
        for i, w in enumerate(items, 1):
            if w["buy_low"] is None and w["buy_high"] is None:
                buy = "無區間"
            elif w["buy_low"] != w["buy_high"]:
                buy = f"{fp(w['buy_low'])}~{fp(w['buy_high'])}"
            else:
                buy = fp(w["buy_low"])
            est = "（估算）" if w["est"] else ""
            score_txt = f"　評分 {w['score']}" if w["score"] not in ("", "-") else ""
            lines.append(f"{i}. {w['code']} {w['name']}{score_txt}")
            lines.append(f"   買進 {buy}｜🎯 {fp(w['target'])}｜🛑 {fp(w['stop'])}｜風報比 {w['rr'] or '-'}{est}")
    return "\n".join(lines)


def fmt_summary(watch, last, state):
    lines = ["📊 盯盤收盤摘要"]
    for w in watch:
        q = last.get(w["code"])
        ev = state["events"].get(w["code"], {})
        fired = [n for k, n in (("buy_n", "進買進區"), ("target", "達目標"), ("stop", "觸停損"),
                                ("surge", "爆量"), ("breakout", "突破前高")) if ev.get(k)]
        if q:
            p = q["price"]
            lines.append(f"{w['code']} {w['name']}　{fp(p)}（{fpct(pct(p, q['y']), 2)}）"
                         f"｜距目標 {fpct(pct(w['target'], p))}／距停損 {fpct(pct(w['stop'], p))}"
                         + (f"｜已觸發：{'、'.join(fired)}" if fired else ""))
        else:
            lines.append(f"{w['code']} {w['name']}　（今日沒有取得報價）")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 狀態檔 / 鎖 / log
# ═══════════════════════════════════════════════════════════════

def load_state(today, dry_run):
    st = {"date": today, "events": {}, "flags": {}}
    if dry_run or not os.path.exists(STATE_FILE):
        return st
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today:
            return d
    except Exception:
        pass
    return st


def save_state(st, dry_run):
    if dry_run:
        return
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        log(f"⚠️ 狀態檔寫入失敗：{e}")


def pid_alive(lock_path=None):
    """★ v1.1 鎖檔裡的 PID 是否還活著（供 bot /status 判斷盯盤程式有沒有在跑）。"""
    path = lock_path or LOCK_FILE
    try:
        pid = int(open(path).read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def acquire_lock(lock_path=None):
    path = lock_path or LOCK_FILE
    if os.path.exists(path):
        try:
            pid = int(open(path).read().strip())
            os.kill(pid, 0)
            return False       # 該 PID 還活著 → 已有另一個實例在跑
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass               # 殘留的舊鎖
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock(lock_path=None):
    path = lock_path or LOCK_FILE
    try:
        if os.path.exists(path) and open(path).read().strip() == str(os.getpid()):
            os.remove(path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# ★ v1.1 控制檔（bot 寫、盯盤程式讀）
# ═══════════════════════════════════════════════════════════════

def load_control():
    """讀 bot_control.json；不存在或壞掉一律當作空設定（不影響盯盤）。"""
    try:
        with open(CONTROL_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_control(d):
    """原子寫入（先寫暫存檔再 os.replace），避免盯盤程式剛好讀到寫一半的檔案。"""
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CONTROL_FILE)


def muted_now(ctl):
    try:
        return now_tw().timestamp() < float(ctl.get("muted_until") or 0)
    except (TypeError, ValueError):
        return False


def extras_to_watch(ctl, today):
    """
    控制檔裡「今天」臨時加入的股票 → (watch 清單, {代號: 均量}, {代號: 近N日高點}, {代號: 上市/上櫃})。
    手動加入沒有買進區間（buy_low/buy_high=None），所以不會觸發「進入買進區間」。
    """
    watch, avg, high, exm = [], {}, {}, {}
    for e in ctl.get("extras", []) or []:
        if str(e.get("date")) != today:
            continue
        code, entry = str(e.get("code", "")), _num(e.get("entry"))
        if not code or entry is None:
            continue
        watch.append({
            "code": code, "name": str(e.get("name", "")), "group": "手動加入", "score": "-",
            "close": entry, "buy_low": None, "buy_high": None,
            "target": _num(e.get("target")), "stop": _num(e.get("stop")),
            "rr": None, "est": bool(e.get("est")), "buy_label": "",
        })
        if _num(e.get("avg_vol")):
            avg[code] = _num(e.get("avg_vol"))
        if _num(e.get("high_n")):
            high[code] = _num(e.get("high_n"))
        if e.get("ex") in ("tse", "otc"):
            exm[code] = e["ex"]
    return watch, avg, high, exm


def effective_watch(base_watch, avg_vol, high_n, today):
    """★ v1.1 清單 = 「明日關注」清單 + 今天手動加入的；回傳 (清單, 均量, 高點, ex_map, 是否靜音)。"""
    ctl = load_control()
    ex_watch, ex_avg, ex_high, ex_ex = extras_to_watch(ctl, today)
    base_codes = {w["code"] for w in base_watch}
    cur = list(base_watch) + [w for w in ex_watch if w["code"] not in base_codes]
    return cur, {**avg_vol, **ex_avg}, {**high_n, **ex_high}, ex_ex, muted_now(ctl)


def trim_log(max_bytes=600_000, keep_lines=3000):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > max_bytes:
            with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-keep_lines:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def at(now, hm):
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def poll_once(watch, ex_map, state, notifier, avg_vol, high_n, last, fails, allow_stale=False):
    """
    查一輪、判斷事件、發通知。回傳 (是否取得資料, 報價日期是否為今天)。
    allow_stale=False（持續模式）時，報價日期不是今天（休市日/開盤前仍是昨日資料）不觸發任何事件；
    --once 測試時 allow_stale=True，收盤後也能用最後成交價看到效果。
    """
    try:
        quotes = fetch_quotes([w["code"] for w in watch], ex_map)
        fails[0] = 0
    except Exception as e:
        fails[0] += 1
        log(f"⚠️ 報價取得失敗（連續 {fails[0]} 次）：{e}")
        if fails[0] == 5 and not state["flags"].get("fail_warned"):
            state["flags"]["fail_warned"] = True
            notifier.send("⚠️ 盯盤：連續 5 次取不到即時報價，請檢查網路或證交所服務。", force=True)
        return False, True
    if not quotes:
        log("⚠️ 這一輪沒有任何報價（可能尚未開盤或休市）")
        return False, True

    today = now_tw().strftime("%Y%m%d")
    qdates = {str(q["d"]).replace("/", "").replace("-", "") for q in quotes.values() if q["d"]}
    same_day = (not qdates) or (today in qdates)

    sent = 0
    for w in watch:
        q = quotes.get(w["code"])
        if not q:
            continue
        last[w["code"]] = q
        if not same_day and not allow_stale:   # 報價不是今天的：只更新 last，不當成盤中事件
            continue
        for kind, text in check_events(w, q, state["events"].setdefault(w["code"], {}), avg_vol, high_n):
            notifier.send(text)
            sent += 1
    state["flags"]["last_poll"] = now_tw().strftime("%H:%M:%S")   # ★ v1.1 心跳，供 bot /status 使用
    log(f"報價 {len(quotes)}/{len(watch)} 檔，本輪事件 {sent} 則" + ("（靜音中，未發送）" if notifier.muted else ""))
    return True, same_day


def run(args):
    if not SPREADSHEET_ID:
        print("❌ 找不到 config.py 的 SPREADSHEET_ID")
        return 1
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"❌ 找不到 credentials.json（{CREDENTIALS_FILE}）")
        return 1

    now = now_tw()
    today, today_disp = now.strftime("%Y%m%d"), now.strftime("%Y/%m/%d")
    continuous = not args.once

    if continuous:
        if now.weekday() >= 5 or today in HOLIDAYS:
            log("今天不是交易日，不啟動盯盤。")
            return 0
        if now >= at(now, CLOSE_HM) + timedelta(minutes=5):
            log("已經收盤，不啟動盯盤。（想測試請加 --once）")
            return 0
        if not args.dry_run and not acquire_lock():
            log("已有另一個盯盤程式在執行（intraday_monitor.lock），這次不重複啟動。")
            return 0

    try:
        log(f"盯盤 {VERSION} 啟動｜{'dry-run（不發通知）' if args.dry_run else '正式'}"
            f"｜{'持續' if continuous else '單次'}")
        ss = connect_sheets()
        block_date, watch = load_watchlist(ss)
        if not watch:
            log("⚠️ 「明日關注」讀不到盯盤清單，結束。")
            return 1
        avg_vol, high_n = build_context(ss, watch, today_disp)
        log(f"清單日期 {block_date}，共 {len(watch)} 檔｜均量 {len(avg_vol)} 檔｜近{HIGH_LOOKBACK}日高點 {len(high_n)} 檔")
        try:     # 清單日期過舊提醒（例如連假後第一天屬正常，超過 5 天才警告）
            age = (now.date() - datetime.strptime(block_date, "%Y/%m/%d").date()).days
            if age > 5:
                log(f"⚠️ 清單日期距今 {age} 天，主程式可能沒有跑完，請確認。")
        except ValueError:
            pass

        notifier = Notifier(args.dry_run)
        state = load_state(today, args.dry_run)
        notifier.state = state
        ex_map, last, fails = {}, {}, [0]

        # 盤前清單（每天只發一次；開盤後 5 分鐘內啟動才發，避免盤中手動執行時重複打擾）
        if (continuous and not state["flags"].get("preopen")
                and now < at(now, OPEN_HM) + timedelta(minutes=5)):
            notifier.send(fmt_preopen(block_date, watch), force=True)
            state["flags"]["preopen"] = True
            save_state(state, args.dry_run)

        if not continuous:
            _mis_session()
            cur_watch, cur_avg, cur_high, ex_ex, muted = effective_watch(watch, avg_vol, high_n, today)
            for k, v in ex_ex.items():
                ex_map.setdefault(k, v)
            notifier.muted = muted
            poll_once(cur_watch, ex_map, state, notifier, cur_avg, cur_high, last, fails, allow_stale=True)
            save_state(state, args.dry_run)
            return 0

        # ── 持續模式：等開盤 → 輪詢 → 收盤摘要 ──
        while now_tw() < at(now_tw(), OPEN_HM):
            time.sleep(min(30, max(1, (at(now_tw(), OPEN_HM) - now_tw()).total_seconds())))
        _mis_session()     # 開盤時重建 session（盤前等待太久 cookie 可能過期）
        checked_day = False
        while True:
            n = now_tw()
            if n >= at(n, CLOSE_HM) + timedelta(seconds=20):
                break
            # ★ v1.1 每輪重讀控制檔：靜音狀態與臨時加入的股票隨時生效
            cur_watch, cur_avg, cur_high, ex_ex, muted = effective_watch(watch, avg_vol, high_n, today)
            for k, v in ex_ex.items():
                ex_map.setdefault(k, v)
            notifier.muted = muted
            got, same_day = poll_once(cur_watch, ex_map, state, notifier, cur_avg, cur_high, last, fails)
            save_state(state, args.dry_run)
            # 休市判斷放在 09:10 之後：剛開盤時 API 可能仍回傳昨日資料，太早判斷會誤判成休市
            if got and not checked_day and n >= at(n, OPEN_HM) + timedelta(minutes=10):
                checked_day = True
                if not same_day:
                    log("報價日期不是今天，判斷今日休市，結束。")
                    return 0
            time.sleep(POLL_SECONDS)

        if not state["flags"].get("summary"):
            cur_watch = effective_watch(watch, avg_vol, high_n, today)[0]
            sup = state["flags"].get("suppressed", 0)
            tail = f"\n\n🔕 今天靜音期間共略過 {sup} 則事件通知（未補發）" if sup else ""
            notifier.send(fmt_summary(cur_watch, last, state) + tail, force=True)
            state["flags"]["summary"] = True
            save_state(state, args.dry_run)
        log("已收盤，盯盤結束。")
        return 0
    except KeyboardInterrupt:
        log("已手動中止。")
        return 0
    finally:
        if continuous and not args.dry_run:
            release_lock()


def main():
    ap = argparse.ArgumentParser(description=f"台灣股市盤中盯盤 {VERSION}")
    ap.add_argument("--once", action="store_true", help="只查一輪就結束（不管現在幾點）")
    ap.add_argument("--dry-run", action="store_true", help="只印在螢幕，不發通知、不記錄已通知狀態")
    ap.add_argument("--list", action="store_true", help="列出今日盯盤清單與目標價後結束")
    ap.add_argument("--debug-quote", metavar="CODE", help="印出某檔即時報價 API 原始內容")
    ap.add_argument("--setup-telegram", action="store_true", help="互動式設定 Telegram 通知")
    ap.add_argument("--test-notify", action="store_true", help="發一則測試通知")
    args = ap.parse_args()

    if args.setup_telegram:
        setup_telegram()
        return 0
    if args.test_notify:
        creds = load_telegram()
        if not creds[0]:
            print("❌ 找不到 Telegram 設定，請先執行 --setup-telegram")
            return 1
        ok = telegram_send(f"🔔 盯盤測試通知 {now_tw().strftime('%Y/%m/%d %H:%M:%S')}（台北時間）", creds)
        print("✅ 已送出" if ok else "❌ 發送失敗")
        return 0 if ok else 1
    if args.debug_quote:
        _mis_session()
        try:
            print(json.dumps(mis_raw([args.debug_quote], {}), ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"❌ {e}")
            return 1
        return 0
    if args.list:
        ss = connect_sheets()
        block_date, watch = load_watchlist(ss)
        if watch:
            watch = effective_watch(watch, {}, {}, now_tw().strftime("%Y%m%d"))[0]
        print(fmt_preopen(block_date, watch) if watch else "（「明日關注」讀不到清單）")
        return 0

    trim_log()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
