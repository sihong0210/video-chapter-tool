# 直播影片摘要工具 0.2.0 Alpha 1

發布通道：內部 Portable Alpha

## 主要變更

- EXE 第一次啟動會在程式旁建立 `UserData`，不再將正式 GUI 資料預設放到 `%LOCALAPPDATA%`。
- 設定中的內部模型／匯出位置與 SQLite 管理的匯出／待復原音訊路徑改為可搬移表示。
- 搬移整個 `VideoChapterTool` 資料夾後，預設模型、SQLite、逐字稿與待復原音訊會在新位置繼續使用。
- GUI 啟用 5 MiB × 6 檔的輪替日誌，記錄啟動、錄製、模型、復原、AI 與錯誤事件。
- 診斷頁可開啟 `UserData`、日誌資料夾，並匯出診斷 JSON 或支援用 ZIP。
- 診斷 ZIP 不包含逐字稿、音訊、SQLite、API Key、Hugging Face Token 或原始設定檔。
- Portable 建置新增「EXE 旁自動建立 `UserData`」Gate，封裝 ZIP 本身不含使用者資料。

## 相容性

- 支援 Windows 10／11 64 位元。
- NVIDIA CUDA 可用時優先加速；不可用時 Whisper 回退 CPU。
- AMD GPU 電腦目前使用 CPU，功能不受影響但速度較慢。
- Whisper 模型不內附，首次使用前由「模型與復原」頁按需要下載。

## 升級注意事項

從開發用 `.runtime` 延續資料時，先以新版原始碼開啟一次：

```powershell
.\.venv\Scripts\python.exe -m app.gui --data-dir .runtime
```

正常開啟後關閉程式，再把 `.runtime` 的完整內容複製為 Portable 程式旁的 `UserData`。操作前請先備份 `.runtime`。

## 已知限制

- 尚未簽署程式碼，SmartScreen／防毒可能顯示警告。
- 目前擷取整個 Windows 預設播放裝置，尚不能指定單一程式視窗。
- 錄製期間字幕預覽尚未帶 A／B；說話者標籤在停止後分析完成才寫入。
- 不提供自動更新、安裝程式或自動建立捷徑。
