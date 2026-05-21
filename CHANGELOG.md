# 台灣股市三大法人買超追蹤 — 版本修改紀錄

專案腳本：`fetch_and_update.py` + `config.py`（v10 起）
Google Sheets ID：`1DCceOxjew5O4ljeBVTdZ1F9URsvl90k42AAdynaYV9g`

---

## v10.2 — 2026/05/20

### 修正

- **T86 API 欄位結構修正（重大）**
  * 實際回傳 19 欄，非原本預期的 18 欄
  * 外資被拆為「外陸資（不含外資自營商）」`[2][3][4]` + 「外資自營商」`[5][6][7]` 兩組
  * 投信：`[8][9][10]`（原本抓的是 `[5][6][7]` 即外資自營商，完全抓錯）
  * 自營商合計買超：`[11]`（原本用 `[14][15][16]` 是自行買賣欄，非合計）
  * 三大法人合計：`[18]`
  * 修正後各法人數字還原正確，外資 = `[4]+[7]`，自營商買進/賣出顯示 = 自行`[12][13]` + 避險`[15][16]`
- **買超張數單位錯誤（重大）**
  * T86 所有數量欄位單位為「股」，原本直接寫入未換算
  * 全部加上 `// 1000` 換算為張
  * 注意：歷史紀錄中 v10.2 之前的資料仍為股數，需手動修正或清除重跑
- **`_build_sector_triggered()` 變數名稱錯誤**
  * set comprehension 中 `SECTOR_MAP[s]` 在 `for s in triggered` 之前使用，`NameError: name 's' is not defined`
  * 改為 `{c for sector in triggered for c in SECTOR_MAP.get(sector, [])}`
- **gspread `ws.update()` 參數順序**
  * 新版 gspread 要求 named arguments
  * `ws.update("A1", data)` → `ws.update(range_name="A1", values=data)`
- **欄位數檢查門檻**
  * 從 `≥17` 改為 `≥19`，符合實際 API 結構

### 新增

- **`calc_position_fifo(buy_entries, sell_entries)`**
  * 以**價格升序** FIFO 扣減賣超，優先出清低成本部位（符合實際出場行為）
  * 回傳 `(remaining_lots, weighted_avg_price)`
  * `avg_price = 0` 的 entry 仍參與張數扣減，但不列入均價計算
  * 取代原本的 `weighted_avg()`（純買超加權，忽略賣超）
- **`sell_hist` 拆分為各法人獨立紀錄**
  * 原：`sell_hist[code] = [(date, net), ...]`（三法人混合）
  * 新：`sell_hist[code] = {"f": [...], "t": [...], "d": [...]}` 各自獨立
  * 各法人 FIFO 扣減只扣自己的賣超，不互相影響
- **`fetch_industry_map()`**
  * 從 `isin.twse.com.tw` 抓 Big5 編碼的上市股票官方產業別
  * 回傳 `{代號: 產業別名稱}`，失敗時回傳空 dict 不中斷執行
- **`auto_update_sector_map()`**
  * 買超榜出現不在 `SECTOR_MAP` 的新股票時，查官方產業別自動歸類
  * 即時更新記憶體中的 `SECTOR_MAP` 與 `CODE_TO_SECTOR`（本次執行生效）
  * 同步寫回 `config.py`（下次執行不需重查）
  * 族群已存在時插入列表末尾；族群不存在時新增整個族群區塊
- **`CODE_TO_SECTOR` 反查表**
  * 啟動時從 `SECTOR_MAP` 自動建立 `{代號: 族群名}` 反查表
  * `_build_sector_triggered()` 改為從買超榜出發查反查表，邏輯更快且新補入代號即時生效

### 調整

- **`fetch_institutional()` 重構為具名函式**
  * `foreign_buy/sell()`、`trust_buy/sell()`、`dealer_buy/sell()` 各自獨立，欄位對應清晰可讀

---

## v10.1 — 2026/05/20

### 修正

- **自營商欄位索引錯誤**
  * 舊：`T86[8][9][10]`（自營商「自行買賣」）
  * 新：`T86[14][15][16]`（自營商「合計 = 自行 + 避險」）
  * 影響：原本自營商買超/賣超數字偏小，現已改用正確合計欄
- **`to_int()` 強化**
  * TWSE API 無資料時常回傳空字串 `""` 或破折號 `-`／`－`，原本靠 `except` 兜底
  * 現明確攔截這些值並回傳 0，避免潛在解析異常

### 優化

- **`fetch_stock_day` + `fetch_stock_quote` 合併**
  * 兩者都打 `STOCK_DAY` API，邏輯幾乎相同
  * 統一為 `fetch_stock_day_full()`，回傳 `(avg_price, close, volume_lots, change_pct)`
  * 一次 API 取得均價、收盤、成交量、漲跌幅，不再重複請求
- **族群聯動優先使用價格快取**
  * 舊：`update_sector_sheet()` 對所有族群成員重新呼叫 `fetch_stock_quote()`，已在買超榜抓過的股票也重抓一遍
  * 新：優先查 `current_prices` 快取，只對快取中沒有的股票才補抓，大幅減少 API 呼叫次數與等待時間
- **`build_row()` 獨立為模組層函式**
  * 原為 `update_analysis()` 內的 inner function，現拉出為模組層函式，`all_dates`、`net_by_date` 改為顯式參數傳入
- **`update_analysis()` 拆分為三段**
  * `_build_history_maps()` — 從歷史紀錄建立 `buy_map`、`sell_hist`、`net_by_date`、`all_dates`
  * `_consecutive_days()` — v9 連續天數邏輯獨立成函式
  * `update_analysis()` — 只負責組裝資料與寫入工作表
- **`ANALYSIS_HEADERS` 提升為模組層常數**
  * 對照分析與每日快照共用同一份 header，避免日後欄位不同步
- **對照分析 / 每日快照排序改為最近出現日**
  * 舊：依三法人累計出現天數加總排序
  * 新：依 `last`（最近出現日）降序，今天有出現在買超榜的優先排前面
- **Step 1 log 新增各法人筆數**
  * 執行後顯示外資／投信／自營商各自買超、賣超幾筆，方便判斷投信當日是否有資料
- **`main()` 版本標題修正**
  * 原顯示「v8」與 v8 說明文字，改為正確的「v10」

---

## v10 — 2026/05/19

### 新增

- **獨立設定檔 `config.py`**
  * 所有參數、族群表、名稱對照集中管理，不需動主程式
  * 涵蓋：SPREADSHEET_ID、CREDENTIALS_FILE、RISK_LOW/MID、CHIP_HIGH/MID、TRUST_STAR/FIRE、MARGIN_WARN、SECTOR_MAP、CODE_NAME_MAP
  * 主程式啟動時自動 `import config`，找不到時 fallback 內建預設值，不中斷執行
- **`CODE_NAME_MAP` 股票名稱對照表**
  * 族群成員名稱不再依賴買超榜，所有成員（含未上榜者）均能正確顯示中文名稱
  * 名稱優先順序：`CODE_NAME_MAP` → 買超榜現有名稱 → 代號本身

### 調整

- **族群聯動改為每日累積模式**
  * 原本每次執行覆蓋，改為新資料插最上方、舊資料加分隔線往下保留（同今日買超排行邏輯）
  * 今日無觸發時仍插入一筆「無觸發」紀錄，不跳過、不清空歷史
- 主程式設定區移除（改由 `config.py` 統一管理），僅保留 `COOKIE_FILE` 等系統固定值

---

## v9 — 2026/05/19

### 修正

- **連續天數計算邏輯重寫（方案 A+C）**
  * 舊邏輯：只看某法人是否出現在買超榜，賣超不中斷
  * 新邏輯：以「當日三法人合計淨買超 > 0」判斷，任一天合計為負（含賣超）即歸零重計
  * 範例：外資買 500 張、投信賣 600 張 → 合計 -100 張 → 當天中斷；外資買 500、投信賣 200 → 合計 +300 → 繼續連續
  * 新增 `net_by_date` dict，從歷史紀錄重建每支股票每日三法人合計淨張數
  * `consecutive_days()` 新增 `code` 參數，改從 `net_by_date` 查詢
- **族群聯動名稱與漲跌幅補強**
  * 舊邏輯：未在買超榜的成員走快取路徑，名稱空白、漲跌幅常為 N/A
  * 新邏輯：族群觸發後，所有成員一律呼叫 `fetch_stock_quote()` 重抓完整行情，不再走快取捷徑

---

## v8 — 2026/05/15

### 新增

- **族群聯動工作表**（獨立工作表「族群聯動」）
  * 新增 `SECTOR_MAP` 族群對照表（設定區），預設 8 個族群：探針卡、AI伺服器、光通訊、HBM/CoWoS封裝、散熱、PCB、記憶體模組、電源供應
  * 每次執行自動比對今日買超榜，只要族群內有任何成員出現，整個族群全員當日行情一併列出
  * 族群聯動顯示欄位：代號、股票名稱、收盤價、漲跌幅%、成交量(張)、是否在買超榜、觸發族群
  * 族群未被觸發時工作表顯示「今日無觸發」提示，不留空
  * 族群成員若未在價格快取中，自動補抓行情（間隔 0.4 秒，避免 API 限流）
- 新增 `fetch_stock_quote()` — 抓單支股票收盤價、漲跌幅、成交量（從月資料計算前日比較）
- 新增 `build_sector_in_buy()` — 從買超榜代號集合找出觸發族群
- 新增 `update_sector_sheet()` — 寫入族群聯動工作表
- 主程式新增 Step 5（原 4 步變 5 步）

### 調整

- `main()` 新增 `all_buy_codes`（set）與 `all_buy_names`（dict）快取，傳給族群聯動使用
- 執行步驟標示由 Step 1/4 改為 Step 1/5

---

## v7 — 2026/05/15

### 新增

- **籌碼集中度**
  * 計算公式：三法人合計買超張數 ÷ 當日成交量
  * 顯示位置：今日買超排行（新增欄位）、歷史紀錄、對照分析
  * 等級：🔵 高度集中（≥20%）/ 🟦 中度集中（≥10%）/ ⬜ 偏低
  * 新增設定參數 `CHIP_HIGH`、`CHIP_MID`
- **投信連續買超標記**
  * 顯示位置：對照分析新增「投信標記」欄
  * 🔥 連續 ≥5 天 / ⭐ 連續 ≥3 天 / 數字（未達門檻）
  * 新增設定參數 `TRUST_STAR`、`TRUST_FIRE`
- **融資融券資料**（新增 4 欄至對照分析）
  * 融資餘額(張)、融資增減(張)、融券餘額(張)、融資健康度
  * 融資健康度判斷：✅ 籌碼乾淨 / 🟡 小幅跟進 / ⚠️ 散戶大量跟進 / 🔴 法人不買散戶買
  * 新增設定參數 `MARGIN_WARN`（預設 500 張）
  * 資料來源：TWSE `MI_MARGN` API（一次抓全市場，不逐支查詢）

### 調整

- `fetch_stock_day()` 回傳值新增第三個欄位 `volume_lots`（成交量張數）
- `enrich_with_prices()` 同步更新，將 `volume` 寫入每支股票的 dict
- 新增 `enrich_with_margin()` — 批次抓融資融券（一次 API 全市場比對）
- `append_history()` 歷史紀錄新增「成交量(張)」與「籌碼集中度」兩欄
- `update_analysis()` 新增 `current_margin` 參數，對照分析欄位由 23 欄擴充至 30 欄
- 今日買超排行欄位由 8 欄擴充至 10 欄（新增成交量、籌碼集中度）
- 主程式新增 Step 3（融資融券）、Step 4（原寫入 Sheets），共 4 步

---

## v6 — 2026/05 前

### 新增

- **賣超排行**
  * 新增「今日賣超排行」工作表（外資、投信、自營商各前十名）
  * 歷史紀錄新增「買/賣」欄位，賣超資料一併寫入
  * `fetch_institutional()` 新增 `top10_sell()` 方法
- **收盤價**
  * `fetch_stock_day()` 回傳值新增收盤價（`close`）
  * 今日買超/賣超排行、歷史紀錄均顯示收盤價
- **連續天數**
  * 對照分析新增「外資/投信/自營商連續天數」欄位
  * 以歷史交易日序列計算，斷掉即歸零
- **每日快照工作表**
  * 每次執行插入最上方，舊的加分隔線往下，供跨日比較用
- 出貨風險訊號整合至對照分析

### 工作表結構（v6 確立）

| 工作表    | 說明            |
| ------ | ------------- |
| 今日買超排行 | 最新插最上面，舊的加分隔線 |
| 今日賣超排行 | 同上            |
| 歷史紀錄   | 每日累積，不覆蓋      |
| 對照分析   | 每次執行完整覆蓋      |
| 每日快照   | 最新插最上面，舊的加分隔線 |

---

## v5 — 2026/05 前

### 新增

- 抓取個股**當日均價**（成交金額 ÷ 成交股數）
- 對照分析新增**加權平均成本**（各法人 Σ(買超張數×當日均價) ÷ Σ(買超張數)）
- 出貨風險欄位（現價 vs 加權均價漲幅判斷）

---

## v4 — 2026/05 前

### 修正

- 改用 `curl subprocess` 抓取 TWSE API，解決 SSL 憑證驗證問題
- 加入 cookie 暖機（`warm_up_cookie()`），避免 TWSE 反爬蟲阻擋
- 抓取間隔加入 0.5 秒 sleep，降低被限流機率

---

## v3 — 2026/05 前

### 修正

- 放棄 `requests` library，改用 `requests + session` 模擬瀏覽器行為
- 加入 User-Agent、Referer header

---

## v2 — 2026/05 前

### 嘗試（失敗）

- 改用 Google Sheets `IMPORTHTML` 直接抓 Yahoo Finance
- 失敗原因：Yahoo Finance 使用 JavaScript 動態渲染，IMPORTHTML 無法取得資料

---

## v1 — 2026/05 前

### 初版

- 基本 TWSE T86 API 抓取三大法人買超資料
- 寫入 Google Sheets（使用 `gspread` + 服務帳戶金鑰）
- 資料來源：`https://www.twse.com.tw/rwd/zh/fund/T86`

---

## 設定參數一覽（v10.2 現況）
> 所有參數統一在 `config.py` 調整

| 參數              | 預設值   | 說明                     |
| --------------- | ----- | ---------------------- |
| `RISK_LOW`      | 0.10  | 出貨風險低門檻（漲幅 <10% → 🟢）   |
| `RISK_MID`      | 0.15  | 出貨風險中門檻（漲幅 10~15% → 🟡） |
| `CHIP_HIGH`     | 0.20  | 籌碼集中度高門檻（≥20% → 🔵）     |
| `CHIP_MID`      | 0.10  | 籌碼集中度中門檻（≥10% → 🟦）     |
| `TRUST_STAR`    | 3     | 投信連續買超⭐門檻（天）           |
| `TRUST_FIRE`    | 5     | 投信連續買超🔥門檻（天）           |
| `MARGIN_WARN`   | 500   | 融資大量跟進警示門檻（張）          |
| `SECTOR_MAP`    | 8 族群  | 族群聯動對照表，可自由增刪          |
| `CODE_NAME_MAP` | 30+ 支 | 股票中文名稱對照，補全族群成員名稱      |

---

## 檔案結構（v10.2 現況）

```
~/Documents/Z/study/stock/
├── fetch_and_update.py   ← 主程式（每次更新換這個）
├── config.py             ← 設定檔（族群、門檻、名稱對照，日常調整改這個）
├── CHANGELOG.md          ← 版本修改紀錄
├── 每日更新.sh            ← 手動執行用
├── credentials.json      ← Google API 金鑰（勿外洩）
└── log.txt               ← 自動執行記錄
```

---

*最後更新：2026/05/20（v10.2）*
