"""
warrant_screener.py — 主程式 v2
每天 08:00 執行，產出 index.html 並 push GitHub Pages
執行方式：python3 warrant_screener.py
"""

import html as html_mod
import json
import os
import time
from collections import Counter
from datetime import datetime, date

import schedule

from warrant_fetcher import (
    fetch_all_warrants,
    fetch_stock_names,
    batch_fetch_stock_histories,
)
from warrant_scorer import (
    score_stock,
    score_stock_bearish,
    score_warrant,
    get_risk_flags,
    has_red_flag,
    get_selection_reasons,
    get_deduction_reasons,
)
from stock_radar import build_stock_radar, render_stock_radar_html

try:
    from warrant_config import ID_NUMBER, PASSWORD, CERT_PATH, CERT_PASS
except ImportError:
    ID_NUMBER = PASSWORD = CERT_PATH = CERT_PASS = ""
    print("[!] 找不到 warrant_config.py，Fubon SDK 將無法登入，改用 yfinance")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'index.html')

STRONG_STOCK_MIN_SCORE = 58   # 認購標的門檻
WEAK_STOCK_MIN_SCORE   = 55   # 認售標的門檻


def _fmt_pct(v):
    return f'{v:+.1f}%' if v is not None else '—'


# ─── Fubon SDK 初始化 ─────────────────────────────────────

def init_fubon():
    try:
        from fubon_neo.sdk import FubonSDK
        sdk = FubonSDK()
        accounts = sdk.login(ID_NUMBER, PASSWORD, CERT_PATH, CERT_PASS)
        print(f'[Fubon] 登入成功，帳號 {len(accounts.data)} 個（僅用 REST API）')
        return sdk
    except Exception as e:
        print(f'[Fubon] 初始化失敗: {e}，改用 yfinance 備援')
        return None


# ─── 主篩選流程 ───────────────────────────────────────────

def run_screening():
    start_time = datetime.now()
    print(f'\n{"="*55}')
    print(f'  權證篩選器啟動  {start_time.strftime("%Y/%m/%d %H:%M")}')
    print(f'{"="*55}')

    sdk = init_fubon()

    print('\n[1/5] 抓取全市場權證清單...')
    all_warrants = fetch_all_warrants()
    if not all_warrants:
        print('[!] 無法取得權證資料，結束')
        return

    print('\n[2/5] 分析標的股清單...')
    underlying_set = {
        w['underlying'] for w in all_warrants.values()
        if w.get('underlying') and w.get('days_left', 0) >= 15
    }
    print(f'    共 {len(underlying_set)} 支不重複標的股')

    print('\n[3/5] 抓取個股名稱 + 歷史行情...')
    stock_names = fetch_stock_names()
    histories = batch_fetch_stock_histories(sdk, sorted(underlying_set), days=28)
    print(f'    成功取得 {len(histories)} 支股票歷史資料')

    print('\n[4/5] 計算技術指標（多頭 + 空頭）...')
    stock_bull_scores  = {}
    stock_bear_scores  = {}
    stock_indicators   = {}

    for sym, candles in histories.items():
        bull_s, bull_ind = score_stock(candles)
        bear_s, bear_ind = score_stock_bearish(candles)
        stock_bull_scores[sym] = bull_s
        stock_bear_scores[sym] = bear_s
        # 合用同一份 indicators（price/hv20 共用），附加 bear_score
        stock_indicators[sym]  = bull_ind
        stock_indicators[sym]['bear_score'] = bear_s

    strong_stocks = {
        sym: stock_bull_scores[sym]
        for sym in stock_bull_scores
        if stock_bull_scores[sym] >= STRONG_STOCK_MIN_SCORE
    }
    weak_stocks = {
        sym: stock_bear_scores[sym]
        for sym in stock_bear_scores
        if stock_bear_scores[sym] >= WEAK_STOCK_MIN_SCORE
    }
    strong_stocks = dict(sorted(strong_stocks.items(), key=lambda x: -x[1]))
    weak_stocks   = dict(sorted(weak_stocks.items(),   key=lambda x: -x[1]))
    print(f'    強勢股: {len(strong_stocks)} 支（認購用）'
          f'  弱勢股: {len(weak_stocks)} 支（認售用）')

    print('\n[5/5] 計算權證評分 + 分類...')
    formal_calls  = []   # 正式認購候選
    formal_puts   = []   # 正式認售候選
    insufficient  = []   # 資料不足
    high_risk     = []   # 高風險排除

    for code, w in all_warrants.items():
        udly   = w.get('underlying', '')
        wtype  = w.get('type', 'call')

        # 按認購/認售分別過濾對應的強/弱勢股
        if wtype == 'call':
            if udly not in strong_stocks:
                continue
            s_score = strong_stocks[udly]
        else:
            if udly not in weak_stocks:
                continue
            s_score = weak_stocks[udly]

        s_ind = stock_indicators.get(udly, {})
        score, enriched = score_warrant(w, s_score, s_ind)

        flags = get_risk_flags(enriched)
        enriched['risk_flags']   = flags
        enriched['has_red_flag'] = has_red_flag(flags)
        enriched['stock_score']  = s_score
        enriched['direction']    = 'bull' if wtype == 'call' else 'bear'
        enriched['stock_position'] = s_ind.get('position_label', '盤整不明')
        enriched['stock_pct5'] = s_ind.get('pct5')
        enriched['stock_pct20'] = s_ind.get('pct20')
        enriched['stock_bias5'] = s_ind.get('bias5')
        enriched['stock_bias20'] = s_ind.get('bias20')
        enriched['chg_pct'] = s_ind.get('chg_pct', 0)
        enriched['stock_consecutive_long_red'] = s_ind.get('consecutive_long_red', 0)
        enriched['stock_near_limit_up'] = s_ind.get('near_limit_up', False)
        enriched['stock_overheat'] = s_ind.get('overheat', False)
        enriched['underlying_name'] = stock_names.get(udly, '')
        add_practical_fields(enriched)
        enriched['selection_reasons'] = get_selection_reasons(enriched)
        enriched['deduction_reasons'] = get_deduction_reasons(enriched)

        if not enriched.get('qualification_passed', False):
            if enriched.get('qualification_bucket') == 'high_risk':
                high_risk.append(enriched)
            else:
                insufficient.append(enriched)
        elif not enriched.get('formal_data_ready', False):
            insufficient.append(enriched)
        else:
            if wtype == 'call':
                formal_calls.append(enriched)
            else:
                formal_puts.append(enriched)

    formal_calls.sort(key=lambda x: -x.get('practical_score', x.get('score', 0)))
    formal_puts.sort(key=lambda x:  -x.get('practical_score', x.get('score', 0)))
    insufficient.sort(key=lambda x: -x.get('completeness_pct', 0))
    high_risk.sort(key=lambda x:    -x['score'])

    print(f'    正式認購: {len(formal_calls)}  正式認售: {len(formal_puts)}'
          f'  資料不足: {len(insufficient)}  高風險排除: {len(high_risk)}')

    elapsed = (datetime.now() - start_time).seconds
    print(f'\n[完成] 耗時 {elapsed}s，寫出報表...')
    html = generate_html(
        strong_stocks, weak_stocks, stock_indicators,
        formal_calls, formal_puts, insufficient, high_risk,
        start_time, stock_names
    )
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[報表] {OUTPUT_PATH}')
    print('='*55)

    auto_push_report(start_time)
    return formal_calls, formal_puts


def auto_push_report(run_time):
    import subprocess
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    date_str = run_time.strftime('%Y/%m/%d %H:%M')
    try:
        subprocess.run(['git', 'add', 'index.html'], cwd=repo_dir, check=True, capture_output=True)
        result = subprocess.run(
            ['git', 'commit', '-m', f'報表自動更新 {date_str}'],
            cwd=repo_dir, capture_output=True, text=True
        )
        if 'nothing to commit' in result.stdout:
            print('[push] 報表無變更，略過 push')
            return
        subprocess.run(['git', 'push', 'origin', 'main'], cwd=repo_dir, check=True, capture_output=True)
        print(f'[push] ✅ 已推上 GitHub Pages ({date_str})')
    except subprocess.CalledProcessError as e:
        print(f'[push] ⚠️ push 失敗: {e}')


# ─── HTML 輔助 ────────────────────────────────────────────

def _score_color(score):
    if score >= 75: return '#27ae60'
    if score >= 55: return '#e67e22'
    return '#e74c3c'

def _compl_color(pct):
    if pct >= 100: return '#27ae60'
    if pct >= 80:  return '#e67e22'
    return '#e74c3c'

def _flag_html(flags):
    if not flags:
        return ''
    colors = {'red': '#e74c3c', 'orange': '#e67e22', 'yellow': '#d4ac0d'}
    parts = []
    for f in flags:
        c = colors.get(f['color'], '#999')
        parts.append(
            f'<span style="background:{c};color:#fff;border-radius:4px;'
            f'padding:1px 6px;font-size:11px;margin:1px 2px 1px 0;display:inline-block">'
            f'{f["label"]}</span>'
        )
    return ''.join(parts)


def _tag(label, kind='neutral'):
    styles = {
        'good':    ('#eafaf1', '#1e8449', '#abebc6'),
        'warn':    ('#fef9e7', '#8a5a00', '#f9e79f'),
        'bad':     ('#fdedec', '#922b21', '#f5b7b1'),
        'info':    ('#eef5ff', '#1f5f99', '#bad6f7'),
        'neutral': ('#f4f4f4', '#666', '#e1e1e1'),
    }
    bg, color, border = styles.get(kind, styles['neutral'])
    return (f'<span style="background:{bg};color:{color};border:1px solid {border};'
            f'border-radius:4px;padding:1px 6px;font-size:11px;'
            f'margin:1px 2px 1px 0;display:inline-block">{html_mod.escape(str(label))}</span>')


def calc_exit_difficulty(w):
    vol = w.get('volume', 0)
    spd = w.get('spread_pct', 99)
    price = w.get('close', 0)
    cp = w.get('completeness_pct', 0)
    points = 0
    if vol >= 500:
        points += 3
    elif vol >= 200:
        points += 2
    elif vol >= 50:
        points += 1
    if spd <= 2:
        points += 3
    elif spd <= 3.5:
        points += 2
    elif spd <= 5:
        points += 1
    if price >= 0.5:
        points += 2
    elif price >= 0.3:
        points += 1
    if cp >= 100:
        points += 1

    if points >= 7:
        return '低', '容易進出', 0
    if points >= 4:
        return '中', '可觀察', 8
    return '高', '不易出場', 18


RISK_TAG_LABELS = {
    'IV 偏貴',
    '深度價外',
    '量能不足',
    '價差過大',
    '低槓桿',
    '剩餘天數過短',
    '追高風險',
    '短線過熱',
}


def _tag_labels(w):
    return {label for label, _kind in w.get('practical_tags', [])}


def has_scalp_or_overheat_signal(w):
    labels = _tag_labels(w)
    return (
        bool(labels & {'僅適合短打', '短線過熱', '避免追高', '追高風險'})
        or (w.get('chg_pct') or 0) >= 7
        or (w.get('stock_pct5') is not None and w.get('stock_pct5') > 10)
        or (w.get('stock_pct20') is not None and w.get('stock_pct20') > 25)
        or w.get('stock_consecutive_long_red', 0) >= 2
    )


def has_steady_exclusion_signal(w):
    labels = _tag_labels(w)
    return (
        bool(labels & {'僅適合短打', '短線過熱', '避免追高', '追高風險'})
        or (w.get('chg_pct') or 0) >= 7
        or (w.get('stock_pct5') is not None and w.get('stock_pct5') > 10)
        or (w.get('stock_pct20') is not None and w.get('stock_pct20') > 25)
        or (w.get('stock_bias20') is not None and w.get('stock_bias20') > 12)
        or w.get('stock_consecutive_long_red', 0) >= 2
    )


def calc_risk_level(w):
    exit_level = w.get('exit_difficulty', '中')
    risk_points = 0
    if exit_level == '高':
        risk_points += 3
    elif exit_level == '中':
        risk_points += 1
    if w.get('stock_overheat'):
        risk_points += 2
    if (w.get('chg_pct') or 0) >= 7:
        risk_points += 2
    if w.get('spread_pct', 0) > 3.5:
        risk_points += 1
    if w.get('volume', 0) < 100:
        risk_points += 1
    if (w.get('moneyness') is not None and w.get('moneyness', 0) < -20):
        risk_points += 1
    if (w.get('iv_hv') or 0) > 1.5:
        risk_points += 1
    if w.get('days_left', 0) < 60:
        risk_points += 1
    labels = _tag_labels(w)
    tag_risks = labels & RISK_TAG_LABELS
    if tag_risks:
        risk_points = max(risk_points, 2)
    if len(tag_risks) >= 2:
        risk_points = max(risk_points, 4)

    if risk_points >= 4:
        return '高'
    if risk_points >= 2:
        return '中'
    return '低'


def add_practical_fields(w):
    exit_level, exit_label, exit_penalty = calc_exit_difficulty(w)
    w['exit_difficulty'] = exit_level
    w['exit_label'] = exit_label

    score = w.get('score', 0)
    stock_score = w.get('stock_score', 0)
    vol = w.get('volume', 0)
    spd = w.get('spread_pct', 99)
    dl = w.get('days_left', 0)
    m = w.get('moneyness')
    lev = w.get('leverage') or 0
    wtype = w.get('type', 'call')
    position = w.get('stock_position', '盤整不明')
    overheat = w.get('stock_overheat', False)

    practical_raw = score
    practical_raw += int(stock_score * 0.25)
    practical_raw += 10 if vol >= 500 else 7 if vol >= 200 else 3 if vol >= 50 else 0
    practical_raw += 8 if spd <= 2 else 5 if spd <= 3.5 else 2 if spd <= 5 else -10
    practical_raw += 8 if 60 <= dl <= 120 else 4 if 45 <= dl <= 180 else -10
    if wtype == 'call' and position == '低位轉強':
        practical_raw += 8
    elif wtype == 'call' and position == '強勢續攻':
        practical_raw += 4
    elif wtype == 'put' and position == '弱勢破底':
        practical_raw += 10
    elif position == '盤整不明':
        practical_raw -= 6
    if overheat:
        practical_raw -= 18
    if exit_level == '高':
        practical_raw -= exit_penalty
    elif exit_level == '中':
        practical_raw -= exit_penalty
    if vol < 100:
        practical_raw -= 8
    if m is not None and m < -20:
        practical_raw -= 12
    if dl < 60:
        practical_raw -= 8
    if wtype == 'call' and not (3 <= lev <= 10) and lev:
        practical_raw -= 4

    # 正規化成 0-100，保留原始分數只供內部除錯。
    w['practical_raw_score'] = int(round(practical_raw))
    w['practical_score'] = max(0, min(100, int(round(practical_raw / 150 * 100))))

    tags = []
    if overheat:
        tags.append(('短線過熱', 'bad'))
        tags.append(('避免追高', 'warn'))
    if (w.get('chg_pct') or 0) >= 7:
        tags.append(('追高風險', 'bad'))
    pct5 = w.get('stock_pct5')
    pct20 = w.get('stock_pct20')
    if wtype == 'call' and ((pct5 is not None and pct5 >= 10) or (pct20 is not None and pct20 >= 18)):
        tags.append(('僅適合短打', 'warn'))
    if position in ('低位轉強', '強勢續攻', '弱勢破底'):
        tags.append((position, 'good' if position != '弱勢破底' else 'info'))
    if exit_level == '低':
        tags.append(('出場容易', 'good'))
    elif exit_level == '高':
        tags.append(('出場困難', 'bad'))
    if spd > 3.5:
        tags.append(('價差過大', 'bad' if spd > 5 else 'warn'))
    if dl < 60:
        tags.append(('時間價值風險', 'warn'))
        tags.append(('剩餘天數過短', 'warn'))
    if (w.get('iv_hv') or 0) > 1.5:
        tags.append(('IV 偏貴', 'warn'))
    if m is not None and m < -20:
        tags.append(('深度價外', 'warn'))
    if lev and lev < 2:
        tags.append(('低槓桿', 'warn'))
    if vol < 100:
        tags.append(('量能不足', 'warn'))
    w['practical_tags'] = tags
    w['risk_level'] = calc_risk_level(w)
    if w['practical_score'] >= 75 and exit_level != '高' and not overheat and w['risk_level'] == '低':
        w['practical_tags'].insert(0, ('推薦觀察', 'good'))

    if w['risk_level'] == '高' or exit_level == '高':
        advice_type = '高風險觀察'
    elif overheat or '短線過熱' in _tag_labels(w) or '避免追高' in _tag_labels(w):
        advice_type = '不建議追價'
    elif wtype == 'call' and has_scalp_or_overheat_signal(w):
        advice_type = '短打觀察'
    elif wtype == 'call' and 60 <= dl <= 120 and exit_level == '低':
        advice_type = '穩健觀察'
    elif wtype == 'call':
        advice_type = '短打觀察'
    else:
        advice_type = '認售觀察'

    reminders = [f'建議類型：{advice_type}']
    if overheat or w.get('stock_near_limit_up'):
        reminders.append('追高提醒：標的今日或短線漲幅過大，建議等回測再看')
    if exit_level == '高':
        reminders.append('出場提醒：成交量或價差不佳，不宜重押')
    if wtype == 'put':
        reminders.append('停損提醒：若標的重新站回 5 日線，或認售權證跌破買進價 15%–20%，應考慮停損。')
        reminders.append('停利提醒：若標的續跌、認售權證短線上漲 20%–30%，可考慮分批停利。')
    else:
        reminders.append('停損提醒：若標的跌破 5 日線，或權證跌破買進價 15%–20%，應考慮停損。')
        reminders.append('停利提醒：若權證短線上漲 20%–30%，可考慮分批停利。')
    reminders.append('時間價值提醒：若標的 2–3 天沒有續強，權證可能被時間價值侵蝕')
    w['operation_type'] = advice_type
    w['operation_reminders'] = reminders
    return w


def _build_copy_text(w):
    direction = '認購' if w.get('type') == 'call' else '認售'
    iv_s   = f"{w['iv']:.1f}%" if w.get('iv') else '—'
    lev_s  = f"{w['leverage']:.1f}x" if w.get('leverage') else '—'
    m      = w.get('moneyness')
    m_s    = f"{m:+.1f}%" if m is not None else '—'
    sel    = '、'.join(w.get('selection_reasons', []))
    ded    = '、'.join(w.get('deduction_reasons', []))
    excl   = '、'.join(w.get('exclusion_reasons', []))
    flags  = '、'.join(f['label'] for f in w.get('risk_flags', []))
    strike_s = f"{w.get('strike',0):.2f}" if w.get('strike', 0) > 0 else '未知'
    lines = [
        f"【{direction}】{w['code']} {w.get('name','')}",
        f"標的: {w.get('underlying','')} @ {w.get('stock_price',0):.2f}",
        f"現價: {w.get('close',0):.2f}  履約價: {strike_s}  剩餘: {w.get('days_left',0)}天",
        f"總分: {w.get('score',0)}  實戰: {w.get('practical_score',0)}  風險: {w.get('risk_level','中')}  完整度: {w.get('completeness_pct',0)}%",
        f"IV: {iv_s}  槓桿: {lev_s}  價性: {m_s}",
        f"行使比例: {w.get('ratio',1):.2f}  量: {w.get('volume',0)}張  價差: {w.get('spread_pct',0):.1f}%",
        f"標的位置: {w.get('stock_position','—')}  5日: {_fmt_pct(w.get('stock_pct5'))}  20日: {_fmt_pct(w.get('stock_pct20'))}",
        f"出場難度: {w.get('exit_difficulty','—')}｜{w.get('exit_label','—')}",
    ]
    if sel:   lines.append(f"入選: {sel}")
    if ded:   lines.append(f"注意: {ded}")
    if excl:  lines.append(f"排除: {excl}")
    if flags: lines.append(f"風險: {flags}")
    if w.get('operation_reminders'):
        lines.append('提醒: ' + '；'.join(w.get('operation_reminders', [])[:3]))
    return '\n'.join(lines)


def _warrant_card(w, show_reasons=True):
    score      = w.get('score', 0)
    sc_c       = _score_color(score)
    flags_html = _flag_html(w.get('risk_flags', []))
    dl         = w.get('days_left', 0)
    iv_s       = f"{w['iv']:.1f}%" if w.get('iv') else '—'
    iv_hv_s    = f"{w['iv_hv']:.2f}" if w.get('iv_hv') else '—'
    lev_s      = f"{w['leverage']:.1f}x" if w.get('leverage') else '—'
    m          = w.get('moneyness')
    if m is None:
        money_s, money_c = '—', '#aaa'
    else:
        money_s = f"+{m:.1f}%" if m >= 0 else f"{m:.1f}%"
        money_c = '#27ae60' if m >= 0 else '#e74c3c'
    spd        = w.get('spread_pct', 0)
    stk_chg    = w.get('chg_pct') or _stock_ind_cache.get(w.get('underlying', ''), {}).get('chg_pct', 0) or 0
    stk_c      = '#27ae60' if stk_chg >= 0 else '#e74c3c'
    strike_s   = f"{w.get('strike',0):.2f}" if w.get('strike', 0) > 0 else '未知'
    exch       = w.get('exchange', 'tse')
    exch_badge = ('<span style="background:#3498db;color:#fff;border-radius:3px;padding:0 4px;font-size:10px">上市</span>'
                  if exch in ('tse', 'TWSE') else
                  '<span style="background:#9b59b6;color:#fff;border-radius:3px;padding:0 4px;font-size:10px">上櫃</span>')
    cp         = w.get('completeness_pct', 0)
    cp_c       = _compl_color(cp)
    practical  = w.get('practical_score', score)
    pos        = w.get('stock_position', '盤整不明')
    pos_kind   = 'bad' if pos == '短線過熱' else 'info' if pos == '弱勢破底' else 'good' if pos in ('低位轉強', '強勢續攻') else 'neutral'
    exit_level = w.get('exit_difficulty', '—')
    exit_kind  = 'good' if exit_level == '低' else 'warn' if exit_level == '中' else 'bad'
    tags_html  = ''.join(_tag(t, k) for t, k in w.get('practical_tags', []))
    pos_html   = (
        '<div style="margin-top:5px;line-height:1.65">'
        f'{_tag(pos, pos_kind)}{_tag("出場難度：" + exit_level + "｜" + w.get("exit_label",""), exit_kind)}'
        f'<span style="color:#777;font-size:11px;margin-left:2px">'
        f'5日 {_fmt_pct(w.get("stock_pct5"))}／20日 {_fmt_pct(w.get("stock_pct20"))}　'
        f'乖離5日 {_fmt_pct(w.get("stock_bias5"))}／20日 {_fmt_pct(w.get("stock_bias20"))}</span>'
        '</div>'
    )
    heat_html = (
        f'<div style="color:#b03a2e;font-size:11px;margin-top:2px">'
        f'連續長紅 {w.get("stock_consecutive_long_red",0)} 根'
        f'{"／接近漲停" if w.get("stock_near_limit_up") else ""}'
        f'{"／短線過熱" if w.get("stock_overheat") else ""}</div>'
    )
    reminder_html = ''
    if w.get('operation_reminders'):
        reminder_html = (
            '<div style="margin-top:6px;color:#555;font-size:11px;line-height:1.55">'
            + '<br>'.join(html_mod.escape(x) for x in w.get('operation_reminders', [])[:3])
            + '</div>'
        )

    # copy button
    copy_text  = html_mod.escape(_build_copy_text(w), quote=True)

    sel_html = ded_html = ''
    if show_reasons:
        sels = w.get('selection_reasons', [])
        deds = w.get('deduction_reasons', [])
        if sels:
            sel_html = '<div style="margin-top:5px">' + ''.join(
                f'<span style="background:#eafaf1;color:#1e8449;border:1px solid #abebc6;border-radius:3px;'
                f'padding:1px 6px;font-size:11px;margin:1px 2px 1px 0;display:inline-block">✓ {s}</span>'
                for s in sels
            ) + '</div>'
        if deds:
            ded_html = '<div style="margin-top:3px">' + ''.join(
                f'<span style="background:#fef9e7;color:#784212;border:1px solid #f9e79f;border-radius:3px;'
                f'padding:1px 6px;font-size:11px;margin:1px 2px 1px 0;display:inline-block">▲ {d}</span>'
                for d in deds
            ) + '</div>'

    return f'''
  <tr style="border-bottom:1px solid #f0f0f0;vertical-align:top">
    <td style="padding:10px 8px">
      <div style="font-weight:600;color:#222">{w['code']} {exch_badge}
        <button onclick="copyWarrant(this)" data-text="{copy_text}"
          style="float:right;background:#f0f0f0;border:none;border-radius:4px;
                 padding:2px 8px;font-size:11px;cursor:pointer;color:#555">📋 複製</button>
      </div>
      <div style="font-size:12px;color:#555;margin-top:2px">{w.get('name','')}</div>
      <div style="font-size:12px;color:#888">標的: {w.get('underlying','')}
        <span style="color:#777">{html_mod.escape(w.get('underlying_name',''))}</span>
        <span style="color:{stk_c}">@ {w.get('stock_price',0):.2f} ({stk_chg:+.2f}%)</span></div>
      <div style="margin-top:4px">{flags_html}</div>
      <div style="margin-top:4px">{tags_html}</div>
      {pos_html}{heat_html}
      {sel_html}{ded_html}{reminder_html}
    </td>
    <td style="padding:10px 8px;text-align:center;white-space:nowrap">
      <div style="font-size:26px;font-weight:700;color:{sc_c}">{score}</div>
      <div style="font-size:11px;color:#555;margin-top:-2px">總分 {score}｜實戰 {practical}｜風險{w.get('risk_level','中')}</div>
      <div style="background:#eee;border-radius:3px;height:5px;margin:4px 0 6px">
        <div style="width:{score}%;background:{sc_c};height:5px;border-radius:3px"></div>
      </div>
      <div style="font-size:10px;color:{cp_c}">完整度 {cp}%</div>
      <div style="background:#eee;border-radius:2px;height:3px;margin-top:2px">
        <div style="width:{cp}%;background:{cp_c};height:3px;border-radius:2px"></div>
      </div>
    </td>
    <td style="padding:10px 8px;font-size:13px;white-space:nowrap">
      <div>現價 <b>${w.get('close',0):.2f}</b></div>
      <div>履約 <b style="color:{'#555' if w.get('strike',0)>0 else '#e74c3c'}">{strike_s}</b></div>
      <div>剩餘 <b style="color:{'#e74c3c' if dl<60 else '#333'}">{dl}天</b></div>
      <div>量 {w.get('volume',0):,}張</div>
      <div>價差 {spd:.1f}%</div>
      <div style="color:#aaa;font-size:11px">比例 {w.get('ratio',1):.2f}</div>
    </td>
    <td style="padding:10px 8px;font-size:13px;white-space:nowrap">
      <div>IV <b>{iv_s}</b></div>
      <div>IV/HV <b>{iv_hv_s}</b></div>
      <div>槓桿 <b>{lev_s}</b></div>
      <div>價性 <span style="color:{money_c}">{money_s}</span></div>
      <div style="color:#aaa;font-size:11px">股強 {w.get('stock_score',0)}分</div>
    </td>
  </tr>'''


_stock_ind_cache = {}


def _warrant_table(warrants, limit=30):
    if not warrants:
        return '<p style="color:#aaa;padding:10px 0">暫無符合條件的候選</p>'
    rows = ''.join(_warrant_card(w) for w in warrants[:limit])
    extra = f'<p style="text-align:center;color:#aaa;font-size:12px;margin-top:8px">（共 {len(warrants)} 支，顯示前 {limit} 筆）</p>' if len(warrants) > limit else ''
    return f'''<div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8f8f8;color:#888;font-size:12px">
          <th style="padding:8px;text-align:left">代號 / 名稱 / 理由</th>
          <th style="padding:8px;text-align:center">評分<br>完整度</th>
          <th style="padding:8px;text-align:left">基本</th>
          <th style="padding:8px;text-align:left">指標</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table></div>{extra}'''


def _compact_row(w, show_flags=True, show_cp=True):
    """資料不足 / 高風險區的精簡列"""
    score  = w.get('score', 0)
    sc_c   = _score_color(score)
    cp     = w.get('completeness_pct', 0)
    cp_c   = _compl_color(cp)
    flags  = _flag_html(w.get('risk_flags', [])) if show_flags else ''
    blocking = w.get('blocking_missing_fields') or w.get('missing_fields', [])
    exclusion = w.get('exclusion_reasons', [])
    missing = '、'.join(exclusion or blocking)
    copy_text = html_mod.escape(_build_copy_text(w), quote=True)
    return f'''
  <tr style="border-bottom:1px solid #f5f5f5;font-size:12px">
    <td style="padding:7px 8px">
      <span style="font-weight:600">{w['code']}</span>
      <span style="color:#888;margin-left:5px">{w.get('name','')}</span>
    </td>
    <td style="padding:7px 8px;color:#888">{w.get('underlying','')}
      <span style="color:#aaa">@ {w.get('stock_price',0):.2f}</span></td>
    <td style="padding:7px 8px;text-align:center;font-weight:700;color:{sc_c}">{score}</td>
    {'<td style="padding:7px 8px;color:' + cp_c + '">' + str(cp) + '%<br><span style="color:#bbb;font-size:11px">' + missing + '</span></td>' if show_cp else ''}
    <td style="padding:7px 8px">
      {flags}
      {('<br><span style="color:#c0392b;font-size:11px">排除：' + '、'.join(exclusion) + '</span>') if exclusion else ''}
    </td>
    <td style="padding:7px 4px">
      <button onclick="copyWarrant(this)" data-text="{copy_text}"
        style="background:#f0f0f0;border:none;border-radius:4px;
               padding:2px 6px;font-size:10px;cursor:pointer;color:#555">📋</button>
    </td>
  </tr>'''


def _stock_rows(stocks, indicators, top=20, names=None):
    names = names or {}
    rows = ''
    for sym, sc in list(stocks.items())[:top]:
        ind = indicators.get(sym, {})
        pr  = ind.get('price', 0)
        chg = ind.get('chg_pct', 0)
        rsi = ind.get('rsi')
        vr  = ind.get('vol_ratio', 1)
        m20 = ind.get('ma20')
        c   = '#27ae60' if chg >= 0 else '#e74c3c'
        sc_c = _score_color(sc)
        if m20 and pr > m20:
            trend = '↑均線上'
            trend_c = '#27ae60'
        elif m20:
            trend = '↓均線下'
            trend_c = '#e74c3c'
        else:
            trend = '—'
            trend_c = '#aaa'
        sname = names.get(sym, '')
        sym_cell = (f'{sym}<br><span style="font-size:11px;color:#888">{sname}</span>'
                    if sname else sym)
        rows += f'''
        <tr style="border-bottom:1px solid #f5f5f5">
          <td style="padding:8px">{sym_cell}</td>
          <td style="padding:8px;font-weight:600">{pr:.2f}</td>
          <td style="padding:8px;color:{c}">{chg:+.2f}%</td>
          <td style="padding:8px">{f"{rsi:.0f}" if rsi else "—"}</td>
          <td style="padding:8px">{vr:.1f}x</td>
          <td style="padding:8px;color:{trend_c}">{trend}</td>
          <td style="padding:8px;font-weight:700;color:{sc_c}">{sc}</td>
        </tr>'''
    return rows


def _steady_call_score(w):
    score = w.get('score', 0) * 0.35
    score += w.get('practical_score', 0) * 0.25
    dl = w.get('days_left', 0)
    lev = w.get('leverage') or 0
    vol = w.get('volume', 0)
    spd = w.get('spread_pct', 99)
    m = w.get('moneyness')
    pos = w.get('stock_position')

    score += 18 if 60 <= dl <= 120 else 8 if 45 <= dl <= 150 else -15
    score += 16 if 3 <= lev <= 6 else 8 if 2.5 <= lev <= 8 else -8
    score += 12 if vol >= 500 else 8 if vol >= 200 else 4 if vol >= 100 else -8
    score += 12 if spd <= 2 else 8 if spd <= 3.5 else -12
    score += 12 if m is not None and -12 <= m <= 8 else 4 if m is not None and m >= -18 else -10
    score += 10 if pos == '低位轉強' else 6 if pos == '強勢續攻' else -6 if pos == '短線過熱' else 0
    pct5 = w.get('stock_pct5') or 0
    pct20 = w.get('stock_pct20') or 0
    bias20 = w.get('stock_bias20') or 0
    if pos == '強勢續攻' and (pct5 <= 8 and pct20 <= 18 and bias20 <= 10):
        score += 5
    if w.get('stock_overheat'):
        score -= 25
    return score


def calc_scalp_momentum_score(w):
    chg = w.get('chg_pct') or _stock_ind_cache.get(w.get('underlying', ''), {}).get('chg_pct', 0) or 0
    vol_ratio = _stock_ind_cache.get(w.get('underlying', ''), {}).get('vol_ratio', 1)
    pct5 = w.get('stock_pct5') or 0
    stock_score = w.get('stock_score', 0)
    vol = w.get('volume', 0)
    spd = w.get('spread_pct', 99)
    dl = w.get('days_left', 0)
    m = w.get('moneyness')
    momentum = 0

    momentum += 18 if chg >= 4 else 14 if chg >= 2.5 else 9 if chg >= 1 else 3 if chg > 0 else -8
    momentum += 14 if vol_ratio >= 1.8 else 10 if vol_ratio >= 1.3 else 5 if vol_ratio >= 1.0 else 0
    momentum += 12 if 3 <= pct5 <= 10 else 8 if 0 < pct5 < 3 else 4 if 10 < pct5 <= 15 else -6 if pct5 > 15 else 0
    momentum += int(stock_score / 100 * 14)
    momentum += 12 if vol >= 500 else 8 if vol >= 200 else 4 if vol >= 100 else -8
    momentum += 10 if spd <= 2.5 else 6 if spd <= 4 else -12
    momentum += 8 if 60 <= dl <= 150 else 4 if 45 <= dl < 60 else -12

    if w.get('stock_overheat') or pct5 > 12 or (w.get('stock_pct20') or 0) > 25:
        momentum -= 10
    if m is not None and m < -20:
        momentum -= 10
    if spd > 4:
        momentum -= 12

    return max(0, min(100, int(round(momentum))))


def _scalp_call_score(w):
    momentum = calc_scalp_momentum_score(w)
    w['scalp_momentum_score'] = momentum
    score = momentum * 0.55
    score += w.get('stock_score', 0) * 0.20
    score += w.get('practical_score', 0) * 0.15
    chg = w.get('chg_pct') or _stock_ind_cache.get(w.get('underlying', ''), {}).get('chg_pct', 0) or 0
    pct5 = w.get('stock_pct5') or 0
    pct20 = w.get('stock_pct20') or 0
    vol_ratio = _stock_ind_cache.get(w.get('underlying', ''), {}).get('vol_ratio', 1)
    lev = w.get('leverage') or 0
    vol = w.get('volume', 0)
    spd = w.get('spread_pct', 99)
    dl = w.get('days_left', 0)

    score += 16 if chg >= 3 else 10 if chg >= 1.5 else 4 if chg > 0 else -8
    score += 12 if vol_ratio >= 1.5 else 7 if vol_ratio >= 1.2 else 0
    score += 12 if 5 <= lev <= 12 else 7 if 3 <= lev < 5 or 12 < lev <= 18 else -4
    score += 10 if vol >= 500 else 7 if vol >= 200 else 3 if vol >= 100 else -10
    score += 10 if spd <= 2.5 else 5 if spd <= 4 else -12
    score += 6 if dl >= 60 else 2 if dl >= 45 else -15
    if pct5 >= 10 or pct20 >= 18 or w.get('stock_overheat'):
        score += 3
    if w.get('exit_difficulty') == '高':
        score -= 20
    return score


def _put_watch_score(w):
    score = w.get('stock_score', 0) * 0.35
    score += w.get('practical_score', 0) * 0.30
    pos = w.get('stock_position')
    vol = w.get('volume', 0)
    spd = w.get('spread_pct', 99)
    score += 16 if pos == '弱勢破底' else 4 if pos == '盤整不明' else -8
    score += 10 if vol >= 300 else 6 if vol >= 100 else 2 if vol >= 50 else -10
    score += 10 if spd <= 2.5 else 5 if spd <= 4 else -12
    if w.get('exit_difficulty') == '高':
        score -= 18
    return score


def _watch_reason(w, bucket):
    parts = []
    pos = w.get('stock_position')
    if pos in ('低位轉強', '強勢續攻', '短線過熱', '弱勢破底'):
        parts.append(pos)
    chg = w.get('chg_pct') or _stock_ind_cache.get(w.get('underlying', ''), {}).get('chg_pct', 0) or 0
    vol_ratio = _stock_ind_cache.get(w.get('underlying', ''), {}).get('vol_ratio', 1)
    if bucket == '短打':
        if chg >= 1.5 and vol_ratio >= 1.2:
            parts.append('今日量價轉強')
        elif chg > 0:
            parts.append('短線動能轉強')
    if w.get('volume', 0) >= 200:
        parts.append('成交量充足')
    elif w.get('volume', 0) >= 50:
        parts.append('成交量尚可')
    if w.get('spread_pct', 99) <= 2.5:
        parts.append('價差小')
    elif w.get('spread_pct', 99) <= 4:
        parts.append('價差可接受')
    if 60 <= w.get('days_left', 0) <= 120:
        parts.append('剩餘天數適中')
    if bucket == '短打':
        if ((w.get('stock_pct5') or 0) >= 10 or (w.get('stock_pct20') or 0) >= 18 or w.get('stock_overheat')):
            parts.append('僅適合短打觀察，不宜追高')
        elif pos == '強勢續攻':
            parts.append('適合盤中觀察，不宜追高')
    if bucket == '認售':
        parts.append('認售條件尚可')
        if w.get('exit_difficulty') != '低':
            parts.append('需注意流動性')
    if not parts:
        parts.append('條件相對均衡')
    return '入選原因：' + '、'.join(parts[:5]) + '。'


def _unique_top(items, key_func, limit):
    result = []
    seen = set()
    for w in sorted(items, key=key_func, reverse=True):
        code = w.get('code')
        if code in seen:
            continue
        seen.add(code)
        result.append(w)
        if len(result) >= limit:
            break
    return result


def build_watchlists(formal_calls, formal_puts):
    steady_calls = [
        w for w in formal_calls
        if 60 <= w.get('days_left', 0) <= 120
        and w.get('volume', 0) >= 100
        and w.get('spread_pct', 99) <= 3.5
        and (w.get('leverage') is None or 3 <= w.get('leverage', 0) <= 6)
        and (w.get('moneyness') is None or w.get('moneyness', 0) >= -15)
        and not w.get('stock_overheat')
        and w.get('exit_difficulty') != '高'
        and w.get('stock_position') in ('低位轉強', '強勢續攻', '盤整不明')
        and not has_steady_exclusion_signal(w)
    ]
    steady_top = _unique_top(steady_calls, _steady_call_score, 5)
    steady_codes = {w.get('code') for w in steady_top}

    scalp_calls = [
        w for w in formal_calls
        if w.get('stock_score', 0) >= 65
        and w.get('volume', 0) >= 100
        and w.get('spread_pct', 99) <= 4
        and w.get('days_left', 0) >= 45
        and w.get('exit_difficulty') != '高'
        and w.get('code') not in steady_codes
    ]
    put_watch = [
        w for w in formal_puts
        if w.get('stock_score', 0) >= 55
        and w.get('volume', 0) >= 50
        and w.get('spread_pct', 99) <= 4
        and w.get('stock_position') in ('弱勢破底', '盤整不明')
        and w.get('exit_difficulty') != '高'
    ]
    return {
        '穩健認購 Top 5': steady_top,
        '短打認購 Top 5': _unique_top(scalp_calls, _scalp_call_score, 5),
        '認售觀察 Top 5': _unique_top(put_watch, _put_watch_score, 5),
    }


def _watch_row(w, bucket):
    code = html_mod.escape(w.get('code', ''))
    udly = html_mod.escape(w.get('underlying', ''))
    uname = html_mod.escape(w.get('underlying_name', ''))
    pos = w.get('stock_position', '—')
    pos_kind = 'bad' if pos == '短線過熱' else 'info' if pos == '弱勢破底' else 'good' if pos in ('低位轉強', '強勢續攻') else 'neutral'
    chase = _tag('追高風險', 'bad') if w.get('stock_overheat') or (w.get('chg_pct') or 0) >= 7 else ''
    tags = ''.join(_tag(t, k) for t, k in w.get('practical_tags', [])[:5])
    reason = html_mod.escape(_watch_reason(w, bucket))
    risk_kind = 'good' if w.get('risk_level') == '低' else 'warn' if w.get('risk_level') == '中' else 'bad'
    momentum_html = ''
    if bucket == '短打':
        momentum_html = f'<br><span style="font-size:11px;color:#777">短線動能 {w.get("scalp_momentum_score", calc_scalp_momentum_score(w))}</span>'
    return f'''
    <tr style="border-bottom:1px solid #f2f2f2">
      <td style="padding:8px">
        <b>{code}</b><br>
        <span style="font-size:11px;color:#777">{udly} {uname}</span>
      </td>
      <td style="padding:8px;text-align:center;font-weight:700;color:{_score_color(w.get('practical_score',0))}">
        實戰 {w.get('practical_score',0)}<br>
        <span style="font-size:11px;color:#666;font-weight:400">總分 {w.get('score',0)}｜實戰 {w.get('practical_score',0)}｜風險{w.get('risk_level','中')}</span><br>
        {_tag('風險' + w.get('risk_level','中'), risk_kind)}{momentum_html}
      </td>
      <td style="padding:8px;font-size:12px">
        現價 {w.get('close',0):.2f}／量 {w.get('volume',0):,}<br>
        價差 {w.get('spread_pct',0):.1f}%／剩 {w.get('days_left',0)}天
      </td>
      <td style="padding:8px;font-size:12px">
        {_tag(pos, pos_kind)}{_tag(w.get('exit_label','—'), 'good' if w.get('exit_difficulty') == '低' else 'warn' if w.get('exit_difficulty') == '中' else 'bad')}{chase}<br>
        <span style="color:#777">5日 {_fmt_pct(w.get('stock_pct5'))}／20日 {_fmt_pct(w.get('stock_pct20'))}</span><br>
        {tags}
        <div style="margin-top:4px;color:#555;line-height:1.45">{reason}</div>
      </td>
    </tr>'''


def _watchlist_block(watchlists):
    blocks = []
    for title, items in watchlists.items():
        bucket = '穩健' if title.startswith('穩健') else '短打' if title.startswith('短打') else '認售'
        rows = ''.join(_watch_row(w, bucket) for w in items)
        if not rows:
            rows = '<tr><td colspan="4" style="padding:12px;color:#aaa">今日沒有符合這組實戰條件的權證</td></tr>'
        blocks.append(f'''
        <div style="margin-bottom:14px">
          <div style="font-weight:700;margin:4px 0 8px">{title}</div>
          <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              <thead>
                <tr style="background:#f8f8f8;color:#888;font-size:12px">
                  <th style="padding:8px;text-align:left">權證 / 標的</th>
                  <th style="padding:8px;text-align:center">實戰分</th>
                  <th style="padding:8px;text-align:left">權證條件</th>
                  <th style="padding:8px;text-align:left">觀察重點</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        </div>''')
    return ''.join(blocks)


def build_risk_stats(high_risk, insufficient):
    stats = Counter()
    for w in high_risk:
        keys = set(w.get('exclusion_keys', []))
        flag_keys = {f.get('key') for f in w.get('risk_flags', [])}
        if 'zero_volume' in keys:
            stats['成交量為0'] += 1
        if 'low_volume' in flag_keys:
            stats['成交量偏低'] += 1
        if 'wide_spread_gate' in keys or 'wide_spread' in flag_keys:
            stats['買賣價差過大'] += 1
        if 'short_days_gate' in keys or 'near_expiry' in flag_keys:
            stats['剩餘天數過短'] += 1
        if 'deep_otm' in flag_keys:
            stats['深度價外'] += 1
        if 'high_iv' in flag_keys:
            stats['IV偏貴'] += 1
    stats['資料不足'] = len(insufficient)
    return stats


def _risk_stats_block(stats):
    order = ['成交量為0', '成交量偏低', '買賣價差過大', '剩餘天數過短', '深度價外', 'IV偏貴', '資料不足']
    cells = ''.join(
        f'<div class="stat-box"><div class="stat-val" style="color:#e67e22">{stats.get(k,0)}</div>'
        f'<div class="stat-lbl">{k}</div></div>'
        for k in order
    )
    return f'<div class="stat-grid">{cells}</div>'


# ─── HTML 報表產生 ────────────────────────────────────────

def generate_html(strong_stocks, weak_stocks, stock_indicators,
                  formal_calls, formal_puts, insufficient, high_risk,
                  run_time, stock_names=None):
    global _stock_ind_cache
    _stock_ind_cache = stock_indicators
    names = stock_names or {}

    now_str  = run_time.strftime('%Y/%m/%d %H:%M')
    date_str = run_time.strftime('%m/%d')

    total_formal = len(formal_calls) + len(formal_puts)
    total_all    = total_formal + len(insufficient) + len(high_risk)
    high_score   = sum(1 for w in formal_calls + formal_puts if w.get('score', 0) >= 75)
    watchlists   = build_watchlists(formal_calls, formal_puts)
    watch_html   = _watchlist_block(watchlists)
    risk_stats   = build_risk_stats(high_risk, insufficient)
    risk_stats_html = _risk_stats_block(risk_stats)
    stock_radar = build_stock_radar(
        strong_stocks, weak_stocks, stock_indicators,
        formal_calls, formal_puts, names
    )
    stock_radar_html = render_stock_radar_html(stock_radar)

    strong_rows = _stock_rows(strong_stocks, stock_indicators, names=names)
    weak_rows   = _stock_rows(weak_stocks,   stock_indicators, names=names)

    # ── 認購正式清單 ──
    calls_html = _warrant_table(formal_calls)

    # ── 認售正式清單 ──
    puts_html = _warrant_table(formal_puts)

    # ── 資料不足 ──
    if insufficient:
        insuf_rows = ''.join(_compact_row(w, show_flags=False, show_cp=True) for w in insufficient[:50])
        insuf_block = f'''
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead>
            <tr style="background:#f8f8f8;color:#888;font-size:11px">
              <th style="padding:7px 8px;text-align:left">代號</th>
              <th style="padding:7px 8px;text-align:left">標的</th>
              <th style="padding:7px 8px;text-align:center">分數</th>
              <th style="padding:7px 8px">完整度 / 缺少欄位</th>
              <th style="padding:7px 8px">標記</th>
              <th></th>
            </tr>
          </thead>
          <tbody>{insuf_rows}</tbody>
        </table>'''
    else:
        insuf_block = '<p style="color:#aaa;padding:10px 0">無資料不足項目</p>'

    # ── 高風險排除 ──
    if high_risk:
        risk_rows = ''.join(_compact_row(w, show_flags=True, show_cp=True) for w in high_risk[:50])
        risk_block = f'''
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead>
            <tr style="background:#f8f8f8;color:#888;font-size:11px">
              <th style="padding:7px 8px;text-align:left">代號</th>
              <th style="padding:7px 8px;text-align:left">標的</th>
              <th style="padding:7px 8px;text-align:center">分數</th>
              <th style="padding:7px 8px">完整度</th>
              <th style="padding:7px 8px">風險標記</th>
              <th></th>
            </tr>
          </thead>
          <tbody>{risk_rows}</tbody>
        </table>'''
    else:
        risk_block = '<p style="color:#aaa;padding:10px 0">無高風險排除項目</p>'

    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>權證篩選 {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;
     background:#f0ede8;color:#1e293b;padding:16px}}
.wrap{{max-width:980px;margin:0 auto}}
.hdr{{background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;
     border-radius:16px;padding:22px 26px;margin-bottom:14px}}
.hdr h1{{font-size:20px;font-weight:800;letter-spacing:.3px}}
.hdr .sub{{font-size:12px;color:#94a3b8;margin-top:6px}}
.card{{background:#fff;border-radius:16px;padding:20px 22px;margin-bottom:14px;
      box-shadow:0 2px 12px rgba(15,23,42,.07)}}
.card-title{{font-size:16px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:8px}}
.badge{{font-size:11px;font-weight:normal;padding:2px 8px;border-radius:20px;color:#fff}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}}
.stat-box{{background:#f8f7f4;border-radius:10px;padding:14px;text-align:center}}
.stat-val{{font-size:26px;font-weight:700}}
.stat-lbl{{font-size:11px;color:#94a3b8;margin-top:3px}}
.tabs{{display:flex;gap:8px;margin:0 0 14px;position:sticky;top:8px;z-index:5;
      background:rgba(240,237,232,.92);backdrop-filter:blur(8px);padding:6px 0}}
.tab-btn{{flex:1;border:1px solid #e2e8f0;background:#fff;color:#64748b;border-radius:10px;
         padding:10px 12px;font-size:14px;font-weight:700;cursor:pointer;transition:all .15s}}
.tab-btn.active{{background:#0f172a;color:#fff;border-color:#0f172a}}
.tab-panel{{display:none}}
.tab-panel.active{{display:block}}
.tracker-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tracker-grid label{{font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.4px}}
.tracker-grid input,.tracker-grid select{{width:100%;margin-top:4px;border:1px solid #e2e8f0;border-radius:8px;
      padding:9px 10px;font-size:13px;background:#fff;color:#334155}}
.tracker-actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}
.tracker-actions button{{border:none;border-radius:8px;padding:9px 12px;font-size:13px;font-weight:700;cursor:pointer}}
.tracker-top{{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid #f1f5f9;margin-bottom:20px}}
.tracker-title h2{{font-size:22px;font-weight:800;color:#0f172a;letter-spacing:-.3px;margin-bottom:3px}}
.tracker-sub{{font-size:12px;color:#94a3b8}}
.tracker-toolbar{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.tracker-toolbar select{{border:1px solid #e2e8f0;border-radius:9px;padding:8px 10px;background:#f8fafc;font-size:13px;color:#475569}}
.tracker-primary{{background:#0f172a!important;color:#fff!important;border:none!important;border-radius:10px;padding:9px 16px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.2px}}
.tracker-secondary{{background:#f1f5f9!important;color:#475569!important;border:1px solid #e2e8f0!important;border-radius:10px;padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer}}
.tracker-danger{{background:#fff5f3!important;color:#c0392b!important;border:1px solid #fecaca!important;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}}
.tracker-main-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:4px}}
.tracker-small-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:12px}}
.metric-card{{background:#fff;border-radius:16px;padding:18px 20px;border:1px solid #f1f5f9;box-shadow:0 2px 12px rgba(15,23,42,.06);position:relative;overflow:hidden}}
.metric-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent,#334155);opacity:.8;border-radius:16px 16px 0 0}}
.metric-card.primary{{min-height:108px}}
.metric-label{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.7px;font-weight:700;margin-bottom:10px}}
.metric-value{{font-size:28px;font-weight:800;line-height:1.1;letter-spacing:-.5px}}
.metric-note{{font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.4}}
.metric-small .metric-value{{font-size:20px}}
.cash-detail-row{{display:flex;gap:0;flex-wrap:wrap;background:#f8fafc;border:1px solid #e8eef4;border-radius:12px;padding:10px 16px;margin:8px 0 16px;font-size:12px;color:#64748b}}
.cash-detail-row span{{padding:2px 12px 2px 0;border-right:1px solid #e2e8f0;margin-right:12px;white-space:nowrap}}
.cash-detail-row span:last-child{{border-right:none;margin-right:0}}
.section-label{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:#94a3b8;margin:16px 0 10px;padding-left:2px}}
.returns-dashboard{{display:grid;gap:16px}}
.chart-grid{{display:grid;grid-template-columns:1.3fr .9fr;gap:12px}}
.chart-card{{background:#fff;border-radius:16px;padding:18px;box-shadow:0 2px 10px rgba(15,23,42,.05);border:1px solid #f1f5f9}}
.chart-title{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#64748b;margin-bottom:12px;display:flex;justify-content:space-between;gap:8px;align-items:center}}
.chart-tabs{{display:flex;gap:4px;flex-wrap:wrap}}
.chart-tabs button{{border:1px solid #e2e8f0;background:#f8fafc;border-radius:8px;padding:4px 10px;font-size:11px;color:#64748b;cursor:pointer;font-weight:600}}
.chart-tabs button.active{{background:#0f172a;color:#fff;border-color:#0f172a}}
.simple-chart{{width:100%;height:190px;display:block}}
.donut-wrap{{display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap}}
.donut-legend{{font-size:12px;color:#64748b;line-height:1.9}}
.holding-list{{display:grid;gap:8px}}
.holding-row{{background:#fff;border-radius:14px;padding:14px 18px;box-shadow:0 1px 6px rgba(15,23,42,.05);border:1px solid #f1f5f9;display:grid;grid-template-columns:1.2fr repeat(4,.75fr);gap:8px;align-items:center}}
.holding-row b{{font-size:14px;color:#0f172a;font-weight:800}}
.holding-cell{{font-size:12px;color:#94a3b8;font-weight:500}}
.holding-cell strong{{display:block;font-size:14px;color:#334155;margin-top:3px;font-weight:700}}
.recent-list{{display:grid;gap:6px}}
.recent-row{{background:#fff;border-radius:12px;padding:10px 14px;display:grid;grid-template-columns:90px 1fr 70px 90px;gap:8px;align-items:center;box-shadow:0 1px 4px rgba(15,23,42,.04);border:1px solid #f1f5f9;font-size:12px}}
.tracker-filter{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 10px}}
.tracker-filter button{{border:1px solid #e2e8f0;background:#fff;border-radius:999px;padding:6px 12px;font-size:12px;cursor:pointer;font-weight:600;color:#64748b}}
.tracker-filter button.active{{background:#0f172a;color:#fff;border-color:#0f172a}}
.position-card{{border:1px solid #f1f5f9;border-radius:14px;padding:14px 18px;margin-bottom:10px;background:#fff;box-shadow:0 1px 4px rgba(15,23,42,.04)}}
.position-head{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}}
.position-title{{font-weight:800;font-size:15px;color:#0f172a}}
.position-meta,.position-line{{font-size:12px;color:#94a3b8;line-height:1.7}}
.position-pnl{{text-align:right;font-weight:800;font-size:18px}}
.mini-actions{{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}}
.mini-actions button{{border:1px solid #e2e8f0;background:#f8fafc;border-radius:8px;padding:7px 12px;font-size:12px;font-weight:700;cursor:pointer;color:#475569}}
.returns-modal{{display:none;position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:50;padding:18px;align-items:flex-end;justify-content:center}}
.returns-modal.open{{display:flex}}
.returns-dialog{{background:#fff;border-radius:20px;width:min(760px,100%);max-height:92vh;overflow:auto;padding:24px;box-shadow:0 20px 60px rgba(15,23,42,.22)}}
.returns-dialog-head{{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #f1f5f9}}
.returns-close{{border:1px solid #e2e8f0;background:#f8fafc;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:13px;color:#64748b;font-weight:600}}
.advanced-box{{margin-top:10px;border-top:1px solid #f1f5f9;padding-top:12px}}
.ledger-extra{{display:none}}
.ledger-extra.show{{display:block}}
.analysis-entry{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}}
.analysis-entry button{{border:1px solid #e2e8f0;background:#f8fafc;border-radius:10px;padding:12px;font-weight:700;color:#475569;cursor:pointer;font-size:13px}}
.profit-bar{{height:6px;background:#f1f5f9;border-radius:99px;overflow:hidden;margin-top:6px}}
.profit-fill{{height:6px;border-radius:99px;transition:width .3s ease}}
details summary{{cursor:pointer;list-style:none;user-select:none}}
details summary::-webkit-details-marker{{display:none}}
details summary::before{{content:"▶ ";font-size:10px;color:#94a3b8}}
details[open] summary::before{{content:"▼ ";}}
@media(max-width:620px){{
  body{{padding:8px}}
  .tracker-main-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .chart-grid{{grid-template-columns:1fr}}
  .holding-row{{grid-template-columns:1fr 1fr}}
  .recent-row{{grid-template-columns:74px 1fr;gap:4px}}
  .tracker-top{{display:block}}
  .tracker-toolbar{{margin-top:10px}}
  .position-head{{display:block}}
  .position-pnl{{text-align:left;margin-top:6px}}
  .stat-val{{font-size:20px}}
  .metric-value{{font-size:22px}}
  td:nth-child(4){{display:none}}
}}
</style>
<script>
function copyWarrant(btn) {{
  var text = btn.dataset.text || btn.getAttribute('data-text');
  var orig = btn.textContent;
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(function(){{
      btn.textContent='✅'; setTimeout(function(){{btn.textContent=orig;}},2000);
    }});
  }} else {{
    var ta=document.createElement('textarea');
    ta.value=text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    btn.textContent='✅'; setTimeout(function(){{btn.textContent=orig;}},2000);
  }}
}}
function showTab(tab) {{
  document.querySelectorAll('.tab-btn').forEach(function(btn) {{
    btn.classList.toggle('active', btn.dataset.tab === tab);
  }});
  document.querySelectorAll('.tab-panel').forEach(function(panel) {{
    panel.classList.toggle('active', panel.id === 'tab-' + tab);
  }});
}}
var TRACKER_LEDGER_KEY = 'twReturnLedger';
var TRACKER_OLD_KEY = 'twReturnTracker';
var TRACKER_BACKUP_KEY = 'twReturnTrackerBackupV1';
var TRACKER_MIGRATED_KEY = 'twReturnLedgerMigratedV1';
var TRACKER_PRICE_KEY = 'twReturnPrices';
function trackerUid() {{
  return 'tx_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
}}
function trackerLoadJson(key, fallback) {{
  try {{ return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback)); }}
  catch(e) {{ return fallback; }}
}}
function trackerSaveJson(key, value) {{
  localStorage.setItem(key, JSON.stringify(value));
}}
function trackerSourceValue(text) {{
  if (text === '股票雷達' || text === 'stock_radar') return 'stock_radar';
  if (text === '網頁評分篩選' || text === '權證篩選' || text === 'screener') return 'screener';
  if (text === '事件交易' || text === 'event') return 'event';
  if (text === '推薦' || text === 'recommendation') return 'recommendation';
  if (text === '其他' || text === 'other') return 'other';
  return 'self';
}}
function trackerSourceLabel(value) {{
  if (value === 'stock_radar') return '股票雷達';
  if (value === 'screener') return '網頁評分篩選';
  if (value === 'event') return '事件交易';
  if (value === 'recommendation') return '推薦';
  if (value === 'other') return '其他';
  return '自行判斷';
}}
function trackerAssetValue(text) {{
  if (text === '股票' || text === 'stock') return 'stock';
  return 'warrant';
}}
function trackerAssetLabel(value) {{
  return value === 'stock' ? '股票' : '權證';
}}
function trackerWarrantLabel(value) {{
  if (value === 'call') return '認購';
  if (value === 'put') return '認售';
  return '未分類';
}}
function trackerPositionKey(tx) {{
  return [tx.account_id || 'default', tx.asset_type || 'warrant', tx.warrant_type || '', tx.symbol].join('|');
}}
function trackerLoadPrices() {{
  return trackerLoadJson(TRACKER_PRICE_KEY, {{}});
}}
function trackerSavePrices(prices) {{
  trackerSaveJson(TRACKER_PRICE_KEY, prices);
}}
function trackerMigrateOldData() {{
  if (localStorage.getItem(TRACKER_MIGRATED_KEY) === '1') return;
  var oldRaw = localStorage.getItem(TRACKER_OLD_KEY);
  if (!oldRaw || localStorage.getItem(TRACKER_LEDGER_KEY)) {{
    localStorage.setItem(TRACKER_MIGRATED_KEY, '1');
    return;
  }}
  localStorage.setItem(TRACKER_BACKUP_KEY, oldRaw);
  var oldRows = trackerLoadJson(TRACKER_OLD_KEY, []);
  var prices = trackerLoadPrices();
  var ledger = oldRows.map(function(r, idx) {{
    var side = (r.action || r.side) === 'sell' ? 'sell' : 'buy';
    var assetType = trackerAssetValue(r.type || r.asset_type);
    var tx = {{
      id: String(r.id || ('legacy_' + idx + '_' + Date.now())),
      account_id: r.account_id || 'default',
      asset_type: assetType,
      warrant_type: r.warrant_type || null,
      symbol: String(r.code || r.symbol || '').trim(),
      name: r.name || '',
      side: side,
      price: side === 'sell' ? Number(r.current || r.price || 0) : Number(r.buy || r.price || 0),
      quantity: Number(r.qty || r.quantity || 0),
      trade_date: r.date || r.trade_date || new Date().toISOString().slice(0,10),
      source: trackerSourceValue(r.source),
      note: r.note || '',
      fee: Number(r.fee || 0),
      tax: Number(r.tax || 0),
      other_cost: Number(r.other_cost || 0),
      created_at: r.created_at || new Date().toISOString()
    }};
    if (tx.symbol) {{
      prices[trackerPositionKey(tx)] = Number(r.current || tx.price || 0);
    }}
    return tx;
  }}).filter(function(tx) {{ return tx.symbol && tx.price > 0 && tx.quantity > 0; }});
  trackerSaveJson(TRACKER_LEDGER_KEY, ledger);
  trackerSavePrices(prices);
  localStorage.setItem(TRACKER_MIGRATED_KEY, '1');
}}
function trackerLoad() {{
  trackerMigrateOldData();
  return trackerLoadJson(TRACKER_LEDGER_KEY, []);
}}
function trackerSave(rows) {{
  trackerSaveJson(TRACKER_LEDGER_KEY, rows);
}}
function trackerLoadBudget() {{
  var v = parseFloat(localStorage.getItem('twReturnBudget') || '10000');
  return isNaN(v) || v < 0 ? 10000 : v;
}}
function trackerSaveBudget(skipRender) {{
  var el = document.getElementById('rt-budget');
  if (!el) return;
  if (el.value === '') return;
  var v = parseFloat(el.value);
  if (isNaN(v) || v < 0) {{
    alert('投入本金請輸入 0 以上的數字');
    return;
  }}
  localStorage.setItem('twReturnBudget', String(v));
  if (skipRender === true) return;
  renderTracker();
}}
function trackerBudgetChanged() {{
  trackerSaveBudget(true);
  window.clearTimeout(window._trackerBudgetTimer);
  window._trackerBudgetTimer = window.setTimeout(function() {{
    renderTracker();
  }}, 250);
}}
function trackerNum(id) {{
  var el = document.getElementById(id);
  var v = parseFloat(el ? el.value : '');
  return isNaN(v) ? 0 : v;
}}
function trackerText(id) {{
  var el = document.getElementById(id);
  return el ? el.value.trim() : '';
}}
function trackerEscape(text) {{
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}}
function resetTradeForm() {{
  ['rt-code','rt-name','rt-price','rt-qty','rt-note','rt-fee','rt-tax','rt-other-cost','rt-strategy','rt-stop-loss','rt-take-profit'].forEach(function(id){{var el=document.getElementById(id); if(el) el.value='';}});
  document.getElementById('rt-edit-id').value = '';
  document.getElementById('rt-action').value = 'buy';
  document.getElementById('rt-type').value = 'warrant';
  document.getElementById('rt-warrant-type').value = '';
  document.getElementById('rt-source').value = 'self';
  document.getElementById('rt-submit').textContent = '新增紀錄';
  document.getElementById('rt-cancel-edit').style.display = 'none';
  var d = document.getElementById('rt-date');
  if (d) d.value = new Date().toISOString().slice(0,10);
}}
function openTradeModal(title) {{
  var modal = document.getElementById('trade-modal');
  var titleEl = document.getElementById('trade-modal-title');
  if (titleEl) titleEl.textContent = title || '新增交易';
  if (modal) modal.classList.add('open');
}}
function closeTradeModal() {{
  var modal = document.getElementById('trade-modal');
  if (modal) modal.classList.remove('open');
}}
function openNewTrade() {{
  resetTradeForm();
  openTradeModal('新增交易');
}}
function openCapitalSettings() {{
  var box = document.getElementById('capital-settings');
  if (box) box.open = true;
  var el = document.getElementById('rt-budget');
  if (el) {{
    el.focus();
    el.scrollIntoView({{behavior:'smooth', block:'center'}});
  }}
}}
function toggleAllLedger() {{
  var extra = document.getElementById('ledger-extra');
  var btn = document.getElementById('ledger-toggle');
  if (!extra) return;
  extra.classList.toggle('show');
  if (btn) btn.textContent = extra.classList.contains('show') ? '收合交易紀錄' : '查看全部交易紀錄';
}}
document.addEventListener('click', function(e) {{
  var el = e.target.closest('[data-tracker-action]');
  if (!el) return;
  var action = el.getAttribute('data-tracker-action');
  if (action === 'filter') setPositionFilter(el.getAttribute('data-filter'));
  if (action === 'chart-range') setChartRange(el.getAttribute('data-range'));
  if (action === 'prefill') prefillTrade(el.getAttribute('data-pos-key'), el.getAttribute('data-side'), el.getAttribute('data-all-out') === '1');
  if (action === 'prompt-price') updatePositionPrice(el.getAttribute('data-pos-key'), prompt('輸入新的目前價格', el.getAttribute('data-current-price') || ''));
  if (action === 'edit-tx') editTradeRecord(el.getAttribute('data-tx-id'));
  if (action === 'delete-tx') deleteTradeRecord(el.getAttribute('data-tx-id'));
  if (action === 'toggle-ledger') toggleAllLedger();
  if (action === 'go-warrants') {{ showTab('warrants'); location.hash = 'warrants'; }}
}});
function trackerBuildPositions(rows) {{
  var prices = trackerLoadPrices();
  var map = {{}};
  var ledger = rows.slice().sort(function(a, b) {{
    return String(a.trade_date || '').localeCompare(String(b.trade_date || '')) ||
      String(a.created_at || '').localeCompare(String(b.created_at || '')) ||
      String(a.id || '').localeCompare(String(b.id || ''));
  }});
  ledger.forEach(function(tx) {{
    var key = trackerPositionKey(tx);
    if (!map[key]) {{
      map[key] = {{
        key: key,
        account_id: tx.account_id || 'default',
        asset_type: tx.asset_type || 'warrant',
        warrant_type: tx.warrant_type || null,
        symbol: tx.symbol,
        name: tx.name || '',
        source: tx.source || 'self',
        total_buy_quantity: 0,
        total_sell_quantity: 0,
        remaining_quantity: 0,
        remaining_cost: 0,
        realized_pnl: 0,
        buy_spend: 0,
        sell_income: 0,
        transactions: []
      }};
    }}
    var p = map[key];
    var qty = Number(tx.quantity || 0);
    var price = Number(tx.price || 0);
    var costs = Number(tx.fee || 0) + Number(tx.tax || 0) + Number(tx.other_cost || 0);
    var gross = price * qty;
    p.name = tx.name || p.name;
    p.source = tx.source || p.source;
    p.transactions.push(tx);
    if (tx.side === 'sell') {{
      var sellQty = Math.min(qty, p.remaining_quantity);
      var avgBefore = p.remaining_quantity > 0 ? p.remaining_cost / p.remaining_quantity : 0;
      var costRemoved = avgBefore * sellQty;
      p.total_sell_quantity += sellQty;
      p.remaining_quantity -= sellQty;
      p.remaining_cost = Math.max(0, p.remaining_cost - costRemoved);
      p.realized_pnl += (price * sellQty - costs) - costRemoved;
      p.sell_income += gross - costs;
    }} else {{
      var buySpend = gross + costs;
      p.total_buy_quantity += qty;
      p.remaining_quantity += qty;
      p.remaining_cost += buySpend;
      p.buy_spend += buySpend;
    }}
  }});
  return Object.keys(map).map(function(key) {{
    var p = map[key];
    p.average_cost = p.remaining_quantity > 0 ? p.remaining_cost / p.remaining_quantity : 0;
    p.current_price = Number(prices[key] || p.average_cost || 0);
    p.market_value = p.remaining_quantity * p.current_price;
    p.unrealized_pnl = (p.current_price - p.average_cost) * p.remaining_quantity;
    p.total_pnl = p.realized_pnl + p.unrealized_pnl;
    p.unrealized_return_pct = p.remaining_cost > 0 ? p.unrealized_pnl / p.remaining_cost * 100 : 0;
    p.status = p.remaining_quantity > 0 ? 'open' : 'closed';
    if (Math.abs(p.remaining_quantity) < 0.000001) {{
      p.remaining_quantity = 0;
      p.remaining_cost = 0;
      p.average_cost = 0;
      p.market_value = 0;
      p.unrealized_pnl = 0;
    }}
    return p;
  }});
}}
function trackerCalcSummary(rows, positions) {{
  var principal = trackerLoadBudget();
  var buySpend = 0, sellIncome = 0, openCost = 0, marketValue = 0, realized = 0, unrealized = 0;
  rows.forEach(function(tx) {{
    var gross = Number(tx.price || 0) * Number(tx.quantity || 0);
    var costs = Number(tx.fee || 0) + Number(tx.tax || 0) + Number(tx.other_cost || 0);
    if (tx.side === 'sell') sellIncome += gross - costs;
    else buySpend += gross + costs;
  }});
  positions.forEach(function(p) {{
    if (p.status === 'open') {{
      openCost += p.remaining_cost;
      marketValue += p.market_value;
      unrealized += p.unrealized_pnl;
    }}
    realized += p.realized_pnl;
  }});
  var totalPnl = realized + unrealized;
  return {{
    principal: principal,
    availableCash: principal - buySpend + sellIncome,
    openCost: Math.max(0, openCost),
    marketValue: marketValue,
    realized: realized,
    unrealized: unrealized,
    totalPnl: totalPnl,
    returnPct: principal > 0 ? totalPnl / principal * 100 : 0,
    usagePct: principal > 0 ? Math.max(0, openCost) / principal * 100 : 0,
    openCount: positions.filter(function(p){{return p.status === 'open';}}).length,
    closedCount: positions.filter(function(p){{return p.status === 'closed' && p.total_buy_quantity > 0;}}).length,
    buySpend: buySpend,
    sellIncome: sellIncome
  }};
}}
function trackerFormTx() {{
  var side = document.getElementById('rt-action').value || 'buy';
  var price = trackerNum('rt-price');
  return {{
    id: document.getElementById('rt-edit-id').value || trackerUid(),
    account_id: 'default',
    asset_type: document.getElementById('rt-type').value || 'warrant',
    warrant_type: (document.getElementById('rt-type').value === 'warrant' ? (document.getElementById('rt-warrant-type').value || null) : null),
    symbol: trackerText('rt-code'),
    name: trackerText('rt-name'),
    side: side,
    price: price,
    quantity: trackerNum('rt-qty'),
    trade_date: document.getElementById('rt-date').value || new Date().toISOString().slice(0,10),
    source: document.getElementById('rt-source').value || 'self',
    note: trackerText('rt-note'),
    strategy: trackerText('rt-strategy'),
    stop_loss: trackerNum('rt-stop-loss'),
    take_profit: trackerNum('rt-take-profit'),
    fee: trackerNum('rt-fee'),
    tax: trackerNum('rt-tax'),
    other_cost: trackerNum('rt-other-cost'),
    created_at: new Date().toISOString()
  }};
}}
function addTradeRecord() {{
  var tx = trackerFormTx();
  if (!tx.symbol || tx.price <= 0 || tx.quantity <= 0) {{
    alert('請至少填寫代號、交易價格與數量');
    return false;
  }}
  var rows = trackerLoad();
  var editId = document.getElementById('rt-edit-id').value;
  if (editId) {{
    var existing = rows.find(function(r){{return String(r.id) === String(editId);}});
    if (existing) tx.created_at = existing.created_at || tx.created_at;
  }}
  if (tx.side === 'sell') {{
    var rowsForCheck = rows.filter(function(r){{return String(r.id) !== String(editId);}});
    var available = 0;
    trackerBuildPositions(rowsForCheck).forEach(function(p) {{
      if (p.key === trackerPositionKey(tx)) available = p.remaining_quantity;
    }});
    if (tx.quantity > available) {{
      alert('賣出數量超過目前持有數量，請確認交易紀錄。');
      return false;
    }}
  }}
  if (editId) {{
    var updated = false;
    rows = rows.map(function(r){{ if (String(r.id) === String(editId)) {{ updated = true; return tx; }} return r; }});
    if (!updated) rows.push(tx);
  }} else {{
    rows.push(tx);
  }}
  var prices = trackerLoadPrices();
  var currentHint = trackerNum('rt-price') || tx.price;
  prices[trackerPositionKey(tx)] = currentHint;
  trackerSavePrices(prices);
  trackerSave(rows);
  resetTradeForm();
  renderTracker();
  return true;
}}
function editTradeRecord(id) {{
  var row = trackerLoad().find(function(r){{return String(r.id) === String(id);}});
  if (!row) return;
  document.getElementById('rt-edit-id').value = row.id;
  document.getElementById('rt-action').value = row.side || 'buy';
  document.getElementById('rt-type').value = row.asset_type || 'warrant';
  document.getElementById('rt-warrant-type').value = row.warrant_type || '';
  document.getElementById('rt-code').value = row.symbol || '';
  document.getElementById('rt-name').value = row.name || '';
  document.getElementById('rt-price').value = row.price || '';
  document.getElementById('rt-qty').value = row.quantity || '';
  document.getElementById('rt-source').value = row.source || 'self';
  document.getElementById('rt-date').value = row.trade_date || new Date().toISOString().slice(0,10);
  document.getElementById('rt-fee').value = row.fee || '';
  document.getElementById('rt-tax').value = row.tax || '';
  document.getElementById('rt-other-cost').value = row.other_cost || '';
  document.getElementById('rt-strategy').value = row.strategy || '';
  document.getElementById('rt-stop-loss').value = row.stop_loss || '';
  document.getElementById('rt-take-profit').value = row.take_profit || '';
  document.getElementById('rt-note').value = row.note || '';
  document.getElementById('rt-submit').textContent = '儲存修改';
  document.getElementById('rt-cancel-edit').style.display = 'inline-block';
  openTradeModal('修改交易');
  document.getElementById('rt-code').focus();
}}
function deleteTradeRecord(id) {{
  trackerSave(trackerLoad().filter(function(r){{return String(r.id) !== String(id);}}));
  if (String(document.getElementById('rt-edit-id').value || '') === String(id)) resetTradeForm();
  renderTracker();
}}
function clearTradeRecords() {{
  if (confirm('確定清空所有報酬追蹤紀錄？')) {{
    trackerSave([]);
    trackerSavePrices({{}});
    renderTracker();
  }}
}}
function updateTradeCurrent(id, value) {{
  updatePositionPrice(id, value);
}}
function updatePositionPrice(key, value) {{
  var current = parseFloat(value);
  if (isNaN(current) || current <= 0) return;
  var prices = trackerLoadPrices();
  prices[key] = current;
  trackerSavePrices(prices);
  renderTracker();
}}
function prefillTrade(key, side, allOut) {{
  var pos = trackerBuildPositions(trackerLoad()).find(function(p){{return p.key === key;}});
  if (!pos) return;
  document.getElementById('rt-edit-id').value = '';
  document.getElementById('rt-action').value = side;
  document.getElementById('rt-type').value = pos.asset_type;
  document.getElementById('rt-warrant-type').value = pos.warrant_type || '';
  document.getElementById('rt-code').value = pos.symbol;
  document.getElementById('rt-name').value = pos.name || '';
  document.getElementById('rt-price').value = pos.current_price || pos.average_cost || '';
  document.getElementById('rt-qty').value = side === 'sell' ? (allOut ? pos.remaining_quantity : '') : '';
  document.getElementById('rt-source').value = pos.source || 'self';
  document.getElementById('rt-date').value = new Date().toISOString().slice(0,10);
  document.getElementById('rt-submit').textContent = '新增紀錄';
  document.getElementById('rt-cancel-edit').style.display = 'none';
  openTradeModal(side === 'sell' ? (allOut ? '全部出場' : '部分賣出') : '加碼買進');
  document.getElementById('rt-code').focus();
}}
function trackerMoney(v) {{
  return Number(v || 0).toLocaleString('zh-TW', {{maximumFractionDigits: 0}});
}}
function trackerPrice(v) {{
  return Number(v || 0).toLocaleString('zh-TW', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
}}
function trackerRenderCashDetail(summaryData, txCount) {{
  var warn = summaryData.principal <= 0 && txCount > 0
    ? '<div style="margin-top:6px;color:#e74c3c">提醒：目前本金為 0，但仍有交易紀錄，請確認是否要設定投入本金。</div>'
    : '';
  return '<div style="background:#fafafa;border:1px solid #eee;border-radius:8px;padding:10px 12px;margin:0 0 12px;font-size:12px;color:#666;line-height:1.7">' +
    '<b style="color:#333">資金明細</b><br>' +
    '可用資金 = 累計投入本金 ' + trackerMoney(summaryData.principal) +
    ' - 累計買進支出 ' + trackerMoney(summaryData.buySpend) +
    ' + 累計賣出收入 ' + trackerMoney(summaryData.sellIncome) +
    ' = <b style="color:'+(summaryData.availableCash>=0?'#27ae60':'#e74c3c')+'">' + trackerMoney(summaryData.availableCash) + '</b>' +
    '<br><span style="color:#999">買進支出含手續費、交易稅與其他成本；賣出收入已扣除手續費、交易稅與其他成本。</span>' +
    warn +
    '</div>';
}}
var trackerPositionFilter = 'all';
var trackerChartRange = 'all';
function setPositionFilter(filter) {{
  trackerPositionFilter = filter || 'all';
  renderTracker();
}}
function setChartRange(range) {{
  trackerChartRange = range || 'all';
  renderTracker();
}}
function trackerPassPositionFilter(p) {{
  if (trackerPositionFilter === 'stock') return p.asset_type === 'stock';
  if (trackerPositionFilter === 'call') return p.asset_type === 'warrant' && p.warrant_type === 'call';
  if (trackerPositionFilter === 'put') return p.asset_type === 'warrant' && p.warrant_type === 'put';
  if (trackerPositionFilter === 'profit') return p.total_pnl > 0;
  if (trackerPositionFilter === 'loss') return p.total_pnl < 0;
  return true;
}}
function trackerMonthKey(d) {{
  return String(d || '').slice(0,7);
}}
function trackerBuildEquityPoints(rows, summaryData) {{
  var byDate = {{}};
  rows.forEach(function(tx) {{
    var d = tx.trade_date || new Date().toISOString().slice(0,10);
    if (!byDate[d]) byDate[d] = 0;
    var gross = Number(tx.price || 0) * Number(tx.quantity || 0);
    var costs = Number(tx.fee || 0) + Number(tx.tax || 0) + Number(tx.other_cost || 0);
    byDate[d] += tx.side === 'sell' ? gross - costs : -(gross + costs);
  }});
  var dates = Object.keys(byDate).sort();
  var principal = summaryData.principal || 0;
  var running = principal;
  var points = dates.map(function(d) {{
    running += byDate[d];
    return {{date:d, value:running}};
  }});
  if (!points.length) points = [{{date:new Date().toISOString().slice(0,10), value:principal}}];
  if (trackerChartRange !== 'all') {{
    var days = Number(trackerChartRange);
    var cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - days);
    points = points.filter(function(p) {{ return new Date(p.date) >= cutoff; }});
    if (!points.length) points = [{{date:new Date().toISOString().slice(0,10), value:running}}];
  }}
  return points;
}}
function trackerSvgLine(points) {{
  var w = 520, h = 190, pad = 22;
  var vals = points.map(function(p){{return p.value;}});
  var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
  if (min === max) {{ min -= 1; max += 1; }}
  var step = points.length > 1 ? (w - pad*2) / (points.length - 1) : 0;
  var coords = points.map(function(p, i) {{
    var x = pad + step * i;
    var y = h - pad - ((p.value - min) / (max - min)) * (h - pad*2);
    return [x, y];
  }});
  var d = coords.map(function(c, i){{return (i?'L':'M')+c[0].toFixed(1)+' '+c[1].toFixed(1);}}).join(' ');
  var area = d + ' L '+(coords[coords.length-1][0]).toFixed(1)+' '+(h-pad)+' L '+pad+' '+(h-pad)+' Z';
  return '<svg class="simple-chart" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><path d="'+area+'" fill="rgba(39,174,96,.08)"></path><path d="'+d+'" fill="none" stroke="#27ae60" stroke-width="3"></path><line x1="'+pad+'" y1="'+(h-pad)+'" x2="'+(w-pad)+'" y2="'+(h-pad)+'" stroke="#eee"/><text x="'+pad+'" y="16" fill="#999" font-size="11">'+trackerMoney(max)+'</text><text x="'+pad+'" y="'+(h-6)+'" fill="#999" font-size="11">'+trackerMoney(min)+'</text></svg>';
}}
function trackerDonut(summaryData) {{
  var parts = [
    ['可用現金', Math.max(0, summaryData.availableCash), '#555'],
    ['持倉市值', Math.max(0, summaryData.marketValue), '#27ae60'],
    ['已實現損益', Math.max(0, summaryData.realized), '#1f7a5c'],
    ['未實現損益', Math.max(0, summaryData.unrealized), '#7f8c8d']
  ];
  var total = parts.reduce(function(s,p){{return s+p[1];}},0) || 1;
  var acc = 0;
  var circles = parts.map(function(p) {{
    var len = p[1] / total * 100;
    var dash = len.toFixed(2)+' '+(100-len).toFixed(2);
    var circle = '<circle r="15.9" cx="20" cy="20" fill="transparent" stroke="'+p[2]+'" stroke-width="8" stroke-dasharray="'+dash+'" stroke-dashoffset="'+(-acc).toFixed(2)+'"></circle>';
    acc += len;
    return circle;
  }}).join('');
  var legend = parts.map(function(p){{return '<div><span style="display:inline-block;width:9px;height:9px;background:'+p[2]+';border-radius:50%;margin-right:6px"></span>'+p[0]+' '+trackerMoney(p[1])+'</div>';}}).join('');
  return '<div class="donut-wrap"><svg width="150" height="150" viewBox="0 0 40 40">'+circles+'<text x="20" y="21" text-anchor="middle" font-size="4" fill="#555">資產</text></svg><div class="donut-legend">'+legend+'</div></div>';
}}
function trackerMonthlyBars(positions) {{
  var months = {{}};
  positions.forEach(function(p) {{
    p.transactions.forEach(function(tx) {{
      if (tx.side === 'sell') {{
        var m = trackerMonthKey(tx.trade_date);
        if (!months[m]) months[m] = 0;
        months[m] += p.realized_pnl;
      }}
    }});
  }});
  var keys = Object.keys(months).sort().slice(-6);
  if (!keys.length) keys = [trackerMonthKey(new Date().toISOString().slice(0,10))];
  var maxAbs = Math.max.apply(null, keys.map(function(k){{return Math.abs(months[k] || 0);}})) || 1;
  var bars = keys.map(function(k) {{
    var v = months[k] || 0;
    var height = Math.max(3, Math.abs(v) / maxAbs * 80);
    var color = v >= 0 ? '#27ae60' : '#e74c3c';
    return '<div style="display:flex;flex-direction:column;align-items:center;gap:5px;justify-content:flex-end;height:120px"><div style="font-size:11px;color:'+color+'">'+(v>0?'+':'')+trackerMoney(v)+'</div><div style="width:28px;height:'+height+'px;background:'+color+';border-radius:5px"></div><div style="font-size:11px;color:#999">'+k.slice(5)+'</div></div>';
  }}).join('');
  return '<div style="display:flex;gap:10px;align-items:flex-end;justify-content:space-around;height:160px">'+bars+'</div>';
}}
function renderTracker() {{
  var rows = trackerLoad();
  var body = document.getElementById('tracker-body');
  var summary = document.getElementById('tracker-summary');
  var cashDetail = document.getElementById('tracker-cash-detail');
  if (!body || !summary) return;
  var budgetInput = document.getElementById('rt-budget');
  if (budgetInput && budgetInput.value === '') budgetInput.value = trackerLoadBudget();
  var positions = trackerBuildPositions(rows);
  var openPositions = positions.filter(function(p){{return p.status === 'open';}});
  var summaryData = trackerCalcSummary(rows, positions);
  var totalAssets = summaryData.availableCash + summaryData.marketValue;
  var pnlColor = summaryData.totalPnl > 0 ? '#27ae60' : summaryData.totalPnl < 0 ? '#e74c3c' : '#555';
  var monthKey = new Date().toISOString().slice(0,7);
  var monthlyPnl = 0;
  positions.forEach(function(p){{ if (p.status === 'closed' && p.transactions.some(function(tx){{return trackerMonthKey(tx.trade_date) === monthKey;}})) monthlyPnl += p.realized_pnl; }});
  if (cashDetail) cashDetail.innerHTML = '<div class="cash-detail-row"><span>已實現 <b style="color:'+(summaryData.realized>=0?'#16a34a':'#dc2626')+'">'+(summaryData.realized>0?'+':'')+trackerMoney(summaryData.realized)+'</b></span><span>未實現 <b style="color:'+(summaryData.unrealized>=0?'#16a34a':'#dc2626')+'">'+(summaryData.unrealized>0?'+':'')+trackerMoney(summaryData.unrealized)+'</b></span><span>可用現金 <b>'+trackerMoney(summaryData.availableCash)+'</b></span></div>';
  var monthColor = monthlyPnl>0?'#16a34a':monthlyPnl<0?'#dc2626':'#64748b';
  var pnlColorNew = summaryData.totalPnl>0?'#16a34a':summaryData.totalPnl<0?'#dc2626':'#64748b';
  summary.innerHTML =
    '<div class="tracker-main-grid">' +
    '<div class="metric-card primary" style="--accent:#3b82f6"><div class="metric-label">總資產</div><div class="metric-value" style="color:#0f172a">'+trackerMoney(totalAssets)+'</div><div class="metric-note">可用現金 + 持倉市值</div></div>' +
    '<div class="metric-card primary" style="--accent:'+pnlColorNew+'"><div class="metric-label">總損益</div><div class="metric-value" style="color:'+pnlColorNew+'">'+(summaryData.totalPnl>0?'+':'')+trackerMoney(summaryData.totalPnl)+'</div><div class="metric-note">已實現 + 未實現</div></div>' +
    '<div class="metric-card primary" style="--accent:'+pnlColorNew+'"><div class="metric-label">總報酬率</div><div class="metric-value" style="color:'+pnlColorNew+'">'+(summaryData.returnPct>0?'+':'')+summaryData.returnPct.toFixed(2)+'%</div><div class="metric-note">以投入本金計算</div></div>' +
    '<div class="metric-card primary" style="--accent:'+monthColor+'"><div class="metric-label">本月損益</div><div class="metric-value" style="color:'+monthColor+'">'+(monthlyPnl>0?'+':'')+trackerMoney(monthlyPnl)+'</div><div class="metric-note">本月已完成交易</div></div>' +
    '</div>';
  var points = trackerBuildEquityPoints(rows, summaryData);
  var tabs = [['7','7天'],['30','30天'],['90','90天'],['all','全部']].map(function(r){{return '<button class="'+(trackerChartRange===r[0]?'active':'')+'" data-tracker-action="chart-range" data-range="'+r[0]+'">'+r[1]+'</button>';}}).join('');
  var chartHtml = '<section class="chart-grid">' +
    '<div class="chart-card"><div class="chart-title">累積績效曲線 <span class="chart-tabs">'+tabs+'</span></div>'+trackerSvgLine(points)+'</div>' +
    '<div class="chart-card"><div class="chart-title">資產結構</div>'+trackerDonut(summaryData)+'</div>' +
    '<div class="chart-card" style="grid-column:1/-1"><div class="chart-title">每月損益</div>'+trackerMonthlyBars(positions)+'</div>' +
    '</section>';
  var holdingHtml = openPositions.length ? '<div class="holding-list">' + openPositions.map(function(p) {{
    var color = p.unrealized_pnl > 0 ? '#16a34a' : p.unrealized_pnl < 0 ? '#dc2626' : '#64748b';
    return '<div class="holding-row"><div><b>'+trackerEscape(p.name || p.symbol)+'</b><div class="holding-cell">'+trackerEscape(p.symbol)+'｜'+trackerWarrantLabel(p.warrant_type)+'</div></div>' +
      '<div class="holding-cell">平均成本<strong>'+trackerPrice(p.average_cost)+'</strong></div>' +
      '<div class="holding-cell">現價<strong>'+trackerPrice(p.current_price)+'</strong></div>' +
      '<div class="holding-cell">未實現損益<strong style="color:'+color+'">'+(p.unrealized_pnl>0?'+':'')+trackerMoney(p.unrealized_pnl)+'</strong></div>' +
      '<div class="holding-cell">報酬率<strong style="color:'+color+'">'+(p.unrealized_return_pct>0?'+':'')+p.unrealized_return_pct.toFixed(2)+'%</strong></div></div>';
  }}).join('') + '</div>' : '<p style="color:#94a3b8;padding:10px 0">目前沒有持倉。</p>';
  var sortedRows = rows.slice().sort(function(a,b){{return String(b.trade_date||'').localeCompare(String(a.trade_date||'')) || String(b.created_at||'').localeCompare(String(a.created_at||''));}});
  var recentRows = sortedRows.slice(0,5).map(function(tx) {{
    var gross = Number(tx.price || 0) * Number(tx.quantity || 0);
    var sideColor = tx.side === 'sell' ? '#ea580c' : '#1d4ed8';
    return '<div class="recent-row"><div style="color:#94a3b8">'+trackerEscape(tx.trade_date)+'</div><div><b>'+trackerEscape(tx.name || tx.symbol)+'</b><div style="font-size:11px;color:#94a3b8">'+trackerEscape(tx.symbol)+'｜預設帳戶</div></div><div style="color:'+sideColor+';font-weight:800">'+(tx.side === 'sell' ? '賣出' : '買進')+'</div><div style="text-align:right">'+trackerPrice(tx.price)+' × '+trackerMoney(tx.quantity)+'<br><span style="color:#94a3b8">'+trackerMoney(gross)+'</span></div></div>';
  }}).join('');
  var allRows = sortedRows.slice(5).map(function(tx) {{
    var gross = Number(tx.price || 0) * Number(tx.quantity || 0);
    var sideColor = tx.side === 'sell' ? '#ea580c' : '#1d4ed8';
    return '<div class="recent-row"><div style="color:#94a3b8">'+trackerEscape(tx.trade_date)+'</div><div><b>'+trackerEscape(tx.name || tx.symbol)+'</b><div style="font-size:11px;color:#94a3b8">'+trackerEscape(tx.symbol)+'｜預設帳戶</div></div><div style="color:'+sideColor+';font-weight:800">'+(tx.side === 'sell' ? '賣出' : '買進')+'</div><div style="text-align:right">'+trackerPrice(tx.price)+' × '+trackerMoney(tx.quantity)+'<br><span style="color:#94a3b8">'+trackerMoney(gross)+'</span></div></div>';
  }}).join('');
  var recentHtml = rows.length ? '<div class="recent-list">'+recentRows+'</div>' + (rows.length>5 ? '<div id="ledger-extra" class="ledger-extra recent-list" style="margin-top:6px">'+allRows+'</div><button id="ledger-toggle" class="tracker-secondary" data-tracker-action="toggle-ledger" style="margin-top:8px">查看全部</button>' : '') : '<p style="color:#94a3b8;padding:10px 0">尚無交易紀錄。</p>';
  body.innerHTML = '<div class="returns-dashboard">' + chartHtml +
    '<section><div class="section-label">目前持倉摘要</div>'+holdingHtml+'</section>' +
    '<section><div class="section-label">最近交易</div>'+recentHtml+'</section>' +
    '</div>';
}}
document.addEventListener('DOMContentLoaded', function() {{
  showTab(location.hash === '#warrants' ? 'warrants' : location.hash === '#returns' ? 'returns' : 'stocks');
  var d = document.getElementById('rt-date');
  if (d && !d.value) d.value = new Date().toISOString().slice(0,10);
  var b = document.getElementById('rt-budget');
  if (b && !b.value) b.value = trackerLoadBudget();
  renderTracker();
}});
</script>
</head>
<body>
<div class="wrap">

<!-- ── 標題 ── -->
<div class="hdr">
  <h1>台股權證篩選報表</h1>
  <div class="sub">
    執行時間：{now_str} ／ 強勢股 {len(strong_stocks)} 支 ／ 弱勢股 {len(weak_stocks)} 支
    ／ 全市場掃描 {total_all} 支候選
  </div>
</div>

<!-- ── A. 今日市場總覽 ── -->
<div class="card">
  <div class="card-title">今日市場總覽
    <span style="font-size:11px;font-weight:normal;color:#aaa">僅供觀察，不構成投資建議</span>
  </div>
  <div class="stat-grid">
    <div class="stat-box">
      <div class="stat-val" style="color:#27ae60">{len(formal_calls)}</div>
      <div class="stat-lbl">正式認購候選</div>
    </div>
    <div class="stat-box">
      <div class="stat-val" style="color:#e74c3c">{len(formal_puts)}</div>
      <div class="stat-lbl">正式認售候選</div>
    </div>
    <div class="stat-box">
      <div class="stat-val" style="color:#f39c12">{high_score}</div>
      <div class="stat-lbl">高分（≥75）</div>
    </div>
    <div class="stat-box">
      <div class="stat-val" style="color:#aaa">{len(insufficient)}</div>
      <div class="stat-lbl">資料不足</div>
    </div>
    <div class="stat-box">
      <div class="stat-val" style="color:#ccc">{len(high_risk)}</div>
      <div class="stat-lbl">高風險排除</div>
    </div>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" data-tab="stocks" onclick="showTab('stocks');location.hash='stocks'">股票雷達</button>
  <button class="tab-btn" data-tab="warrants" onclick="showTab('warrants');location.hash='warrants'">權證篩選</button>
  <button class="tab-btn" data-tab="returns" onclick="showTab('returns');location.hash='returns'">報酬追蹤</button>
</div>

<div id="tab-stocks" class="tab-panel active">
<!-- ── B. 台股個股雷達 ── -->
{stock_radar_html}
</div>

<div id="tab-returns" class="tab-panel">
<div class="card">
  <div class="tracker-top">
    <div class="tracker-title">
      <h2>報酬追蹤</h2>
      <div class="tracker-sub">實盤紀錄｜資料只存在此瀏覽器，不會上傳</div>
    </div>
    <div class="tracker-toolbar">
      <label style="font-size:12px;color:#777">帳戶：
        <select id="rt-account-filter">
          <option value="all">全部帳戶</option>
          <option value="default">預設帳戶</option>
        </select>
      </label>
      <button class="tracker-primary" onclick="openNewTrade()">＋ 新增交易</button>
      <button class="tracker-secondary" onclick="openCapitalSettings()">資金設定</button>
    </div>
  </div>
  <details id="capital-settings" style="margin:14px 0 10px">
    <summary style="font-size:12px;color:#888">資金設定</summary>
    <div class="tracker-grid" style="margin-top:10px">
      <label>累計投入本金
        <input id="rt-budget" type="number" step="1000" value="10000" oninput="trackerBudgetChanged()" onchange="trackerSaveBudget()" placeholder="例如 10000">
      </label>
      <label>&nbsp;
        <button type="button" onclick="trackerSaveBudget()" style="width:100%;border:none;background:#16213e;color:#fff;border-radius:8px;padding:10px;cursor:pointer">更新本金</button>
      </label>
    </div>
  </details>
  <h3 style="font-size:16px;margin:10px 0 8px">績效總覽</h3>
  <div id="tracker-summary" style="margin-bottom:12px"></div>
  <div id="tracker-cash-detail"></div>
  <div id="tracker-body" style="margin-top:12px"></div>
  <details style="margin-top:14px">
    <summary style="font-size:12px;color:#999">資料管理</summary>
    <button onclick="clearTradeRecords()" class="tracker-danger" style="margin-top:8px;border-radius:8px;padding:8px 10px;cursor:pointer">清空所有交易紀錄</button>
  </details>
</div>

<div id="trade-modal" class="returns-modal" onclick="if(event.target.id==='trade-modal') closeTradeModal()">
  <div class="returns-dialog">
    <div class="returns-dialog-head">
      <div>
        <h3 id="trade-modal-title" style="font-size:18px">新增交易</h3>
        <div style="font-size:12px;color:#888;margin-top:3px">新增或修改交易後，持倉與績效會自動重算。</div>
      </div>
      <button class="returns-close" onclick="closeTradeModal()">關閉</button>
    </div>
    <input id="rt-edit-id" type="hidden">
    <div class="tracker-grid">
      <label>帳戶
        <select id="rt-account"><option value="default">預設帳戶</option></select>
      </label>
      <label>商品類型
        <select id="rt-type">
          <option value="stock">股票</option>
          <option value="warrant">權證</option>
        </select>
      </label>
      <label>權證類型
        <select id="rt-warrant-type">
          <option value="">未分類</option>
          <option value="call">認購</option>
          <option value="put">認售</option>
        </select>
      </label>
      <label>交易方向
        <select id="rt-action">
          <option value="buy">買進</option>
          <option value="sell">賣出</option>
        </select>
      </label>
      <label>代號
        <input id="rt-code" placeholder="例如 2330 / 056772">
      </label>
      <label>名稱
        <input id="rt-name" placeholder="可不填">
      </label>
      <label>成交價格
        <input id="rt-price" type="number" step="0.01" placeholder="例如 1.25">
      </label>
      <label>數量
        <input id="rt-qty" type="number" step="1" placeholder="股數 / 張數">
      </label>
      <label>交易日期
        <input id="rt-date" type="date">
      </label>
    </div>
    <details class="advanced-box">
      <summary style="font-size:13px;color:#1f5f99;font-weight:700">展開進階設定</summary>
      <div class="tracker-grid" style="margin-top:10px">
        <label>交易依據
          <select id="rt-source">
            <option value="self">自行判斷</option>
            <option value="stock_radar">股票雷達</option>
            <option value="screener">權證篩選</option>
            <option value="event">事件交易</option>
            <option value="recommendation">推薦</option>
            <option value="other">其他</option>
          </select>
        </label>
        <label>策略
          <input id="rt-strategy" placeholder="例如：短打 / 波段 / 測試單">
        </label>
        <label>手續費
          <input id="rt-fee" type="number" step="1" placeholder="可填 0">
        </label>
        <label>交易稅
          <input id="rt-tax" type="number" step="1" placeholder="可填 0">
        </label>
        <label>其他成本
          <input id="rt-other-cost" type="number" step="1" placeholder="可填 0">
        </label>
        <label>預設停損價
          <input id="rt-stop-loss" type="number" step="0.01" placeholder="可不填">
        </label>
        <label>預設停利價
          <input id="rt-take-profit" type="number" step="0.01" placeholder="可不填">
        </label>
        <label>備註
          <input id="rt-note" placeholder="例如：觀察出場難度">
        </label>
      </div>
    </details>
    <div class="tracker-actions">
      <button id="rt-submit" onclick="if(addTradeRecord()) closeTradeModal();" style="background:#16213e;color:#fff">儲存交易</button>
      <button id="rt-cancel-edit" onclick="resetTradeForm();closeTradeModal()" style="display:none;background:#f4f4f4;color:#777">取消修改</button>
    </div>
  </div>
</div>
</div>

<div id="tab-warrants" class="tab-panel">
<!-- ── C. 今日最值得觀察 ── -->
<div class="card">
  <div class="card-title">
    今日最值得觀察 Top 10
    <span class="badge" style="background:#1f7a5c">實戰優先排序</span>
    <span style="font-size:11px;font-weight:normal;color:#aaa">三組各顯示最多 5 檔，先看出場容易與不追高</span>
  </div>
  {watch_html}
</div>

<!-- ── D. 強勢股 ── -->
<div class="card">
  <div class="card-title">
    強勢標的股 Top {min(len(strong_stocks),20)}
    <span class="badge" style="background:#27ae60">認購用</span>
    <span style="font-size:12px;font-weight:normal;color:#aaa">門檻 {STRONG_STOCK_MIN_SCORE} 分</span>
  </div>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8f8f8;color:#888;font-size:12px">
          <th style="padding:8px">代號</th><th style="padding:8px">現價</th>
          <th style="padding:8px">漲跌</th><th style="padding:8px">RSI</th>
          <th style="padding:8px">量比</th><th style="padding:8px">均線</th>
          <th style="padding:8px">強勢分</th>
        </tr>
      </thead>
      <tbody>{strong_rows if strong_rows else "<tr><td colspan=7 style='padding:12px;color:#aaa'>今日無強勢股</td></tr>"}</tbody>
    </table>
  </div>
</div>

<!-- ── E. 弱勢股 ── -->
<div class="card">
  <div class="card-title">
    弱勢標的股 Top {min(len(weak_stocks),20)}
    <span class="badge" style="background:#e74c3c">認售用</span>
    <span style="font-size:12px;font-weight:normal;color:#aaa">門檻 {WEAK_STOCK_MIN_SCORE} 分</span>
  </div>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8f8f8;color:#888;font-size:12px">
          <th style="padding:8px">代號</th><th style="padding:8px">現價</th>
          <th style="padding:8px">漲跌</th><th style="padding:8px">RSI</th>
          <th style="padding:8px">量比</th><th style="padding:8px">均線</th>
          <th style="padding:8px">空頭分</th>
        </tr>
      </thead>
      <tbody>{weak_rows if weak_rows else "<tr><td colspan=7 style='padding:12px;color:#aaa'>今日無弱勢股</td></tr>"}</tbody>
    </table>
  </div>
</div>

<!-- ── F. 正式認購候選 ── -->
<div class="card">
  <div class="card-title">
    正式認購候選（Call）
    <span class="badge" style="background:#27ae60">{len(formal_calls)} 支</span>
    <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:4px">
      強勢股標的 · 通過資格審查 · 依實戰優先分數排序 · 僅供觀察，不構成投資建議</span>
  </div>
  {calls_html}
</div>

<!-- ── G. 正式認售候選 ── -->
<div class="card">
  <div class="card-title">
    正式認售候選（Put）
    <span class="badge" style="background:#e74c3c">{len(formal_puts)} 支</span>
    <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:4px">
      弱勢股標的 · 通過資格審查 · 依實戰優先分數排序 · 僅供觀察，不構成投資建議</span>
  </div>
  {puts_html}
</div>

<!-- ── H. 高風險排除統計 ── -->
<div class="card">
  <div class="card-title">
    高風險排除原因統計
    <span style="font-size:11px;font-weight:normal;color:#aaa">用來判斷今天權證市場是否好做</span>
  </div>
  {risk_stats_html}
  <details>
    <summary style="font-size:13px;font-weight:700;padding:14px 0 4px;color:#e67e22">
      資料不足清單（{len(insufficient)} 支）
      <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:6px">
        缺少履約價、買賣價等核心欄位，僅供參考</span>
    </summary>
    <div style="margin-top:12px;overflow-x:auto">
      {insuf_block}
    </div>
  </details>
</div>

<!-- ── I. 高風險排除清單 ── -->
<div class="card">
  <details>
    <summary style="font-size:15px;font-weight:700;padding:4px 0;color:#e74c3c">
      已排除高風險清單（{len(high_risk)} 支）
      <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:6px">
        有紅色警示，建議避開</span>
    </summary>
    <div style="margin-top:12px;overflow-x:auto">
      {risk_block}
    </div>
  </details>
</div>

<!-- ── Footer ── -->
<div style="text-align:center;color:#bbb;font-size:11px;margin-top:4px;padding-bottom:24px">
  本系統僅供觀察與研究，不構成投資建議，不提供自動下單，也不保證獲利 · 資料來源：TWSE / TPEX / Fubon Neo<br>
  自動產生 · {now_str}
</div>

</div>
</div>
</body>
</html>'''


# ─── 排程 ────────────────────────────────────────────────

def main():
    print('權證篩選系統 v2 啟動')
    print('排程：每天 08:00 執行（TWSE + TPEX 全市場）')
    print('按 Ctrl+C 停止\n')

    run_screening()

    schedule.every().day.at('08:00').do(run_screening)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == '__main__':
    main()
