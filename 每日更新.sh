#!/bin/bash
# 台灣股市三大法人買超追蹤 — Mac 執行腳本
# 雙擊此檔案即可執行（需先在終端機執行一次 chmod +x 每日更新.sh）

cd "$(dirname "$0")"

echo "================================================"
echo " 台灣股市三大法人買超追蹤 v11.5"
echo "================================================"
echo ""
echo "請選擇執行模式："
echo "  1) 完整執行（抓資料 + 寫 Sheets）"
echo "  2) 只抓資料（不寫 Sheets，存快取）"
echo "  3) 只寫 Sheets（用今日快取，不打 API）"
echo "  4) Debug 融資券（印出 API 原始欄位）"
echo "  0) 離開"
echo ""
read -p "請輸入選項 [0-4]：" choice
echo ""

case "$choice" in
    1)
        echo "🚀 完整執行..."
        echo ""
        python3 fetch_and_update.py
        ;;
    2)
        echo "📡 只抓資料（存快取）..."
        echo ""
        python3 fetch_and_update.py --fetch-only
        ;;
    3)
        echo "📊 只寫 Sheets（讀快取）..."
        echo ""
        python3 fetch_and_update.py --sheet-only
        ;;
    4)
        echo "🔍 Debug 融資券..."
        echo ""
        python3 fetch_and_update.py --debug-margin
        ;;
    0)
        echo "👋 離開"
        exit 0
        ;;
    *)
        echo "❌ 無效選項，請輸入 0~4"
        echo ""
        read -p "按 Enter 關閉..."
        exit 1
        ;;
esac

echo ""
read -p "按 Enter 關閉..."
