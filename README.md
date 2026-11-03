# text_2_visual_novel

**把一本小說，變成一個可以玩的視覺小說遊戲。**

你給它小說文字，它幫你把每一段拆成旁白、台詞、說話者與表情，讓你在瀏覽器裡逐句校對，
再產出立繪、背景、音樂，最後輸出一份可以直接開來玩的 Ren'Py 專案。

這是一套「人在迴路」的工具：AI 做粗活，你只做判斷。標註完一定要校對，程式不會替你決定
誰在說話——它只會把沒把握的地方標出來。

![《示範》示範遊戲畫面](docs/images/readme/sample_scene.jpg)

*《示範》第 1 章：立繪、背景、對話框與語音都是這套管線產出的實驗成果。*

---

```mermaid
flowchart LR
  A[小說 TXT / DOCX / EPUB] --> B[拆章、角色表]
  B --> C[標註 scene graph]
  C --> D[人工校對]
  D --> E[立繪 / 背景 / BGM]
  D --> F[sherpa-onnx 語音]
  E --> G[Ren'Py 專案]
  F --> G
```

## 先玩看看

[下載 Windows 試玩版](https://github.com/yamasoo/text_2_visual_novel/releases/latest)（約 96 MB）

解壓後進入資料夾，執行裡面的 `.exe`。不需要安裝 Ren'Py 或 Python。

這是《示範》的實驗章節，不是完整商業版；Windows 第一次執行可能出現 SmartScreen 警告。

## 它能幫你做什麼

| 階段 | 你做什麼 | 它做什麼 |
|---|---|---|
| **匯入** | 選小說資料夾，或直接丟 TXT／Markdown／DOCX／EPUB | 自動拆章、編段落號 |
| **理解** | 按一個鍵 | 讀出章綱、情節點、角色表（誰是誰、是什麼身分） |
| **標註** | 去泡杯茶 | 把每章變成場景與台詞：分場、旁白、說話者、表情、演出效果 |
| **校對** | 左邊原文、右邊劇本，逐句修 | 標出推斷的說話者（`?`）、漏掉的敘述、不逐字的改寫 |
| **美術** | 在「素材」頁挑風格、按生成 | 用 ComfyUI 出生立繪與背景，自動去背、對齊、命名 |
| **聲音** | 選氣氛、按生成 | 產生 BGM 與環境音；可選 **sherpa-onnx 固定聲線**逐句配音（CPU、不需 ComfyUI），輸出 OGG 與 `VoiceMap.json` |
| **輸出** | 填一次 Ren'Py SDK 路徑 | 產生可執行的 Ren'Py 專案，並檢查缺素材、字型缺字 |
| **驗收** | 按「開始驗收」 | 打包成 QA 包，用截圖驗證畫面與流程 |

## 你會得到什麼

- **可以玩的遊戲** — Ren'Py 專案，開起來就能跑，也能打包成 Windows 執行檔
- **校對過的劇本** — `works/<書>/scenes/chNNN.json`（機器可讀，之後要改編或重出都靠它）
- **給人看的校對表** — `scenes/chNNN_review.md`，可以直接拿給別人審
- **素材庫** — 立繪、背景、BGM、音效，統一命名與清單管理
- **可重複的流程** — 同一本書改幾句、重跑一章，不用從頭來

![《示範》示範主選單](docs/images/readme/sample_menu.jpg)

*同一個 Ren'Py 專案的主選單；背景圖也是管線產出的素材。*

## 需要什麼

**必備**

| 項目 | 說明 |
|---|---|
| Windows 10／11 | 目前只在 Windows 實測過 |
| Python 3.10 以上 | 先確認電腦已安裝；不需要重裝，也不要覆蓋既有 Python |
| 小說文字 | TXT／Markdown／DOCX／EPUB；程式**只讀不寫**，不會動到你的原稿 |
| 一個 LLM | [DeepSeek API 金鑰](https://platform.deepseek.com/)，或本機跑的 llama.cpp |

**選配（沒有也能用，只是少幾步）**

| 項目 | 什麼時候需要 |
|---|---|
| ComfyUI ＋ 顯示卡 | 想自動生立繪／背景、BGM 時 |
| `sherpa-onnx`（`requirements-tts.txt`）＋約 116 MB 中文模型 | 想用 sherpa-onnx 固定聲線配音時（CPU，不需要 ComfyUI） |
| ffmpeg | 想處理音樂音效、或把語音轉 OGG 時 |
| [Ren'Py SDK 8.x](https://www.renpy.org/latest.html) | 想輸出成遊戲時 |

**資源量級**：一本 20 萬字的小說，作品資料夾大約 1–3 GB（多數是圖片）。
顯示卡用於出圖，沒有也可以先做劇本、之後再補圖。

### 先檢查環境（已安裝可跳過）

先開 PowerShell 確認版本；**不要為了這個專案重裝 Python**，以免影響其他環境：

```powershell
python --version
git --version
ffmpeg -version
```

如果缺少：

- Python 3.10+：<https://www.python.org/downloads/windows/>（安裝時勾「Add Python to PATH」；已有 Python 就跳過）
- Git：<https://git-scm.com/download/win>
- ffmpeg：<https://ffmpeg.org/download.html>
- Ren'Py SDK 8.x：<https://www.renpy.org/latest.html>

`start_studio.bat` 只會在專案內建立 `venv\`，不會把套件裝進你的全域 Python。

**語音模型不在 GitHub**，第一次使用請跑：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-tts.txt
.\venv\Scripts\python.exe tools\sherpa_get_model.py vits-zh-hf-fanchen-C
```

完整步驟見 [36_語音快速開始.md](docs/36_語音快速開始.md)。

## 怎麼開始

```powershell
git clone https://github.com/yamasoo/text_2_visual_novel.git
cd text_2_visual_novel
.\start_studio.bat
```

第一次執行會自動建虛擬環境、裝好套件、產生 `config.yaml`，然後打開
<http://127.0.0.1:7866/>。

打開後到 **設定** 頁，用「作品存放位置」和「書庫位置」的 **瀏覽…** 直接選資料夾即可，
**不需要手動編輯 `config.yaml`**。

只有在設定頁無法開啟時，才需要手動檢查 `config.yaml` 的 `book_library.path`。

接著在網頁上：

1. **書架** → 匯入一本書
2. 勾章節 → **抽取章綱與角色**
3. 勾章節 → **AI 標註**（背景執行，可以看進度和花費）
4. **章節** → 打開校對，把 `?` 和紅色提示修掉
5. 想先看看 → **建立可玩原型**（HTML，不用 Ren'Py）
6. 想配音 → 先照 [語音快速開始](docs/36_語音快速開始.md) 下載 sherpa 模型 → **語音**頁填聲線 → 生成 → 重新匯出
7. 要正式版 → 設定 Ren'Py 路徑 → **匯出**

## 費用大概多少

成本幾乎都在 LLM 的 token。以 DeepSeek 的便宜模型計算，一章通常是幾萬 token，
全書數十萬 token，換算下來是「一杯飲料到一頓飯」的量級；如果重試很多次會再往上。
用本機 llama.cpp 則完全免費，代價是慢、而且需要夠大的顯示記憶體。

金鑰只存在 Windows 憑證管理員（keyring），從網頁「設定」頁填入，**不會寫進檔案、不會上傳**。

## 常見問題

**Q：一定要有 API 金鑰嗎？**
不一定。可以先用本機模型（llama.cpp）跑小章節試水溫；也可以只匯入、只校對，不呼叫模型。

**Q：它會改我的原稿嗎？**
不會。小說資料夾是唯讀的，所有產出都寫在 `works/`。

**Q：標註完可以直接用嗎？**
強烈建議校對。AI 很會抓場景與對白，但「這句話是誰說的」常常需要人判斷，所以介面會把
沒把握的行標成 `?`。

**Q：可以只做劇本、不做美術嗎？**
可以。輸出時缺的素材會用佔位圖補上，先能玩，之後再補。

## 這個 repo 裡沒有什麼

為了尊重原稿與授權，以下**不會**出現在 repo 裡（`.gitignore` 已排除）：

- `works/` — 小說原文、標註結果、圖片、遊戲建置產物
- `config.yaml` — 你自己的本機路徑設定
- `vendor/fonts/`、`vendor/wildcards/` — 第三方素材，需要時自行下載
- `models/` — sherpa-onnx 與 ComfyUI 的模型權重，檔案太大，請照文件下載

## 想更深入了解

| 文件 | 內容 |
|---|---|
| [docs/05_資料格式規格.md](docs/05_資料格式規格.md) | 資料夾權限、命名規則、輸入輸出格式 |
| [docs/20_標註規則.md](docs/20_標註規則.md) | 旁白／台詞怎麼拆、說話者怎麼判斷 |
| [docs/30_RenPy匯出規格.md](docs/30_RenPy匯出規格.md) | Ren'Py 專案結構與命名 |
| [docs/31_美術管線規格.md](docs/31_美術管線規格.md) | 立繪、背景與出圖流程 |
| [docs/32_外型卡規格.md](docs/32_外型卡規格.md) | 角色外型與 wildcard 卡池 |
| [docs/34_sherpa模型清單.md](docs/34_sherpa模型清單.md) | sherpa-onnx 中文／粵語模型與下載 |
| [docs/36_語音快速開始.md](docs/36_語音快速開始.md) | 從安裝、下載模型到生成語音的完整指令 |

想跑測試或參與開發：`pip install -r requirements-dev.txt` 後執行 `pytest`。

## 授權

本專案為 MIT，見 [LICENSE](LICENSE)。
`vendor/` 下的程式與素材各自依原授權（`vendor/novel_studio/LICENSE`、`vendor/ruxi/LICENSE`、
`library/sfx/kenney_interface/LICENSE.txt`）。
