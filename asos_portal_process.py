# -*- coding: utf-8 -*-
"""
기상자료개방포털 ASOS 시간자료(연도별 zip/csv) 병합 + 강우 임계 분석

[기능 변화 이력]
- v1.1 (2026-10-02)
  · 파일명과 무관하게 ASOS 시간자료 csv 인식(헤더에 지점·일시·강수량이 있으면 읽음)
    — 포털 기간지정 다운로드 파일(OBS_ASOS_TIM_*.csv) 대응
  · 파일마다 컬럼 구성이 달라도 병합(일시 컬럼을 이름으로 찾고 컬럼 합집합으로 저장)
  · 분석 종료시각 = 마지막 관측시각(연말 고정 아님) → 진행 중인 연도의 남은 시간이 결측으로 잡히지 않음
  · 연도별요약에 연도별 분석기간(period_start/period_end), 처리로그에 부분연도 표시
- v1.0 (2026-10-02)
  · 기상자료개방포털에서 받은 지점별 연도별 시간자료(SURFACE_ASOS_*_HR_*.zip / *.csv, cp949)를 풀지 않고 병합
  · 일시 기준 중복 제거, 결측 시간 점검(연도별)
  · 강수량 공란 = 무강수(0mm) 처리
  · 산출: 병합 시간자료 / 일강수량+선행강우(CAR, K=0.9, 20일) / 강우이벤트·임계초과표(무강우 24시간 기준 분리) / 연도별 요약 / 처리로그
  · 임계기준: 산림청 고정기준(시우량 30mm·일강우량 150mm·연속강우량 200mm)
              + Kim et al. 2020 ID 임계식 I = 10.40·D^-0.31 (D = 4~84시간)
  · QGIS Python Console 실행 지원, 기본 경로는 스크립트 폴더 기준 ./data/…

[사용법]
  python asos_portal_process.py [--in ./data/asos_portal] [--out ./data/ASOS_결과]
  외부 패키지 불필요(표준 라이브러리만 사용)
"""
import argparse, csv, glob, io, os, sys, zipfile
from datetime import datetime, timedelta

TH_HOURLY, TH_DAILY, TH_EVENT = 30.0, 150.0, 200.0
ID_A, ID_B, ID_DMIN, ID_DMAX = 10.40, -0.31, 4, 84
CAR_K, CAR_DAYS = 0.9, 20
EVENT_DRY_GAP_H = 24


def _script_dir():
    """스크립트 폴더 — QGIS 편집기 실행(exec)에서는 __file__ 이 없으므로 컴파일 파일명으로 대체"""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        import inspect
        return os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename))


def read_text(raw):
    for enc in ("cp949", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp949", errors="replace")


def load_tables(in_dir):
    srcs = []
    for p in sorted(glob.glob(os.path.join(in_dir, "**", "*"), recursive=True)):
        low = p.lower()
        if low.endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.lower().endswith(".csv"):
                        txt = read_text(z.read(n))
                        if is_asos_hourly(txt):
                            srcs.append((f"{os.path.basename(p)}/{n}", txt))
        elif low.endswith(".csv"):
            if any(part.startswith("ASOS_") for part in os.path.relpath(p, in_dir).split(os.sep)[:-1]):
                continue   # 이 스크립트의 산출 폴더는 제외
            with open(p, "rb") as f:
                txt = read_text(f.read())
            if is_asos_hourly(txt):
                srcs.append((os.path.basename(p), txt))
    return srcs


def is_asos_hourly(txt):
    head = txt.split("\n", 1)[0]
    return "지점" in head and "일시" in head and "강수량" in head


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(_script_dir(), "data", "asos_portal"))
    ap.add_argument("--out", default=os.path.join(_script_dir(), "data", "ASOS_결과"))
    a = ap.parse_args(argv)
    if not os.path.isdir(a.inp):
        sys.exit(f"[중단] 입력 폴더가 없습니다: {a.inp}")
    os.makedirs(a.out, exist_ok=True)
    log = []

    def say(s):
        print(s)
        log.append(s)

    header, recs = [], {}
    for name, txt in load_tables(a.inp):
        rd = csv.reader(io.StringIO(txt))
        h = [c.strip() for c in next(rd)]
        if "일시" not in h:
            say(f"  ※ {name}: '일시' 컬럼 없음 — 건너뜀")
            continue
        ti = h.index("일시")
        for c in h:                      # 컬럼 합집합(첫 등장 순서 유지)
            if c not in header:
                header.append(c)
        n = 0
        for r in rd:
            if len(r) <= ti or not r[ti].strip():
                continue
            row = dict(zip(h, r))
            recs[row["일시"].strip()] = row   # 일시 기준 중복 제거(뒤에 읽은 파일 우선)
            n += 1
        say(f"읽음: {name}  {n:,}행")
    if not recs:
        sys.exit("[중단] ASOS 시간자료 zip / csv 를 찾지 못했습니다.")

    rcol = next(c for c in header if c.startswith("강수량") and "QC" not in c)
    times = sorted(datetime.strptime(k, "%Y-%m-%d %H:%M") for k in recs)
    t0, t1 = times[0], times[-1]
    start = datetime(t0.year, 1, 1)
    end = t1   # 마지막 관측시각까지(진행 중인 연도는 부분연도)
    tag = f"{start:%Y%m%d}-{end:%Y%m%d}"
    stn = recs[t0.strftime("%Y-%m-%d %H:%M")].get("지점", "STN")

    # 1) 병합 원본
    with open(os.path.join(a.out, f"ASOS_{stn}_시간자료_병합_{tag}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for t in times:
            w.writerow(recs[t.strftime("%Y-%m-%d %H:%M")])

    # 시간강수 (공란=0)
    hourly = {}
    for t in times:
        v = recs[t.strftime("%Y-%m-%d %H:%M")].get(rcol, "").strip()
        try:
            hourly[t] = float(v) if v else 0.0
        except ValueError:
            hourly[t] = 0.0

    # 결측 점검
    missing_by_year = {}
    t = start
    while t <= end:
        if t not in hourly:
            missing_by_year[t.year] = missing_by_year.get(t.year, 0) + 1
        t += timedelta(hours=1)

    # 2) 일강수 + CAR
    dsum = {}
    for t, v in hourly.items():
        dsum[t.date()] = dsum.get(t.date(), 0.0) + v
    daily, hist, d = [], [], start.date()
    while d <= end.date():
        r = round(dsum.get(d, 0.0), 1)
        hist.append(r)
        car = sum((CAR_K ** i) * x for i, x in enumerate(hist[-(CAR_DAYS + 1):][::-1]))
        obs = sum(1 for h in range(24) if datetime(d.year, d.month, d.day, h) in hourly)
        daily.append({"date": d.isoformat(), "rain_mm": r, "obs_hours": obs,
                      "CAR_mm": round(car, 1), "exceed_daily150": int(r >= TH_DAILY)})
        d += timedelta(days=1)
    with open(os.path.join(a.out, f"ASOS_{stn}_일강수_CAR_{tag}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(daily[0].keys()))
        w.writeheader()
        w.writerows(daily)

    # 3) 강우이벤트
    wet = sorted(t for t, v in hourly.items() if v > 0)
    groups, cur = [], []
    for t in wet:
        if cur and (t - cur[-1]) > timedelta(hours=EVENT_DRY_GAP_H):
            groups.append(cur)
            cur = []
        cur.append(t)
    if cur:
        groups.append(cur)
    events = []
    for i, g in enumerate(groups, 1):
        s, e = g[0], g[-1]
        dur = int((e - s).total_seconds() // 3600) + 1
        tot = sum(hourly[t] for t in g)
        mi = tot / dur
        mh = max(hourly[t] for t in g)
        dd = {}
        for t in g:
            dd[t.date()] = dd.get(t.date(), 0.0) + hourly[t]
        md = max(dd.values())
        idth = ID_A * dur ** ID_B if ID_DMIN <= dur <= ID_DMAX else None
        car_start = next((x["CAR_mm"] for x in daily if x["date"] == (s.date() - timedelta(days=1)).isoformat()), "")
        events.append({
            "event_id": i, "start": f"{s:%Y-%m-%d %H:%M}", "end": f"{e:%Y-%m-%d %H:%M}",
            "duration_h": dur, "total_mm": round(tot, 1), "mean_intensity_mmh": round(mi, 2),
            "max_hourly_mm": round(mh, 1), "max_daily_mm": round(md, 1),
            "antecedent_CAR_mm": car_start,
            "exceed_hourly30": int(mh >= TH_HOURLY), "exceed_daily150": int(md >= TH_DAILY),
            "exceed_event200": int(tot >= TH_EVENT),
            "ID_threshold_mmh": round(idth, 2) if idth else "",
            "exceed_ID_Kim2020": int(idth is not None and mi >= idth),
        })
    with open(os.path.join(a.out, f"ASOS_{stn}_강우이벤트_임계초과_{tag}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
        w.writeheader()
        w.writerows(events)

    # 4) 연도별 요약
    years = []
    for y in range(start.year, end.year + 1):
        hv = [v for t, v in hourly.items() if t.year == y]
        dv = [x["rain_mm"] for x in daily if x["date"].startswith(str(y))]
        ev = [e for e in events if e["start"].startswith(str(y))]
        ps = max(start, datetime(y, 1, 1))
        pe = min(end, datetime(y, 12, 31, 23))
        if pe < ps:
            continue
        years.append({
            "year": y, "period_start": f"{ps:%Y-%m-%d %H:%M}", "period_end": f"{pe:%Y-%m-%d %H:%M}",
            "obs_hours": len(hv), "missing_hours": missing_by_year.get(y, 0),
            "annual_rain_mm": round(sum(hv), 1), "max_hourly_mm": max(hv) if hv else "",
            "max_daily_mm": max(dv) if dv else "", "events": len(ev),
            "n_hourly30": sum(e["exceed_hourly30"] for e in ev),
            "n_daily150": sum(e["exceed_daily150"] for e in ev),
            "n_event200": sum(e["exceed_event200"] for e in ev),
            "n_ID_Kim2020": sum(e["exceed_ID_Kim2020"] for e in ev),
            "max_event_mm": max((e["total_mm"] for e in ev), default=""),
        })
    with open(os.path.join(a.out, f"ASOS_{stn}_연도별요약_{tag}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(years[0].keys()))
        w.writeheader()
        w.writerows(years)

    say("")
    say(f"=== 지점 {stn} / {start:%Y-%m-%d} ~ {end:%Y-%m-%d} ===")
    say("연도  관측시간 결측  연강수(mm) 최대시우량 최대일강수 이벤트 시30 일150 연200 ID초과 최대이벤트(mm)")
    for y in years:
        say(f"{y['year']}  {y['obs_hours']:>6} {y['missing_hours']:>4}  {y['annual_rain_mm']:>9} "
            f"{y['max_hourly_mm']:>9} {y['max_daily_mm']:>9} {y['events']:>5} {y['n_hourly30']:>4} "
            f"{y['n_daily150']:>4} {y['n_event200']:>4} {y['n_ID_Kim2020']:>5} {y['max_event_mm']:>10}")
    if end < datetime(end.year, 12, 31, 23):
        say(f"※ {end.year}년은 부분연도({end.year}-01-01 ~ {end:%Y-%m-%d %H:%M})")
    say(f"최대 CAR {max(x['CAR_mm'] for x in daily):.1f} mm")
    with open(os.path.join(a.out, f"처리로그_{datetime.now():%Y%m%d%H%M}.txt"), "w", encoding="utf-8") as f:
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
