"""
warrant_scorer.py — 評分引擎 v2
計算技術指標、BS IV、槓桿、資料完整度、評分、風險標記、入選/扣分原因
"""

import math
from datetime import date


RF_RATE = 0.015  # 無風險利率 1.5%


# ─── 技術指標 ─────────────────────────────────────────────

def calc_closes(candles):
    return [float(c.get('close', 0)) for c in candles if c.get('close')]


def calc_volumes(candles):
    return [int(c.get('volume', 0)) for c in candles if c.get('volume') is not None]


def calc_ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def calc_rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def calc_hv20(closes):
    """20 日年化歷史波動率（%）"""
    if len(closes) < 21:
        return None
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - 20, len(closes))
        if closes[i - 1] > 0
    ]
    if len(log_returns) < 10:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return round(math.sqrt(variance) * math.sqrt(252) * 100, 2)


def calc_vol_ratio(volumes):
    """今日量 / 5日均量"""
    if len(volumes) < 6:
        return 1.0
    avg5 = sum(volumes[-6:-1]) / 5
    if avg5 == 0:
        return 1.0
    return round(volumes[-1] / avg5, 2)


# ─── 資料完整度 ───────────────────────────────────────────

def calc_data_completeness(w, stock_price):
    """
    計算資料完整度（0-100%）
    必要欄位：標的現價、權證現價、買價、賣價、履約價、到期日、剩餘天數、行使比例
    每欄 12.5%，共 8 欄
    """
    fields = {
        '標的現價': stock_price > 0,
        '權證現價': w.get('close', 0) > 0,
        '買價':    w.get('bid', 0) > 0,
        '賣價':    w.get('ask', 0) > 0,
        '履約價':  w.get('strike', 0) > 0,
        '到期日':  bool(w.get('expiry')),
        '剩餘天數': w.get('days_left', 0) > 0,
        '行使比例': w.get('ratio', 0) > 0,
    }
    present = sum(1 for v in fields.values() if v)
    pct = round(present / len(fields) * 100)
    missing = [k for k, v in fields.items() if not v]
    return pct, missing


# ─── Black-Scholes ────────────────────────────────────────

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, r, T, sigma, option_type='call'):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if option_type == 'call' else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == 'call':
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    else:
        return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def calc_iv(market_price, S, K, T, ratio, option_type='call'):
    """二分法反推 IV，失敗回傳 None"""
    if T <= 0 or ratio <= 0 or S <= 0 or K <= 0:
        return None
    bs_target = market_price / ratio
    intrinsic = max(0.0, (S - K) if option_type == 'call' else (K - S))
    if bs_target <= intrinsic * 1.001:
        return None

    lo, hi = 0.001, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        p = bs_price(S, K, RF_RATE, T, mid, option_type)
        if p < bs_target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.0002:
            break
    return round((lo + hi) / 2.0, 4)


def calc_delta(S, K, T, sigma, option_type='call'):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    d1 = (math.log(S / K) + (RF_RATE + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    if option_type == 'call':
        return round(_ncdf(d1), 4)
    else:
        return round(_ncdf(d1) - 1, 4)


def calc_leverage(delta, ratio, S, warrant_price):
    if warrant_price <= 0:
        return None
    lev = abs(delta) * ratio * S / warrant_price
    return round(lev, 2)


def calc_moneyness_pct(S, K, option_type='call'):
    if K <= 0:
        return None
    if option_type == 'call':
        return round((S - K) / K * 100, 2)
    else:
        return round((K - S) / K * 100, 2)


# ─── 個股強勢評分（用於認購篩選）─────────────────────────

def score_stock(candles):
    """評估個股多頭強度，0-100 分（分數高 = 適合買認購）"""
    closes  = calc_closes(candles)
    volumes = calc_volumes(candles)

    if len(closes) < 10:
        return 0, {}

    price     = closes[-1]
    prev      = closes[-2] if len(closes) >= 2 else price
    chg_pct   = (price - prev) / prev * 100 if prev else 0
    ma5       = calc_ma(closes, 5)
    ma20      = calc_ma(closes, 20)
    rsi       = calc_rsi(closes)
    vol_ratio = calc_vol_ratio(volumes)
    hv20      = calc_hv20(closes)

    score = 40  # base

    if ma20 and price > ma20:
        score += 15
    if ma5 and ma20 and ma5 > ma20:
        score += 5

    if chg_pct > 2:
        score += 15
    elif chg_pct > 1:
        score += 10
    elif chg_pct > 0:
        score += 5
    elif chg_pct < -3:
        score -= 20
    elif chg_pct < -1:
        score -= 10

    if rsi is not None:
        if 55 <= rsi <= 72:
            score += 15
        elif 50 <= rsi < 55:
            score += 8
        elif rsi > 78:
            score -= 10
        elif rsi < 40:
            score -= 10

    if vol_ratio >= 1.5:
        score += 10
    elif vol_ratio >= 1.2:
        score += 5

    score = max(0, min(100, score))

    indicators = {
        'price':     round(price, 2),
        'chg_pct':   round(chg_pct, 2),
        'ma5':       round(ma5, 2) if ma5 else None,
        'ma20':      round(ma20, 2) if ma20 else None,
        'rsi':       rsi,
        'vol_ratio': vol_ratio,
        'hv20':      hv20,
        'score':     score,
        'direction': 'bull',
    }
    return score, indicators


# ─── 個股空頭評分（用於認售篩選）─────────────────────────

def score_stock_bearish(candles):
    """
    評估個股空頭強度，0-100 分（分數高 = 適合買認售）
    標準：跌破均線、RSI 偏弱、近期下跌、放量下跌
    """
    closes  = calc_closes(candles)
    volumes = calc_volumes(candles)

    if len(closes) < 10:
        return 0, {}

    price     = closes[-1]
    prev      = closes[-2] if len(closes) >= 2 else price
    chg_pct   = (price - prev) / prev * 100 if prev else 0
    ma5       = calc_ma(closes, 5)
    ma20      = calc_ma(closes, 20)
    rsi       = calc_rsi(closes)
    vol_ratio = calc_vol_ratio(volumes)
    hv20      = calc_hv20(closes)

    score = 25  # base（空頭門檻略高，避免誤判震盪）

    # 空頭均線排列
    if ma20 and price < ma20:
        score += 20
    if ma5 and ma20 and ma5 < ma20:
        score += 15

    # 跌幅加分
    if chg_pct < -2:
        score += 20
    elif chg_pct < -1:
        score += 12
    elif chg_pct < 0:
        score += 5
    elif chg_pct > 2:
        score -= 20
    elif chg_pct > 1:
        score -= 10

    # RSI 偏弱
    if rsi is not None:
        if 30 <= rsi <= 42:
            score += 15
        elif 42 < rsi <= 50:
            score += 8
        elif rsi < 30:
            score += 5   # 過度超賣反而要小心
        elif rsi > 60:
            score -= 15
        elif rsi > 55:
            score -= 8

    # 放量下跌更強烈的空頭信號
    if vol_ratio >= 1.5 and chg_pct < -1:
        score += 10
    elif vol_ratio >= 1.5 and chg_pct > 0:
        score -= 10

    score = max(0, min(100, score))

    indicators = {
        'price':     round(price, 2),
        'chg_pct':   round(chg_pct, 2),
        'ma5':       round(ma5, 2) if ma5 else None,
        'ma20':      round(ma20, 2) if ma20 else None,
        'rsi':       rsi,
        'vol_ratio': vol_ratio,
        'hv20':      hv20,
        'score':     score,
        'direction': 'bear',
    }
    return score, indicators


# ─── 權證評分 ─────────────────────────────────────────────

def score_warrant(w, stock_score, stock_indicators):
    """
    計算單支權證綜合評分（0-100）
    同時計算 IV、delta、leverage、moneyness、資料完整度
    """
    w = dict(w)

    S           = stock_indicators.get('price', 0)
    K           = w.get('strike', 0)
    T           = w.get('days_left', 0) / 365.0
    ratio       = w.get('ratio', 1.0) or 1.0
    close       = w.get('close', 0)
    option_type = w.get('type', 'call')
    hv20        = stock_indicators.get('hv20')

    # 資料完整度
    completeness_pct, missing_fields = calc_data_completeness(w, S)
    w['completeness_pct'] = completeness_pct
    w['missing_fields']   = missing_fields

    # 計算衍生指標（需有履約價）
    has_strike = K > 0 and S > 0 and T > 0 and close > 0
    iv        = calc_iv(close, S, K, T, ratio, option_type) if has_strike else None
    delta     = calc_delta(S, K, T, iv, option_type) if iv else None
    leverage  = calc_leverage(delta, ratio, S, close) if delta and close > 0 else None
    moneyness = calc_moneyness_pct(S, K, option_type) if K > 0 else None
    iv_hv     = round(iv / (hv20 / 100), 2) if iv and hv20 and hv20 > 0 else None

    w.update({
        'iv':          round(iv * 100, 2) if iv else None,
        'delta':       delta,
        'leverage':    leverage,
        'moneyness':   moneyness,
        'iv_hv':       iv_hv,
        'hv20':        hv20,
        'stock_price': S,
    })

    # ── 子項評分 ──
    dl         = w.get('days_left', 0)
    spread_pct = w.get('spread_pct', 99)
    volume     = w.get('volume', 0)

    # 剩餘天數 (20 pts)
    if 60 <= dl <= 90:
        days_score = 20
    elif 30 <= dl < 60 or 91 <= dl <= 150:
        days_score = 14
    elif 20 <= dl < 30 or 150 < dl <= 210:
        days_score = 8
    else:
        days_score = 2

    # 買賣價差 (20 pts)
    if spread_pct <= 2:
        spread_score = 20
    elif spread_pct <= 4:
        spread_score = 14
    elif spread_pct <= 6:
        spread_score = 8
    elif spread_pct <= 9:
        spread_score = 4
    else:
        spread_score = 0

    # IV/HV 比 (20 pts)
    if iv_hv is None:
        ivhv_score = 8
    elif 0.8 <= iv_hv <= 1.2:
        ivhv_score = 20
    elif iv_hv < 0.6:
        ivhv_score = 15
    elif 0.6 <= iv_hv < 0.8:
        ivhv_score = 12
    elif 1.2 < iv_hv <= 1.5:
        ivhv_score = 10
    elif 1.5 < iv_hv <= 2.0:
        ivhv_score = 5
    else:
        ivhv_score = 0

    # 標的強度 (20 pts)
    strength_score = int(stock_score / 100 * 20)

    # 有效槓桿 (10 pts)
    if leverage is None:
        lev_score = 4
    elif 4 <= leverage <= 8:
        lev_score = 10
    elif 3 <= leverage < 4 or 8 < leverage <= 12:
        lev_score = 7
    elif 2 <= leverage < 3:
        lev_score = 4
    else:
        lev_score = 2

    # 價性 (10 pts)
    if moneyness is None:
        money_score = 5
    elif -10 <= moneyness <= 0:
        money_score = 10
    elif 0 < moneyness <= 10:
        money_score = 8
    elif -20 <= moneyness < -10:
        money_score = 5
    elif 10 < moneyness <= 20:
        money_score = 5
    else:
        money_score = 2

    total = days_score + spread_score + ivhv_score + strength_score + lev_score + money_score

    # 上限規則
    if completeness_pct < 80:
        total = min(total, 60)
    if K <= 0:                      # 缺履約價
        total = min(total, 50)

    w['score'] = min(100, total)
    w['score_detail'] = {
        'days':      days_score,
        'spread':    spread_score,
        'iv_hv':     ivhv_score,
        'strength':  strength_score,
        'leverage':  lev_score,
        'moneyness': money_score,
    }
    return w['score'], w


# ─── 風險標記 ─────────────────────────────────────────────

RISK_FLAGS = [
    ('near_expiry',   '近到期',    'red',    lambda w: w.get('days_left', 99) < 20),
    ('low_volume',    '流動性低',  'red',    lambda w: 10 <= w.get('volume', 0) < 50),
    ('med_volume',    '量偏少',    'yellow', lambda w: 50 <= w.get('volume', 0) < 100),
    ('wide_spread',   '價差過大',  'red',    lambda w: w.get('spread_pct', 0) > 8),
    ('high_iv',       'IV偏貴',    'red',    lambda w: (w.get('iv_hv') or 0) > 2.0),
    ('near_delist',   '接近下市',  'red',    lambda w: w.get('outstanding_pct', 0) > 90),
    ('deep_otm',      '深度價外',  'orange', lambda w: w.get('moneyness', 0) is not None and (w.get('moneyness') or 0) < -20),
    ('low_leverage',  '低槓桿',    'orange', lambda w: 0 < (w.get('leverage') or 0) < 2),
    ('high_leverage', '高槓桿',    'orange', lambda w: (w.get('leverage') or 0) > 20),
    ('expiry_warn',   '天數偏短',  'yellow', lambda w: 20 <= w.get('days_left', 99) < 60),
    ('no_strike',     '缺履約價',  'orange', lambda w: w.get('strike', 0) == 0),
]


def get_risk_flags(w):
    flags = []
    for key, label, color, cond in RISK_FLAGS:
        try:
            if cond(w):
                flags.append({'key': key, 'label': label, 'color': color})
        except Exception:
            pass
    return flags


def has_red_flag(flags):
    return any(f['color'] == 'red' for f in flags)


# ─── 入選 / 扣分原因 ─────────────────────────────────────

def get_selection_reasons(w):
    """回傳入選理由字串 list（正向）"""
    reasons = []
    stock_score = w.get('stock_score', 0)
    direction   = w.get('direction', 'bull')

    if direction == 'bull':
        if stock_score >= 80:
            reasons.append('標的極強勢')
        elif stock_score >= 65:
            reasons.append('標的強勢')
        elif stock_score >= 55:
            reasons.append('標的偏強')
    else:
        if stock_score >= 80:
            reasons.append('標的極弱勢')
        elif stock_score >= 65:
            reasons.append('標的弱勢')
        elif stock_score >= 55:
            reasons.append('標的偏弱')

    spd = w.get('spread_pct', 99)
    if spd <= 2:
        reasons.append('低買賣價差')
    elif spd <= 4:
        reasons.append('價差合理')

    iv_hv = w.get('iv_hv')
    if iv_hv:
        if iv_hv <= 0.8:
            reasons.append('IV便宜')
        elif iv_hv <= 1.2:
            reasons.append('IV合理')

    dl = w.get('days_left', 0)
    if 60 <= dl <= 150:
        reasons.append(f'天數適中({dl}天)')

    lev = w.get('leverage')
    if lev and 4 <= lev <= 10:
        reasons.append(f'槓桿適中({lev:.1f}x)')

    vol = w.get('volume', 0)
    if vol > 200:
        reasons.append('成交活絡')
    elif vol > 100:
        reasons.append('成交尚可')

    m = w.get('moneyness')
    if m is not None and -15 <= m <= 5:
        reasons.append('價性良好')

    if w.get('completeness_pct', 0) >= 100:
        reasons.append('資料完整')

    return reasons


def get_deduction_reasons(w):
    """回傳扣分原因字串 list（負向）"""
    reasons = []

    spd = w.get('spread_pct', 0)
    if spd > 6:
        reasons.append(f'價差過大({spd:.1f}%)')
    elif spd > 4:
        reasons.append(f'價差偏大({spd:.1f}%)')

    dl = w.get('days_left', 0)
    if dl < 30:
        reasons.append(f'剩餘天數極短({dl}天)')
    elif dl < 60:
        reasons.append(f'天數偏短({dl}天)')

    iv_hv = w.get('iv_hv')
    if iv_hv and iv_hv > 2.0:
        reasons.append(f'IV嚴重偏貴({iv_hv:.1f}x)')
    elif iv_hv and iv_hv > 1.5:
        reasons.append(f'IV偏貴({iv_hv:.1f}x)')

    lev = w.get('leverage')
    if lev and lev > 15:
        reasons.append(f'槓桿過高({lev:.1f}x)')
    elif lev and lev < 2:
        reasons.append(f'槓桿過低({lev:.1f}x)')

    vol = w.get('volume', 0)
    if vol < 50:
        reasons.append(f'成交稀少({vol}張)')
    elif vol < 100:
        reasons.append(f'成交偏少({vol}張)')

    cp = w.get('completeness_pct', 100)
    if cp < 80:
        missing = '、'.join(w.get('missing_fields', [])[:3])
        reasons.append(f'資料不足({missing})')
    elif cp < 100:
        reasons.append(f'部分資料缺失')

    m = w.get('moneyness')
    if m is not None and m < -20:
        reasons.append(f'深度價外({m:.1f}%)')

    if w.get('strike', 0) == 0:
        reasons.append('無履約價(分數上限50)')

    return reasons
