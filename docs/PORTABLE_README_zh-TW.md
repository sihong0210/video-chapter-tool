# 直播影片摘要工具 0.2.1（Portable）

這是 Windows 10／11 64 位元 one-folder 版本。程式已隨附 Python 與執行元件，使用電腦不需要另外安裝 Python；請保留整個資料夾，不可只複製 `VideoChapterTool.exe`。本工具採 MIT License，授權文字位於同一資料夾的 `LICENSE.txt`。

## 啟動

1. CPU 版直接解壓單一 RAR；NVIDIA 版先下載所有分卷並放在同一資料夾，再從 `.part1.rar` 完整解壓縮到一般可寫入的資料夾。
2. 執行 `VideoChapterTool.exe`。
3. 到「設定」選擇模型與匯出資料夾。
4. 到「模型與復原」下載並驗證 `small` 或 `medium` Whisper 模型。
5. 需要 AI 摘要或多人標註時，再分別保存 OpenAI API Key 與 Hugging Face Token。

模型不包含在程式壓縮檔內。第一次啟動會在 EXE 旁建立：

```text
VideoChapterTool\
├─ VideoChapterTool.exe
├─ _internal\
└─ UserData\
   ├─ Config\
   ├─ Database\
   ├─ Logs\
   ├─ Models\
   ├─ PendingAudio\
   ├─ Exports\
   └─ Diagnostics\
```

設定、SQLite、日誌、暫存音訊與預設匯出均位於 `UserData`。模型預設也在其中，但模型與匯出資料夾可由介面另外指定。內部路徑採相對保存；程式關閉後可搬移整個 `VideoChapterTool` 資料夾，不要只搬 EXE 或只搬 `UserData` 的一部分。

## 更新、備份與移除

- 更新前先關閉程式，將整個 `UserData` 複製到另一個磁碟或壓縮成 ZIP。
- 新版本壓縮檔不含 `UserData`。建議解壓到新資料夾，再把舊 `UserData` 整個移入；也可保留既有 `UserData`，用新程式檔與 `_internal` 覆蓋舊版本。
- 若模型或匯出設定指向 Portable 資料夾外，完整備份時也要另外備份那些外部資料夾。
- 這是免安裝版本，不寫入解除安裝清單。要移除程式，先備份需要的逐字稿，再刪除整個程式資料夾；Windows 憑證管理員中的 API Key／Token 可先在設定頁按「刪除」。
- 從開發用 `.runtime` 延續資料時，應先以新版程式開啟該資料目錄一次完成路徑升級，關閉後再把 `.runtime` 的完整內容複製為 Portable 的 `UserData`。

## 日誌與診斷

一般日誌保存在 `UserData\Logs\application.log`，每個檔案最多 5 MiB，保留 5 份舊檔。日誌只記錄程式版本、運算選項、工作階段編號、進度計數與錯誤類型，不記逐字稿文字、音訊、API Key 或 Token。

若程式異常，可到「診斷」頁重新檢測並匯出診斷包 ZIP。診斷包只包含版本、硬體診斷與已遮蔽敏感資訊的輪替日誌，不含 SQLite、原始設定檔、逐字稿或音訊。

## CPU、NVIDIA 與 AMD

- CPU 版使用 PyTorch CPU；沒有 NVIDIA GPU、使用 Intel／AMD 顯示晶片或希望下載較小套件時，請選擇此版本。功能完整，但 Whisper 與說話者分析會比較慢。
- NVIDIA 版已隨附 Whisper 所需的 CUDA 12 BLAS，以及完整 PyTorch CUDA 13 執行元件；仍需要相容的 NVIDIA 顯示卡與驅動程式。
- 自動模式若無法使用 CUDA，Whisper 會回退 CPU。診斷頁可確認實際硬體與運算狀態。
- AMD GPU 目前使用 CPU 路徑；ROCm／HIP 加速不在 0.2.1 範圍。

建議至少預留 10 GiB 可用空間，另依模型、錄製時長與未完成復原音訊增加。成功處理的 Chunk 會清理，不會無限保留 WAV；失敗或強制關閉時則會保留待復原音訊。

## 隱私與憑證

- OpenAI 分析只傳送逐字稿、時間戳與說話者標籤，不傳送原始音訊。
- Hugging Face 模型與 pyannote 說話者分析在本機執行。
- API Key 與 Token 保存在目前 Windows 使用者的憑證管理員，不寫入一般設定檔、SQLite 或日誌。

## 0.2.1 已知限制

- 此版本尚未做程式碼簽章，Windows SmartScreen 或防毒可能第一次警告或花較久時間掃描。請只使用可信來源提供且 SHA-256 相符的檔案。
- 目前擷取整個 Windows 預設播放裝置的系統音訊，尚不能指定單一瀏覽器或目標視窗。
- 錄製中可預覽延遲逐字稿；A／B 說話者標籤要在停止錄製並完成分析後才會寫入。
- 雙人內容開頭一至兩句若聲音短、重疊或不清楚，A／B 可能暫時混淆；後續角色通常會穩定。
- pyannote 的 TorchCodec／FFmpeg 檔案解碼不是本工具的處理路徑；工具直接提供記憶體 waveform，因此建置時相關警告不影響錄製或說話者分析。
- Portable 版沒有安裝程式、開始功能表捷徑或自動更新；更新、備份與移除依上方步驟手動執行。

若 Windows SmartScreen 或防毒攔截，請先核對發布頁提供的 SHA-256；不要從不明來源下載或自行加入防毒排除。
