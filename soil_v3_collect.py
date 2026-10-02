# -*- coding: utf-8 -*-
"""
토양도 기반 토양특성 상세정보 V3 수집 — PNU(필지) 단위

[기능 변화 이력]
- v1.0 (2026-10-02)
  · 농촌진흥청 국립농업과학원 「토양도 기반 토양특성 상세 정보 V3」 getSoilCharacter 를 PNU(19자리)로 호출 (요청변수 PNU_CD)
  · 인증키는 코드에 넣지 않음 — 환경변수 DATAGOKR_KEY 또는 스크립트 옆 datagokr_key.txt
  · 개발계정 일일 호출 한도 대응: --daily-limit(기본 950)에서 멈추고, 다음 날 재실행하면 이어받기
  · HTTP 429 대기 후 재시도, 인증 실패 즉시 중단, 호출 간격 1초
  · 산출: 토양특성 CSV(27종 코드 + 한글 해석: 배수등급·유효토심·침식등급·임지적성등급·임지저해요인), 필드설명.csv, 무자료 PNU 목록, 수집로그
  · QGIS Python Console 실행 지원, 기본 경로는 스크립트 폴더 기준 ./data/…

[PNU 목록 준비]
  · 연속지적도(LSMD_CONT_LDREG) 속성을 CSV로 내보내 --pnu-file 로 지정
    (QGIS: Layer ▸ Export ▸ Save Features As…(다른 이름으로 저장) ▸ CSV)
  · PNU 컬럼명이 PNU / pnu / A1 / PNU_Cd 중 하나면 자동 인식, 아니면 --pnu-col 로 지정
  · --prefix <시군구코드 5자리> 로 대상 지역만 거르기, --mountain-only 를 주면 산번지(11번째 자리=2)만

[사용법]
  python soil_v3_collect.py --pnu-file ./data/PNU목록.csv [--prefix <시군구코드>] [--mountain-only]
  외부 패키지 불필요(표준 라이브러리만 사용)
"""
import argparse, csv, json, os, sys, time
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime

ENDPOINT = "https://apis.data.go.kr/1390802/SoilEnviron/SoilCharac/V3/getSoilCharacter"
CALL_INTERVAL = 1.0   # 호출 간격(초) — 게이트웨이 초당 제한 회피
PARAM_CANDIDATES = ["PNU_CD"]   # 기술명세서 V3 ver1.0 확인값

FIELDS = [
    "PNU_Cd", "Soildra_Cd", "Vldsoildep_Cd", "Erosion_Cd", "Surtture_Cd", "Sur_Ston_Cd",
    "Soil_Color_Cd", "Soil_Structure_Cd", "Deepsoil_Color_Cd", "Matrix_Cd",
    "Soil_Type_Geo_Cd", "Accu_Style_Cd", "Main_Order_Cd", "Sub_Order_Cd", "Grategroup_Cd",
    "Main_Landuse_Cd", "Soil_Use_Rec_Cd", "Soil_Type_Cd",
    "Rfld_Grd_Cd", "Paddy_Factor_Cd", "Pfld_Grd_Cd", "Upland_Factor_Cd",
    "Fruit_Grd_Cd", "Fruit_Factor_Cd", "Pasture_Grd_Cd", "Grass_Factor_Cd",
    "Frst_Grd_Cd", "Forest_Factor_Cd",
]
FIELD_KO = {
    "PNU_Cd": "지번코드", "Soildra_Cd": "배수등급코드", "Vldsoildep_Cd": "유효토심코드",
    "Erosion_Cd": "침식등급코드", "Surtture_Cd": "표토토성코드", "Sur_Ston_Cd": "표토자갈함량코드",
    "Soil_Color_Cd": "토색코드", "Soil_Structure_Cd": "구조코드", "Deepsoil_Color_Cd": "심토주토색코드",
    "Matrix_Cd": "모암(모재)코드", "Soil_Type_Geo_Cd": "분포지형코드", "Accu_Style_Cd": "퇴적양식코드",
    "Main_Order_Cd": "토양목코드", "Sub_Order_Cd": "토양아목코드", "Grategroup_Cd": "토양대군코드",
    "Main_Landuse_Cd": "주토지이용코드", "Soil_Use_Rec_Cd": "토지이용추천코드", "Soil_Type_Cd": "토양유형코드",
    "Rfld_Grd_Cd": "논적성등급코드", "Paddy_Factor_Cd": "논저해요인코드", "Pfld_Grd_Cd": "밭적성등급코드",
    "Upland_Factor_Cd": "밭저해요인코드", "Fruit_Grd_Cd": "과수상전적성등급코드",
    "Fruit_Factor_Cd": "과수상전저해요인코드", "Pasture_Grd_Cd": "초지적성등급코드",
    "Grass_Factor_Cd": "초지저해요인코드", "Frst_Grd_Cd": "임지적성등급코드",
    "Forest_Factor_Cd": "임지저해요인코드",
}
# 기술명세서 3.1.5~3.1.7, 3.1.26~27 코드표
CODE_LABEL = {
    "Soildra_Cd": {"01": "매우양호", "02": "양호", "03": "약간양호", "04": "약간불량", "05": "불량", "06": "매우불량", "99": "기타"},
    "Vldsoildep_Cd": {"01": "매우얕음_0-25cm", "02": "얕음_25-50cm", "03": "보통_50-100cm", "04": "깊음_100cm이상", "99": "기타"},
    "Erosion_Cd": {"01": "없음", "02": "있음", "03": "심함", "04": "매우심함", "99": "기타"},
    "Frst_Grd_Cd": {"01": "임지_1급지", "02": "임지_2급지", "03": "임지_3급지", "04": "임지_4급지", "05": "임지_5급지", "99": "기타"},
    "Forest_Factor_Cd": {"00": "제외", "01": "없음", "02": "경사", "03": "저습", "04": "사질", "05": "석력", "09": "중점",
                         "10": "경반", "11": "암반", "12": "침식", "13": "화산회", "14": "분석", "99": "기타"},
}
LABEL_COLS = {"Soildra_Cd": "배수등급", "Vldsoildep_Cd": "유효토심", "Erosion_Cd": "침식등급",
              "Frst_Grd_Cd": "임지적성등급", "Forest_Factor_Cd": "임지저해요인"}
FATAL = ("KEY_AUTH_FAIL", "SERVICE_KEY_IS_NOT_REGISTERED", "SERVICE_ACCESS_DENIED", "LIMITED_NUMBER_OF_SERVICE_REQUESTS",
         "DEADLINE_HAS_EXPIRED", "UNREGISTERED_IP")


def _script_dir():
    """스크립트 폴더 — QGIS 편집기 실행(exec)에서는 __file__ 이 없으므로 컴파일 파일명으로 대체"""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        import inspect
        return os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename))


def load_key():
    key = os.environ.get("DATAGOKR_KEY", "").strip()
    if not key:
        here = _script_dir()
        for name in ("datagokr_key.txt", "asos_key.txt"):
            p = os.path.join(here, name)
            if os.path.exists(p):
                with open(p, encoding="utf-8-sig") as f:
                    key = f.read().strip()
                break
    if not key:
        sys.exit("[중단] 인증키가 없습니다. 스크립트 옆에 datagokr_key.txt(키 한 줄)를 만들어 주세요.")
    return urllib.parse.unquote(key) if "%" in key else key


class Fatal(Exception):
    pass


def request(key, param, pnu, retries=3):
    url = ENDPOINT + "?" + urllib.parse.urlencode({"serviceKey": key, param: pnu})
    last = ""
    for k in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                txt = r.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace").strip()[:300]
            if e.code == 429:
                # 게이트웨이 호출 제한 — 짧게 재시도하면 더 막힘. 길게 쉬고 재시도, 계속이면 중단
                last = f"HTTP 429 Too Many Requests / 응답: {body or '(본문 없음)'}"
                if k < retries:
                    wait = 60 * k
                    print(f"  HTTP 429 — {wait}초 대기 후 재시도({k}/{retries - 1})")
                    time.sleep(wait)
                    continue
                raise Fatal(last)
            if e.code in (401, 403):
                raise Fatal(f"HTTP {e.code} 권한 오류 / 응답: {body}")
            last = f"HTTP {e.code} / 응답: {body}"
            time.sleep(3 * k)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(e)
            time.sleep(3 * k)
            continue
        if any(f in txt for f in FATAL):
            raise Fatal(txt[:300])
        try:
            root = ET.fromstring(txt)
        except ET.ParseError:
            if txt.startswith("{"):
                return parse_json(txt), txt
            last = "파싱 실패: " + txt[:200]
            time.sleep(3 * k)
            continue
        items = []
        for it in root.iter("item"):
            items.append({c.tag: (c.text or "").strip() for c in it})
        return items, txt
    raise RuntimeError(f"재시도 실패 — {last}")


def parse_json(txt):
    d = json.loads(txt)
    body = d.get("response", d).get("body", {})
    it = body.get("items", {})
    it = it.get("item", []) if isinstance(it, dict) else it
    return [it] if isinstance(it, dict) else (it or [])


def read_pnus(path, col, prefix, mountain_only):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rd = csv.reader(f, dialect)
        header = next(rd)
        if col:
            idx = header.index(col)
        else:
            cand = [i for i, h in enumerate(header) if h.strip().upper() in ("PNU", "A1", "PNU_CD")]
            if not cand:
                sys.exit(f"[중단] PNU 컬럼을 못 찾았습니다. 헤더: {header[:15]} → --pnu-col 로 지정하세요.")
            idx = cand[0]
        out, seen = [], set()
        for row in rd:
            if idx >= len(row):
                continue
            p = row[idx].strip().split(".")[0]
            if len(p) != 19 or not p.isdigit():
                continue
            if prefix and not p.startswith(prefix):
                continue
            if mountain_only and p[10] != "2":
                continue
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pnu-file", default="")
    ap.add_argument("--pnu-col", default=None)
    ap.add_argument("--prefix", default="")   # 시군구 행정코드 앞자리로 거르기(예: 11110), 빈칸이면 전체
    ap.add_argument("--mountain-only", action="store_true")
    ap.add_argument("--daily-limit", type=int, default=950)
    ap.add_argument("--param", default="PNU_CD", help="PNU 파라미터명(모르면 비워두면 자동 판별)")
    ap.add_argument("--out", default=os.path.join(_script_dir(), "data", "토양특성V3"))
    a = ap.parse_args(argv)

    if not a.pnu_file or not os.path.exists(a.pnu_file):
        sys.exit("[중단] PNU 목록 CSV가 없습니다. --pnu-file 로 지정하세요"
                 " (QGIS 편집기 실행이면 파일 맨 아래 QGIS_ARGS 의 경로를 채우세요).")
    os.makedirs(a.out, exist_ok=True)
    state_p = os.path.join(a.out, "_state.json")
    res_p = os.path.join(a.out, "_results.jsonl")
    state = {"param": None, "done": [], "nodata": [], "calls": {}}
    if os.path.exists(state_p):
        with open(state_p, encoding="utf-8") as f:
            state.update(json.load(f))
    done, nodata = set(state["done"]), set(state["nodata"])
    today = datetime.now().strftime("%Y%m%d")
    calls = state["calls"].get(today, 0)

    def save_state():
        state["done"], state["nodata"] = sorted(done), sorted(nodata)
        state["calls"][today] = calls
        with open(state_p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

    key = load_key()
    pnus = read_pnus(a.pnu_file, a.pnu_col, a.prefix, a.mountain_only)
    todo = [p for p in pnus if p not in done and p not in nodata]
    print(f"대상 PNU {len(pnus):,}건 / 완료 {len(done):,} / 무자료 {len(nodata):,} / 남음 {len(todo):,}")
    print(f"오늘({today}) 사용 {calls}/{a.daily_limit}건")
    if todo:
        print(f"남은 건수 기준 약 {-(-len(todo) // a.daily_limit)}일 소요 (개발계정 한도 기준)")

    param = a.param or state.get("param")
    try:
        # 파라미터명 자동 판별 — 자료가 나오는 첫 조합을 채택
        raw = ""
        if not param:
            probe = todo[:5]
            for cand in PARAM_CANDIDATES:
                hit = False
                for p in probe:
                    if calls >= a.daily_limit:
                        break
                    items, raw = request(key, cand, p)
                    calls += 1
                    time.sleep(CALL_INTERVAL)
                    if items:
                        param, hit = cand, True
                        break
                if hit:
                    break
            if not param:
                save_state()
                sys.exit("[중단] 파라미터명을 판별하지 못했습니다. 기술명세서(hwp)의 요청변수명을 --param 으로 주세요.\n"
                         f"  마지막 응답 앞부분: {raw[:300]}")
            state["param"] = param
            print(f"PNU 파라미터명 판별: {param}")

        with open(res_p, "a", encoding="utf-8") as fo:
            for i, p in enumerate(todo, 1):
                if calls >= a.daily_limit:
                    print(f"\n[일일 한도 도달] 오늘 {calls}건 사용 — 내일 같은 명령으로 다시 실행하면 이어받습니다.")
                    break
                try:
                    items, _ = request(key, param, p)
                except RuntimeError as e:
                    print(f"  {p} 실패(건너뜀): {e}")
                    calls += 1
                    continue
                calls += 1
                if items:
                    for it in items:
                        it.setdefault("PNU_Cd", p)
                        it["_query_pnu"] = p
                        fo.write(json.dumps(it, ensure_ascii=False) + "\n")
                    done.add(p)
                else:
                    nodata.add(p)
                if i % 50 == 0:
                    fo.flush()
                    save_state()
                    print(f"  진행 {i:,}/{len(todo):,}  (오늘 {calls}건)")
                time.sleep(CALL_INTERVAL)
    except Fatal as e:
        print(f"[중단] 인증/한도 오류: {e}")
        if "429" in str(e):
            print("  → 데이터포털 게이트웨이가 호출을 막고 있습니다. 같은 키로 포털 '미리보기'도 429면 서버측 제한"
                  "(승인 직후 미반영/일일한도)이므로 시간을 두고 다시 실행하세요. 받은 건 저장돼 이어받습니다.")
    finally:
        save_state()

    # 누적 결과 → CSV
    rows = []
    if os.path.exists(res_p):
        with open(res_p, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    extra = sorted({k for r in rows for k in r} - set(FIELDS) - {"_query_pnu"} - set(LABEL_COLS.values()))
    for r in rows:
        for c, lab in LABEL_COLS.items():
            r[lab] = CODE_LABEL[c].get(str(r.get(c, "")).strip(), "")
    cols = ["_query_pnu"] + list(LABEL_COLS.values()) + FIELDS + extra
    out_csv = os.path.join(a.out, "토양특성V3_PNU.csv")
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    with open(os.path.join(a.out, "필드설명.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["필드", "설명"])
        w.writerow(["_query_pnu", "조회에 쓴 PNU(QGIS 조인 키)"])
        for c in FIELDS:
            w.writerow([c, FIELD_KO.get(c, "")])
    with open(os.path.join(a.out, "무자료_PNU.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(nodata)))
    from collections import Counter
    dra = Counter(r.get("Soildra_Cd", "") for r in rows)
    summary = [
        f"[{datetime.now():%Y-%m-%d %H:%M}] 파라미터 {param} / 누적 자료 {len(done):,}필지({len(rows):,}행) / 무자료 {len(nodata):,}",
        f"남은 PNU {len([p for p in pnus if p not in done and p not in nodata]):,}건",
        "배수등급 분포: " + ", ".join(f"{k or '공란'}({CODE_LABEL['Soildra_Cd'].get(k, '')})={v}" for k, v in sorted(dra.items())),
    ]
    print("\n".join(summary))
    with open(os.path.join(a.out, "수집로그.txt"), "a", encoding="utf-8") as f:
        f.write("\n".join(summary) + "\n")
    print(f"\n저장 위치: {a.out}")


# ---------------------------------------------------------------- 실행 진입점
# QGIS Python Console 편집기의 ▶(Run Script)는 __name__ 이 "__main__" 이 아니어서
# 그대로 두면 아무것도 실행되지 않음 → QGIS 안이면 아래 QGIS_ARGS 로 바로 실행
QGIS_ARGS = [
    "--pnu-file", os.path.join(_script_dir(), "data", "PNU목록.csv"),
    # "--prefix", "11110",      # 대상 시군구 코드로 거르기(선택)
]

_IN_QGIS = "qgis" in sys.modules
if __name__ == "__main__" or _IN_QGIS:
    try:
        main(QGIS_ARGS if (_IN_QGIS and __name__ != "__main__") else None)
    except SystemExit as _e:
        if _e.code not in (None, 0):
            print(_e.code)
