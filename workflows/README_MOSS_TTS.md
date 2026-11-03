# MOSS-TTS workflow（手動測試用）

這裡有四份 workflow：

| 檔案 | 格式 | 用途 |
|---|---|---|
| `moss_tts_clone_ui.json` | ComfyUI 畫布（UI） | 參考音克隆／一般 TTS，拖進 ComfyUI 就能看到節點 |
| `moss_tts_design_ui.json` | ComfyUI 畫布（UI） | Voice Design（VoiceGenerator） |
| `moss_tts_clone_api.json` | ComfyUI API | 後端 `/prompt` 用；`t2vn.voice_gen` 預設模板 |
| `moss_tts_design_api.json` | ComfyUI API | 同上，Voice Design |

## 手動測試（UI）

1. 重開 ComfyUI，確認節點 `MOSS-TTS Model Loader`、`MOSS-TTS Generate`、`MOSS-TTS Voice Design` 有載入。
2. 把 `moss_tts_clone_ui.json` 拖進 ComfyUI 畫布。
3. `LoadAudio` 的檔案如果不存在，按資料夾圖示上傳一段 **3–10 秒、乾淨、單一說話者** 的參考音。
4. 在 `MOSS-TTS Generate` 填：
   - `language`: `zh`
   - `text`: 要唸的台詞
   - `max_new_tokens`: 先用 **256**（太短會把句子截斷；太長會很慢）
   - `temperature`: `1.0`、`top_p`: `0.95`、`top_k`: `50`、`repetition_penalty`: `1.1`
5. `SaveAudioAdvanced` 的 `format` 用 `flac`，按 Queue。
6. 聽輸出。如果句子被截斷，把 `max_new_tokens` 加到 384／512；如果聲音不像人，先換一段乾淨參考音，或改用 Voice Design 重新做參考音。

Voice Design 用 `moss_tts_design_ui.json`：`Model Loader` 選 `MOSS-VoiceGenerator`，填 `text` 與 `instruction`（例如「低沉、緩慢的中年男性，說中文。」），先做出一段 4–8 秒參考音，再拿它去 Clone。

## 直接送 API（不開畫布）

```powershell
# 先上傳參考音到 ComfyUI 的 input（把 <檔名> 換成你的檔名）
# 然後用 Python 送 API；t2vn.voice_gen.VendorTtsBackend 會幫你上傳與等待
python -c "from t2vn import voice_workflow; from t2vn.voice_gen import VendorTtsBackend; b=VendorTtsBackend('http://127.0.0.1:8188'); w=voice_workflow.fill_clone(b.template('clone'), text='你來了。', seed=4217, reference_audio='<檔名>', params={'max_new_tokens':256}); print(b.run(w, lambda:False)[0][1][:16])"
```

## 為什麼聲音可能不像人

- `max_new_tokens` 太小（例如 64–96）會讓句子在講完前被切掉；先用 256 以上。
- Voice Design 的參考音太短、或描述太抽象，Clone 出來會不穩；參考音 4–8 秒、單一說話者最穩。
- `MOSS-TTS (Local 1.7B)` 是 5 GB 等級的小模型，音質本來就不如 8B Delay；要更好只能換大模型（需要約 18 GB VRAM）。
- 參考音有背景噪音、多人、音樂，克隆會把那些也學進去。
- 同一角色要固定 `seed` 與同一段參考音；換 seed 等於換一次取樣。

## 目前後端預設

- `max_new_tokens` 由 `voice_workflow.estimate_max_tokens()` 依字數估算（3.5 token／字＋40，下限 64）。
  這個下限對很短的中文句子可能偏低；手動測試時請用 256 起跳。
- 參考音路徑：`works/<book>/voice/refs/`；Voice Design 產物是 `voice/refs/<char_id>_design.flac`。
- 語音輸出：`assets/voice/<chapter>/<speaker>_<line_id>.ogg`（24 kHz mono Vorbis）。
