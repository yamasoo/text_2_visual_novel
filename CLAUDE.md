# CLAUDE.md — text_2_visual_novel 專案規則

給之後在本專案工作的 Claude Code / agent 看的規則。細節以 `docs/05_資料格式規格.md` 為準。

## 目錄權限

| 目錄 | 路徑 | 權限 | 說明 |
|---|---|---|---|
| 專案目錄 | `E:\text_2_visual_novel` | 讀寫 | 所有程式、schema、產出都寫在這裡 |
| 作品目錄 | `config.yaml` 的 `works_dir`（預設 `<專案目錄>\works`） | 讀寫 | 匯入、標註、原型、圖片；可放在專案目錄以外 |
| 書目錄 | `%USERPROFILE%\Documents\小說` | **唯讀** | 小說原稿與設定。任何程式都不得寫入、改名、刪除 |
| novel-studio | `E:\novel-studio` | **唯讀** | 只用來複製模組，複製時記錄 commit（見 `vendor/novel_studio/VENDORED.md`） |

- 寫入範圍只限專案目錄與 `works_dir`（一律經 `Config.assert_writable`）。產出放 `<works_dir>/<book_id>/`，不寫回書目錄。
- 不要把書目錄複製進專案；一律透過 `config.yaml` 的 `book_library.path` 讀取。

## 命名規則（最重要）

1. **所有 key 一律英文**：JSON/YAML 的欄位名、ID、檔名（程式會讀的）、資料夾名（程式會讀的）、enum 值、變數名、函式名，全部用 `[a-z0-9_]`，以小寫字母開頭。
2. **中文只能出現在「值」與「顯示名稱」**：例如 `"names": {"zh-TW": "艾莉・里德"}`。多語名稱用 BCP-47 語言標籤當 key（`zh-TW`、`en`、`ja`），標籤本身是英文字元，可以接受。
3. **ID 不從中文名稱產生**。角色 ID 是 `ellie_reed`，中文名放在 `names`，原文寫法放在 `aliases`。
4. **讀取 novel-studio 的來源檔時**，中文 metadata 欄位（如 `類型`、`章號`）一律經 `config/source_field_map.json` 轉成英文 key 後才進入程式。程式碼裡不得出現中文欄位名。
5. 例外：`docs/` 下的說明文件可以用中文檔名（給人看）；程式不得依賴這些檔名。

## 資料格式

- 規格：`docs/05_資料格式規格.md`
- JSON Schema：`schema/*.schema.json`
- 任何新欄位或新 enum 值，先改規格與 schema，再改程式。不要在程式裡私自加欄位。

## 工作流程

- 新對話開始時依序讀：本檔 → `docs/05_資料格式規格.md` → `docs/20_標註規則.md`。
- 專案技能原始檔在 `skills/`，安裝位置是 `.claude/skills/`（把 `skills/` 底下每個資料夾整個複製到 `.claude/skills/`）。
- 金鑰只存系統 keyring（沿用 novel-studio 的 `KEYRING_SERVICE` 機制），不寫入 `config.yaml`、不進 git。
- `config.yaml`、`build/`、`out/`、大型素材不進 git。
