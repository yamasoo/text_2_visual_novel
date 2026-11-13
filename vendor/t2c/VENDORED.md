# vendor/t2c

從 [text2comics](https://github.com/yamasoo/text2comics)（MIT）複製的 `t2c` 套件。**不要直接修改這裡的檔案**；要改就回 text2comics 改、提交、再跑 `python tools/sync_t2c.py`。

## 來源

- 本機來源：`E:\dev\text2comics`
- commit：`d02213cbbd453000d7297d31caf646f35d4c5630`
- commit 日期：2026-11-13
- 套件版本：`1.13.0`
- 同步時工作區：無
- 目錄 digest：`0394e5e8ee4bc64691985f19a21754e82a8142aac2e6b65783cc6648fbe55dac`
- 授權：MIT，`LICENSE` 一併保留。
- 這份檔案由 `tools/sync_t2c.py` 產生，不要手改。

## 檔案

內容取自 commit，所以換行是 LF；說明欄是模組 docstring 的第一行（自動抓，不手寫）。

| 檔案 | 原路徑 | SHA-256 | 說明 | 修改 |
|---|---|---|---|---|
| `LICENSE` | `LICENSE` | `23fabeb72b973a0d12a1bd5993fb9a369a6875d27c2edc3590d7a76ee31369c4` | 授權全文 | 無 |
| `__init__.py` | `t2c/__init__.py` | `d3e2a15bbe1806c56c81337b780d8cc96a87e85447b3973b026ab6cd79d2536d` | text2comics 核心：不依賴網頁框架（只用標準函式庫、PyYAML、Pillow），之後可以整包放進 Novel Studio 或其他專案 | 無 |
| `adapt.py` | `t2c/adapt.py` | `f2729982d66150984b8d7e10677e52dad708ca2b2374b0ae5b0f8f56f5aa2929` | 文字端流程（S5）：小說一章 → 改編規劃 → 漫畫劇本 → 分鏡 JSON | 無 |
| `annotate.py` | `t2c/annotate.py` | `17d55a58609751eae75f5c6ecca2f5ba9903300d774f161a256f43cbefa41b73` | 切對話（N3）：小說一章 → 場 → 旁白／對話／演出，每一句都回指原著的段落（source_para） | 無 |
| `bible.py` | `t2c/bible.py` | `0a53ea9a7cc70b07803d88dd78b88ec7b99fb911925e885e7b0178d86ac707a9` | 視覺聖經（S6）：從小說抽角色與地點 → look_en 草稿 → 人工確認 → 定裝圖 → 服裝矩陣 | 無 |
| `bible_check.py` | `t2c/bible_check.py` | `247a159f96070cbf81a480ae213f0c3e7497b6dab7e38cce8fdd7ab70864e567` | 視覺聖經的規則檢查（S6）：角色卡、地點卡、掃描結果 | 無 |
| `bible_prompts.py` | `t2c/bible_prompts.py` | `b759b408d3c75f7c25574fa633a5e5eb60874832a9048cb031428e68fdd74f13` | 視覺聖經抽取的提示詞（S6）：原著 → ① 掃描（每章一次）→ ② 成卡（每個角色、地點一次） | 無 |
| `cast.py` | `t2c/cast.py` | `ddb8f2b7b87a3a4ae0d486bda65a071f9906af38e0f4265d4e70c6caadb4aebf` | 角色解析（微5／docs/06 決策 D9–D12）：名字、別名或 id → 角色 id，全專案只有這一份 | 無 |
| `check.py` | `t2c/check.py` | `c9b44d7cfa0594038e65b88fb5fa571e260f8f043c7d6cc4e4edf9e2e758c20f` | LLM 輸出的驗證（S5）：改編規劃、分鏡頁 | 無 |
| `comfy.py` | `t2c/comfy.py` | `653ffc64f9f99538e21bb1d0670095a468860f2a315df84f1fdde00ce37be6f5` | 最小的 ComfyUI API 客戶端（只用標準函式庫） | 無 |
| `compile.py` | `t2c/compile.py` | `4fa1f50710d58ee309e68cb46c96402281af194429fd5b483aec80326c1927c6` | 分鏡 JSON → 每一格的 MOSS-Image layout prompt | 無 |
| `compose.py` | `t2c/compose.py` | `c125738f99c96e9f6a7779e02c263c183fb36b7c19f2dc1e13a707ad933dfc2b` | 組版：把每格選好的圖裁進格子、畫框、排成一整頁 PNG | 無 |
| `default_config.json` | `config.example.json` | `a0edd4aba1d871887e1744467e944964c5b2ffe62014943367b016759dae1e15` | 授權全文 | 無 |
| `export.py` | `t2c/export.py` | `40428a48e7fedbb5aa2e476541d1975e4f9af1407dbb8ce0f9da23b54da966dd` | 輸出：把 pages/chNNN/pNNN.png 打包成 PDF 或 CBZ（日式右翻） | 無 |
| `gpu.py` | `t2c/gpu.py` | `6324a72149f1c2dce516eaebd97f2e304105dc4379128414714f6bd25019e1b7` | VRAM 排程（S7）：本機 llama.cpp 與 ComfyUI 不要同時佔 GPU | 無 |
| `importer.py` | `t2c/importer.py` | `14ca53734722828fc349b9d4229f54d32f014a0a34056fb824561a83dbbe9ae8` | 整本小說匯入 → 自動切章，寫成專案的 `source/chNNN.txt`（N2） | 無 |
| `layout.py` | `t2c/layout.py` | `618aee2fac2349d3e2ba5ebdf3d008c5e38f6271b257bbc6e15a7aef792486eb` | 頁面模板、格子尺寸、MOSS-Image 的 bbox 換算 | 無 |
| `letter.py` | `t2c/letter.py` | `9e3412adcc7a431c4e7cbc84a8807903d2f45963cd199a4ab4d9f0c15c2b985e` | 嵌字：對話框、尾巴、直排繁中、避開臉部、狀聲詞 | 無 |
| `llm.py` | `t2c/llm.py` | `68113301cf37a2507b2e7bbb3e1c61779e6e95a8d686bd258b2ab7a94dae6f14` | LLM 連線（S4）：OpenAI 相容的 Chat Completions，DeepSeek、llama.cpp（llama-server）都適用 | 無 |
| `look.py` | `t2c/look.py` | `8b7dbe946a81c36ec48f46f8a9c5f0f9289300769a27c76c2e5652b6c9aba1ee` | 角色外觀的分段（docs/06 決策記錄 D4、docs/07 微調簡報步驟 2、docs/reports/01 的「卡片要分八段」） | 無 |
| `project.py` | `t2c/project.py` | `f02aae811e134321802493c318655779ed69dd68b80a77bc3befd76d901ba086` | 漫畫專案資料夾的讀取：project.yaml、視覺聖經（角色、地點）、分鏡 JSON | 無 |
| `prompts.py` | `t2c/prompts.py` | `166e72b1ed7a7c52b7e507033939b110899da6335fa9e11e682259e4989f9ca6` | 文字端三階段的提示詞（S5）：改編規劃 → 漫畫劇本 → 分鏡 JSON | 無 |
| `render.py` | `t2c/render.py` | `264c97edcd8e647b241d536a12fe313f2ea0f7d40502c2388fdcf8dc3f1f41a0` | 逐格出圖（S7）：compile_chapter 的工作 → ComfyUI → renders/chNNN/<格 id>_s<seed>[_<預設>][__<profile>].png | 無 |
| `reverse.py` | `t2c/reverse.py` | `4da1c1422b0c31585156c5b85571e57f962bec3995cfe4349152b64747753869` | N5 反推（排程層）：完成的稿子 → 逐章抽取 → 本機累積 → 視覺聖經草稿 | 無 |
| `script.py` | `t2c/script.py` | `d0a7ce318c098b2ef3f3283ef2d2dd0a7daa68b7836a379b17f6392db98b3503` | 漫畫劇本 `script/chNNN.md` 的解析與檢查（S5） | 無 |
| `settings.py` | `t2c/settings.py` | `e2e2349ed6000b46866e1af28b3205b4a69cb6421c1e0f8fe9946088bee2d9a0` | 工具本身的設定：config.json（不進版控）；沒有就用 config.example.json | 無 |
| `stale.py` | `t2c/stale.py` | `b6fc2d20f198133a2816dbc11b462ca783228797aafcc18e05ca89fb102ac700` | 帳單軌（docs/reports/01_build_book_flow.md、docs/06 D1）：哪幾格因為卡片或畫風改了而過期 | 無 |
| `styles.py` | `t2c/styles.py` | `c4191d4c0270830acb12cc03320e3db37882481a10471c4847aa8f796a6209ce` | 畫風預設集（N5）：黑白／彩色／美式三組，style、meta 與文字端的畫風描述（look）只有這一份來源 | 無 |
| `workspace.py` | `t2c/workspace.py` | `b25a298e542db85443303919b6feed207b00937df5d1bcd11e1ce76d79fa1490` | 介面與命令列共用的專案操作（S7）：總覽、新建專案、讀寫原著／規劃／劇本／分鏡、每格的出圖狀態、加畫用的新 seed | 無 |

## 資料檔

套件自帶的出圖資料，`t2c/data/` 原樣搬過來。`layout.load_pages()` 與 `comfy.load_workflow()` 讀的就是這裡；檔案不在就整條出圖線停擺。

| 檔案 | 原路徑 | SHA-256 |
|---|---|---|
| `data/anima.api.json` | `t2c/data/anima.api.json` | `4f42a2ed180e6b9a6933a5b92d0b44d997d42bade38e89650a01dcd7831e4ad5` |
| `data/moss_layout.api.json` | `t2c/data/moss_layout.api.json` | `58404479602a3693b942dd73c4537ab40a2044664e9d46ba9f34ffdde58b786a` |
| `data/pages.json` | `t2c/data/pages.json` | `418b457b826010cffcb983a07a0ce8f163df0c17c5f1f0d42422965f25b5844a` |
| `data/styles.json` | `t2c/data/styles.json` | `34146fad41614dbca67e90fa4a17ed895ca21c234dde02545e6927abb4d8e543` |

## 沒有搬過來的東西

- `t2c/vendor/`：不搬第二份 fekit（整合計畫的風險 R5）。

## 更新流程

1. 在 text2comics 改 `t2c/`，跑它的測試，提交。
2. 在這裡跑 `python tools/sync_t2c.py`。
3. 它會重寫這份檔案，然後自動跑 `tools/comic_check.py`；綠燈才提交。

同步讀的是 commit，不是工作區。text2comics 有未提交的修改時，這裡搬的仍是最後一次提交；
上面「同步時工作區」那一行會寫出來。
