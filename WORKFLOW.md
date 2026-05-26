# 開發工作流程規範

---

## 每次對話結束時，Claude 必須提供

1. **更新後的檔案**（fetch_and_update.py、config.py 等）
2. **更新後的 CHANGELOG.md**
3. **對應的 git commit 指令**，格式固定如下：

```bash
git add -A && git commit -m "vXX 說明" && git push
```

commit message 對應 CHANGELOG 的版本號與說明，例如：
```bash
git add -A && git commit -m "v11 新增XX族群、調整籌碼集中度門檻" && git push
```

---

## 每次開工（含開新對話）

1. 打開 `專案備忘錄.md`，確認「⚠️ 待處理事項」區塊，有高優先度項目先處理
2. 開新對話時貼以下 raw 連結，讓 Claude 讀取最新版本後再開始開發：

```
https://raw.githubusercontent.com/zas007/stock-tracker/main/fetch_and_update.py
https://raw.githubusercontent.com/zas007/stock-tracker/main/config.py
https://raw.githubusercontent.com/zas007/stock-tracker/main/CHANGELOG.md
https://raw.githubusercontent.com/zas007/stock-tracker/main/WORKFLOW.md
https://raw.githubusercontent.com/zas007/stock-tracker/main/專案備忘錄.md
```

---

## 版本號規則

- 每次對話產生的修改，統一為一個版本號（vXX）
- 版本號必須在以下三個地方保持一致：
  - 程式碼 docstring 第一行
  - CHANGELOG.md 對應區塊標題
  - git commit message

---

## 檔案結構

```
~/Documents/Z/study/stock/
├── fetch_and_update.py   ← 主程式
├── config.py             ← 設定檔（族群、門檻、名稱對照）
├── CHANGELOG.md          ← 版本修改紀錄
├── WORKFLOW.md           ← 本檔案，開發流程規範
├── 專案備忘錄.md         ← 待處理事項、欄位說明、執行流程
├── README.md             ← 專案說明
├── 每日更新.sh            ← 手動執行腳本
├── credentials.json      ← ⛔ Google API 金鑰（不推 git）
└── log.txt               ← ⛔ 執行記錄（不推 git）
```

---

## GitHub Repo

`https://github.com/zas007/stock-tracker`（private）

*最後更新：2026/05/26（v10.6）*
