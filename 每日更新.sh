#!/bin/bash
# 台灣股市三大法人買超追蹤 — Mac 執行腳本
# 雙擊此檔案即可執行（需先在終端機執行一次 chmod +x 每日更新.sh）

cd "$(dirname "$0")"

export PYTHONWARNINGS="ignore::FutureWarning,ignore::Warning"

echo "========================================="
echo " 台灣股市三大法人買超追蹤 v11.30"
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
echo "  0) 離開"
echo ""
read -p "請輸入選項 [0-8]: " choice
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
    0)
        echo "👋 離開"
        exit 0
        ;;
    *)
        echo "❌ 無效選項，請輸入 0~8"
        echo ""
        read -p "按 Enter 關閉..."
        exit 1
        ;;
esac

echo ""
read -p "按 Enter 關閉..."
