# -*- coding: utf-8 -*-
"""pytest가 프로젝트 루트의 scoring.py/classify_words.py/mask_pii.py 등을 import할 수 있도록
루트 디렉터리를 sys.path에 추가한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
