# tokyoinnmonitor

以「地區」為主監控東橫 Inn 空房：支援同時監控多個地區，且只要該地區任一飯店有空房就通知。

## 功能

- 支援多地區監控（`AREA_IDS=475,476,...`）
- 自動讀取每個地區下所有飯店並查空房
- 內建排程常駐執行（不需你自己再寫 crontab）
- 通知模式：
  - `NOTIFY_WHEN_AVAILABLE_ALWAYS=true`：只要該地區有空房，每次循環都通知
  - `NOTIFY_WHEN_AVAILABLE_ALWAYS=false`：僅在狀態變化時通知（去重）
- 支援 Telegram 與 LINE Bot（Messaging API Push）
- 內建請求節流與隨機抖動，降低短時間重複請求風險
- API request headers 盡量模擬一般瀏覽器請求

## 使用方式

1. 複製設定檔：

```bash
cp .env.example .env
```

2. 編輯 `.env`：

- 必填：`AREA_IDS`, `CHECKIN_DATE`
- 選填：`CHECKOUT_DATE`, `NUMBER_OF_PEOPLE`, `NUMBER_OF_ROOM`, `SMOKING_TYPE`
- 選填：`HOTEL_CODES`（不填就查各地區全部）
- 單次查詢節流：`MIN_REQUEST_INTERVAL_SECONDS`, `REQUEST_JITTER_SECONDS`, `AREA_LOOP_DELAY_SECONDS`
- 內建排程：`SCHEDULE_INTERVAL_SECONDS`, `SCHEDULE_JITTER_SECONDS`, `RUN_ONCE`
- Telegram：`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- LINE Bot：`LINE_BOT_CHANNEL_ACCESS_TOKEN` + `LINE_BOT_TO`

3. 啟動監控（常駐）：

```bash
python3 monitor.py
```

程式會一直跑，每隔 `SCHEDULE_INTERVAL_SECONDS + 隨機抖動` 自動查一次。

## LINE Bot 設定重點

- 建立 LINE Messaging API Channel，取得 `Channel access token`。
- `LINE_BOT_TO` 需填可推播的目標 ID（`userId` / `groupId` / `roomId`）。
- Bot 需要已加入該對話（或群組），且具備推播權限。

## 通知邏輯

- 預設（`NOTIFY_WHEN_AVAILABLE_ALWAYS=true`）：
  - 只要該地區「有任一飯店有空房」就通知。
- 若改成 `false`：
  - 啟用去重模式，只有空房結果改變才通知。

## 防 bot 注意事項

- 每次 API 請求前會套用最小間隔 + 隨機抖動。
- 多地區輪詢時會插入延遲，避免高頻連打。
- Header 使用瀏覽器風格（User-Agent / Accept / Referer / Origin 等）。

## 注意

- 目標網站 API 若調整欄位結構，解析規則可能需微調。
