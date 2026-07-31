# -*- coding: utf-8 -*-
"""`온도를 조져보자` 프로젝트의 최신 산출물을 이 저장소 안(heatwave_data/)으로 복사한다.

Streamlit Cloud는 이 깃 저장소 밖의 파일(로컬 절대경로)에 접근할 수 없으므로, 배포판은
항상 이 폴더 안의 스냅샷을 읽는다. main.py를 새로 돌린 뒤 이 스크립트를 실행하고
git add/commit/push 하면 클라우드에도 최신 값이 반영된다(Streamlit Cloud는 push 시 자동 재배포).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SOURCE = Path(r"C:\Users\윤용호\Desktop\온도를 조져보자")
DEST = Path(__file__).parent / "heatwave_data"

FILES = [
    SOURCE / "data" / "alerts.db",
    SOURCE / "config" / "sites.yaml",
    SOURCE / "Map_of_South_Korea-blank.svg",
]


def main() -> None:
    DEST.mkdir(exist_ok=True)
    missing = [f for f in FILES if not f.exists()]
    if missing:
        print("다음 원본 파일을 찾을 수 없습니다:")
        for f in missing:
            print(f"  - {f}")
        sys.exit(1)

    for f in FILES:
        shutil.copy2(f, DEST / f.name)
        print(f"복사됨: {f.name}")

    print(f"완료: {DEST}")


if __name__ == "__main__":
    main()
