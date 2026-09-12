# RSS 來源探測結果

執行時間：2026-09-12 20:49（台北）

「可用」代表網址通、回傳的內容能剖析出項目。
要啟用某個來源，把 rss_sources.json 裡它的 enabled 改成 true。

## 設定檔內的來源

| 狀態 | id | 目前啟用 | 項目數 | 條件式請求 | 說明 |
|---|---|---|---|---|---|
| 可用 | cna_finance | 是 | 20 | 支援 | 最新一則距今 7.4 小時 |
| 可用 | yahoo_tw_market | 是 | 50 | 不支援 | 最新一則距今 0.3 小時 |
| 可用 | yahoo_research | 是 | 50 | 不支援 | 最新一則距今 28.9 小時 |
| 可用 | yahoo_news | 否 | 50 | 不支援 | 最新一則距今 0.4 小時 |
| 可用 | cna_tech | 否 | 20 | 支援 | 最新一則距今 4.8 小時 |
| 不可用 | cnyes_tw_stock | 否 | 0 | 不支援 | HTTPError: 404 Client Error: Not Found for url: https://news.cnyes.com/rss/cat/tw_stock |
| 不可用 | moneydj | 否 | 0 | 不支援 | HTTP 200，Content-Type text/html; charset=utf-8，剖析出 0 筆，XML 異常：<unknown>:43:2904: not well-formed (invalid token) |

「條件式請求」欄位是指來源有沒有回 ETag 或 Last-Modified。
不支援的來源每次都得整份下載，往後擴充來源數時要優先控制它的抓取頻率。
