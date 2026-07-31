# -*- coding: utf-8 -*-
"""
mask_pii.py 회귀 테스트.
- 오늘 발견한 두 가지 실제 사고를 회귀 테스트로 고정한다:
  1) 차량번호와 같은 괄호 안의 실명은 반드시 마스킹되어야 한다(경인지사 노출 사고).
  2) 사고자 매핑(회사명/장비명 오탐 포함 가능)을 점검 텍스트에 재사용해
     '지게차' 같은 일상 어휘가 대량으로 뭉개지면 안 된다.
"""
import pandas as pd

import mask_pii as M


def test_plate_pattern_matches_real_examples():
    assert M.PLATE_PAT.search("경기98아1149")
    assert M.PLATE_PAT.search("부산04나7158")
    assert M.PLATE_PAT.search("04라7451")


def test_extract_names_near_plates_catches_real_leak_case():
    """회귀 테스트: '(임창환, 경기98아1149)' 같은 패턴에서 이름이 추출돼야 한다."""
    series = pd.Series(["타사 소속 운전원(임창환, 경기98아1149) 1인 하차"])
    names = M.extract_names_near_plates(series)
    assert "임창환" in names


def test_extract_names_near_plates_does_not_catch_equipment_id():
    """차량번호가 장비 고유번호로만 쓰인 경우(사람 이름이 없는 괄호)는 아무 것도 추출하면 안 된다."""
    series = pd.Series([
        "용연부두 용차 지게차 (04라7451호) 경광등 미작동",
        "의왕CY 운용 중인 리치스태커 1대(부산04나7158) 방호장치 불량",
    ])
    names = M.extract_names_near_plates(series)
    assert names == set()


def test_mask_text_replaces_plate_and_mapped_names():
    mapping = {"임창환": "PERSON_0001"}
    out = M.mask_text("운전원(임창환, 경기98아1149) 확인", mapping)
    assert "임창환" not in out
    assert "경기98아1149" not in out
    assert "PERSON_0001" in out
    assert "차량번호" in out


def test_mask_text_is_noop_on_unrelated_vocabulary():
    """사고자 매핑에 우연히 낀 일반명사(예: '지게차')가 점검 텍스트를 훼손하면 안 된다는
    회귀 방지 테스트 — 점검 전용 매핑에는 애초에 이런 단어가 들어가면 안 된다."""
    insp_mapping = {"임창환": "PERSON_0001"}  # build_insp_mapping이 만드는 좁은 매핑을 흉내
    text = "지게차 안전교육 실시 및 사전 지게차 오더 배차시 관리감독자인 포맨 확인"
    out = M.mask_text(text, insp_mapping)
    assert out == text  # 변경 없이 원문 그대로(차량번호 패턴도 없음)
    assert "지게차" in out
    assert "PERSON_" not in out


def test_build_insp_mapping_scoped_to_plate_adjacent_names_only():
    insp = pd.DataFrame({
        "지적내용": [
            "타사 소속 운전원(임창환, 경기98아1149) 1인 하차",
            "지게차 안전교육 실시 및 배차 관리감독 강화 필요",
        ]
    })
    mapping = M.build_insp_mapping(insp, start=1)
    assert "임창환" in mapping
    assert "지게차" not in mapping
    assert "관리감독" not in mapping
