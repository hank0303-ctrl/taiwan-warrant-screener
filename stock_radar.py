"""
stock_radar.py — 台股個股雷達 v1

第一版原則：
- 動能與權證銜接使用現有真實資料。
- EPS / 月營收 / 事件資料採可插拔資料層；沒有資料時顯示資料不足，不用假資料。
- 金融股先排除在一般基本面排名外。
"""

import html as html_mod
import json
import os
from pathlib import Path


RADAR_LIMIT = 20


def _fmt_pct(v):
    return f'{v:+.1f}%' if isinstance(v, (int, float)) else '—'


def _fmt_num(v, digits=2):
    return f'{v:.{digits}f}' if isinstance(v, (int, float)) else '—'


def _tag(label, kind='neutral'):
    colors = {
        'good': ('#eafaf1', '#1e8449', '#abebc6'),
        'warn': ('#fef9e7', '#8a5a00', '#f9e79f'),
        'bad': ('#fdedec', '#922b21', '#f5b7b1'),
        'info': ('#eef5ff', '#1f5f99', '#bad6f7'),
        'neutral': ('#f4f4f4', '#666', '#e1e1e1'),
    }
    bg, fg, border = colors.get(kind, colors['neutral'])
    return (f'<span style="background:{bg};color:{fg};border:1px solid {border};'
            f'border-radius:4px;padding:1px 6px;font-size:11px;'
            f'margin:1px 2px 1px 0;display:inline-block">{html_mod.escape(str(label))}</span>')


def _safe(v):
    try:
        return float(v)
    except Exception:
        return None


def _latest_pair(series):
    if not isinstance(series, dict) or not series:
        return None, None, None, None
    items = sorted(series.items(), key=lambda x: str(x[0]), reverse=True)
    latest_key, latest_val = items[0]
    prev_key, prev_val = items[1] if len(items) > 1 else (None, None)
    return latest_key, _safe(latest_val), prev_key, _safe(prev_val)


def is_financial_stock(symbol):
    s = str(symbol)
    return s.startswith('28') or s in {'5880', '5876', '5871'}


def load_fundamental_store(base_dir=None):
    """
    支援兩種資料：
    1. data/stock_fundamentals.json：未來正式 API 快取格式。
    2. taiwan-stock-analysis/*_raw_data.json：現有 Goodinfo 年度資料，只取年營收與利潤率。
    """
    root = Path(base_dir or os.path.dirname(__file__))
    store = {}

    cache = root / 'data' / 'stock_fundamentals.json'
    if cache.exists():
        try:
            raw = json.loads(cache.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                store.update(raw)
        except Exception:
            pass

    raw_dir = root / 'taiwan-stock-analysis'
    if raw_dir.exists():
        for p in raw_dir.glob('*_raw_data.json'):
            try:
                raw = json.loads(p.read_text(encoding='utf-8'))
                sym = str(raw.get('stock_id') or p.name.split('_')[0])
                store.setdefault(sym, {})
                store[sym]['annual_financials'] = raw
            except Exception:
                continue
    return store


def _parse_annual_financials(fund):
    raw = fund.get('annual_financials') if isinstance(fund, dict) else None
    if not isinstance(raw, dict):
        return {}
    inc = raw.get('income_statement') or {}
    rev_y, rev, prev_y, prev_rev = _latest_pair(inc.get('營業收入'))
    gp_y, gp, _, _ = _latest_pair(inc.get('營業毛利') or inc.get('營業毛利淨額'))
    op_y, op, _, _ = _latest_pair(inc.get('營業利益'))
    net_y, net, _, _ = _latest_pair(inc.get('稅後淨利') or inc.get('合併稅後淨利'))
    prev_gp = prev_op = prev_net = None
    _, _, _, prev_gp = _latest_pair(inc.get('營業毛利') or inc.get('營業毛利淨額'))
    _, _, _, prev_op = _latest_pair(inc.get('營業利益'))
    _, _, _, prev_net = _latest_pair(inc.get('稅後淨利') or inc.get('合併稅後淨利'))

    revenue_yoy = ((rev - prev_rev) / abs(prev_rev) * 100) if rev is not None and prev_rev else None
    gross_margin = (gp / rev * 100) if gp is not None and rev else None
    op_margin = (op / rev * 100) if op is not None and rev else None
    net_margin = (net / rev * 100) if net is not None and rev else None
    prev_gross_margin = (prev_gp / prev_rev * 100) if prev_gp is not None and prev_rev else None
    prev_op_margin = (prev_op / prev_rev * 100) if prev_op is not None and prev_rev else None
    prev_net_margin = (prev_net / prev_rev * 100) if prev_net is not None and prev_rev else None

    return {
        'annual_year': rev_y,
        'annual_revenue_yoy': revenue_yoy,
        'gross_margin': gross_margin,
        'op_margin': op_margin,
        'net_margin': net_margin,
        'gross_margin_change': (gross_margin - prev_gross_margin) if gross_margin is not None and prev_gross_margin is not None else None,
        'op_margin_change': (op_margin - prev_op_margin) if op_margin is not None and prev_op_margin is not None else None,
        'net_margin_change': (net_margin - prev_net_margin) if net_margin is not None and prev_net_margin is not None else None,
    }


def _score_eps(fund):
    eps = fund.get('eps_quarters') if isinstance(fund, dict) else None
    if not isinstance(eps, list) or len(eps) < 4:
        return 0, {
            'quality': 'EPS 資料不足',
            'quarters': None,
            'yoy': None,
            'qoq': None,
            'missing': True,
        }
    vals = [_safe(x) for x in eps[-4:]]
    if any(v is None for v in vals):
        return 0, {'quality': 'EPS 資料不足', 'quarters': None, 'yoy': None, 'qoq': None, 'missing': True}
    latest = vals[-1]
    prev_q = vals[-2]
    prev_y = _safe(eps[-5]) if len(eps) >= 5 else None
    yoy = ((latest - prev_y) / abs(prev_y) * 100) if prev_y else None
    qoq = ((latest - prev_q) / abs(prev_q) * 100) if prev_q else None
    score = 0
    score += 5 if sum(vals) > 0 else 0
    score += 5 if yoy is not None and yoy > 0 else 0
    score += 5 if qoq is not None and qoq > 0 else 0
    score += 5 if vals[-1] > vals[-2] > vals[-3] else 0
    stable = all(vals[i] <= vals[i + 1] * 1.8 for i in range(3))
    score += 5 if stable and vals[-1] > vals[0] else 0
    quality = '強' if score >= 20 else '轉強' if score >= 12 else '普通'
    return score, {'quality': quality, 'quarters': vals, 'yoy': yoy, 'qoq': qoq, 'missing': False}


def _score_revenue(fund, annual):
    rev = fund.get('monthly_revenue') if isinstance(fund, dict) else None
    if not isinstance(rev, dict):
        annual_yoy = annual.get('annual_revenue_yoy')
        return 5 if annual_yoy and annual_yoy > 0 else 0, {
            'status': '營收資料不足',
            'latest_yoy': None,
            'latest_mom': None,
            'avg3_yoy': None,
            'cum_yoy': None,
            'annual_yoy': annual_yoy,
            'missing': True,
        }
    latest_yoy = _safe(rev.get('latest_yoy'))
    latest_mom = _safe(rev.get('latest_mom'))
    avg3_yoy = _safe(rev.get('avg3_yoy'))
    cum_yoy = _safe(rev.get('cum_yoy'))
    improving = bool(rev.get('improving'))
    score = 0
    score += 5 if latest_yoy is not None and latest_yoy > 0 else 0
    score += 3 if latest_mom is not None and latest_mom > 0 else 0
    score += 5 if avg3_yoy is not None and avg3_yoy > 0 else 0
    score += 5 if cum_yoy is not None and cum_yoy > 0 else 0
    score += 2 if improving else 0
    status = '轉強' if score >= 12 else '成長' if score >= 8 else '普通'
    return score, {'status': status, 'latest_yoy': latest_yoy, 'latest_mom': latest_mom, 'avg3_yoy': avg3_yoy, 'cum_yoy': cum_yoy, 'annual_yoy': None, 'missing': False}


def _score_profit(fund, annual):
    profit = fund.get('profitability') if isinstance(fund, dict) else None
    if isinstance(profit, dict):
        gross_chg = _safe(profit.get('gross_margin_yoy_change'))
        op_chg = _safe(profit.get('op_margin_yoy_change'))
        net_chg = _safe(profit.get('net_margin_change'))
        roe = _safe(profit.get('roe'))
        cashflow_ok = bool(profit.get('operating_cashflow_ok'))
        gross_margin = _safe(profit.get('gross_margin'))
        op_margin = _safe(profit.get('op_margin'))
    else:
        gross_chg = annual.get('gross_margin_change')
        op_chg = annual.get('op_margin_change')
        net_chg = annual.get('net_margin_change')
        roe = None
        cashflow_ok = False
        gross_margin = annual.get('gross_margin')
        op_margin = annual.get('op_margin')

    if gross_margin is None and op_margin is None and roe is None:
        return 0, {'status': '獲利結構資料不足', 'missing': True}
    score = 0
    score += 5 if gross_chg is not None and gross_chg > 0 else 0
    score += 5 if op_chg is not None and op_chg > 0 else 0
    score += 4 if net_chg is not None and net_chg > 0 else 0
    score += 4 if roe is not None and roe >= 12 else 0
    score += 2 if cashflow_ok else 0
    status = '改善' if score >= 10 else '普通'
    return score, {
        'status': status,
        'gross_margin': gross_margin,
        'op_margin': op_margin,
        'roe': roe,
        'gross_chg': gross_chg,
        'op_chg': op_chg,
        'missing': False,
    }


def _score_momentum(ind):
    chg = ind.get('chg_pct') or 0
    vol_ratio = ind.get('vol_ratio') or 1
    price = ind.get('price') or 0
    ma5 = ind.get('ma5')
    ma20 = ind.get('ma20')
    pct5 = ind.get('pct5')
    pct20 = ind.get('pct20')
    bias20 = ind.get('bias20')
    position = ind.get('position_label', '盤整不明')
    score = 0
    score += 5 if chg >= 3 else 4 if chg >= 2 else 2 if chg > 0 else 0
    score += 5 if vol_ratio >= 1.8 else 3 if vol_ratio >= 1.2 else 0
    score += 4 if ma5 and price > ma5 else 0
    score += 4 if ma20 and price > ma20 else 0
    score += 4 if position in ('低位轉強', '強勢續攻') and chg > 0 else 0
    score += 3 if chg >= 0 else 0
    status = '強' if score >= 18 else '轉強' if score >= 12 else '普通'
    overheat = bool(ind.get('overheat')) or (pct5 is not None and pct5 > 10) or (pct20 is not None and pct20 > 25) or (bias20 is not None and bias20 > 12)
    return score, {'status': status, 'overheat': overheat}


def _score_events(fund):
    events = fund.get('events') if isinstance(fund, dict) else None
    if not isinstance(events, dict):
        return 0, {'summary': '暫無法說資料', 'tags': [], 'missing': True}
    tags = []
    score = 0
    if events.get('investor_conference_days') is not None:
        days = int(events.get('investor_conference_days'))
        tags.append('法說前' if days >= 0 else '法說後')
        score += 4 if -3 <= days <= 7 else 1
    if events.get('earnings_recent'):
        tags.append('財報後')
        score += 2
    if events.get('monthly_revenue_recent'):
        tags.append('月營收公布')
        score += 2
    if events.get('material_news'):
        tags.append('重大訊息')
        score += 1
    if events.get('ex_dividend'):
        tags.append('除權息')
    if events.get('attention_stock'):
        tags.append('注意股')
        score -= 4
    if events.get('disposition_stock'):
        tags.append('處置股')
        score -= 6
    summary = '、'.join(tags) if tags else '暫無法說資料'
    return max(0, min(10, score)), {'summary': summary, 'tags': tags, 'missing': not bool(tags)}


def _fundamental_grade(total):
    if total >= 52:
        return 'A'
    if total >= 38:
        return 'B'
    if total >= 24:
        return 'C'
    return '資料不足'


def _build_warrant_linkage(symbol, formal_calls, formal_puts):
    calls = [w for w in formal_calls if w.get('underlying') == symbol]
    puts = [w for w in formal_puts if w.get('underlying') == symbol]
    best_call = max(calls, key=lambda w: w.get('practical_score', w.get('score', 0)), default=None)
    best_put = max(puts, key=lambda w: w.get('practical_score', w.get('score', 0)), default=None)
    best_exit = None
    best_scores = [w for w in (best_call, best_put) if w]
    if best_scores:
        best_exit = sorted(best_scores, key=lambda w: {'低': 0, '中': 1, '高': 2}.get(w.get('exit_difficulty'), 3))[0].get('exit_difficulty')
    if best_call and best_call.get('exit_difficulty') == '低' and best_call.get('practical_score', 0) >= 70:
        fit = '適合'
    elif best_scores:
        fit = '僅觀察'
    else:
        fit = '不建議'
    return {
        'has_call': bool(calls),
        'has_put': bool(puts),
        'best_call_score': best_call.get('practical_score') if best_call else None,
        'best_put_score': best_put.get('practical_score') if best_put else None,
        'best_exit': best_exit,
        'fit': fit,
    }


def build_stock_radar(strong_stocks, weak_stocks, stock_indicators, formal_calls, formal_puts, stock_names, base_dir=None):
    fundamentals = load_fundamental_store(base_dir)
    symbols = set(strong_stocks) | set(weak_stocks)
    symbols |= {w.get('underlying') for w in formal_calls + formal_puts if w.get('underlying')}
    rows = []
    excluded_financials = []
    for sym in sorted(symbols):
        if is_financial_stock(sym):
            excluded_financials.append(sym)
            continue
        ind = stock_indicators.get(sym) or {}
        if not ind:
            continue
        fund = fundamentals.get(sym, {})
        annual = _parse_annual_financials(fund)
        eps_score, eps = _score_eps(fund)
        revenue_score, revenue = _score_revenue(fund, annual)
        profit_score, profit = _score_profit(fund, annual)
        momentum_score, momentum = _score_momentum(ind)
        event_score, events = _score_events(fund)
        total = min(100, eps_score + revenue_score + profit_score + momentum_score + event_score)
        fundamental_score = eps_score + revenue_score + profit_score
        tags = []
        if fundamental_score >= 45:
            tags.append(('基本面強', 'good'))
        elif fundamental_score >= 25:
            tags.append(('基本面普通', 'neutral'))
        elif eps.get('missing') and revenue.get('missing') and profit.get('missing'):
            tags.append(('基本面資料不足', 'warn'))
        else:
            tags.append(('基本面普通', 'neutral'))
        if eps_score >= 15:
            tags.append(('EPS 轉強', 'good'))
        if revenue_score >= 10:
            tags.append(('營收成長', 'good'))
        if profit_score >= 10:
            tags.append(('獲利結構改善', 'good'))
        if momentum['status'] == '強':
            tags.append(('動能強', 'good'))
        if ind.get('position_label') in ('低位轉強', '強勢續攻', '短線過熱', '盤整不明', '弱勢破底'):
            kind = 'bad' if ind.get('position_label') == '短線過熱' else 'info' if ind.get('position_label') == '弱勢破底' else 'good'
            tags.append((ind.get('position_label'), kind))
        if momentum['overheat']:
            tags.append(('短線過熱', 'bad'))
            tags.append(('避免追高', 'warn'))
            tags.append(('等回測', 'warn'))
        for t in events['tags']:
            tags.append((t, 'warn' if t in ('注意股', '處置股') else 'info'))

        linkage = _build_warrant_linkage(sym, formal_calls, formal_puts)
        if linkage['fit'] == '適合' and momentum['status'] in ('強', '轉強'):
            tags.append(('適合認購觀察', 'good'))
        elif ind.get('position_label') == '弱勢破底' and linkage['has_put']:
            tags.append(('適合認售觀察', 'info'))
        elif momentum['status'] == '強':
            tags.append(('僅適合短打', 'warn'))

        if fundamental_score >= 45 and momentum['status'] == '強' and not momentum['overheat']:
            category = '基本面強 + 今日有動能'
            advice = '基本面強且動能延續，可優先觀察。'
        elif eps_score >= 12 and revenue_score >= 8 and not momentum['overheat']:
            category = '基本面轉強 + 剛啟動'
            tags.append(('剛啟動', 'good'))
            tags.append(('優先觀察', 'good'))
            advice = 'EPS 轉強且剛啟動，適合列入認購觀察。'
        elif fundamental_score >= 38 and momentum['overheat']:
            category = '基本面佳但短線過熱'
            advice = '基本面佳但短線過熱，避免追高，等回測再看。'
        elif momentum['status'] == '強':
            category = '有動能但基本面普通'
            tags.append(('題材短打', 'warn'))
            if fundamental_score < 25:
                tags.append(('不適合波段', 'warn'))
            advice = '有動能但基本面資料仍不足或普通，僅適合短打，不適合波段。'
        else:
            category = '觀察名單'
            advice = '資料仍需補齊，先列入觀察。'

        rows.append({
            'symbol': sym,
            'name': stock_names.get(sym, ''),
            'category': category,
            'score': total,
            'fundamental_score': fundamental_score,
            'grade': _fundamental_grade(fundamental_score),
            'eps_score': eps_score,
            'revenue_score': revenue_score,
            'profit_score': profit_score,
            'momentum_score': momentum_score,
            'event_score': event_score,
            'eps': eps,
            'revenue': revenue,
            'profit': profit,
            'events': events,
            'ind': ind,
            'tags': tags,
            'advice': advice,
            'linkage': linkage,
        })
    return {
        'items': rows,
        'financial_excluded_count': len(excluded_financials),
        'has_fundamental_store': bool(fundamentals),
    }


def _card(item):
    ind = item['ind']
    eps = item['eps']
    revenue = item['revenue']
    profit = item['profit']
    linkage = item['linkage']
    eps_quarters = 'EPS 資料不足'
    if eps.get('quarters'):
        eps_quarters = ' / '.join(_fmt_num(v, 2) for v in eps['quarters'])
    rev_latest = '營收資料不足'
    if not revenue.get('missing'):
        rev_latest = f"最新月 YoY {_fmt_pct(revenue.get('latest_yoy'))}｜MoM {_fmt_pct(revenue.get('latest_mom'))}"
    elif revenue.get('annual_yoy') is not None:
        rev_latest = f"月營收資料不足｜年度營收 YoY {_fmt_pct(revenue.get('annual_yoy'))}"
    profit_line = '獲利結構資料不足'
    if not profit.get('missing'):
        profit_line = f"毛利率 {_fmt_pct(profit.get('gross_margin'))}｜營益率 {_fmt_pct(profit.get('op_margin'))}｜ROE {_fmt_pct(profit.get('roe'))}"
    tags = ''.join(_tag(t, k) for t, k in item['tags'][:8])
    call_s = item['linkage']['best_call_score']
    put_s = item['linkage']['best_put_score']
    warrant_line = (
        f"是否有認購權證：{'是' if linkage['has_call'] else '否'}｜"
        f"是否有認售權證：{'是' if linkage['has_put'] else '否'}｜"
        f"最佳認購 {call_s if call_s is not None else '—'}｜最佳認售 {put_s if put_s is not None else '—'}｜"
        f"出場難度 {linkage['best_exit'] or '—'}｜{linkage['fit']}"
    )
    return f'''
    <div style="border-top:1px solid #f0f0f0;padding:12px 0">
      <div style="font-weight:700;color:#222">
        {html_mod.escape(item['symbol'])} {html_mod.escape(item['name'])}
        <span style="color:#1f7a5c">｜個股觀察分 {item['score']}</span>
        <span style="color:#777">｜基本面 {item['grade']}｜動能{item['momentum_score']}</span>
      </div>
      <div style="margin:5px 0">{tags}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px;font-size:12px;line-height:1.65;color:#444">
        <div>
          <b>股價</b><br>
          股價：{_fmt_num(ind.get('price'), 2)}<br>
          今日漲幅：{_fmt_pct(ind.get('chg_pct'))}<br>
          量比：{_fmt_num(ind.get('vol_ratio'), 1)} 倍
        </div>
        <div>
          <b>EPS</b><br>
          近四季 EPS：{eps_quarters}<br>
          最新季 YoY：{_fmt_pct(eps.get('yoy'))}<br>
          最新季 QoQ：{_fmt_pct(eps.get('qoq'))}
        </div>
        <div>
          <b>營收</b><br>
          {rev_latest}<br>
          近 3 月平均 YoY：{_fmt_pct(revenue.get('avg3_yoy'))}
        </div>
        <div>
          <b>獲利</b><br>
          {profit_line}
        </div>
        <div>
          <b>位階</b><br>
          5 日漲幅：{_fmt_pct(ind.get('pct5'))}｜20 日：{_fmt_pct(ind.get('pct20'))}<br>
          5 日乖離：{_fmt_pct(ind.get('bias5'))}｜20 日：{_fmt_pct(ind.get('bias20'))}<br>
          位階判斷：{html_mod.escape(ind.get('position_label', '盤整不明'))}
        </div>
        <div>
          <b>事件提醒</b><br>
          {html_mod.escape(item['events']['summary'])}<br>
          注意：法說前後波動可能放大
        </div>
      </div>
      <div style="font-size:12px;color:#555;margin-top:8px">
        <b>操作提醒：</b>{html_mod.escape(item['advice'])}<br>
        <b>權證銜接：</b>{html_mod.escape(warrant_line)}
      </div>
    </div>'''


def _event_radar_block(items):
    recent_meetings = []
    post_meetings = []
    earnings = []
    revenues = []
    for item in items:
        tags = item['events'].get('tags', [])
        if '法說前' in tags:
            recent_meetings.append(item)
        if '法說後' in tags:
            post_meetings.append(item)
        if '財報後' in tags:
            earnings.append(item)
        if '月營收公布' in tags:
            revenues.append(item)

    def mini(title, rows, empty):
        if not rows:
            body = f'<p style="color:#aaa;font-size:12px;padding:6px 0">{empty}</p>'
        else:
            body = ''.join(
                f'<div style="font-size:12px;padding:5px 0;border-top:1px solid #f5f5f5">'
                f'<b>{html_mod.escape(x["symbol"])} {html_mod.escape(x["name"])}</b>｜'
                f'{html_mod.escape(x["events"]["summary"])}｜股價反應 {_fmt_pct(x["ind"].get("chg_pct"))}｜量比 {_fmt_num(x["ind"].get("vol_ratio"),1)}x'
                f'</div>'
                for x in rows[:8]
            )
        return f'<div><div style="font-weight:700;margin:8px 0 4px">{title}</div>{body}</div>'

    return (
        '<details style="margin-top:12px"><summary style="font-size:14px;font-weight:700;color:#1f5f99">事件提醒雷達</summary>'
        '<div style="margin-top:8px">'
        + mini('近期法說會', recent_meetings, '暫無法說資料')
        + mini('法說後觀察', post_meetings, '暫無法說後資料')
        + mini('財報剛公布', earnings, '暫無財報事件資料')
        + mini('月營收剛公布', revenues, '暫無月營收事件資料')
        + '</div></details>'
    )


def render_stock_radar_html(radar):
    items = radar.get('items', [])
    priority = {
        '基本面轉強 + 剛啟動': 0,
        '基本面強 + 今日有動能': 1,
        '基本面佳但短線過熱': 2,
        '有動能但基本面普通': 3,
        '觀察名單': 4,
    }
    selected = sorted(items, key=lambda x: (priority.get(x['category'], 9), -x['score']))[:RADAR_LIMIT]
    by_cat = {}
    for item in selected:
        by_cat.setdefault(item['category'], []).append(item)

    order = ['基本面轉強 + 剛啟動', '基本面強 + 今日有動能', '基本面佳但短線過熱', '有動能但基本面普通']
    blocks = []
    for cat in order:
        cards = ''.join(_card(x) for x in by_cat.get(cat, []))
        if not cards:
            cards = '<p style="color:#aaa;font-size:12px;padding:8px 0">目前沒有符合此分類的股票，或基本面資料尚未補齊。</p>'
        blocks.append(f'<div style="margin-top:12px"><div style="font-weight:700;color:#333">{cat}</div>{cards}</div>')

    data_note = (
        f"基本面資料來源：{'已讀取本機資料快取' if radar.get('has_fundamental_store') else '尚未接 API，使用資料不足狀態'}；"
        f"金融股已排除 {radar.get('financial_excluded_count', 0)} 檔。"
    )
    return f'''
<div class="card">
  <div class="card-title">
    台股個股雷達｜基本面 × 動能 × 事件提醒
    <span class="badge" style="background:#1f5f99">今日個股觀察 Top 20</span>
  </div>
  <div style="font-size:12px;color:#777;line-height:1.6;margin-bottom:8px">
    本系統僅供觀察與研究，不構成投資建議，不提供自動下單，也不保證獲利。{html_mod.escape(data_note)}
  </div>
  {''.join(blocks)}
  {_event_radar_block(selected)}
</div>'''
