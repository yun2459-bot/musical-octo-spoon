# -*- coding: utf-8 -*-
"""폭염 대응 탭 데이터 로더.

`온도를 조져보자` 프로젝트(기상청 API허브 연동, 지사별 체감온도 자동 관제)가 쌓는
SQLite(alerts.db)와 사업장 주소 설정(sites.yaml)을 읽기 전용으로 읽어온다.

Streamlit Cloud 등 외부 배포 환경은 이 저장소 밖의 로컬 파일에 접근할 수 없으므로,
원본을 직접 읽지 않고 이 저장소 안의 heatwave_data/ 스냅샷을 읽는다. 최신화는
sync_heatwave_data.py를 실행해 원본을 이 폴더로 복사한 뒤 git commit/push하면 된다.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

_KST = timezone(timedelta(hours=9))


def now_kst() -> pd.Timestamp:
    """한국 표준시 기준 '지금'을 tz 정보 없는 값으로 반환.

    Streamlit Cloud 등 배포 서버는 시스템 시각이 UTC라, pd.Timestamp.now()를 그대로
    쓰면 매일 00~09시(KST)에는 아직 전날로 계산돼 "금일/이번주" 경계가 하루 어긋난다.
    observed_at 등 DB에 쌓인 타임스탬프는 전부 KST 기준 naive 값이므로, 비교가
    어긋나지 않도록 여기서도 tz 정보를 뗀 KST naive 값으로 맞춘다.
    """
    return pd.Timestamp.now(tz=_KST).tz_localize(None)

HEATWAVE_DATA_DIR = Path(__file__).parent / "heatwave_data"
ALERTS_DB_PATH = HEATWAVE_DATA_DIR / "alerts.db"
SITES_YAML_PATH = HEATWAVE_DATA_DIR / "sites.yaml"

# 온열질환·조치 현황 구글폼 응답 시트를 "파일 > 웹에 게시 > CSV"로 발행한 링크.
GOOGLE_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQd-qH5R5USiu7_UgJuZOqKokyUtVtKSwzJQ2HzNY48th5USyr_V9FtXgEyI094andE7laDaKH6Q5EO/"
    "pub?gid=705177955&single=true&output=csv"
)

# 현장 사진 전용 구글폼(별도 폼) 응답 시트를 "파일 > 웹에 게시 > CSV"로 발행한 링크.
PHOTO_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSYVutauCiy_ontPnVoGqHhWVNTddj_AeVO37miWMtHdt9K7Fhe-I4vTyMe1VolOQcLAU0WOafOG90F/"
    "pub?gid=1033239324&single=true&output=csv"
)

# 작업중지 즉시 보고 전용 구글폼(별도 폼, GOOGLE_SHEET_CSV_URL과 분리) 응답 시트를
# "파일 > 공유 > 웹에 게시"로 발행한 CSV 링크. 채워지기 전까지는 관련 집계가 전부
# 0/빈값으로 폴백한다(호출부가 None 체크로 안전하게 처리).
STOPPAGE_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vSMezEV5mVSNLvKezk0vs1IOfnNN_SU4hiG2wiXRu685-j0ZOVBT1mpz7oWXfdPoEiHGmsLNbL6HCo7/"
    "pub?gid=473275652&single=true&output=csv"
)

# 온열질환 발생 즉시 보고 전용 구글폼(별도 폼) 응답 시트를 "웹에 게시"로 발행한 CSV 링크.
# 환자 발생은 주간 정기보고를 기다릴 수 없어 별도 폼으로 즉시 받는다. 비어 있으면
# 관련 집계가 0으로 폴백하고, 주차별 폼의 "금주 환자 수"는 교차검증용으로 계속 쓴다.
PATIENT_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vRfRHrwSzyVKtL_YBe7mzWpfDlrZWfctO28RIrO9A7J4tSOfTZbj0ItjNiKxkpNv9kOzQbXAKwmorqh/"
    "pub?gid=870352651&single=true&output=csv"
)

LEVEL_ORDER = ["주의", "경고", "위험"]
LEVEL_COLOR = {"주의": "#f4d35e", "경고": "#f2a154", "위험": "#c81d25"}
NORMAL_COLOR = "#6fb56f"


def available() -> bool:
    return ALERTS_DB_PATH.exists()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{ALERTS_DB_PATH.as_posix()}?mode=ro", uri=True)


def load_observations() -> pd.DataFrame:
    with _connect() as conn:
        df = pd.read_sql("SELECT * FROM observations", conn)
    if df.empty:
        return df
    df["observed_at"] = pd.to_datetime(df["observed_at"])
    df["recorded_at"] = pd.to_datetime(df["recorded_at"])
    return df


def load_notifications() -> pd.DataFrame:
    with _connect() as conn:
        df = pd.read_sql("SELECT * FROM notifications", conn)
    if df.empty:
        return df
    df["sent_at"] = pd.to_datetime(df["sent_at"])
    return df


def load_branch_cities() -> pd.DataFrame:
    """sites.yaml -> (branch, city, lat, lon, stn, office, site_count, satellite) 평탄화."""
    cols = ["branch", "city", "lat", "lon", "stn", "office", "site_count", "satellite"]
    if not SITES_YAML_PATH.exists():
        return pd.DataFrame(columns=cols)
    raw = yaml.safe_load(SITES_YAML_PATH.read_text(encoding="utf-8"))
    rows = []
    for b in raw.get("branches", []):
        for c in b.get("cities", []):
            rows.append({
                "branch": b["name"], "city": c["name"],
                "lat": c.get("lat"), "lon": c.get("lon"), "stn": c.get("stn"),
                "office": bool(c.get("office", False)), "site_count": c.get("site_count", 1),
                "satellite": bool(c.get("satellite", False)),
            })
    return pd.DataFrame(rows)


def branch_order() -> list[str]:
    """sites.yaml에 적힌 지사 순서 그대로(2026-07-31 지정: 경인/광양/부산/전북/경남/울산/강원/경북/목포/당진/삼천포)."""
    cities = load_branch_cities()
    if cities.empty:
        return []
    return cities["branch"].drop_duplicates().tolist()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _is_blank_level(level) -> bool:
    """None/NaN 모두 '정상(단계 없음)'으로 취급한다.

    SQLite NULL은 pandas를 거치면 float('nan')이 되는데, 파이썬에서 `not float('nan')`은
    False라서 `level or "정상"` 같은 흔한 폴백 패턴이 NaN에서는 조용히 깨진다(카드에
    "정상" 대신 "nan"이 찍히는 버그의 원인). 반드시 이 함수로 판정할 것.
    """
    return level is None or (isinstance(level, float) and math.isnan(level))


def level_label(level: str | None) -> str:
    return "정상" if _is_blank_level(level) else level


def level_color(level: str | None) -> str:
    if _is_blank_level(level):
        return NORMAL_COLOR
    return LEVEL_COLOR.get(level, NORMAL_COLOR)


def temp_level(apparent_temp: float) -> str | None:
    """체감온도 -> 세방 SAFETY TF 3단계(주의33/경고35/위험38). 미만이면 None(정상)."""
    if apparent_temp >= 38:
        return "위험"
    if apparent_temp >= 35:
        return "경고"
    if apparent_temp >= 33:
        return "주의"
    return None


def latest_by_key(obs: pd.DataFrame) -> pd.DataFrame:
    """(branch, site) 조합별 가장 최근 관측치."""
    if obs.empty:
        return obs
    idx = obs.groupby(["branch", "site"])["recorded_at"].idxmax()
    return obs.loc[idx].sort_values(["branch", "site"]).reset_index(drop=True)


# 부속 도시를 지도에 별도 "막대"로 붙여 보여줄 최소 사업장 개수 기준(안성=3처럼 규모가 있는 곳만).
SATELLITE_MIN_SITES = 2


def _city_reading(latest: pd.DataFrame, branch: str, city: str) -> dict:
    match = latest[(latest["branch"] == branch) & (latest["site"] == city)] if not latest.empty else None
    if match is not None and not match.empty:
        row = match.iloc[0]
        official_advisory = row.get("official_advisory")
        if pd.isna(official_advisory):
            official_advisory = None
        return {"apparent_temp": row["apparent_temp"], "level": row["level"],
                "observed_at": row["observed_at"], "has_data": True, "is_estimate": False,
                "estimate_source": None, "estimate_km": None, "official_advisory": official_advisory}
    return {"apparent_temp": None, "level": None, "observed_at": None, "has_data": False,
            "is_estimate": False, "estimate_source": None, "estimate_km": None, "official_advisory": None}


def _data_points(cities: pd.DataFrame, latest: pd.DataFrame) -> list[dict]:
    """실측값이 있는 모든 도시 목록(전 지사 통틀어) — 최근접 추정치 산출용."""
    points = []
    for r in cities.itertuples():
        reading = _city_reading(latest, r.branch, r.city)
        if reading["has_data"]:
            points.append({"branch": r.branch, "city": r.city, "lat": r.lat, "lon": r.lon, **reading})
    return points


def _reading_or_estimate(latest: pd.DataFrame, data_points: list[dict], branch: str, city: str,
                          lat: float, lon: float) -> dict:
    """직접 관측값이 없으면 전 지사 통틀어 가장 가까운 실측 도시 값을 추정치로 대신 쓴다."""
    direct = _city_reading(latest, branch, city)
    if direct["has_data"] or not data_points:
        return direct
    nearest = min(data_points, key=lambda p: _haversine_km(lat, lon, p["lat"], p["lon"]))
    km = _haversine_km(lat, lon, nearest["lat"], nearest["lon"])
    return {
        "apparent_temp": nearest["apparent_temp"], "level": nearest["level"],
        "observed_at": nearest["observed_at"], "has_data": True, "is_estimate": True,
        "estimate_source": f'{nearest["branch"]} {nearest["city"]}', "estimate_km": km,
        # 다른 도시(다른 특보구역)에서 빌려온 추정치라 공식특보는 같이 빌려오지 않는다.
        "official_advisory": None,
    }


def map_clusters(obs: pd.DataFrame) -> list[dict]:
    """지사당 하나의 클러스터: 사무실 도시(메인 원, 실제 좌표) + 부속 도시(막대, 사무실에 붙여 표시

    — 실제 좌표와 무관). 부속 막대는 사업장 2개 이상이거나 sites.yaml에서 수동 지정(satellite: true)한
    도시만 표시한다. 자체 관측지점이 없는 도시는 가장 가까운 실측 도시 값을 추정치로 보여준다.
    """
    cities = load_branch_cities()
    if cities.empty:
        return []

    latest = latest_by_key(obs)
    data_points = _data_points(cities, latest)

    clusters = []
    for branch, grp in cities.groupby("branch", sort=False):
        office_rows = grp[grp["office"]]
        office = office_rows.iloc[0] if not office_rows.empty else grp.iloc[0]

        satellites = []
        for r in grp.itertuples():
            if r.office or (r.site_count < SATELLITE_MIN_SITES and not r.satellite):
                continue
            reading = _reading_or_estimate(latest, data_points, branch, r.city, r.lat, r.lon)
            satellites.append({"city": r.city, "site_count": r.site_count, "lat": r.lat, "lon": r.lon, **reading})

        clusters.append({
            "branch": branch, "city": office["city"], "lat": office["lat"], "lon": office["lon"],
            **_reading_or_estimate(latest, data_points, branch, office["city"], office["lat"], office["lon"]),
            "satellites": satellites,
        })
    return clusters


def branch_summary(obs: pd.DataFrame) -> pd.DataFrame:
    """지사별 실시간 카드/차트용 요약: 지사 내 도시 중 최고 체감온도.

    지사 내 어느 도시에도 실측값이 없으면(목포/당진/삼천포 등) 사무실 좌표 기준으로
    가장 가까운 실측 도시 값을 추정치로 사용한다(지도 핀과 동일한 로직).
    """
    cities = load_branch_cities()
    if cities.empty:
        return cities

    latest = latest_by_key(obs)
    data_points = _data_points(cities, latest)

    rows = []
    for branch, grp in cities.groupby("branch", sort=False):
        city_names = grp["city"].tolist()
        branch_obs = latest[latest["branch"] == branch] if not latest.empty else latest
        is_estimate, estimate_source, estimate_km = False, None, None
        official_advisory = None
        if branch_obs is not None and not branch_obs.empty:
            worst = branch_obs.loc[branch_obs["apparent_temp"].idxmax()]
            apparent_temp, level = worst["apparent_temp"], worst["level"]
            worst_city, observed_at, has_data = worst["site"], worst["observed_at"], True
            official_advisory = worst.get("official_advisory")
            if pd.isna(official_advisory):
                official_advisory = None
        else:
            office_rows = grp[grp["office"]]
            office = office_rows.iloc[0] if not office_rows.empty else grp.iloc[0]
            est = _reading_or_estimate(latest, data_points, branch, office["city"], office["lat"], office["lon"])
            apparent_temp, level, observed_at, has_data = (
                est["apparent_temp"], est["level"], est["observed_at"], est["has_data"])
            worst_city = office["city"]
            is_estimate, estimate_source, estimate_km = est["is_estimate"], est["estimate_source"], est["estimate_km"]
            # 추정치는 다른 도시(다른 특보구역)에서 빌려온 값이라 공식특보는 같이 빌려오지 않는다.

        rows.append({
            "branch": branch, "cities": city_names,
            "apparent_temp": apparent_temp, "level": level,
            "is_estimate": is_estimate, "estimate_source": estimate_source, "estimate_km": estimate_km,
            "worst_city": worst_city, "observed_at": observed_at, "has_data": has_data,
            "official_advisory": official_advisory,
        })
    return pd.DataFrame(rows)


def daily_max_by_branch(obs: pd.DataFrame) -> pd.DataFrame:
    """지사별 일별 최고 체감온도. 자체 관측지점이 없는 지사(목포/당진/삼천포 등)는

    그 날짜에 가장 가까운 실측 도시의 값을 빌려와 채운다(카드/지도와 동일한 추정 로직,
    다만 여기서는 표시상 구분하지 않는다).
    """
    if obs.empty:
        return obs
    d = obs.copy()
    d["date"] = d["observed_at"].dt.date
    direct = d.groupby(["branch", "date"], as_index=False)["apparent_temp"].max()

    cities = load_branch_cities()
    if cities.empty:
        return direct

    city_daily = d.groupby(["branch", "site", "date"], as_index=False)["apparent_temp"].max()
    city_daily = city_daily.merge(cities[["branch", "city", "lat", "lon"]],
                                   left_on=["branch", "site"], right_on=["branch", "city"], how="left")

    have = set(zip(direct["branch"], direct["date"]))
    dates = sorted(d["date"].unique())

    extra_rows = []
    for branch, grp in cities.groupby("branch", sort=False):
        office_rows = grp[grp["office"]]
        office = office_rows.iloc[0] if not office_rows.empty else grp.iloc[0]
        for date in dates:
            if (branch, date) in have:
                continue
            same_date = city_daily[city_daily["date"] == date]
            if same_date.empty:
                continue
            dists = same_date.apply(
                lambda r: _haversine_km(office["lat"], office["lon"], r["lat"], r["lon"]), axis=1)
            nearest = same_date.loc[dists.idxmin()]
            extra_rows.append({"branch": branch, "date": date, "apparent_temp": nearest["apparent_temp"]})

    if extra_rows:
        direct = pd.concat([direct, pd.DataFrame(extra_rows)], ignore_index=True)
    return direct


def _week_start(d) -> pd.Timestamp:
    """해당 날짜가 속한 주의 월요일(자정)."""
    ts = pd.Timestamp(d)
    return (ts - pd.Timedelta(days=ts.dayofweek)).normalize()


def this_and_last_week() -> tuple[pd.Timestamp, pd.Timestamp]:
    """(왼쪽="지난주" 라벨용, 오른쪽="이번주" 라벨용) 두 주차의 월요일 날짜.

    진짜 달력 기준(오늘이 속한 주 = 이번주, 그 전 주 = 지난주)이라 매주 월요일마다
    사람 손 안 대도 저절로 굴러간다. 파일럿 시작 직후처럼 지난주에 데이터가 없으면
    왼쪽 패널이 빈 채로 뜨는 게 정상이며, 한 주가 지나면 자동으로 채워진다.
    """
    today = now_kst().normalize()
    this_week = _week_start(today)
    return this_week - pd.Timedelta(days=7), this_week


def weekly_max_by_branch(obs: pd.DataFrame) -> pd.DataFrame:
    """지사별 주차(월요일 시작) 최고 체감온도 — 지난주/이번주 두 칸만 고정으로 보여준다.

    일별 최고값(daily_max_by_branch, 추정 로직 포함)을 주 단위로 다시 최댓값 집계한다.
    이번주에 아직 관측이 없어도 빈 칸으로라도 항상 두 주차가 함께 보이도록,
    지사 x 주차 전 조합을 채워 넣는다(값 없는 칸은 막대가 안 뜬다).
    """
    last_week, this_week = this_and_last_week()
    cities = load_branch_cities()
    branches = cities["branch"].unique().tolist() if not cities.empty else []

    if obs.empty:
        weekly = pd.DataFrame(columns=["branch", "week", "apparent_temp"])
    else:
        dmax = daily_max_by_branch(obs)
        dmax["week"] = dmax["date"].apply(_week_start)
        dmax = dmax[dmax["week"].isin([last_week, this_week])]
        weekly = dmax.groupby(["branch", "week"], as_index=False)["apparent_temp"].max()

    idx = pd.MultiIndex.from_product([branches, [last_week, this_week]], names=["branch", "week"])
    full = pd.DataFrame(index=idx).reset_index()
    result = full.merge(weekly, on=["branch", "week"], how="left")
    result["주차"] = result["week"].map({last_week: "지난주", this_week: "이번주"})
    return result


def incident_placeholder() -> pd.DataFrame:
    """온열질환 환자·작업조정·작업중지 현황 — 자리표시자(전부 0/빈값).

    현장에서 이 수치를 입력할 경로(구글폼)가 없던 지사는 이 기본값으로 채워진다.
    """
    cities = load_branch_cities()
    if cities.empty:
        return cities
    branches = cities["branch"].unique().tolist()
    return pd.DataFrame({
        "branch": branches,
        "환자수": [0] * len(branches),
        "작업조정": [0] * len(branches),
        "작업중지": [0] * len(branches),
        "중지상세": [""] * len(branches),
    })


_KO_TS_PAT = re.compile(
    r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\s+(오전|오후)\s+(\d{1,2}):(\d{1,2}):(\d{1,2})"
)


def _to_count(series: pd.Series) -> pd.Series:
    """건수/인원 문항을 정수로 변환 — "0명", "2건", "없음" 같은 자유 입력도 견딘다.

    구글폼 단답형은 숫자만 받도록 강제할 수 없어 현장에서 단위를 붙여 적는 일이 잦다.
    문자열에서 첫 숫자만 뽑아 쓰고(예: "3명"→3), 숫자가 없으면 0으로 본다.
    """
    nums = series.astype(str).str.extract(r"(-?\d+)", expand=False)
    return pd.to_numeric(nums, errors="coerce").fillna(0).astype(int)


def _parse_google_timestamp(raw) -> pd.Timestamp:
    """구글폼 응답 시트의 기본 타임스탬프 형식("2026. 7. 31 오후 4:10:16")을 파싱한다.

    pandas.to_datetime은 이 한국어 오전/오후 형식을 못 읽고 그냥 NaT로 버리므로
    (응답이 있어도 조용히 사라짐) 직접 정규식으로 파싱한다. 이 형식이 아니면
    일반 pandas 파서로 한 번 더 시도한다(형식이 바뀌는 경우 대비).
    """
    m = _KO_TS_PAT.search(str(raw))
    if m:
        year, month, day, ampm, hour, minute, second = m.groups()
        hour = int(hour)
        if ampm == "오후" and hour != 12:
            hour += 12
        elif ampm == "오전" and hour == 12:
            hour = 0
        try:
            return pd.Timestamp(int(year), int(month), int(day), hour, int(minute), int(second))
        except ValueError:
            return pd.NaT
    return pd.to_datetime(raw, errors="coerce")


@st.cache_data(ttl=300)
def incident_sheet_status() -> tuple[str, str]:
    """온열질환 조사 시트를 읽을 수 있는 상태인지 진단해 (코드, 설명)로 반환.

    load_incident_reports_raw()는 실패해도 None만 돌려주기 때문에, 화면에는 "제출이
    없는 것"과 "연동이 깨진 것"이 똑같이 0으로 보인다 — 안전 대시보드에서는 이 둘을
    반드시 구분해야 해서(파이프라인이 끊겼는데 '이상 없음'으로 읽히면 위험) 상태를
    따로 알려주는 함수를 둔다.
    """
    if not GOOGLE_SHEET_CSV_URL:
        return ("no_url", "응답 시트 주소가 설정되지 않았습니다.")
    try:
        df = pd.read_csv(GOOGLE_SHEET_CSV_URL)
    except Exception as e:
        return ("fetch_fail", f"응답 시트를 불러오지 못했습니다({type(e).__name__}). "
                              "구글시트 '웹에 게시'가 해제됐는지 확인하세요.")
    if load_incident_reports_raw() is None:
        return ("bad_columns", "응답 시트에서 필요한 문항(지사/환자수/작업조정 등)을 찾지 "
                               "못했습니다. 구글폼 문항 이름이 바뀌지 않았는지 확인하세요.")
    return ("ok", "")


@st.cache_data(ttl=300)
def load_incident_reports_raw() -> pd.DataFrame | None:
    """구글폼 응답 시트를 읽어 제출행 전부(지사별 중복 제거 없이) 반환 — 누적 집계용.

    시트 헤더는 폼 문항 텍스트를 그대로 쓴다는 전제로 매칭한다. URL 미설정이거나
    조회 실패, 필요한 컬럼이 없으면 None을 반환해 호출부가 기본값으로 폴백하게 한다.

    5분 캐시: 이 함수 하나를 patient_summary/today_stoppages/incident_status가 전부
    호출하므로, 캐시가 없으면 화면 한 번 그릴 때마다 같은 시트를 3~4번 중복 다운로드한다.
    """
    if not GOOGLE_SHEET_CSV_URL:
        return None
    try:
        df = pd.read_csv(GOOGLE_SHEET_CSV_URL)
    except Exception:
        return None

    # 폼 문항이 늘어나면서 시트에 옛 문항 잔재 컬럼('물', '지사.1' 등)이 남아있을 수
    # 있고, 부분일치 규칙을 잘못 쓰면 서로 다른 두 컬럼이 같은 이름으로 매핑돼 중복
    # 컬럼이 생긴다(= Styler.apply가 KeyError로 죽는다). 그래서 아래 규칙은 판별력
    # 높은 것부터 순서대로 검사하고, 한 번 매핑된 표준명은 다시 쓰지 않는다.
    def _match(c: str) -> str | None:
        if "타임스탬프" in c or "timestamp" in c.lower():
            return "timestamp"
        if c.strip() == "지사":
            return "branch"
        if "환자" in c:
            return "환자수"
        if "조정" in c and "건" in c:
            return "작업조정"
        if "중지" in c and "건" in c:
            return "작업중지"
        if "비상대응" in c or ("교육" in c and "물품" in c):
            return "비상물품교육"
        if "민감군" in c:
            return "민감군"
        if "바람" in c or "그늘" in c:
            return "바람그늘"
        if "휴식" in c:
            return "휴식"
        if "포도당" in c or ("물" in c and "관리" in c):
            return "물"
        if "점검" in c and "특이사항" in c:
            return "점검특이사항"
        if "상세" in c:
            return "중지상세"
        return None

    col_map = {}
    for col in df.columns:
        std = _match(str(col))
        if std and std not in col_map.values():
            col_map[col] = std
    # 매핑에 쓰인 컬럼만 남긴다 — 시트에 표준명과 똑같은 이름의 잔재 컬럼(예: 옛 문항 '물')이
    # 그대로 있으면 rename 후 같은 이름이 둘이 되어 중복 컬럼이 생긴다.
    df = df[list(col_map.keys())].rename(columns=col_map)

    # 작업중지/중지상세는 별도 폼("작업중지 즉시 보고")으로 분리돼 이 시트에는 더 이상
    # 새로 쌓이지 않는다 — 과거(분리 이전) 데이터는 남아있을 수 있어 있으면 읽고, 없으면
    # 0/빈값으로 채워 하위 함수(stoppage_summary 등)가 그대로 동작하게 한다.
    needed = {"timestamp", "branch", "환자수", "작업조정"}
    if not needed.issubset(df.columns):
        return None
    for c in ["작업중지", "물", "바람그늘", "휴식", "비상물품교육", "민감군"]:
        if c not in df.columns:
            df[c] = 0 if c == "작업중지" else ""
    for c in ["중지상세", "점검특이사항"]:
        if c not in df.columns:
            df[c] = ""

    df["timestamp"] = df["timestamp"].apply(_parse_google_timestamp)
    # 지사명 앞뒤 공백은 제거한다 — 공백 하나 때문에 지사별 표에서 통째로 누락되면
    # 전사 합계와 지사별 합계가 어긋나 원인을 찾기 매우 어렵다.
    df["branch"] = df["branch"].astype(str).str.strip()
    df = df.dropna(subset=["branch", "timestamp"])
    if df.empty:
        return df
    text_cols = ["중지상세", "물", "바람그늘", "휴식", "비상물품교육", "민감군", "점검특이사항"]
    for c in text_cols:
        df[c] = df[c].fillna("")
    # 숫자 문항이지만 구글폼 단답형이라 "0명", "0건", "없음" 처럼 자유롭게 적히는 경우가
    # 실제로 있다 — 그대로 두면 컬럼이 문자열이 되어 .sum()이 값을 이어붙이고("0"+"0명"),
    # int() 변환에서 앱이 죽는다. 숫자만 뽑아 쓰고, 못 뽑으면 0으로 본다.
    for c in ["환자수", "작업조정", "작업중지"]:
        df[c] = _to_count(df[c])
    return df.sort_values("timestamp")[
        ["branch", "환자수", "작업조정", "작업중지", "중지상세",
         "물", "바람그늘", "휴식", "비상물품교육", "민감군", "점검특이사항", "timestamp"]]


def load_incident_reports() -> pd.DataFrame | None:
    """구글폼 응답 시트(웹에 게시 CSV)를 읽어 지사별 최신 제출값만 남긴다."""
    raw = load_incident_reports_raw()
    if raw is None or raw.empty:
        return raw
    return raw.groupby("branch", as_index=False).last()[
        ["branch", "환자수", "작업조정", "작업중지", "중지상세", "timestamp"]]


def patient_summary() -> dict:
    """온열질환 환자수 전사 합계: 26년(올해) 누적, 이번주.

    구글폼 문항은 "금주 온열질환 의심 신규 환자 수"(응답자가 그때까지 파악한 신규 발생분만
    입력 — 누적 계산은 응답자가 직접 하지 않음)이므로, 같은 지사가 같은 날 여러 번
    제출해도 그날의 마지막 제출값만 그날의 대표값으로 쓴 뒤(중복 합산 방지) 나머지는
    전부 합산으로 집계한다 — 주간·연간 누적 계산은 응답자가 아니라 대시보드(이 함수)가
    담당한다. 문항마다 집계 기준이 다르면 지사에서 헷갈리므로, 작업조정·작업중지와 동일한
    원칙을 쓴다. 전년도(25년) 비교치는 온열질환 신고 체계가 이번 시즌에 처음 도입돼
    시스템 내에 원천 데이터가 없다.
    """
    raw = load_incident_reports_raw()
    if raw is None or raw.empty:
        return {"cumulative": 0, "this_week": 0}
    _, this_week = this_and_last_week()
    now = now_kst()
    year_rows = raw[raw["timestamp"].dt.year == now.year].copy()
    year_rows["date"] = year_rows["timestamp"].dt.normalize()
    daily_last = (
        year_rows.sort_values("timestamp").groupby(["branch", "date"], as_index=False).last()
    )
    # 로더에서 이미 숫자로 바꾸지만, 배포 직후 st.cache_data에 남아있는 옛 캐시(문자열)가
    # 그대로 넘어와 합계가 문자열로 이어붙는 사고가 있었다 — 쓰는 쪽에서 한 번 더 막는다.
    counts = _to_count(daily_last["환자수"])
    cumulative = int(counts.sum())
    this_week_total = int(counts[daily_last["date"] >= this_week].sum())
    return {"cumulative": cumulative, "this_week": this_week_total}


# 온열질환 예방(폭염 대응) 기간 — 매년 이 날짜로 고정. 시즌 밖 날짜의 제출은 시즌
# 누적에서 제외한다(예: 관리자가 테스트 삼아 비시즌에 제출한 값이 섞이는 것 방지).
SEASON_START_MD = (6, 1)
SEASON_END_MD = (9, 30)


def _season_bounds(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year, *SEASON_START_MD)
    end = pd.Timestamp(year, *SEASON_END_MD, 23, 59, 59)
    return start, end


@st.cache_data(ttl=300)
def load_patient_reports_raw() -> pd.DataFrame | None:
    """온열질환 발생 즉시 보고 폼(별도 폼) 응답 시트를 읽어 제출행 전부 반환.

    사건 1건당 1행이며 한 건에 여러 명일 수 있어 "환자수" 문항을 함께 받는다.
    "1명"처럼 단위를 붙인 객관식 응답이 들어오므로 _to_count로 숫자만 뽑는다.
    URL 미설정/조회 실패/필수 컬럼 없음이면 None을 반환해 호출부가 0으로 폴백한다.
    """
    if not PATIENT_SHEET_CSV_URL:
        return None
    try:
        df = pd.read_csv(PATIENT_SHEET_CSV_URL)
    except Exception:
        return None

    def _match(c: str) -> str | None:
        if "타임스탬프" in c or "timestamp" in c.lower():
            return "timestamp"
        if c.strip() == "지사":
            return "branch"
        if "환자" in c:
            return "환자수"
        if "증상" in c:
            return "증상"
        if "경위" in c or "조치" in c:
            return "상세"
        return None

    col_map = {}
    for col in df.columns:
        std = _match(str(col))
        if std and std not in col_map.values():
            col_map[col] = std
    df = df[list(col_map.keys())].rename(columns=col_map)

    if not {"timestamp", "branch"}.issubset(df.columns):
        return None
    for c in ["증상", "상세"]:
        if c not in df.columns:
            df[c] = ""
    if "환자수" not in df.columns:
        df["환자수"] = 1

    df["timestamp"] = df["timestamp"].apply(_parse_google_timestamp)
    df["branch"] = df["branch"].astype(str).str.strip()
    df = df.dropna(subset=["branch", "timestamp"])
    if df.empty:
        return df
    for c in ["증상", "상세"]:
        df[c] = df[c].fillna("")
    df["환자수"] = _to_count(df["환자수"])
    return df.sort_values("timestamp")[["branch", "환자수", "증상", "상세", "timestamp"]]


def patient_event_summary() -> dict:
    """온열질환 발생 즉시 보고 기준 환자 수: 올해 누적 / 이번주 / 금일.

    주차별 폼의 자기보고(patient_summary)와 달리 사건 단위 기록이라 이 값이 공식
    집계다. 두 값이 어긋나면 어느 지사가 주간 보고를 누락했는지 찾는 단서가 된다.
    """
    raw = load_patient_reports_raw()
    if raw is None or raw.empty:
        return {"cumulative": 0, "this_week": 0, "today": 0}
    now = now_kst()
    _, this_week = this_and_last_week()
    year = raw[raw["timestamp"].dt.year == now.year]
    return {
        "cumulative": int(year["환자수"].sum()),
        "this_week": int(year.loc[year["timestamp"] >= this_week, "환자수"].sum()),
        "today": int(year.loc[year["timestamp"].dt.normalize() == now.normalize(), "환자수"].sum()),
    }


def weekly_patient_events() -> pd.DataFrame:
    """이번주 접수된 온열질환 발생 보고 전체 — 최신순(인쇄 보고서/화면 카드용)."""
    cols = ["branch", "환자수", "증상", "상세", "timestamp"]
    raw = load_patient_reports_raw()
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    _, this_week = this_and_last_week()
    return raw[raw["timestamp"] >= this_week][cols].sort_values("timestamp", ascending=False)


def patient_report_mismatch() -> pd.DataFrame:
    """지사별 [즉시보고 환자수] vs [주차별 폼 자기보고 환자수] 차이 — 누락 지사 추적용.

    두 숫자가 다르면 그 지사가 둘 중 하나를 빠뜨렸다는 뜻이라, 어디를 확인해야
    하는지 바로 알 수 있다. 차이가 없는 지사는 반환하지 않는다.
    """
    events = weekly_patient_events()
    weekly = weekly_incident_totals()
    ev = (events.groupby("branch", as_index=False)["환자수"].sum()
          .rename(columns={"환자수": "즉시보고"}) if not events.empty
          else pd.DataFrame(columns=["branch", "즉시보고"]))
    wk = weekly[["branch", "환자수"]].rename(columns={"환자수": "주간보고"})
    m = wk.merge(ev, on="branch", how="outer")
    m["즉시보고"] = m["즉시보고"].fillna(0).astype(int)
    m["주간보고"] = m["주간보고"].fillna(0).astype(int)
    return m[m["즉시보고"] != m["주간보고"]].reset_index(drop=True)


@st.cache_data(ttl=300)
def load_stoppage_reports_raw() -> pd.DataFrame | None:
    """작업 조정·중지 즉시 보고 폼(별도 폼) 응답 시트를 읽어 제출행 전부 반환.

    이 폼은 사건 1건당 1회 즉시 제출이 원칙이라 건수 문항이 없다 — "건수"는 그냥
    이 함수가 반환하는 행(row) 개수다. 폼이 "작업 조정/작업 중지" 중 하나를 고르고
    그에 맞는 상세 문항(조정 분류 / 중지 상세)만 응답받는 구조라, 여기서 "구분"·
    "상세" 두 컬럼으로 통일해 반환한다. URL 미설정이거나 조회 실패, 필요한 컬럼이
    없으면 None을 반환해 호출부가 0/빈값으로 폴백하게 한다.
    """
    if not STOPPAGE_SHEET_CSV_URL:
        return None
    try:
        df = pd.read_csv(STOPPAGE_SHEET_CSV_URL)
    except Exception:
        return None

    # load_incident_reports_raw()와 같은 이유로, 한 번 매핑된 표준명은 다시 쓰지 않아
    # 중복 컬럼이 생기지 않게 한다(옛 문항 잔재 컬럼이 시트에 남아있을 수 있음).
    def _match(c: str) -> str | None:
        if "타임스탬프" in c or "timestamp" in c.lower():
            return "timestamp"
        if c.strip() == "지사":
            return "branch"
        if "선택" in c and ("조정" in c or "중지" in c):
            return "구분"
        if "조정" in c and "분류" in c:
            return "조정분류"
        if "중지" in c and "상세" in c:
            return "중지상세"
        return None

    col_map = {}
    for col in df.columns:
        std = _match(str(col))
        if std and std not in col_map.values():
            col_map[col] = std
    df = df[list(col_map.keys())].rename(columns=col_map)

    if not {"timestamp", "branch"}.issubset(df.columns):
        return None
    for c in ["구분", "조정분류", "중지상세"]:
        if c not in df.columns:
            df[c] = ""

    df["timestamp"] = df["timestamp"].apply(_parse_google_timestamp)
    # 지사명 앞뒤 공백은 제거한다 — 공백 하나 때문에 지사별 표에서 통째로 누락되면
    # 전사 합계와 지사별 합계가 어긋나 원인을 찾기 매우 어렵다.
    df["branch"] = df["branch"].astype(str).str.strip()
    df = df.dropna(subset=["branch", "timestamp"])
    if df.empty:
        return df
    for c in ["구분", "조정분류", "중지상세"]:
        df[c] = df[c].fillna("")
    df["구분"] = df["구분"].apply(lambda v: str(v).strip())
    df["상세"] = df.apply(
        lambda r: r["중지상세"] if "중지" in r["구분"] else r["조정분류"], axis=1)
    return df.sort_values("timestamp")[["branch", "구분", "상세", "timestamp"]]


def stoppage_summary() -> dict:
    """작업중지 보고 건수: 온열질환 예방 기간(6/1~9/30) 누적('26년) · 이번주 · 금일 3단계.

    "작업 조정·중지 즉시 보고" 폼은 조정/중지 두 종류를 함께 받지만, 이 카드는
    옥외 작업 "중지"만 센다 — 사건 1건당 1회 제출이므로 건수 = 그 구분의 제출
    행 개수다(하루에 여러 건 발생하면 그만큼 여러 번 제출 — 중복 제거하지 않는다).
    """
    raw = load_stoppage_reports_raw()
    if raw is None or raw.empty:
        return {"season_cumulative": 0, "this_week": 0, "today": 0}
    stop_only = raw[raw["구분"].str.contains("중지", na=False)]
    now = now_kst()
    start, end = _season_bounds(now.year)
    season_rows = stop_only[(stop_only["timestamp"] >= start) & (stop_only["timestamp"] <= end)]
    _, this_week_start = this_and_last_week()
    week_rows = season_rows[season_rows["timestamp"] >= this_week_start]
    today_rows = week_rows[week_rows["timestamp"].dt.normalize() == now.normalize()]
    return {"season_cumulative": len(season_rows), "this_week": len(week_rows), "today": len(today_rows)}


def today_stoppages() -> pd.DataFrame:
    """금일 제출된 작업 조정·중지 즉시 보고 전체(두 구분 모두) — 없으면 빈 DataFrame.

    사건 1건당 1행이라 같은 지사가 오늘 여러 건 제출했으면 여러 행 그대로 보여준다
    (patient_summary류와 달리 "그날의 대표값 하나"로 접지 않는다).
    """
    cols = ["branch", "구분", "상세", "timestamp"]
    raw = load_stoppage_reports_raw()
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    today = now_kst().normalize()
    today_rows = raw[raw["timestamp"].dt.normalize() == today]
    return today_rows[cols]


def weekly_stoppage_reports() -> pd.DataFrame:
    """이번주(월요일~오늘) 제출된 작업 조정·중지 즉시 보고 전체(두 구분 모두) — 최신순.

    인쇄용 주간 보고서에서 쓴다 — "옥외 작업 중지" 절은 구분이 "작업 중지"인 행만
    걸러서 사용한다.
    """
    cols = ["branch", "구분", "상세", "timestamp"]
    raw = load_stoppage_reports_raw()
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    _, this_week = this_and_last_week()
    week_rows = raw[raw["timestamp"] >= this_week]
    return week_rows[cols].sort_values("timestamp", ascending=False)


def weekly_incident_totals() -> pd.DataFrame:
    """지사별 이번주(월요일~오늘) 누적 온열질환·조치 현황.

    환자수/작업조정은 구글폼 문항이 "금주 신규"(응답자는 그때까지 파악한 신규분만
    입력, 누적 계산은 하지 않음)이므로 지사·일자별 마지막 제출값을 그날의 대표값으로
    쓴 뒤(하루 중복 제출 방지) 이번주 날짜들의 대표값을 합산한다 — 응답자가 아니라
    이 함수가 주간 누적을 계산한다. 작업중지는 별도 "작업중지 즉시 보고" 폼에서
    사건 1건당 1행으로 들어오므로 그냥 이번주 행 개수를 센다. 중지상세(자유서술)는
    주 단위로 합쳐서 보여줄 방법이 마땅치 않아 이 집계에서는 제외한다 — 상세 내용이
    필요하면 weekly_stoppage_reports()를 직접 확인한다.
    """
    cols = ["branch", "환자수", "작업조정", "작업중지", "최근제출"]
    cities = load_branch_cities()
    branches = cities["branch"].unique().tolist() if not cities.empty else []
    if not branches:
        return pd.DataFrame(columns=cols)
    base = pd.DataFrame({"branch": branches, "환자수": 0, "작업조정": 0, "작업중지": 0,
                          "최근제출": pd.NaT})

    _, this_week = this_and_last_week()

    raw = load_incident_reports_raw()
    incident_agg = pd.DataFrame(columns=["branch", "환자수", "작업조정", "최근제출"])
    if raw is not None and not raw.empty:
        week_rows = raw[raw["timestamp"] >= this_week].copy()
        if not week_rows.empty:
            week_rows["date"] = week_rows["timestamp"].dt.normalize()
            daily_last = week_rows.sort_values("timestamp").groupby(["branch", "date"], as_index=False).last()
            # patient_summary와 같은 이유로, 합산 직전에 숫자형을 한 번 더 보장한다.
            for c in ["환자수", "작업조정"]:
                daily_last[c] = _to_count(daily_last[c])
            incident_agg = daily_last.groupby("branch", as_index=False).agg(
                환자수=("환자수", "sum"), 작업조정=("작업조정", "sum"), 최근제출=("timestamp", "max"))

    stoppages = weekly_stoppage_reports()
    stoppage_agg = pd.DataFrame(columns=["branch", "작업중지", "최근제출_중지"])
    if not stoppages.empty:
        # "작업중지" 컬럼은 옥외 작업 중지만 센다(같은 폼의 "작업 조정" 유형은 온열질환
        # 조사 폼의 "작업조정" 컬럼이 이미 담당) — 다만 "최근제출"은 조정 포함 두 유형
        # 모두의 최신 시각이어야 정확하므로, is_stop 플래그로 한 번에 같이 구한다.
        stoppages = stoppages.copy()
        stoppages["is_stop"] = stoppages["구분"].str.contains("중지", na=False)
        stoppage_agg = stoppages.groupby("branch", as_index=False).agg(
            작업중지=("is_stop", "sum"), 최근제출_중지=("timestamp", "max"))

    merged = base[["branch"]].merge(incident_agg, on="branch", how="left")
    merged = merged.merge(stoppage_agg, on="branch", how="left")
    # 최근제출 = 온열질환 조사 폼과 작업중지 즉시 보고 폼 중 그 지사의 더 최근 제출시각.
    # 두 폼 중 한쪽에 이번주 제출이 하나도 없으면 그 집계(incident_agg/stoppage_agg)가
    # 빈 DataFrame이라 병합 후 해당 컬럼이 datetime64가 아니라 전부 NaN인 object dtype이
    # 되고, 이 상태로 다른 쪽(datetime64)과 .max(axis=1)을 하면 TypeError가 난다 —
    # 두 컬럼 다 명시적으로 datetime64로 맞춘 뒤 합친다.
    merged["최근제출"] = pd.to_datetime(merged["최근제출"])
    merged["최근제출_중지"] = pd.to_datetime(merged["최근제출_중지"])
    merged["최근제출"] = merged[["최근제출", "최근제출_중지"]].max(axis=1)
    for c in ["환자수", "작업조정", "작업중지"]:
        merged[c] = merged[c].fillna(0).astype(int)
    return merged[cols]


CHECKLIST_FIELDS = ["물", "바람그늘", "휴식", "비상물품교육", "민감군"]
CHECKLIST_LABELS = {
    "물": "물", "바람그늘": "바람·그늘", "휴식": "휴식",
    "비상물품교육": "비상대응물품·교육", "민감군": "민감군",
}
# 각 점검 항목에서 "양호"로 볼 응답(구글폼 객관식 1번 보기) — 대시보드에서 초록 음영 처리.
# 폼 보기 문구를 바꾸면 여기도 같이 바꿔야 한다(문구가 어긋나면 양호 표시가 안 될 뿐,
# 오류가 나지는 않는다).
CHECKLIST_OK = {
    "물": "상시 비치·보충 정상",
    "바람그늘": "전량 정상 가동",
    "휴식": "휴게시설 운영·휴식 부여 정상",
    "비상물품교육": "응급키트 정상·TBM 매일 실시",
    "민감군": "해당 없음",
}


def weekly_checklist() -> pd.DataFrame:
    """지사별 이번주 온열질환 예방 점검 결과 — 5개 항목 최신 제출값 + 특이사항.

    체크리스트 문항은 "제출 시점 기준 현재 상태"를 묻는 객관식이라(예: 상시 비치·보충
    정상 / 부족 등) 날짜별로 합산할 수 있는 값이 아니다 — 이번주 안에서 가장 최근
    제출값만 그 지사의 대표값으로 쓴다. 같은 지사에서 이번주 여러 명이 각자 제출했을
    수도 있으므로(응답자 구분 문항이 없어 누가 냈는지는 알 수 없다) "제출횟수"를 같이
    보여준다 — 1보다 크면 최근 값 하나만 대표로 쓰고 있다는 걸 알아챌 수 있게.
    """
    cols = ["branch", *CHECKLIST_FIELDS, "점검특이사항", "최근제출", "제출횟수"]
    cities = load_branch_cities()
    branches = cities["branch"].unique().tolist() if not cities.empty else []
    if not branches:
        return pd.DataFrame(columns=cols)
    base = pd.DataFrame({"branch": branches, **{f: "" for f in CHECKLIST_FIELDS},
                          "점검특이사항": "", "최근제출": pd.NaT, "제출횟수": 0})

    raw = load_incident_reports_raw()
    if raw is None or raw.empty:
        return base[cols]

    _, this_week = this_and_last_week()
    week_rows = raw[raw["timestamp"] >= this_week].copy()
    if week_rows.empty:
        return base[cols]

    latest = (
        week_rows.sort_values("timestamp").groupby("branch", as_index=False).last()
        .rename(columns={"timestamp": "최근제출"})
    )
    counts = week_rows.groupby("branch", as_index=False).size().rename(columns={"size": "제출횟수"})
    latest = latest.merge(counts, on="branch", how="left")
    merged = base[["branch"]].merge(
        latest[["branch", *CHECKLIST_FIELDS, "점검특이사항", "최근제출", "제출횟수"]], on="branch", how="left")
    merged["제출횟수"] = merged["제출횟수"].fillna(0).astype(int)
    for f in CHECKLIST_FIELDS + ["점검특이사항"]:
        merged[f] = merged[f].fillna("")
    return merged[cols]


def incident_status() -> pd.DataFrame:
    """지사별 온열질환·조치 현황: 구글폼 제출값이 있으면 그 값, 없으면 기본값(전부 0)."""
    base = incident_placeholder()
    if base.empty:
        return base

    reports = load_incident_reports()
    if reports is None or reports.empty:
        base["제출 현황"] = "기본값(미제출)"
        base["제출시각"] = pd.NaT
        return base

    merged = base.merge(reports, on="branch", how="left", suffixes=("_기본", ""))
    for col in ["환자수", "작업조정", "작업중지"]:
        merged[col] = merged[col].where(merged[col].notna(), merged[f"{col}_기본"])
        merged = merged.drop(columns=[f"{col}_기본"])
    merged["중지상세"] = merged["중지상세"].where(merged["중지상세"].notna(), merged["중지상세_기본"])
    merged = merged.drop(columns=["중지상세_기본"])
    merged["제출 현황"] = merged["timestamp"].notna().map({True: "제출됨", False: "기본값(미제출)"})
    merged = merged.rename(columns={"timestamp": "제출시각"})
    return merged


_DRIVE_ID_PAT = re.compile(r"(?:id=|/d/)([a-zA-Z0-9_-]{20,})")


def _drive_thumbnail_url(link: str, size: int = 500) -> str | None:
    """구글 드라이브 공유 링크 -> 바로 <img src=...>로 쓸 수 있는 썸네일 URL.

    파일이 "링크가 있는 모든 사용자 - 뷰어"로 공유돼 있어야 인증 없이 열린다.
    """
    m = _DRIVE_ID_PAT.search(str(link))
    if not m:
        return None
    return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w{size}"


# 캐러셀·인쇄 보고서 사진 고르기에 실제로 그리는 지사별 사진 수 상한 — 시즌 내내
# 제한 없이 쌓이면 <img> 태그가 계속 늘어나(각각 구글 드라이브로 네트워크 요청)
# 페이지가 점점 무거워진다. 예전엔 전체 합산 24장으로 한 번에 잘랐는데, 그러면
# 사진을 많이 올린 지사가 상한을 다 채워버려서 다른 지사(특히 오래전에 1장만
# 올린 지사)가 통째로 사라지는 사고가 있었다(2026-08-04, 경남 지사 사진 누락
# 확인) — 지사별로 상한을 두어 어떤 지사든 최소 최근 사진은 항상 보이게 한다.
MAX_PHOTOS_PER_BRANCH = 12


@st.cache_data(ttl=300)
def load_photo_reports() -> pd.DataFrame:
    """현장 사진 전용 구글폼(별도 폼) 응답 -> (branch, photo_url, timestamp) 최신순,
    지사별 최대 MAX_PHOTOS_PER_BRANCH건.

    PHOTO_SHEET_CSV_URL이 비어있거나 제출이 없으면 빈 DataFrame을 반환한다
    (호출부는 이 경우 캐러셀을 안전하게 숨긴다). 5분 캐시로 화면 조작마다
    전체 시트를 다시 받는 걸 막는다.
    """
    cols = ["branch", "photo_url", "timestamp"]
    if not PHOTO_SHEET_CSV_URL:
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_csv(PHOTO_SHEET_CSV_URL)
    except Exception:
        return pd.DataFrame(columns=cols)

    photo_col = branch_col = ts_col = None
    for col in df.columns:
        c = str(col)
        if "사진" in c or "이미지" in c or "photo" in c.lower():
            photo_col = col
        elif c.strip() == "지사":
            branch_col = col
        elif "타임스탬프" in c or "timestamp" in c.lower():
            ts_col = col
    if photo_col is None or branch_col is None or ts_col is None:
        return pd.DataFrame(columns=cols)

    rows = []
    for _, r in df.iterrows():
        raw = r.get(photo_col)
        if pd.isna(raw) or not str(raw).strip():
            continue
        ts = _parse_google_timestamp(r.get(ts_col))
        if pd.isna(ts):
            continue
        # 한 문항에 여러 장 업로드하면 콤마로 링크가 이어져 온다.
        for link in str(raw).split(","):
            url = _drive_thumbnail_url(link.strip())
            if url:
                rows.append({"branch": r.get(branch_col), "photo_url": url, "timestamp": ts})

    if not rows:
        return pd.DataFrame(columns=cols)
    sorted_rows = pd.DataFrame(rows).sort_values("timestamp", ascending=False)
    return (sorted_rows.groupby("branch", group_keys=False)
            .head(MAX_PHOTOS_PER_BRANCH)
            .sort_values("timestamp", ascending=False)
            .reset_index(drop=True))


def weekly_photo_reports() -> pd.DataFrame:
    """이번주(월요일~오늘) 제출된 현장 활동 사진만 — load_photo_reports()를 이번주로 거른다.

    지난주 이전 사진이 "이번주 활동사진"에 섞여 들어가면 안 되므로(예: 지사가 이번주에
    아직 사진을 안 올렸는데 지난주 사진이 최신값으로 잡혀 보고서에 실리는 사고),
    캐러셀·인쇄 보고서 사진 고르기·인쇄 보고서 자체를 전부 이 함수로 통일한다.
    """
    photos = load_photo_reports()
    if photos.empty:
        return photos
    _, this_week = this_and_last_week()
    return photos[photos["timestamp"] >= this_week].reset_index(drop=True)


def weekly_alert_counts_by_branch(notif: pd.DataFrame) -> pd.DataFrame:
    """지사별 주차별 경보 발령 건수 — 지난주/이번주 두 칸을 항상 고정으로 보여준다.

    이번주에 발령이 하나도 없어도(정상적인 상태) 오른쪽 패널이 아예 안 뜨는 대신
    빈 패널로라도 뜨도록, 그 주차에 0건짜리 더미 행을 하나 채워 넣는다.

    notifications 테이블은 지사 내 도시(site) 단위·재발송(resend) 단위로 행이 쌓여서,
    같은 지사가 같은 날 도시 여러 곳(예: 전북의 군산/전주/완주)에서 동시에 특보가
    뜨면 그만큼 중복 집계됐다. 지사·단계·일자 기준으로 먼저 중복 제거해 "지사 단위로
    하루 1건"만 세도록 한다(날짜가 다르면 별개 건으로 유지).
    """
    last_week, this_week = this_and_last_week()

    if not notif.empty:
        d = notif.copy()
        d["week"] = pd.to_datetime(d["sent_at"]).apply(_week_start)
        d = d[d["week"].isin([last_week, this_week])]
        d["date"] = pd.to_datetime(d["sent_at"]).dt.date
        d = d.drop_duplicates(subset=["week", "branch", "level", "date"])
        out = d.groupby(["week", "branch", "level"], as_index=False).size().rename(columns={"size": "발령횟수"})
    else:
        out = pd.DataFrame(columns=["week", "branch", "level", "발령횟수"])

    for wk in (last_week, this_week):
        if wk not in out["week"].values:
            out = pd.concat([out, pd.DataFrame([{
                "week": wk, "branch": "", "level": LEVEL_ORDER[0], "발령횟수": 0,
            }])], ignore_index=True)

    out["주차"] = out["week"].map({last_week: "지난주", this_week: "이번주"})
    return out
