"""
warrant_fetcher.py — 資料抓取層 v2
資料來源：
  1. ISIN 登錄所 → 取得近期掛牌權證代號清單
  2. TWSE MIS API → 批次取得即時報價（bid/ask/量/到期日/標的代號）
  3. Fubon Neo REST → 個股歷史 OHLCV；備援 yfinance
"""

import re
import requests
import time
from datetime import date, timedelta

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, */*; q=0.01',
    'Accept-Language': 'zh-TW,zh;q=0.9',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': 'https://www.twse.com.tw/',
}

MIS_URL = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'


# ─── 工具函式 ─────────────────────────────────────────────

def safe_float(v, default=0.0):
    try:
        s = str(v).replace(',', '').strip()
        if s in ('-', '', 'N/A', 'null'):
            return default
        return float(s)
    except Exception:
        return default


def safe_int(v, default=0):
    try:
        return int(str(v).replace(',', '').strip())
    except Exception:
        return default


def last_trading_date():
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_expiry(s):
    """解析到期日，支援 20260730 / 2026/07/30 / 115/07/30 格式，回傳 date"""
    if not s:
        return None
    s = str(s).strip()
    # 8-digit: 20260730
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    # / or - separated
    s2 = s.replace('/', '-').replace('.', '-')
    parts = s2.split('-')
    if len(parts) == 3:
        try:
            y, m_, d_ = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 200:
                y += 1911
            return date(y, m_, d_)
        except Exception:
            pass
    return None


def days_left(expiry_str):
    exp = parse_expiry(expiry_str)
    if not exp:
        return 0
    return max(0, (exp - date.today()).days)


# ─── ISIN 登錄所 ─────────────────────────────────────────

def fetch_isin_warrants(min_listing_year=2024):
    """
    從 ISIN 取得近期掛牌的認購/認售權證
    CFI code 以 'RW' 開頭者即為認購/認售
    Returns: list of {code, name, type, exchange, listing_date}
    """
    result = []
    # strMode=2 上市(TWSE), strMode=4 上櫃(TPEX)
    for mode, exchange in [('2', 'tse'), ('4', 'otc')]:
        try:
            url = f'https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}'
            r = requests.get(url, headers={'User-Agent': HEADERS['User-Agent'],
                                           'Accept-Language': 'zh-TW,zh;q=0.9'},
                             timeout=20)
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL)
            count = 0
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if len(cells) < 6:
                    continue
                cfi = cells[5]
                if not cfi.startswith('RW'):
                    continue
                # 過濾：只要近 min_listing_year 年以後上市
                listing = cells[2]  # 格式 2024/01/15
                try:
                    yr = int(listing.split('/')[0])
                    if yr < min_listing_year:
                        continue
                except Exception:
                    pass
                parts = cells[0].split('　')
                code = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else ''
                wtype = 'put' if '售' in name else 'call'
                result.append({
                    'code':         code,
                    'name':         name,
                    'type':         wtype,
                    'exchange':     exchange,
                    'listing_date': listing,
                })
                count += 1
            print(f'[ISIN {exchange}] 近期掛牌: {count} 支 (CFI=RW)')
        except Exception as e:
            print(f'[ISIN {exchange}] 失敗: {e}')
    return result


# ─── TWSE MIS API ─────────────────────────────────────────

def _parse_mis_row(row):
    """
    解析 MIS 單一記錄，回傳 warrant dict
    MIS 欄位：c=代號, n=名稱, nf=完整名稱(含到期日+標的)
              rch=標的股代號, z=成交價('-'=無成交), y=昨收
              h=最高, l=最低, o=開盤, v=成交量
              a=委賣五檔, b=委買五檔(底線分隔)
    """
    code = row.get('c', '')
    if not code:
        return None

    nf = row.get('nf', '')
    # 解析到期日: nf = 'AES凱基57購02   -AES-KY   20260730美購'
    exp_match = re.search(r'(\d{8})', nf)
    expiry_str = exp_match.group(1) if exp_match else ''
    expiry_date = parse_expiry(expiry_str) if expiry_str else None

    wtype = 'put' if ('售' in nf or '售' in row.get('n', '')) else 'call'
    underlying_code = str(row.get('rch', '')).strip()

    # 盤前 z='-'，用 y (昨收) 作為參考價
    z_raw = row.get('z', '')
    y_raw = row.get('y', '')
    close      = safe_float(z_raw, 0) if z_raw not in ('-', '', 'null') else 0
    prev_close = safe_float(y_raw, 0)
    price_ref  = close if close > 0 else prev_close   # 盤前用昨收

    high   = safe_float(row.get('h'), 0) or price_ref
    low    = safe_float(row.get('l'), 0) or price_ref
    volume = safe_int(row.get('v'), 0)

    ask_str = row.get('a', '')
    bid_str = row.get('b', '')
    ask = safe_float(ask_str.split('_')[0]) if ask_str and '_' in ask_str else 0
    bid = safe_float(bid_str.split('_')[0]) if bid_str and '_' in bid_str else 0
    spread_pct = ((ask - bid) / price_ref * 100) if price_ref > 0 and ask > bid else 0

    dl = (expiry_date - date.today()).days if expiry_date else 0

    return {
        'code':            code,
        'name':            row.get('n', ''),
        'type':            wtype,
        'underlying':      underlying_code,
        'expiry':          expiry_str,
        'days_left':       max(0, dl),
        'close':           price_ref,       # 有成交用成交，否則用昨收
        'volume':          volume,
        'bid':             bid,
        'ask':             ask,
        'spread_pct':      round(spread_pct, 2),
        # v1.0 暫缺，後續可由 Fubon SDK 補充
        'strike':          0,
        'ratio':           1.0,
        'outstanding_pct': 50,
        'issuer':          '',
    }


def fetch_prices_mis(warrant_list, batch_size=100, delay=0.25):
    """
    批次查詢 TWSE MIS API 取得即時報價
    注意：MIS URL 有約 1500 字元上限，batch_size 最大 100
    warrant_list: list of {code, exchange, ...} from fetch_isin_warrants()
    Returns: dict {code: parsed warrant data}
    """
    batch_size = min(batch_size, 100)  # 強制上限 100
    result = {}
    total = len(warrant_list)
    print(f'  批次查詢 MIS，共 {total} 支（每批 {batch_size}，預估 {total//batch_size+1} 次）...')

    for i in range(0, total, batch_size):
        batch = warrant_list[i:i + batch_size]
        ex_ch = '|'.join(f'{w["exchange"]}_{w["code"]}.tw' for w in batch)
        try:
            # 直接拼接 URL（不用 params=），避免 | 被 URL-encode 為 %7C
            url = f'{MIS_URL}?ex_ch={ex_ch}&json=1&delay=0'
            r = requests.get(url, headers=HEADERS, timeout=20)
            msgs = r.json().get('msgArray', [])
            exch_map = {w['code']: w.get('exchange', 'tse') for w in batch}
            for row in msgs:
                parsed = _parse_mis_row(row)
                # 有昨收（代表曾交易）且到期日還有餘裕
                if parsed and parsed.get('close', 0) > 0 and parsed.get('days_left', 0) > 5:
                    parsed['exchange'] = exch_map.get(parsed['code'], 'tse')
                    result[parsed['code']] = parsed
        except Exception as e:
            print(f'  [MIS batch {i//batch_size+1}] 失敗: {e}')
        progress = min(i + batch_size, total)
        print(f'  進度: {progress}/{total} ...', end='\r')
        time.sleep(delay)

    print()
    print(f'  [MIS] 取得有效報價: {len(result)} 支')
    return result


# ─── 合併取得全市場權證 ───────────────────────────────────

def fetch_all_warrants(min_listing_year=2025):
    """
    整合 ISIN + MIS，回傳全市場有效掛牌權證 dict
    {code: {code, name, type, underlying, expiry, days_left,
            close, volume, bid, ask, spread_pct, exchange, ...}}
    """
    print('[1] 從 ISIN 取得近期掛牌權證清單...')
    all_isin = fetch_isin_warrants(min_listing_year)
    if not all_isin:
        print('[!] ISIN 無資料')
        return {}

    print(f'[2] 查詢 MIS 即時報價（{len(all_isin)} 支）...')
    prices = fetch_prices_mis(all_isin)

    # 補回 ISIN 的 type（名稱判斷較準確）
    isin_map = {w['code']: w for w in all_isin}
    for code, w in prices.items():
        if code in isin_map:
            w.setdefault('type', isin_map[code]['type'])

    # 過濾：需有標的、到期日還有餘裕
    valid = {
        k: v for k, v in prices.items()
        if v.get('underlying') and len(v['underlying']) >= 4 and v.get('days_left', 0) >= 10
    }
    print(f'[fetch_all_warrants] 有效權證: {len(valid)} 支')
    return valid


# ─── 個股歷史資料 ─────────────────────────────────────────

def fetch_stock_history_fubon(sdk, symbol, days=30):
    """Fubon Neo REST 歷史日 K"""
    try:
        rest  = sdk.marketdata.rest_client.stock
        end   = date.today()
        start = end - timedelta(days=days + 15)
        r = rest.historical.candles(
            symbol=symbol,
            **{'from_': start.strftime('%Y-%m-%d'), 'to': end.strftime('%Y-%m-%d')}
        )
        candles = r.get('data', r) if isinstance(r, dict) else r
        if not isinstance(candles, list):
            return None
        candles = sorted(candles, key=lambda x: str(x.get('date', '')))
        return candles[-days:] if len(candles) > days else candles
    except Exception as e:
        print(f'[Fubon hist] {symbol}: {e}')
        return None


def fetch_stock_history_yfinance(symbol, days=30):
    """yfinance 備援，台股 .TW / .TWO 後綴"""
    try:
        import yfinance as yf
        for suffix in ['.TW', '.TWO']:
            df = yf.Ticker(f'{symbol}{suffix}').history(period='3mo')
            if not df.empty:
                candles = [
                    {
                        'date':   str(idx.date()),
                        'open':   float(row.Open),
                        'high':   float(row.High),
                        'low':    float(row.Low),
                        'close':  float(row.Close),
                        'volume': int(row.Volume),
                    }
                    for idx, row in df.iterrows()
                ]
                return candles[-days:]
    except Exception as e:
        print(f'[yfinance] {symbol}: {e}')
    return None


def fetch_stock_history(sdk, symbol, days=30):
    if sdk:
        result = fetch_stock_history_fubon(sdk, symbol, days)
        if result and len(result) >= 15:
            return result
    return fetch_stock_history_yfinance(symbol, days)


def batch_fetch_stock_histories(sdk, symbols, days=30, delay=0.25):
    """批次抓取多支股票歷史，回傳 {symbol: candles list}"""
    result = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        print(f'  [{i}/{total}] 抓取 {sym}...', end='\r')
        candles = fetch_stock_history(sdk, sym, days)
        if candles and len(candles) >= 10:
            result[sym] = candles
        if sdk:
            time.sleep(delay)
    print()
    return result
