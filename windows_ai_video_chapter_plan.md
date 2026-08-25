# Windows 版長影片 AI 章節摘要工具－開發計畫

## 1. 專案目的

本專案目標是開發一套 Windows 桌面程式，用於觀看 Facebook、Instagram 或其他網頁平台上的直播與重播影片時，自動擷取影片中的語音內容，產生帶時間標記的文字稿，並進一步利用 AI 將長時間影片整理成「章節、摘要與重點」。

主要使用情境以美妝介紹、產品介紹、購物直播、一般商品介紹等非專業學術內容為主。

核心價值不是單純產生逐字稿，而是：

> 讓使用者不用完整看完 1～3 小時甚至更長的直播，也能快速知道影片每個區段在講什麼，並直接跳到想看的時間點。

---

## 2. 第一版產品定位

第一版 Windows MVP 聚焦於以下流程：

```text
FB / IG / 網頁直播或重播影片
        ↓
Chrome / Edge 播放
        ↓
Windows 音訊擷取
        ↓
本機 Speech-to-Text
        ↓
帶時間標記逐字稿
        ↓
AI 主題分析
        ↓
自動章節
        ↓
章節標題 + 摘要 + 重點
        ↓
影片內容導覽
```

第一版不加入：

- 全文搜尋
- AI 問答
- 使用者帳號
- 雲端同步
- 多人協作
- App Store / Microsoft Store 上架
- iPhone / iPad 版本
- 自動下載 Facebook / Instagram 影片
- 完整影片錄製與保存

---

# 3. 主要使用情境

## 3.1 直播

使用者在 Chrome 或 Edge 中觀看 Facebook / Instagram Live。

程式同步擷取瀏覽器音訊並產生文字稿。

例如：

```text
[00:04:32]
今天先介紹我們新出的粉底液。

[00:05:08]
這款主要是比較偏自然光澤的妝感。

[00:06:14]
如果是乾性肌膚，我會比較推薦這個版本。
```

直播結束後，AI 將完整文字稿整理成：

```text
00:00
開場與今日直播內容

04:25
新品粉底液介紹
- 主打自然光澤妝感
- 適合乾性與混合性肌膚
- 介紹主要產品特色

13:42
色號比較
- 01 偏白
- 02 自然色
- 03 偏健康膚色

26:18
實際上妝示範
- 示範用量
- 遮瑕效果
- 完妝效果

41:35
直播優惠與價格
```

---

## 3.2 重播影片

使用者播放已存在 Facebook / Instagram 上的直播重播。

系統不下載整支影片，也不保存長時間錄影。

流程仍然是：

```text
播放影片
   ↓
即時擷取音訊
   ↓
即時轉錄
   ↓
保留 Timestamp
   ↓
播放完後整理章節
```

因此 1～3 小時甚至更長的影片，不會因為錄製完整影片而大量佔用硬碟空間。

---

# 4. Windows 版整體架構

## 4.1 音訊來源

優先使用 Windows WASAPI Loopback。

第一階段可以先實作：

```text
Windows System Audio
        ↓
WASAPI Loopback
```

之後升級為：

```text
Chrome / Edge Process
        ↓
Application Loopback
```

只擷取瀏覽器的播放音訊。

好處是避免：

- LINE 通知音
- Windows 系統提示
- Spotify
- Discord
- 其他應用程式聲音

被一起送入語音辨識。

---

# 5. 語音轉文字

## 5.1 建議方案

使用：

**faster-whisper**

於 Windows 本機執行。

架構：

```text
WASAPI Audio
     ↓
Audio Buffer
     ↓
faster-whisper
     ↓
Timestamped Transcript
```

---

## 5.2 主要優點

- 不需要 OpenAI Speech API
- 不需要每分鐘支付語音辨識費
- 音訊不需要上傳雲端
- 適合長時間直播
- 可使用 CPU 或 NVIDIA GPU
- 可自由選擇 Whisper 模型大小

---

## 5.3 模型建議

第一階段建議測試：

- small
- medium

如果電腦有 NVIDIA GPU，可以再評估較大的模型。

測試重點不是只看 Word Error Rate，而是實際使用場景：

- 主播講話速度快
- 背景音樂
- 多人對話
- 商品名稱
- 品牌英文名稱
- 中英混合
- 口語用詞

---

# 6. 音訊暫存策略

不保存完整影片。

不建議長時間保存完整 WAV。

採用：

**Ring Buffer / Chunk Processing**

例如：

```text
最近 20～30 秒音訊
        ↓
送入 Whisper
        ↓
辨識完成
        ↓
釋放舊音訊
        ↓
繼續下一段
```

因此影片長度不會直接造成大量儲存空間消耗。

---

# 7. Timestamp 設計

Timestamp 是整個產品的重要資料。

每筆 transcript 至少保存：

```text
start_time
end_time
text
```

例如：

```json
{
  "start": 1104.2,
  "end": 1111.8,
  "text": "接下來我們來介紹這一款粉底液。"
}
```

畫面顯示：

```text
[00:18:24]
接下來我們來介紹這一款粉底液。
```

---

# 8. 直播時間與影片時間

## 8.1 直播

直播可以直接使用：

```text
開始轉錄 = 00:00:00
```

例如：

```text
00:00:00
04:32
18:24
01:12:36
```

也可以額外記錄實際時間：

```text
20:31:24
20:42:18
```

第一版以「相對時間」為主要時間軸即可。

---

## 8.2 重播影片

第一版 MVP 可以先假設：

- 從影片開始播放時開始轉錄
- 使用者不中途大幅跳轉
- 轉錄時間約等於影片播放時間

第二階段再加入 Browser Extension，讀取真正的：

```javascript
video.currentTime
```

用來處理：

- 快轉
- 倒退
- 暫停
- 從影片中間開始播放

---

# 9. AI 內容整理

真正需要付費 AI API 的核心部分是：

**逐字稿 → 主題章節與摘要**

不需要把原始音訊送給 LLM。

流程：

```text
Timestamped Transcript
        ↓
AI Topic Analysis
        ↓
Topic Boundaries
        ↓
Chapter Merge / Split
        ↓
Chapter Title
        ↓
Summary
        ↓
Key Points
```

---

# 10. AI 自動分章邏輯

章節數量不固定。

不設定：

```text
一定 5 章
一定 10 章
一定 15 章
```

改由 AI 根據語意判斷。

主要規則：

1. 主題明顯改變時建立新章節。
2. 太短且沒有獨立意義的章節與前後章節合併。
3. 過長章節若含多個明顯子主題則進一步拆分。
4. 開場可獨立成章。
5. Q&A 可視內容決定是否獨立。
6. 不使用固定時間每 5 分鐘切割。

軟性建議：

```text
一般章節：約 3～15 分鐘
```

但不是硬性限制。

---

# 11. 每個章節的輸出格式

建議保存：

```json
{
  "start": 1104.0,
  "end": 1632.0,
  "title": "新品粉底液介紹",
  "summary": "介紹新品粉底的主要特色、適合膚質與妝感。",
  "key_points": [
    "主打自然光澤妝感",
    "適合乾性與混合性肌膚",
    "介紹新版與舊版的差異"
  ]
}
```

---

# 12. 最終 UI 目標

Windows App 可以採左右分割：

```text
┌──────────────────────────┬──────────────────────────┐
│                          │ AI 影片導覽              │
│ FB / IG / Chrome         │                          │
│                          │ 00:00 開場               │
│ 播放直播 / 重播影片      │                          │
│                          │ 04:32 新品粉底液介紹     │
│                          │ - 妝感                  │
│                          │ - 適合膚質              │
│                          │ - 產品特色              │
│                          │                          │
│                          │ 13:42 色號比較           │
│                          │                          │
│                          │ 26:18 上妝示範           │
│                          │                          │
└──────────────────────────┴──────────────────────────┘
```

也可以先做成獨立視窗，不強制與 Chrome 整合。

---

# 13. 第一版 UI 功能

首頁：

```text
選擇音訊來源

[ Chrome ]
[ Edge ]
[ 系統音訊 ]

語言：
[ 中文 ▼ ]

Whisper Model：
[ small ▼ ]

[ 開始轉錄 ]
```

轉錄中：

```text
● 正在轉錄

00:18:24
接下來介紹這一款產品……

00:18:31
這款最大的特色……

00:18:42
如果你的膚質……

[暫停] [停止]
```

停止後：

```text
轉錄完成

影片長度：01:42:18
逐字稿：完成

[AI 整理內容]
```

AI 完成：

```text
共整理為 12 個章節

00:00
開場

04:32
新品介紹

13:42
色號比較

26:18
上妝示範

41:35
價格與優惠
```

---

# 14. AI 模型與成本策略

## 14.1 本機 AI

### faster-whisper

用途：

```text
Audio → Transcript
```

API 成本：

```text
0
```

只有：

- CPU / GPU
- RAM
- 電力

成本。

---

## 14.2 雲端 AI

用途：

- 主題切分
- 章節標題
- 每章摘要
- Key Points
- 全片簡短摘要

建議優先使用價格較低、適合摘要與結構化文字處理的小型 LLM。

不建議第一版直接使用昂貴大型模型處理所有內容。

---

## 14.3 成本控制原則

避免：

```text
整份 Transcript
→ AI

整份 Transcript
→ AI

整份 Transcript
→ AI
```

反覆送相同內容。

建議：

```text
完整 Transcript
        ↓
Chunk
        ↓
初步 Topic Detection
        ↓
產生 Chapter Boundary
        ↓
每章只送自己的文字
        ↓
產生摘要
        ↓
最後只送 Chapter Summary
        ↓
產生影片總摘要
```

---

# 15. AI 使用流程建議

例如 2 小時直播：

```text
120 min transcript
       ↓
切成約 10～20 分鐘 Chunk
       ↓
AI 分析 Topic
       ↓
找出主題變化
       ↓
合併成 Chapter
       ↓
每 Chapter 單獨摘要
       ↓
產生最終目錄
```

---

# 16. 章節時間調整

AI 偵測出主題開始：

```text
18:24
```

實際提供使用者觀看時，建議往前保留：

```text
5～10 秒
```

例如：

```text
AI Chapter Start:
18:24

Jump Time:
18:17
```

原因：

避免使用者跳轉後直接進入一句話中間。

---

# 17. Browser Extension

Browser Extension 不列入第一階段核心需求。

第二階段加入。

用途：

1. 取得播放器真正時間。
2. 處理暫停。
3. 處理快轉。
4. 處理倒退。
5. 處理從影片中間開始播放。
6. 點擊章節時間後控制影片跳轉。

架構：

```text
Windows App
      ↕
Browser Extension
      ↕
HTML5 Video Player
```

---

# 18. 點擊章節跳轉

第二階段希望做到：

```text
26:18
實際上妝示範

[▶ 從這裡觀看]
```

點擊後：

```javascript
video.currentTime = 1578;
```

如果平台播放器允許操作，即可直接跳到指定段落。

需要針對：

- Facebook
- Instagram
- 其他網頁播放器

分別測試。

---

# 19. 第一版不做搜尋

目前需求明確不加入搜尋功能。

因此第一版不包含：

- Transcript Full Text Search
- AI Semantic Search
- Chat with Video
- 「哪裡提到某商品？」
- 「有沒有講到油性肌膚？」
- RAG
- Embedding
- Vector Database

但資料結構需要保留未來擴充可能。

因此 transcript 與 chapter 建議使用結構化資料保存。

---

# 20. 本機資料儲存

建議使用：

**SQLite**

主要資料表可以包含：

## videos

```text
id
title
platform
source
created_at
duration
status
```

## transcripts

```text
id
video_id
start_time
end_time
text
```

## chapters

```text
id
video_id
start_time
end_time
title
summary
```

## chapter_points

```text
id
chapter_id
content
sort_order
```

---

# 21. 輸出格式

第一版至少提供：

## JSON

保留完整結構化資料。

## TXT

例如：

```text
[00:18:24]
接下來介紹這一款新品。

[00:18:31]
這款最大的特色是……
```

## SRT

例如：

```text
1
00:18:24,000 --> 00:18:31,000
接下來介紹這一款新品。
```

## Markdown

最適合輸出 AI 整理結果：

```markdown
# 直播內容整理

## 00:04:32 新品粉底液介紹

介紹新品粉底的主要特色。

### 重點

- 自然光澤妝感
- 適合乾性與混合性肌膚
- 新版質地較清爽
```

---

# 22. 程式技術選擇

第一版建議：

## Language

Python

原因：

- faster-whisper 整合容易
- 音訊處理工具成熟
- AI API 整合簡單
- MVP 開發速度快
- 後續可使用 PyInstaller 打包成 Windows EXE

---

## UI

第一版可考慮：

- PySide6
- PyQt6

較建議：

**PySide6**

因為後續如果要做較完整桌面 UI，比 Tkinter 更適合。

---

## Audio

候選：

- WASAPI
- PyAudio
- sounddevice
- Windows Audio Session API wrapper

第一階段先完成可靠的 loopback capture。

之後再開發 process-specific capture。

---

## Speech-to-Text

```text
faster-whisper
```

---

## Database

```text
SQLite
```

---

## AI API

第一版抽象化為：

```python
class LLMProvider:
    analyze_topics()
    summarize_chapter()
    summarize_video()
```

避免程式與特定 AI 供應商綁死。

未來可以替換：

- OpenAI
- Anthropic
- Google
- 本機 LLM

---

# 23. 程式模組規劃

建議：

```text
app/
│
├── main.py
│
├── ui/
│   ├── main_window.py
│   ├── transcript_panel.py
│   └── chapter_panel.py
│
├── audio/
│   ├── loopback.py
│   ├── buffer.py
│   └── devices.py
│
├── transcription/
│   ├── whisper_engine.py
│   └── transcript_manager.py
│
├── ai/
│   ├── provider.py
│   ├── topic_segmenter.py
│   └── summarizer.py
│
├── database/
│   ├── db.py
│   └── models.py
│
├── export/
│   ├── txt_export.py
│   ├── srt_export.py
│   └── markdown_export.py
│
└── config/
    └── settings.py
```

---

# 24. MVP 開發階段

## Phase 1：音訊擷取 Prototype

目標：

```text
Chrome / Edge
↓
Windows audio capture
↓
取得乾淨 PCM audio
```

測試：

- Facebook Live
- Facebook Replay
- Instagram Live
- Instagram Replay

成功條件：

- 可連續擷取 1 小時以上
- 不明顯斷音
- 不造成播放器卡頓

---

## Phase 2：Whisper 即時轉錄

目標：

```text
Audio
↓
faster-whisper
↓
Timestamp Transcript
```

成功條件：

- 中文辨識可接受
- 1 小時以上不崩潰
- CPU / GPU 使用量合理
- Timestamp 不明顯漂移

---

## Phase 3：逐字稿保存

加入：

- SQLite
- TXT
- JSON
- SRT

確保即使程式意外關閉，也不會失去整場逐字稿。

---

## Phase 4：AI Topic Segmentation

輸入：

```text
Timestamped Transcript
```

輸出：

```text
Chapter Start
Chapter End
Chapter Title
```

驗證：

- 不固定章節數
- 能辨識商品切換
- 能辨識介紹 / 示範 / 優惠 / Q&A
- 不切得過碎

---

## Phase 5：AI Summary

每個章節產生：

- 1 個標題
- 1 段簡短摘要
- 3～5 個重點

---

## Phase 6：Windows UI

整合：

```text
開始
暫停
停止
Transcript
Chapter
Export
```

---

## Phase 7：長時間壓力測試

測試：

- 1 小時
- 3 小時

檢查：

- RAM
- CPU
- GPU VRAM
- Disk usage
- Timestamp drift
- Whisper 延遲
- 程式穩定度

---

# 25. 第二階段功能

MVP 穩定後，再加入：

## Browser Extension

取得真正：

```text
video.currentTime
```

並支援：

```text
Chapter → Player Jump
```

---

## Process Audio Capture

只擷取：

```text
Chrome.exe
```

或：

```text
msedge.exe
```

---

## 即時暫時章節

直播進行中：

```text
目前直播已進行 45 分鐘

00:00 開場
05:32 第一項新品
18:42 第二項產品
31:18 使用示範

目前正在談：
直播優惠
```

直播結束後再重新整理正式章節。

---

# 26. 第三階段候選功能

未來可以考慮：

- 搜尋逐字稿
- 搜尋商品
- AI 問答
- 「這支影片有哪些產品？」
- 「哪一段有優惠？」
- 「哪一段介紹乾性肌膚？」
- AI 商品清單
- 多支影片內容比較
- 雲端同步
- iPhone / iPad App
- 帳號系統
- 共享影片索引

目前均不列入 MVP。

---

# 27. 主要技術風險

## 27.1 Facebook / Instagram 播放器

不同平台播放器行為可能不同。

需要實際測試：

- Chrome
- Edge
- FB Live
- FB Replay
- IG Live
- IG Replay

---

## 27.2 Whisper 延遲

若模型速度低於 real-time：

```text
播放速度 > Whisper 處理速度
```

Audio buffer 會逐漸累積。

因此需要：

- 模型大小選擇
- GPU 加速
- Chunk size 調整
- Queue 控制

---

## 27.3 背景音樂

美妝與產品直播常有：

- 音樂
- 特效音
- 多人聊天

因此 Whisper 品質必須以實際直播資料測試。

---

## 27.4 商品名稱

品牌與商品名稱可能：

- 英文
- 中文
- 日文品牌音譯
- 縮寫

第一版不追求 100% 名稱辨識正確。

後續可增加：

- 自訂詞庫
- AI Post-processing
- 使用者修正

---

## 27.5 Topic Segmentation

最重要的 AI 品質指標不是摘要文筆，而是：

```text
章節切點是否合理？
```

需要建立實際測試資料。

---

# 28. MVP 驗證指標

第一版最重要的不是功能數量，而是驗證以下五件事。

## 指標 1

Windows 能否穩定取得 FB / IG 音訊。

---

## 指標 2

faster-whisper 能否長時間穩定轉錄一般中文產品直播。

---

## 指標 3

Timestamp 是否能大致對應影片位置。

---

## 指標 4

AI 能否把 1～3 小時聊天型直播合理整理成章節。

---

## 指標 5

使用者看到章節後，是否真的可以不用完整看影片就找到想看的內容。

---

# 29. 第一版成功標準

例如一支：

```text
2 小時 15 分鐘美妝直播
```

系統可以在影片播放過程中建立完整逐字稿。

播放完成後自動輸出：

```text
影片長度：
02:15:32

AI 整理：
14 個章節
```

例如：

```text
00:00
開場與今日直播內容

06:42
新品精華液介紹

18:15
精華液使用方式

27:38
新款粉底介紹

41:52
三款粉底色號比較

58:14
實際上妝

01:14:32
口紅新品介紹

01:37:28
直播限定優惠

01:52:40
觀眾 Q&A

02:09:18
結尾與活動提醒
```

每章包含：

- 標題
- 摘要
- 3～5 個重點
- 可用 Timestamp

即代表 MVP 核心成立。

---

# 30. 建議實際開發順序

最推薦的順序：

```text
① WASAPI 音訊擷取
        ↓
② faster-whisper
        ↓
③ Timestamp Transcript
        ↓
④ 長時間穩定性
        ↓
⑤ AI Topic Segmentation
        ↓
⑥ Chapter Summary
        ↓
⑦ Windows GUI
        ↓
⑧ Browser Extension
        ↓
⑨ 點擊章節跳轉
```

不要一開始就先做：

- 漂亮 UI
- 搜尋
- 登入
- 雲端
- iOS
- Extension

首先驗證最重要的核心：

> 能不能把一支很長、聊天很多、主題不規則的商品直播，可靠地變成「有時間點的 AI 章節導覽」。

---

# 31. 最終產品概念

產品並不是：

> AI 逐字稿工具

而應定位為：

> **AI 長影片快速導覽工具**

核心使用體驗：

```text
原本：
2～3 小時直播
↓
不知道內容在哪
↓
只能快轉或整場看完
```

變成：

```text
2～3 小時直播
↓
AI 自動整理
↓
10～20 個主題
↓
每個主題都有時間點
↓
使用者只看自己想看的部分
```

這是 Windows 版本第一階段最重要的產品方向。

---

# 32. 已確認的 MVP 實作決策

## 32.1 音訊來源

第一階段只提供：

```text
Windows 系統播放音訊
```

介面需明確提醒，其他應用程式的通知音或播放音訊也可能被一併擷取。

確認系統音訊擷取與長時間轉錄穩定後，第二階段再加入以 Windows Application Loopback 為基礎的目標應用程式音訊擷取，優先支援：

- Chrome
- Microsoft Edge
- 使用者選擇的其他音訊應用程式

瀏覽器可能由多個程序組成，因此實作上需處理目標程序與其子程序，不先承諾能精確選擇單一分頁。

## 32.2 模型下載與儲存

Whisper 模型不包入主程式安裝檔，由使用者首次使用時下載。

預設模型位置：

```text
%LOCALAPPDATA%\VideoChapterTool\Models
```

使用者可在下載前指定其他本機資料夾。設定需提供：

- 選擇模型資料夾
- 開啟模型資料夾
- 顯示已安裝模型
- 顯示預估下載大小與磁碟剩餘空間
- 下載失敗後重試
- 刪除不再使用的模型
- 變更路徑時選擇移動既有模型或重新下載
- 啟動時驗證模型完整性
- 離線載入已下載模型

## 32.3 運算裝置

MVP 必須支援 CPU 模式，並以此作為所有 Windows 電腦的基本相容方案。

硬體策略：

```text
CPU               必須支援
NVIDIA CUDA       優先加入正式 GPU 加速
AMD ROCm / HIP    實驗性支援，核心穩定後再驗證
```

GPU 初始化失敗時需自動退回 CPU，不能讓程式無法啟動。

## 32.4 逐字稿與匯出位置

轉錄期間持續將完成片段寫入 SQLite，不等到整場影片結束才保存。

使用者可在介面選擇匯出資料夾，並可設定預設位置。每次工作建立獨立資料夾，例如：

```text
直播名稱_2026-08-14\
├── transcript.txt
├── transcript.json
├── subtitles.srt
└── summary.md
```

內部自動保存位置由程式管理；使用者選擇的是最終匯出位置。

## 32.5 語言與多人聲音

- MVP 的中文工作預設輸出臺灣繁體，使用 `s2twp` 詞彙轉換。
- SQLite 同時保存 Whisper `raw_text` 與正規化後 `text`，避免轉換後無法回溯。
- 英文、數字與無需轉換的內容維持原樣。
- 系統音訊內有兩人或多人說話時，MVP 需支援本機 speaker diarization 並以匿名 A／B／C 標示；資料表已預留可空白的 `speaker_id`。
- 說話者分析採直接整合 `pyannote.audio`，不以完整 WhisperX 取代已驗證的 faster-whisper、Chunk、SQLite 與復原核心。
- 啟用說話者分析時暫時保留成功 Chunk，完成並交易保存 `speaker_id` 後自動刪除；分析失敗時不得影響逐字稿，且應允許重試、略過或稍後清理。
- 介面提供不區分、自動判斷及指定 2～6 人；完成後可將匿名說話者重新命名。
- 台語專用辨識暫緩；正式語言範圍維持臺灣中文及其中穿插的少量英文。

---

# 33. 階段性完成路線圖

每個階段完成並通過驗收後，才進入下一階段。若某階段未通過，優先解決該階段的核心風險，不以增加 UI 或其他功能繞過問題。

## Stage 0：工程基礎與診斷工具

**狀態：已完成（2026-08-14）**

### 目標

建立可持續開發與測試的 Python 專案骨架。

### 產出

- 專案目錄與模組骨架
- Python 依賴與版本鎖定
- 統一設定檔與資料路徑管理
- 結構化應用程式日誌
- Windows、CPU、RAM、音訊裝置與 GPU 基本偵測
- 最小化自動測試架構

### 完成條件

- 新環境可依文件安裝並啟動程式
- 日誌不記錄 API Key 或不必要的逐字稿內容
- 中文路徑與包含空格的路徑可正常處理

---

## Stage 1：Windows 系統音訊擷取 Prototype

**狀態：進行中（WASAPI Loopback 裝置列舉、靜音安全停止與短片段端到端測試已通過；平台與 1 小時驗收待執行）**

### 目標

可靠取得 Windows 系統正在播放的 PCM 音訊。

### 產出

- WASAPI Loopback 裝置列舉
- 系統預設播放裝置擷取
- 取樣率與聲道格式正規化
- 開始、停止與裝置中斷處理
- 音量活動與靜音偵測
- 測試用短片段 WAV 輸出；只供開發驗證，不作為正式產品的完整錄音功能

### 驗收情境

- Chrome 播放一般影片
- Edge 播放一般影片
- Facebook Live / Replay
- Instagram Live / Replay
- 播放裝置切換
- 長時間靜音後恢復播放

### 完成條件

- 連續擷取至少 1 小時
- 不明顯斷音或造成播放器卡頓
- 停止後可完整關閉音訊資源
- 音訊裝置失效時能顯示可理解的錯誤

---

## Stage 2：Whisper 模型管理與基本轉錄

**狀態：進行中（small 模型自訂路徑下載與驗證、CUDA 12 + cuDNN 9 GPU 實際推論、CPU INT8 回退、30.5 分鐘中文串流與 Timestamp JSON 已通過；辨識品質與影片時間人工抽查待執行）**

### 目標

將擷取音訊送入 faster-whisper，先完成 CPU 可用版本。

### 產出

- 模型下載目錄設定
- small 模型下載、驗證、載入與刪除
- CPU INT8 轉錄
- NVIDIA GPU 偵測與可用時的 CUDA 路徑
- AMD GPU 電腦上的 CPU 回退
- 中文與自動語言選擇
- Whisper 處理速度與待處理時間監控

### 完成條件

- 沒有獨立 GPU 的 Windows 電腦可以完成轉錄
- AMD GPU 不會造成程式啟動或轉錄失敗
- GPU 初始化失敗時能安全退回 CPU
- 模型下載中斷後可重試，不破壞既有模型
- 能辨識 30 分鐘以上的連續中文內容

---

## Stage 3：Streaming、Timestamp 與去重

**狀態：核心技術驗收通過（2026-08-14；人工內容抽查仍併入長時間 Gate）**

### 2026-08-14 長度驗證紀錄

- CUDA / float16 連續擷取 1830 秒並完成 80 個 Chunk、840 個文字片段。
- 來源 PCM 實收 1829.867 秒，僅補 0.133 秒靜音，音訊串流狀態事件為 0。
- Whisper 累計處理時間 86.224 秒，Queue 未持續累積，整體工作於擷取結束後 0.793 秒完成。
- JSON 成功保存後 `PendingAudio` 已清空。
- 首次輸出發現 34 個 Chunk 邊界開始時間倒序與 55 組相鄰重疊；已改為捨棄完整落在 overlap 內的替代辨識、裁切跨界片段並強制單調時間軸。
- 修正後 47 項自動測試及 CUDA 短語音回歸通過，倒序與相鄰時間重疊均為 0。
- 修正後再次完成 CUDA / float16 連續擷取 620 秒，27 個 Chunk、297 個片段；來源覆蓋率 99.9363%，只補 0.395 秒靜音，倒序、相鄰重疊與完全重複均為 0。
- 人工抽查實際影片的時間位置與文字內容，併入 Stage 5 長時間穩定性 Gate 持續執行。

### 目標

建立可供長影片使用的串流轉錄管線。

### 產出

- 約 20～30 秒音訊 Chunk
- Chunk 間短時間重疊
- 跨 Chunk 句子合併與重複文字移除
- Timestamp 以擷取音訊樣本數計算，不以辨識完成時間計算
- Producer / Consumer Queue
- Whisper 落後時的待處理音訊暫存
- 停止擷取後繼續處理剩餘 Queue

### 完成條件

- Chunk 邊界不會大量切斷或重複句子
- Whisper 延遲增加時不會直接丟棄尚未辨識的音訊
- 連續正常播放時 Timestamp 不持續漂移
- UI 或日誌可得知目前轉錄延遲

---

## Stage 4：SQLite、自動保存、復原與匯出

**狀態：核心實作與自動驗證完成（2026-08-14；實際低磁碟與含待處理 Chunk 的強制關閉情境併入 Stage 5 壓力測試）**

### 2026-08-14 實作與驗證紀錄

- 建立具 WAL、外鍵與完整交易的 SQLite schema，包含 `videos`、`transcripts`、`pending_chunks`、`chapters` 與 `chapter_points`。
- 每個 WAV Chunk 先原子寫入磁碟並登記，再轉錄及交易保存；交易成功後才清理 WAV。
- 支援未完成工作列舉、`session-recover` 接續轉錄，以及 `session-export` 重新匯出。
- 自動輸出 UTF-8 TXT、JSON、SRT；JSON 同時保存原文與臺灣繁體正規化文字。
- 寫入 Chunk 前檢查磁碟，至少保留 256 MiB 安全空間。
- 12 秒 CUDA / float16 實機煙霧測試完成 3 個 Chunk、2 個片段，SQLite、三種匯出及正常清理均通過。
- 真實強制關閉後可偵測 `capturing` 工作階段並復原；待處理 WAV 的接續轉錄與刪除另由自動測試驗證。
- 共 55 項自動測試涵蓋交易耐久性、復原、匯出、設定遷移、繁體轉換與低磁碟拒絕寫入。

### 目標

確保已取得的逐字稿不因後續錯誤或程式關閉而消失。

### 產出

- videos、transcripts、chapters、chapter_points 資料表
- 每個完成片段立即交易寫入 SQLite
- 工作階段狀態管理
- 未辨識音訊的短期磁碟暫存
- 未完成工作復原介面
- TXT、JSON、SRT 匯出
- 使用者自訂匯出資料夾
- 同名檔案自動編號或由使用者確認

工作階段狀態至少包含：

```text
created
capturing
draining_queue
transcript_ready
ai_analyzing
completed
failed
cancelled
```

### 完成條件

- 強制關閉程式後，可恢復到最後成功保存的片段
- AI 尚未執行也能匯出完整逐字稿
- 匯出失敗不影響 SQLite 內部資料
- 磁碟空間不足時停止新增資料並明確警告

---

## Stage 5：長時間穩定性 Gate

**狀態：已完成（2026-08-19）**

### 2026-08-15 監控與短基準紀錄

- 每次串流自動記錄來源 Frame 覆蓋率、音訊事件、Queue、Timestamp、暫存清理與程序 RAM 樣本。
- RAM 以暖機後線性趨勢計算；少於 30 分鐘只標記 `inconclusive`，不宣稱通過。
- 報告只含統計與錯誤摘要，不含逐字稿全文或音訊。
- 支援 `stability-show <session_id>` 顯示各項 Gate 結果。
- 12 秒短測成功識別到來源音訊未完整覆蓋而判定 `fail`，證明 Gate 能攔截異常資料。
- 6 秒整合測試完成 CUDA、SQLite、Queue 排空、Timestamp、匯出與報告鏈路，結果為 `provisional_pass`。
- 先前 1830 秒與 620 秒 CUDA 測試已證明長串流可完成；因當時尚未採集 RAM 趨勢，本日另以新版報告重新執行正式 30 分鐘 Gate，結果如下。

### 2026-08-15 正式 30 分鐘 Gate

- SQLite 工作階段 `#9`，CUDA / float16，擷取 1800 秒。
- 來源 Frame 覆蓋率 99.9941%，只補 5120 Frame；音訊串流狀態事件 0。
- 79 個 Chunk 全數處理，最高積壓 1 個 Chunk／25 秒，結束待處理量為 0。
- 共 791 個片段；無無效時間範圍、倒序、相鄰重疊或超出擷取範圍。
- Whisper 累計推論 87.267 秒，整個工作只比擷取時長多 0.369 秒。
- RAM 共取樣 362 次；暖機後成長趨勢 5.672 MiB/小時，峰值 785.223 MiB，結束 758.852 MiB。
- `PendingAudio` 為 0，SQLite 未完成工作為 0；Gate 結果 `pass`。

### 2026-08-18 正式 1 小時 Gate

- SQLite 工作階段 `#10`，CUDA / float16，擷取 3600 秒。
- 來源 Frame 覆蓋率 99.8370%，補入 281600 Frame；音訊串流狀態事件 0。
- 157 個 Chunk 全數處理，最高積壓 1 個 Chunk／25 秒，結束待處理量為 0。
- 共 1669 個片段；無無效時間範圍、倒序、相鄰重疊或超出擷取範圍。
- Whisper 累計推論 154.294 秒，整個工作只比擷取時長多 0.306 秒。
- RAM 共取樣 721 次；暖機後成長趨勢 3.230 MiB/小時，峰值 765.504 MiB，結束 760.137 MiB。
- `PendingAudio` 為 0，SQLite 未完成工作為 0；候選版本 Gate 結果 `pass`。

### 2026-08-19 正式 3 小時 Gate

- SQLite 工作階段 `#11`，CUDA / float16，擷取 10800 秒；使用循環播放影片完成目前 MVP 最長時數驗證。
- 來源 Frame 覆蓋率 99.9044%，補入 495616 Frame；音訊串流狀態事件 0。
- 470 個 Chunk 全數處理，最高積壓 1 個 Chunk／25 秒，結束待處理量為 0。
- 共 5040 個片段；無無效時間範圍、倒序、相鄰重疊或超出擷取範圍。
- Whisper 累計推論 473.781 秒，整個工作只比擷取時長多 0.644 秒。
- RAM 共取樣 2161 次；暖機後成長趨勢 1.733 MiB/小時，峰值 767.582 MiB，結束 760.293 MiB。
- `PendingAudio` 為 0，SQLite 未完成工作為 0；發布前最長時數 Gate 結果 `pass`。

### 2026-08-19 強制關閉與復原

- 工作階段 `#12` 在擷取中直接終止 PowerShell，SQLite 正確保留 `capturing` 未完成狀態。
- 強制關閉前 3 個 Chunk 已完成交易保存並安全清除 WAV，共保留 24 個片段至 71 秒。
- `session-recover 12` 成功從已保存逐字稿恢復正確工作長度、完成 TXT／JSON／SRT 匯出，未完成工作與待處理 WAV 均歸零。
- 測試發現「沒有待處理 WAV 時沿用原始 `duration=0`」的復原記帳問題；已改為取已保存逐字稿的最後時間並加入回歸測試。
- 修正後共 59 項自動測試通過。

### 2026-08-19 長時間靜音與恢復

- 首次 180 秒實測在約 60 秒暫停後恢復，來源只收到約 123 秒 Frame；舊實作將缺少的約 57 秒靜音補到尾端，造成恢復後 Timestamp 被壓縮，最大文字間隔僅 10.1 秒。
- 串流時間軸已改為依牆鐘持續校準；WASAPI 在靜音時停止 callback，會在靜音發生位置即時補零，而非結束時集中補入。
- 修正後工作階段 `#15` 在 70.81 秒至 131.72 秒之間保留 60.91 秒空白，符合實際暫停時間。
- 修正後 8 個 Chunk 全數處理，最高積壓 1 個 Chunk，Timestamp 結構錯誤、殘留 WAV 與未完成工作均為 0。
- Gate 因刻意靜音使來源覆蓋率為 71.7511% 而顯示 `fail`，但此失敗符合測試設定；靜音位置與恢復時間軸功能驗收通過。
- 加入預設播放裝置每秒監控，裝置切換時應明確停止並保留已完成內容；共 64 項自動測試通過。

### 2026-08-19 播放裝置與使用者停止

- 本機只列出一個 Realtek WASAPI Loopback（索引 10）；筆電喇叭與 3.5 mm 耳機是同一端點內部路由，插拔耳機不構成 Windows 預設端點索引切換，因此程式持續運作符合系統狀態。
- 不同預設端點切換已加入每秒索引監控；偵測到索引改變時會停止擷取、保留已完成內容並顯示可理解錯誤。因本機沒有第二個獨立端點，採自動測試驗收。
- 工作階段 `#16` 在 94 秒由使用者按 `Ctrl+C`；舊版將沒有訊息的 `KeyboardInterrupt` 顯示成空白擷取錯誤，已改為提前停止、排空 Queue、自動匯出並記錄 `cancelled`。
- 復原時長進一步改用已完成 Chunk 與逐字稿的最遠時間；`#16` 的 4 個完成 Chunk 正確恢復為 94 秒，34 個片段與匯出均保留。
- 自動測試增至 65 項並全數通過；Stage 5 長時間、復原、靜音時間軸、Queue、磁碟與可測裝置情境完成驗收。

### 目標

在投入 AI 與完整 GUI 前，先證明核心轉錄管線可長時間運行。

### 測試層級

```text
30 分鐘    每日基本測試
1 小時     每個候選版本必要測試
3 小時     Alpha 前必要測試
```

目前使用情境不包含 6 小時影片，因此 3 小時為 MVP 最長壓力測試。程式可保留更長擷取上限供未來需求使用，但不列入現在的交付門檻。

### 必測情境

- Whisper 比播放速度慢
- 長時間靜音
- 音訊裝置切換或移除
- 電腦休眠與喚醒
- 網路中斷；模型已下載時不應影響本機轉錄
- 磁碟空間接近不足
- 程式意外關閉與復原
- 使用者停止後仍有待處理 Queue

### 完成條件

- 不遺失已成功擷取的音訊片段
- 暖機後 RAM 不隨影片時間無限制成長
- Queue 積壓不會全部留在 RAM
- Timestamp 在連續播放情境下不持續漂移
- 意外關閉後能復原已保存逐字稿與待處理工作
- 30 分鐘、1 小時與 3 小時測試均產生可診斷報告

Stage 5 未通過前，不開始製作完整產品 UI。

---

## Stage 6：AI 自動分章與摘要

**狀態：已完成（供應商中立核心、OpenAI Provider、75 項自動測試及真實商品直播品質驗收均通過）**

### 目標

把已保存的 Timestamped Transcript 轉為章節導覽。

### 產出

- LLMProvider 抽象介面
- Transcript Chunk 初步主題分析
- Chapter Boundary 合併與拆分
- 章節標題、摘要與 3～5 個重點
- 全片摘要
- 結構化輸出驗證
- 時間範圍、順序與章節重疊檢查
- 單章失敗重試，不重送全部內容
- Markdown 匯出

### 完成條件

- API 失敗不影響逐字稿
- AI 輸出錯誤時可重試或保留部分成功結果
- 章節時間均落在影片範圍內且順序正確
- 實際商品直播不會被固定時間機械切割
- 使用測試影片人工評估章節切點是否合理

### 2026-08-19 實作紀錄

- SQLite schema 升級至 v4，保存正式摘要、AI 供應商／模型、章節、章節重點與每個分批的成功或失敗快取。
- Transcript 依文字量與最長時間分批，但不切斷單一逐字稿片段；快取雜湊包含語言、時間戳與文字內容。
- 每批結果驗證標題、摘要、3～5 個重點、時間範圍、順序及重疊；成功批次立即落盤，失敗只重試該批。
- 初步章節完成後另經跨批次整併，允許同一語意主題跨越文字批次邊界，不將固定分批機械化為章節。
- 所有批次與最終摘要均成功後才以單一 SQLite 交易取代正式 AI 結果；失敗時逐字稿與已成功批次保留。
- 已加入 `summary.md` 原子匯出與 `ai-export` CLI。
- 離線假 Provider 已驗證暫時失敗重試、永久失敗保全、成功批次重用、跨批次合併、結構驗證與 Markdown 匯出。
- 第一個實際 Provider 採 OpenAI Responses API 與 `gpt-5.6-luna`，使用 strict JSON Schema、`store=false`、SDK 內建重試停用（統一由分批管線局部重試），且只從 `OPENAI_API_KEY` 環境變數讀取金鑰。
- OpenAI SDK 已鎖定 `2.51.0`；完整語法與自動測試為 75 項通過。
- 已以工作階段 #9 的 30 分鐘商品直播完成真實 OpenAI API 品質 Gate：2 個文字批次、4 次 API 呼叫，產生全片摘要與 9 個正式章節，結果寫入 SQLite 並匯出 `summary.md`。
- 正式章節涵蓋時間軸 99.394%，總空隙 7 秒、零重疊，每章均有 3～5 個重點；人工抽查切點與原始逐字稿內容一致。
- 901.5 秒文字批次邊界落在同一個「小杏仁刷具」主題內，最終整併成功產生跨越該邊界的 880.0～1208.5 秒章節，證實分批不會機械化為章節切點。
- 第一章觀察到約 1 秒的相鄰章節語意外溢，未造成導航錯誤，先列為後續多案例提示詞校正項目；不為單一輕微案例改版並增加付費重跑。

---

## Stage 6.5：本機雙人／多人說話者分離

**狀態：核心與跨裝置短 Gate 通過（指定 2 人、自動 2～6 人、NVIDIA CUDA、CPU 與人工品質均已驗收；30 分鐘雙人品質及 Alpha 前 3 小時新流程保留為發布 Gate）**

### 技術決策

- 直接整合 `pyannote.audio` 的本機 `community-1` diarization pipeline。
- 不導入完整 WhisperX：它同樣以 pyannote 執行說話者分離，卻會重疊現有轉錄管線，且目前正式版限制 Python `<3.14`，與專案 Python 3.14.3 不相容。
- 保留 `SpeakerDiarizer` 抽象層，未來若需要更精細的中文字詞時間對齊，可局部替換對齊器，不綁死單一實作。
- 啟用說話者分析時使用 faster-whisper 內建 word timestamps；原始 `transcripts` 不因說話者分析而改寫，另以 `transcript_words`、`speaker_turns` 與說話者別名資料保存結果。
- pyannote 模型下載位置可指定；首次下載所需的 Hugging Face 授權與 Token 不寫入 SQLite、匯出檔或一般日誌，模型下載完成後於本機執行。

### 資料流程

```text
系統音訊 Chunk
    ├─ faster-whisper 轉錄與持續保存
    └─ 暫時保留音訊
             ↓
停止擷取並排空轉錄 Queue
             ↓
重建不含 Chunk overlap 的單聲道分析音訊
             ↓
pyannote 判斷說話者時間區段
             ↓
將字詞時間與匿名 A／B／C 對齊
             ↓
交易保存 speaker_turns／別名並重新匯出
             ↓
刪除分析音訊與已完成 Chunk
```

### 產出

- 說話者分析設定：關閉、自動判斷 2～6 人、指定 2～6 人；單人內容使用關閉
- 匿名且單一工作階段內一致的說話者 A／B／C 標籤
- 完成後重新命名說話者
- 保留原始逐字稿的字詞時間與獨立 `speaker_turns` 資料
- TXT、JSON、SRT 與後續 GUI 顯示說話者
- 分析進度、取消、失敗重試與略過
- pyannote 模型路徑與下載狀態管理
- 成功後自動清理暫存音訊；失敗時明確顯示保留位置與所需空間

### 限制

- 說話者標籤不是身分辨識，不會自動知道主持人姓名。
- 兩人同時說話時可偵測重疊，但單一混合音軌的 Whisper 不保證完整轉錄雙方內容。
- 背景音樂、極短插話、相似聲線與音量差距可能造成標籤錯置。
- 台語專用語音辨識不屬於本階段。

### 2026-08-22 實測紀錄

- 工作階段 #17 使用 5 分鐘真實雙人直播、指定 2 人與 NVIDIA RTX 5060 CUDA 完成端到端測試。
- 300 秒擷取、13 個 Chunk、128 個逐字稿片段、60 個說話者時間區段；所有逐字稿片段均有 A／B 標籤，沒有未標註片段。
- 成功後分析 WAV 與 Chunk 已清理，SQLite、TXT、JSON、SRT 均可保存及重建說話者標籤。
- 人工比對：開頭一至兩句在兩人聲音不夠清楚時可能混淆；角色聲音確立後沒有再發生 A／B 交換。此為可接受的冷啟動限制，指定 2 人／CUDA 品質 Gate 判定通過。
- 工作階段 #19 的完全無提示自動估算首次只判斷出 1 人；產品的多人「自動」因此改採 pyannote 官方支援的 `min_speakers=2`、`max_speakers=6` 有界估算。沿用保留音訊重測後成功判定 2 人並產生 69 個說話者區段，人工比對沒有發現問題，自動人數 Gate 通過。
- 相同工作階段再以 CPU、指定 2 人重跑，完成 2 位說話者與 69 個區段，且保留分析 WAV 供後續 Gate 使用；CPU fallback 短 Gate 通過。
- 已將 Triton FLOP 統計、TF32 精度設定、極短 embedding window 與新版 Windows 缺少 WMIC 等非致命底層訊息轉為受控診斷；未知警告仍會顯示。
- 待補：30 分鐘實際雙人內容人工品質 Gate，以及 Alpha 前 3 小時新流程的儲存、RAM、失敗復原與清理 Gate。這兩項不阻擋 GUI 工程開始，但在對應發布節點前必須完成。

### 完成條件

- 實際雙人直播能將主要對話穩定區分為 A／B，不因 25 秒 Chunk 邊界頻繁交換標籤。
- 自動人數與指定 2 人模式均完成實測，GUI 預設採自動並記住使用者上次選擇。
- 加入說話者標籤不改寫或遺失原始逐字稿文字與 Timestamp。
- 分析失敗、取消或程式關閉後，可沿用 SQLite 逐字稿與保留音訊重試。
- 分析成功後暫存音訊完整清理，匯出可由 SQLite 重新產生。
- NVIDIA GPU 與 CPU 路徑至少各完成短測；30 分鐘實際雙人內容通過人工品質 Gate。
- Alpha 前以新流程重跑 3 小時儲存、RAM、失敗復原與清理 Gate。

---

## Stage 7：Windows GUI 整合

**狀態：核心完成（Phase 1～4 GUI 錄製、AI、模型管理與工作階段復原均通過 Gate；發布前長時間與雙人品質 Gate 仍依原計畫保留）**

### 目標

以 PySide6 將已驗證的核心模組整合成可操作的桌面程式。

### 主要畫面

1. 首次啟動與硬體檢測
2. 模型下載與模型路徑設定
3. 系統音訊擷取與說話者人數設定
4. 即時逐字稿與轉錄延遲
5. 停止與剩餘 Queue 處理
6. AI 章節與摘要
7. 匯出位置與格式
8. 未完成工作復原
9. 設定與診斷紀錄

### 完成條件

- 使用者不需命令列即可完成整個流程
- 暫停、停止、關閉與錯誤狀態含義清楚
- 介面操作不阻塞音訊擷取或 Whisper 執行緒
- 長任務有進度、延遲與取消回饋
- API Key 不以明文顯示在介面或日誌
- 說話者分析可關閉、自動判斷或指定人數；目前使用匿名 A／B／C 標籤，重新命名列為後續功能

### Phase 1 實作紀錄（2026-08-22）

- 鎖定 `PySide6-Essentials 6.11.2`，只納入目前 Qt Widgets 所需模組，未提前加入未使用的 Qt Addons。
- 新增獨立 `python -m app.gui --data-dir ...` 入口，不影響既有 CLI 與可復原工作流程。
- 總覽頁可讀取最近 20 個 SQLite 工作階段，顯示狀態、長度與轉錄運算環境。
- 硬體診斷透過 Qt thread pool 背景執行，不阻塞介面；錯誤訊息先經既有敏感資訊遮罩。
- 設定頁可選模型與匯出資料夾、Whisper 裝置、說話者關閉／自動／指定人數，以及 pyannote 裝置。
- 設定 schema 升至 v3，舊 v1／v2 自動補上安全預設值；OpenAI API Key 與 Hugging Face Token 只顯示「已設定／未設定」，不保存內容。
- 「開始新的錄製」在串流控制器尚未整合前維持停用，避免形成不可恢復的半成品流程。
- 91 項自動測試、Python compileall、Qt offscreen 啟動與繁體中文畫面渲染均通過。

### Phase 2 實作紀錄（2026-08-22）

- 新增錄製頁，可由 GUI 啟動系統音訊擷取並手動停止；內部以 6 小時作為遺留程序的安全上限，不要求使用者事先預估直播長度。
- 錄製背景工作顯示時間、系統音訊峰值、Chunk 數、Queue 與估計積壓；每個 Chunk 完成後提供尚未標註 A／B 的暫時逐字稿。
- 停止時不直接中斷處理，而是完成目前音訊、排空 Queue、保存 SQLite，再依設定進行本機說話者分析與 TXT／JSON／SRT 匯出。
- 錄製期間呼叫 Windows `SetThreadExecutionState` 防止系統睡眠；預設同時保持顯示器開啟，完成後恢復系統原有電源政策。
- OpenAI API Key 與 Hugging Face Token 可保存於目前使用者的 Windows Credential Manager；畫面不回填明文，環境變數仍具有較高優先權。
- 設定 schema 升至 v4，舊版本自動補上「錄製時保持螢幕開啟」安全預設。
- 人數控制改為明確的 2～6 人下拉選單；儲存按鈕固定在可見位置，開始錄製時也會自動保存畫面設定，並在錄製頁顯示實際採用的說話者模式。
- 100 項自動測試（含完整 GUI 手動停止工作流與畫面設定自動套用）、Python compileall、Qt offscreen 啟動、錄製頁渲染與 `pip check` 均通過。
- 下一個 GUI Gate：由使用者完成 3～5 分鐘真實雙人錄製，確認音量、暫時逐字稿、手動停止、A／B 匯出與暫存音訊清理。

### Phase 3 實作紀錄（2026-08-22）

- 新增「AI 章節摘要」頁，可選擇任何已有逐字稿的 SQLite 工作階段，並在重新開啟程式後載入既有摘要與章節。
- 開始分析前明確提示只傳送逐字稿文字、時間戳及說話者標籤，不傳送音訊，並提醒會使用 OpenAI API 額度。
- AI 管線改由 GUI 背景執行，不阻塞介面；顯示目前文字批次、已沿用快取及 API 呼叫數，支援在目前 API 請求完成後停止。
- 成功文字批次會立即保留快取；取消或失敗不刪除逐字稿。重新分析時，既有正式摘要會保留到新結果完整成功後才以交易取代。
- 完成後在介面預覽 Markdown，可直接開啟 `summary.md` 或工作階段輸出資料夾；沒有 OpenAI Key 時會導向設定頁。
- 工作階段 #23 已由 GUI 完成約 4 分鐘指定 2 人實測，說話者覆蓋率 99.27%、61 個說話者區段、待處理 Chunk 為 0，人工檢視較前次結果改善。
- 103 項自動測試、Python compileall、Qt offscreen 啟動、AI 頁面離線渲染與 `pip check` 均通過；測試使用假 Provider，不產生 OpenAI API 費用。
- Phase 3 GUI Gate：由使用者以 3～5 分鐘既有工作階段執行一次真實 OpenAI 分析，確認進度、Markdown 預覽、`summary.md`、重新載入與快取重試行為。
- 工作階段 #23 已完成第一次真實 OpenAI GUI Gate：4 分 17 秒逐字稿整理為 4 個章節，摘要預覽、`summary.md`、輸出資料夾與重新載入皆由使用者確認正常。
- Gate 後修正 AI 進度條文字對齊與完成狀態：百分比置中且保留最小寬度，已有完整摘要的工作階段重新載入時顯示 `100%`，不再回到容易誤解的 `0%`。

### Phase 4 實作紀錄（2026-08-22）

- 新增「模型與復原」頁，列出 `small`／`medium` Whisper 模型的驗證狀態、大小及實際位置；下載與續傳在 Qt thread pool 背景執行，不阻塞介面。
- 模型移除需人工輸入完整模型名稱，並沿用 `ModelManager` 的根目錄與符號連結安全檢查；只刪除選定模型，不碰 SQLite、匯出檔或說話者模型。
- 設定頁新增 Whisper 模型選擇，下載 `medium` 後可實際切換後續錄製使用的模型，不再只能固定使用 `small`。
- GUI 可列出未完成工作階段及其已保存逐字稿、待處理 Chunk、可讀 WAV 和上次錯誤，並可沿用原工作階段設定執行復原及重新匯出。
- 復原加入逐 Chunk 進度與安全停止點；成功 Chunk 立即提交 SQLite，停止或失敗時剩餘 WAV 保留。沒有待處理 Chunk 時直接沿用已保存逐字稿，不載入 Whisper 模型。
- 若原工作階段等待說話者分析，復原逐字稿後會接續本機 pyannote 分析；失敗仍保留逐字稿與分析音訊供下次重試。
- 107 項自動測試通過，包含待處理 WAV 端到端復原、無待處理音訊的快速完成、Chunk 間取消保全、模型／復原頁載入與進度顯示。
- Phase 4 GUI Gate：確認 `small` 顯示「可使用」，再以目前列出的未完成工作階段執行復原，檢查工作階段從清單移除、輸出可開啟且待處理音訊依結果清理。
- 工作階段 #18 已完成真實復原 Gate：介面顯示 0 個待處理 Chunk、0 段逐字稿與 1 個保留音訊；完成空白逐字稿重新匯出與保留音訊清理後，該工作階段從未完成清單移除，復原輸出可正常開啟。
- Gate 後修正模型表格在較矮視窗被壓縮的問題：表格保留足以顯示標題與兩個模型的最低高度，整個頁面改用垂直捲動；已驗證的選定模型顯示 `100%`。
- 錄製設定的 Whisper 選單改為依模型驗證狀態啟用：`small（可使用）` 可選，未下載的 `medium` 顯示狀態但停用；下載完成、刪除模型或更換模型資料夾後會自動重新同步。

---

## Stage 8：Alpha EXE

### 目標

產生可交付內部測試的 Windows 執行版本。

### 產出

- PyInstaller one-folder 版本
- CPU 基本套件
- NVIDIA GPU 選用支援或獨立測試版本
- 模型保持外部下載
- 版本資訊與診斷日誌匯出
- Alpha 使用說明與已知限制

### 完成條件

- 乾淨 Windows 10 / 11 電腦不安裝 Python 也能執行
- 中文使用者名稱與路徑可正常使用
- 沒有 NVIDIA GPU 時不會因 CUDA 元件失敗
- 更新 EXE 不會刪除模型、SQLite、匯出檔案或設定
- Windows Defender 與常見防毒行為已記錄

### 目前實作狀態（2026-08-22）

- 已建立 `0.1.0a1` PyInstaller 6.20.0 one-folder Alpha；隨附 Python、Qt、系統音訊、Whisper、OpenAI 與 pyannote/PyTorch 執行元件，模型與使用者資料維持外置。
- frozen EXE 已在中文隔離資料路徑通過 GUI Smoke；六組執行元件的深層自我檢查全部通過。
- NVIDIA Gate 偵測到 CTranslate2 CUDA 裝置與 PyTorch CUDA 13；移除 Python/CUDA PATH 並設定隱藏 GPU 後，CTranslate2 顯示 0 裝置、PyTorch CUDA 為 false，CPU 回退自我檢查仍通過。
- Alpha 未壓縮約 3.78 GiB；建置腳本會保存版本、SHA-256、runtime Gate 與 CPU 回退 Gate 結果。
- 尚待另一台乾淨 Windows 10／11 電腦進行無 Python 實機驗收，並記錄 Windows Defender／SmartScreen 行為；完成前 Stage 8 維持 Alpha Gate 狀態。

---

## Stage 9：Portable Beta 與發布候選

### 目標

將 Alpha 整理成可攜、免安裝且能安全搬移、備份與覆蓋更新的版本。目前產品以單機個人使用為主，不製作 Setup EXE。

### 產出

- one-folder Portable ZIP；不可只取出單一 EXE
- 程式旁的 `UserData` 保存設定、SQLite、日誌、暫存與預設匯出
- 模型與匯出資料夾仍可在介面另外指定
- 內部資料路徑使用相對表示，整個資料夾搬移後仍可復原
- 覆蓋更新、完整備份與移除方式說明
- 輪替日誌、GUI 未處理錯誤記錄與不含敏感內容的診斷包
- Release Notes
- 隱私與雲端 AI 資料傳送說明
- 若未來公開散布，再評估程式碼簽章與安裝版

### 完成條件

- 首次啟動會在 EXE 旁建立完整 `UserData`
- 整個 Portable 資料夾搬移後，設定、SQLite 與待復原音訊仍可使用
- 覆蓋程式檔更新不會取代 `UserData`；備份及手動移除步驟已文件化
- 一般日誌不含逐字稿、音訊與憑證，診斷包不含 SQLite
- CPU、NVIDIA GPU 與 AMD GPU + CPU 回退情境均通過
- 3 小時壓力測試通過
- 已知限制已在介面或說明文件揭露

---

## Stage 10：MVP 後硬體與瀏覽器整合

核心 MVP 穩定後依序評估：

1. AMD ROCm / HIP GPU 實驗性加速
2. 指定應用程式音訊擷取
3. Chrome / Edge Browser Extension
4. 讀取真正的 video.currentTime
5. 點擊章節跳轉
6. 快轉、倒退、暫停與中途播放的時間軸校正

這些功能不阻擋 CPU + 系統音訊版本的 MVP 交付。

---

# 34. 失敗情境處理原則

1. 已完成逐字稿永遠優先保存。
2. 音訊擷取、Whisper、AI 與匯出需能各自失敗及重試。
3. AI 失敗不得要求使用者重新播放影片。
4. 未辨識音訊在成功處理前不得因 Ring Buffer 覆寫而遺失。
5. 每個錯誤需記錄階段、時間、可恢復範圍與建議動作。
6. 程式重啟後需偵測未完成工作。
7. 使用者關閉程式時，若仍有未保存或未辨識資料，必須提示。
8. API Key、權杖與敏感設定不得寫入一般日誌。

---

# 35. 版本交付物

## 開發 Prototype

- Python 原始碼
- 測試指令
- 診斷日誌
- 音訊與轉錄驗證報告

## Alpha

- 可執行資料夾版本
- Alpha 使用說明
- 已知問題清單
- 測試資料與測試報告

## Beta

- Windows Setup EXE
- 安裝、升級與解除安裝測試報告
- 30 分鐘、1 小時與 3 小時穩定性報告
- 模型下載與硬體相容性說明

## 正式版

- 簽章安裝程式
- Release Notes
- 隱私說明
- 使用說明
- 可匯出的診斷資訊

---

# 36. MVP Definition of Done

第一版 MVP 完成需同時符合：

1. 可在 Windows 10 / 11 擷取系統播放音訊。
2. CPU 模式可使用 faster-whisper small 完成中文長影片轉錄。
3. 使用者可指定 Whisper 模型與輸出檔案的儲存位置。
4. 逐字稿包含可靠 Timestamp，並可輸出 TXT、JSON、SRT。
5. 程式意外關閉後可復原已保存內容。
6. Whisper 落後時不丟棄音訊，停止後可處理剩餘 Queue。
7. AI 可輸出語意章節、摘要、重點與 Markdown。
8. AI 或網路失敗不影響逐字稿保存與匯出。
9. 連續 3 小時壓力測試通過。
10. 可透過 Windows GUI 完成操作。
11. 可交付不需預先安裝 Python 的 Windows 安裝版。
12. AMD GPU 電腦可透過 CPU 正常執行；AMD GPU 加速不作為 MVP 交付門檻。
