# 台灣股市三大法人買超追蹤 — 版本修改紀錄

專案腳本：`fetch_and_update.py` + `config.py`（v10 起）
Google Sheets ID：`1DCceOxjew5O4ljeBVTdZ1F9URsvl90k42AAdynaYV9g`

---

## v11.31 — 2026/07/07

### fetch_and_update.py

- **【新增】量比評分改用「成交量歷史」批次計算，解決同日兩次執行排名不穩定問題（待處理 #28）**
  * 舊邏輯：`fetch_price_map_batch`（STOCK_DAY_ALL 批次抓價，涵蓋約 95% 股票）固定回傳 `volume_ratio=None`，因為該 API 不接受日期參數、無法取得前 10 日歷史；只有逐支補抓（`fetch_stock_day_full`）才能算出真實量比。當某次執行因時間過早導致批次判斷日期不符（`if western != date_str: return {}, {}`）而全面 fallback 到逐支補抓時，幾乎全市場股票的評分基礎會整批改變（None→3分 vs 真實量比1~7分），造成同一天執行兩次「明日關注」Top5 排名不一致
  * 新邏輯：新增 `update_volume_history()` / `load_volume_history()` / `calc_volume_ratio_from_history()`，仿照「融資歷史」模式另建「成交量歷史」工作表，每日寫入 `current_prices` 涵蓋的約 300 檔股票（買超+賣超共6組）成交量，保留 31 天、不受「對照分析」5天過濾規則限制；`build_row()` 新增 `volume_hist` 參數，量比優先用「今日成交量 ÷ 近10天平均（歷史不足時以現有天數平均）」計算，兩條路徑（batch/逐支）改為共用同一份資料源；完全無歷史的新股票才 fallback 回原本的即時值
  * `_calc_analysis_rows()` 載入 `volume_hist` 並傳給 `build_row()`；`update_analysis`（對照分析）與 `_run_recommendation`（明日關注）皆共用此函式，兩入口同步生效
  * `SHEET_OPTIONS` 新增選項 `B) 成交量歷史`，完整執行（選項1）預設自動包含
  * 效果：batch 命中與逐支補抓不再各自產生不同評分基礎，同日重跑排名穩定性提升；新機制上線後約需 10 個交易日資料完整累積才達完整覆蓋率（漸進式補滿，比照 MA5 batch 的先例）

### config.py

- **【修正】「半導體」族群內 `2379`（瑞昱）重複列出**
  * 舊：`2379` 在同一個族群清單中出現兩次（第 286、306 行），純屬複製貼上疏漏
  * 新：移除多餘的一筆
  * 效果：`SECTOR_MAP` 成員總數由 358 → 357，無實質功能影響（僅清理冗餘資料）

- **【調整】3 組跨族群重複代號補上「也保留」註解**
  * 舊：`6669`（緯穎，AI伺服器/光通訊）、`4977`（眾達-KY，光通訊/電源供應）、`5483`（中美晶，HBM/CoWoS封裝/電源供應）在兩個族群中重複出現，但沒有任何註解說明是否為刻意設計，與其他 7 組同類型重複（皆有標註「也保留」/「亦保留」）不一致
  * 新：比照既有慣例，各補上一筆「也保留」註解，明確標示為刻意讓該股票同時觸發兩個族群聯動
  * 效果：目前跨族群重複代號共 10 組，全部都有清楚標註，避免日後誤判為疏漏而誤刪

---

## backtest v1.1 — 2026/06/29

### backtest.py

- **單股回測模式（`--single`）新增**
  * 新增 `load_single_stock_setting(ss)`：讀取「回測設定」工作表（A欄=代號、B欄=備註），工作表不存在時自動建立並填入範例
  * 新增 `run_single_stock_backtest(ss, hist_records, codes_remarks)`：從「歷史紀錄」撈指定股票所有法人買超訊號，進場=買超當日收盤，出場=T+3 收盤 或 法人當日轉賣超（取先到者），呼叫 TWSE STOCK_DAY API 補抓收盤價
  * 新增 `_single_stock_summary(results)`：依股票彙整勝率、平均損益、最佳/最差
  * 新增 `write_single_stock_sheet(ss, results, summary)`：輸出至「單股回測」工作表，上半部彙整摘要，下半部逐筆明細（進出場日/價/原因/損益%/T+1~T+3收盤/法人各方向張數/籌碼集中度）
  * 新增 `main_single(ss, dry_run)`：單股回測主流程，整合上述函式
  * 新增 `curl_get(url)`、`fetch_close_price_single(code, date_str)`、`_next_trading_days(date_str, n)`：價格抓取工具（含 module-level `_price_cache` 避免重複打 API）
  * 新增 `SINGLE_STOCK_SETTING = "回測設定"`、`SINGLE_STOCK_RESULT = "單股回測"` 常數
  * `main()` 新增 `--single` 參數，連線後依旗標分流至 `main_single()` 或原有推薦回測流程
  * 版本從 v1.0（架子版）升至 v1.1
  * 原有 `fetch_close_price` TODO 函式保留，不影響既有推薦回測流程

- **執行方式**
  ```bash
  python3 backtest.py --single          # 單股回測（讀「回測設定」工作表）
  python3 backtest.py --single --dry-run  # dry-run 模式（不寫 Sheets）
  ```

---

## v11.30 — 2026/07/02

### 每日更新.sh

- **修正輸出緩衝卡住問題**
  * 8 處 `python3 fetch_and_update.py` / `python3 backtest.py` 呼叫全部加上 `-u`（unbuffered）參數
  * 原因：v11.29 加入 `| tee -a log.txt` 後，Python 偵測 stdout 非 tty，改為整塊緩衝，導致畫面停在「🚀 完整執行...」不動（實際背景仍在跑，只是輸出被緩衝住，直到緩衝區滿或程式結束才一次噴出）
  * 修正後 Step 1~4/5 進度會即時印出，不再有「假卡住」的情況
  * banner 版號同步至 v11.30

---

## v11.29 — 2026/06/26

### fetch_and_update.py

- **5日融資增減趨勢（新增 #26）**
  * 新增 `update_margin_history(ss, date_str, current_margin)`：每日將融資餘額寫入「融資歷史」工作表（格式：日期/代號/融資餘額(張)），保留 31 天，重跑自動覆蓋
  * 新增 `load_margin_history(ss)`：讀取近期融資餘額，回傳 `{code: [(disp, mb), ...]}` 按日期升序
  * 新增 `calc_margin_trend(margin_hist, code, days=5)`：計算近 5 天融資餘額增減，回傳 (delta, label)；label 格式：`↗ 大增N張`（≥500張）/ `↗ 增N張` / `↘ 大減N張` / `↘ 減N張` / `➡ 持平`
  * 新增 `_score_margin_trend(margin_trend)`：融資趨勢評分 -6 ~ +6 分；大減（≥500張）+6 / 小減 +3 / 小增 -3 / 大增 -6，中性 0
  * `build_row` 新增 `margin_hist` 參數，計算並輸出 `[39] 融資趨勢` 欄
  * `ANALYSIS_HEADERS` 新增 `[39] 融資趨勢`
  * `score_stock` 與 `score_stock_relaxed` 均納入 `_score_margin_trend`（主榜+觀察組同步）
  * `SHEET_OPTIONS` 新增 `A) 融資歷史`（寫入時機與融券歷史相同）
  * `_calc_analysis_rows` 在載入 short_hist 後同步載入 `margin_hist`

- **crontab 修正**
  * 移除多餘的第二條（用 `~` 和裸 `python3`，crontab 環境不展開，會執行失敗）
  * 只保留第一條（完整絕對路徑），log 正常寫入

### 每日更新.sh

- **新增 `tee -a log.txt`**：每個模式（完整/只抓/只寫/Debug）執行時同時輸出到終端機與 log.txt（append）
- **banner 版號同步**：從 v11.5 更新至 v11.28（解決待處理 #20）


---

## v11.28 — 2026/06/25

### fetch_and_update.py

- **TWSE API 格式改版因應（`fetch_price_map_batch` 重寫）**
  * TWSE STOCK_DAY_ALL 回傳格式從 JSON 改為 CSV，欄位順序亦不同
  * 新 CSV 欄位：日期[0] 代號[1] 名稱[2] 成交股數[3] 成交金額[4] 開盤[5] 最高[6] 最低[7] 收盤[8] 漲跌價差[9]
  * 函式改為自動偵測：`日期,` 開頭 → CSV 解析；`{` 開頭 → 舊 JSON 解析（相容）；其他 → 印出前 80 字 debug 後 retry
  * CSV 日期欄為民國年（如 `1150625`），修正為前3碼 +1911 轉西元再比對，解決「batch 不適用」降級 OTC 問題

- **retry 等待時間調整**：STOCK_DAY_ALL 失敗重試間隔從 60 秒降為 3 秒

- **每個 Step 結束後詢問是否繼續（新增 `_ask_continue`）**
  * 顯示格式：`[v11.28] HH:MM:SS  耗時 Xs  Step N/5 XXX 完成。按 [Enter] 繼續，輸入 q 後 Enter 中止（5 秒後自動繼續）...`
  * 包含版號（`VERSION` 變數）、當前時間、耗時
  * 插入點：Step 1（三大法人）、Step 2（個股價格）、Step 3（融資融券）、快取命中補抓全市場後、Step 4（寫入 Sheets）
  * 5 秒無操作自動繼續；輸入 `q` + Enter 可中止

- **執行時間列加上版號**：`執行時間：2026/06/25 15:40  [v11.28]`

- **`update_short_history` 函式定義遺漏修正**：`def` 行不知何時被刪除，只剩 docstring 浮在外，補回函式定義

- **重大訊息改為同時抓今日與前一交易日（`fetch_news_announcements` 修改）**
  * 原本只抓當日，16:30 前今日資料未釋出時容易漏抓
  * 改為對前一交易日與今日各打一次 MOPS API，各自印出命中筆數
  * 前一交易日用 `_is_trading_day` 往回找，週末/假日自動跳過

### backtest.py

- **T+4 / T+5 擴充**
  * `load_perf_history`：新增讀取 t4（col[8]）、t5（col[9]），欄位索引整體後移（group→[10], risk→[11], margin_health→[12]）
  * `DETAIL_HEADERS`：新增「推薦星期」欄、T+4/T+5 的收盤/漲跌%/勝負欄位
  * `write_detail_sheet`：自動計算推薦日是週幾（週一～週五）
  * 主流程：`row` dict 新增 `t4_pnl`、`t5_pnl`

- **勝率矩陣擴充**
  * 新增切面 8：「推薦日星期幾 × T+1 勝率」（週一～週五各自統計）
  * 整體總覽從 T+1/T+2/T+3 擴充至 T+1/T+2/T+3/T+4/T+5
  * 切面總數：8 → 9

- **量比 None 基礎分調整（`_score_volume_ratio` 修正）**
  * 問題：batch（TWSE/OTC）命中時量比回傳 `None`，逐支補抓時才有實際值；兩條路徑評分差最多 6 分（None=2 vs 爆量=7），同一天執行兩次因 batch 命中率不同而推薦排名改變
  * 修正：None 時從 2 分改為 3 分（落在「正常量2分」與「溫和放量4分」之間），兩路徑最大差距從 6 分縮小為 4 分，排名穩定性提升

- **族群熱度排序改為趨勢優先**：第一鍵趨勢（↗升溫 > ➡持平 > ↘降溫），第二鍵熱度分

---

## v11.27 — 2026/06/24

### fetch_and_update.py
- **`fetch_price_map_batch` retry 機制（新增）**：TWSE STOCK_DAY_ALL 失敗時不再直接跳逐支，改為最多重試 2 次（每次間隔 60 秒）；日期不符的情況不重試直接放棄（資料本就不適用）；重試 2 次仍失敗才改逐支補抓

---

## v11.26 — 2026/06/24

### fetch_and_update.py
- **`fetch_news_announcements(date_str)`（新增）**：POST MOPS `ajax_t51sb10`，一次撈全市場當日重大訊息公告；對每筆主旨做關鍵字比對，回傳利多/利空清單（中性略過）
- **`update_news_history(ss, date_str, news_items)`（新增）**：將命中公告寫入「重大訊息歷史」工作表（欄位：日期/代號/名稱/標籤/主旨），保留 31 天，重跑自動覆蓋
- **`load_news_for_codes(ss, codes, date_str, days=3)`（新增）**：讀取近 N 天重大訊息，回傳 `{code: [tag, ...]}` 供明日關注標記使用
- **`update_recommendation` 修改**：推薦計算前預載 `_news_map`；推薦股若近 3 天有命中公告，在「自營商標記」欄附加 `📢利多` / `📢利空`（不影響評分）
- **`SHEET_OPTIONS` 新增**：`0) 重大訊息歷史`
- **`NEWS_KEYWORDS` 全域載入**：`getattr(_cfg, "NEWS_KEYWORDS", {"利多": [], "利空": []})`
- **`fetch_price_map_batch` 修正**：4 個提前 return 從 `return {}` 改為 `return {}, {}`，與正常路徑 tuple 回傳一致，消除潛在 `TypeError`

### config.py
- **`NEWS_KEYWORDS`（新增）**：利多關鍵字 10 組（重大合約、法說會、營收創新高等）、利空關鍵字 12 組（獲利預警、財報重編、停工等）；可自行調整，不需動主程式

---

## v11.24 — 2026/06/23

### fetch_and_update.py
- **`fetch_holidays_from_twse(year)`（新增）**：呼叫 TWSE 假日月曆 API，解析民國年日期，回傳 `set of "YYYYMMDD"`
- **`ensure_holidays_loaded(year)`（新增）**：檢查 `HOLIDAYS` 是否已含指定年度資料；若無則呼叫 API 查詢，並用 regex 寫回 config.py 永久記錄
- **`_is_trading_day(d)`（新增）**：判斷是否為交易日（非週末且不在 `HOLIDAYS`）
- **`_n_trading_days_after`（修正）**：改用 `_is_trading_day`，國定假日不再被計入交易日，修正推薦成效 T+1/T+2/T+3 欄位在連假後對齊錯誤的問題
- **`find_trading_day`（修正）**：改用 `_is_trading_day`，國定假日不再嘗試抓法人資料
- 全域載入：`HOLIDAYS = getattr(_cfg, "HOLIDAYS", set())`

### v11.24 → v11.25 修正（同日）
- **`ensure_holidays_loaded` 無限重查 bug 修正**：加入 `_HOLIDAYS_ATTEMPTED` set，查詢失敗後不再重試，避免 44 筆推薦成效觸發 44+ 次 TWSE API 呼叫
- **移除多餘的跨年預載**：`_n_trading_days_after` 的 `ensure_holidays_loaded(d.year + 1)`、`find_trading_day` 的 `ensure_holidays_loaded(now.year - 1)` 均為不必要呼叫，直接移除；T+3 跨年時 `d.year` 自然觸發正確年度
- **`update_performance` 迴圈前統一 warm up**：從 rows 收集所有涉及年度，一次預載，迴圈內不再觸發查詢

### config.py
- **`HOLIDAYS`（新增）**：新增國定假日 set，預填 2026 年已知休市日（含端午 6/19、6/22）；程式首次跑到新年度時自動查 TWSE 補入，不需手動維護
- **`其他` 族群整理**：16 支移出，只保留漢唐(2404)
  - → PCB：精成科(6191)、廣宇(2328)
  - → 電子代工：瑞軒(2489)
  - → 半導體：全新(2455)
  - → 網通：兆赫(2485)、智易(3596)
  - → 面板：TPK-KY(3673)
  - → 電源供應：飛宏(2457)
  - → 傳產：寶成(9904)、復盛應用(6670)、億豐(8464)
  - → 被動元件（現有）：立隆電(2472)、凱美(2375)
  - → 生技醫療（現有）：康霈(6919)
  - → **新建「LED」族群**：富采(3714)、宏齊(6168)
- **`CODE_NAME_MAP` 補入** 16 支新歸類股票名稱
- 族群總數：21 → 22；CODE_NAME_MAP：168 → 184 筆

---

## v11.23 — 2026/06/22

### fetch_and_update.py
- **`PERFORMANCE_HEADERS` 新增「出貨風險」「融資健康度」兩欄（col[9], col[10]）**
  * 推薦成效寫入時從明日關注工作表 `r[7]`（出貨風險）、`r[8]`（融資健康度）取值
  * 往後 T+3 封存至推薦歷史時會帶入這兩欄，供 backtest.py 切面分析使用
  * 舊資料（45 筆）欄位為空，新切面需等新版跑幾天後才有數值

### config.py
- **IC封測族群新增成員**：精材(3374)、景碩(3189)、頎邦(6147)、南茂(8150)
- **CODE_NAME_MAP 補上**：尖點(8021)、南茂(8150)、精材(3374)
- IC封測族群從 7 支擴充至 11 支

### backtest.py
- **`load_perf_history` 新增讀取**：col[8]=組別、col[9]=出貨風險、col[10]=融資健康度
- **`_rebuild_features` 改從推薦歷史直接取**出貨風險/融資健康度，不再填 "TODO"
- **勝率矩陣新增三個切面**：
  * 出貨風險 × T+1 勝率
  * 融資健康度 × T+1 勝率
  * 主榜 vs 觀察組 × T+1 勝率
- 切面總數：5 → 8

---

## v11.22 — 2026/06/18

### 修正 / 調整

- **`_score_matrix` 大型股補償（★ v11.22）**
  * 問題：大型股（群創、南亞科、台積電等）成交量大，法人買再多張籌碼集中度也天生偏低（<10%），matrix 最多只拿 4~10 分（滿分 40），嚴重低估法人佈局力道
  * 修正：籌碼偏低時，依今日買超金額額外補償：
    - 買超金額 ≥ **1億**：base × 1.5（4→6 / 7→11 / 10→15）
    - 買超金額 ≥ **3億**：base × 2.0（4→8 / 7→14 / 10→20，上限 27 比照中度集中）
  * 高度集中 / 中度集中不受影響
  * `_score_matrix(consec, chip_lbl, today_amount=0)` 加第三參數，兩個呼叫點（`score_stock`、`score_stock_relaxed`）同步更新

### 驗算（2026/06/18 資料）

| 股票 | 買超金額 | matrix 舊 | matrix 新 | 估計總分變化 |
|------|---------|---------|---------|-----------|
| 群創 3481 | 11.0 億 | 4 | 8 | +4（約 44→48） |
| 南亞科 2408 | 61.3 億 | 4 | 8 | +4（觀察組估計 64~69） |

---

## v11.21 — 2026/06/17

### 新增

- **動能評分（`_score_momentum`，-2 ~ +5 分）**
  * 當日漲跌% 納入評分：漲 ≥5% → +5；漲 ≥3% → +4；漲 ≥1.5% → +3；漲 ≥0.5% → +2；平盤 → +1；跌 ≥-1.5% → 0；跌 ≥-3% → -1；跌更多 → -2
  * 目的：法人持續買進 + 當日股價強勢 → 加分，避免過度追高的評分失真

- **振幅% 欄位（`ANALYSIS_HEADERS[38]`）**
  * 公式：`(當日最高 - 當日最低) / 最低 × 100%`
  * 來源：STOCK_DAY_ALL batch `row[5]`（最高）、`row[6]`（最低），原本未解析
  * 振幅 ≥5% 顯示 `⚡5.2%`，提示短線機會與風險並存
  * `fetch_price_map_batch` 回傳改為 tuple `(price_map, amp_map)`，所有呼叫點同步更新（共 6 處）

- **高風險觀察組（明日關注下方）**
  * 主榜被過濾掉的股票（出貨風險🔴、現價 > 400 等）另列「⚠️ 高風險觀察組」前5名
  * 條件放鬆：允許出貨風險🔴/🟡、允許高股價；保留：非ETF、非今日賣超、連續天數 ≥1 天
  * 新增 `score_stock_relaxed(row)`，邏輯同主榜但移除硬過濾門檻
  * 觀察組不與主榜重複（排除已在主榜的代號）

- **推薦成效記錄觀察組**
  * `PERFORMANCE_HEADERS` 新增第 9 欄「組別」（`主榜` / `觀察組`）
  * `_parse_rec_sheet` 和 `update_performance` 今日 block 解析均識別觀察組 header，正確記錄組別
  * 後續回測可用「組別」欄分開計算主榜 vs 觀察組勝率

---

## v11.20 — 2026/06/17

### 修正

- **對照分析全部跳過（根本原因：`r[-1]` 欄位錯位）**
  * v11.18 新增 `r[37]`（今日買超金額）後，過濾邏輯 `r[-1]` 從「最近出現日」變成「今日買超金額（數字）」
  * `_trading_days_diff` 用數字當日期 parse 失敗回傳 999，`> 5` 導致全部被過濾掉
  * 修正：改用明確 index `r[36]`（最近出現日），排序同步修正
  * 此 bug 自 v11.18 起存在；v11.15 因只有 37 欄（`r[-1]` 剛好是 `r[36]`）不受影響

- **歷史紀錄讀取被集保 exception 拖走**
  * `update_analysis` 原本把歷史紀錄讀取與集保 `fetch_tdcc_if_needed` 包在同一個 try/except 裡
  * 集保 CSV 下載失敗時 exception 把 `hist_rows` 一起吃掉，導致 `_calc_analysis_rows` 拿到 None
  * 修正：歷史紀錄讀取移出 try/except，只有集保呼叫包在 try 裡

- **`_calc_analysis_rows` 重複打 API 可能讀到空值**
  * `update_analysis` 已讀好 `hist_rows` 但未傳入，`_calc_analysis_rows` 自己再打一次 `get_all_values()`
  * 兩次請求之間若遇 rate limit 或延遲，第二次讀到空，對照分析被跳過
  * 修正：`_calc_analysis_rows` 加 `hist_rows=None` 參數，有傳入時直接用；兩個呼叫點均傳入已讀好的資料

---

## v11.18 — 2026/06/16

### 新增

- **今日法人買超金額納入評分（`_score_net_amount`，0~8分）**
  * 計算方式：今日三法人買超張數 × 當日均價 × 1000（元）
  * 均價來源：快取中已有的 `avg_price`，不打新 API
  * 分桶：≥5億=8分 / ≥2億=6分 / ≥1億=4分 / ≥3千萬=2分 / 未達=1分
  * `ANALYSIS_HEADERS` 新增 `[37] 今日買超金額`
  * 目的：區分「法人小量試單」vs「法人大力買進」，讓大型股與小型股在同一金額尺度下公平比較

---

## v11.17 — 2026/06/16

### 修正

- **fetch-only（選項2）補上集保 CSV 更新**
  * 原本選 2 只抓資料存快取，不碰集保；選 3 寫 Sheets 時讀到的是舊資料
  * 現在選 2 完成後從歷史紀錄取出所有股票代號，呼叫 `fetch_tdcc_if_needed` 更新集保快取
  * 失敗時印警告但不中斷主流程
  * 完成訊息同步改為「含價格與融資券與集保」

---

## v11.16 — 2026/06/15

### 修正

- **集保 API 改版（`fetch_tdcc_data` 整個重寫）**
  * 舊端點 `/portal/smWeb/qryStockAjax?REQ_OPR=qryStockNo` 已改版，回傳 `{"query":null,"suggestions":[]}` 而非股東結構資料
  * 舊備用端點 `/smWeb/QryStockAjax.do` 已 404
  * 改用 opendata CSV 批次下載：`https://opendata.tdcc.com.tw/getOD.ashx?id=1-5`（全市場，每週五更新）
  * CSV 欄位：資料日期 / 證券代號 / 持股分級(1~15) / 人數 / 股數 / 占集保庫存數比例%
  * 大戶定義：持股分級 ≥ 13（持股 50 張以上）
  * 一次請求取得全市場資料，命中率大幅提升（實測 460/460）

- **`fetch_tdcc_if_needed` 簡化**
  * 舊：先 probe 一支股票試日期，再決定是否全量抓取（兩次請求）
  * 新：直接下載 CSV 比對日期，`weekly_chg` 改由程式計算（新 big_pct - 快取舊 big_pct）

### 效能

- **5日均線改批次抓取（`fetch_ma5_batch`）**
  * 舊：367 支逐支打 STOCK_DAY API + sleep（耗時數分鐘）
  * 新：新增 `fetch_ma5_batch(codes, date_str)`，抓近 9 個交易日的 STOCK_DAY_ALL（上市）+ OTC 批次行情，一次組出全部收盤序列
  * 請求次數：367 次 → 約 5~10 次，速度大幅提升
  * 月初 edge case：自動往前多抓一個月的資料確保 5 日窗口完整
  * 原 `fetch_ma5`（逐支版）保留備用，不刪除

### 待驗證

- `fetch_ma5_batch` 命中率是否與舊版相當（預期 ≥ 174/367）

---

## v11.15 — 2026/06/12

### 新增

- **集保庫存變化（待處理 #17）**
  * 新增 `fetch_tdcc_data(codes)`：打 TDCC API（`/portal/smWeb/qryStockAjax?REQ_OPR=qryStockNo`），抓大戶持股%與週變化
  * 新增 `load_tdcc_cache(ss)` / `update_tdcc_cache(ss, codes, new_data)`：讀寫「集保快取」工作表（欄位：代號 / 大戶% / 週變化 / 集保日期）
  * 新增 `fetch_tdcc_if_needed(ss, codes)`：比對快取最新集保日期，有新資料才重抓，否則使用快取（集保每週五更新）
  * 新增 `_score_tdcc(big_pct, weekly_chg)`：集保大戶評分 -5 ~ +5 分（大戶% 高且增加 → 加分，大幅減少 → 重懲）
  * 對照分析新增「集保大戶」欄 [32]（格式：`72.3%（↑0.8）`），插在「量比」之後
  * `score_stock` 納入集保評分，總分上限維持 100 分
  * API 無回應或週中查詢時自動 fallback 到快取舊值，查不到給 0 分，不中斷流程

- **SECTOR_MAP 族群重整（config.py）**
  * 新增「太陽能」族群：元晶（6443）、聯合再生（3576）
  * 新增「被動元件」族群：國巨（2327，從半導體移出）、大毅（2478）、揚博（2493）
  * 「其他」暫存區 16 支股票全數歸類：
    - 光學元件：大立光（3008）、今國光（6209）、華晶科（3059）
    - 電源供應：群電（6412）
    - 生技醫療：藥華藥（6446）
    - PCB：聯茂（6213）、環科（2413）
    - 電子代工：致伸（4915）、日電貿（3090）
    - 半導體：禾伸堂（3026）、國碩（2406）
    - IC封測：尖點（8021）
  * 族群總數：34 → 35

### 欄位索引異動（ANALYSIS_HEADERS）

| index | 欄位 | 說明 |
|-------|------|------|
| [31] | 量比 | 原 [31] 不變 |
| [32] | 集保大戶 | ★ 新增 |
| [33] | 融券趨勢 | 原 [31] → [33] |
| [34] | 5日線 | 原 [32] → [34] |
| [35] | 相對強弱% | 原 [33] → [35] |
| [36] | 最近出現日 | 原 [34] → [36] |

---

## v11.14 — 2026/06/11

### 修正

- **`fetch_market_index` tables 全空問題**
  * MI_INDEX 在盤中（16:30 前）回傳空 tables，非 API 格式問題
  * 新增 fallback：改抓 FMTQIK（月指數統計），取當日與前日收盤計算漲跌幅
  * MI_INDEX 欄位掃描改為動態找漲跌幅欄（過濾 >20% 的大數字避免誤判成交金額）

---

## v11.13 — 2026/06/11

### 修正

- **`fetch_market_index` aaData key 遺漏**
  * MI_INDEX 部分 table 用 `aaData` 而非 `data` 存資料，改為 `table.get("data") or table.get("aaData")`
  * debug 輸出升級：同時印出 `fields` 和前 2 列內容，方便下次確認

- **`fetch_ma5` 上櫃股票命中率低**
  * `_fetch_stock_day_rows` 改為先打 TWSE，失敗或無資料時自動 fallback 到 TPEX `st43_result.php`
  * TPEX 收盤欄位在 `row[2]`（TWSE 為 `row[6]`），依 source 自動切換
  * 預期命中率從 ~21% 大幅提升

---

*最後更新：2026/06/25（v11.28）*
