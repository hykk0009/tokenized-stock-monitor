#!/usr/bin/env python3
"""
수집 데이터 요약 — 주간 모니터링용

로컬 실행:   python summarize.py
원격 실행:   python summarize.py --repo <owner>/<name>
             (raw.githubusercontent.com 에서 CSV를 직접 읽습니다)

출력: 사람이 읽을 수 있는 요약 텍스트 + summary.json
보고서 §8.6 모니터링 항목의 임계치 이탈을 자동으로 표시합니다.
"""

import argparse, io, json, os, sys
import pandas as pd
import requests

# 보고서 v3 기준 기준선 — 이탈 시 ⚠ 표시
BASE = dict(
    rh_bn_mid_bps_med=7.1,      # Robinhood↔Binance 중간가 차이 중앙값
    hurdle_exec_bps=20.0,       # 왕복 수수료
    dex_pool_fee="0.25%",       # 현재 주 유동성 풀 수수료 티어
    hl_carry_ann_pct=7.0,       # 순 캐리 중앙값
)


def load(repo, name, branch="main"):
    if repo:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/data/{name}"
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return None
        return pd.read_csv(io.StringIO(r.text))
    p = os.path.join("data", name)
    return pd.read_csv(p) if os.path.exists(p) else None


def load_snapshots(repo, branch):
    frames = []
    if repo:
        # 최근 3개월치 파티션을 시도
        for m in pd.date_range(end=pd.Timestamp.utcnow(), periods=3, freq="MS")[::-1]:
            d = load(repo, f"snapshots_{m:%Y-%m}.csv", branch)
            if d is not None:
                frames.append(d)
    else:
        import glob
        for p in sorted(glob.glob("data/snapshots_*.csv")):
            frames.append(pd.read_csv(p))
    return pd.concat(frames, ignore_index=True) if frames else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="<owner>/<name>. 생략 시 로컬 data/ 사용")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()

    out, alerts = {}, []
    L = []

    snap = load_snapshots(a.repo, a.branch)
    fund = load(a.repo, "funding.csv", a.branch)

    if snap is None or not len(snap):
        print("스냅샷 데이터 없음 — 수집이 아직 시작되지 않았거나 실패했습니다.")
        sys.exit(0)

    snap["ts"] = pd.to_datetime(snap["ts"], utc=True, errors="coerce")
    snap = snap.dropna(subset=["ts"])
    span = (snap.ts.max() - snap.ts.min())
    out["span_days"] = round(span.total_seconds() / 86400, 1)
    out["n_snapshots"] = int(snap.ts.nunique())
    out["last_ts"] = str(snap.ts.max())
    L.append(f"■ 수집 현황: {out['n_snapshots']}회 · {out['span_days']}일 · 최종 {out['last_ts']}")

    stale = (pd.Timestamp.utcnow() - snap.ts.max()).total_seconds() / 3600
    if stale > 6:
        alerts.append(f"⚠ 마지막 수집이 {stale:.1f}시간 전입니다 — Actions 실행 확인 필요")

    # ── Robinhood ↔ Binance ──
    d = snap.dropna(subset=["rh_bn_mid_bps"])
    if len(d):
        med = d.rh_bn_mid_bps.abs().median()
        ex = d.rh_bn_exec_bps
        over = (ex > BASE["hurdle_exec_bps"]).mean()
        out["rh_bn"] = dict(mid_abs_med=round(float(med), 2),
                            exec_med=round(float(ex.median()), 2),
                            exec_max=round(float(ex.max()), 2),
                            share_over_hurdle=round(float(over), 4),
                            n=int(len(d)))
        L.append(f"■ Robinhood↔Binance: 중간가차 중앙값 {med:.1f}bps "
                 f"(기준 {BASE['rh_bn_mid_bps_med']}) · 체결가능 중앙값 {ex.median():.1f}bps · "
                 f"수수료 20bps 초과 비중 {over*100:.2f}%")
        if over > 0.01:
            alerts.append(f"⚠ 체결가능 스프레드가 수수료를 넘은 관측이 {over*100:.1f}% — 재검토 가치 있음")
        top = d.groupby("symbol").rh_bn_exec_bps.max().sort_values(ascending=False).head(5)
        L.append("   상위 종목(체결가능 최대): " + ", ".join(f"{k} {v:.1f}" for k, v in top.items()))

    # ── BSC 풀 수수료 티어 (S1 감시 포인트) ──
    d = snap.dropna(subset=["dex_pool_fee"]) if "dex_pool_fee" in snap else pd.DataFrame()
    if len(d):
        last = d.sort_values("ts").groupby("symbol").tail(1)
        fees = dict(zip(last.symbol, last.dex_pool_fee))
        out["dex_pool_fee"] = fees
        L.append("■ BSC 주 유동성 풀 수수료 티어: " + ", ".join(f"{k} {v}" for k, v in fees.items()))
        moved = {k: v for k, v in fees.items() if v and v != BASE["dex_pool_fee"]}
        if moved:
            alerts.append(f"⚠ 저수수료 티어로 유동성 이동 감지: {moved} — 보고서 S1 시나리오 발동 조건")

    # ── Binance 호가 심도 ──
    if "bn_bid_usd" in snap:
        d = snap.dropna(subset=["bn_bid_usd"])
        if len(d):
            last = d.sort_values("ts").groupby("symbol").tail(1)
            depth = ((last.bn_bid_usd + last.bn_ask_usd) / 2)
            out["depth_med_usd"] = round(float(depth.median()), 0)
            L.append(f"■ Binance 최우선 호가 잔량 중앙값: ${depth.median():,.0f} "
                     f"(최소 ${depth.min():,.0f} / 최대 ${depth.max():,.0f})")
            if depth.median() > 20000:
                alerts.append("⚠ 호가 심도가 크게 개선됨 — 마켓메이킹/차익 재검토 조건")

    # ── Hyperliquid 자금조달률 ──
    if fund is not None and len(fund):
        fund["t"] = pd.to_datetime(fund.time_ms, unit="ms", utc=True)
        rows = []
        for sym, g in fund.groupby("symbol"):
            g = g.sort_values("t")
            for label, days in (("7d", 7), ("30d", 30), ("all", 10**4)):
                s = g[g.t >= g.t.max() - pd.Timedelta(days=days)]
                if len(s) < 12:
                    continue
                dd = (s.t.max() - s.t.min()).total_seconds() / 86400 or 1
                rows.append(dict(symbol=sym, window=label,
                                 ann=round(float(s.funding.sum() * 100 / dd * 365), 1),
                                 pos=round(float((s.funding > 0).mean()), 2), n=len(s)))
        f = pd.DataFrame(rows)
        out["funding"] = f.to_dict("records")
        w7 = f[f.window == "7d"].sort_values("ann", ascending=False)
        if len(w7):
            L.append("■ Hyperliquid 자금조달률 (최근 7일 실현 연환산):")
            for _, r in w7.iterrows():
                mark = " ⚠부호반전" if r.ann < 0 else ""
                L.append(f"   {r.symbol:6} {r.ann:+7.1f}%  (양수시간 {r.pos*100:.0f}%){mark}")
            neg = w7[w7.ann < 0]
            if len(neg) > len(w7) * 0.4:
                alerts.append(f"⚠ 자금조달률이 음(−)인 종목이 {len(neg)}/{len(w7)} — 캐리 전략 재점검")
            med = w7.ann.median()
            L.append(f"   중앙값 {med:+.1f}% (보고서 기준 실현 6~14%, 순 {BASE['hl_carry_ann_pct']}%)")

    print("\n".join(L))
    if alerts:
        print("\n" + "\n".join(alerts))
    else:
        print("\n임계치 이탈 없음 — 보고서 v3 판정 유지")

    out["alerts"] = alerts
    json.dump(out, open("summary.json", "w"), indent=1, ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
