# RSS 來源探測結果

執行時間：2026-09-12 18:39（台北）

「可用」代表網址通、回傳的內容能剖析出項目。
要啟用某個來源，把 rss_sources.json 裡它的 enabled 改成 true。

## 設定檔內的來源

| 狀態 | id | 目前啟用 | 項目數 | 條件式請求 | 說明 |
|---|---|---|---|---|---|
| 可用 | cna_finance | 是 | 20 | 支援 | 最新一則距今 5.2 小時 |
| 可用 | yahoo_tw_market | 是 | 50 | 不支援 | 最新一則距今 0.1 小時 |
| 可用 | yahoo_research | 是 | 50 | 不支援 | 最新一則距今 26.8 小時 |
| 可用 | yahoo_news | 否 | 50 | 不支援 | 最新一則距今 0.4 小時 |
| 可用 | cna_tech | 否 | 20 | 支援 | 最新一則距今 2.7 小時 |
| 不可用 | cnyes_tw_stock | 否 | 0 | 不支援 | HTTPError: 404 Client Error: Not Found for url: https://news.cnyes.com/rss/cat/tw_stock |
| 不可用 | moneydj | 否 | 0 | 不支援 | HTTP 200，Content-Type text/html; charset=utf-8，剖析出 0 筆，XML 異常：<unknown>:43:2904: not well-formed (invalid token) |

「條件式請求」欄位是指來源有沒有回 ETag 或 Last-Modified。
不支援的來源每次都得整份下載，往後擴充來源數時要優先控制它的抓取頻率。

## 自動探索

以下是從各站首頁的 link 標籤撈出來的 feed 網址，未經內容驗證。
挑看起來對的貼回 rss_sources.json，再跑一次這支程式確認。

### https://news.cnyes.com/

| 標題 | 網址 |
|---|---|
| （無） | https://news.cnyes.com/rss/v1/news/category/headline |

### https://www.moneydj.com/

沒有找到任何 feed 連結。

### https://money.udn.com/money/index

沒有找到任何 feed 連結。

### https://www.ctee.com.tw/

沒有找到任何 feed 連結。

### https://www.cna.com.tw/about/rss.aspx

| 標題 | 網址 |
|---|---|
| （頁面連結） | https://www.cna.com.tw/about/rss.aspx |
| （頁面連結） | https://feeds.feedburner.com/rsscna/politics |
| （頁面連結） | https://feeds.feedburner.com/rsscna/intworld |
| （頁面連結） | https://feeds.feedburner.com/rsscna/mainland |
| （頁面連結） | https://feeds.feedburner.com/rsscna/finance |
| （頁面連結） | https://feeds.feedburner.com/rsscna/technology |
| （頁面連結） | https://feeds.feedburner.com/rsscna/lifehealth |
| （頁面連結） | https://feeds.feedburner.com/rsscna/social |
| （頁面連結） | https://feeds.feedburner.com/rsscna/local |
| （頁面連結） | https://feeds.feedburner.com/rsscna/culture |
| （頁面連結） | https://feeds.feedburner.com/rsscna/sport |
| （頁面連結） | https://feeds.feedburner.com/rsscna/stars |
