---
name: vn-task-card
description: 為 text_2_visual_novel 寫「模組任務卡」與驗收測試，把單一程式模組交給外部 AI（DeepSeek 等）實作。要外包模組、寫任務卡、交給 DeepSeek 或其他模型時使用。
---

# vn-task-card：寫模組任務卡

目的：讓外部 AI 在**看不到 repo** 的情況下，也能只靠一張卡片寫出合格的模組；並讓驗收客觀（測試通過與否）。

## 開始前必讀

1. `CLAUDE.md`（權限與命名規則）
2. `docs/05_資料格式規格.md`（資料格式）
4. 相關 `schema/*.schema.json`

## 步驟

### 1. 判斷能不能外包

全部符合才外包，否則告訴使用者這個模組要由 Claude 做，並說明原因：

- [ ] 輸入輸出能用 schema 或範例講清楚
- [ ] 能用 pytest 驗收
- [ ] 只新增或修改 1–2 個檔案
- [ ] 不改 schema、`docs/05`、`CLAUDE.md`
- [ ] 不是跨模組契約（`source_reader.py`、`export_renpy.py`、provider 設定）
- [ ] 不涉及改編決策

### 2. 先寫測試

- 放在 `tests/<module_name>/test_<module_name>.py`。
- 測試資料放 `tests/<module_name>/fixtures/`，用**合成文字**，不用小說原文。
- 至少涵蓋：正常輸入、邊界情況（空檔、標題格式不同、全形符號）、錯誤輸入（要報錯的情況）。
- 先在本機跑一次，確認測試會失敗（因為還沒實作），而且失敗原因是「找不到模組／函式」，不是測試本身寫錯。

### 3. 寫任務卡


- `task_id`：英文小寫＋底線，格式 `t_<stage>_<module>`，例如 `t_s1_normalize_book`。
- **完整貼上**要用到的 schema 片段與範例 JSON，不要只寫「見 schema/xxx」。
- **完整貼上**測試檔內容與 fixtures，外部 AI 才能自己跑。
- 寫明允許使用的套件（預設只用標準庫；需要第三方套件時列出版本）。
- 寫明「只能建立或修改這些檔案」。

### 4. 登記


### 5. 交付給使用者

告訴使用者：
- 任務卡路徑；
- 交法：使用者把卡片整份貼到 DeepSeek harness。harness 不保證會讀 `CLAUDE.md` 或專案技能，所以卡片必須能單獨使用；
- 交回的檔案放進 `E:\text_2_visual_novel`（harness 直接寫入，或使用者貼回）；
- 交回後用 `vn-review-external` 審查。

## 卡片必含的限制（每張都要原文寫入）

- 所有 key、ID、變數名、函式名、程式會讀的檔名只用 `[a-z0-9_]`；中文只能出現在字串值。
- 讀寫檔案一律明確指定 `encoding="utf-8"`；CLI 輸出開頭加 `sys.stdout.reconfigure(encoding="utf-8")`。
- 不寫死任何本機路徑；路徑由參數或 `config.yaml` 傳入。
- 不連網（除非任務本身就是 API 呼叫，且有寫明）。
- 不寫入書目錄；只寫入參數指定的輸出路徑。
- Python 3.10 以上；型別註記；函式要有 docstring（繁體中文可）。
