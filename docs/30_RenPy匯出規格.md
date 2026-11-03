# 30 Ren'Py 匯出規格

> 版本：v0.1（2026-11-01）　狀態：**草案，S5 動工依據**。實作：`t2vn/export_renpy.py`（Claude）；周邊工具見第 8 節。
> 輸入：`works/<book_id>/` 的 `scenes/` 或 `gold/`、`cast.json`、`chapters/`、`prototype/style.json`、`assets/`（可選）。
> 規則來源：`docs/05`（命名、scene graph）。

## 1. 決定（D1–D8）

| # | 決定 | 理由 |
|---|---|---|
| D1 | 純閱讀：沒有選項、旗標、分支。遇到 `choice` 行直接報錯（v1 不支援）。 | 設計決策 |
| D2 | **台詞外部化＝劇本一律由 scene JSON 生成，不手改 .rpy**；台詞以繁中（`zh-TW`）寫在生成的 .rpy 裡，之後加語言用 Ren'Py 內建 `translate` 流程。不自建翻譯 id。 | 內建流程最穩、不需要引擎就能先寫出來 |
| D3 | 所有 Ren'Py 看得到的名稱（label、角色變數、圖片、檔名）只用 `[a-z0-9_]`；中文只在字串值。 | 設計決策 |
| D4 | 語音是**可選**的：有 `voice/VoiceMap.json` 且音檔存在才在對白前加 `voice` 語句；沒有時行為與舊版相同（純 BGM／環境音）。 | docs/33 |
| D5 | 專案用 **Ren'Py 啟動器建立的新專案** 當底（要它的 `gui.rpy`、`screens.rpy`）；匯出器只寫 `game/` 底下我們自己的檔案，並覆蓋範本的 `script.rpy`。 | 沒有 SDK 無法自產完整 GUI；覆蓋範圍小而明確 |
| D6 | 缺的素材一律由「佔位素材產生器」補成純色圖；真素材放進 `works/<book>/assets/` 後重新匯出即可取代。 | 沒美術也能完整玩過 |
| D7 | 字型由使用者提供（OFL 授權的繁中字型，如思源宋體）；匯出器複製成 `game/fonts/vn_font.<ext>` 並設定 GUI 字型；可先用字型 subset 工具縮小。 | Ren'Py 預設字型沒有中文 |
| D8 | `weather` 在 v1 不演出（沒有素材）；只用 `time_of_day` 選背景。 | 範圍控制 |
| D9 | line_id 由 scene graph 的行號決定（`<scene_id>_l<3 位>`）；匯出器只複製 VoiceMap 引用的音檔，孤兒檔不進遊戲。 | docs/33 §3.1 |

## 2. 命令列

```
python -m t2vn.export_renpy --book sample_book --chapters 1-5 --project <Ren'Py 專案資料夾> [--font <字型檔>] [--no-captions] [--no-voice] [--clean]
```

- `--project`：用 Ren'Py 啟動器「新建專案」得到的資料夾（內有 `game/`）。沒給就用 `works/<book>/build/renpy/`（獨立模式，沒有 GUI 範本，只用來測試與預覽）。
- `--chapters` 沿用標註指令的寫法；章節必須已有 `scenes/chNNN.json`（沒有就用 `gold/`，都沒有就報錯）。
- `--clean`：先刪掉上一次匯出留下的 `game/vn_*.rpy`（只刪這個前綴的檔案；其他檔案不碰）。
- 寫入一律經 `Config.assert_writable`；不改 `works/<book>/` 裡的任何檔案。

## 3. 輸出樹

```
<project>/game/
├─ script.rpy              # 覆蓋範本：label start → jump 第一章
├─ vn_theme.rpy            # 字型、字級、對話框顏色、flash 轉場、caption 畫面、環境音頻道
├─ vn_characters.rpy       # define c_<char_id> = Character(...)
├─ vn_images.rpy           # image 宣告（背景、立繪）
├─ vn_ch001.rpy …          # 每章一個檔：label chNNN（章標題卡）＋ label chNNN_scMM（每場）
├─ vn_end.rpy              # label vn_end
├─ vn_export.json          # 匯出清單（第 7 節）；給佔位素材、前置檢查、打包用
├─ fonts/vn_font.<ext>     # 有給 --font 才有
└─ images/ audio/          # 真素材（從 works/<book>/assets 複製）；佔位素材由 placeholders 工具補
   ├─ bg/<asset_id>.png
   ├─ sprite/<char_id>_<expr>.png
   ├─ bgm/<asset_id>.ogg   ambience/<asset_id>.ogg
   └─ voice/<chapter>/<speaker>_<line_id>.ogg
```

## 4. 命名

| 東西 | Ren'Py 名稱 | 例 |
|---|---|---|
| 開始 label | `start`（Ren'Py 規定） | `label start:` |
| 章 label | `chNNN` | `ch001` |
| 場 label | scene_id 原樣 | `ch001_sc03` |
| 結尾 label | `vn_end` | |
| 角色變數 | `c_<char_id>` | `c_lao_zhang` |
| 玩家內心獨白 | `c_player` | 斜體、無名字 |
| 背景圖 | `bg_<location>_<time>`（＝scene 的 `bg.asset_id`） | `image bg_scrapyard_dawn = "images/bg/bg_scrapyard_dawn.png"` |
| 立繪 | tag＝`char_id`，屬性＝`expr` | `image lao_zhang smile = "images/sprite/lao_zhang_smile.png"` |
| 位置 | Ren'Py 內建 `left` `center` `right` | `show lao_zhang smile at left` |

## 5. 語句對照

| scene graph | Ren'Py |
|---|---|
| 章開頭 | `scene black` ＋ `centered "第 N 章　章名"`（`{size=+10}` 放大） |
| 場開頭 | `scene <bg>` ＋ `with dissolve` ＋（未關閉 caption）`show screen vn_caption("地點・時段")`，之後依 `cast` 逐一 `show <char> <expr> at <pos>` |
| `bgm` 與上一場不同 | `play music "audio/bgm/<id>.ogg" fadein 1.0`（有檔才寫，否則留註解）；與上一場相同不重播；`bgm: null` 且上一場有 → `stop music fadeout 1.0` |
| `ambience` | 有檔才寫 `play ambience "..."`；沒檔略過 |
| `narration` | `"text"` |
| `dialogue` | 有配音時先 `voice "audio/voice/<chapter>/<speaker>_<line_id>.ogg"`（line_id = `<scene_id>_l<3 位行號>`），再 `c_<speaker> "text"`；`expr` 與目前顯示的不同就先 `show <speaker> <expr>`（沿用原位置）。`speaker: player` → `c_player "text"` |
| `direction` | `shake`→`with vpunch`；`flash`→`with vn_flash`；`fade_out`→`scene black` ＋ `with fade`；`fade_in`→`with fade`；`none`→略過 |
| 場結尾 | `next` 有值 → `jump <next>`；`next` 為 null → 下一章的 `chNNN`；沒有下一章 → `jump vn_end` |

字串跳脫：`\` → `\\`、`"` → `\"`、`[` → `[[`、`{` → `{{`；換行不會出現。含 `%` 的台詞只警告（不改）。
每行台詞尾端加註解 `# p<source_para>`，方便從畫面回查原文段落。

## 6. 範例（v1 輸出片段）

```renpy
label ch001:
    scene black
    centered "{size=+10}第 1 章　補囊{/size}"

label ch001_sc01:
    scene bg_scrapyard_dawn
    with dissolve
    show screen vn_caption("舊工坊・清晨")
    show lao_zhang neutral at center
    play music "audio/bgm/bgm_a.ogg" fadein 1.0
    "天亮了，工坊裡很安靜。"  # p1
    show lao_zhang smile
    voice "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg"
    c_lao_zhang "走吧。"  # p2
    jump ch001_sc02
```

## 7. `game/vn_export.json`（匯出清單）

供佔位素材產生器、前置檢查、打包使用。所有 `file` 都是相對 `game/` 的路徑，一律 `/` 分隔。

```json
{
  "schema_version": "0.1",
  "book_id": "sample_book",
  "title": "示範小說",
  "chapters": ["ch001"],
  "backgrounds": [
    {"asset_id": "bg_scrapyard_dawn", "file": "images/bg/bg_scrapyard_dawn.png",
     "location_id": "scrapyard", "time_of_day": "dawn", "label": "舊工坊", "present": false}
  ],
  "sprites": [
    {"char_id": "lao_zhang", "expr": "smile", "file": "images/sprite/lao_zhang_smile.png",
     "name": "老張", "hue": 28, "shape": "broad", "present": false}
  ],
  "audio": [
    {"asset_id": "bgm_a", "kind": "bgm", "file": "audio/bgm/bgm_a.ogg", "present": false}
  ],
  "voices": [
    {"line_id": "ch001_sc01_l002", "speaker": "lao_zhang",
     "file": "audio/voice/ch001/lao_zhang_ch001_sc01_l002.ogg", "present": true}
  ],
  "font": {"file": "fonts/vn_font.ttf", "present": true}
}
```

- `present`：這個檔案在匯出當下是否已存在於 `game/`（真素材已複製進去為 true）。
- `font` 沒給 `--font` 時為 `null`。
- `shape` ∈ `tall broad small bent round`；`hue` 0–359（來自 `cast.json`）；`time_of_day` ∈ 第 6 節 enum。

## 8. 周邊工具（外包，介面固定）

| 工具 | 模組 | 輸入 → 輸出 |
|---|---|---|
| 字型 subset | `t2vn/fontsubset.py` | 文字清單＋字型檔 → 縮小的字型檔＋缺字報告 |
| 佔位素材 | `t2vn/placeholders.py` | `vn_export.json` → 缺的背景與立繪 PNG（不覆蓋已存在的檔） |
| 前置檢查 | `t2vn/renpy_preflight.py` | Ren'Py 專案資料夾 → 錯誤／警告清單；組出打包指令 |

## 9. 驗證

1. **自動**：`tests/export_renpy/`——對《示範》第 1 章黃金集匯出，檢查語句對照、跳脫、label 唯一與 jump 目標存在、只含 ASCII 名稱；前置檢查工具對輸出零錯誤。
2. **使用者在自己的電腦**：用 Ren'Py 啟動器開專案 →「檢查腳本（Lint）」零錯誤 →「啟動專案」從頭玩到結尾（含存檔、讀檔、回顧、跳過）。
3. 打包：啟動器「建置發行版」→ Windows → 換一台電腦開得起來。

## 10. 已知限制（v1）

- 沒有 Ren'Py SDK 的環境無法實際執行；本專案的自動測試只檢查語法子集（`renpy_preflight`），**語意以使用者的 Lint 為準**。
- 不支援選項、旗標、天氣、CG、畫廊、多語言（只留 `translate` 路）。
- 語音只支援「一句一個檔案」的預生成模式；沒有即時 TTS、沒有情緒自動判斷。

## 11. 實作補充（2026-11-01，與上文有出入者以此為準）

- `--project` 可以在專案資料夾以外（使用者明示的目的地）；仍禁止寫進唯讀目錄（書庫、參考來源）。專案裡沒有 `game/` 會報錯並提示先用啟動器建立。
- `--placeholders`：匯出後順便呼叫佔位素材產生器；不論有沒有給，結尾都會跑前置檢查並印出結果（有 error 結束碼 1）。
- `fade_in` 前面若有 `fade_out`：先還原該場背景與立繪再 `with fade`（只寫 `with fade` 會停在黑場）。黑場期間不顯示立繪。
- 音訊副檔名依實際檔案（`.ogg .opus .mp3 .wav .flac`），`vn_export.json` 的 `file` 照實填；找不到檔時只留註解。場景沒寫 `bgm`／`ambience` 這個 key ＝ 沿用上一場。
- `next` 為 `end_*` → `jump vn_end`；`next` 指向沒有一起匯出的場 → 警告並 `jump vn_end`；沒有 `next` → 下一個匯出的章，沒有則 `vn_end`。
- 不在 cast.json 的說話者：角色名用 id 並警告；說話者不在場上時不會為了換表情而叫出立繪。
- 字型以 `style default / gui_text / say_label / say_dialogue / input / button_text / label_text` 設定（未在真實 GUI 範本驗證）。
- 全形空白（U+3000）保留；換行與 Tab 轉成一般空白。
- 語音（2026-11-03）：`voice/VoiceMap.json` 有該 line_id 且 `assets/voice/...` 存在時，對白前插入
  `voice "audio/voice/<chapter>/<speaker>_<line_id>.ogg"`；只複製被引用的音檔，孤兒檔不進遊戲。
  `voices` 清單在 `vn_export.json`；`--no-voice` 可完全關閉。

