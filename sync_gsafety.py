# -*- coding: utf-8 -*-
"""
gsafety.kr 안전보건조회 자동 연동
- '점검유형=지사 주관 & 조치부서=OO지사' 구조적 조건으로 지사 점검 데이터를 주 단위로 가져와 누적본에 병합
  (코칭명/제목 문구는 지사마다 표기가 달라 텍스트 매칭 시 누락이 생기므로 쓰지 않음 — filter_branch_records 참고)
- 자격증명은 환경변수(GSAFETY_ID, GSAFETY_PW)에서만 읽음 — 코드/채팅에 절대 하드코딩 금지
- 흐름: 로그인 → 목록(최신 N일) 조회 → 체크된 UID로 엑셀 내보내기 → 파싱 → 누적본에 중복없이 병합
        → clean_data.py 재실행 → mask_pii.py 재실행

실행:  python sync_gsafety.py            (증분: 마지막 동기화 이후 ~ 오늘, 3일 여유)
       python sync_gsafety.py --days 30  (최근 30일 강제 재수집)
       python sync_gsafety.py --init 365 (최초 백필: 최근 365일)
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# Windows 콘솔(cp949)에서 이모지·특수문자 print 시 크래시 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

BASE = "https://sebang.gsafety.kr"
LOGIN_PAGE = f"{BASE}/member/login.php"
LOGIN_POST = f"{BASE}/member/login_ok.php"
LIST_PAGE = f"{BASE}/sb/search/coach.php"
SET_ROWS = f"{BASE}/set_row_count.php"
EXPORT_URL = f"{BASE}/sb/search/coach_export.php"

DATA = Path(__file__).parent / "data"
RAW_ACCUM = DATA / "raw_점검_누적.xlsx"   # 원본 그대로 누적(정제 전)
STATE_FILE = DATA / "_gsafety_sync_state.json"
PAGE_SIZE = 5000  # 사이트 확인된 전체이력(1784건)을 한 번에 받을 수 있도록 충분히 크게

# 코칭명(제목)은 지사마다 표기가 제각각이라("안전관리자 점검일지" vs "안전관리 점검일지" 등)
# 텍스트 매칭으로는 누락이 생긴다. 대신 구조적 필드로 골라낸다:
#   점검유형 == "지사 주관"  AND  조치부서가 "OO지사"/"OOO지사" 형태
BRANCH_DEPT_RE = re.compile(r"^[가-힣]{2,3}지사$")


def filter_branch_records(df: pd.DataFrame) -> pd.DataFrame:
    """지사 주관 + 조치부서가 'OO지사' 형태인 행만 남긴다(코칭명 표기 차이에 영향받지 않음)."""
    mask = (df["점검유형"] == "지사 주관") & df["조치부서"].astype(str).str.match(BRANCH_DEPT_RE)
    return df[mask].copy()

# 검색폼의 고정 파라미터(빈 값 유지) — 사이트가 요구하는 전체 필드를 그대로 채움
FILTER_KEYS = ["rotate", "logo_hide", "coach_only", "sdt", "edt", "sdt2", "edt2",
               "s_job", "s_security", "c_dept", "s_dept", "s_area", "s_check",
               "state", "s_accd", "s_coach_type", "s_measure", "s_cat",
               "subject", "name", "name2"]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get_credentials():
    uid = os.environ.get("GSAFETY_ID")
    pw = os.environ.get("GSAFETY_PW")
    if not uid or not pw:
        sys.exit(
            "GSAFETY_ID / GSAFETY_PW 환경변수가 설정되지 않았습니다.\n"
            "PowerShell에서 한 번만 설정하세요 (본인 계정 정보를 직접 입력):\n"
            '  [System.Environment]::SetEnvironmentVariable("GSAFETY_ID","사번","User")\n'
            '  [System.Environment]::SetEnvironmentVariable("GSAFETY_PW","비밀번호","User")\n'
            "설정 후 터미널을 새로 열어야 반영됩니다."
        )
    return uid, pw


def login(session: requests.Session, uid: str, pw: str):
    session.get(LOGIN_PAGE, timeout=20)  # 세션 쿠키 확보
    # 서버가 Referer/Origin으로 "정상 경로 접근"인지 검사하므로 반드시 포함해야 함
    # (없으면 "정상적인 경로로 접근하지 않았습니다" 경고와 함께 로그인이 거부됨)
    session.post(LOGIN_POST, data={
        "url": "", "lf": "", "m_id": uid, "m_pw": pw,
    }, headers={
        "Referer": LOGIN_PAGE,
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded",
    }, timeout=20, allow_redirects=True)
    # 로그인 성공 여부 확인: 루트 재방문 시 로그인폼(m_id)이 없어야 함
    check = session.get(f"{BASE}/", timeout=20, allow_redirects=True)
    if 'name="m_id"' in check.text:
        sys.exit("로그인 실패 — GSAFETY_ID/GSAFETY_PW를 확인하세요. (OTP가 요구되면 별도 조치 필요)")
    log("로그인 성공")


def filter_dict(sdt: str, edt: str) -> dict:
    # subject(코칭명)는 일부러 비워둔다 — 지사마다 제목 표기가 달라 텍스트 필터로는 누락이
    # 생기므로, 기간 내 전체를 받아온 뒤 parse_export()에서 구조적 필드로 골라낸다.
    vals = {k: "" for k in FILTER_KEYS}
    vals["sdt"], vals["edt"] = sdt, edt
    return vals


def fetch_export(session: requests.Session, sdt: str, edt: str) -> bytes:
    params = filter_dict(sdt, edt)
    # set_row_count.php는 돌아갈 목적지를 자신의 'url' 파라미터 값으로 인코딩해서 받는다
    return_qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return_url = f"/sb/search/coach.php?{return_qs}"

    # 1) 페이지당 PAGE_SIZE건으로 설정(세션에 저장됨) 후 목록 조회
    # (예전엔 1000으로 고정해 전체이력 백필(1784건) 등에서 조용히 잘려나가는 문제가 있었음)
    session.get(SET_ROWS, params={"url": return_url, "v": str(PAGE_SIZE)}, timeout=20)
    listing = session.get(LIST_PAGE, params=params, timeout=30)

    # 실제 마크업: 총 <a ...><i>1784</i></a>건의 데이터가 있습니다. (</i>와 "건의" 사이에 </a> 존재)
    total_m = re.search(r"<i>([\d,]+)</i>.*?건의 데이터", listing.text)
    total = int(total_m.group(1).replace(",", "")) if total_m else None
    # 실제 마크업은 홑따옴표(name='chk[]')를 쓰므로 id="chk숫자"로 잡는 게 안전함
    uids = list(dict.fromkeys(re.findall(r'id="chk(\d+)"', listing.text)))
    log(f"조회 기간 {sdt}~{edt}: 목록 {len(uids)}건 발견" + (f" (사이트 표시 총 {total}건)" if total else ""))
    if total is not None and len(uids) < total:
        log(f"⚠️ 페이지 크기({PAGE_SIZE})보다 데이터가 많아 일부가 잘렸을 수 있습니다 "
            f"(목록 {len(uids)}건 < 사이트 총 {total}건) — 기간을 좁혀 재시도하세요.")

    if not uids:
        return b""

    # 2) 체크된 UID + 동일 필터로 엑셀 내보내기 요청
    form = {k: "" for k in FILTER_KEYS}
    form["sdt"], form["edt"] = sdt, edt
    resp = session.post(EXPORT_URL, data=list(form.items()) + [("chk[]", u) for u in uids],
                        headers={"Referer": LIST_PAGE, "Origin": BASE}, timeout=60)
    ctype = resp.headers.get("Content-Type", "")
    if "sheet" not in ctype and "excel" not in ctype and not resp.content[:2] == b"PK":
        log(f"⚠️ 예상과 다른 응답(Content-Type={ctype}, 앞부분={resp.content[:80]!r}) — 내보내기 실패 가능성")
        return b""
    return resp.content


def parse_export(xlsx_bytes: bytes) -> pd.DataFrame:
    df = pd.read_excel(io.BytesIO(xlsx_bytes), header=1)
    df = df[df["코칭부서"].notna()].copy()
    before = len(df)
    df = filter_branch_records(df)
    log(f"파싱: 전체 {before}건 → 지사 주관/조치부서=OO지사 조건 통과 {len(df)}건")
    return df


DEDUP_KEYS = ["코칭자", "코칭부서", "코칭일자", "위험요소", "코칭내용"]
SUMMARY_FILE = DATA / "_last_sync_summary.json"


def merge_into_accumulator(new_df: pd.DataFrame) -> dict:
    """누적본에 병합하고, 실제로 새로 추가된 행만 골라 지사별 건수를 반환한다."""
    if RAW_ACCUM.exists():
        old_df = pd.read_excel(RAW_ACCUM)
        old_keys = set(map(tuple, old_df[DEDUP_KEYS].astype(str).values))
        newly_added_mask = ~new_df[DEDUP_KEYS].astype(str).apply(tuple, axis=1).isin(old_keys)
        newly_added = new_df[newly_added_mask]
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        newly_added = new_df
        combined = new_df.copy()
    combined = combined.drop_duplicates(subset=DEDUP_KEYS, keep="last")
    combined.to_excel(RAW_ACCUM, index=False)

    # 지사 귀속은 조치부서(실제 시정조치 대상) 기준 — clean_data.py와 동일한 기준을 사용해야
    # CSO EHS팀/그룹합동안전점검TF가 대신 코칭한 건도 실제 대상 지사로 정확히 잡힌다.
    by_branch = {
        k: int(v) for k, v in newly_added["조치부서"].value_counts().items()
        if BRANCH_DEPT_RE.match(str(k))
    }
    return {"total_added": len(newly_added), "by_branch": by_branch}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="최근 N일 강제 재수집")
    ap.add_argument("--init", type=int, default=None, help="최초 백필: 최근 N일")
    args = ap.parse_args()

    today = datetime.now()
    state = load_state()

    if args.init:
        sdt = today - timedelta(days=args.init)
    elif args.days:
        sdt = today - timedelta(days=args.days)
    elif state.get("last_synced"):
        # 마지막 동기화 이후, 3일 여유(지연 입력 대비)를 두고 재수집
        sdt = datetime.fromisoformat(state["last_synced"]) - timedelta(days=3)
    else:
        sdt = today - timedelta(days=14)  # 최초 실행 기본값: 최근 2주

    sdt_str, edt_str = sdt.strftime("%Y.%m.%d"), today.strftime("%Y.%m.%d")
    log(f"동기화 범위: {sdt_str} ~ {edt_str}")

    uid, pw = get_credentials()
    with requests.Session() as sess:
        sess.headers["User-Agent"] = "Mozilla/5.0 (safety-dashboard-sync)"
        login(sess, uid, pw)
        xlsx_bytes = fetch_export(sess, sdt_str, edt_str)

    if not xlsx_bytes:
        log("신규/변경 데이터 없음 또는 내보내기 실패 — 종료")
        SUMMARY_FILE.write_text(json.dumps(
            {"timestamp": today.isoformat(), "total_added": 0, "by_branch": {}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return

    new_df = parse_export(xlsx_bytes)
    merge_result = merge_into_accumulator(new_df)
    added = merge_result["total_added"]
    log(f"누적본 병합 완료 — 이번 수집 {len(new_df)}건 중 신규/갱신 {added}건 반영")
    for branch, cnt in merge_result["by_branch"].items():
        log(f"  · {branch}: 신규 {cnt}건")

    SUMMARY_FILE.write_text(json.dumps(
        {"timestamp": today.isoformat(), **merge_result}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 정제 파이프라인 재실행 (같은 인터프리터로 하위 프로세스 실행)
    # PYTHONIOENCODING을 명시적으로 물려줘서, 호출 부모(Streamlit 등)의 콘솔 코드페이지와
    # 무관하게 하위 스크립트의 한글/특수문자 print가 항상 안전하게 처리되도록 함
    sub_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    log("clean_data.py 재실행...")
    subprocess.run([sys.executable, str(Path(__file__).parent / "clean_data.py")], check=True, env=sub_env)
    log("mask_pii.py 재실행...")
    subprocess.run([sys.executable, str(Path(__file__).parent / "mask_pii.py")], check=True, env=sub_env)

    state["last_synced"] = today.isoformat()
    state["last_added"] = added
    save_state(state)
    log("동기화 완료")


if __name__ == "__main__":
    main()
