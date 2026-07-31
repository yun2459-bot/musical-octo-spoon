# -*- coding: utf-8 -*-
"""
classify_words.py의 절대 기준점 공식(S_i x R_j) 및 문맥 규칙 엔진 회귀 테스트.
- 오늘 고친 '무리한동작=위험증상 역할이 0으로 무력화되던 버그'가 되살아나지 않도록
  회귀 테스트로 고정해둔다.
"""
import re

import classify_words as C


def test_hazard_si_role_rj_formula():
    """S_i x R_j 곱셈이 정확한지 확인."""
    hazard, role, score, note = C.classify_formula("추락", [], syn_map={})
    assert hazard == "추락"
    assert role == "위험행위·현상"
    assert score == 4.0 * 1.0


def test_murihan_dongjak_role_not_zeroed_out():
    """회귀 테스트: '무리한동작' 카테고리 단어(위험증상 역할)가 예전처럼 0점으로
    무력화되지 않고 위해도(2.0) x 위험행위·현상(1.0) = 2.0으로 정상 집계돼야 한다."""
    hazard, role, score, note = C.classify_formula("허리", [], syn_map={})
    assert hazard == "무리한동작"
    assert role == "위험행위·현상"
    assert score == 2.0


def test_zero_si_category_label_collapses_to_general():
    """S_i=0인 카테고리(관리적미흡 등)는 라벨이 '일반'으로 접혀야 한다."""
    hazard, role, score, note = C.classify_formula("미흡", [], syn_map={})
    assert hazard == "일반"
    assert score == 0.0


def test_irrelevant_word_returns_zero():
    hazard, role, score, note = C.classify_formula("세방", [], syn_map={})
    assert hazard == "무관(고유명사)"
    assert score == 0.0


def test_oov_word_defaults_safely_without_arbitrary_tuning():
    """사전에 전혀 없는 신규 단어(OOV)는 임의 조율 없이 안전한 기본값(일반/0점)이어야 한다."""
    hazard, role, score, note = C.classify_formula("완전히새로운단어", [], syn_map={})
    assert hazard == "일반"
    assert role == "일반·무관어"
    assert score == 0.0
    assert "OOV" in note


def test_sanso_reclassified_as_fire_not_asphyxiation():
    """회귀 테스트: 'KOSHA 코퍼스 검증(2026-07)'으로 '산소'를 질식→화재·폭발로 재분류함
    (코퍼스 8건 전부 산소절단/산소용접 등 화기작업 문맥, 질식 문맥 0건).
    docs/RESEARCH_METHODOLOGY_LOG.md 2026-07-27 항목 참고."""
    hazard, role, score, note = C.classify_formula("산소", [], syn_map={})
    assert hazard == "화재·폭발"
    assert score == 4.0 * 1.0


def test_synonym_map_overrides_word_risk():
    """Phase 1(동의어 매핑)이 WORD_RISK 사전보다 우선 적용돼야 한다."""
    syn_map = {"추락": ("추락", 4.0, 0.5, "추락")}  # 위치구조(0.5)로 강제 override
    hazard, role, score, note = C.classify_formula("추락", [], syn_map=syn_map)
    assert score == 4.0 * 0.5
    assert "동의어 매핑" in note


def test_context_rule_invalidation():
    """Phase 2: 위험 단어라도 개인적 일탈 문맥(예: 장난)이면 0점으로 무효화돼야 한다."""
    rules = [{
        "target": "치여",
        "cond_re": re.compile("장난"),
        "adjust": "무효화 (Score=0.0)",
        "note": "개인적 일탈 문맥 오탐지 방지",
    }]
    hazard, role, score, _ = C.apply_context_rules(
        "치여", "충돌", "위험행위·현상", 2.0, ["작업 중 장난치다 부딪힘"], rules)
    assert score == 0.0


def test_context_rule_max_risk_override():
    """Phase 2: 안전장치 미설치 등 조건이 맞으면 최고위험(S_i=4.0)으로 상향돼야 한다."""
    rules = [{
        "target": "안전난간",
        "cond_re": re.compile("미설치|파손"),
        "adjust": "최고 위험 부여 (S_i=4.0 적용)",
        "note": "방호 장치 무력화",
    }]
    hazard, role, score, _ = C.apply_context_rules(
        "안전난간", "추락", "장비시설물", 3.2, ["안전난간 미설치 확인"], rules)
    assert score == 4.0 * 0.8  # 장비시설물 R_j=0.8 유지, S_i만 4.0으로 상향
