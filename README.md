# 文章改寫工具

本地 WebUI 工具，批次呼叫 Anthropic API 對 `.txt` 檔案進行改寫。每個檔案獨立呼叫 API，不共享對話記錄。

## 需求

- Python 3.8+
- Anthropic API 金鑰

## 安裝與設定

**1. 複製 API 金鑰設定檔**

```
copy .env.example .env
```

以文字編輯器開啟 `.env`，填入你的 API 金鑰：

```
ANTHROPIC_API_KEY=sk-ant-...
```

**2. 啟動**

雙擊 `start.bat`，或在終端機執行：

```
.\start.bat
```

瀏覽器會自動開啟 `http://127.0.0.1:8766`。

## 使用方式

1. 選擇「輸入資料夾」（含 `.txt` 檔案）與「輸出資料夾」
2. 輸入改寫提示詞，或點「載入提示詞檔案」從 `.txt` 讀取
3. 選擇模型
4. 選擇處理模式，按「開始處理」

## 處理模式

| 模式 | 說明 |
|------|------|
| **標準模式** | 逐檔呼叫 API，即時顯示進度。支援 Token 用量警告與暫停功能。 |
| **批次 API** | 一次提交所有檔案，Anthropic 非同步處理，費用省 50%。適合不急著拿結果的大量任務。 |

### 批次 API 注意事項

- 提交後可關閉瀏覽器，批次在 Anthropic 雲端處理
- 使用「查詢批次進度」輸入 Batch ID 隨時查詢狀態
- Batch ID 會在提交後自動填入查詢欄位

## Token 用量控制（標準模式）

設定累計 Token 警告閾值（預設 100,000），達到後會暫停並詢問是否繼續。勾選「不限制」可停用警告。

## 專案結構

```
article-rewriter/
├── main.py          # FastAPI 後端
├── index.html       # WebUI 前端
├── start.bat        # 一鍵啟動腳本
├── requirements.txt
├── .env             # API 金鑰（不上傳 GitHub）
├── .env.example     # 金鑰設定範本
└── .gitignore
```

## API 金鑰安全

`.env` 已加入 `.gitignore`，不會被上傳至 GitHub。請勿直接將金鑰寫入程式碼。
