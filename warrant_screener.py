"""
warrant_screener.py — 主程式 v2
每天 08:00 執行，產出 index.html 並 push GitHub Pages
執行方式：python3 warrant_screener.py
"""

import html as html_mod
import json
import os
import time
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

try:
    from warrant_config import ID_NUMBER, PASSWORD, CERT_PATH, CERT_PASS
except ImportError:
    ID_NUMBER = PASSWORD = CERT_PATH = CERT_PASS = ""
    print("[!] 找不到 warrant_config.py，Fubon SDK 將無法登入，改用 yfinance")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'index.html')

STRONG_STOCK_MIN_SCORE = 58   # 認購標的門檻
WEAK_STOCK_MIN_SCORE   = 55   # 認售標的門檻


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

    formal_calls.sort(key=lambda x: -x['score'])
    formal_puts.sort(key=lambda x:  -x['score'])
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
        f"評分: {w.get('score',0)}分  完整度: {w.get('completeness_pct',0)}%",
        f"IV: {iv_s}  槓桿: {lev_s}  價性: {m_s}",
        f"行使比例: {w.get('ratio',1):.2f}  量: {w.get('volume',0)}張  價差: {w.get('spread_pct',0):.1f}%",
    ]
    if sel:   lines.append(f"入選: {sel}")
    if ded:   lines.append(f"注意: {ded}")
    if excl:  lines.append(f"排除: {excl}")
    if flags: lines.append(f"風險: {flags}")
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
        <span style="color:{stk_c}">@ {w.get('stock_price',0):.2f} ({stk_chg:+.2f}%)</span></div>
      <div style="margin-top:4px">{flags_html}</div>
      {sel_html}{ded_html}
    </td>
    <td style="padding:10px 8px;text-align:center;white-space:nowrap">
      <div style="font-size:26px;font-weight:700;color:{sc_c}">{score}</div>
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

<!-- ── 統計 ── -->
<div class="card">
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

<!-- ── 強勢股 ── -->
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

<!-- ── 弱勢股 ── -->
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

<!-- ── 正式認購候選 ── -->
<div class="card">
  <div class="card-title">
    正式認購候選（Call）
    <span class="badge" style="background:#27ae60">{len(formal_calls)} 支</span>
    <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:4px">
      強勢股標的 · 核心欄位完整 · 無紅色警示 · 量≥10張</span>
  </div>
  {calls_html}
</div>

<!-- ── 正式認售候選 ── -->
<div class="card">
  <div class="card-title">
    正式認售候選（Put）
    <span class="badge" style="background:#e74c3c">{len(formal_puts)} 支</span>
    <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:4px">
      弱勢股標的 · 核心欄位完整 · 無紅色警示 · 量≥10張</span>
  </div>
  {puts_html}
</div>

<!-- ── 資料不足 ── -->
<div class="card">
  <details>
    <summary style="font-size:15px;font-weight:700;padding:4px 0;color:#e67e22">
      資料不足清單（{len(insufficient)} 支）
      <span style="font-size:11px;font-weight:normal;color:#aaa;margin-left:6px">
        缺少履約價、買賣價等核心欄位，僅供參考</span>
    </summary>
    <div style="margin-top:12px;overflow-x:auto">
      {insuf_block}
    </div>
  </details>
</div>

<!-- ── 高風險排除 ── -->
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
  本報表僅供觀察參考，不構成投資建議 · 資料來源：TWSE / TPEX / Fubon Neo<br>
  自動產生 · {now_str}
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
