"""키움 업종코드 → 그룹 분류. 2026-07-18 65개 전수 실측 근거
(.superpowers/sdd/p3-pregate-sectors-paged.txt, 스펙 §3-2).

키움 ka10101 업종 목록은 산업 분류 외에 규모·등급·지수·집계 그룹이 섞여 있고
한 종목이 여러 그룹에 중복 소속된다. 스코어링(섹터 로테이션)은 industry만
소비한다. industry_umbrella(금융⊇증권·보험, 제조⊇제조 하위업종)는 하위
업종과 중복 집계를 피하기 위해 로테이션에서 제외한다."""

INDUSTRY = "industry"
UNCLASSIFIED = "unclassified"

_CLASSIFICATION: dict[str, str] = {
    # 집계 (시장 전체)
    "001": "aggregate", "101": "aggregate",
    # 규모
    "002": "size", "003": "size", "004": "size",          # 대·중·소형주
    "138": "size", "139": "size", "140": "size",          # KOSDAQ 100/MID/SMALL
    # 등급 (코스닥 소속부)
    "142": "quality", "143": "quality", "144": "quality", "145": "quality",
    # 지수 멤버십
    "603": "index", "604": "index", "605": "index",       # 변동성/고배당/배당성장
    "150": "index", "151": "index",                        # KOSDAQ150/글로벌지수
    "160": "index", "165": "index",                        # F-KOSDAQ150(인버스)
    # 우산 산업 (하위 업종 포함 — 중복 집계 방지 위해 로테이션 제외)
    "021": "industry_umbrella",   # kospi 금융 (⊇ 증권 024, 보험 025)
    "027": "industry_umbrella",   # kospi 제조 (실측 557명)
    "106": "industry_umbrella",   # kosdaq 제조 (실측 1,116명 = 시장 61%)
    # 산업 — kospi 22개
    "005": INDUSTRY, "006": INDUSTRY, "007": INDUSTRY, "008": INDUSTRY,
    "009": INDUSTRY, "010": INDUSTRY, "011": INDUSTRY, "012": INDUSTRY,
    "013": INDUSTRY, "014": INDUSTRY, "015": INDUSTRY, "016": INDUSTRY,
    "017": INDUSTRY, "018": INDUSTRY, "019": INDUSTRY, "020": INDUSTRY,
    "024": INDUSTRY, "025": INDUSTRY, "026": INDUSTRY, "028": INDUSTRY,
    "029": INDUSTRY, "030": INDUSTRY,
    # 산업 — kosdaq 21개
    "103": INDUSTRY, "107": INDUSTRY, "108": INDUSTRY, "110": INDUSTRY,
    "111": INDUSTRY, "115": INDUSTRY, "116": INDUSTRY, "117": INDUSTRY,
    "118": INDUSTRY, "119": INDUSTRY, "120": INDUSTRY, "121": INDUSTRY,
    "122": INDUSTRY, "123": INDUSTRY, "124": INDUSTRY, "125": INDUSTRY,
    "126": INDUSTRY, "127": INDUSTRY, "128": INDUSTRY, "129": INDUSTRY,
    "141": INDUSTRY,
}


def classify_sector(code: str) -> str:
    """업종코드의 그룹 분류. 미지 코드는 UNCLASSIFIED (소비 제외 + 경고는
    호출자 책임 — CollectionService 참고)."""
    return _CLASSIFICATION.get(code, UNCLASSIFIED)
