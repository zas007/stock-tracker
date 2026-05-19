# 台灣股市三大法人買超追蹤

自動抓取 TWSE 三大法人買超/賣超資料，寫入 Google Sheets，每日 16:30 自動執行。

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `fetch_and_update.py` | 主程式，每次更新換這個 |
| `config.py` | 設定檔，族群、門檻、名稱對照，日常調整改這個 |
| `每日更新.sh` | 手動執行腳本 |
| `CHANGELOG.md` | 版本修改紀錄 |
| `credentials.json` | ⛔ Google API 金鑰，本機保管，不在此 repo |
| `log.txt` | ⛔ 執行記錄，本機保管，不在此 repo |

## 快速開始

```bash
# 手動執行
cd ~/Documents/Z/study/stock
./每日更新.sh

# 查看 log
tail -30 ~/Documents/Z/study/stock/log.txt

# 查看 crontab（每天 16:30 自動執行）
crontab -l
```

## Google Sheets

`https://docs.google.com/spreadsheets/d/1DCceOxjew5O4ljeBVTdZ1F9URsvl90k42AAdynaYV9g`

| 工作表 | 說明 |
|--------|------|
| 今日買超排行 | 三法人買超前十名，新資料插最上面 |
| 今日賣超排行 | 三法人賣超前十名，新資料插最上面 |
| 歷史紀錄 | 所有買超/賣超逐行累積 |
| 對照分析 | 完整統計（連續天數、加權均價、出貨風險），每次覆蓋 |
| 每日快照 | 每天統計快照，新資料插最上面 |
| 族群聯動 | 買超榜觸發族群的全員行情，新資料插最上面 |

## 目前版本

v10 — 詳見 [CHANGELOG.md](CHANGELOG.md)
