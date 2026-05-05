# 台灣股票權證輔助篩選系統

每天 08:00 自動從全市場（TWSE + TPEX）篩選適合觀察的認購/認售權證候選清單，產出 HTML 報表。

## 功能

- 動態抓取全市場 2 萬+ 支掛牌權證（ISIN 登錄所）
- 即時報價來自 TWSE MIS API（批次查詢）
- 強勢標的股篩選（MA / RSI / 量比）
- 100 分制評分系統（天數、價差、IV/HV、標的強度、槓桿、價性）
- 8 種風險警示標記（近到期、低流動性、價差過大、IV偏貴等）
- 認購 / 認售分開顯示，紅色警示另列區塊

## 使用方式

```bash
# 安裝依賴
pip install requests yfinance schedule fubon-neo

# 設定富邦帳號（複製範本後填入）
cp warrant_config.py.example warrant_config.py
# 編輯 warrant_config.py，填入 ID_NUMBER / PASSWORD / CERT_PATH / CERT_PASS

# 執行（啟動後每天 08:00 自動跑）
python3 warrant_screener.py
```

報表輸出：`warrant_report.html`（在瀏覽器開啟）

## 資料來源

| 資料 | 來源 |
|------|------|
| 權證代號清單 | ISIN 登錄所（CFI=RW） |
| 即時報價 / 標的代號 / 到期日 | TWSE MIS API |
| 個股歷史 OHLCV | Fubon Neo REST → yfinance 備援 |

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `warrant_fetcher.py` | 資料抓取層 |
| `warrant_scorer.py` | 評分引擎（Black-Scholes IV、技術指標） |
| `warrant_screener.py` | 主程式 + HTML 產生 + 排程 |
| `warrant_config.py` | **個人帳號設定（gitignored，請自行建立）** |

## 注意事項

- 本系統僅供個人觀察參考，**不自動下單**
- 履約價（strike）在 v1.0 暫缺，IV / 槓桿 / 價性顯示「—」
- v1.1 計畫：接 Fubon SDK 補齊履約價，完整計算 IV
