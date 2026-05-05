"""
warrant_screener.py — 主程式
每天 08:00 執行，產出 warrant_report.html
執行方式：python3 warrant_screener.py
"""

import json
import os
import time
from datetime import datetime, date

import schedule

from warrant_fetcher import (
    fetch_all_warrants,
    batch_fetch_stock_histories,
)
from warrant_scorer import (
    score_stock,
    score_warrant,
    get_risk_flags,
    has_red_flag,
)

# ─── 憑證（從 warrant_config.py 讀取，不進版本控制）────────
try:
    from warrant_config import ID_NUMBER, PASSWORD, CERT_PATH, CERT_PASS
except ImportError:
    ID_NUMBER = PASSWORD = CERT_PATH = CERT_PASS = ""
    print("[!] 找不到 warrant_config.py，Fubon SDK 將無法登入，改用 yfinance")

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), 'index.html')

STRONG_STOCK_MIN_SCORE = 58  # 強勢股最低分門檻


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

    # 1. 抓全市場權證
    print('\n[1/5] 抓取全市場權證清單...')
    all_warrants = fetch_all_warrants()
    if not all_warrants:
        print('[!] 無法取得權證資料，結束')
        return

    # 2. 提取不重複標的股
    print('\n[2/5] 分析標的股清單...')
    underlying_set = {
        w['underlying'] for w in all_warrants.values()
        if w.get('underlying') and w.get('days_left', 0) >= 15
    }
    print(f'    共 {len(underlying_set)} 支不重複標的股')

    # 3. 批次抓取標的股歷史資料
    print('\n[3/5] 抓取個股歷史行情（這步驟需要幾分鐘）...')
    histories = batch_fetch_stock_histories(sdk, sorted(underlying_set), days=28)
    print(f'    成功取得 {len(histories)} 支股票歷史資料')

    # 4. 計算技術指標 + 篩選強勢股
    print('\n[4/5] 計算技術指標、篩選強勢股...')
    stock_scores    = {}   # {symbol: score}
    stock_indicators = {}  # {symbol: indicators dict}

    for sym, candles in histories.items():
        s, ind = score_stock(candles)
        stock_scores[sym]     = s
        stock_indicators[sym] = ind

    strong_stocks = {
        sym: stock_scores[sym]
        for sym in stock_scores
        if stock_scores[sym] >= STRONG_STOCK_MIN_SCORE
    }
    strong_stocks = dict(sorted(strong_stocks.items(), key=lambda x: -x[1]))
    print(f'    強勢股：{len(strong_stocks)} 支（門檻 {STRONG_STOCK_MIN_SCORE} 分）')

    # 5. 篩選強勢股對應的權證 + 計算評分
    print('\n[5/5] 計算權證評分...')
    calls, puts = [], []

    for code, w in all_warrants.items():
        udly = w.get('underlying', '')
        if udly not in strong_stocks:
            continue
        if w.get('close', 0) <= 0:
            continue

        s_score = strong_stocks.get(udly, 0)
        s_ind   = stock_indicators.get(udly, {})

        score, enriched = score_warrant(w, s_score, s_ind)
        flags  = get_risk_flags(enriched)
        enriched['risk_flags']    = flags
        enriched['has_red_flag']  = has_red_flag(flags)
        enriched['stock_score']   = s_score
        enriched['stock_name']    = ''  # placeholder

        if enriched['type'] == 'call':
            calls.append(enriched)
        else:
            puts.append(enriched)

    calls.sort(key=lambda x: -x['score'])
    puts.sort(key=lambda x: -x['score'])
    print(f'    認購候選：{len(calls)} 支  認售候選：{len(puts)} 支')

    # 6. 產出 HTML
    elapsed = (datetime.now() - start_time).seconds
    print(f'\n[完成] 耗時 {elapsed}s，寫出報表...')
    html = generate_html(strong_stocks, stock_indicators, calls, puts, start_time)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'[報表] {OUTPUT_PATH}')
    print('='*55)

    auto_push_report(start_time)
    return calls, puts


def auto_push_report(run_time):
    """產完報表後自動 git commit + push，更新 GitHub Pages"""
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


# ─── HTML 報表產生 ────────────────────────────────────────

def _score_color(score):
    if score >= 75:   return '#27ae60'
    if score >= 60:   return '#e67e22'
    return '#e74c3c'


def _flag_html(flags):
    if not flags:
        return ''
    colors = {'red': '#e74c3c', 'orange': '#e67e22', 'yellow': '#f39c12'}
    parts = []
    for f in flags:
        c = colors.get(f['color'], '#999')
        parts.append(f'<span style="background:{c};color:#fff;border-radius:4px;'
                     f'padding:1px 6px;font-size:11px;margin-right:3px">{f["label"]}</span>')
    return ''.join(parts)


def _warrant_row(w):
    score     = w.get('score', 0)
    sc        = _score_color(score)
    flags_html = _flag_html(w.get('risk_flags', []))
    dl        = w.get('days_left', 0)
    iv        = f"{w['iv']:.1f}%" if w.get('iv') else '—'
    iv_hv     = f"{w['iv_hv']:.2f}" if w.get('iv_hv') else '—'
    lev       = f"{w['leverage']:.1f}x" if w.get('leverage') else '—'
    money     = w.get('moneyness')
    if money is None:
        money_s, money_c = '—', '#aaa'
    else:
        money_s = f"+{money:.1f}%" if money >= 0 else f"{money:.1f}%"
        money_c = '#27ae60' if money >= 0 else '#e74c3c'
    spd       = w.get('spread_pct', 0)
    stk       = w.get('stock_price', 0)
    stk_chg   = w.get('chg_pct') or stock_indicators_cache.get(w.get('underlying', ''), {}).get('chg_pct', 0)
    stk_chg   = stk_chg or 0
    stk_c     = '#27ae60' if stk_chg >= 0 else '#e74c3c'
    strike    = w.get('strike', 0)
    exch      = w.get('exchange', 'tse')
    exch_badge = ('<span style="background:#3498db;color:#fff;border-radius:3px;padding:0 5px;font-size:10px">上市</span>'
                  if exch in ('tse', 'TWSE') else
                  '<span style="background:#9b59b6;color:#fff;border-radius:3px;padding:0 5px;font-size:10px">上櫃</span>')

    return f'''
  <tr style="border-bottom:1px solid #f0f0f0">
    <td style="padding:10px 8px">
      <div style="font-weight:600;color:#222">{w['code']} {exch_badge}</div>
      <div style="font-size:12px;color:#666">{w['name']}</div>
      <div style="font-size:12px;color:#888">標的：{w.get('underlying','')}
        <span style="color:{stk_c}">@{stk:.2f} ({stk_chg:+.2f}%)</span></div>
      <div style="margin-top:4px">{flags_html}</div>
    </td>
    <td style="padding:10px 8px;text-align:center">
      <div style="font-size:22px;font-weight:700;color:{sc}">{score}</div>
      <div style="background:#eee;border-radius:3px;height:5px;margin-top:4px">
        <div style="width:{score}%;background:{sc};height:5px;border-radius:3px"></div>
      </div>
    </td>
    <td style="padding:10px 8px;font-size:13px">
      <div>現價 <b>${w.get('close',0):.2f}</b> 量 {w.get('volume',0):,}張</div>
      <div>履約價 {strike:.2f} 剩 <b style="color:{'#e74c3c' if dl<30 else '#333'}">{dl}天</b></div>
      <div>價差 {spd:.1f}%　行使比 {w.get('ratio',1):.2f}</div>
    </td>
    <td style="padding:10px 8px;font-size:13px">
      <div>IV {iv} / HV比 <b>{iv_hv}</b></div>
      <div>槓桿 <b>{lev}</b></div>
      <div>價性 <span style="color:{money_c}">{money_s}</span></div>
    </td>
  </tr>'''


# 全域快取供 _warrant_row 使用
stock_indicators_cache = {}


def generate_html(strong_stocks, stock_indicators, calls, puts, run_time):
    global stock_indicators_cache
    stock_indicators_cache = stock_indicators

    now_str = run_time.strftime('%Y/%m/%d %H:%M')
    date_str = run_time.strftime('%m/%d')

    # 強勢股前20
    top_stocks = list(strong_stocks.items())[:20]

    stock_rows = ''
    for sym, sc in top_stocks:
        ind = stock_indicators.get(sym, {})
        pr  = ind.get('price', 0)
        chg = ind.get('chg_pct', 0)
        rsi = ind.get('rsi')
        vr  = ind.get('vol_ratio', 1)
        m20 = ind.get('ma20')
        c   = '#27ae60' if chg >= 0 else '#e74c3c'
        sc_c = _score_color(sc)
        above = '↑MA20' if m20 and pr > m20 else '↓MA20'
        above_c = '#27ae60' if '↑' in above else '#e74c3c'
        stock_rows += f'''
        <tr style="border-bottom:1px solid #f5f5f5">
          <td style="padding:8px">{sym}</td>
          <td style="padding:8px;font-weight:600">{pr:.2f}</td>
          <td style="padding:8px;color:{c}">{chg:+.2f}%</td>
          <td style="padding:8px">{f"{rsi:.1f}" if rsi else "—"}</td>
          <td style="padding:8px">{vr:.2f}x</td>
          <td style="padding:8px;color:{above_c}">{above}</td>
          <td style="padding:8px;color:{sc_c};font-weight:700">{sc}</td>
        </tr>'''

    def warrant_section(warrants, title, color):
        if not warrants:
            return f'<div class="card"><div class="card-title" style="color:{color}">{title}</div><p style="color:#aaa">暫無符合條件的候選</p></div>'

        # 分：有紅色警示 / 無紅色警示
        clean  = [w for w in warrants if not w.get('has_red_flag')]
        risky  = [w for w in warrants if w.get('has_red_flag')]

        rows_clean = ''.join(_warrant_row(w) for w in clean[:30])
        rows_risky = ''.join(_warrant_row(w) for w in risky[:15])

        risky_section = ''
        if rows_risky:
            risky_section = f'''
            <div style="margin-top:20px">
              <div style="font-size:13px;color:#e74c3c;font-weight:600;margin-bottom:8px">
                ⚠️ 有紅色警示（僅供參考，建議避開）</div>
              <div style="overflow-x:auto">
                <table style="width:100%;border-collapse:collapse;font-size:13px">{rows_risky}</table>
              </div>
            </div>'''

        return f'''
        <div class="card">
          <div class="card-title" style="color:{color}">{title}
            <span style="font-size:13px;color:#aaa;font-weight:normal;margin-left:8px">
              共 {len(warrants)} 支（顯示前 30 筆）</span>
          </div>
          <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              <thead>
                <tr style="background:#f8f8f8;color:#888;font-size:12px">
                  <th style="padding:8px;text-align:left">代號 / 名稱</th>
                  <th style="padding:8px;text-align:center">評分</th>
                  <th style="padding:8px;text-align:left">基本資訊</th>
                  <th style="padding:8px;text-align:left">指標</th>
                </tr>
              </thead>
              <tbody>{rows_clean}</tbody>
            </table>
          </div>
          {risky_section}
        </div>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>權證篩選報表 {date_str}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;
        background:#f0ede8;color:#333;padding:16px}}
  .wrap{{max-width:960px;margin:0 auto}}
  .header{{background:#1a1a2e;color:#fff;border-radius:14px;padding:20px 24px;margin-bottom:16px}}
  .header h1{{font-size:22px;font-weight:700}}
  .header .sub{{font-size:13px;color:#aaa;margin-top:4px}}
  .card{{background:#fff;border-radius:14px;padding:20px;margin-bottom:16px;
         box-shadow:0 1px 6px rgba(0,0,0,.07)}}
  .card-title{{font-size:17px;font-weight:700;margin-bottom:14px}}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:4px}}
  .stat-box{{background:#f8f7f4;border-radius:10px;padding:12px;text-align:center}}
  .stat-val{{font-size:24px;font-weight:700;color:#222}}
  .stat-lbl{{font-size:11px;color:#aaa;margin-top:2px}}
  table{{width:100%}}
  th{{text-align:left}}
  @media(max-width:600px){{
    .hide-mobile{{display:none}}
    body{{padding:8px}}
  }}
</style>
</head>
<body>
<div class="wrap">

<div class="header">
  <h1>台股權證篩選報表</h1>
  <div class="sub">執行時間：{now_str} ／ 強勢股 {len(strong_stocks)} 支 ／
    認購候選 {len(calls)} 支 ／ 認售候選 {len(puts)} 支</div>
</div>

<div class="card">
  <div class="stat-grid">
    <div class="stat-box"><div class="stat-val">{len(strong_stocks)}</div><div class="stat-lbl">強勢標的股</div></div>
    <div class="stat-box"><div class="stat-val" style="color:#27ae60">{len([w for w in calls if not w.get('has_red_flag')])}</div><div class="stat-lbl">認購候選（無紅警）</div></div>
    <div class="stat-box"><div class="stat-val" style="color:#e74c3c">{len([w for w in puts if not w.get('has_red_flag')])}</div><div class="stat-lbl">認售候選（無紅警）</div></div>
    <div class="stat-box"><div class="stat-val">{len([w for w in calls+puts if w.get('score',0)>=75])}</div><div class="stat-lbl">高分（≥75）</div></div>
  </div>
</div>

<!-- 強勢股 -->
<div class="card">
  <div class="card-title">強勢標的股 Top {len(top_stocks)}</div>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f8f8f8;color:#888;font-size:12px">
          <th style="padding:8px">代號</th>
          <th style="padding:8px">現價</th>
          <th style="padding:8px">漲跌</th>
          <th style="padding:8px">RSI</th>
          <th style="padding:8px">量比</th>
          <th style="padding:8px">均線</th>
          <th style="padding:8px">強勢分</th>
        </tr>
      </thead>
      <tbody>{stock_rows}</tbody>
    </table>
  </div>
</div>

{warrant_section(calls, "認購權證（Call）候選清單", "#27ae60")}
{warrant_section(puts,  "認售權證（Put）候選清單",  "#e74c3c")}

<div style="text-align:center;color:#bbb;font-size:12px;margin-top:8px;padding-bottom:20px">
  本報表僅供觀察參考，不構成投資建議。資料來源：TWSE / TPEX / Fubon Neo<br>
  自動產生 · {now_str}
</div>

</div>
</body>
</html>'''
    return html


# ─── 排程 ────────────────────────────────────────────────

def main():
    print('權證篩選系統啟動')
    print(f'排程：每天 08:00 執行（共 TWSE + TPEX 全市場）')
    print('按 Ctrl+C 停止\n')

    # 啟動時先跑一次（可註解掉）
    run_screening()

    schedule.every().day.at('08:00').do(run_screening)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == '__main__':
    main()
