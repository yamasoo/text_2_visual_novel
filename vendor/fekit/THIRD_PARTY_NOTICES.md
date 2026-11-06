# THIRD_PARTY_NOTICES

`vendor/fekit/vendor/` 內每個第三方檔案都要附上自己的 LICENSE，並登記在這裡。
授權以 MIT 優先；0BSD、BSD、ISC、Apache-2.0 需在階段報告說明理由；GPL 類不採用。

| 名稱 | 版本 | 授權 | 檔案 | 來源 |
|---|---|---|---|---|

## 說明

- **只改過一處**：刪掉檔尾的 `//# sourceMappingURL=...` 註解（沒有附 `.map` 檔，留著開發者工具會一直 404）。其餘與 npm 套件內容逐位元組相同。
- 以上都不連網載入，執行時只讀 `vendor/fekit/vendor/` 內的檔案。

（這個專案沒有帶任何第三方檔案。）
