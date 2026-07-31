# -*- coding: utf-8 -*-
"""
PII 마스킹 파이프라인
- 대상: 사고_clean.csv (사고자/사고개요/사고원인/조치사항에 실명·차량번호 포함)
- 점검_clean.csv 지적내용/조치내용(원문+평문)도 차량번호 패턴은 전량 마스킹하고, 차량번호와
  같은 괄호 안에 붙어있는 이름(예: "(임창환, 경기98아1149)")도 함께 마스킹한다. 자유서술문
  전체를 무차별 스캔하지는 않는다 — 그 방식은 일반명사까지 대량 오매칭된 전례가 있음.
- 출력:
  - data/사고_masked.csv, data/점검_masked.csv  → LLM API로 전송 가능(외부 유출용)
  - data/pii_mapping.json                       → 실명 복원용 로컬 전용 파일. **절대 API로 전송·외부 공유 금지**
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

# Windows 콘솔(cp949)에서 em-dash 등 유니코드 print 시 크래시 방지
# (subprocess로 실행될 때 부모 프로세스가 PYTHONIOENCODING을 안 물려주는 경우 대비)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA = Path(__file__).parent / "data"

# ---------------------------------------------------------------------------
# 차량번호 패턴: (지역 0~2자)+숫자2~3자리+한글1자+숫자4자리  예: 전남98사7433, 05구2091
PLATE_PAT = re.compile(r'[가-힣]{0,2}\d{2,3}[가-힣]\d{4}')

# 이름 후보에서 제외할 조직/직급/역할/지명/선박용어 (이 단어와 일치하는 토큰은 이름이 아님)
ORG_ROLE_STOP = set("""
지사 지점 본사 협력사 위수탁 항운 노조 노동조합 연락소 소속 부두 항만 물류 팀장 팀
반 조 센터 공장 부서 관리자 담당자 사원 선임 책임 주임 기사 대리 과장 차장 부장
운전원 운전자 기술사원 근로자 작업자 직원 인원 화주 협력업체 업체 회사 세방 제품지부
동해 광양 부산 경인 전북 강원 경남 경북 삼천포 목포 당진 인천 경기 울산 창원 군산
포항 제주 서울 대전 대구 광주 담당 관리 사고자 상대방 피해자 재해자 운전 차량 내수
운송 하역 중량 물류팀 항만항운 직계약 위탁 소속직원 본선 선박 일반부두 전용부두
전북지사 경남지사 경인지사 광양지사 강원지사 당진지사 목포지사 부산지사 삼천포지사 경북지사
기타 없음 미상 확인중 세명소속
""".split())

# 사고자 필드 형식: "조직/직급 설명 + 이름 + (선택 부가정보)". 괄호·차량번호·조직어를 제거한 뒤
# 남는 순한글 2~4자 토큰만 이름 후보로 채택한다(자유서술문 전체에서 무차별 추출하지 않음 —
# 그 방식은 일반명사까지 대량 오매칭되어 본문 의미를 훼손함).
NAME_TOKEN_PAT = re.compile(r'[가-힣]{2,4}')


def extract_names_from_person_field(series: pd.Series) -> set:
    names = set()
    for text in series.dropna().astype(str):
        s = re.sub(r'\([^)]*\)', ' ', text)   # 괄호 내용(생년, 차량번호 등) 제거
        s = PLATE_PAT.sub(' ', s)              # 잔여 차량번호 제거
        for tok in re.split(r'[\s/,·]+', s):
            tok = tok.strip()
            if re.fullmatch(NAME_TOKEN_PAT, tok) and tok not in ORG_ROLE_STOP:
                names.add(tok)
    return names


PAREN_GROUP_PAT = re.compile(r'\(([^)]*)\)')


def extract_names_near_plates(series: pd.Series) -> set:
    """차량번호와 같은 괄호 안에 붙어있는 이름만 좁게 추출한다.
    예: "운전원(임창환, 경기98아1149)" → '임창환'. 괄호 밖 자유서술문은 건드리지 않는다 —
    차량번호와 동반 등장하는 문맥만 '특정 개인 식별' 위험이 실제로 확인된 패턴이기 때문."""
    names = set()
    for text in series.dropna().astype(str):
        for group in PAREN_GROUP_PAT.findall(text):
            if not PLATE_PAT.search(group):
                continue
            g = PLATE_PAT.sub(' ', group)
            for tok in re.split(r'[\s,/·]+', g):
                tok = tok.strip().rstrip('호')
                if re.fullmatch(NAME_TOKEN_PAT, tok) and tok not in ORG_ROLE_STOP:
                    names.add(tok)
    return names


def _names_to_mapping(names: set, start: int = 1) -> dict:
    # 긴 이름부터 치환해야 부분 문자열 오치환을 방지(예: '김철수' 안의 '김철' 오매칭 방지)
    ordered = sorted(names, key=lambda x: -len(x))
    return {name: f"PERSON_{i:04d}" for i, name in enumerate(ordered, start=start)}


def build_acc_mapping(acc: pd.DataFrame) -> dict:
    """사고 데이터 '사고자' 필드 전용 매핑. 이 필드는 '이름/직책/회사' 형식의 정형 서술이라
    ORG_ROLE_STOP으로 걸러도 회사명·장비명 등 완전히 걸러지지 않는 잔여 오탐이 섞여 있을 수
    있음 — 그래도 이 매핑은 사고 데이터에만 적용되므로 점검 텍스트의 일반 어휘와는 충돌하지
    않는다(아래 build_insp_mapping과 분리 운영하는 이유)."""
    names = extract_names_from_person_field(acc["사고자"]) if "사고자" in acc.columns else set()
    return _names_to_mapping(names)


def build_insp_mapping(insp: pd.DataFrame, start: int) -> dict:
    """점검 텍스트 전용 매핑. 차량번호와 동반 등장하는 이름만 좁게 잡는다 — 사고자 매핑을
    그대로 재사용하면 그 목록에 섞인 회사명·장비명(예: '지게차')이 점검 텍스트의 일상 어휘와
    충돌해 대량 오치환을 일으키므로, 반드시 별도 매핑을 사용해야 한다."""
    names = set()
    for col in ["지적내용", "조치내용", "지적내용_평문", "조치내용_평문"]:
        if col in insp.columns:
            names |= extract_names_near_plates(insp[col])
    return _names_to_mapping(names, start=start)


def mask_text(text, mapping: dict) -> str:
    if pd.isna(text):
        return text
    s = str(text)
    s = PLATE_PAT.sub("차량번호", s)
    for name, pid in mapping.items():
        if name in s:
            s = s.replace(name, pid)
    return s


def main():
    insp = pd.read_csv(DATA / "점검_clean.csv")
    acc = pd.read_csv(DATA / "사고_clean.csv")

    acc_mapping = build_acc_mapping(acc)
    insp_mapping = build_insp_mapping(insp, start=len(acc_mapping) + 1)
    print(f"추출된 이름 후보: 사고자 {len(acc_mapping)}개 + 점검텍스트(차량번호 동반) {len(insp_mapping)}개")

    # ---- 사고 데이터 마스킹 ----
    acc_masked = acc.copy()
    for col in ["사고자", "사고개요", "사고원인", "조치사항"]:
        if col in acc_masked.columns:
            acc_masked[col] = acc_masked[col].apply(lambda t: mask_text(t, acc_mapping))
    acc_masked.to_csv(DATA / "사고_masked.csv", index=False, encoding="utf-8-sig")

    # ---- 점검 데이터 마스킹: 차량번호는 전량, 이름은 점검텍스트 전용 매핑만 사용 ----
    # (사고자 매핑을 재사용하면 그 목록에 섞인 회사명·장비명이 점검 텍스트의 일상 어휘와
    #  충돌해 대량 오치환을 일으키므로 절대 재사용하지 않는다)
    insp_masked = insp.copy()
    for col in ["지적내용", "조치내용", "지적내용_평문", "조치내용_평문"]:
        if col in insp_masked.columns:
            insp_masked[col] = insp_masked[col].apply(lambda t: mask_text(t, insp_mapping))
    inspector_names = sorted(insp["점검자"].unique())
    inspector_map = {n: f"INSPECTOR_{i+1:02d}" for i, n in enumerate(inspector_names)}
    insp_masked["점검자"] = insp_masked["점검자"].map(inspector_map)
    insp_masked.to_csv(DATA / "점검_masked.csv", index=False, encoding="utf-8-sig")

    # ---- 로컬 전용 매핑 파일 (외부 전송 금지) ----
    full_mapping = {"사고자_매핑": acc_mapping, "점검텍스트_매핑": insp_mapping, "점검자_매핑": inspector_map}
    with open(DATA / "pii_mapping.json", "w", encoding="utf-8") as f:
        json.dump(full_mapping, f, ensure_ascii=False, indent=2)

    print(f"완료: 사고_masked.csv({len(acc_masked)}행), 점검_masked.csv({len(insp_masked)}행)")
    print(f"매핑 파일: data/pii_mapping.json (로컬 전용 — 절대 외부/API 전송 금지)")

    # ---- 검증: 마스킹 후 남은 차량번호 패턴 잔존 여부 점검 (사고+점검 전체) ----
    # PERSON_0734/INSPECTOR_02 같은 치환 토큰의 숫자가 바로 뒤 시각 표기(예: '1400시경')와
    # 우연히 붙어 PLATE_PAT과 충돌하는 가짜 잔존을 배제하기 위해, 검증 전 치환 토큰을 지운다.
    PLACEHOLDER_PAT = re.compile(r'(?:PERSON|INSPECTOR)_\d+')
    residual = 0
    for df_, cols in [
        (acc_masked, ["사고자", "사고개요", "사고원인", "조치사항"]),
        (insp_masked, ["지적내용", "조치내용", "지적내용_평문", "조치내용_평문"]),
    ]:
        for col in cols:
            if col not in df_.columns:
                continue
            for t in df_[col].dropna().astype(str):
                if PLATE_PAT.search(PLACEHOLDER_PAT.sub(' ', t)):
                    residual += 1
    print(f"검증: 마스킹 후에도 차량번호 패턴 남은 셀 수 = {residual} (0이어야 정상)")


if __name__ == "__main__":
    main()
