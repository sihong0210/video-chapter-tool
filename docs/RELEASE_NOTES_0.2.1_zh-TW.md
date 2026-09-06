# 直播影片摘要工具 0.2.1

發布通道：Windows Portable 正式版

## 本次發佈重點

- 將 Windows Portable 拆分為 CPU 與 NVIDIA 兩個執行環境，避免沒有 NVIDIA 顯示卡的使用者下載不需要的 CUDA 元件。
- CPU 版使用 PyTorch CPU；NVIDIA 版使用 PyTorch CUDA 13.0，並保留完整、未刪減的 CUDA 執行元件。
- NVIDIA 版以 RAR 分卷發佈，每個附件均低於 GitHub Release 的單檔大小限制。
- 加入 `SHA256SUMS.txt`，方便下載後核對 CPU 與 NVIDIA 附件的完整性。
- README 與 Portable 說明新增版本選擇、分卷解壓、磁碟空間、模型與憑證需求。
- 專案改採 MIT License，原始碼與 Portable 套件均附授權文字。

## 使用方式

- CPU 版下載單一 `VideoChapterTool-0.2.1-CPU-win64.rar`。
- NVIDIA 版必須同時下載 `.part1.rar` 與 `.part2.rar`，放在同一資料夾後從 `.part1.rar` 開始解壓縮。
- 完整解壓縮後執行 `VideoChapterTool.exe`，不可只移動 EXE 或刪除 `_internal`。
- 模型與使用者資料不包含在發佈附件內；第一次使用時需另行下載模型。

## 驗證要求

- 完整自動化測試與 Python 語法檢查必須通過。
- CPU 套件必須通過 Whisper CPU／INT8 與 pyannote CPU 實際推論 Gate。
- NVIDIA 套件必須在停用外部 CUDA 搜尋後，通過 Whisper CUDA／FP16 與 pyannote CUDA 實際推論 Gate。
- 兩種壓縮檔必須通過 WinRAR 完整測試、來源內容比對與 SHA-256 驗證。

## 已知限制

- 尚未提供單一視窗音訊來源、AMD GPU 加速、說話者自訂名稱、自動更新或程式碼簽章。
- A／B 分析在開頭短句、重疊說話或音訊不清晰時可能暫時混淆。
- Windows SmartScreen 或防毒可能在第一次執行未簽章程式時顯示警告。
