# tokyoinnmonitor

監控東橫 Inn 空房：支援依**地區 ID**或**都道府縣代碼**搜尋，可同時監控多個目標，只要有空房即通知。

## 功能

- 支援兩種搜尋方式（可同時使用）：
  - **地區 ID**：`AREA_IDS=473,475,...`
  - **都道府縣代碼**：`PREFECTURES=13-all,...`（例如 `13-all` = 東京都全部）
- 自動讀取每個搜尋目標下所有飯店並查空房
- 日期填寫以 **GMT+8（香港／台灣／日本）** 當天 00:00 為基準，自動換算 UTC
- 內建排程常駐執行（不需另設 crontab）
- 通知模式：
  - `NOTIFY_WHEN_AVAILABLE_ALWAYS=true`：只要有空房，每次循環都通知
  - `NOTIFY_WHEN_AVAILABLE_ALWAYS=false`：僅在狀態變化時通知（去重）
- 支援 Telegram 與 LINE Bot（Messaging API Push）
- 通知訊息中的日期顯示為 GMT+8 本地日期（即你填寫的日期）
- 監控失敗時同樣發送錯誤通知，不會靜默失敗
- 內建請求節流與隨機抖動，降低短時間重複請求風險
- API request headers 盡量模擬一般瀏覽器請求

## 使用方式

1. 複製設定檔：

```bash
cp .env.example .env
```

2. 編輯 `.env`：

- 搜尋目標（至少填一項）：
  - `AREA_IDS`：地區 ID，逗號分隔，例如 `473,475`
  - `PREFECTURES`：都道府縣代碼，逗號分隔，例如 `13-all`
- 必填：`CHECKIN_DATE`（格式 `YYYY-MM-DD`，視為 GMT+8 當天 00:00）
- 選填：`CHECKOUT_DATE`（不填預設為入住 +1 天）
- 選填：`NUMBER_OF_PEOPLE`, `NUMBER_OF_ROOM`, `SMOKING_TYPE`
- 選填：`HOTEL_CODES`（不填就查各目標全部飯店）
- 單次查詢節流：`MIN_REQUEST_INTERVAL_SECONDS`, `REQUEST_JITTER_SECONDS`, `AREA_LOOP_DELAY_SECONDS`
- 內建排程：`SCHEDULE_INTERVAL_SECONDS`, `SCHEDULE_JITTER_SECONDS`, `RUN_ONCE`
- Telegram：`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- LINE Bot：`LINE_BOT_CHANNEL_ACCESS_TOKEN` + `LINE_BOT_TO`

3. 啟動監控（常駐）：

```bash
python3 main.py
```

程式會一直跑，每隔 `SCHEDULE_INTERVAL_SECONDS + 隨機抖動` 自動查一次。

## 日期填寫說明

`CHECKIN_DATE` / `CHECKOUT_DATE` 填 `YYYY-MM-DD` 時，視為 **GMT+8 當天 00:00**，程式自動換算為 UTC：

| 填寫值 | 實際查詢 UTC |
|---|---|
| `2026-04-04` | `2026-04-03T16:00:00.000Z` |
| `2026-04-05` | `2026-04-04T16:00:00.000Z` |

亦可直接填完整 UTC ISO8601，例如 `2026-04-03T16:00:00.000Z`。

通知訊息中顯示的日期會換算回 GMT+8，即你原本填寫的日期。

## LINE Bot 設定重點

- 建立 LINE Messaging API Channel，取得 `Channel access token`。
- `LINE_BOT_TO` 需填可推播的目標 ID（`userId` / `groupId` / `roomId`）。
- Bot 需要已加入該對話（或群組），且具備推播權限。

## 通知邏輯

- 預設（`NOTIFY_WHEN_AVAILABLE_ALWAYS=true`）：
  - 只要「有任一飯店有空房」就通知。
- 若改成 `false`：
  - 啟用去重模式，只有空房結果改變才通知。
- 任一搜尋目標查詢失敗時，會傳送錯誤通知，並繼續處理其餘目標。

## 防 bot 注意事項

- 每次 API 請求前會套用最小間隔 + 隨機抖動。
- 多目標輪詢時會插入延遲，避免高頻連打。
- Header 使用瀏覽器風格（User-Agent / Accept / Referer / Origin 等）。

## 注意

- `SEARCH_URL` 含有 Next.js build hash（`_next/data/<hash>/...`），網站重新部署後此值會變，需手動更新 `main.py` 中的 `SEARCH_URL`。
- 目標網站 API 若調整欄位結構，解析規則可能需微調。
