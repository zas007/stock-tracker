#!/usr/bin/env bash
# 家用主機（樹莓派 / 舊電腦，Debian、Ubuntu、Raspberry Pi OS）一鍵安裝
# 用法：把整個專案資料夾（含 deploy/）複製到主機後，在專案資料夾執行：  bash deploy/install.sh
# 會做的事：建 Python 虛擬環境並裝套件 → 檢查必要檔案 → 檢查時區 → 安裝 tg_bot 服務 → 詢問後加入盯盤 cron
# 每個會改系統的動作（sudo、時區、crontab）都會先問你。可重複執行。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$DIR/.venv"
ME="$(id -un)"
ask() { read -r -p "$1 [y/N] " a; [[ "${a:-}" =~ ^[Yy]$ ]]; }

echo "📁 專案資料夾：$DIR"
echo "👤 執行身分：$ME"
echo

# 1) 必要檔案
missing=0
for f in intraday_monitor.py tg_bot.py config.py; do
  [[ -f "$DIR/$f" ]] || { echo "❌ 缺少 $f"; missing=1; }
done
[[ -f "$DIR/credentials.json" ]] || { echo "❌ 缺少 credentials.json（要放「唯讀」service account 的金鑰，見 deploy/部署說明.md 第 2 步）"; missing=1; }
[[ -f "$DIR/telegram.json" ]]    || { echo "❌ 缺少 telegram.json（從 Mac 複製過來，或在主機執行 python3 intraday_monitor.py --setup-telegram）"; missing=1; }
[[ $missing -eq 0 ]] || { echo; echo "請補齊檔案後再執行一次。"; exit 1; }
chmod 600 "$DIR/credentials.json" "$DIR/telegram.json"

# 2) curl / python3 / venv
command -v curl >/dev/null    || { echo "❌ 沒有 curl，請先執行：sudo apt install -y curl"; exit 1; }
command -v python3 >/dev/null || { echo "❌ 沒有 python3，請先執行：sudo apt install -y python3"; exit 1; }
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "❌ 缺少 python3-venv，請先執行：sudo apt install -y python3-venv"; exit 1
fi
python3 - <<'PY' || { echo "❌ 需要 Python 3.8 以上"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY

# 3) 虛擬環境與套件
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "🐍 建立虛擬環境 $VENV ..."
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet gspread google-auth
echo "✅ 套件安裝完成（gspread、google-auth）"

# 4) 時區（cron 以系統時區為準；腳本自己判斷台北時間，但 cron 08:45 要對）
TZNOW="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unknown)"
echo "🕒 系統時區：$TZNOW"
if [[ "$TZNOW" != "Asia/Taipei" ]]; then
  if ask "系統時區不是 Asia/Taipei，要用 sudo 改成台北時間嗎？（不改的話，cron 時間要自己換算）"; then
    sudo timedatectl set-timezone Asia/Taipei
    echo "✅ 已改為 Asia/Taipei"
  fi
fi
if command -v timedatectl >/dev/null && [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo yes)" != "yes" ]]; then
  echo "⚠️ 系統時間尚未與網路校時同步（NTP）。時間不準會影響盯盤判斷，可執行：sudo timedatectl set-ntp true"
fi

# 5) 連線測試（只讀 Sheets）
echo "🔎 測試讀取 Google Sheets 與盯盤清單 ..."
if "$VENV/bin/python" "$DIR/intraday_monitor.py" --list >/tmp/stock_install_list.txt 2>&1; then
  head -8 /tmp/stock_install_list.txt
  echo "✅ 讀取成功"
else
  echo "❌ 讀取失敗，輸出如下："; tail -15 /tmp/stock_install_list.txt
  echo "常見原因：①試算表沒有分享給唯讀 service account ②credentials.json 放錯 ③網路"
  exit 1
fi

# 6) systemd：tg_bot 常駐服務
if command -v systemctl >/dev/null; then
  if ask "要安裝並啟動 Telegram 指令 bot 服務（stock-bot，開機自動啟動、掛掉自動重啟，需要 sudo）嗎？"; then
    sed -e "s|@USER@|$ME|g" -e "s|@DIR@|$DIR|g" -e "s|@VENV@|$VENV|g" \
        "$DIR/deploy/stock-bot.service.template" > /tmp/stock-bot.service
    sudo cp /tmp/stock-bot.service /etc/systemd/system/stock-bot.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now stock-bot
    sleep 2
    systemctl --no-pager --lines=5 status stock-bot || true
  fi
else
  echo "⚠️ 找不到 systemctl（非 systemd 系統），bot 請自行用其他方式常駐：$VENV/bin/python -u $DIR/tg_bot.py"
fi

# 7) cron：盯盤每個交易日 08:45 啟動
CRON_LINE="45 8 * * 1-5 cd \"$DIR\" && \"$VENV/bin/python\" -u intraday_monitor.py >> intraday_log.txt 2>&1"
if crontab -l 2>/dev/null | grep -Fq "intraday_monitor.py"; then
  echo "✅ crontab 已經有盯盤排程，不重複加入"
else
  echo "將加入的 cron 行：$CRON_LINE"
  if ask "要把盯盤排程加入你的 crontab 嗎？"; then
    ( crontab -l 2>/dev/null || true; echo "$CRON_LINE" ) | crontab -
    echo "✅ 已加入"
  else
    echo "略過。之後自己執行 crontab -e 貼上上面那一行即可。"
  fi
fi

cat <<MSG

🎉 安裝完成。接下來：
  1. 在 Telegram 對你的 bot 傳 /help、/list，確認有回應
  2. 看狀態：bash deploy/status.sh
  3. 提醒：Mac 上不要再跑 tg_bot.py 或另外排程盯盤，避免重複通知與互搶訊息
MSG
