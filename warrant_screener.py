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
     background:#f0ede8;color:#333;padding:16px}}
.wrap{{max-width:980px;margin:0 auto}}
.hdr{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;
     border-radius:14px;padding:20px 24px;margin-bottom:14px}}
.hdr h1{{font-size:20px;font-weight:700;letter-spacing:.5px}}
.hdr .sub{{font-size:12px;color:#aaa;margin-top:6px}}
.card{{background:#fff;border-radius:14px;padding:18px 20px;margin-bottom:14px;
      box-shadow:0 1px 8px rgba(0,0,0,.07)}}
.card-title{{font-size:16px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:8px}}
.badge{{font-size:11px;font-weight:normal;padding:2px 8px;border-radius:20px;color:#fff}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}}
.stat-box{{background:#f8f7f4;border-radius:10px;padding:14px;text-align:center}}
.stat-val{{font-size:26px;font-weight:700}}
.stat-lbl{{font-size:11px;color:#aaa;margin-top:3px}}
.tabs{{display:flex;gap:8px;margin:0 0 14px;position:sticky;top:8px;z-index:5;
      background:rgba(240,237,232,.92);backdrop-filter:blur(8px);padding:6px 0}}
.tab-btn{{flex:1;border:1px solid #ddd;background:#fff;color:#555;border-radius:10px;
         padding:10px 12px;font-size:14px;font-weight:700;cursor:pointer}}
.tab-btn.active{{background:#16213e;color:#fff;border-color:#16213e}}
.tab-panel{{display:none}}
.tab-panel.active{{display:block}}
.tracker-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.tracker-grid label{{font-size:11px;color:#777;font-weight:700}}
.tracker-grid input,.tracker-grid select{{width:100%;margin-top:4px;border:1px solid #ddd;border-radius:8px;
      padding:9px 10px;font-size:13px;background:#fff}}
.tracker-actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}
.tracker-actions button{{border:none;border-radius:8px;padding:9px 12px;font-size:13px;font-weight:700;cursor:pointer}}
.profit-bar{{height:8px;background:#eee;border-radius:99px;overflow:hidden;margin-top:5px}}
.profit-fill{{height:8px;border-radius:99px;transition:width .2s ease}}
details summary{{cursor:pointer;list-style:none;user-select:none}}
details summary::-webkit-details-marker{{display:none}}
details summary::before{{content:"▶ ";font-size:10px;color:#aaa}}
details[open] summary::before{{content:"▼ ";}}
@media(max-width:620px){{
  body{{padding:8px}}
  .stat-val{{font-size:20px}}
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
function trackerLoad() {{
  try {{ return JSON.parse(localStorage.getItem('twReturnTracker') || '[]'); }}
  catch(e) {{ return []; }}
}}
function trackerSave(rows) {{
  localStorage.setItem('twReturnTracker', JSON.stringify(rows));
}}
function trackerLoadBudget() {{
  var v = parseFloat(localStorage.getItem('twReturnBudget') || '10000');
  return isNaN(v) || v < 0 ? 10000 : v;
}}
function trackerSaveBudget() {{
  var el = document.getElementById('rt-budget');
  var v = el ? parseFloat(el.value) : trackerLoadBudget();
  if (isNaN(v) || v < 0) v = 0;
  localStorage.setItem('twReturnBudget', String(v));
  renderTracker();
}}
function trackerNum(id) {{
  var v = parseFloat(document.getElementById(id).value);
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
  ['rt-code','rt-name','rt-buy','rt-current','rt-qty','rt-note'].forEach(function(id){{document.getElementById(id).value='';}});
  document.getElementById('rt-edit-id').value = '';
  document.getElementById('rt-submit').textContent = '新增紀錄';
  document.getElementById('rt-cancel-edit').style.display = 'none';
  var d = document.getElementById('rt-date');
  if (d) d.value = new Date().toISOString().slice(0,10);
}}
function addTradeRecord() {{
  var code = trackerText('rt-code');
  var buy = trackerNum('rt-buy');
  var current = trackerNum('rt-current');
  var qty = trackerNum('rt-qty');
  if (!code || buy <= 0 || current <= 0 || qty <= 0) {{
    alert('請至少填寫代號、買入價、目前價與數量');
    return;
  }}
  var rows = trackerLoad();
  var editId = parseInt(document.getElementById('rt-edit-id').value || '0', 10);
  var record = {{
    id: editId || Date.now(),
    type: document.getElementById('rt-type').value,
    code: code,
    name: trackerText('rt-name'),
    buy: buy,
    current: current,
    qty: qty,
    source: document.getElementById('rt-source').value,
    note: trackerText('rt-note'),
    date: document.getElementById('rt-date').value || new Date().toISOString().slice(0,10)
  }};
  if (editId) {{
    var updated = false;
    rows = rows.map(function(r){{ if (r.id === editId) {{ updated = true; return record; }} return r; }});
    if (!updated) rows.push(record);
  }} else {{
    rows.push(record);
  }}
  trackerSave(rows);
  resetTradeForm();
  renderTracker();
}}
function editTradeRecord(id) {{
  var row = trackerLoad().find(function(r){{return r.id === id;}});
  if (!row) return;
  document.getElementById('rt-edit-id').value = row.id;
  document.getElementById('rt-type').value = row.type || '權證';
  document.getElementById('rt-code').value = row.code || '';
  document.getElementById('rt-name').value = row.name || '';
  document.getElementById('rt-buy').value = row.buy || '';
  document.getElementById('rt-current').value = row.current || '';
  document.getElementById('rt-qty').value = row.qty || '';
  document.getElementById('rt-source').value = row.source || '自行判斷';
  document.getElementById('rt-date').value = row.date || new Date().toISOString().slice(0,10);
  document.getElementById('rt-note').value = row.note || '';
  document.getElementById('rt-submit').textContent = '儲存修改';
  document.getElementById('rt-cancel-edit').style.display = 'inline-block';
  document.getElementById('rt-code').focus();
}}
function deleteTradeRecord(id) {{
  trackerSave(trackerLoad().filter(function(r){{return r.id !== id;}}));
  if (parseInt(document.getElementById('rt-edit-id').value || '0', 10) === id) resetTradeForm();
  renderTracker();
}}
function clearTradeRecords() {{
  if (confirm('確定清空所有報酬追蹤紀錄？')) {{
    trackerSave([]);
    renderTracker();
  }}
}}
function updateTradeCurrent(id, value) {{
  var current = parseFloat(value);
  if (isNaN(current) || current <= 0) return;
  var rows = trackerLoad();
  rows.forEach(function(r){{ if (r.id === id) r.current = current; }});
  trackerSave(rows);
  renderTracker();
}}
function renderTracker() {{
  var rows = trackerLoad();
  var body = document.getElementById('tracker-body');
  var summary = document.getElementById('tracker-summary');
  if (!body || !summary) return;
  var totalCost = 0, totalValue = 0, wins = 0;
  var budget = trackerLoadBudget();
  var budgetInput = document.getElementById('rt-budget');
  if (budgetInput && budgetInput.value === '') budgetInput.value = budget;
  rows.forEach(function(r) {{
    totalCost += r.buy * r.qty;
    totalValue += r.current * r.qty;
    if (r.current >= r.buy) wins += 1;
  }});
  var pnl = totalValue - totalCost;
  var roi = totalCost > 0 ? pnl / totalCost * 100 : 0;
  var remaining = budget - totalCost;
  var usage = budget > 0 ? totalCost / budget * 100 : 0;
  summary.innerHTML =
    '<div class="stat-box"><div class="stat-val" style="color:#555">'+budget.toFixed(0)+'</div><div class="stat-lbl">預計投入</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:#555">'+totalCost.toFixed(0)+'</div><div class="stat-lbl">目前總成本</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:'+(remaining>=0?'#555':'#e74c3c')+'">'+remaining.toFixed(0)+'</div><div class="stat-lbl">剩餘資金</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:'+(pnl>=0?'#27ae60':'#e74c3c')+'">'+(pnl>=0?'+':'')+pnl.toFixed(0)+'</div><div class="stat-lbl">總損益</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:'+(roi>=0?'#27ae60':'#e74c3c')+'">'+(roi>=0?'+':'')+roi.toFixed(2)+'%</div><div class="stat-lbl">總報酬率</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:'+(usage>100?'#e74c3c':'#555')+'">'+usage.toFixed(0)+'%</div><div class="stat-lbl">資金使用率</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:#555">'+rows.length+'</div><div class="stat-lbl">紀錄筆數</div></div>' +
    '<div class="stat-box"><div class="stat-val" style="color:#555">'+(rows.length ? Math.round(wins/rows.length*100) : 0)+'%</div><div class="stat-lbl">勝率</div></div>';
  if (!rows.length) {{
    body.innerHTML = '<p style="color:#aaa;padding:10px 0">尚未新增紀錄。可以先用少量資金試單，再把買入價與目前價填進來追蹤實際報酬。</p>';
    return;
  }}
  body.innerHTML = rows.map(function(r) {{
    var cost = r.buy * r.qty;
    var value = r.current * r.qty;
    var p = value - cost;
    var rr = cost > 0 ? p / cost * 100 : 0;
    var color = p >= 0 ? '#27ae60' : '#e74c3c';
    var icon = p >= 0 ? '▲' : '▼';
    var width = Math.min(100, Math.max(4, Math.abs(rr) * 3));
    var code = trackerEscape(r.code);
    var name = trackerEscape(r.name || '');
    var type = trackerEscape(r.type || '');
    var source = trackerEscape(r.source || '');
    var date = trackerEscape(r.date || '');
    var note = trackerEscape(r.note || '');
    return '<div style="border-top:1px solid #f0f0f0;padding:12px 0">' +
      '<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap">' +
      '<div><b>'+code+' '+name+'</b> <span style="color:#777;font-size:12px">｜'+type+'｜'+source+'｜'+date+'</span><br>' +
      '<span style="font-size:12px;color:#777">買入 '+r.buy+'｜目前 <input value="'+r.current+'" onchange="updateTradeCurrent('+r.id+', this.value)" style="width:76px;border:1px solid #ddd;border-radius:6px;padding:3px 6px">｜數量 '+r.qty+'</span></div>' +
      '<div style="text-align:right;color:'+color+';font-weight:700;font-size:18px">'+icon+' '+(rr>=0?'+':'')+rr.toFixed(2)+'%<br><span style="font-size:12px">'+(p>=0?'+':'')+p.toFixed(0)+' 元</span></div>' +
      '</div><div class="profit-bar"><div class="profit-fill" style="width:'+width+'%;background:'+color+'"></div></div>' +
      (note ? '<div style="font-size:12px;color:#777;margin-top:5px">備註：'+note+'</div>' : '') +
      '<button onclick="editTradeRecord('+r.id+')" style="margin-top:7px;margin-right:6px;border:none;background:#16213e;color:#fff;border-radius:6px;padding:4px 8px;cursor:pointer">修改</button>' +
      '<button onclick="deleteTradeRecord('+r.id+')" style="margin-top:7px;border:none;background:#f4f4f4;color:#777;border-radius:6px;padding:4px 8px;cursor:pointer">刪除</button>' +
      '</div>';
  }}).join('');
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
  <div class="card-title">
    報酬追蹤
    <span class="badge" style="background:#1f5f99">手動紀錄</span>
    <span style="font-size:11px;font-weight:normal;color:#aaa">資料只存在你的瀏覽器，不會上傳</span>
  </div>
  <div style="font-size:12px;color:#777;line-height:1.6;margin-bottom:12px">
    可填寫你實際買入的股票或權證，追蹤少量資金試單的實際報酬。買入來源可選「自行判斷」、「網頁評分篩選」、「推薦」。
  </div>
  <div class="tracker-grid" style="margin-bottom:10px">
    <label>預計投入資金
      <input id="rt-budget" type="number" step="1000" value="10000" onchange="trackerSaveBudget()" placeholder="例如 10000">
    </label>
  </div>
  <div id="tracker-summary" class="stat-grid" style="margin-bottom:12px"></div>
  <input id="rt-edit-id" type="hidden">
  <div class="tracker-grid">
    <label>類型
      <select id="rt-type">
        <option>權證</option>
        <option>股票</option>
      </select>
    </label>
    <label>代號
      <input id="rt-code" placeholder="例如 2330 / 056772">
    </label>
    <label>名稱
      <input id="rt-name" placeholder="可不填">
    </label>
    <label>買入價
      <input id="rt-buy" type="number" step="0.01" placeholder="例如 1.25">
    </label>
    <label>目前價
      <input id="rt-current" type="number" step="0.01" placeholder="例如 1.38">
    </label>
    <label>數量
      <input id="rt-qty" type="number" step="1" placeholder="股數 / 張數">
    </label>
    <label>買入來源
      <select id="rt-source">
        <option>自行判斷</option>
        <option>網頁評分篩選</option>
        <option>推薦</option>
      </select>
    </label>
    <label>買入日期
      <input id="rt-date" type="date">
    </label>
  </div>
  <div class="tracker-grid" style="margin-top:10px">
    <label>備註
      <input id="rt-note" placeholder="例如：小資金試單、觀察出場難度">
    </label>
  </div>
  <div class="tracker-actions">
    <button id="rt-submit" onclick="addTradeRecord()" style="background:#16213e;color:#fff">新增紀錄</button>
    <button id="rt-cancel-edit" onclick="resetTradeForm()" style="display:none;background:#f4f4f4;color:#777">取消修改</button>
    <button onclick="clearTradeRecords()" style="background:#f4f4f4;color:#777">清空紀錄</button>
  </div>
  <div id="tracker-body" style="margin-top:12px"></div>
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
