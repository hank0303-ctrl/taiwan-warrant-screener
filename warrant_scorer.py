"""
warrant_scorer.py — 評分引擎
計算技術指標、BS IV、槓桿、評分、風險標記
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


# ─── Black-Scholes ────────────────────────────────────────

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, r, T, sigma, option_type='call'):
    """Black-Scholes 理論價格"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if option_type == 'call' else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == 'call':
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    else:
        return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def calc_iv(market_price, S, K, T, ratio, option_type='call'):
    """
    二分法反推 IV（隱含波動率）
    market_price: 權證市價
    ratio: 行使比例
    回傳 IV (小數，例如 0.45 代表 45%)，失敗回傳 None
    """
    if T <= 0 or ratio <= 0 or S <= 0 or K <= 0:
        return None
    # 調整為 BS 等值價格
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
    """BS Delta（已含行使比例調整前）"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    d1 = (math.log(S / K) + (RF_RATE + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    if option_type == 'call':
        return round(_ncdf(d1), 4)
    else:
        return round(_ncdf(d1) - 1, 4)


def calc_leverage(delta, ratio, S, warrant_price):
    """有效槓桿 = delta × ratio × S / warrant_price"""
    if warrant_price <= 0:
        return None
    lev = abs(delta) * ratio * S / warrant_price
    return round(lev, 2)


def calc_moneyness_pct(S, K, option_type='call'):
    """
    價性百分比
    +% 代表價內，-% 代表價外
    """
    if K <= 0:
        return 0
    if option_type == 'call':
        return round((S - K) / K * 100, 2)
    else:
        return round((K - S) / K * 100, 2)


# ─── 個股強勢評分 ─────────────────────────────────────────

def score_stock(candles):
    """
    評估個股強勢程度，回傳 0-100 分
    同時回傳計算好的技術指標 dict
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

    score = 40  # base

    # 均線多頭排列
    if ma20 and price > ma20:
        score += 15
    if ma5 and ma20 and ma5 > ma20:
        score += 5

    # 漲跌幅
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

    # RSI
    if rsi is not None:
        if 55 <= rsi <= 72:
            score += 15
        elif 50 <= rsi < 55:
            score += 8
        elif rsi > 78:
            score -= 10  # 過熱
        elif rsi < 40:
            score -= 10

    # 量比
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
    }
    return score, indicators


# ─── 權證評分 ─────────────────────────────────────────────

def score_warrant(w, stock_score, stock_indicators):
    """
    計算單支權證的綜合評分（0-100）
    同時計算 IV、delta、leverage、moneyness 並填回 w
    回傳 (score, enriched_warrant_dict)
    """
    w = dict(w)

    S           = stock_indicators.get('price', 0)
    K           = w.get('strike', 0)
    T           = w.get('days_left', 0) / 365.0
    ratio       = w.get('ratio', 1.0) or 1.0
    close       = w.get('close', 0)
    option_type = w.get('type', 'call')
    hv20        = stock_indicators.get('hv20')

    # ── 計算 IV / delta / leverage / moneyness ──
    has_strike = K > 0 and S > 0 and T > 0 and close > 0
    iv        = calc_iv(close, S, K, T, ratio, option_type) if has_strike else None
    delta     = calc_delta(S, K, T, iv, option_type) if iv else None
    leverage  = calc_leverage(delta, ratio, S, close) if delta and close > 0 else None
    moneyness = calc_moneyness_pct(S, K, option_type) if K > 0 else None
    iv_hv     = round(iv / (hv20 / 100), 2) if iv and hv20 and hv20 > 0 else None

    w.update({
        'iv':         round(iv * 100, 2) if iv else None,
        'delta':      delta,
        'leverage':   leverage,
        'moneyness':  moneyness,
        'iv_hv':      iv_hv,
        'hv20':       hv20,
        'stock_price': S,
    })

    # ── 評分 ──
    dl          = w.get('days_left', 0)
    spread_pct  = w.get('spread_pct', 99)
    volume      = w.get('volume', 0)
    outp        = w.get('outstanding_pct', 100)

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

    # IV/HV 比 (20 pts)  <1.0=便宜  >1.0=貴
    if iv_hv is None:
        ivhv_score = 8   # 無資料給中性分
    elif 0.8 <= iv_hv <= 1.2:
        ivhv_score = 20  # 合理
    elif iv_hv < 0.6:
        ivhv_score = 15  # 偏便宜，好
    elif 0.6 <= iv_hv < 0.8:
        ivhv_score = 12
    elif 1.2 < iv_hv <= 1.5:
        ivhv_score = 10
    elif 1.5 < iv_hv <= 2.0:
        ivhv_score = 5
    else:
        ivhv_score = 0   # > 2.0，太貴

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
        money_score = 5  # 無履約價資料，中性分
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
    w['score'] = min(100, total)
    w['score_detail'] = {
        'days': days_score, 'spread': spread_score, 'iv_hv': ivhv_score,
        'strength': strength_score, 'leverage': lev_score, 'moneyness': money_score,
    }
    return w['score'], w


# ─── 風險標記 ─────────────────────────────────────────────

RISK_FLAGS = [
    # (key, label, color, condition_fn)
    ('near_expiry',   '近到期',   'red',    lambda w: w.get('days_left', 99) < 20),
    ('low_volume',    '低流動性', 'red',    lambda w: w.get('volume', 0) < 50),
    ('wide_spread',   '價差過大', 'red',    lambda w: w.get('spread_pct', 0) > 8),
    ('high_iv',       'IV偏貴',   'red',    lambda w: (w.get('iv_hv') or 0) > 2.0),
    ('near_delist',   '接近下市', 'red',    lambda w: w.get('outstanding_pct', 0) > 90),
    ('deep_otm',      '深度價外', 'orange', lambda w: w.get('moneyness', 0) < -20),
    ('low_leverage',  '低槓桿',   'orange', lambda w: 0 < (w.get('leverage') or 0) < 2),
    ('high_leverage', '高槓桿',   'orange', lambda w: (w.get('leverage') or 0) > 20),
    ('expiry_warn',   '天數偏短', 'yellow', lambda w: 20 <= w.get('days_left', 99) < 30),
]


def get_risk_flags(w):
    """回傳觸發的風險標記 list，每項 dict: {key, label, color}"""
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
