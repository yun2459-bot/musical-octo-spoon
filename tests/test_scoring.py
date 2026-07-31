# -*- coding: utf-8 -*-
"""
scoring.py 핵심 집계 함수 회귀 테스트.
실제 data/*.csv에 의존하지 않고, 작은 합성 데이터로 계산 로직만 검증한다
(데이터 파일이 주간 동기화로 계속 바뀌기 때문에 실데이터에 의존하면 테스트가 깨지기 쉬움).
"""
import pandas as pd
import pytest

import scoring as S


def make_insp(rows):
    """rows: [(지사, 점검일자, 점검자, 사업장, 심각도점수)]"""
    df = pd.DataFrame(rows, columns=["지사", "점검일자", "점검자", "사업장", "심각도점수"])
    df["점검일자"] = pd.to_datetime(df["점검일자"])
    return df


def test_branch_risk_index_basic_ranking():
    """심각도 합이 큰 지사가 위험지수 100(최대)을 받아야 한다."""
    insp = make_insp([
        ("A지사", "2026-01-01", "김철수", "1공장", 5),
        ("A지사", "2026-01-02", "김철수", "1공장", 5),
        ("B지사", "2026-01-01", "이영희", "2공장", 2),
    ])
    out = S.branch_risk_index(insp, severity_col="심각도점수")
    a = out[out["지사"] == "A지사"].iloc[0]
    b = out[out["지사"] == "B지사"].iloc[0]
    assert a["위험지수"] == 100.0
    assert a["가중점수"] == 10
    assert b["가중점수"] == 2
    assert b["위험지수"] == pytest.approx(20.0)  # 2/10*100


def test_branch_risk_index_per_site_normalization():
    """사업장당 지수는 사업장 수로 나눈 뒤 재정규화되어야 한다 —
    총량은 같아도 사업장이 많으면 사업장당 지수는 낮아진다."""
    insp = make_insp([
        ("A지사", "2026-01-01", "김철수", "1공장", 10),
        ("B지사", "2026-01-01", "이영희", "1공장", 5),
        ("B지사", "2026-01-02", "이영희", "2공장", 5),
    ])
    out = S.branch_risk_index(insp, severity_col="심각도점수").set_index("지사")
    # 가중점수는 둘 다 10으로 동일
    assert out.loc["A지사", "가중점수"] == out.loc["B지사", "가중점수"] == 10
    # A지사는 사업장 1곳이라 사업장당지수가 더 높아야 함(10/1 > 10/2)
    assert out.loc["A지사", "사업장당지수"] > out.loc["B지사", "사업장당지수"]


def test_branch_risk_index_empty_input_returns_empty_frame():
    """빈 입력에서 예외 없이 빈 DataFrame(정해진 컬럼 포함)을 반환해야 한다."""
    insp = make_insp([])
    out = S.branch_risk_index(insp, severity_col="심각도점수")
    assert out.empty
    assert "위험지수" in out.columns


def test_branch_risk_index_period_filter():
    """period로 지정한 구간 밖 데이터는 집계에서 제외되어야 한다."""
    insp = make_insp([
        ("A지사", "2026-01-01", "김철수", "1공장", 10),
        ("A지사", "2026-06-01", "김철수", "1공장", 10),
    ])
    period = (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-31"))
    out = S.branch_risk_index(insp, period=period, severity_col="심각도점수")
    assert out.iloc[0]["가중점수"] == 10  # 6월 데이터는 제외되어야 함


def test_hazard_intensity_pivot_shape():
    df = pd.DataFrame({
        "지사": ["A지사", "A지사", "B지사"],
        "위험분류": ["추락", "충돌", "추락"],
        "심각도점수": [3, 2, 1],
    })
    out = S.hazard_intensity(df, severity_col="심각도점수")
    assert out.loc["A지사", "추락"] == 3
    assert out.loc["A지사", "충돌"] == 2
    assert out.loc["B지사", "추락"] == 1


def test_past_injury_matrix_weighting():
    """산재>미분류>공상 순으로 가중치가 반영되어야 한다(1.0 > 0.8 > 0.6)."""
    acc = pd.DataFrame({
        "지사": ["A지사", "A지사", "A지사"],
        "위험분류": ["추락", "추락", "추락"],
        "재해성격": ["인적재해"] * 3,
        "산재구분": ["산재", "미분류", "공상"],
    })
    mat, cnt = S.past_injury_matrix(acc)
    assert mat.loc["A지사", "추락"] == pytest.approx(1.0 + 0.8 + 0.6)
    assert cnt.loc["A지사", "추락"] == 3
