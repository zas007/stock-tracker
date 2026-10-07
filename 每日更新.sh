#!/bin/bash
# 台灣股市三大法人買超追蹤 — Mac 執行腳本
# 雙擊此檔案即可執行（需先在終端機執行一次 chmod +x 每日更新.sh）

cd "$(dirname "$0")"

export PYTHONWARNINGS="ignore::FutureWarning,ignore::Warning"

echo "========================================="
echo " 台灣股市三大法人買超追蹤 v11.58"
echo "========================================="
echo ""
echo "請選擇執行方式："
echo "  1) 完整執行（抓資料 + 寫 Sheets）"
echo "  2) 只抓資料（不寫 Sheets，存快取）"
echo "  3) 只寫 Sheets（用今日快取，不打 API）"
echo "  4) Debug 融資券（印出 API 原始欄位）"
echo "  5) 回測：單股（讀「回測設定」工作表）"
echo "  6) 回測：推薦歷史（全部）"
echo "  7) 回測：推薦歷史（近 30 天）"
echo "  8) 回測：dry-run（不寫 Sheets，只印結果）"
echo "  --- 盤中盯盤（讀「明日關注」清單，Telegram 通知）---"
echo "  9) 盤中盯盤：持續執行（等開盤→每分鐘查價→收盤摘要）"
echo " 10) 盤中盯盤：只查一輪（dry-run，不發通知，測試用）"
echo " 11) 盤中盯盤：列出今日盯盤清單與目標價"
echo " 12) Telegram：設定通知"
echo " 13) Telegram：發送測試通知"
echo " 14) Telegram 指令 bot：本機前景執行（測試用，Ctrl+C 中止；正式請放家用主機）"
echo " 15) Telegram 指令：在本機直接試一個指令（不經 Telegram，例如 /status）"
echo " 16) 檢查假日：比對 config.py 的 HOLIDAYS 與證交所行事曆（只提示，不修改）"
echo "  0) 離開"
echo ""
read -p "請輸入選項 [0-16]: " choice
echo ""

case "$choice" in
    1)
        echo "🚀 完整執行..."
        echo ""
        python3 -u fetch_and_update.py | tee -a log.txt
        ;;
    2)
        echo "📦 只抓資料（存快取）..."
        echo ""
        python3 -u fetch_and_update.py --fetch-only | tee -a log.txt
        ;;
    3)
        echo "📊 只寫 Sheets（讀快取）..."
        echo ""
        python3 -u fetch_and_update.py --sheet-only | tee -a log.txt
        ;;
    4)
        echo "🔍 Debug 融資券..."
        echo ""
        python3 -u fetch_and_update.py --debug-margin | tee -a log.txt
        ;;
    5)
        echo "📈 回測：單股（讀「回測設定」工作表）..."
        echo ""
        python3 -u backtest.py --single | tee -a log.txt
        ;;
    6)
        echo "📊 回測：推薦歷史（全部）..."
        echo ""
        python3 -u backtest.py | tee -a log.txt
        ;;
    7)
        echo "📊 回測：推薦歷史（近 30 天）..."
        echo ""
        python3 -u backtest.py --days 30 | tee -a log.txt
        ;;
    8)
        echo "🧪 回測：dry-run（不寫 Sheets）..."
        echo ""
        python3 -u backtest.py --dry-run | tee -a log.txt
        ;;
    9)
        echo "👀 盤中盯盤：持續執行（Ctrl+C 可中止）..."
        echo ""
        caffeinate -i python3 -u intraday_monitor.py | tee -a intraday_log.txt
        ;;
    10)
        echo "🧪 盤中盯盤：只查一輪（dry-run，不發通知）..."
        echo ""
        python3 -u intraday_monitor.py --once --dry-run
        ;;
    11)
        echo "📋 今日盯盤清單與目標價..."
        echo ""
        python3 -u intraday_monitor.py --list
        ;;
    12)
        echo "🔔 Telegram 通知設定..."
        echo ""
        python3 -u intraday_monitor.py --setup-telegram
        ;;
    13)
        echo "🔔 發送 Telegram 測試通知..."
        echo ""
        python3 -u intraday_monitor.py --test-notify
        ;;
    14)
        echo "🤖 Telegram 指令 bot（前景執行，Ctrl+C 中止）..."
        echo "⚠️ 同一個 bot 同時只能有一個程式在聽；家用主機已在跑 bot 時請勿在 Mac 執行"
        echo ""
        python3 -u tg_bot.py
        ;;
    15)
        read -p "請輸入指令（例如 /list、/status、/add 2330）: " tgcmd
        echo ""
        python3 -u tg_bot.py --cmd "$tgcmd"
        ;;
    16)
        echo "📅 比對 HOLIDAYS 與證交所行事曆（今年與明年）..."
        echo ""
        python3 -u fetch_and_update.py --check-holidays | tee -a log.txt
        ;;
    0)
        echo "👋 離開"
        exit 0
        ;;
    *)
        echo "❌ 無效選項，請輸入 0~16"
        echo ""
        read -p "按 Enter 關閉..."
        exit 1
        ;;
esac

echo ""
read -p "按 Enter 關閉..."
