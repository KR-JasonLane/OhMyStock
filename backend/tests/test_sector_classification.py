"""업종 분류 맵 검증 — 2026-07-18 65개 전수 실측 근거
(.superpowers/sdd/p3-pregate-sectors-paged.txt, 스펙 §3-2)."""

from app.domain.sector_classification import (INDUSTRY, UNCLASSIFIED,
                                              classify_sector)


def test_집계_규모_등급_지수_분류():
    assert classify_sector("001") == "aggregate"
    assert classify_sector("101") == "aggregate"
    assert classify_sector("002") == "size"       # 대형주
    assert classify_sector("140") == "size"       # KOSDAQ SMALL
    assert classify_sector("142") == "quality"    # 코스닥 우량기업
    assert classify_sector("150") == "index"      # KOSDAQ 150
    assert classify_sector("603") == "index"      # 변동성지수


def test_우산_업종():
    assert classify_sector("027") == "industry_umbrella"  # kospi 제조
    assert classify_sector("106") == "industry_umbrella"  # kosdaq 제조(시장 61%)


def test_하위_산업은_로테이션_제외():
    """021(금융) ⊇ 024∪025 완전 포함 실측(p3-task-1-finance-probe.txt) —
    상위 021이 industry, 완전 포함된 024/025는 industry_sub로 중복 집계 방지."""
    assert classify_sector("024") == "industry_sub"  # 증권 ⊂ 금융
    assert classify_sector("025") == "industry_sub"  # 보험 ⊂ 금융


def test_산업_업종():
    for code in ("005", "008", "013", "021", "030", "103", "120", "141"):
        assert classify_sector(code) == INDUSTRY


def test_미지_코드는_unclassified():
    assert classify_sector("999") == UNCLASSIFIED


def test_분류_전수_개수():
    """실측 65개 코드가 전부 맵에 있고 industry는 kospi 21 + kosdaq 21."""
    from app.domain.sector_classification import _CLASSIFICATION
    assert len(_CLASSIFICATION) == 65
    assert sum(1 for v in _CLASSIFICATION.values() if v == INDUSTRY) == 42


def test_instrument_상태_필드_기본값():
    from app.domain.broker import Instrument
    i = Instrument(symbol="005930", name="삼성전자", market="kospi",
                   instrument_type="A")
    assert i.state == "" and i.audit_info == ""
