# -*- coding: utf-8 -*-
"""`온도를 조져보자`의 일일 안전보건 브리핑 스냅샷만 이 저장소로 동기화 + git push한다
(2026-08-20 분리 — 예전엔 sync_heatwave_data.py/run_heatwave_cycle.py가 폭염 관측
데이터와 한 묶음으로 동기화했는데, HeatwaveCycle 스케줄러가 불안정해지면서 그 주기에
얹혀 있던 일일 브리핑까지 같이 지연되는 문제가 실측으로 확인됐다(2026-08-20).
DailyReportCycle이 발송 직후 이 스크립트를 직접 호출해, HeatwaveCycle의 실행 여부와
무관하게 브리핑이 대시보드에 반영되도록 분리한다.

daily_report.py의 run()에서 실발송(dry_run/preview 아닌 경우)에만 호출된다 — 매번
테스트 실행마다 커밋이 쌓이는 걸 막기 위함(run_heatwave_cycle.py의 "변경 없으면
커밋 생략" 관행과 같은 원칙)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Windows 작업 스케줄러(콘솔 없음/cp949)에서 한글 출력이 UnicodeEncodeError로 죽는
# 걸 방지한다 — run_heatwave_cycle.py와 같은 안전장치(2026-08-20, 이 스크립트를
# 처음 테스트할 때 실제로 이 문제로 크래시하는 걸 확인하고 추가했다).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SOURCE_FILE = Path(r"C:\Users\윤용호\Desktop\온도를 조져보자\data\daily_briefing_latest.html")
DASH_DIR = Path(r"C:\Users\윤용호\Desktop\나무발발이")
DEST_FILE = DASH_DIR / "heatwave_data" / "daily_briefing_latest.html"


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


def main() -> int:
    if not SOURCE_FILE.exists():
        print(f"원본 파일을 찾을 수 없습니다: {SOURCE_FILE}", file=sys.stderr)
        return 1

    DEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_FILE, DEST_FILE)
    print(f"복사됨: {DEST_FILE.name}")

    status = subprocess.run(
        ["git", "status", "--porcelain", "heatwave_data/daily_briefing_latest.html"],
        cwd=DASH_DIR, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print("변경 없음 — 커밋 생략")
        return 0

    run(["git", "add", "heatwave_data/daily_briefing_latest.html"], cwd=DASH_DIR)
    run(["git", "commit", "-m", "자동 갱신: 일일 안전보건 브리핑"], cwd=DASH_DIR)
    push = run(["git", "push"], cwd=DASH_DIR)
    if push.returncode != 0:
        print("git push 실패 — 인증이 만료됐을 수 있습니다. 터미널에서 수동으로 git push 한 번 해보세요.",
              file=sys.stderr)
        return 1

    print("완료 — Streamlit Cloud가 곧 자동 재배포합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
