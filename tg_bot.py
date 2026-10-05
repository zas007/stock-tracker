#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 指令 bot — tg_bot.py
版本：v1.0（對應主程式 v11.57，搭配 intraday_monitor.py v1.1）

用途：
  在 Telegram 對你的 bot 下指令，查今日盯盤清單與目前狀態，或調整盯盤行為。
  這支程式是常駐的（long polling），建議用 systemd 管理（見 deploy/部署說明.md）。
  盯盤本身（intraday_monitor.py）仍由 cron 在 08:45 啟動，兩者獨立運作。

指令：
  /list                    今日盯盤清單（買進區間、目標價、停損價、風報比）
  /status                  現在報價、距目標/停損多少、已觸發事件、盯盤程式是否在跑
  /mute [分鐘]             靜音盤中事件通知（不給分鐘＝靜音到今天收盤）。盤前清單、收盤摘要、系統警告不受影響
  /unmute                  恢復通知
  /add 代號 [目標價] [停損價]   臨時加入盯盤（只有當天有效；沒給目標/停損就用「現價 ± 預設%」估算）
  /del 代號                刪除「手動加入」的股票（不能刪「明日關注」清單裡的）
  /help                    說明

安全：
  - 只回應 telegram.json 裡設定的 chat_id，其他人傳的訊息一律忽略（只寫 log，不回覆）
  - 唯讀：不寫 Google Sheets，只寫本機 bot_control.json（靜音/臨時加入）與 bot_offset.json
  - 沒有任何指令可以下單、改 Sheets 或執行系統指令

執行方式：
  python3 tg_bot.py                    # 常駐，聽 Telegram 指令
  python3 tg_bot.py --cmd "/status"    # 不經 Telegram，在本機直接執行一個指令並印出回覆（測試用）

注意：同一個 bot token 同時只能有一個程式在「聽」（getUpdates）。
  如果 Mac 和家用主機都跑 tg_bot.py，會互搶訊息（Telegram 回 409）。只在家用主機跑 bot。
"""

import os, sys, json, re, time, argparse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intraday_monitor as im

VERSION = "v1.0"
OFFSET_FILE = os.path.join(HERE, "bot_offset.json")
BOT_LOCK    = os.path.join(HERE, "tg_bot.lock")
MAX_EXTRAS  = 10
WATCH_CACHE_SECONDS = 90
CLOSE_MUTE_HM = (13, 35)     # 預設靜音到收盤後 5 分鐘（收盤摘要 13:30 才發，不受靜音影響）

# bot 自己用獨立的 cookie 檔，避免和盯盤程式同時寫同一個檔
im.COOKIE_FILE = "/tmp/twse_cookie_mis_bot.txt"

CODE_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")

HELP = """🤖 盯盤指令（只有你的 chat 可用）
/list　今日盯盤清單與目標價
/status　現在報價、距目標/停損、已觸發事件
/mute [分鐘]　靜音盤中事件通知（不給分鐘＝到今天收盤）
/unmute　恢復通知
/add 代號 [目標價] [停損價]　臨時加入盯盤（只有當天有效）
　例：/add 2330　或　/add 2330 1100 1020
/del 代號　刪除手動加入的股票

說明：
• 靜音只擋「盤中事件通知」；盤前清單、收盤摘要、系統警告照發，靜音期間的事件不會補發
• 手動加入的股票沒有買進區間，所以不會發「進入買進區間」；會通知達目標、觸停損、爆量、突破前高
• 目標價是回測統計加價位結構推算的參考值，非預測，也不構成買賣建議"""

BOT_COMMANDS = [
    {"command": "list",   "description": "今日盯盤清單與目標價"},
    {"command": "status", "description": "現在報價與距目標/停損"},
    {"command": "mute",   "description": "靜音盤中通知 [分鐘]"},
    {"command": "unmute", "description": "恢復通知"},
    {"command": "add",    "description": "臨時加入盯盤 代號 [目標] [停損]"},
    {"command": "del",    "description": "刪除手動加入的股票 代號"},
    {"command": "help",   "description": "指令說明"},
]


# ═══════════════════════════════════════════════════════════════
# Telegram API（token 走 curl stdin，不出現在 ps）
# ═══════════════════════════════════════════════════════════════

def tg_post(method, params, creds, timeout=15):
    token, _ = creds
    args = ["--config", "-", "-X", "POST"]
    for k, v in (params or {}).items():
        args += ["--data-urlencode", f"{k}={v}"]
    out = im._curl(args, stdin_text=f'url = "https://api.telegram.org/bot{token}/{method}"\n', timeout=timeout)
    try:
        return json.loads(out)
    except Exception:
        return None


def get_updates(creds, offset, wait=25):
    return tg_post("getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": '["message"]'},
                   creds, timeout=wait + 15)


def load_offset():
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            return int(json.load(f).get("offset"))
    except Exception:
        return None


def save_offset(off):
    try:
        tmp = OFFSET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offset": off}, f)
        os.replace(tmp, OFFSET_FILE)
    except Exception as e:
        im.log(f"⚠️ offset 寫入失敗：{e}")


# ═══════════════════════════════════════════════════════════════
# 共用：盯盤清單（Sheets 讀取有短暫快取，避免連續下指令打爆 API）
# ═══════════════════════════════════════════════════════════════

_cache = {"t": 0.0, "block_date": "", "watch": []}


def get_base_watch(force=False):
    if not force and time.time() - _cache["t"] < WATCH_CACHE_SECONDS and _cache["watch"]:
        return _cache["block_date"], _cache["watch"]
    ss = im.connect_sheets()
    block_date, watch = im.load_watchlist(ss)
    _cache.update(t=time.time(), block_date=block_date, watch=watch)
    return block_date, watch


def today_str():
    return im.now_tw().strftime("%Y%m%d")


def full_watch():
    """回傳 (清單日期, 完整清單[明日關注+手動加入], extras 的 ex_map)。"""
    block_date, base = get_base_watch()
    today = today_str()
    ctl = im.load_control()
    ex_watch, _a, _h, ex_ex = im.extras_to_watch(ctl, today)
    codes = {w["code"] for w in base}
    return block_date, base + [w for w in ex_watch if w["code"] not in codes], ex_ex


def in_session_day(now=None):
    """今天是交易日且還沒過 13:35（可以靜音/臨時加入）。"""
    now = now or im.now_tw()
    if now.weekday() >= 5 or now.strftime("%Y%m%d") in im.HOLIDAYS:
        return False
    return now < now.replace(hour=CLOSE_MUTE_HM[0], minute=CLOSE_MUTE_HM[1], second=0, microsecond=0)


def in_trading_hours(now=None):
    now = now or im.now_tw()
    if now.weekday() >= 5 or now.strftime("%Y%m%d") in im.HOLIDAYS:
        return False
    return im.at(now, im.OPEN_HM) <= now <= im.at(now, im.CLOSE_HM) + timedelta(minutes=1)


def fired_list(ev):
    names = (("buy_n", "進買進區"), ("target", "達目標"), ("stop", "觸停損"),
             ("surge", "爆量"), ("breakout", "突破前高"))
    return [n for k, n in names if ev.get(k)]


# ═══════════════════════════════════════════════════════════════
# 指令實作（每個回傳要回覆的文字）
# ═══════════════════════════════════════════════════════════════

def cmd_list():
    block_date, watch, _ = full_watch()
    if not watch:
        return "「明日關注」讀不到盯盤清單。"
    return im.fmt_preopen(block_date, watch)


def _zone_text(w, p):
    lo, hi = w["buy_low"], w["buy_high"]
    if lo is None and hi is None:
        return "無買進區間（手動加入）"
    lo = lo or hi
    hi = hi or lo
    if p < lo:
        return f"低於區間 {im.fpct(im.pct(p, lo))}"
    if p <= hi:
        return "✅ 在買進區間內"
    return f"高於區間 {im.fpct(im.pct(p, hi))}"


def cmd_status():
    block_date, watch, ex_ex = full_watch()
    if not watch:
        return "「明日關注」讀不到盯盤清單。"
    now = im.now_tw()
    today = today_str()
    ctl = im.load_control()
    state = im.load_state(today, False)

    # 盯盤程式 / 通知狀態
    alive = im.pid_alive()
    last_poll = state.get("flags", {}).get("last_poll")
    head = [f"📈 盯盤狀態　{now.strftime('%H:%M')}（台北）"]
    if alive:
        head.append(f"盯盤程式：✅ 執行中" + (f"（最後查價 {last_poll}）" if last_poll else "（尚未查價）"))
    elif in_trading_hours(now):
        head.append("盯盤程式：⚠️ 目前沒有在跑！請檢查 cron / 主機（intraday_log.txt）")
    else:
        head.append("盯盤程式：非盤中（沒有在跑屬正常）")
    if im.muted_now(ctl):
        until = datetime.fromtimestamp(float(ctl["muted_until"]), im.TW).strftime("%H:%M")
        sup = state.get("flags", {}).get("suppressed", 0)
        head.append(f"通知：🔕 靜音至 {until}" + (f"（今天已略過 {sup} 則）" if sup else ""))
    else:
        head.append("通知：🔔 開啟")
    if not in_trading_hours(now):
        head.append("（非盤中：顯示最後成交價/收盤價）")

    # 即時報價
    im._mis_session()
    ex_map = dict(ex_ex)
    try:
        quotes = im.fetch_quotes([w["code"] for w in watch], ex_map)
    except Exception as e:
        return "\n".join(head) + f"\n\n⚠️ 取得即時報價失敗：{e}"

    lines = []
    for w in watch:
        q = quotes.get(w["code"])
        tag = "" if w["group"] in ("主榜",) else f"［{w['group']}］"
        if not q:
            lines.append(f"{w['code']} {w['name']}{tag}　（沒有報價）")
            continue
        p = q["price"]
        ev = state.get("events", {}).get(w["code"], {})
        mark = ""
        if w["stop"] and p <= w["stop"]:
            mark = "🛑已低於停損　"
        elif w["target"] and p >= w["target"]:
            mark = "🎯已達目標　"
        fired = fired_list(ev)
        lines.append(
            f"{w['code']} {w['name']}{tag}　{im.fp(p)}（{im.fpct(im.pct(p, q['y']), 2)}）\n"
            f"　{mark}{_zone_text(w, p)}｜距目標 {im.fpct(im.pct(w['target'], p))}／"
            f"距停損 {im.fpct(im.pct(w['stop'], p))}" + (f"\n　已觸發：{'、'.join(fired)}" if fired else ""))
    return "\n".join(head) + "\n\n" + "\n".join(lines)


def cmd_mute(args):
    now = im.now_tw()
    if not in_session_day(now):
        return "現在不是交易日盤中時段（或已收盤），不需要靜音。"
    if args:
        try:
            mins = int(float(args[0]))
        except ValueError:
            return "用法：/mute 或 /mute 30（分鐘）"
        if mins <= 0:
            return "分鐘數要大於 0。"
        mins = min(mins, 600)
        until = now + timedelta(minutes=mins)
    else:
        until = now.replace(hour=CLOSE_MUTE_HM[0], minute=CLOSE_MUTE_HM[1], second=0, microsecond=0)
    ctl = im.load_control()
    ctl["muted_until"] = until.timestamp()
    im.save_control(ctl)
    return (f"🔕 已靜音至 {until.strftime('%H:%M')}。\n"
            "期間的買進區間/目標/停損/爆量/突破通知不會發送，也不會補發（可用 /status 查已觸發事件）。\n"
            "盤前清單、收盤摘要、系統警告仍會送出。/unmute 可隨時恢復。")


def cmd_unmute():
    ctl = im.load_control()
    was = im.muted_now(ctl)
    ctl["muted_until"] = 0
    im.save_control(ctl)
    state = im.load_state(today_str(), False)
    fl = state.get("flags", {})
    sup = fl.get("suppressed", 0)
    msg = "🔔 已恢復通知。" if was else "目前本來就沒有靜音，通知是開啟的。"
    if sup:
        titles = "\n".join("　" + t for t in fl.get("suppressed_titles", [])[-10:])
        msg += f"\n今天靜音期間共略過 {sup} 則事件（未補發）：\n{titles}"
    return msg


def _parse_code(args):
    if not args:
        return None
    c = args[0].strip().upper()
    return c if CODE_RE.match(c) else None


def cmd_add(args):
    code = _parse_code(args)
    if not code:
        return "用法：/add 代號 [目標價] [停損價]　例：/add 2330　或　/add 2330 1100 1020"
    now = im.now_tw()
    if not in_session_day(now):
        return "臨時加入只在「交易日 13:35 前」有效，現在不是盤中時段。請在交易日盤前或盤中再加。"

    target_in = stop_in = None
    if len(args) >= 2:
        target_in = im._num(args[1])
        if target_in is None:
            return "目標價格式不對（要是正數）。用法：/add 代號 [目標價] [停損價]"
    if len(args) >= 3:
        stop_in = im._num(args[2])
        if stop_in is None:
            return "停損價格式不對（要是正數）。用法：/add 代號 [目標價] [停損價]"
    if target_in and stop_in and stop_in >= target_in:
        return "停損價必須低於目標價。"

    _, base = get_base_watch()
    if code in {w["code"] for w in base}:
        return f"{code} 已經在「明日關注」盯盤清單裡了（/list 可查看），不需要再加。"

    ctl = im.load_control()
    today = today_str()
    extras = [e for e in ctl.get("extras", []) if str(e.get("date")) == today]
    if code not in {e.get("code") for e in extras} and len(extras) >= MAX_EXTRAS:
        return f"今天手動加入已達上限 {MAX_EXTRAS} 檔，請先 /del 刪除其他的。"

    # 現價（開盤前沒有成交就退用昨收）
    im._mis_session()
    ex_map = {}
    try:
        quotes = im.fetch_quotes([code], ex_map, allow_prev=True)
    except Exception as e:
        return f"⚠️ 查報價失敗，沒有加入：{e}"
    q = quotes.get(code)
    if not q:
        return f"查不到 {code} 的報價（代號可能打錯，或今天沒有資料），沒有加入。"
    price = q["price"]

    # 爆量/突破需要的均量與近 N 日高點（讀不到就略過這兩種通知）
    avg_vol = high_n = None
    try:
        ss = im.connect_sheets()
        a, h = im.build_context(ss, [{"code": code}], now.strftime("%Y/%m/%d"))
        avg_vol, high_n = a.get(code), h.get(code)
    except Exception as e:
        im.log(f"⚠️ /add {code} 讀均量/高點失敗（略過爆量/突破通知）：{e}")

    est = target_in is None or stop_in is None
    target = target_in or round(price * (1 + im.FALLBACK_GAIN_PCT / 100), 2)
    stop = stop_in or round(price * (1 - im.FALLBACK_STOP_PCT / 100), 2)
    entry = {"code": code, "name": q["name"], "date": today, "entry": price, "target": target,
             "stop": stop, "est": est, "avg_vol": avg_vol, "high_n": high_n,
             "ex": q["ex"] if q["ex"] in ("tse", "otc") else "",
             "added": now.strftime("%H:%M")}
    updated = code in {e.get("code") for e in extras}
    ctl["extras"] = [e for e in extras if e.get("code") != code] + [entry]   # 順便清掉舊日期的
    im.save_control(ctl)

    warn = ""
    if target <= price:
        warn += f"\n⚠️ 目標價 {im.fp(target)} 不高於現價，加入後會立刻觸發「達目標價」。"
    if stop >= price:
        warn += f"\n⚠️ 停損價 {im.fp(stop)} 不低於現價，加入後會立刻觸發「觸及停損」。"
    alive = im.pid_alive()
    run_note = ("" if alive else "\n⚠️ 目前盯盤程式沒有在跑；它啟動後（cron 08:45）才會開始監看這檔。")
    return (f"{'♻️ 已更新' if updated else '✅ 已加入'}盯盤：{code} {q['name']}\n"
            f"參考價 {im.fp(price)}（{q['src']}）\n"
            f"🎯 目標 {im.fp(target)}（{im.fpct(im.pct(target, price))}）　"
            f"🛑 停損 {im.fp(stop)}（{im.fpct(im.pct(stop, price))}）"
            + ("　（預設估算）" if est else "　（自訂）") + "\n"
            f"通知：達目標／觸停損" + ("／爆量" if avg_vol else "") + ("／突破前高" if high_n else "")
            + "。沒有買進區間，所以不發「進入買進區間」。\n只有今天有效，明天自動移除；約 1 分鐘內開始監看。"
            + warn + run_note)


def cmd_del(args):
    code = _parse_code(args)
    if not code:
        return "用法：/del 代號（只能刪除手動加入的股票）"
    ctl = im.load_control()
    extras = ctl.get("extras", []) or []
    keep = [e for e in extras if e.get("code") != code]
    if len(keep) == len(extras):
        return f"{code} 不在「手動加入」清單裡（「明日關注」清單的股票不能用指令刪除）。"
    ctl["extras"] = keep
    im.save_control(ctl)
    return f"🗑 已移除手動加入的 {code}，約 1 分鐘內停止監看。"


def handle_text(text):
    """指令文字 → 回覆文字（None＝不回覆）。"""
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "我只認得指令，輸入 /help 看說明。" if parts else None
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    try:
        if cmd in ("/help", "/start"):
            return HELP
        if cmd == "/list":
            return cmd_list()
        if cmd == "/status":
            return cmd_status()
        if cmd == "/mute":
            return cmd_mute(args)
        if cmd == "/unmute":
            return cmd_unmute()
        if cmd == "/add":
            return cmd_add(args)
        if cmd == "/del":
            return cmd_del(args)
        return "不認得這個指令，輸入 /help 看說明。"
    except Exception as e:
        im.log(f"❌ 指令 {cmd} 執行失敗：{type(e).__name__}: {e}")
        return f"❌ 指令執行失敗：{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════
# 主迴圈
# ═══════════════════════════════════════════════════════════════

def run_bot():
    creds = im.load_telegram()
    if not creds[0]:
        print("❌ 找不到 Telegram 設定（telegram.json），請先執行 intraday_monitor.py --setup-telegram")
        return 1
    if not im.acquire_lock(BOT_LOCK):
        im.log("已有另一個 tg_bot.py 在這台機器上執行，這次不重複啟動。")
        return 0
    try:
        allowed_chat = str(creds[1])
        im.log(f"指令 bot {VERSION} 啟動（只回應 chat_id={allowed_chat}）")
        tg_post("setMyCommands", {"commands": json.dumps(BOT_COMMANDS, ensure_ascii=False)}, creds)

        offset = load_offset()
        if offset is None:     # 第一次啟動：丟掉啟動前累積的舊訊息，避免重播舊指令
            r = tg_post("getUpdates", {"offset": -1, "timeout": 0}, creds)
            res = (r or {}).get("result") or []
            offset = (res[-1]["update_id"] + 1) if res else 0
            save_offset(offset)

        errs = 0
        while True:
            r = get_updates(creds, offset)
            if not r or not r.get("ok"):
                errs += 1
                desc = (r or {}).get("description", "無回應")
                if errs in (1, 5) or errs % 30 == 0:
                    im.log(f"⚠️ getUpdates 失敗（連續 {errs} 次）：{desc}"
                           + ("　← 可能有另一個程式也在用同一個 bot token 聽訊息" if "onflict" in desc else ""))
                time.sleep(min(60, 5 * errs))
                continue
            errs = 0
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                save_offset(offset)
                msg = u.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = msg.get("text", "")
                if chat_id != allowed_chat:
                    im.log(f"🚫 忽略非授權 chat（{chat_id}）的訊息")
                    continue
                if not text:
                    continue
                im.log(f"📩 {text[:60]}")
                reply = handle_text(text)
                if reply:
                    im.telegram_send(reply, creds)
    except KeyboardInterrupt:
        im.log("已手動中止。")
        return 0
    finally:
        im.release_lock(BOT_LOCK)


def main():
    ap = argparse.ArgumentParser(description=f"Telegram 指令 bot {VERSION}")
    ap.add_argument("--cmd", metavar="TEXT", help='直接執行一個指令並印出回覆，例如 --cmd "/status"')
    args = ap.parse_args()
    if args.cmd:
        print(handle_text(args.cmd) or "（無回覆）")
        return 0
    return run_bot()


if __name__ == "__main__":
    sys.exit(main())
