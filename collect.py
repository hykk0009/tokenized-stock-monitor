#!/usr/bin/env python3
"""
토큰화 주식 크로스-venue 수집기 (GitHub Actions / 로컬 공용)

수집 대상 (전부 무인증 공개 API)
  1. Robinhood 스톡토큰 호가        api.robinhood.com
  2. Binance bStock 현물 호가       api.binance.com  (US IP 차단 시 자동 폴백)
  3. Hyperliquid 주식 perp          api.hyperliquid.xyz — mark/oracle/funding/OI + funding 이력
  4. BSC 온체인 bStock 풀           GeckoTerminal

출력
  data/snapshots_YYYY-MM.csv  월별 파티션. 한 행 = (시각, 심볼, venue별 가격)
  data/funding.csv            Hyperliquid 자금조달률 이력 (중복 자동 제거, 72h 소급 → 결측 자가치유)
  data/pools.json             BSC 풀 주소 캐시
  data/status.json            마지막 실행 상태 (venue별 성공 여부)

사용
  python collect.py                    # 1회
  python collect.py --count 3 --loop 300   # 5분 간격 3회 (Actions 1회 실행 내 해상도 향상)
  python collect.py --no-dex           # 온체인 생략
"""

import argparse, csv, json, os, sys, time
from datetime import datetime, timezone
import requests

OUT = "data"
FUND = os.path.join(OUT, "funding.csv")
POOL_CACHE = os.path.join(OUT, "pools.json")
STATUS = os.path.join(OUT, "status.json")
TIMEOUT = 20

SYMBOLS = ["CRCL","NVDA","SNDK","TSLA","SPCX","AMD","EWY","INTC","MSTR","LITE","META","MSFT",
           "PLTR","QQQ","CBRS","COIN","GLW","GOOGL","NBIS","QCOM","SPY","SKHY","AAOI","AVGO",
           "BABA","TSM","RKLB","CRWV","ORCL","AAPL","AMAT","AMZN","BE","DELL","FLNC","ASML",
           "ASTS","IREN","NFLX","SMCI","USAR","MRVL"]

HL_DEX = "xyz"
HL_COINS = ["NVDA","TSLA","AAPL","MSFT","META","GOOGL","AMZN","AVGO",
            "COIN","HOOD","INTC","PLTR","GOLD"]

# Binance 는 일부 지역(US 등)에서 api.binance.com 을 차단합니다.
# GitHub Actions 러너가 US 리전일 수 있으므로 폴백 호스트를 순차 시도합니다.
BINANCE_HOSTS = ["https://api.binance.com",
                 "https://data-api.binance.vision",
                 "https://api1.binance.com",
                 "https://api2.binance.com"]

BSC_TOKENS = {
    "NVDA": "0x02fca66c1d1afb4e2a7884261eb00f63598a7436",
    "TSLA": "0x5b1910eaad6450e50f816082aa078c41f10c292f",
    "AAPL": "0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a",
    "SPCX": "0xbe9d156892e55e7154bcd3cb0fea677f9d3103e1",
}

FIELDS = ["ts","symbol",
          "rh_bid","rh_ask","rh_halt","rh_mb_usd",
          "bn_bid","bn_ask","bn_bid_usd","bn_ask_usd",
          "hl_mark","hl_oracle","hl_basis_bps","hl_funding_1h","hl_funding_ann_pct",
          "hl_oi_usd","hl_vol24_usd",
          "dex_usd","dex_liq_usd","dex_vol24_usd","dex_pool_fee",
          "dex_lowfee_liq_usd","dex_lowfee_vol24_usd",
          "rh_bn_mid_bps","rh_bn_exec_bps","dex_bn_bps","dex_rh_bps"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, **kw):
    try:
        r = requests.get(url, timeout=TIMEOUT, **kw)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def post(url, payload):
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# ── 1. Robinhood ────────────────────────────────────────────────
def fetch_robinhood():
    out = {}
    for s in SYMBOLS:
        d = get(f"https://api.robinhood.com/rhj/prices/{s}")
        qs = (d or {}).get("quotes") or []
        q = qs[0] if qs else None
        if q and q.get("bid"):
            out[s] = dict(rh_bid=float(q["bid"]), rh_ask=float(q["ask"]),
                          rh_halt=bool(q.get("isTradingHalt")),
                          rh_mb_usd=float(q.get("mintBurnUsdVolume") or 0))
        time.sleep(0.12)
    return out


# ── 2. Binance ──────────────────────────────────────────────────
def fetch_binance():
    syms = json.dumps([f"{s}BUSDT" for s in SYMBOLS])
    for host in BINANCE_HOSTS:
        d = get(f"{host}/api/v3/ticker/bookTicker", params={"symbols": syms})
        if not d:
            continue
        out = {}
        for x in d:
            sym = x.get("symbol", "")
            if not sym.endswith("BUSDT"):
                continue
            t = sym[:-5]                       # 뒤 "BUSDT" 5글자 제거 → 원 티커
            bid, ask = float(x["bidPrice"]), float(x["askPrice"])
            if bid <= 0 or ask <= 0:
                continue
            out[t] = dict(bn_bid=bid, bn_ask=ask,
                          bn_bid_usd=round(bid * float(x["bidQty"]), 2),
                          bn_ask_usd=round(ask * float(x["askQty"]), 2))
        if out:
            return out, host
    return {}, None


# ── 3. Hyperliquid ──────────────────────────────────────────────
def fetch_hyperliquid():
    d = post("https://api.hyperliquid.xyz/info",
             {"type": "metaAndAssetCtxs", "dex": HL_DEX})
    if not d:
        return {}
    out = {}
    for u, c in zip(d[0]["universe"], d[1]):
        t = u["name"].split(":")[-1]
        if t not in HL_COINS:
            continue
        mark, orc = float(c["markPx"]), float(c["oraclePx"])
        if orc <= 0:
            continue
        out[t] = dict(hl_mark=mark, hl_oracle=orc,
                      hl_basis_bps=round((mark / orc - 1) * 1e4, 2),
                      hl_funding_1h=float(c["funding"]),
                      hl_funding_ann_pct=round(float(c["funding"]) * 24 * 365 * 100, 2),
                      hl_oi_usd=round(float(c["openInterest"]) * mark),
                      hl_vol24_usd=round(float(c["dayNtlVlm"])))
    return out


def fetch_hl_funding(hours_back=72):
    """72h 소급 수집 + 적재 시 중복 제거 → 실행이 몇 번 걸러져도 결측이 자가치유됩니다."""
    end = int(time.time() * 1000)
    start = end - hours_back * 3600_000
    rows = []
    for c in HL_COINS:
        d = post("https://api.hyperliquid.xyz/info",
                 {"type": "fundingHistory", "coin": f"{HL_DEX}:{c}",
                  "startTime": start, "endTime": end})
        for x in (d or []):
            rows.append(dict(time_ms=x["time"], symbol=c,
                             funding=float(x["fundingRate"]),
                             premium=float(x["premium"])))
        time.sleep(0.25)
    return rows


# ── 4. BSC 온체인 ───────────────────────────────────────────────
POOL_TTL_H = 6          # 풀 재탐색 주기(시간)
MAIN_FEE = 0.25         # 현재 주 유동성 풀의 수수료 티어(%) — 이보다 낮으면 "저수수료 티어"


def fee_of(name):
    """'NVDAB / USDT 0.25%' → 0.25 (파싱 실패 시 None)"""
    tok = name.split()[-1] if name else ""
    try:
        return float(tok.rstrip("%")) if tok.endswith("%") else None
    except ValueError:
        return None


def resolve_pools():
    """주 유동성 풀을 주기적으로 재탐색합니다.

    한 번 캐시하고 끝내면 유동성이 저수수료 티어로 옮겨가도 영영 옛 풀만 보게 되어,
    정작 감시 대상인 S1 시나리오를 놓칩니다. 그래서 TTL 이 지나면 다시 고릅니다.
    동시에 저수수료 티어 풀의 유동성·거래대금을 합산해 선행지표로 기록합니다.
    """
    cache = json.load(open(POOL_CACHE)) if os.path.exists(POOL_CACHE) else {}
    now = int(time.time())
    for t, addr in BSC_TOKENS.items():
        cur = cache.get(t)
        fresh = (isinstance(cur, dict) and cur.get("addr")
                 and now - int(cur.get("ts", 0)) < POOL_TTL_H * 3600)
        if fresh:
            continue
        d = get(f"https://api.geckoterminal.com/api/v2/networks/bsc/tokens/{addr}/pools")
        cand = [p["attributes"] for p in ((d or {}).get("data") or [])
                if p["attributes"]["name"].upper().startswith(f"{t}B / USDT")]
        if not cand:
            time.sleep(2.5)
            continue
        best = max(cand, key=lambda a: float(a["volume_usd"]["h24"]))
        low = [a for a in cand
               if (fee_of(a["name"]) is not None and fee_of(a["name"]) < MAIN_FEE)]
        cache[t] = dict(
            addr=best["address"], name=best["name"], ts=now,
            lowfee_liq_usd=round(sum(float(a["reserve_in_usd"]) for a in low)),
            lowfee_vol24_usd=round(sum(float(a["volume_usd"]["h24"]) for a in low)),
            lowfee_pools=len(low))
        time.sleep(2.5)
    os.makedirs(OUT, exist_ok=True)
    json.dump(cache, open(POOL_CACHE, "w"), indent=1, ensure_ascii=False)
    return cache


def fetch_bsc(pools):
    out = {}
    for t, p in pools.items():
        addr = p["addr"] if isinstance(p, dict) else p
        name = p.get("name", "") if isinstance(p, dict) else ""
        d = get(f"https://api.geckoterminal.com/api/v2/networks/bsc/pools/{addr}")
        a = ((d or {}).get("data") or {}).get("attributes")
        if a:
            # 풀 이름 끝의 수수료 티어를 기록 — 저수수료 티어로 유동성이 이동하는지가 감시 포인트
            fee = name.split()[-1] if "%" in name else ""
            out[t] = dict(dex_usd=float(a["base_token_price_usd"]),
                          dex_liq_usd=round(float(a["reserve_in_usd"])),
                          dex_vol24_usd=round(float(a["volume_usd"]["h24"])),
                          dex_pool_fee=fee)
            # 저수수료 티어 선행지표 — 티어가 완전히 뒤집히기 전에 유동성 형성을 먼저 잡습니다
            if isinstance(p, dict) and "lowfee_liq_usd" in p:
                out[t]["dex_lowfee_liq_usd"] = p["lowfee_liq_usd"]
                out[t]["dex_lowfee_vol24_usd"] = p["lowfee_vol24_usd"]
        time.sleep(2.5)
    return out


# ── 적재 ────────────────────────────────────────────────────────
def snap_path():
    return os.path.join(OUT, f"snapshots_{datetime.now(timezone.utc):%Y-%m}.csv")


def append(path, fields, rows):
    os.makedirs(OUT, exist_ok=True)
    new = not os.path.exists(path)

    # 컬럼이 추가된 경우 기존 파일의 헤더와 어긋나 열이 밀립니다.
    # 헤더가 다르면 기존 행을 새 컬럼 구성으로 옮겨 적고 이어붙입니다.
    if not new:
        with open(path, newline="") as f:
            old = next(csv.reader(f), None)
        if old and old != list(fields):
            with open(path, newline="") as f:
                kept = list(csv.DictReader(f))
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(kept)

    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def dedupe_funding(rows):
    seen = set()
    if os.path.exists(FUND):
        with open(FUND) as f:
            for r in csv.DictReader(f):
                seen.add((r["time_ms"], r["symbol"]))
    out, added = [], set()
    for r in rows:
        k = (str(r["time_ms"]), r["symbol"])
        if k in seen or k in added:
            continue
        added.add(k)
        out.append(r)
    return out


def collect(use_dex=True):
    ts = now_iso()
    st = {"ts": ts}
    rh = fetch_robinhood();                st["robinhood"] = len(rh)
    bn, host = fetch_binance();            st["binance"] = len(bn); st["binance_host"] = host
    hl = fetch_hyperliquid();              st["hyperliquid"] = len(hl)
    dx = fetch_bsc(resolve_pools()) if use_dex else {}
    st["bsc"] = len(dx)

    rows = []
    for s in sorted(set(rh) | set(bn) | set(hl) | set(dx)):
        r = dict(ts=ts, symbol=s)
        for src in (rh, bn, hl, dx):
            r.update(src.get(s, {}))
        if all(k in r for k in ("rh_bid", "rh_ask", "bn_bid", "bn_ask")):
            rm = (r["rh_bid"] + r["rh_ask"]) / 2
            bm = (r["bn_bid"] + r["bn_ask"]) / 2
            r["rh_bn_mid_bps"] = round((rm / bm - 1) * 1e4, 2)
            r["rh_bn_exec_bps"] = round(max(r["rh_bid"] / r["bn_ask"] - 1,
                                            r["bn_bid"] / r["rh_ask"] - 1) * 1e4, 2)
        if "dex_usd" in r and "bn_bid" in r:
            bm = (r["bn_bid"] + r["bn_ask"]) / 2
            r["dex_bn_bps"] = round((r["dex_usd"] / bm - 1) * 1e4, 2)
        # Binance 가 막힌 환경(GitHub Actions US 러너 등)에서의 대체 기준축.
        # 보고서 §2.3 기준 RH↔Binance 중간가 차이는 중앙값 7.1bps 이므로
        # dex_bn_bps 대신 써도 해석이 크게 흔들리지 않습니다.
        if "dex_usd" in r and "rh_bid" in r and "rh_ask" in r:
            rm = (r["rh_bid"] + r["rh_ask"]) / 2
            if rm > 0:
                r["dex_rh_bps"] = round((r["dex_usd"] / rm - 1) * 1e4, 2)
        rows.append(r)
    append(snap_path(), FIELDS, rows)
    st["rows"] = len(rows)

    fh = dedupe_funding(fetch_hl_funding())
    if fh:
        append(FUND, ["time_ms", "symbol", "funding", "premium"], fh)
    st["funding_new"] = len(fh)

    os.makedirs(OUT, exist_ok=True)
    json.dump(st, open(STATUS, "w"), indent=1)
    print(json.dumps(st, ensure_ascii=False))
    return st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="반복 간격(초)")
    ap.add_argument("--count", type=int, default=1, help="반복 횟수 (0=무한)")
    ap.add_argument("--no-dex", action="store_true")
    a = ap.parse_args()

    n, ok = 0, 0
    while True:
        n += 1
        try:
            s = collect(use_dex=not a.no_dex)
            if s.get("rows"):
                ok += 1
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
        if a.count and n >= a.count:
            break
        time.sleep(a.loop or 300)

    # 모든 시도가 실패하면 Actions 가 빨간불로 알려주도록 종료코드 1
    sys.exit(0 if ok else 1)