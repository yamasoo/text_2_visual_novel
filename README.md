# text_2_visual_novel

把小說轉成 Ren'Py 視覺小說：匯入 → 章綱／角色抽取 → LLM 標註成 scene graph → 人工校對
→ 美術／聲音 → Ren'Py 輸出 → 驗收打包。

> 「裝什麼、怎麼跑、repo 裡有什麼」。

## 這個 repo 有什麼

| 路徑 | 內容 |
|---|---|
| `t2vn/` | 轉換管線與本機網頁工作台（FastAPI） |
| `ui/` | 工作台前端（原生 JS，無框架） |
| `schema/` | JSON Schema（scene／cast／manifest／chapter…） |
| `config/` | 來源欄位對照表、ComfyUI workflow |
| `docs/` | 規格、階段計畫、決策紀錄、任務卡 |
| `tests/` | 測試（多數不需要小說資料就能跑） |
| `vendor/` | 從 novel-studio、ruxi 複製的模組（各自保留 LICENSE 與 `VENDORED.md`） |
| `skills/` | 給 Claude Code 用的專案技能 |
| `library/sfx/` | 介面音效（Kenney，CC0） |

## 這個 repo **沒有**的東西

這些是刻意排除的，`.gitignore` 已擋住：

| 路徑 | 原因 |
|---|---|
| `works/` | 小說原文、標註、立繪、Ren'Py 建置產物（著作權與體積） |
| `config.yaml` | 本機絕對路徑設定，由 `config.example.yaml` 複製而來 |
| `reports/`、`_qa_review_2/` | 驗收截圖與暫存腳本 |
| `vendor/fonts/` | Noto Serif TC（16 MB，OFL），需要時自行下載 |
| `vendor/wildcards/` | wildcard 卡池，授權未載明，不隨 repo 散布 |

## 需要什麼

- **Python 3.10+**
- **ffmpeg** — 聲音處理（S7）
- **Ren'Py SDK 8.x** — 輸出與預檢（S5）
- **ComfyUI** — 可選，出圖（S6）
- **LLM** — DeepSeek API，或本機 llama.cpp（OpenAI 相容）

## 快速開始

```powershell
git clone https://github.com/<你的帳號>/text_2_visual_novel.git
cd text_2_visual_novel
.\start_studio.bat
```

`start_studio.bat` 每次都會自己檢查並補齊，不用手動安裝：

1. 找 `venv`／`.venv`，都沒有就用 `python -m venv venv` 建一個
2. `requirements.txt` 有變更、或匯入失敗，就 `pip install -r requirements.txt`
3. 第一次執行時從 `config.example.yaml` 產生 `config.yaml`，`project_root` 自動指到這個資料夾
4. 啟動工作台並開 <http://127.0.0.1:7866/>

跑起來之後只要改 `config.yaml` 的 `book_library.path`（小說資料夾，唯讀，每本書一個子資料夾）。

不想用 bat 的話，就等於這四行：

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python -m t2vn.config --init
venv\Scripts\python -m t2vn.server
```

服務只綁 `127.0.0.1`，並檢查 `Host`／`Origin`。

## 金鑰

金鑰只存系統 **keyring**（沿用 novel-studio 的 `KEYRING_SERVICE`），可從網頁「設定」頁
輸入與測試連線，但**不會**寫進 `config.yaml`、也不會回傳到前端。

## 測試

```powershell
venv\Scripts\pip install -r requirements-dev.txt
python -m pytest -q
```

`tests/annotate` 與 `tests/chunked` 用作者本機的 `works/sample_book` 當人工黃金集
（不進 repo）；沒有那份資料時整組 **skip**，不會失敗。
`tests/annotate/test_synthetic_book.py` 用自造的迷你書覆蓋同一條管線，
clone 之後仍會執行。

## 文件

| 文件 | 內容 |
|---|---|
| [docs/05_資料格式規格.md](docs/05_資料格式規格.md) | 目錄權限、命名規則、輸入輸出格式 |
| [docs/20_標註規則.md](docs/20_標註規則.md) | op 語法與標註規則 |
| [docs/30_RenPy匯出規格.md](docs/30_RenPy匯出規格.md) | Ren'Py 輸出結構 |
| [docs/31_美術管線規格.md](docs/31_美術管線規格.md) | 美術素材與出圖管線 |
| [docs/32_外型卡規格.md](docs/32_外型卡規格.md) | 外型卡與 wildcard |

## 相關專案

- [yamasoo/novel-studio](https://github.com/yamasoo/novel-studio) — 小說寫作工作台（MIT），本專案沿用其 provider／keyring 機制
- [zenstory-ai/novel-to-game](https://github.com/zenstory-ai/novel-to-game)（MIT）
- [swaylq/ruxi-skill](https://github.com/swaylq/ruxi-skill)（MIT）

## 授權

本專案為 MIT，見 [LICENSE](LICENSE)。

`vendor/` 下的程式與素材各自依原授權：`vendor/novel_studio/LICENSE`、
`vendor/ruxi/LICENSE`、`library/sfx/kenney_interface/LICENSE.txt`（CC0）。
