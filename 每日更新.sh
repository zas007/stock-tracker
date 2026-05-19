#!/bin/bash
# 台灣股市三大法人買超追蹤 — Mac 執行腳本
# 雙擊此檔案即可執行（需先在終端機執行一次 chmod +x 每日更新.sh）

cd "$(dirname "$0")"

echo "================================================"
echo " 台灣股市三大法人買超追蹤"
echo "================================================"
echo ""

# 檢查 Python3
if ! command -v python3 &>/dev/null; then
    echo "❌ 找不到 Python3！"
    echo ""
    echo "請先安裝 Python："
    echo "1. 前往 https://www.python.org/downloads/"
    echo "2. 下載 macOS 版本安裝"
    echo ""
    read -p "按 Enter 關閉..."
    exit 1
fi

echo "🔧 確認套件安裝狀態..."
pip3 install requests gspread google-auth --quiet --upgrade

echo ""
echo "🚀 開始執行..."
echo ""

python3 fetch_and_update.py

echo ""
read -p "按 Enter 關閉..."
