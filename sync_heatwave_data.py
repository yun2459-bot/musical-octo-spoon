# -*- coding: utf-8 -*-
"""`온도를 조져보자` 프로젝트의 최신 산출물을 이 저장소 안(heatwave_data/)으로 복사한다.

Streamlit Cloud는 이 깃 저장소 밖의 파일(로컬 절대경로)에 접근할 수 없으므로, 배포판은
항상 이 폴더 안의 스냅샷을 읽는다. main.py를 새로 돌린 뒤 이 스크립트를 실행하고
git add/commit/push 하면 클라우드에도 최신 값이 반영된다(Streamlit Cloud는 push 시 자동 재배포).
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import yaml

SOURCE = Path(r"C:\Users\윤용호\Desktop\온도를 조져보자")
DEST = Path(__file__).parent / "heatwave_data"

FILES = [
    SOURCE / "data" / "alerts.db",
    SOURCE / "config" / "sites.yaml",
    SOURCE / "config" / "checklist_pool.yaml",  # 재해별 점검항목 풀(2026-08-10) — PII 없음, 그대로 복사
    # 일일 안전보건 브리핑(뉴스+DART+판결문+날씨) 최신 스냅샷(2026-08-13 추가) —
    # "📰 일일 안전보건 브리핑" 탭이 그대로 읽어서 보여준다. 뉴스 제목/URL,
    # DART 공시, 판례 사건명/URL, 특보만 담겨 있고 개인정보(수신자 이름·전화번호
    # 등)는 없다 — sites.yaml과 달리 익명화 없이 그대로 복사해도 된다.
    SOURCE / "data" / "daily_briefing_latest.html",
]
# Map_of_South_Korea-blank.svg는 카카오맵 전환 후 app.py/heatwave.py 어디서도
# 더 이상 참조하지 않는 죽은 파일이라(2026-08-13 확인) 목록에서 뺐다 — 원본은
# 온도를 조져보자/docs/참고자료/로 옮겨져 있다. heatwave_data/의 예전 사본은
# 남겨둬도 무해하니 그대로 둔다.

# 010-1234-5678 / 01012345678 등 휴대폰번호로 보이는 패턴 — sites.yaml을 이 public
# 저장소로 복사하기 직전 마지막 안전장치로 쓴다. 지금은 recipients 필드 하나만
# 걸러내면 되지만, 나중에 다른 개인정보 필드가 실수로 추가돼도(예: 필드명을 빼먹고
# 커밋) 이 스캔이 잡아내도록 필드명이 아니라 "패턴 자체"를 검사한다.
_PHONE_PATTERN = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")


def _sync_sites_yaml_without_recipients(src: Path, dst: Path) -> None:
    """sites.yaml은 지사별 담당자 이름·휴대폰번호(recipients)를 담고 있어 원본 그대로
    복사하면 안 된다 — 이 저장소(나무발발이)는 public GitHub라, 그대로 커밋되면 개인
    연락처가 그대로 공개된다(2026-08-06, 경인 지사 3명 등록 직후 발견해 긴급 수정).
    recipients 필드를 뺀 사본만 만들고, 그래도 휴대폰번호 패턴이 남아있으면(다른
    필드에 실수로 개인정보가 들어간 경우 등) 아예 파일을 쓰지 않고 중단한다.
    """
    with open(src, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for branch in data.get("branches", []):
        branch.pop("recipients", None)
    output = yaml.dump(data, allow_unicode=True, sort_keys=False)
    if _PHONE_PATTERN.search(output):
        raise RuntimeError(
            "sites.yaml에서 recipients를 뺀 뒤에도 휴대폰번호로 보이는 패턴이 남아있어 "
            "동기화를 중단합니다 — 개인정보가 public 저장소에 올라갈 위험이 있습니다. "
            "원본에 새로 추가된 개인정보 필드가 있는지 확인해주세요."
        )
    with open(dst, "w", encoding="utf-8") as f:
        f.write(output)


def main() -> None:
    DEST.mkdir(exist_ok=True)
    missing = [f for f in FILES if not f.exists()]
    if missing:
        print("다음 원본 파일을 찾을 수 없습니다:")
        for f in missing:
            print(f"  - {f}")
        sys.exit(1)

    for f in FILES:
        if f.name == "sites.yaml":
            _sync_sites_yaml_without_recipients(f, DEST / f.name)
            print(f"복사됨(recipients 제외): {f.name}")
            continue
        shutil.copy2(f, DEST / f.name)
        print(f"복사됨: {f.name}")

    print(f"완료: {DEST}")


if __name__ == "__main__":
    main()
