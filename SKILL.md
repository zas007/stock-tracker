---
name: stock-tracker-session
description: 開始或結束 stock-tracker 專案的工作 session。開工時：讀取使用者上傳的檔案並摘要現況。結束時：整理今日所有修改，產出 CHANGELOG 新增區塊與 git commit 指令。當使用者說「開工」、「開始看 stock-tracker」、「讀取專案」、「結束」、「收工」、「整理今天修改」、「放上 git」時，使用這個 skill。
---

# Stock Tracker Session Skill

協助管理 stock-tracker 專案的工作 session，分為「開工」和「收工」兩個階段。

---

## 開工流程

使用者說「開工」或要開始看專案時執行。

### Step 1：讀取核心檔案

使用者需上傳以下檔案（直接拖曳到對話）：

```
fetch_and_update.py
config.py
CHANGELOG.md
專案備忘錄.md   ← 選擇性，有需要時再上傳
```

若使用者說「開工」但沒有上傳檔案，提示：
> 請把 `fetch_and_update.py`、`config.py`、`CHANGELOG.md` 拖進來，我來讀取最新版本。

### Step 2：摘要現況

讀取完成後，輸出以下摘要：

1. **目前版本**：從 CHANGELOG.md 找最新版本號與日期
2. **主程式概況**：fetch_and_update.py 的 Step 流程（Step 1~5）與各工作表
3. **config.py 概況**：SECTOR_MAP 族群數量與成員數、CODE_NAME_MAP 筆數
4. **待處理事項**：從 專案備忘錄.md 的「⚠️ 待處理事項」表格列出（若有上傳）

格式簡潔，讓使用者快速掌握現況即可。

---

## 收工流程

使用者說「收工」、「整理今天修改」、「放上 git」時執行。

### Step 1：整理本次 session 的修改

從對話紀錄中彙整今天做了哪些改動，分類為：
- **修正（fix）**：bug 修正
- **新增（feat）**：新功能
- **調整（refactor/chore）**：重構或雜項

### Step 2：產出 CHANGELOG 新增區塊

格式對照現有 CHANGELOG.md 的版本區塊：

```markdown
## v{版本號} — {YYYY/MM/DD}

### 修正 / 新增 / 調整

- **{修改項目標題}**
  * 舊問題/舊邏輯：...
  * 新邏輯：...
  * 效果：...
```

版本號規則：
- 僅修正 bug → patch（如 v10 → v10.1，v10.1 → v10.2）
- 新增功能 → minor（如 v10 → v11）

### Step 3：產出 git 指令

```bash
cd ~/Documents/Z/study/stock
git add -A
git commit -m "{type}({版本號}): {一行摘要}"
git push origin main
```

commit message 格式：
- type：`fix` / `feat` / `refactor` / `chore`
- 第一行不超過 72 字元

### Step 4：提示使用者

說明需要手動完成的部分：
- 將 CHANGELOG 新增區塊貼入本機 `CHANGELOG.md` 最上方
- 確認 `fetch_and_update.py` 已是最新版（本次對話產出的版本）
- 執行 git 指令推上 GitHub

---

## 注意事項

- 開工後將三個檔案的關鍵內容記在對話 context 中，避免後續需要重複詢問
- 收工時若本次 session 沒有任何修改，直接說明「本次無異動，無需 commit」
- 版本號必須在程式碼 docstring、CHANGELOG.md、git commit message 三處保持一致
