# kenney_interface：介面音效（100 個，opus）

- 來源：Kenney「Interface Sounds」（https://kenney.nl/assets/interface-sounds），經 Calinou 整理的 repo（https://github.com/Calinou/kenney-interface-sounds）取得。
- 授權：CC0 1.0（見 `LICENSE.txt`），商用、改檔都不需要署名。
- 處理：原檔 wav，以 ffmpeg `libopus 96k` 轉成 `.opus`，檔名不變（`click_001.opus` 等）。Ren'Py 可直接播放 opus。
- 用途：全書共用的素材庫，挑要用的複製到作品的 `assets/ui/` 或 `assets/sfx/`；挑進作品時，把來源網址與授權 `CC0 1.0` 記進 manifest 的 `license`。
