#!/usr/bin/env bash
# 一眼看家用主機上的盯盤狀況：bash deploy/status.sh
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "🕒 系統時間：$(date '+%Y-%m-%d %H:%M:%S %Z')｜時區：$(timedatectl show -p Timezone --value 2>/dev/null || echo '?')"
echo
echo "── Telegram 指令 bot（systemd）──"
if command -v systemctl >/dev/null; then
  systemctl is-active stock-bot 2>/dev/null | sed 's/^/狀態：/'
  journalctl -u stock-bot --no-pager -n 5 2>/dev/null || true
else
  echo "（沒有 systemctl）"
fi
echo
echo "── 盯盤程式 ──"
if [[ -f "$DIR/intraday_monitor.lock" ]] && kill -0 "$(cat "$DIR/intraday_monitor.lock")" 2>/dev/null; then
  echo "狀態：✅ 執行中（PID $(cat "$DIR/intraday_monitor.lock")）"
else
  echo "狀態：目前沒有在跑（非盤中屬正常）"
fi
echo "crontab：$(crontab -l 2>/dev/null | grep -F intraday_monitor.py || echo '⚠️ 沒有盯盤排程')"
echo
echo "── 盯盤 log（最後 8 行）──"
tail -n 8 "$DIR/intraday_log.txt" 2>/dev/null || echo "（還沒有 log）"
echo
echo "── 今日狀態檔 ──"
cat "$DIR/intraday_state.json" 2>/dev/null | head -c 600 || echo "（沒有）"; echo
