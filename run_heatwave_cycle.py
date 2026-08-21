# -*- coding: utf-8 -*-
"""주기 실행용 통합 스크립트(폭염 관측 전용) — Windows 작업 스케줄러(HeatwaveCycle)에
이 스크립트 하나만 등록하면 된다.

매 실행마다 순서대로:
1. `온도를 조져보자/main.py` 실행 — 기상청 API허브 조회 → alerts.db 갱신 → 임계치 초과 시 알림 발송
2. `나무발발이/sync_heatwave_data.py` 실행 — 최신 alerts.db/sites.yaml/체크리스트를 대시보드 저장소 안으로 복사
3. heatwave_data/ 폴더에 변경사항이 있으면 git commit + push
   → Streamlit Cloud가 push를 감지해 1분 내 자동 재배포한다.

2026-08-20 — 일일 안전보건 브리핑은 여기서 분리됐다. 이 스케줄러가 지연되면
브리핑까지 같이 지연되는 문제가 있어(실측 확인), daily_report.py가 발송 직후
`sync_daily_briefing.py`를 직접 호출해 독립적으로 배포한다. 이 스크립트는 이제
폭염 관측 데이터만 책임진다 — daily_briefing_latest.html은 신경 쓸 필요 없다.

전제조건: 이 PC에서 `git push`가 이미 한 번 성공해 인증정보가 캐시돼 있어야 한다
(무인 실행이라 로그인 창이 뜨면 멈춘다).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Windows 작업 스케줄러(콘솔 없음/cp949)에서 하위 프로세스가 돌려주는 한글·이모지 출력을
# print()할 때 UnicodeEncodeError로 죽는 걸 방지한다 — 이 크래시가 나면 뒤에 있는
# git add/commit/push가 아예 실행되지 않아 대시보드가 조용히 갱신을 멈춘다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WEATHER_DIR = Path(r"C:\Users\윤용호\Desktop\온도를 조져보자")
DASH_DIR = Path(r"C:\Users\윤용호\Desktop\나무발발이")


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}  (in {cwd.name})")
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.stdout.strip():
        print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    return result


def main() -> None:
    run([sys.executable, "main.py"], cwd=WEATHER_DIR)
    run([sys.executable, "sync_heatwave_data.py"], cwd=DASH_DIR)

    status = subprocess.run(
        ["git", "status", "--porcelain", "heatwave_data"],
        cwd=DASH_DIR, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print("변경 없음 — 커밋 생략")
        return

    run(["git", "add", "heatwave_data"], cwd=DASH_DIR)
    run(["git", "commit", "-m", "자동 갱신: 폭염 관측 데이터"], cwd=DASH_DIR)
    push = run(["git", "push"], cwd=DASH_DIR)
    if push.returncode != 0:
        print("git push 실패 — 인증이 만료됐을 수 있습니다. 터미널에서 수동으로 git push 한 번 해보세요.",
              file=sys.stderr)
        sys.exit(1)

    print("완료 — Streamlit Cloud가 곧 자동 재배포합니다.")


if __name__ == "__main__":
    main()
