# vendor/novel_studio

從 [yamasoo/novel-studio](https://github.com/yamasoo/novel-studio)（MIT）複製的模組。**不要直接修改這裡的檔案**；要改就在本專案另寫包裝，或先在 novel-studio 改再重新複製。

## 來源

- 本機來源：`E:\novel-studio`，HEAD `c58af13ea0af0cc139ee1c847c0e539a8fd8e2e0`
- 複製日期：2026-11-01
- 複製時比對：三個 `.py` 與 GitHub `yamasoo/novel-studio` 的內容完全相同（逐檔 `diff`）。本機工作目錄是否另有未提交的修改無法確認，但這三個檔案沒有差異。
- 授權：MIT，`LICENSE` 一併保留。

## 檔案

| 檔案 | 原路徑 | SHA-256 | 說明 | 修改 |
|---|---|---|---|---|
| `comfy.py` | `studio/comfy.py` | `6a7c6ea733144dcbfe384c629b5614bcfd16948c76c6cfd30063dded0f4d92ab` | ComfyUI API、workflow 分析與改寫、LoRA 注入 | 無 |
| `providers.py` | `studio/providers.py` | `27c3949c9739c497dec77ea69b112a2bb5a51bf10d5174a3da9cd934fa85622c` | LLM provider 抽象（DeepSeek、llama.cpp、OpenAI 相容）、金鑰存取 | 無 |
| `importer.py` | `studio/importer.py` | `2bc243c8b0860b711120d7c7fbe3e37d1ff9b43e223e4e3744e8c73ddac4a1d1` | 匯入舊稿：TXT／MD／docx 解碼、簡轉繁、依標題拆章、段落整理（本專案只用其純函式，不用 `commit`） | 無 |
| `lang.py` | `studio/lang.py` | `854ac8e2d3115757567ad7bc48c3887ed4f218024303515e755e207e0b085f33` | 語言判斷、字數計算 | 無 |
| `LICENSE` | `LICENSE` | `73c7f61ac81ffbd8d366b6ce7a7194cb064f2eb1b463324900c933d7a1bfd406` | MIT 授權 | 無 |

## 使用注意

- 三個模組都只用標準庫；`providers.py` 的 `keyring` 是選用依賴（沒裝就不能存金鑰）。
- `providers.py` 的 `KEYRING_SERVICE = "novel-studio"`：本專案**刻意沿用**，這樣 novel-studio 已存的 DeepSeek 金鑰不用重設。
- 這些檔案裡有中文 key（novel-studio 的舊習慣）。`tools/lint_rules.py` 排除 `vendor/`，本專案自己寫的程式不可以這樣。
- `vendor/novel_studio/project.py` 是**本專案自寫的替身**（只有 `ProjectError` 與 `count_chars`），讓 `importer.py` 能不修改直接 import；不在上表、沒有 hash。
- 本專案不使用 novel-studio 的 `project.py`（讀書資料）：它的 key 是中文，且和 novel-studio 的資料夾模型綁在一起。讀書資料用 `t2vn/source_reader.py`。

## 更新流程

1. 在 `E:\novel-studio` 確認要複製的 commit。
2. 重新複製檔案，更新上表的 SHA-256 與本檔的 commit。
3. 跑 `pytest tests/vendor/`。
