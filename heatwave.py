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

# 구글폼 응답 시트를 "파일 > 웹에 게시 > CSV"로 발행한 링크. 비어있으면 기본값만 표시한다.
GOOGLE_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQd-qH5R5USiu7_UgJuZOqKokyUtVtKSwzJQ2HzNY48th5USyr_V9FtXgEyI094andE7laDaKH6Q5EO/"
    "pub?gid=705177955&single=true&output=csv"
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


def level_color(level: str | None) -> str:
    if not level:
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


def incident_placeholder() -> pd.DataFrame:
    """온열질환 환자·작업조정/중지·휴식부여 현황 — 자리표시자(전부 0/기본값).

    현장에서 이 수치를 입력할 경로(모바일 폼 등)가 아직 없다. 입력 경로가 생기면 이 함수를
    실제 DB 조회로 교체하면 되고, 호출부(app.py)는 컬럼 구조가 같은 한 수정할 필요가 없다.
    """
    cities = load_branch_cities()
    if cities.empty:
        return cities
    branches = cities["branch"].unique().tolist()
    return pd.DataFrame({
        "branch": branches,
        "환자수": [0] * len(branches),
        "작업조정중지": [0] * len(branches),
        "휴식부여분": [20] * len(branches),
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


def load_incident_reports() -> pd.DataFrame | None:
    """구글폼 응답 시트(웹에 게시 CSV)를 읽어 지사별 최신 제출값만 남긴다.

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
        elif "조정" in c or "중지" in c:
            col_map[col] = "작업조정중지"
        elif "휴식" in c:
            col_map[col] = "휴식부여분"
    df = df.rename(columns=col_map)

    needed = {"timestamp", "branch", "환자수", "작업조정중지", "휴식부여분"}
    if not needed.issubset(df.columns):
        return None

    df["timestamp"] = df["timestamp"].apply(_parse_google_timestamp)
    df = df.dropna(subset=["branch", "timestamp"])
    if df.empty:
        return df
    return df.sort_values("timestamp").groupby("branch", as_index=False).last()[
        ["branch", "환자수", "작업조정중지", "휴식부여분", "timestamp"]]


def incident_status() -> pd.DataFrame:
    """지사별 온열질환·조치 현황: 구글폼 제출값이 있으면 그 값, 없으면 기본값(0/0/20)."""
    base = incident_placeholder()
    if base.empty:
        return base

    reports = load_incident_reports()
    if reports is None or reports.empty:
        base["출처"] = "기본값(미제출)"
        base["제출시각"] = pd.NaT
        return base

    merged = base.merge(reports, on="branch", how="left", suffixes=("_기본", ""))
    for col in ["환자수", "작업조정중지", "휴식부여분"]:
        merged[col] = merged[col].where(merged[col].notna(), merged[f"{col}_기본"])
        merged = merged.drop(columns=[f"{col}_기본"])
    merged["출처"] = merged["timestamp"].notna().map({True: "제출됨", False: "기본값(미제출)"})
    merged = merged.rename(columns={"timestamp": "제출시각"})
    return merged


def weekly_alert_counts_by_branch(notif: pd.DataFrame) -> pd.DataFrame:
    if notif.empty:
        return notif
    d = notif.copy()
    d["week"] = d["sent_at"].dt.to_period("W-MON").apply(lambda p: p.start_time.date())
    out = d.groupby(["week", "branch", "level"], as_index=False).size()
    return out.rename(columns={"size": "발령횟수"})
