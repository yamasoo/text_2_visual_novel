# vendor/ruxi

從 [swaylq/ruxi-skill](https://github.com/swaylq/ruxi-skill)（入戲，MIT）複製的工具。**不要直接修改這裡的檔案**；本專案的修改全部寫在 `t2vn/prototype.py`（建置時套用，見下方「本專案的修改」）。

## 來源

- GitHub `swaylq/ruxi-skill`，commit `f33eed8bc580c2106d0ef7b30eea4bd0dfdb9798`（2026-09-01）
- 複製日期：2026-11-01
- 授權：MIT，`LICENSE` 一併保留。

## 檔案

| 檔案 | 原路徑 | SHA-256 | 說明 | 修改 |
|---|---|---|---|---|
| `build_html.py` | `tools/build_html.py` | `d42b80f9ddfaaa241f27046a51a973721b0507c5cb4558cd887d3d361edd798c` | script.json 驗證、注入模板 | 無 |
| `walk_script.py` | `tools/walk_script.py` | `1150626258cd3ff1293c975e5bb50297cd993b479605c0587518f47ecedec087` | 窮舉所有路徑、檢查結局可達 | 無 |
| `scan_scenes.py` | `tools/scan_scenes.py` | `62c71accd921f65866a8771e6d34896c2007d4224f124324e88442f361734d9a` | 掃描全書找「戲眼」（選戲用） | 無 |
| `template.html` | `tools/template.html` | `781877963021912fdfbeed734678315fde00bc816086ac15223a7b90f0a5da95` | 單檔遊戲模板（剪影立繪、打字機、回想、存檔） | 無 |
| `script-schema.md` | `references/script-schema.md` | `403455536ae9daebccbfa8ac95bf15a6a64d008dafe19595c215f13e830f304e` | script.json 格式說明（簡體中文） | 無 |
| `LICENSE` | `LICENSE` | `5dcefdfaa3da7524db5dc01332455afcc6b13e88cfd8afbbedd0e3d83d2f2f1b` | MIT 授權 | 無 |

## 本專案的修改（在 `t2vn/prototype.py`，不改這裡的檔案）

| 問題 | 處理 |
|---|---|
| 終端機輸出 ✓ ⚠ 在 Windows（cp950）會崩潰 | 不直接跑這些 CLI；改用 `python -m t2vn.prototype`，入口先 `sys.stdout.reconfigure(encoding="utf-8")`。真的要直接跑，先設 `PYTHONUTF8=1`。 |
| 模板介面是簡體中文、字型是簡體優先 | 建置時套用 `TEMPLATE_PATCHES`：介面字串轉繁體、`lang="zh-Hant-TW"`、字型改 Noto Serif TC 優先（Google Fonts，離線時退回系統繁中字型）。每條補丁都要求原字串恰好出現一次，上游改版會直接報錯。 |
| 沒有分支的作品，結算頁會出現空的「你讀懂了多少」 | 補丁：`verdict` 為空時不顯示該標題。 |
| `pd_status` 只有公有領域與「有版權・僅限本地」 | 加 `author-owned`（作者自己的書），建置時擴充 `build_html.PD`。 |
| 「5–8 場」的限制 | 只寫在 ruxi 的 SKILL.md／prompts，程式沒有硬限制，不用改。 |
| `build()` 用 macOS 的 `sips` 壓縮圖片 | 本專案只用 `validate()`，注入模板自己做；圖片（素材階段）之後另外處理。 |
| 模板只有兩個立繪位 | 保留；同場三人時輪替上台，原型階段可接受。 |

## 更新流程

1. 確認要複製的 commit，重新複製檔案，更新上表的 SHA-256 與 commit。
2. 跑 `pytest tests/vendor/ tests/prototype/`（補丁對不上會失敗）。
