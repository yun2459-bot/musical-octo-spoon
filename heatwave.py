# -*- coding: utf-8 -*-
"""폭염 대응 탭 데이터 로더.

`온도를 조져보자` 프로젝트(기상청 API허브 연동, 지사별 체감온도 자동 관제)가 쌓는
SQLite(alerts.db)와 사업장 주소 설정(sites.yaml)을 읽기 전용으로 읽어온다.

Streamlit Cloud 등 외부 배포 환경은 이 저장소 밖의 로컬 파일에 접근할 수 없으므로,
원본을 직접 읽지 않고 이 저장소 안의 heatwave_data/ 스냅샷을 읽는다. 최신화는
sync_heatwave_data.py를 실행해 원본을 이 폴더로 복사한 뒤 git commit/push하면 된다.
"""
from __future__ import annotations

import base64
import math
import re
import sqlite3
from pathlib import Path

import pandas as pd
import requests
import yaml

HEATWAVE_DATA_DIR = Path(__file__).parent / "heatwave_data"
ALERTS_DB_PATH = HEATWAVE_DATA_DIR / "alerts.db"
SITES_YAML_PATH = HEATWAVE_DATA_DIR / "sites.yaml"
KOREA_SVG_PATH = HEATWAVE_DATA_DIR / "Map_of_South_Korea-blank.svg"

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

LEVEL_ORDER = ["주의", "경고", "위험"]
LEVEL_COLOR = {"주의": "#f4d35e", "경고": "#f2a154", "위험": "#c81d25"}
NORMAL_COLOR = "#6fb56f"

# 위경도 -> Map_of_South_Korea-blank.svg 좌표계(viewBox 0 0 800 1200) 변환식.
# 11개 시/도 중심점(실제 위경도 vs SVG 도형 bbox 중심)으로 최소자승 적합.
SVG_VIEWBOX = (800, 1200)
_X_COEF = (169.306327, -2.35651831, -21154.8859)   # a*lon + b*lat + c
_Y_COEF = (2.88404005, -218.071214, 8062.87449)     # d*lon + e*lat + f


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


def latlon_to_svg_pct(lat: float, lon: float) -> tuple[float, float]:
    """위경도 -> Map_of_South_Korea-blank.svg 상의 (left%, top%)."""
    x = _X_COEF[0] * lon + _X_COEF[1] * lat + _X_COEF[2]
    y = _Y_COEF[0] * lon + _Y_COEF[1] * lat + _Y_COEF[2]
    return x / SVG_VIEWBOX[0] * 100, y / SVG_VIEWBOX[1] * 100


def korea_svg_data_uri() -> str | None:
    if not KOREA_SVG_PATH.exists():
        return None
    raw = KOREA_SVG_PATH.read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


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
        return {"apparent_temp": row["apparent_temp"], "level": row["level"],
                "observed_at": row["observed_at"], "has_data": True, "is_estimate": False,
                "estimate_source": None, "estimate_km": None}
    return {"apparent_temp": None, "level": None, "observed_at": None, "has_data": False,
            "is_estimate": False, "estimate_source": None, "estimate_km": None}


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
            satellites.append({"city": r.city, "site_count": r.site_count, **reading})

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
        if branch_obs is not None and not branch_obs.empty:
            worst = branch_obs.loc[branch_obs["apparent_temp"].idxmax()]
            apparent_temp, level = worst["apparent_temp"], worst["level"]
            worst_city, observed_at, has_data = worst["site"], worst["observed_at"], True
        else:
            office_rows = grp[grp["office"]]
            office = office_rows.iloc[0] if not office_rows.empty else grp.iloc[0]
            est = _reading_or_estimate(latest, data_points, branch, office["city"], office["lat"], office["lon"])
            apparent_temp, level, observed_at, has_data = (
                est["apparent_temp"], est["level"], est["observed_at"], est["has_data"])
            worst_city = office["city"]
            is_estimate, estimate_source, estimate_km = est["is_estimate"], est["estimate_source"], est["estimate_km"]

        rows.append({
            "branch": branch, "cities": city_names,
            "apparent_temp": apparent_temp, "level": level,
            "is_estimate": is_estimate, "estimate_source": estimate_source, "estimate_km": estimate_km,
            "worst_city": worst_city, "observed_at": observed_at, "has_data": has_data,
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
    today = pd.Timestamp.now().normalize()
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


def load_incident_reports_raw() -> pd.DataFrame | None:
    """구글폼 응답 시트를 읽어 제출행 전부(지사별 중복 제거 없이) 반환 — 누적 집계용.

    시트 헤더는 폼 문항 텍스트를 그대로 쓴다는 전제로 매칭한다. URL 미설정이거나
    조회 실패, 필요한 컬럼이 없으면 None을 반환해 호출부가 기본값으로 폴백하게 한다.
    """
    if not GOOGLE_SHEET_CSV_URL:
        return None
    try:
        df = pd.read_csv(GOOGLE_SHEET_CSV_URL)
    except Exception:
        return None

    col_map = {}
    for col in df.columns:
        c = str(col)
        if "타임스탬프" in c or "timestamp" in c.lower():
            col_map[col] = "timestamp"
        elif c.strip() == "지사":
            col_map[col] = "branch"
        elif "환자" in c:
            col_map[col] = "환자수"
        elif "조정" in c and "건" in c:
            col_map[col] = "작업조정"
        elif "중지" in c and "건" in c:
            col_map[col] = "작업중지"
        elif "상세" in c:
            col_map[col] = "중지상세"
    df = df.rename(columns=col_map)

    needed = {"timestamp", "branch", "환자수", "작업조정", "작업중지"}
    if not needed.issubset(df.columns):
        return None
    if "중지상세" not in df.columns:
        df["중지상세"] = ""

    df["timestamp"] = df["timestamp"].apply(_parse_google_timestamp)
    df = df.dropna(subset=["branch", "timestamp"])
    if df.empty:
        return df
    df["중지상세"] = df["중지상세"].fillna("")
    return df.sort_values("timestamp")[
        ["branch", "환자수", "작업조정", "작업중지", "중지상세", "timestamp"]]


def load_incident_reports() -> pd.DataFrame | None:
    """구글폼 응답 시트(웹에 게시 CSV)를 읽어 지사별 최신 제출값만 남긴다."""
    raw = load_incident_reports_raw()
    if raw is None or raw.empty:
        return raw
    return raw.groupby("branch", as_index=False).last()[
        ["branch", "환자수", "작업조정", "작업중지", "중지상세", "timestamp"]]


def patient_summary() -> dict:
    """온열질환 환자수 전사 합계: 26년(올해) 누적, 금일.

    구글폼 "환자수"는 지사가 그날 보고하는 신규 발생 건수라는 전제로, 누적은 올해
    제출분 전체를 합산하고 금일은 오늘 날짜 제출분만 합산한다. 전년도(25년) 비교치는
    온열질환 신고 체계가 이번 시즌에 처음 도입돼 시스템 내에 원천 데이터가 없다.
    """
    raw = load_incident_reports_raw()
    if raw is None or raw.empty:
        return {"cumulative": 0, "today": 0}
    now = pd.Timestamp.now()
    year_rows = raw[raw["timestamp"].dt.year == now.year]
    cumulative = int(year_rows["환자수"].sum())
    today_rows = year_rows[year_rows["timestamp"].dt.normalize() == now.normalize()]
    today = int(today_rows["환자수"].sum())
    return {"cumulative": cumulative, "today": today}


def today_stoppages() -> pd.DataFrame:
    """금일 제출분 중 작업중지가 있는 지사만 반환 — 없으면 빈 DataFrame.

    전일 이전 제출은 절대 이월하지 않는다(하루 단위로 리셋되는 알림 카드용).
    """
    cols = ["branch", "작업중지", "중지상세", "timestamp"]
    raw = load_incident_reports_raw()
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    today = pd.Timestamp.now().normalize()
    today_rows = raw[raw["timestamp"].dt.normalize() == today]
    today_rows = today_rows[today_rows["작업중지"] > 0]
    if today_rows.empty:
        return pd.DataFrame(columns=cols)
    return today_rows.groupby("branch", as_index=False).last()[cols]


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


def load_photo_reports() -> pd.DataFrame:
    """현장 사진 전용 구글폼(별도 폼) 응답 -> (branch, photo_url, timestamp) 목록.

    PHOTO_SHEET_CSV_URL이 비어있거나 제출이 없으면 빈 DataFrame을 반환한다
    (호출부는 이 경우 캐러셀을 안전하게 숨긴다).
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
    return pd.DataFrame(rows).sort_values("timestamp", ascending=False).reset_index(drop=True)


def weekly_alert_counts_by_branch(notif: pd.DataFrame) -> pd.DataFrame:
    """지사별 주차별 경보 발령 건수 — 지난주/이번주 두 칸을 항상 고정으로 보여준다.

    이번주에 발령이 하나도 없어도(정상적인 상태) 오른쪽 패널이 아예 안 뜨는 대신
    빈 패널로라도 뜨도록, 그 주차에 0건짜리 더미 행을 하나 채워 넣는다.
    """
    last_week, this_week = this_and_last_week()

    if not notif.empty:
        d = notif.copy()
        d["week"] = pd.to_datetime(d["sent_at"]).apply(_week_start)
        d = d[d["week"].isin([last_week, this_week])]
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
