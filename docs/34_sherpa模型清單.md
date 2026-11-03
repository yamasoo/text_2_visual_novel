# 34 sherpa-onnx 中文／粵語模型清單

> 語音後端是 **sherpa-onnx**：CPU 可跑、固定 speaker id、不需要 ComfyUI。
> 模型權重檔案較大，**不放在 GitHub**；請照下面指令下載到 `models/sherpa/`。

## 建議先試

| 模型 | 語言 | speaker | 取樣率 | 備註 |
|---|---|---:|---:|---|
| `vits-zh-hf-fanchen-C` | 中文 | 187 | 16 kHz | 檔案較小，適合先試；187 個 speaker 音質不均 |
| `vits-zh-hf-theresa` | 中文 | 804 | 22.05 kHz | 音質較好；角色聲線，商用授權要確認 |
| `vits-cantonese-hf-xiaomaiiwn` | 香港粵語 | 1 | 22.05 kHz | 粵語台詞要另外寫 |

## 下載模型（複製即可）

```powershell
# 先確認 venv 已建立；start_studio.bat 第一次執行會建立
.\venv\Scripts\python.exe tools\sherpa_get_model.py vits-zh-hf-fanchen-C
.\venv\Scripts\python.exe tools\sherpa_get_model.py vits-zh-hf-theresa
.\venv\Scripts\python.exe tools\sherpa_get_model.py vits-cantonese-hf-xiaomaiiwn

# 列出已安裝的模型
.\venv\Scripts\python.exe tools\sherpa_get_model.py --list
```

下載後會放在：

```text
models\sherpa\<模型名稱>\
```

一個可用模型至少要有：

```text
models\sherpa\vits-zh-hf-fanchen-C\
  vits-zh-hf-fanchen-C.onnx
  tokens.txt
  lexicon.txt
  *.fst
```

`models/` 已列在 `.gitignore`，**不會進 GitHub**。

## 如果想自己下載

```powershell
$model = "vits-zh-hf-fanchen-C"
$url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/$model.tar.bz2"
New-Item -ItemType Directory -Force models\sherpa | Out-Null
Invoke-WebRequest -Uri $url -OutFile "models\sherpa\$model.tar.bz2"
tar -xjf "models\sherpa\$model.tar.bz2" -C models\sherpa
Remove-Item "models\sherpa\$model.tar.bz2"
```

> `tar` 是 Windows 10 內建；如果沒有，先裝 7-Zip 或 Git for Windows。

## 其他可下載的中文模型

| 模型 | 語言 | speaker | 備註 |
|---|---|---:|---|
| `vits-zh-hf-fanchen-wnj` | 中文 | 1 | 單人 |
| `vits-zh-hf-fanchen-unity` | 中文 | ? | 社群音色 |
| `vits-zh-hf-eula` | 中文 | 804 | 遊戲角色聲線；商用授權要確認 |
| `sherpa-onnx-vits-zh-ll` | 中文 | 5 | 較小、5 人 |
| `vits-zh-aishell3` | 中文 | 174 | AISHELL-3 資料集 |
| `vits-icefall-zh-aishell3` | 中文 | 174 | 同上轉換版 |
| `vits-melo-tts-zh_en` | 中文＋英文 | 1 | 44.1 kHz；英文只認 lexicon 內的詞 |

## 目前不支援的模型

以下需要不同的 loader，這個版本還沒有接：

- `vits-piper-zh_CN-*`（Piper，需要 `data_dir`）
- `matcha-icefall-zh-baker`（Matcha）
- `kokoro-multi-lang-v1_1`（Kokoro，24 kHz）

## 在 UI 換模型

1. `start_studio.bat` 開工作台 → 書 → **語音**
2. 「模型」卡片選已安裝模型 → **套用模型**
3. 也可以在「自訂路徑」貼 `models/sherpa/<資料夾>`
4. 換模型後 VoiceMap 的 `config_hash` 會變，舊句會重新生成

角色聲線沒有現成對照時，直接填 speaker id 即可；介面會顯示手動 ID 輸入。

## 授權提醒

- `sherpa-onnx` 本體是 Apache-2.0。
- 個別模型有自己的授權；`theresa`／`eula` 等遊戲角色聲線，以及 `fanchen-C`，商用前都要逐一確認。
- 這份清單只代表「技術上可載入」，不代表可商用。

更完整的使用步驟見 [36_語音快速開始.md](36_語音快速開始.md)。
