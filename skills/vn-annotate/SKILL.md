---
name: vn-annotate
description: 把 text_2_visual_novel 正規化後的章節（works/<book_id>/chapters/chNNN.json）標註成 scene graph（scenes/chNNN.json）：分場、旁白／台詞、說話者、表情、演出欄位。要標註章節、校對標註、或寫標註用的提示詞時使用。
---

# vn-annotate：章節標註

規則正本是 `docs/20_標註規則.md`，格式正本是 `schema/scene.schema.json`。本技能是操作流程；兩者衝突時以規則檔與 schema 為準。

## 開始前必讀

1. `docs/20_標註規則.md`（分場、台詞拆法、說話者判斷、表情對照表）
2. `works/<book_id>/cast.json`（只能用裡面的 `char_id`，外加 `player`）
3. 該章的章綱（`source_reader.read_chapter_outline`），用「情節點」當分場參考
4. 範例：`works/sample_book/gold/ch001.json`（人工定稿的黃金集）

## 自動管線（S4）

大量標註用程式，不要手工：

```
python tools/normalize_book.py --book-dir <書目錄> --book-id sample_book --out works/sample_book --chapters 1-10
python -m t2vn.annotate --book sample_book --chapters 2-10            # 呼叫 DeepSeek
python -m t2vn.annotate --book sample_book --chapters 2 --dry-run     # 只看提示詞
python -m t2vn.annotate --book sample_book --chapters 2 --replay works/sample_book/scenes/_raw/ch002_r1.json --force
```

產出 `scenes/chNNN.json` 與 `scenes/chNNN_review.md`。改規則時 `docs/20_標註規則.md` 與 `prompts/annotate_system.md` 要一起改。下面的手工流程用於校對、修正，或沒有 API 時。

## 手工流程

1. **分場**：通讀全章，依地點／時間／焦點對象切場，邊界以段落為單位。先列出「場號：段落範圍：一句話內容」再動手。
2. **角色**：找出所有說話的人。`cast.json` 沒有的新角色先停下來補 `cast.json`（ID 手動取，見規則第 6 節），不要用臨時 ID。
3. **逐段轉換**：依規則第 2 節。敘述保留原文、只在句末斷句；台詞去引號；夾在兩截話中間的動作移到台詞前；純說話標記刪掉。
4. **說話者與表情**：依規則第 3、4 節。原文沒有直接標明、靠輪替推斷的行，記下來給校對者。
5. **演出欄位**：`location_id`、`bg`、`bgm`、`ambience` 用描述性英文 ID；`weather` 原文沒寫就不填。
6. **驗證**（全部要過）：
   ```
   python -m t2vn.schema_check works/<book_id>/scenes/chNNN.json --kind scene --cast works/<book_id>/cast.json
   ```
   再自查：每個段落都至少出現在一行的 `source_para`；`source_para` 遞增不亂序；每行 ≤ 90 字；旁白的每個子句都能在原段落找到。

## 黃金集範例（`works/sample_book/gold/ch001.json` 節錄）

原文第 4 段：`「好了，」她拍拍工具箱，朝老張喊，「試一趟看看，升力回得來就沒問題。」`

```json
{ "type": "narration", "text": "她拍拍工具箱，朝老張喊。", "source_para": 4 },
{ "type": "dialogue", "speaker": "ellie_reed", "expr": "neutral", "text": "好了，試一趟看看，轉速回得來就沒問題。", "source_para": 4 }
```

原文第 20 段（有「她想」）：`……大概是換零件的時候手上沾到的，她想，把手在圍裙上擦了兩下。`

```json
{ "type": "dialogue", "speaker": "player", "text": "大概是換零件的時候手上沾到的。", "source_para": 20 },
{ "type": "narration", "text": "她把手在圍裙上擦了兩下。", "source_para": 20 }
```

原文第 43 段（打圓場）：`老張……一手按在陳姓船主的肩膀上，「阿弟，你少講兩句。……」` → `lao_zhang`、`embarrassed`。

原文第 37 段（工作架突然震動）：在那句旁白後加 `{ "type": "direction", "effect": "shake" }`。

## 交給校對者時

產生校對表（每行：編號 `scNN-NN`、段、類型、說話者中文名、表情中文、內容、註記），輪替推斷的行註記 `?`，內心獨白註記「內心獨白」。校對者回覆編號＋改法即可。

## 不要做

- 不要改寫或濃縮旁白（那是改編，不是標註）。
- 不要猜天氣、不要為只被提到的人加 `cast`。
- 不要在程式或 JSON key 裡用中文（中文只在 `text` 與 `names` 的值）。
