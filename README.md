# 直播影片摘要工具

Windows 長影片逐字稿與 AI 章節導覽工具。目前 Stage 0～9 已完成；系統音訊擷取、長時間串流轉錄、SQLite 復原、逐字稿匯出、OpenAI AI 分章、本機多人標註、完整 GUI、工作階段管理與 Portable one-folder EXE 均已通過實測。`0.2.0` 是首個完成介面與 Portable 實機驗收的正式版本。

完整產品與階段規格請參閱 [windows_ai_video_chapter_plan.md](windows_ai_video_chapter_plan.md)。

## 下載 Windows Portable

正式版請到 [GitHub Release v0.2.0](https://github.com/sihong0210/video-chapter-tool/releases/tag/v0.2.0) 下載。請選擇 CPU 或 NVIDIA 版；GitHub 自動產生的 `Source code (zip)` 與 `Source code (tar.gz)` 不是可直接執行的 Portable 版本。

### CPU 版

下載單一檔案 `VideoChapterTool-0.2.0-CPU-win64.rar`。適合沒有 NVIDIA 顯示卡、使用 Intel／AMD 顯示晶片，或希望下載較小套件的使用者。Whisper 與說話者分離都會使用 CPU。

### NVIDIA 版

必須同時下載下列兩個分卷：

- `VideoChapterTool-0.2.0-NVIDIA-win64.part1.rar`
- `VideoChapterTool-0.2.0-NVIDIA-win64.part2.rar`

將兩個分卷放在同一資料夾並保持原始檔名，再使用 WinRAR 對 `.part1.rar` 解壓縮；不要單獨解壓 `.part2.rar`。套件內含完整 CUDA 執行元件，一般使用者不必另外安裝 CUDA Toolkit，但仍需安裝相容的 NVIDIA 顯示卡驅動程式。

### 執行需求與注意事項

- 支援 Windows 10／11 64 位元。
- CPU 版解壓後約需 0.83 GiB；NVIDIA 版約需 3.78 GiB。模型另占空間，建議至少保留 10 GiB 可用空間。
- 完整解壓縮後執行 `VideoChapterTool.exe`；不要將 EXE 單獨移出資料夾，也不要刪除 `_internal`。
- Whisper 與說話者分離模型不包含在壓縮檔內，第一次使用時需另行下載。
- Whisper 本機轉錄不需要 OpenAI API Key；AI 章節摘要功能才需要 OpenAI API Key。
- 說話者分離需要 Hugging Face Token，並須先接受 pyannote 模型的使用條款。
- 程式目前沒有程式碼簽章，Windows SmartScreen 可能顯示警告。請使用 Release 附帶的 `SHA256SUMS.txt` 核對檔案完整性。

2026-09-06 已完成兩個最終壓縮來源的實際推論 Gate：CPU 版通過 Whisper CPU／INT8 與 pyannote CPU；NVIDIA 版在 NVIDIA GeForce RTX 5060 Laptop GPU 通過 Whisper CUDA／FP16 與 pyannote CUDA。NVIDIA Gate 已停用外部 CUDA 搜尋，確認測試使用套件內附的完整 CUDA 執行元件。

## 開發環境

- Windows 10 / 11
- Python 3.11～3.14
- Stage 1 使用 `PyAudioWPatch 0.2.12.8` 存取 WASAPI Loopback
- Stage 2 使用 `faster-whisper 1.2.1` 與 `CTranslate2 4.8.1`
- Stage 4 使用 `opencc-pyo3 0.10.3` 產生臺灣繁體中文
- Stage 6.5 使用 `pyannote.audio 4.0.7` 執行本機說話者分離；NVIDIA 開發環境使用 PyTorch 2.11.0 / CUDA 13.0
- Stage 7 使用 `PySide6-Essentials 6.11.2` 建立 Windows Qt Widgets 介面
- Stage 8～9 使用 `PyInstaller 6.20.0` 產生支援 Python 3.14 的 Windows one-folder Portable 版本

封裝依賴鎖定於 `requirements-build.lock`。

## 快速開始

初始化開發用資料目錄：

```powershell
python -m app --data-dir .runtime init
```

建議先建立虛擬環境並安裝已鎖定依賴：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

開發 Stage 7 GUI 時再安裝 Qt Widgets 依賴：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-gui.lock
```

顯示 Windows 硬體診斷：

```powershell
python -m app --data-dir .runtime diagnostics
```

輸出 JSON 診斷資料：

```powershell
python -m app --data-dir .runtime diagnostics --json
```

列出 WASAPI Loopback 裝置：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime audio-devices
```

擷取 5 秒系統音訊作為 Prototype 驗證：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime capture-audio --duration 5
```

播放約半秒低音量測試音並做端到端驗證：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime capture-audio --duration 2 --play-test-tone
```

Stage 1 的 WAV 僅供音訊管線驗證，正式產品會改為短 Chunk Queue，不保存完整長時間錄音。

## Whisper 模型與基本轉錄

查看模型狀態：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime model-list
```

下載並驗證 `small` 模型：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime model-download small
```

轉錄音訊檔並輸出 Timestamp JSON：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime transcribe-file "D:\audio\sample.wav" --model small --device auto --language zh
```

`auto` 會先檢查 NVIDIA CUDA 12／cuDNN 9 執行環境；DLL 不完整或 GPU 推論失敗時，會自動以 CPU INT8 重試。AMD GPU 加速目前仍屬實驗性，AMD GPU 電腦預設使用 CPU。

## 重疊 Chunk 串流轉錄

Stage 3 可在擷取系統播放音訊的同時，以 Producer / Consumer Queue 將約 20～30 秒的重疊 Chunk 送入 Whisper：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime stream-transcribe --duration 60 --chunk-seconds 25 --overlap-seconds 2 --device auto --language zh
```

開發用短測試可在開始擷取時播放測試音：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime stream-transcribe --duration 6 --chunk-seconds 3 --overlap-seconds 0.5 --device auto --language auto --play-test-tone
```

Chunk 先完整寫入 `PendingAudio` 並登記至 SQLite，才排入轉錄 Queue；Timestamp 依來源 PCM sample 數計算。每個 Chunk 的逐字稿以單一交易保存，成功後才刪除音訊；擷取或轉錄失敗時保留檔案供復原。使用 `--keep-chunks` 可在成功後保留 Chunk。

## SQLite 自動保存、復原與匯出

串流工作完成後會自動建立獨立匯出資料夾，內含 UTF-8 `transcript.txt`、`transcript.json` 與 `subtitles.srt`。中文工作預設將顯示及匯出文字轉為臺灣繁體；SQLite 與 JSON 仍保留 `raw_text`，方便日後回溯 Whisper 原始辨識結果。

可在開始前指定標題、平台與匯出位置：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime stream-transcribe --duration 1800 --title "直播名稱" --platform youtube --export-directory "D:\逐字稿" --device auto --language zh
```

列出全部或未完成的工作階段：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime session-list
.\.venv\Scripts\python.exe -m app --data-dir .runtime session-list --incomplete
```

意外中止後，可依畫面中的工作階段編號接續處理已落盤 Chunk；若只是匯出失敗，可不重新轉錄直接重試匯出：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime session-recover 12 --device auto
.\.venv\Scripts\python.exe -m app --data-dir .runtime session-export 12 --output-directory "D:\逐字稿"
```

內部 SQLite 位於資料目錄的 `Database\video_chapter_tool.db`。`PendingAudio` 只保留尚未完成或明確要求保留的短 Chunk，不會在正常成功流程中無限累積。寫入前另保留 256 MiB 磁碟安全空間，不足時會停止新增 Chunk 並顯示錯誤。

## Stage 5 長時間穩定性 Gate

每次 `stream-transcribe` 都會在 `Diagnostics\Stability` 產生不含音訊及逐字稿內容的 JSON 報告，並將位置記入 SQLite。報告會檢查：

- 擷取時長及來源 Frame 覆蓋率
- 音訊串流狀態事件
- Queue 是否完全排空與最高積壓
- Timestamp 倒序、重疊及超出擷取範圍
- `PendingAudio` 是否正常清理
- 暖機後程序 RAM 每小時成長趨勢

30 分鐘每日 Gate：先執行命令，看到「音訊擷取已開始」後立即播放連續影片：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime stream-transcribe --duration 1800 --chunk-seconds 25 --overlap-seconds 2 --device auto --language zh --title "Stage5 30分鐘 Gate"
```

候選版本與發布前最長壓力測試的時間分別改為 `3600`、`10800` 秒。目前產品沒有 6 小時影片需求，因此不將 6 小時測試列為交付門檻。測試期間應播放連續內容；短片或播放器停止會降低來源 Frame 覆蓋率，Gate 會據實標記失敗。

依工作階段編號查看摘要或完整 JSON：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime stability-show 12
.\.venv\Scripts\python.exe -m app --data-dir .runtime stability-show 12 --json
```

報告結果：`pass` 表示所有適用檢查通過；`provisional_pass` 表示沒有失敗，但測試太短而有無法判定項目；`fail` 或 `error` 代表需要先調查。

在串流期間按 `Ctrl+C` 會視為使用者提前停止：程式會結束目前擷取、處理已落盤 Queue、保存 SQLite 並匯出已完成內容。直接關閉終端機仍屬強制中斷，下次可用 `session-list --incomplete` 與 `session-recover` 復原。

部分筆電的內建喇叭與 3.5 mm 耳機共用同一個 Realtek WASAPI 端點，插拔耳機不會被視為播放裝置切換；切換到不同的 USB、HDMI 或藍牙端點時，程式會偵測預設端點索引改變並停止，避免默默錄到錯誤來源。

## Stage 6 AI 分章核心

已保存的 Timestamped Transcript 會按文字量與時間範圍分批送入 `LLMProvider`，每批成功結果立即寫入 SQLite。後續批次或 API 暫時失敗時，逐字稿不受影響；重新執行只處理失敗或內容已改變的批次。初稿完成後另做一次跨批次語意整併，因此文字批次邊界不會直接變成固定章節切點。

最終結果包含全片摘要、語意章節、章節摘要及每章 3～5 個重點，並檢查時間順序、重疊及影片範圍。完成實際 Provider 分析後，可重新匯出 Markdown：

第一個實際 Provider 使用 OpenAI Responses API 與 `gpt-5.6-luna`。請先在 OpenAI API 平台建立專案金鑰；不要把金鑰貼進對話、命令參數、設定檔或專案檔案。只在目前 PowerShell 工作階段設定環境變數，關閉視窗後即失效：

```powershell
$stage6Key = Read-Host "OpenAI API Key" -AsSecureString
$stage6Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($stage6Key)
try { $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($stage6Pointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($stage6Pointer) }
Remove-Variable stage6Key, stage6Pointer
.\.venv\Scripts\python.exe -m app --data-dir .runtime ai-analyze 9
```

分析完成後會在該工作階段原本的匯出資料夾新增 `summary.md`。若 API 中途失敗，可直接重跑同一命令，已成功且逐字稿內容未改變的文字批次會從 SQLite 沿用。

單獨重新匯出既有 AI 結果：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime ai-export 9
```

傳送至雲端的內容只有逐字稿文字與時間戳，不包含原始音訊。API Key 不寫入 SQLite、匯出檔或一般日誌。

2026-08-19 已使用 30 分鐘商品直播工作階段 #9 完成真實 API 品質驗收：2 個文字批次、4 次 API 呼叫，產生 9 個語意章節；章節覆蓋逐字稿時間軸 99.394%，總空隙 7 秒且沒有章節重疊。跨批次整併成功將橫跨 901.5 秒批次邊界的同一刷具主題合併為單一章節，未被固定批次機械切割。人工抽查各章切點與原始逐字稿一致；第一章有約 1 秒的相鄰章節語意外溢，列為後續提示詞觀察項目，不影響本階段通過。

## Stage 6.5 本機說話者分離

多人直播已提升為進入 GUI 前的核心需求。技術方案採直接整合 `pyannote.audio`，沿用現有 faster-whisper、Chunk Queue、SQLite、復原與匯出管線，不以完整 WhisperX 取代已驗證的轉錄核心。WhisperX 仍作為文字精細對齊的參考方案；目前正式版要求 Python `<3.14`，不相容於本專案 Python 3.14.3。

說話者分析啟用時，成功轉錄的 Chunk 將暫時保留，待本機分析完成並將字詞時間、`speaker_turns` 與匿名標籤寫入 SQLite 後自動刪除。原始逐字稿維持不變，由匯出層依時間組合 A／B 字幕，避免說話者判斷失誤破壞已保存文字。介面提供「不區分、自動判斷 2～6 人、指定 2～6 人」；目前維持匿名 A／B（多人時延伸 C／D），重新命名留作後續功能。單人內容應使用「不區分」。台語專用辨識暫不納入目前階段；正式語言範圍仍為臺灣中文及其中穿插的少量英文。

GPU 工作採循序交接：Whisper 完成逐字稿後會先卸載 CTranslate2 模型，再載入 pyannote，避免 8 GB 顯示記憶體同時保留兩套模型。pyannote 權重本身約為數十 MB；主要磁碟占用來自 Python／PyTorch CUDA 執行環境。

NVIDIA 環境先安裝基本依賴，再安裝 Stage 6.5 的 CUDA 鎖定檔：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -r requirements-diarization-cuda.lock
```

首次使用 `community-1` 前，必須先在 Hugging Face 接受模型條款並建立唯讀 Token。程式只從目前程序的 `HF_TOKEN` 或 `HUGGINGFACE_TOKEN` 讀取，不會寫入 SQLite、匯出檔或一般日誌。可用下列方式隱藏輸入並只設定於目前 PowerShell 視窗：

```powershell
$hfTokenSecret = Read-Host "Hugging Face Token" -AsSecureString
$env:HF_TOKEN = [Net.NetworkCredential]::new("", $hfTokenSecret).Password
```

設定後可先檢查套件、CUDA、GPU 與 Token 狀態；診斷只顯示 Token 是否存在，不顯示內容：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime speaker-diagnostics
```

診斷顯示 Token 已設定後，先下載並載入模型，確認授權與 CUDA 都能正常使用：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime speaker-model-download --device auto
```

新的兩人測試應直接在串流命令啟用指定人數；Chunk 只會保留到說話者分析成功，失敗時則保留以便重試：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime stream-transcribe --duration 300 --chunk-seconds 25 --overlap-seconds 2 --device auto --language zh --speakers 2 --diarization-device auto --title "Stage6.5 兩人辨識 Gate"
```

若分析階段失敗，可在修正 Token、模型或運算環境後針對同一工作階段重試：

```powershell
.\.venv\Scripts\python.exe -m app --data-dir .runtime speaker-analyze SESSION_ID --speakers 2 --device auto
```

2026-08-22 已以工作階段 #17 完成 5 分鐘真實雙人直播的指定 2 人／NVIDIA CUDA 品質 Gate：300 秒音訊、13 個 Chunk、128 個逐字稿片段與 60 個說話者區段均完成，128 段全部標註為 A 或 B，成功後暫存 Chunk 全數清理。人工比對結果為開頭一至兩句因兩人聲音尚不清晰而可能混淆，角色聲音確立後 A／B 維持一致且未再交換；此冷啟動誤差列為已知限制，不阻擋指定 2 人模式通過。

同日工作階段 #19 首次「完全無人數提示」自動測試只估出 1 人；依產品語意修正為多人模式的 2～6 人有界自動估算後，重跑成功判定 2 人、產生 69 個區段，人工比對通過。相同音訊再以 CPU／指定 2 人完成 69 個區段，證實 CUDA 與 CPU 路徑均可用。單人影片不需執行說話者分析。剩餘 30 分鐘雙人品質與 Alpha 前 3 小時新流程列為後續發布 Gate，不阻擋 Stage 7 GUI 開始。

## Stage 7 Windows GUI

Phase 3 已將驗證過的錄製與 AI 管線接入 PySide6 介面。可從「錄製」頁開始並手動停止；錄製期間顯示經過時間、音訊峰值、Chunk／Queue／積壓及延遲逐字稿。停止後會排空 Queue、執行選定的說話者分析、輸出 TXT／JSON／SRT，再提供開啟輸出資料夾。錄製對使用者呈現為不限預估時長，內部安全上限為 6 小時。

錄製期間程式會要求 Windows 不進入睡眠；設定預設也會保持顯示器開啟，可由使用者關閉顯示器保持選項。這只在錄製工作執行的背景執行緒有效，完成或失敗後會恢復 Windows 原有電源策略。

OpenAI API Key 與 Hugging Face Token 可在設定頁貼上，內容只保存於目前 Windows 使用者的「憑證管理員」，不會回填至畫面、寫入 `settings.json`、SQLite 或一般日誌。若同時設有環境變數，環境變數優先；介面只顯示是否設定與來源。啟動方式：

設定頁的人數使用 2～6 人下拉選單；「儲存設定」固定顯示在頁面底部。即使使用者未先按儲存，開始錄製時也會自動保存畫面上的最新選項。錄製頁會顯示本次實際採用的說話者模式，避免將「自動判斷」誤認為「指定 2 人」。

```powershell
.\.venv\Scripts\python.exe -m app.gui --data-dir .runtime
```

第一次 GUI Gate 建議錄製 3～5 分鐘雙人內容：確認音量條有反應、約每 25 秒出現暫時逐字稿，按下「停止並完成處理」後能得到含 A／B 的 TXT、JSON、SRT。預覽文字在說話者分析前產生，因此錄製中不會先顯示 A／B；正式標註以結束後的匯出檔為準。

「AI 章節摘要」頁可選擇任何已有逐字稿的工作階段。開始前會再次確認雲端傳送與 API 費用；程式只傳送逐字稿文字、時間戳與說話者標籤，不傳送音訊。分析期間會顯示文字批次、快取及 API 呼叫進度；可要求停止，已成功的文字批次會保留供下次沿用。完成後可直接預覽章節、開啟 `summary.md` 或輸出資料夾。已有摘要的工作階段可查看舊結果或重新分析；新結果完整成功前不會取代舊摘要。

第一次 AI GUI Gate 建議先選擇 3～5 分鐘工作階段，按「產生 AI 章節摘要」並確認：進度完成、Markdown 預覽可讀、`summary.md` 已建立，且重新選擇該工作階段仍能載入既有結果。這項測試會使用 OpenAI API 額度。

2026-08-22 已以工作階段 #23 完成第一次真實 AI GUI Gate：4 分 17 秒逐字稿成功整理為 4 個章節，摘要可在介面預覽，`summary.md` 與輸出資料夾按鈕可用，重新載入後仍正確顯示既有摘要。進度百分比已改為置中顯示；已有完整摘要時維持 `100%`。

「模型與復原」頁會驗證 `small`／`medium` Whisper 模型、顯示大小與實際位置，並可在背景下載或續傳。移除模型前必須輸入完整模型名稱，且只移除模型檔，不會刪除 SQLite、逐字稿或摘要。設定頁可選擇後續錄製要使用的 Whisper 模型。

錄製設定的 Whisper 下拉選單會同步顯示模型驗證狀態；只有「可使用」的模型能被選取。尚未下載、下載未完成或內容不完整的模型仍會顯示原因，但必須先到「模型與復原」完成下載或修復。

同一頁會列出未完成的 SQLite 工作階段、已保存逐字稿數量、待處理 Chunk 與音訊檔可讀狀態。復原會沿用原工作階段的 Whisper 及說話者設定，處理完成後重新匯出 TXT／JSON／SRT；可在 Chunk 邊界安全停止，已完成內容與剩餘 WAV 都會保留。沒有待處理 Chunk 時不會無謂載入 Whisper 模型。

2026-08-22 已以工作階段 #18 完成真實 GUI 復原 Gate：系統正確辨識 0 個待處理 Chunk、0 段逐字稿與 1 個保留音訊，完成空白逐字稿重新匯出、清理保留音訊，並從未完成清單移除；復原輸出按鈕可正常使用。模型表格已加入固定最低高度，整頁在較小視窗改用垂直捲動，不再壓縮表格；選定的已驗證模型會顯示 `100%`。

執行測試：

```powershell
python -m unittest discover -s tests -v
```

執行語法檢查：

```powershell
python -m compileall -q app tests
```

## Stage 9 Portable EXE

建置工具安裝與乾淨建置：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.lock
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

腳本會依序執行完整測試、建立 one-folder、驗證 EXE 旁自動建立 `UserData`、執行 GUI 中文路徑 Smoke、在 EXE 內載入六組主要執行元件，再以受限 PATH 與隱藏 GPU 的環境驗證 CPU 回退。任一 Gate 失敗都不會建立交付產物。模型、`.runtime` 與使用者資料不會封入套件。

GitHub Release 目前分為兩個執行環境：CPU 版使用 PyTorch CPU，未壓縮約 0.83 GiB；NVIDIA 版使用 PyTorch CUDA 13.0 並保留完整 CUDA 執行元件，未壓縮約 3.78 GiB。兩者皆為 one-folder Portable，分別以單一 RAR 與兩個 RAR 分卷發佈。使用與限制請參閱 [docs/PORTABLE_README_zh-TW.md](docs/PORTABLE_README_zh-TW.md)。

## 使用者資料位置

Portable EXE 第一次執行時會在程式旁建立：

```text
VideoChapterTool\UserData
```

其中包含設定、SQLite、日誌、暫存音訊與預設匯出；模型與匯出路徑仍可另外指定。內部路徑以相對方式保存，因此可關閉程式後搬移整個 `VideoChapterTool` 資料夾。開發模式仍可使用 `--data-dir` 指定隔離資料目錄。

日誌位於 `UserData\Logs`，單檔最多 5 MiB 並保留 5 份輪替檔。它只記錄啟動、錄製、模型、復原、AI 與錯誤事件，不記逐字稿、音訊或憑證。診斷頁可匯出 JSON，或匯出包含版本、硬體診斷及日誌的 ZIP；ZIP 不含 SQLite、原始設定檔或使用者內容。

## 版本編號

專案採語意化版本：`主版本.次版本.修訂版`。目前正式版本為 `0.2.0`；先前的 `0.2.0a1`、`0.2.0a2` 是內部 Alpha 驗收版。不相容的大改版才升 `1.0.0`／`2.0.0`；相容的新功能升次版本，例如 `0.3.0`；錯誤修正升修訂版，例如 `0.2.1`。
