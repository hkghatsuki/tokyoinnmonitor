# tokyoinnmonitor

以「地區」為主監控東橫 Inn 空房：支援同時監控多個地區，且只要該地區任一飯店有空房就通知。

## 功能

- 支援多地區監控（`AREA_IDS=475,476,...`）
- 自動讀取每個地區下所有飯店並查空房
- 通知模式：
  - `NOTIFY_WHEN_AVAILABLE_ALWAYS=true`：只要該地區有空房，每次執行都通知
  - `NOTIFY_WHEN_AVAILABLE_ALWAYS=false`：僅在狀態變化時通知（去重）
- 支援 Telegram 與 LINE Notify
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
- 節流：`MIN_REQUEST_INTERVAL_SECONDS`, `REQUEST_JITTER_SECONDS`, `AREA_LOOP_DELAY_SECONDS`
- Telegram：`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
- LINE：`LINE_NOTIFY_TOKEN`

3. 執行：

```bash
python3 monitor.py
```

## 定時執行（crontab）

每 15 分鐘跑一次：

```bash
*/15 * * * * cd /path/to/tokyoinnmonitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1
```

## 通知邏輯

- 預設（`NOTIFY_WHEN_AVAILABLE_ALWAYS=true`）：
  - 只要該地區「有任一飯店有空房」就通知。
- 若改成 `false`：
  - 啟用去重模式，只有空房結果改變才通知。

## 防 bot 注意事項

- 腳本會在每次 API 請求前套用最小間隔 + 隨機抖動。
- 多地區輪詢時也會插入延遲，避免高頻連打。
- Header 使用瀏覽器風格（User-Agent / Accept / Referer / Origin 等）。

## 注意

- 目標網站 API 若調整欄位結構，解析規則可能需微調。
- LINE Notify 官方已停止新申請，建議優先使用 Telegram。
