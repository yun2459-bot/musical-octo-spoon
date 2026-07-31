# -*- coding: utf-8 -*-
"""
안전 위험지수 스코어링 & 분석 로직
- 설계 원칙(사용자 결정 반영):
  * 위험지수는 '점검 심각도' 기반으로만 산출 (과거 산재이력은 점수에 곱하지 않음)
  * 과거 인적재해는 '경보 임계값을 낮추는' 조정 요소로만 사용 (임계값 분리)
  * 물량 축은 이번 단계 제외
"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA = Path(__file__).parent / "data"

# 분석 대상 위험분류(관리적미흡/정리정돈/기타 등 비-물리위험은 스코어에서 약하게)
PHYSICAL_HAZARDS = ["추락", "전도", "끼임·협착", "충돌", "낙하·비래·붕괴",
                    "감전", "화재·폭발", "질식", "무리한동작", "온열·온도"]

# 갭분석용 확장 위험분류 (인적재해엔 드물지만 사고자료엔 많은 화재·풍수해 포함)
GAP_HAZARDS = PHYSICAL_HAZARDS + ["풍수해"]

# 위험분류 특성 — 해석/권장활동 분기용
#  질병성  : 지연발현(작업 후 질병 판정) → 현장 실시간 점검으로 포착 어려움, 별도관리 필요
#  기상재해: 화재·풍수 등 사전대비 점검 대상
HAZARD_TRAIT = {
    "무리한동작": "질병성", "질병": "질병성",
    "풍수해": "기상재해", "온열·온도": "기상재해",
    "화재·폭발": "중대사고",
}

FIRE_PAT = "화재|폭발|발화|연소|화염|불꽃"
WEATHER_PAT = "풍수|태풍|호우|침수|강풍|폭우|폭설|해일|낙뢰|동파"

# 질병성(지연발현) 위험분류 — 편향/갭 분석에서 제외 대상
DISEASE_HAZARDS = [h for h, t in HAZARD_TRAIT.items() if t == "질병성"]


def safety_accident_hazard(acc):
    """안전점검과 비교할 '사고 위험분류' 산출.
    - 인적재해: 상해유형 기반 위험분류 사용
    - 화재·폭발/풍수해: 인적재해가 아니어도(재물·기타사고 포함) 키워드로 포착 → 점검 대상이므로 포함
    반환: 사고 DataFrame + '점검관련위험' 컬럼 (해당 없으면 제외)"""
    df = acc.copy()
    blob = (df["사고유형1"].astype(str) + " " + df.get("사고유형2", "").astype(str)
            + " " + df["사고개요"].astype(str))
    is_fire = blob.str.contains(FIRE_PAT, na=False)
    is_weather = blob.str.contains(WEATHER_PAT, na=False)
    hz = pd.Series(pd.NA, index=df.index, dtype=object)
    inj = df["재해성격"] == "인적재해"
    hz[inj] = df.loc[inj, "위험분류"]
    hz[(~inj) & is_fire] = "화재·폭발"
    hz[(~inj) & is_weather & hz.isna()] = "풍수해"
    df["점검관련위험"] = hz
    return df[df["점검관련위험"].notna()].copy()


# ---------------------------------------------------------------------------
def load_data():
    insp = pd.read_csv(DATA / "점검_masked.csv", parse_dates=["점검일자"])
    acc = pd.read_csv(DATA / "사고_masked.csv", parse_dates=["일시"])
    insp["심각도점수"] = pd.to_numeric(insp["심각도점수"], errors="coerce").fillna(1)

    # LLM 하이브리드 심각도 병합 — (지사,점검일자,지적내용_평문) 안정 키로 병합.
    # 위치(행번호) 조인이 아니라 키 조인이라, 매주 신규 점검이 쌓여 행수가 달라져도
    # 과거 파일럿 판정(195건 직접판정 등)이 올바른 원본 행에 계속 붙는다.
    # 신규 행(파일럿 이후 추가된 점검)은 자동으로 '미적용'(=규칙기반 값 그대로)이 된다.
    hybrid_path = DATA / "점검_LLM하이브리드.csv"
    insp["_join_key"] = (insp["지사"].astype(str) + "|" + insp["점검일자"].astype(str)
                         + "|" + insp["지적내용_평문"].astype(str))
    if hybrid_path.exists():
        hyb = pd.read_csv(hybrid_path, parse_dates=["점검일자"])
        hyb["_join_key"] = (hyb["지사"].astype(str) + "|" + hyb["점검일자"].astype(str)
                            + "|" + hyb["지적내용_평문"].astype(str))
        hyb = hyb.drop_duplicates(subset="_join_key", keep="first")
        keep = hyb.set_index("_join_key")[["LLM심각도", "판정방식", "위험분류_재검토", "근거"]]
        insp = insp.join(keep, on="_join_key")
        insp["LLM심각도"] = pd.to_numeric(insp["LLM심각도"], errors="coerce").fillna(insp["심각도점수"])
        insp["판정방식"] = insp["판정방식"].fillna("미적용(신규점검)")
        insp = insp.rename(columns={"근거": "LLM근거"})
    else:
        insp["LLM심각도"] = insp["심각도점수"]
        insp["판정방식"] = "미적용"
        insp["위험분류_재검토"] = None
        insp["LLM근거"] = None
    insp = insp.drop(columns=["_join_key"])

    return insp, acc


# ---------------------------------------------------------------------------
def branch_risk_index(insp, period=None, severity_col="심각도점수"):
    """지사별 위험지수(0-100)와 정규화 변형.
    - 총량지수  : 점검 심각도 가중합(전 지사 최대 대비) — 원지표
    - 사업장당지수: 심각도합 ÷ 사업장수 (사업장 보유 격차 보정)
    - 관리자당지수: 심각도합 ÷ 안전관리자(점검자)수 (인력 격차 보정)
    period: (start, end) tuple, None이면 전체.
    severity_col: "심각도점수"(규칙기반) 또는 "LLM심각도"(하이브리드 파일럿)."""
    df = insp
    if period:
        s, e = period
        df = df[(df["점검일자"] >= s) & (df["점검일자"] <= e)]
    raw = df.groupby("지사")[severity_col].sum()
    if raw.empty:
        return pd.DataFrame(columns=["지사", "위험지수", "지적건수", "가중점수", "평균심각도",
                                     "안전관리자수", "사업장수", "사업장당지수", "관리자당지수"])
    cnt = df.groupby("지사").size()
    avg = df.groupby("지사")[severity_col].mean()
    mgr = df.groupby("지사")["점검자"].nunique()
    site = df.groupby("지사")["사업장"].nunique()

    def norm(series):
        return (100 * series / series.max()).round(1)

    per_site = raw / site.replace(0, 1)
    per_mgr = raw / mgr.replace(0, 1)
    out = pd.DataFrame({
        "지사": raw.index, "가중점수": raw.values,
        "지적건수": cnt.reindex(raw.index).values,
        "평균심각도": avg.reindex(raw.index).round(2).values,
        "안전관리자수": mgr.reindex(raw.index).values,
        "사업장수": site.reindex(raw.index).values,
        "위험지수": norm(raw).values,
        "사업장당지수": norm(per_site).reindex(raw.index).values,
        "관리자당지수": norm(per_mgr).reindex(raw.index).values,
    })
    return out.sort_values("위험지수", ascending=False).reset_index(drop=True)


def branch_risk_trend(insp, severity_col="심각도점수"):
    """지사×월 위험지수 추이(꺾은선용). 각 월의 전지사 최대값 대비 정규화."""
    df = insp.dropna(subset=["점검일자"]).copy()
    df["연월"] = df["점검일자"].dt.to_period("M").astype(str)
    g = df.groupby(["연월", "지사"])[severity_col].sum().reset_index()
    # 월별 정규화(월 내 최대 대비) → 계절/월별 상대위험
    g["위험지수"] = g.groupby("연월")[severity_col].transform(lambda x: 100 * x / x.max())
    return g


# ---------------------------------------------------------------------------
def past_injury_matrix(acc):
    """지사×위험분류 과거 인적재해 건수 (재물·차량사고 제외). 산재>공상 가중."""
    inj = acc[acc["재해성격"] == "인적재해"].copy()
    # 산재=1.0, 공상=0.6, 미분류=0.8 가중
    w = {"산재": 1.0, "공상": 0.6, "미분류": 0.8}
    inj["가중"] = inj["산재구분"].map(w).fillna(0.8)
    mat = inj.groupby(["지사", "위험분류"])["가중"].sum().unstack(fill_value=0)
    cnt = inj.groupby(["지사", "위험분류"]).size().unstack(fill_value=0)
    return mat, cnt


# ---------------------------------------------------------------------------
def hazard_intensity(insp, period=None, severity_col="심각도점수"):
    """지사×위험분류 점검강도(심각도 가중합)."""
    df = insp
    if period:
        s, e = period
        df = df[(df["점검일자"] >= s) & (df["점검일자"] <= e)]
    return df.groupby(["지사", "위험분류"])[severity_col].sum().unstack(fill_value=0)


def compute_alerts(insp, acc, base_pct=75, hist_relief=15, recent_days=90, severity_col="심각도점수"):
    """경보 산출: 지사×위험분류.
    - 점검강도 백분위(위험분류 내 전지사 대비)
    - 과거 인적재해가 있으면 임계 백분위를 최대 hist_relief 만큼 낮춤(민감도↑)
    - 최근 recent_days 기간 기준
    반환: 경보 DataFrame (지사, 위험분류, 점검강도, 백분위, 조정임계, 과거재해, 경보등급)
    """
    end = insp["점검일자"].max()
    start = end - pd.Timedelta(days=recent_days)
    inten = hazard_intensity(insp, (start, end), severity_col=severity_col)  # 지사×위험분류
    injw, injc = past_injury_matrix(acc)

    rows = []
    for hz in inten.columns:
        if hz not in PHYSICAL_HAZARDS:
            continue
        col = inten[hz]
        # 백분위(0-100): 값이 클수록 상위
        ranks = col.rank(pct=True) * 100
        for 지사, val in col.items():
            if val <= 0:
                continue
            pct = ranks[지사]
            past = injc[hz][지사] if (hz in injc.columns and 지사 in injc.index) else 0
            relief = hist_relief * min(1.0, past / 3.0)   # 과거재해 3건이면 최대 완화
            thr = base_pct - relief
            if pct >= thr:
                if pct >= 90 or (pct >= 80 and past >= 1):
                    grade = "심각"
                elif pct >= thr + 5:
                    grade = "경보"
                else:
                    grade = "주의"
                rows.append({"지사": 지사, "위험분류": hz, "점검강도": round(val, 1),
                             "백분위": round(pct, 0), "조정임계": round(thr, 0),
                             "과거재해": int(past), "경보등급": grade})
    df = pd.DataFrame(rows)
    if not df.empty:
        order = {"심각": 0, "경보": 1, "주의": 2}
        df["_o"] = df["경보등급"].map(order)
        df = df.sort_values(["_o", "점검강도"], ascending=[True, False]).drop(columns="_o")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
def inspector_bias(insp, min_count=10, exclude_disease=True):
    """점검자별 위험분류 분포 다양성(엔트로피).
    엔트로피 낮음 = 특정 위험에 쏠림(편향 가능성). 질병성 위험분류는 기본 제외."""
    cats = PHYSICAL_HAZARDS + ["관리적미흡", "정리정돈"]
    if exclude_disease:
        cats = [c for c in cats if c not in DISEASE_HAZARDS]
    df = insp[insp["위험분류"].isin(cats)]
    rows = []
    for 점검자, g in df.groupby("점검자"):
        n = len(g)
        if n < min_count:
            continue
        p = g["위험분류"].value_counts(normalize=True)
        ent = -(p * np.log2(p)).sum()
        max_ent = np.log2(len(p)) if len(p) > 1 else 1
        norm_ent = ent / max_ent if max_ent > 0 else 0
        top_hz = p.index[0]
        top_share = p.iloc[0]
        rows.append({"점검자": 점검자, "점검건수": n, "지사": g["지사"].mode().iloc[0],
                     "다양성지수": round(norm_ent, 2),
                     "최다위험": top_hz, "최다비중": round(top_share * 100, 0),
                     "다룬위험종류": len(p)})
    out = pd.DataFrame(rows).sort_values("다양성지수").reset_index(drop=True)
    return out


def inspector_detail(insp, 점검자):
    """특정 점검자의 '왜 편향인가' 근거: 위험분류 분포 + 사업장별 점검 분포."""
    g = insp[insp["점검자"] == 점검자]
    hz = g[g["위험분류"].isin(PHYSICAL_HAZARDS + ["관리적미흡", "정리정돈"])]["위험분류"].value_counts()
    site = g.groupby("사업장").size().sort_values(ascending=False)
    # 소외 위험분류(전혀 안 다룬 물리위험, 질병성 제외)
    covered = set(g["위험분류"].unique())
    missing = [h for h in PHYSICAL_HAZARDS if h not in covered and h not in DISEASE_HAZARDS]
    지사 = g["지사"].mode().iloc[0] if not g.empty else ""
    return {"지사": 지사, "위험분류분포": hz, "사업장분포": site,
            "소외위험": missing, "점검건수": len(g)}


def blind_spots(insp, acc, recent_days=180, exclude_disease=False):
    """지사별 사각지대: 과거 인적재해가 있었는데 최근 점검에서 거의 안 다룬 위험분류.
    exclude_disease=True 이면 질병성(근골격 등) 행 제외."""
    end = insp["점검일자"].max()
    start = end - pd.Timedelta(days=recent_days)
    recent = insp[(insp["점검일자"] >= start)]
    inspected = recent.groupby(["지사", "위험분류"]).size().unstack(fill_value=0)
    _, injc = past_injury_matrix(acc)
    rows = []
    for 지사 in injc.index:
        if 지사 not in inspected.index:
            insp_row = pd.Series(0, index=inspected.columns) if not inspected.empty else pd.Series(dtype=int)
        else:
            insp_row = inspected.loc[지사]
        for hz in injc.columns:
            if hz not in PHYSICAL_HAZARDS:
                continue
            past = injc.loc[지사, hz]
            seen = insp_row.get(hz, 0) if len(insp_row) else 0
            trait = HAZARD_TRAIT.get(hz, "일반")
            if trait == "질병성":
                if exclude_disease:
                    continue
                # 근골격 등 지연발현 질병성 재해: 현장 실시간 점검으로 포착 어려움 → 별도관리 대상
                if past >= 2:
                    rows.append({"지사": 지사, "위험분류": hz, "과거재해": int(past),
                                 "최근점검": int(seen), "특성": "질병성",
                                 "상태": "🟣 질병성(별도관리)"})
            elif past >= 2 and seen == 0:
                rows.append({"지사": 지사, "위험분류": hz, "과거재해": int(past),
                             "최근점검": int(seen), "특성": trait, "상태": "🔴 사각지대"})
            elif past >= 3 and seen <= 1:
                rows.append({"지사": 지사, "위험분류": hz, "과거재해": int(past),
                             "최근점검": int(seen), "특성": trait, "상태": "🟡 점검부족"})
    return pd.DataFrame(rows).sort_values("과거재해", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
def generate_insights(insp, acc, 지사=None):
    """편향분석 기반 실행 인사이트 자동 도출 → [{제목, 근거, 권장활동, 유형}] 리스트.
    지사 지정 시 그 지사에 특화된 인사이트를 반환."""
    if 지사:
        return _branch_insights(insp, acc, 지사)
    out = []
    gap = coverage_gap(insp, acc)

    # 1) 질병성(근골격) — 현장점검 사각지대가 아니라 특성상 별도관리 대상
    if "무리한동작" in gap.index:
        r = gap.loc["무리한동작"]
        out.append({
            "유형": "질병성", "제목": "근골격계(무리한동작) — 점검 사각지대 아닌 '질병성' 재해",
            "근거": f"사고비중 {r['사고비중']}% vs 점검비중 {r['점검비중']}%. "
                    "근골격계는 작업 중 즉시 부상보다 작업 이후 질병으로 판정되는 지연발현형이라 "
                    "현장 실시간 점검으로는 포착이 어렵습니다(점검 부족을 탓할 사안이 아님).",
            "권장활동": "① 근골격계 유해요인조사(정기) ② 인간공학적 작업대·도구 개선 "
                     "③ 작업 전 스트레칭·순환근무 ④ 중량물 기계화·2인1조 표준화",
        })

    # 2) 화재·폭발 — 인적재해엔 드물어도 사고자료엔 다수 → 점검 정당
    acch = safety_accident_hazard(acc)
    fire_n = int((acch["점검관련위험"] == "화재·폭발").sum())
    if fire_n > 0:
        out.append({
            "유형": "중대사고", "제목": "화재·폭발 — 점검 지속이 정당(과다점검 아님)",
            "근거": f"인적재해로는 거의 안 나타나지만 사고자료 전체에서 화재·폭발 {fire_n}건 발생. "
                    "현재 활발한 화재 점검은 실제 사고를 예방하는 정당한 활동입니다.",
            "권장활동": "① 화기작업 허가제(Hot Work Permit) 운영 ② 소화설비·가스용기 정기점검 유지 "
                     "③ 인화물 격리·보관 기준 점검",
        })

    # 3) 풍수해 — 사고 다수지만 점검 항목 부재 → 사전대비 점검 신설
    weather_n = int((acch["점검관련위험"] == "풍수해").sum())
    w_insp = int((insp["위험분류"] == "풍수해").sum())
    if weather_n >= 5 and w_insp == 0:
        out.append({
            "유형": "기상재해", "제목": "풍수해 — 사고는 많은데 점검 항목이 없음(신설 필요)",
            "근거": f"사고자료에서 풍수해 관련 {weather_n}건 발생했으나 현장 점검 항목에는 부재. "
                    "산업재해(인적)로 잡히지 않아 그동안 점검 사각에 있었습니다.",
            "권장활동": "① 우기/동절기 사전점검 체크리스트 신설 ② 배수·결빙·강풍 대비 설비점검 "
                     "③ 기상특보 연동 작업중지 기준 마련",
        })

    # 4) 진짜 사각지대 — 과거 인적재해 있고 점검 0인 일반 물리위험
    bs = blind_spots(insp, acc)
    if not bs.empty:
        real = bs[(bs["상태"] == "🔴 사각지대") & (bs["특성"] != "질병성")]
        real = real[real["지사"].isin(insp["지사"].unique())]
        if not real.empty:
            top = real.head(4)
            items = ", ".join(f"{r['지사']}·{r['위험분류']}(과거 {r['과거재해']}건)" for _, r in top.iterrows())
            out.append({
                "유형": "사각지대", "제목": "실질 사각지대 — 과거 재해 있으나 최근 점검 0",
                "근거": f"질병성을 제외한 물리적 위험 중 재발 위험 구간: {items}",
                "권장활동": "① 해당 지사·위험분류 특별점검 즉시 지시 ② 재발방지 TBM 배포 "
                         "③ 다음 분기 점검계획에 필수 항목 반영",
            })

    # 5) 점검자 편향 — 다양성 낮은 점검자
    bias = inspector_bias(insp)
    if not bias.empty:
        low = bias[bias["다양성지수"] <= bias["다양성지수"].quantile(0.25)]
        if not low.empty:
            names = ", ".join(f"{r['점검자']}({r['최다위험']} {r['최다비중']:.0f}%)" for _, r in low.head(3).iterrows())
            out.append({
                "유형": "편향", "제목": "점검자 편향 — 특정 위험에 쏠린 점검자",
                "근거": f"다양성지수 하위 점검자: {names}. 특정 위험분류에 집중되어 다른 위험을 놓칠 수 있습니다.",
                "권장활동": "① 점검 체크리스트 표준화로 누락 위험 강제 확인 ② 지사 간 교차점검 "
                         "③ 점검자 대상 소외 위험분류(예: 감전·질식) 교육",
            })
    return out


def _branch_insights(insp, acc, 지사):
    """지사 특화 인사이트: 그 지사의 주력 위험·재발위험·사각지대·과거재해 특징 반영."""
    out = []
    dfb = insp[insp["지사"] == 지사]
    if dfb.empty:
        return [{"유형": "정보", "제목": f"{지사} — 점검 데이터 없음",
                 "근거": "해당 지사의 점검 기록이 없습니다.", "권장활동": "점검 데이터 확보 필요"}]

    ri = branch_risk_index(insp)
    row = ri[ri["지사"] == 지사]
    # 0) 위험지수 프로필(총량 vs 정규화)
    if not row.empty:
        r = row.iloc[0]
        site_i, mgr_i, tot_i = r['사업장당지수'], r['관리자당지수'], r['위험지수']
        if site_i > tot_i + 15:
            msg = f"사업장 {int(r['사업장수'])}개로 적은데도 사업장당 위험({site_i:.0f})이 높음 → 사업장별 밀도 높음, 실질 집중관리 필요."
        elif tot_i > site_i + 20:
            msg = f"사업장 {int(r['사업장수'])}개에 분산되어 사업장당({site_i:.0f})으로는 낮은 편 → 총량 순위는 규모효과 감안 필요."
        elif mgr_i > tot_i + 15:
            msg = f"안전관리자 {int(r['안전관리자수'])}명 대비 부담(관리자당 {mgr_i:.0f})이 커 인력 보강 검토 필요."
        else:
            msg = "총량과 규모정규화 지수가 비슷해 위험도 해석이 안정적입니다."
        out.append({
            "유형": "프로필", "제목": f"{지사} 위험 프로필 — 총량 {tot_i:.0f} / 사업장당 {site_i:.0f} / 관리자당 {mgr_i:.0f}",
            "근거": f"안전관리자 {int(r['안전관리자수'])}명 · 사업장 {int(r['사업장수'])}개 · 지적 {int(r['지적건수'])}건. " + msg,
            "권장활동": "규모(사업장·관리자) 대비 지수를 함께 보고 자원배분 판단",
        })

    # 1) 이 지사 주력 위험분류(점검강도 상위, 질병성 제외)
    hz = dfb[dfb["위험분류"].isin([h for h in PHYSICAL_HAZARDS if h not in DISEASE_HAZARDS])]
    if not hz.empty:
        topn = hz.groupby("위험분류")["심각도점수"].sum().sort_values(ascending=False).head(3)
        out.append({
            "유형": "주력위험", "제목": f"{지사} 집중 위험 — " + ", ".join(topn.index),
            "근거": "최근 점검 심각도 기준 상위 위험분류: "
                    + ", ".join(f"{h}({v:.0f})" for h, v in topn.items()),
            "권장활동": f"상위 위험분류 대상 TBM·특별점검 우선 배정",
        })

    # 2) 이 지사 재발위험(과거 인적재해 상위)
    pa = acc[(acc["지사"] == 지사) & (acc["재해성격"] == "인적재해")]
    if not pa.empty:
        _phys = [h for h in PHYSICAL_HAZARDS if h not in DISEASE_HAZARDS]
        pt = pa[pa["위험분류"].isin(_phys)]["위험분류"].value_counts().head(3)
        if not pt.empty:
            out.append({
                "유형": "재발위험", "제목": f"{지사} 재발 경계 — " + ", ".join(pt.index),
                "근거": f"과거 인적재해 {len(pa)}건(산재 {int((pa['산재구분']=='산재').sum())}·공상 "
                        f"{int((pa['산재구분']=='공상').sum())}). 최다: "
                        + ", ".join(f"{h} {v}건" for h, v in pt.items()),
                "권장활동": "과거 재해 유형은 임계값을 낮춰 조기경보 + 재발방지 TBM 정기 배포",
            })

    # 3) 이 지사 사각지대(질병성 제외)
    bs = blind_spots(insp, acc, exclude_disease=True)
    if not bs.empty:
        bsb = bs[(bs["지사"] == 지사) & (bs["상태"] == "🔴 사각지대")]
        if not bsb.empty:
            items = ", ".join(f"{r['위험분류']}(과거 {r['과거재해']}건)" for _, r in bsb.iterrows())
            out.append({
                "유형": "사각지대", "제목": f"{지사} 실질 사각지대",
                "근거": f"과거 재해가 있었으나 최근 점검 0인 위험분류: {items}",
                "권장활동": "① 해당 위험분류 특별점검 즉시 지시 ② 다음 분기 필수 점검항목 반영",
            })

    # 4) 이 지사 갭(사고 대비 점검 부족, 질병성 제외)
    gb = coverage_gap_by_branch(insp, acc, 지사, exclude_disease=True)
    if not gb.empty:
        over = gb[gb["갭(사고-점검)"] > 5]
        if not over.empty:
            items = ", ".join(f"{h}(+{gb.loc[h,'갭(사고-점검)']:.0f}%p)" for h in over.index)
            out.append({
                "유형": "갭", "제목": f"{지사} 사고 대비 점검 부족",
                "근거": f"사고비중이 점검비중보다 큰 위험분류: {items}",
                "권장활동": "해당 위험분류 점검 비중 상향 · 관련 사고사례 공유교육",
            })

    if len(out) <= 1:
        out.append({"유형": "정보", "제목": f"{지사} — 특이 위험신호 낮음",
                    "근거": "재발위험·사각지대·갭에서 두드러진 항목이 없습니다.",
                    "권장활동": "현 점검 수준 유지 및 정기 모니터링"})
    return out


def coverage_gap(insp, acc, exclude_disease=False):
    """전사 갭 분석: 위험분류별 [점검 비중] vs [사고 비중].
    사고 비중은 인적재해 + 화재·폭발 + 풍수해(비인적 사고 포함)를 기준으로 산출.
    갭(+) = 사고 대비 점검 부족 / 갭(−) = 점검 대비 사고 적음. '특성' 컬럼으로 해석 보정.
    exclude_disease=True 이면 질병성(근골격 등) 제외."""
    hazards = [h for h in GAP_HAZARDS if not (exclude_disease and h in DISEASE_HAZARDS)]
    acch = safety_accident_hazard(acc)
    ins = insp[insp["위험분류"].isin(hazards)]["위험분류"].value_counts(normalize=True) * 100
    acj = acch[acch["점검관련위험"].isin(hazards)]["점검관련위험"].value_counts(normalize=True) * 100
    df = pd.DataFrame({"점검비중": ins, "사고비중": acj}).fillna(0)
    df["갭(사고-점검)"] = (df["사고비중"] - df["점검비중"]).round(1)
    df["점검비중"] = df["점검비중"].round(1)
    df["사고비중"] = df["사고비중"].round(1)
    df["특성"] = [HAZARD_TRAIT.get(h, "일반") for h in df.index]
    return df.sort_values("갭(사고-점검)", ascending=False)


# ---------------------------------------------------------------------------
def action_hierarchy(insp, 지사=None, sort_by_count=True):
    """조치활동 통제위계 분포. 설치·구조물(공학적) > 제거·차단 > 교육·지도 > 보호구(관리적).
    sort_by_count=True 이면 건수 많은 순으로 정렬(가독성)."""
    df = insp if 지사 is None else insp[insp["지사"] == 지사]
    vc = df["조치활동분류"].value_counts()
    if sort_by_count:
        return vc.sort_values(ascending=False)
    order = ["설치·구조물", "제거·차단", "점검·확인", "교육·지도", "보호구지급", "기타", "미분류"]
    return vc.reindex([o for o in order if o in vc.index])


def coverage_gap_by_branch(insp, acc, 지사, exclude_disease=False):
    """지사별 갭 분석: 특정 지사의 [점검 비중] vs [사고 비중] (인적재해+화재·풍수 포함)."""
    hazards = [h for h in GAP_HAZARDS if not (exclude_disease and h in DISEASE_HAZARDS)]
    acch = safety_accident_hazard(acc)
    ins = insp[(insp["지사"] == 지사) & (insp["위험분류"].isin(hazards))]
    acj = acch[(acch["지사"] == 지사) & (acch["점검관련위험"].isin(hazards))]
    ins_p = ins["위험분류"].value_counts(normalize=True) * 100
    acj_p = acj["점검관련위험"].value_counts(normalize=True) * 100
    df = pd.DataFrame({"점검비중": ins_p, "사고비중": acj_p}).fillna(0)
    if df.empty:
        return df
    df["갭(사고-점검)"] = (df["사고비중"] - df["점검비중"]).round(1)
    df["점검비중"] = df["점검비중"].round(1)
    df["사고비중"] = df["사고비중"].round(1)
    df["특성"] = [HAZARD_TRAIT.get(h, "일반") for h in df.index]
    return df.sort_values("갭(사고-점검)", ascending=False)


# ---------------------------------------------------------------------------
def generate_tbm(지사, 위험분류, insp, acc, n_cases=3):
    """경보 지사×위험분류에 대한 맞춤형 TBM 대본 자동 생성(규칙 기반)."""
    cases = insp[(insp["지사"] == 지사) & (insp["위험분류"] == 위험분류)]
    cases = cases.sort_values("심각도점수", ascending=False).head(n_cases)
    past = acc[(acc["지사"] == 지사) & (acc["위험분류"] == 위험분류) &
               (acc["재해성격"] == "인적재해")]

    lines = []
    lines.append(f"# 🦺 특별 TBM 대본 — {지사} / '{위험분류}' 위험")
    lines.append(f"(자동 생성 · 최근 점검 지적 및 과거 재해 이력 기반)\n")
    lines.append("## 1) 오늘의 핵심 위험")
    lines.append(f"우리 {지사}는 최근 '{위험분류}' 관련 지적이 집중되고 있습니다. "
                 f"작업 전 아래 사항을 반드시 확인합니다.\n")
    lines.append("## 2) 실제 우리 현장 지적 사례")
    if cases.empty:
        lines.append("- (해당 지적 사례 없음)")
    else:
        for _, c in cases.iterrows():
            lines.append(f"- [{c['심각도원본']}] {c['지적내용_평문'][:80]}")
    lines.append("")
    if not past.empty:
        lines.append("## 3) ⚠️ 과거 재해 이력 (재발 방지)")
        lines.append(f"우리 지사는 과거 '{위험분류}'로 **{len(past)}건**의 인적재해가 발생한 이력이 있습니다.")
        ex = past.iloc[0]
        lines.append(f"- 사례: {str(ex['사고개요'])[:100]}")
        lines.append("- 같은 유형의 재해가 반복되지 않도록 오늘 특별히 주의합니다.\n")
    lines.append("## 4) 작업 전 체크리스트")
    checklist = TBM_CHECKLIST.get(위험분류, ["작업 구역 위험요인 확인", "보호구 착용 상태 확인", "비상연락체계 확인"])
    for item in checklist:
        lines.append(f"- [ ] {item}")
    lines.append("\n## 5) 관리감독자 확인")
    lines.append("- [ ] TBM 실시 및 전원 이해 확인  - [ ] 특이사항 안전관리자 보고")
    return "\n".join(lines)


TBM_CHECKLIST = {
    "추락": ["고소작업 구간 안전대·안전난간 설치 확인", "작업발판·사다리 고정 상태 확인",
           "차량/설비 상부 승하차 시 3점 지지 준수", "개구부·단부 덮개 및 방호 확인"],
    "전도": ["작업 바닥 정리정돈 및 미끄럼·요철 제거", "우천/결빙 시 이동 통로 확보",
           "중량물 운반 시 시야 확보 및 무리한 자세 금지"],
    "끼임·협착": ["회전체·컨베이어 방호덮개 확인", "지게차/중장비 작업반경 통제 및 유도자 배치",
              "정비 시 전원 차단(LOTO) 확인"],
    "충돌": ["차량·장비 이동경로와 보행자 동선 분리", "유도자 배치 및 후진 경보 확인",
           "인양물 하부 및 작업반경 내 인원 통제"],
    "낙하·비래·붕괴": ["인양 슬링·와이어 결속 및 노후 상태 확인", "적재물 결박·전도방지 조치",
                 "낙하물 방지망·출입통제 확인"],
    "화재·폭발": ["화기작업 허가 및 소화기 비치 확인", "가연물·인화물 격리",
              "가스용기 전도방지 및 밸브 상태 확인"],
    "감전": ["전선 피복 손상·누전 확인", "이동식 전기기계 접지 확인", "젖은 손/바닥 작업 금지"],
    "무리한동작": ["중량물 2인1조/기계 사용", "반복작업 스트레칭 실시", "적정 작업높이 확보"],
}


if __name__ == "__main__":
    insp, acc = load_data()
    print("=== 지사별 위험지수 ===")
    print(branch_risk_index(insp).to_string(index=False))
    print("\n=== 최근 90일 경보 ===")
    print(compute_alerts(insp, acc).to_string(index=False))
    print("\n=== 점검자 편향(다양성 낮은 순) ===")
    print(inspector_bias(insp).to_string(index=False))
    print("\n=== 전사 갭(사고-점검) ===")
    print(coverage_gap(insp, acc).to_string())
    print("\n=== 사각지대 ===")
    bs = blind_spots(insp, acc)
    print(bs.to_string(index=False) if not bs.empty else "(없음)")
    print("\n=== 자동 도출 인사이트 ===")
    for i in generate_insights(insp, acc):
        print(f"\n[{i['유형']}] {i['제목']}\n  근거: {i['근거']}\n  권장: {i['권장활동']}")
