"""
warrant_fetcher.py — 資料抓取層 v3
資料來源：
  1. ISIN 登錄所 → 取得近期掛牌權證代號清單
  2. TWSE MIS API → 批次即時報價（bid/ask/量/到期日/標的代號）
  3. TWSE TWTB4U / TPEX → 履約價、行使比例、發行量
  4. Fubon Neo REST → 個股歷史 OHLCV；備援 yfinance
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
        if s in ('-', '--', '', 'N/A', 'null', 'nan'):
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


def recent_trading_dates(days=7):
    d = date.today()
    result = []
    for _ in range(days):
        if d.weekday() < 5:
            result.append(d)
        d -= timedelta(days=1)
    return result


def parse_expiry(s):
    """解析到期日，支援 20260730 / 2026/07/30 / 115/07/30 格式，回傳 date"""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r'^(\d{2,4})年(\d{1,2})月(\d{1,2})日$', s)
    if m:
        try:
            y, m_, d_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 200:
                y += 1911
            return date(y, m_, d_)
        except Exception:
            pass
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
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
                listing = cells[2]
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


# ─── TWSE TWTB4U（履約價 / 行使比例 / 發行量）─────────────

def fetch_twse_warrant_extra():
    """
    TWSE 取得上市權證詳細資料
    Returns: {code: {strike, ratio, outstanding_pct, issuer}}
    """
    result = {}
    for d in recent_trading_dates():
        date_str = d.strftime('%Y%m%d')
        url = f'https://www.twse.com.tw/rwd/zh/stock/warrantStock?date={date_str}&response=json'
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            j = r.json()
            if str(j.get('stat', '')).upper() not in ('OK', 'STAT_OK'):
                continue
            fields = j.get('fields', [])
            data   = j.get('data',   [])
            if not data:
                continue

            def find_col(*keywords):
                for i, f in enumerate(fields):
                    if any(k in str(f) for k in keywords):
                        return i
                return None

            idx_code   = find_col('權證代號')
            idx_strike = find_col('履約價格', '履約價')
            idx_ratio  = find_col('行使比例')
            idx_issuer = find_col('發行公司', '發行人')
            idx_expiry = find_col('履約截止日', '到期日', '最後交易日')

            if idx_code is None or idx_strike is None or idx_ratio is None:
                continue

            count = 0
            for row in data:
                if not row:
                    continue
                # 第一欄可能有空白/全型空格
                raw_code = str(row[idx_code]).replace('　', '').replace(' ', '').strip()
                if not raw_code or not raw_code[0].isdigit():
                    continue
                try:
                    strike = safe_float(row[idx_strike])
                    ratio  = safe_float(row[idx_ratio], 1.0)
                    if ratio <= 0:
                        ratio = 1.0
                    issuer = str(row[idx_issuer]).strip() if idx_issuer is not None else ''
                    expiry = parse_expiry(row[idx_expiry]) if idx_expiry is not None else None

                    result[raw_code] = {
                        'strike':          strike,
                        'ratio':           ratio,
                        'outstanding_pct': 50,
                        'issuer':          issuer,
                        'expiry':          expiry.strftime('%Y%m%d') if expiry else '',
                        'days_left':       max(0, (expiry - date.today()).days) if expiry else 0,
                    }
                    count += 1
                except Exception:
                    pass

            if count > 0:
                print(f'[TWSE extra] 上市權證詳細: {count} 支 ({date_str})')
                break
        except Exception as e:
            print(f'[TWSE extra] {date_str}: {e}')
    return result


def fetch_tpex_warrant_extra():
    """
    TPEX 取得上櫃權證：履約價、行使比例
    Returns: {code: {strike, ratio, outstanding_pct, issuer}}
    """
    result = {}
    url = 'https://www.tpex.org.tw/www/zh-tw/warrant/searchWnt'
    payload = {
        'code': 'ALL',
        'company': 'ALL',
        'bsType': 'ALL',
        'tradeType': 'ALL',
        'remainDay': 'ALL',
        'response': 'json',
    }
    try:
        r = requests.post(url, headers=HEADERS, data=payload, timeout=25)
        j = r.json()
        if str(j.get('stat', '')).lower() != 'ok':
            return result
        table = (j.get('tables') or [{}])[0]
        fields = table.get('fields', [])
        data = table.get('data', [])

        def find_col(*keywords):
            for i, f in enumerate(fields):
                if any(k in str(f) for k in keywords):
                    return i
            return None

        idx_code   = find_col('權證代號')
        idx_strike = find_col('最新履約價', '履約價')
        idx_ratio  = find_col('最新行使比例', '行使比例')
        idx_expiry = find_col('到期日', '履約截止日', '最後交易日')
        if idx_code is None or idx_strike is None or idx_ratio is None:
            return result

        for row in data:
            if not row or len(row) <= max(idx_code, idx_strike, idx_ratio):
                continue
            code = str(row[idx_code]).strip()
            if not code or not code[0].isdigit():
                continue
            strike = safe_float(row[idx_strike])
            ratio  = safe_float(row[idx_ratio], 1.0)
            if ratio <= 0:
                ratio = 1.0
            expiry = parse_expiry(row[idx_expiry]) if idx_expiry is not None and len(row) > idx_expiry else None
            result[code] = {
                'strike': strike,
                'ratio':  ratio,
                'outstanding_pct': 50,
                'issuer': '',
                'expiry': expiry.strftime('%Y%m%d') if expiry else '',
                'days_left': max(0, (expiry - date.today()).days) if expiry else 0,
            }
        if result:
            print(f'[TPEX extra] 上櫃權證詳細: {len(result)} 支')
    except Exception as e:
        print(f'[TPEX extra] {e}')
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
    exp_match = re.search(r'(\d{8})', nf)
    expiry_str = exp_match.group(1) if exp_match else ''
    expiry_date = parse_expiry(expiry_str) if expiry_str else None

    wtype = 'put' if ('售' in nf or '售' in row.get('n', '')) else 'call'
    underlying_code = str(row.get('rch', '')).strip()

    z_raw = row.get('z', '')
    y_raw = row.get('y', '')
    close      = safe_float(z_raw, 0) if z_raw not in ('-', '', 'null') else 0
    prev_close = safe_float(y_raw, 0)
    price_ref  = close if close > 0 else prev_close

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
        'close':           price_ref,
        'volume':          volume,
        'bid':             bid,
        'ask':             ask,
        'spread_pct':      round(spread_pct, 2),
        # 預設值；後續由 fetch_twse_warrant_extra / fetch_tpex_warrant_extra 覆蓋
        'strike':          0,
        'ratio':           1.0,
        'outstanding_pct': 50,
        'issuer':          '',
    }


def fetch_prices_mis(warrant_list, batch_size=100, delay=0.25):
    """
    批次查詢 TWSE MIS API 取得即時報價
    注意：MIS URL 約 1500 字元上限，batch_size 最大 100
    """
    batch_size = min(batch_size, 100)
    result = {}
    total = len(warrant_list)
    print(f'  批次查詢 MIS，共 {total} 支（每批 {batch_size}，預估 {total//batch_size+1} 次）...')

    for i in range(0, total, batch_size):
        batch = warrant_list[i:i + batch_size]
        ex_ch = '|'.join(f'{w["exchange"]}_{w["code"]}.tw' for w in batch)
        try:
            url = f'{MIS_URL}?ex_ch={ex_ch}&json=1&delay=0'
            r = requests.get(url, headers=HEADERS, timeout=20)
            msgs = r.json().get('msgArray', [])
            exch_map = {w['code']: w.get('exchange', 'tse') for w in batch}
            for row in msgs:
                parsed = _parse_mis_row(row)
                # MIS no longer consistently includes expiry in nf; fill days_left
                # later from TWSE/TPEX warrant detail sources before final filtering.
                if parsed and parsed.get('close', 0) > 0:
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

def fetch_all_warrants(min_listing_year=None):
    """
    整合 ISIN + MIS + TWTB4U/TPEX，回傳全市場有效掛牌權證 dict
    v3：補齊履約價、行使比例、發行量
    """
    if min_listing_year is None:
        min_listing_year = date.today().year   # 只查本年度掛牌，避免抓到大量已到期券
    print('[1] 從 ISIN 取得近期掛牌權證清單...')
    all_isin = fetch_isin_warrants(min_listing_year)
    if not all_isin:
        print('[!] ISIN 無資料')
        return {}

    print(f'[2] 查詢 MIS 即時報價（{len(all_isin)} 支）...')
    prices = fetch_prices_mis(all_isin)

    print('[3] 取得履約價 / 行使比例詳細資料...')
    twse_extra = fetch_twse_warrant_extra()
    tpex_extra = fetch_tpex_warrant_extra()
    extra = {**tpex_extra, **twse_extra}   # TWSE 優先

    # 補回 ISIN type + 合併 extra
    isin_map = {w['code']: w for w in all_isin}
    for code, w in prices.items():
        if code in isin_map:
            w.setdefault('type', isin_map[code]['type'])
        if code in extra:
            ex = extra[code]
            w['strike']          = ex.get('strike', 0)
            w['ratio']           = ex.get('ratio', w.get('ratio', 1.0))
            w['outstanding_pct'] = ex.get('outstanding_pct', 50)
            w['issuer']          = ex.get('issuer', '')
            if ex.get('expiry'):
                w['expiry'] = ex.get('expiry', '')
            if ex.get('days_left'):
                w['days_left'] = ex.get('days_left', 0)

    valid = {
        k: v for k, v in prices.items()
        if v.get('underlying') and len(v['underlying']) >= 4 and v.get('days_left', 0) >= 10
    }

    with_strike = sum(1 for v in valid.values() if v.get('strike', 0) > 0)
    total_v = len(valid)
    coverage = f'{with_strike/total_v*100:.0f}%' if total_v else '—'
    print(f'[fetch_all_warrants] 有效: {total_v} 支，履約價覆蓋率: {coverage} ({with_strike} 支)')
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


def fetch_stock_names(symbols=None):
    """
    從 TWSE / TPEX Open API 取得全市場股票名稱
    Returns: {code: name}
    """
    result = {}
    wanted = {str(s).strip() for s in symbols} if symbols else None

    def add_name(code, name, overwrite=False):
        code = str(code or '').strip()
        name = str(name or '').strip()
        if not code or not name:
            return
        if wanted and code not in wanted:
            return
        if overwrite or code not in result:
            result[code] = name

    try:
        r = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS, timeout=20)
        for item in r.json():
            add_name(item.get('Code'), item.get('Name'))
        print(f'[fetch_stock_names] TWSE: {len(result)} 支')
    except Exception as e:
        print(f'[fetch_stock_names] TWSE 失敗: {e}')

    try:
        r2 = requests.get(
            'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes',
            headers=HEADERS, timeout=20)
        before = len(result)
        for item in r2.json():
            add_name(item.get('SecuritiesCompanyCode'), item.get('CompanyName'))
        print(f'[fetch_stock_names] TPEX quotes: {len(result)-before} 支')
    except Exception as e:
        print(f'[fetch_stock_names] TPEX quotes 失敗: {e}')

    # 備援：若即時行情端點失效，市值列表也有上櫃代號與中文名稱。
    try:
        r3 = requests.get(
            'https://www.tpex.org.tw/openapi/v1/tpex_daily_market_value',
            headers=HEADERS, timeout=20)
        before = len(result)
        for item in r3.json():
            add_name(item.get('SecuritiesCompanyCode'), item.get('CompanyName'))
        print(f'[fetch_stock_names] TPEX market value: {len(result)-before} 支')
    except Exception as e:
        print(f'[fetch_stock_names] TPEX market value 失敗: {e}')

    print(f'[fetch_stock_names] 合計 {len(result)} 支')
    return result


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
