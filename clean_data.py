# -*- coding: utf-8 -*-
"""
현장 안전 데이터 통합/정제 파이프라인
- 입력: 안전보건조회(점검 텍스트), 안전사고현황(사고 이력)  [물량 축은 이번 단계 제외]
- 출력: 점검_clean.csv, 사고_clean.csv, 통계 요약(cleaning_report.txt)
- 목적: 위험지수/편향분석/워드클라우드/조치활동 마이닝의 공통 입력 데이터셋 생성
"""
import json
import sys
import pandas as pd
import numpy as np
import re
from pathlib import Path

# Windows 콘솔(cp949)에서 이모지·특수문자 print 시 크래시 방지
# (subprocess로 실행될 때 부모 프로세스가 PYTHONIOENCODING을 안 물려주는 경우 대비)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC = Path(r"C:\Users\윤용호\Downloads")
OUT = Path(r"C:\Users\윤용호\Desktop\나무발발이\data")
OUT.mkdir(parents=True, exist_ok=True)

# 점검 데이터: sync_gsafety.py가 관리하는 누적본이 있으면 그걸 우선 사용(주 단위 자동 최신화).
# 누적본이 아직 없으면(최초 1회) Downloads의 원본 파일로 부트스트랩.
_RAW_ACCUM = OUT / "raw_점검_누적.xlsx"
F_INSP = _RAW_ACCUM if _RAW_ACCUM.exists() else SRC / "안전보건조회-(260714).xlsx"
F_ACC  = SRC / "안전사고현황0714.xlsx"

report = []
def log(*a):
    line = " ".join(str(x) for x in a)
    report.append(line)
    print(line)

# ---------------------------------------------------------------------------
# 0) 공통 위험 분류 체계 (점검 위험요소 · 사고 상해유형을 하나의 taxonomy로 통일)
# ---------------------------------------------------------------------------
HAZARD_MAP = {
    # 추락
    "추락": "추락",
    # 전도(넘어짐/미끄러짐 포함)
    "전도": "전도", "발목접지름": "전도", "접질림": "전도", "미끄러짐": "전도",
    # 끼임·협착
    "끼임ㆍ협착": "끼임·협착", "끼임·협착": "끼임·협착", "협착": "끼임·협착", "끼임": "끼임·협착",
    # 충돌(부딪힘)
    "충돌": "충돌", "충돟": "충돌", "접축": "충돌", "부딪힘": "충돌",
    # 낙하·비래·붕괴
    "낙하ㆍ붕괴": "낙하·비래·붕괴", "낙하·붕괴": "낙하·비래·붕괴", "낙하·비래": "낙하·비래·붕괴",
    "낙하ㆍ비래": "낙하·비래·붕괴", "낙하": "낙하·비래·붕괴", "비래": "낙하·비래·붕괴", "붕괴": "낙하·비래·붕괴",
    # 감전
    "감전": "감전",
    # 화재·폭발
    "화재": "화재·폭발", "폭발": "화재·폭발", "화재·폭발": "화재·폭발",
    # 질식
    "질식": "질식",
    # 근골격/무리한동작
    "무리한동작": "무리한동작", "무리한 동작": "무리한동작", "파열": "무리한동작", "통증": "무리한동작",
    # 온열/온도
    "온도ㆍ습도": "온열·온도", "온열": "온열·온도",
    # 관리/정리
    "관리적 미흡": "관리적미흡", "관리적미흡": "관리적미흡", "정리정돈": "정리정돈",
    # 질병/사망 등
    "질병": "질병", "본인사망": "사망",
}
def map_hazard(x):
    if pd.isna(x):
        return np.nan
    s = str(x).strip().replace("\n", " ")
    return HAZARD_MAP.get(s, "기타")

# ---------------------------------------------------------------------------
# 1) 점검(안전보건조회) 정제
# ---------------------------------------------------------------------------
log("="*70)
log("[1] 점검 데이터(안전보건조회) 정제")
log("="*70)

_using_accum = (F_INSP == _RAW_ACCUM)
insp = pd.read_excel(F_INSP, header=0 if _using_accum else 1)
insp = insp[insp["코칭부서"].notna()].copy()
# '지사'는 조치부서(실제 시정조치가 필요한 대상 지사) 기준으로 정한다 — 코칭부서(코칭 수행자 소속)로
# 정하면 CSO EHS팀/그룹합동안전점검TF처럼 본사 조직이 지사를 대신 코칭한 건이 엉뚱하게 잡히거나
# (그 조직명이 '지사'로 노출) 아예 누락된다. 조치부서 기준이면 두 경우 모두 실제 대상 지사로 정확히 귀속된다.
_before_dept_filter = len(insp)
insp = insp[
    (insp["점검유형"] == "지사 주관")
    & insp["조치부서"].astype(str).str.match(r"^[가-힣]{2,3}지사$")
].copy()
if _before_dept_filter != len(insp):
    log(f" 지사주관/조치부서=OO지사 조건 불일치 제외: {_before_dept_filter} → {len(insp)}행")
log(f" 원본 유효행: {len(insp)}")

# 컬럼 정리/리네임
insp = insp.rename(columns={
    "코칭자": "점검자", "조치부서": "지사", "안전보건환경 구분": "심각도원본",
    "위험요소": "위험요소원본", "코칭일자": "점검일자", "조치일자": "조치일자",
    "코칭내용": "지적내용", "조치내용": "조치내용", "조치유형": "조치유형", "완료": "완료여부",
})

# 날짜 파싱 (2026.07.13 형식)
def parse_date(s):
    if pd.isna(s):
        return pd.NaT
    return pd.to_datetime(str(s).strip().replace(".", "-").strip("-"), errors="coerce")
insp["점검일자"] = insp["점검일자"].apply(parse_date)
insp["조치일자"] = insp["조치일자"].apply(parse_date)
insp["연월"] = insp["점검일자"].dt.to_period("M").astype(str)

# 심각도 점수화 (개선필요[하/중/상] → 1/2/3, 규정위반은 별도 상향)
def sev_score(s):
    if pd.isna(s):
        return np.nan
    s = str(s)
    if "핵심안전수칙" in s: return 4
    if "관계법규" in s:    return 4
    if "[상]" in s:        return 3
    if "[중]" in s:        return 2
    if "[하]" in s:        return 1
    return np.nan
insp["심각도점수"] = insp["심각도원본"].apply(sev_score)

# 위험분류 통일
insp["위험분류"] = insp["위험요소원본"].apply(map_hazard)

# 텍스트 정제: <지적사항>/<위험요인>/<조치요청> 태그·개행 제거한 평문 컬럼 추가(원문은 보존)
def clean_text(s):
    if pd.isna(s):
        return ""
    t = str(s)
    t = re.sub(r"<[^>]{1,20}>", " ", t)      # <지적사항> 등 태그 제거
    t = re.sub(r"\[[^\]]{1,20}\]", " ", t)   # [군산항 5부두] 등 대괄호 라벨 제거
    t = re.sub(r"\s+", " ", t).strip()
    return t
insp["지적내용_평문"] = insp["지적내용"].apply(clean_text)
insp["조치내용_평문"] = insp["조치내용"].apply(clean_text)

# 조치활동 유형 분류 (조치내용 텍스트에서 활동 동사 추출 → 통제위계)
ACTION_RULES = [
    ("설치·구조물", r"설치|난간|방호|펜스|덮개|추락방지망|고정|교체|신품|보수|정비"),
    ("제거·차단",   r"제거|철거|차단|폐기|반납|출입금지|통제|정리"),
    ("교육·지도",   r"교육|지도|주지|계도|전파|안내|경고|주의"),
    ("보호구지급",  r"안전대|안전모|보호구|지급|착용"),
    ("점검·확인",   r"확인|점검|순찰|모니터"),
]
def classify_action(s):
    if not s:
        return "미분류"
    for name, pat in ACTION_RULES:
        if re.search(pat, s):
            return name
    return "기타"
insp["조치활동분류"] = insp["조치내용_평문"].apply(classify_action)

# 사업장 추출: 'Area/공정' = "지사_ 사업장명" → 사업장명만. 구분자 없으면 값 그대로.
def extract_site(x):
    s = str(x).strip()
    if "_" in s:
        return s.split("_", 1)[1].strip()
    return s
insp["사업장"] = insp["Area/공정"].apply(extract_site)

insp_cols = ["점검일자","연월","지사","사업장","점검자","직급","점검유형","Area/공정",
             "위험분류","위험요소원본","심각도원본","심각도점수",
             "지적내용_평문","조치유형","조치활동분류","조치내용_평문","완료여부",
             "지적내용","조치내용"]
insp_out = insp[[c for c in insp_cols if c in insp.columns]].copy()
insp_out.to_csv(OUT / "점검_clean.csv", index=False, encoding="utf-8-sig")

log(f" 정제 후 행: {len(insp_out)}")
log(f" 지사({insp_out['지사'].nunique()}): " + ", ".join(sorted(insp_out['지사'].unique())))
log(f" 점검자 수: {insp_out['점검자'].nunique()}")
log(f" 기간: {insp_out['점검일자'].min().date()} ~ {insp_out['점검일자'].max().date()}")
log(" 위험분류 분포: " + str(insp_out["위험분류"].value_counts().to_dict()))
log(" 심각도 분포: " + str(insp_out["심각도원본"].value_counts().to_dict()))
log(" 조치활동분류 분포: " + str(insp_out["조치활동분류"].value_counts().to_dict()))

# ---------------------------------------------------------------------------
# 2) 사고(안전사고현황) 정제 - 전 연도 통합
# ---------------------------------------------------------------------------
log("")
log("="*70)
log("[2] 사고 데이터(안전사고현황) 정제 - 산재/일반사고 분리")
log("="*70)

xls = pd.ExcelFile(F_ACC)
year_sheets = [s for s in xls.sheet_names if re.match(r"\d{4}", str(s))]

# 사고유형1 대분류 → 재해성격
def accident_nature(t):
    if pd.isna(t):
        return np.nan
    s = str(t).replace("\n", "").strip()
    if "산재" in s or "상해" in s:
        return "인적재해"
    if "차량" in s or "화물" in s or "제3자" in s or "배상" in s:
        return "재물·차량사고"
    return "기타사고"   # 풍수해/AEO/장비/선박/비산먼지 등

rows = []
for sh in year_sheets:
    raw = pd.read_excel(F_ACC, sheet_name=sh, header=None)
    # 헤더행 탐색
    hrow = None
    for i in range(min(8, len(raw))):
        vals = [str(x) for x in raw.iloc[i].tolist()]
        if any("사고유형1" in v for v in vals):
            hrow = i; header = [str(x).strip() for x in vals]; break
    if hrow is None:
        log(f"  [{sh}] 헤더 없음 - 건너뜀"); continue
    # 헤더 중복/빈칸 유니크화
    seen = {}
    uniq = []
    for h in header:
        h = h if h and h != "nan" else "_빈칸"
        if h in seen:
            seen[h] += 1
            uniq.append(f"{h}.{seen[h]}")
        else:
            seen[h] = 0
            uniq.append(h)
    df = raw.iloc[hrow+1:].copy()
    df.columns = uniq
    df["연도시트"] = re.match(r"(\d{4})", str(sh)).group(1)
    # 관심 컬럼만 선별(중복 컬럼 문제 회피)
    keep = [c for c in ["지사점","일시","사고유형1","사고유형2","상해사고 유형","사고자",
                        "사고 개요","사고원인","조치사항","피해금액","사손금액",
                        "사고종결여부","해당 보험사","진행사항","사고제외 요청 사유","연도시트"] if c in df.columns]
    rows.append(df[keep].copy())

acc = pd.concat(rows, ignore_index=True)

# 컬럼 표준화 (존재하는 것만)
colmap = {"지사점":"지사점","일시":"일시","사고유형1":"사고유형1","사고유형2":"사고유형2",
          "상해사고 유형":"상해유형","사고자":"사고자","사고 개요":"사고개요","사고원인":"사고원인",
          "조치사항":"조치사항","피해금액":"피해금액","사손금액":"사손금액",
          "사고종결여부":"종결여부","해당 보험사":"보험사","진행사항":"진행사항"}
acc = acc.rename(columns={k:v for k,v in colmap.items() if k in acc.columns})

# 사고유형1 없는 잡행 제거
acc = acc[acc["사고유형1"].notna() & (acc["사고유형1"].astype(str).str.strip()!="")].copy()

# 날짜
acc["일시"] = pd.to_datetime(acc["일시"], errors="coerce")
acc["연월"] = acc["일시"].dt.to_period("M").astype(str)
acc["연도"] = acc["일시"].dt.year.fillna(acc["연도시트"].astype(float)).astype("Int64")

# 재해성격 분류
acc["재해성격"] = acc["사고유형1"].apply(accident_nature)

# 산재/공상 마커 (여러 컬럼에 흩어져 있어 텍스트 스캔)
scan_cols = [c for c in ["보험사","진행사항","사고제외 요청 사유"] if c in acc.columns]
def injury_class(row):
    if row["재해성격"] != "인적재해":
        return np.nan
    blob = " ".join(str(row[c]) for c in scan_cols if pd.notna(row.get(c)))
    if "산재" in blob: return "산재"
    if "공상" in blob: return "공상"
    return "미분류"
acc["산재구분"] = acc.apply(injury_class, axis=1)

# 상해유형 → 위험분류 통일
acc["위험분류"] = acc["상해유형"].apply(map_hazard) if "상해유형" in acc.columns else np.nan

# 피해금액/사손금액 숫자화
for c in ["피해금액","사손금액"]:
    if c in acc.columns:
        acc[c] = pd.to_numeric(acc[c].astype(str).str.replace(r"[^\d.]","",regex=True), errors="coerce")

# 중복 제거: '중복' 마커행 + (일시,사고자,개요앞부분) 동일행
before = len(acc)
# 명시적 중복 마커
mark_col = None
for c in acc.columns:
    if acc[c].astype(str).str.contains("중복").any():
        mark_col = c; break
if mark_col:
    acc = acc[~acc[mark_col].astype(str).str.strip().eq("중복")].copy()
# 내용 기반 중복
acc["_개요키"] = acc.get("사고개요", pd.Series([""]*len(acc))).astype(str).str.replace(r"\s+","",regex=True).str[:40]
acc["_사고자키"] = acc.get("사고자", pd.Series([""]*len(acc))).astype(str).str.replace(r"\s+","",regex=True)
acc = acc.drop_duplicates(subset=["일시","_사고자키","_개요키"], keep="first").copy()
log(f" 중복 제거: {before} → {len(acc)} ({before-len(acc)}건 제거)")

# 지사점 정제(공백) + 점검 지사명으로 매핑
# 매핑표는 코드가 아니라 data/region_mapping.json에서 관리한다 — 신규 사업장/지사점이
# 생겨도 이 파일만 고치면 되고, 코드 수정·재배포가 필요 없다.
with open(OUT / "region_mapping.json", encoding="utf-8") as f:
    REGION_TO_JISA = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

acc["지사점"] = acc["지사점"].astype(str).str.strip()
unmapped = sorted(set(acc["지사점"]) - set(REGION_TO_JISA))
if unmapped:
    log(f" ⚠️ region_mapping.json에 없는 신규 지사점 발견: {unmapped} "
        f"— data/region_mapping.json에 매핑을 추가해주세요. 우선 '(미매핑)'으로 표시합니다.")
acc["지사"] = acc["지사점"].map(REGION_TO_JISA).fillna(acc["지사점"] + "(미매핑)")

acc_cols = ["일시","연도","연월","지사","지사점","재해성격","사고유형1","사고유형2",
            "산재구분","상해유형","위험분류","사고자","피해금액","사손금액",
            "종결여부","사고개요","사고원인","조치사항"]
acc_out = acc[[c for c in acc_cols if c in acc.columns]].copy()
acc_out.to_csv(OUT / "사고_clean.csv", index=False, encoding="utf-8-sig")

log(f" 정제 후 행: {len(acc_out)}")
log(" 재해성격 분포: " + str(acc_out["재해성격"].value_counts().to_dict()))
inj = acc_out[acc_out["재해성격"]=="인적재해"]
log(f" ▶ 인적재해만: {len(inj)}건 (산재이력 가중치 대상)")
log("   - 산재구분: " + str(inj["산재구분"].value_counts().to_dict()))
log("   - 위험분류: " + str(inj["위험분류"].value_counts().to_dict()))

# ---------------------------------------------------------------------------
# 3) 지사 매핑 정합성 리포트 (점검 vs 사고)
# ---------------------------------------------------------------------------
log("")
log("="*70)
log("[3] 지사 매핑 정합성 (점검 ↔ 사고)")
log("="*70)
insp_j = set(insp_out["지사"].unique())
acc_j  = set(acc_out["지사"].unique())
log(" 점검 지사: " + ", ".join(sorted(insp_j)))
log(" 사고 지사(매핑후): " + ", ".join(sorted(acc_j)))
log(" 양쪽 매칭: " + ", ".join(sorted(insp_j & acc_j)))
log(" 점검에만 있음: " + ", ".join(sorted(insp_j - acc_j)))
log(" 사고에만/미매핑: " + ", ".join(sorted(acc_j - insp_j)))

# 저장
with open(Path(r"C:\Users\윤용호\Desktop\나무발발이") / "cleaning_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(report))
print("\n[완료] data/점검_clean.csv, data/사고_clean.csv, cleaning_report.txt 생성")
