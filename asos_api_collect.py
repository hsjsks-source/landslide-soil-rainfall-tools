# -*- coding: utf-8 -*-
"""
ASOS 시간자료 Open API 수집 + 강우 임계 분석 (예비용 — 포털 일괄 다운로드가 안 될 때)

[기능 변화 이력]
- v1.0 (2026-10-02)
  · 기상청_지상(종관,ASOS) 시간자료 조회서비스에서 월 단위로 분할 수집, 페이지 단위 반복
  · 인증키는 코드에 넣지 않음 — 환경변수 ASOS_SERVICE_KEY 또는 스크립트 옆 datagokr_key.txt
  · 인코딩키/디코딩키 어느 쪽이든 한 번만 인코딩되도록 자동 처리
  · 월별 캐시(_months)로 중단 후 이어받기
  · 산출: 시간자료 원본 / 일강수량+CAR / 강우이벤트·임계초과표 / 수집로그 (임계기준은 asos_portal_process.py 와 동일)
  · QGIS Python Console 실행 지원, 기본 경로는 스크립트 폴더 기준 ./data/…

[사용법]
  python asos_api_collect.py --stn <지점번호> [--start 20210101 --end 20251231 --out ./data/ASOS_결과]
  외부 패키지 불필요(표준 라이브러리만 사용)
"""
import argparse, calendar, csv, json, os, sys, time
import urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, date

ENDPOINT = "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList"

# 산림청 고정 강우기준
TH_HOURLY, TH_DAILY, TH_EVENT = 30.0, 150.0, 200.0
# Kim et al. 2020 ID 임계식
ID_A, ID_B, ID_DMIN, ID_DMAX = 10.40, -0.31, 4, 84
# 선행강우 감쇠
CAR_K, CAR_DAYS = 0.9, 20
# 강우이벤트 구분: 무강우 연속 시간
EVENT_DRY_GAP_H = 24


# ---------------------------------------------------------------- 인증키
def _script_dir():
    """스크립트 폴더 — QGIS 편집기 실행(exec)에서는 __file__ 이 없으므로 컴파일 파일명으로 대체"""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        import inspect
        return os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename))


def load_key():
    key = os.environ.get("ASOS_SERVICE_KEY", "").strip()
    if not key:
        here = _script_dir()
        for name in ("datagokr_key.txt", "asos_key.txt"):
            p = os.path.join(here, name)
            if os.path.exists(p):
                with open(p, encoding="utf-8-sig") as f:
                    key = f.read().strip()
                break
    if not key:
        sys.exit("[중단] 인증키가 없습니다. 스크립트 옆에 datagokr_key.txt(키 한 줄)를 만들거나 "
                 "환경변수 ASOS_SERVICE_KEY를 설정하세요.")
    # 인코딩키(%2B, %3D 포함)면 디코딩 → 이후 urlencode에서 정확히 한 번만 인코딩
    if "%" in key:
        key = urllib.parse.unquote(key)
    return key


# ---------------------------------------------------------------- API 호출
class ApiError(Exception):
    pass


def call_api(key, params, retries=4):
    q = dict(params)
    q["serviceKey"] = key
    url = ENDPOINT + "?" + urllib.parse.urlencode(q)
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                txt = r.read().decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"네트워크 오류: {e}"
            time.sleep(3 * attempt)
            continue
        # 게이트웨이 오류는 dataType=JSON이어도 XML로 옴
        if txt.startswith("<"):
            reason = _xml_tag(txt, "returnReasonCode") or _xml_tag(txt, "resultCode")
            msg = _xml_tag(txt, "returnAuthMsg") or _xml_tag(txt, "resultMsg") or txt[:200]
            if reason in ("30", "31", "32", "20", "22"):   # 키 미등록/만료/IP/권한/한도 → 재시도 무의미
                raise ApiError(f"[{reason}] {msg}")
            last = f"XML 응답 [{reason}] {msg}"
            time.sleep(3 * attempt)
            continue
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            last = f"JSON 파싱 실패: {txt[:200]}"
            time.sleep(3 * attempt)
            continue
        hdr = data.get("response", {}).get("header", {})
        code = str(hdr.get("resultCode", ""))
        if code == "00":
            return data["response"]["body"]
        if code == "03":    # NO_DATA
            return {"totalCount": 0, "items": {"item": []}}
        if code in ("20", "22", "30", "31", "32"):
            raise ApiError(f"[{code}] {hdr.get('resultMsg')}")
        last = f"resultCode {code}: {hdr.get('resultMsg')}"
        time.sleep(3 * attempt)
    raise ApiError(f"재시도 {retries}회 실패 — {last}")


def _xml_tag(txt, tag):
    a, b = txt.find(f"<{tag}>"), txt.find(f"</{tag}>")
    return txt[a + len(tag) + 2:b].strip() if a >= 0 and b > a else ""


def fetch_month(key, stn, y, m, rows=999):
    last_day = calendar.monthrange(y, m)[1]
    base = {
        "pageNo": 1, "numOfRows": rows, "dataType": "JSON",
        "dataCd": "ASOS", "dateCd": "HR",
        "startDt": f"{y:04d}{m:02d}01", "startHh": "00",
        "endDt": f"{y:04d}{m:02d}{last_day:02d}", "endHh": "23",
        "stnIds": stn,
    }
    items, page, total = [], 1, None
    while True:
        base["pageNo"] = page
        body = call_api(key, base)
        total = int(body.get("totalCount", 0) or 0)
        it = body.get("items", {})
        it = it.get("item", []) if isinstance(it, dict) else []
        if isinstance(it, dict):
            it = [it]
        items.extend(it)
        if len(items) >= total or not it:
            break
        page += 1
        time.sleep(0.3)
    return items, total


# ---------------------------------------------------------------- 분석
def to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def build_hourly(rows):
    """tm → 시간강수량(mm). 공란은 무강수 0, QC 9(결측)은 None."""
    out = {}
    for r in rows:
        try:
            t = datetime.strptime(r["tm"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        if str(r.get("rnQcflg", "")).strip() == "9":
            out[t] = None
        else:
            v = to_float(r.get("rn"))
            out[t] = v if v is not None else 0.0
    return out


def daily_and_car(hourly, start, end):
    d_sum, d_missing = {}, {}
    for t, v in hourly.items():
        d = t.date()
        d_sum.setdefault(d, 0.0)
        d_missing.setdefault(d, 0)
        if v is None:
            d_missing[d] += 1
        else:
            d_sum[d] += v
    days, d = [], start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    rows, hist = [], []
    for d in days:
        n_obs = sum(1 for h in range(24) if (datetime(d.year, d.month, d.day) + timedelta(hours=h)) in hourly)
        r = round(d_sum.get(d, 0.0), 1)
        hist.append(r)
        window = hist[-(CAR_DAYS + 1):][::-1]
        car = sum((CAR_K ** i) * x for i, x in enumerate(window))
        rows.append({
            "date": d.isoformat(), "rain_mm": r, "obs_hours": n_obs,
            "missing_qc9_hours": d_missing.get(d, 0),
            "CAR_mm": round(car, 1),
            "exceed_daily150": int(r >= TH_DAILY),
        })
    return rows


def rain_events(hourly):
    ts = sorted(t for t, v in hourly.items() if v and v > 0)
    events, cur = [], []
    for t in ts:
        if cur and (t - cur[-1]) > timedelta(hours=EVENT_DRY_GAP_H):
            events.append(cur)
            cur = []
        cur.append(t)
    if cur:
        events.append(cur)
    out = []
    for i, ev in enumerate(events, 1):
        s, e = ev[0], ev[-1]
        dur = int((e - s).total_seconds() // 3600) + 1
        tot = sum(hourly[t] for t in ev)
        mean_i = tot / dur
        max_h = max(hourly[t] for t in ev)
        daily = {}
        for t in ev:
            daily[t.date()] = daily.get(t.date(), 0.0) + hourly[t]
        id_th = ID_A * dur ** ID_B if ID_DMIN <= dur <= ID_DMAX else None
        out.append({
            "event_id": i, "start": s.strftime("%Y-%m-%d %H:%M"), "end": e.strftime("%Y-%m-%d %H:%M"),
            "duration_h": dur, "total_mm": round(tot, 1), "mean_intensity_mmh": round(mean_i, 2),
            "max_hourly_mm": round(max_h, 1), "max_daily_mm": round(max(daily.values()), 1),
            "exceed_hourly30": int(max_h >= TH_HOURLY),
            "exceed_daily150": int(max(daily.values()) >= TH_DAILY),
            "exceed_event200": int(tot >= TH_EVENT),
            "ID_threshold_mmh": round(id_th, 2) if id_th else "",
            "exceed_ID_Kim2020": int(id_th is not None and mean_i >= id_th),
        })
    return out


def write_csv(path, rows, fields=None):
    if not rows:
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210101")
    ap.add_argument("--end", default="20251231")
    ap.add_argument("--stn", default="108")   # ASOS 지점번호(예: 108 서울) — 대상 지점으로 바꿔 쓰기
    ap.add_argument("--out", default=os.path.join(_script_dir(), "data", "ASOS_결과"))
    a = ap.parse_args(argv)

    start = datetime.strptime(a.start, "%Y%m%d").date()
    end = datetime.strptime(a.end, "%Y%m%d").date()
    yesterday = date.today() - timedelta(days=1)
    if end > yesterday:
        end = yesterday   # API는 전일(D-1)까지만 제공
    os.makedirs(a.out, exist_ok=True)
    cache = os.path.join(a.out, "_months")
    os.makedirs(cache, exist_ok=True)

    key = load_key()
    log = []
    all_rows = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        cp = os.path.join(cache, f"{a.stn}_{y:04d}{m:02d}.json")
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                rows = json.load(f)
            src = "캐시"
        else:
            try:
                rows, total = fetch_month(key, a.stn, y, m)
            except ApiError as e:
                msg = f"{y:04d}-{m:02d} 실패: {e}"
                print("[중단] " + msg)
                log.append(msg)
                if any(c in str(e) for c in ("[30]", "[31]", "[32]", "[20]", "[22]")):
                    print("  → 인증키/권한/한도 문제라 재시도해도 같습니다. 키 확인 후 다시 실행하면 받은 달은 건너뜁니다.")
                    break
                rows = []
            else:
                with open(cp, "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False)
                time.sleep(0.5)
            src = "API"
        exp = calendar.monthrange(y, m)[1] * 24
        line = f"{y:04d}-{m:02d}  {len(rows):4d}/{exp}행  ({src})"
        print(line)
        log.append(line)
        all_rows.extend(rows)
        m += 1
        if m > 12:
            y, m = y + 1, 1

    if not all_rows:
        sys.exit("[중단] 받은 데이터가 없습니다.")

    # 중복 제거 + 정렬
    uniq = {r.get("tm"): r for r in all_rows if r.get("tm")}
    all_rows = [uniq[k] for k in sorted(uniq)]
    tag = f"{a.start}-{end.strftime('%Y%m%d')}"
    fields = list(dict.fromkeys(k for r in all_rows for k in r.keys()))
    write_csv(os.path.join(a.out, f"ASOS_{a.stn}_시간자료_{tag}.csv"), all_rows, fields)

    hourly = build_hourly(all_rows)
    daily = daily_and_car(hourly, start, end)
    write_csv(os.path.join(a.out, f"ASOS_{a.stn}_일강수_CAR_{tag}.csv"), daily)
    events = rain_events(hourly)
    write_csv(os.path.join(a.out, f"ASOS_{a.stn}_강우이벤트_임계초과_{tag}.csv"), events)

    exp_h = ((end - start).days + 1) * 24
    n_qc9 = sum(1 for v in hourly.values() if v is None)
    summ = [
        "",
        "=== 요약 ===",
        f"기간 {start} ~ {end} / 지점 {a.stn}",
        f"시간자료 {len(hourly)}/{exp_h}행 (결측 {exp_h - len(hourly)}시간, QC9 {n_qc9}시간)",
        f"총 강수량 {sum(v for v in hourly.values() if v):.1f} mm",
        f"강우이벤트 {len(events)}건 (무강우 {EVENT_DRY_GAP_H}시간 기준 분리)",
        f"  시우량 30mm 이상 이벤트   : {sum(e['exceed_hourly30'] for e in events)}",
        f"  일강우량 150mm 이상 이벤트: {sum(e['exceed_daily150'] for e in events)}",
        f"  연속강우량 200mm 이상     : {sum(e['exceed_event200'] for e in events)}",
        f"  Kim 2020 ID 임계 초과     : {sum(e['exceed_ID_Kim2020'] for e in events)}",
        f"최대 CAR {max(d['CAR_mm'] for d in daily):.1f} mm",
    ]
    print("\n".join(summ))
    log.extend(summ)
    with open(os.path.join(a.out, f"수집로그_{datetime.now().strftime('%Y%m%d%H%M')}.txt"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    print(f"\n저장 위치: {a.out}")


# ---------------------------------------------------------------- 실행 진입점
# QGIS Python Console 편집기의 ▶(Run Script)는 __name__ 이 "__main__" 이 아니어서
# 그대로 두면 아무것도 실행되지 않음 → QGIS 안이면 아래 QGIS_ARGS 로 바로 실행
QGIS_ARGS = []

_IN_QGIS = "qgis" in sys.modules
if __name__ == "__main__" or _IN_QGIS:
    try:
        main(QGIS_ARGS if (_IN_QGIS and __name__ != "__main__") else None)
    except SystemExit as _e:
        if _e.code not in (None, 0):
            print(_e.code)
