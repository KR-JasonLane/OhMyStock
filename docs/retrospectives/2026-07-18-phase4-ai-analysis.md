# 회고록 — Phase 4: AI 멀티에이전트 분석 (2026-07-18)

> 규칙 4(원자적 작업 + 회고록)에 따른 기록. 비전문가도 따라올 수 있도록
> "무엇을 요청받아, 무엇을 만들었고, 어떤 결함이 잡혔고, 무엇이 남았는지"를
> 순서대로 기록한다. 진행 원장(작업 중 실시간 기록)은
> `.superpowers/sdd/progress.md`.

## 1. 무엇을 요청받았나

Phase 3 스코어링이 뽑은 후보 종목(업종·전략 점수 상위)을 **LLM 두 에이전트가
필터링**하는 단계. 브레인스토밍에서 사용자가 확정한 설계:

- **economist 에이전트**: 시장 폭(breadth)·업종 표·시장 뉴스로 국면(regime:
  risk_on/neutral/risk_off)과 "오늘 최대 몇 종목까지"(max_picks_advice) 판단.
- **trader 에이전트**: 후보 종목별로 점수·전략 통계·종목 뉴스를 보고
  approve/reject + confidence + 사유 판정.
- **synthesize**(순수 함수): approve를 confidence×총점으로 정렬,
  economist advice로 상한, 최종 매수 리스트(≤5) 산출.
- 뉴스는 **네이버 검색 API** 헤드라인(시장 5건 + 종목당 3건, "종목명 주가"
  쿼리로 동음이의 완화). LLM은 **호스트 Ollama**, 모델은 사용자 지정
  **gemma4:31b-cloud**(클라우드 추론 — 외부 전송 수용, 단 로컬 모델 전환이
  항상 가능하도록 설정만으로 교체 가능하게 유지, 실전 전환 전 재평가 필수).
- 오케스트레이션은 **LangGraph**(아키텍처 문서 §6 예정대로).

## 2. 태스크별 커밋과 패널 결과

선행 리팩터(P3 T7 아키텍트 조건부 수용 이행): CollectionService/
ScoringService의 중복 오케스트레이션 스캐폴딩을 `BackgroundRunService`
(`backend/app/core/background_service.py`)로 추출.

| 태스크 | 구현 커밋 | 패널 수정 커밋 | 패널이 잡은 핵심 결함 |
|---|---|---|---|
| 선행 리팩터 | `81c4f3a`(계획) `d303285` | `2e0c3f6` | 런별 상태 초기화가 서브클래스 재량에 방치 → `_on_accepted()` 후크(no-throw 계약)로 구조화 |
| T1 포트·설정·파싱 | `b68201e` | `dc09a40` | LLM 응답 파싱이 타입 미검증(비-str/NaN/Inf 통과), ParseError 메시지에 원문 무제한 포함 → 타입 강제 + 길이 캡 + `repr` 절단 |
| T2 프롬프트+파이프라인 | `680c9da` | `698f77e` | (보안, 재오픈 1회) LangSmith 텔레메트리 가드가 SDK가 인식하는 4개 env var 중 2개만 차단 — 설치된 SDK 소스를 실측해 4개 전부 차단 + compose 고정. (아키) 런 입력이 인스턴스 필드라 재진입 불안전 → `_State` 채널로 이동 |
| T3 Ollama·네이버 어댑터 | `1e130c6` | `abd7c2e` | 네이버 items dict 미검증, Ollama response str 미검증, 모델 기본값을 사용자 결정(gemma4:31b-cloud)으로 |
| T4 분석 저장소(0005) | `67c7606` | `3e94b1d` | 뉴스 스냅샷 저장 실패가 판정/픽 트랜잭션까지 롤백 → 2단 트랜잭션 분리(판정 필수, 뉴스 best-effort). 외부 문자열 → 고정폭 칼럼 오버플로(반복 결함 클래스, 아래 §5) |
| T5 AnalysisService | `e7739eb` | `b505deb` | `_fail`이 실패 시점 stage를 "finished"로 뭉갬, 게이트 비대칭(런 미생성 vs 감사 기록) 미구조화, create_run 직후 progress 미동기화(run_id=None 계약 위반 시간창 — 아키 재검증 2라운드에서 발견) |
| T6 /analyze API+조립 | `9a50a47` | `9254999` | AnalysisConfig 이중 생성(SSOT 위반), NaverNewsClient 조립 분기 무테스트, config 미노출 "보안 테스트"가 판별력 없는 fake 검증(실제 게이트는 store 왕복 테스트로 이전) |
| T7 수용 검증 | (본 문서 커밋) | — | 아래 §3·§4 |

패널 프로세스 유효성: **코드 태스크 7개 전부**에서 Critical/Important가
1건 이상 나와 수정 후 재검증됐다(1차 전원 승인 0건). Phase 1의 7/8과 같은
패턴 — 패널은 비용이 아니라 방어선이다.

## 3. T7 수용 검증 — 실환경에서 발견된 불일치 2건

T7은 원래 "코드 변경 없음" 검증 태스크였으나, 실제 호출에서 어댑터 가정
2건이 깨져 수정이 필요했다. **둘 다 단위 테스트로는 절대 잡을 수 없고
실환경 수용 검증에서만 드러나는 결함**이다.

### 3-1. 네이버: 구 오픈 API와 API HUB는 키가 호환되지 않는다

- 사용자가 발급한 키는 **NAVER API HUB**(신규 플랫폼, secret 40자)용.
  어댑터가 호출하던 구 `openapi.naver.com/v1/search/news.json`은 이 키를
  **401 (errorCode 024 "Authentication failed")** 로 거부 — 실측.
- 수정(`backend/app/adapters/naver/client.py`): base URL
  `https://naverapihub.apigw.ntruss.com`, 경로 `/search/v1/news`, 인증 헤더
  `X-NCP-APIGW-API-KEY-ID`/`X-NCP-APIGW-API-KEY`. 공식 문서:
  https://api.ncloud-docs.com/docs/naver-api-hub-search-news (일 25,000회).
- **응답 본문 형식은 두 체계가 동일**(items/title/originallink/link/
  description/pubDate, `<b>` 태그·HTML 엔티티 포함, format 미지정 기본
  JSON — 전부 실측) → 파싱·제목 정제 로직은 무변경.

### 3-2. Ollama: 클라우드 추론 경로는 `format: "json"`을 무시한다

- `gemma4:31b-cloud`(ollama.com 원격 추론)는 `format: "json"` 제약을
  무시하고 응답을 마크다운 펜스(` ```json ... ``` `)로 감싼다 — 실측.
  이대로면 모든 economist/trader 호출이 ParseError → 재시도 소진 →
  폴백(neutral/전원 reject)으로 **파이프라인이 조용히 무력화**된다.
- 수정(`backend/app/adapters/ollama/client.py`): `_strip_markdown_fence` —
  여닫는 백틱 개수가 같은 **대칭 펜스 한 겹만** 벗긴다(역참조 매치).
  전송/모델 아티팩트는 어댑터 소관, `LlmPort`의 "JSON 문자열 반환" 계약을
  어댑터가 지키고 도메인 파싱은 엄격 유지(fail-loud) — 아키 패널 승인.
- 패널(개발자)이 1차 수정의 결함을 추가로 잡음: 고정 3-백틱 매치는
  4-백틱 펜스에서 잔여 백틱이 본문을 오염("벗기다 만" 제3의 결과).
  역참조 + `(?!`)`(여는 백틱 전부 소비 강제)로 재수정, 대칭/비대칭/빈
  펜스 회귀 테스트 추가. 보안 패널은 ReDoS 벤치마크(20만자 ~15ms,
  치명적 백트래킹 구조 아님)와 인젝션 방어(T2 입력 측)와의 책임 분리
  유효성을 확인.

### 3-3. 인프라 실측

- **컨테이너 → 호스트 Ollama**: `host.docker.internal:11434` HTTP 200 —
  Docker Desktop(Windows) 게이트웨이가 호스트 루프백을 프록시하므로
  `OLLAMA_HOST=0.0.0.0`(LAN 노출) **불필요**. 보안상 기본 바인딩 유지가
  정답. (호스트에서 같은 이름은 LAN IP로 풀려 연결 거부 — 라이브 스모크는
  127.0.0.1 사용.) 증거: `.superpowers/sdd/p4-task-7-ollama.txt`, 컨테이너
  내부 프로브(end-to-end 완주 자체가 증거).
- Alembic `0005` 적용, 분석 테이블 3종(analysis_runs/verdicts/news) 생성
  확인: `.superpowers/sdd/p4-task-7-tables.txt`.

## 4. 실데이터 end-to-end 결과 (run_id=1)

증거: `.superpowers/sdd/p4-task-7-latest.json`(원본),
`p4-task-7-review.txt`(육안 검토용 정리본).

- 후보 18종목(score run 2, 기준일 2026-07-16), **65초 완주**(19:05:00→
  19:06:06 KST), 뉴스 103건 저장, 판정 18건(approve 1 / reject 17), 픽 0.
- **economist**: regime=risk_off — "R20>0 업종 비율 0%"(DB 실측 0/38 일치)
  + 뉴스("SK하이닉스 폭락", "반도체발 코스피 쇼크") 인용. advice=0 →
  approve 1건이 있어도 픽 0 (synthesize 상한 설계 의도대로 —
  `test_synthesize_advice_0이면_빈_리스트`).
- **환각 검증: 인용 수치 전수 DB 대조 일치.** 055550(신한지주) approve
  근거의 momentum 212회/승률 58.96%/평균 2.56%, 종합 0.8300/전략 0.8732;
  469900 reject 근거의 0.01%/45.83%; breadth 0% — 모두 `scores`/
  `score_details`/`score_sectors` 행과 일치. 뉴스 인용("은행주 나홀로
  질주", "자사주 매입 밸류업")도 실제 수집 뉴스에서 온 것 확인.
- **판정 품질 소견(코디네이터 + 트레이더 패널)**: reject 사유가 거래비용
  (0.2~0.3%p) 대비 기대수익, 승률 50% 미만, risk_off 국면 우선순위로
  일관 — 단기 스윙 기준으로 합리적. 다만 트레이더 패널이 통계적 한계
  2건을 지적: ① 212회 표본은 hold_days=10 중복 보유 허용의 겹치는
  윈도우라 유효 독립 표본은 훨씬 적음(자기상관 과신), ② 전략 통계가
  regime 미조건화(하락장 서브셋 성과 아님). → Phase 5 전 프롬프트에
  한계 명시 권고(§6 이월).

## 5. 프로세스 기록 (정직 기록)

1. **T4 stash-RED 편차**: 구현자가 RED 캡처를 구현 후에 뜬 사실을 자진
   공개 — 개발자 패널 판정 "판별력 있는 테스트가 있을 때만 수용", breadth
   픽스처 판별력 복원으로 종결.
2. **T6 RED 허위 양성**: 구현자가 첫 RED 캡처가 false-pass였음을 인지하고
   `analyze.py`를 임시 제거해 진짜 RED를 재캡처 — 자진 공개, 증거 유효.
3. **T7 펜스 정규식 2회 수정**: 코디네이터의 1차 수정(고정 3-백틱)을
   패널이 실측 재현으로 반박(4-백틱 오염) → 역참조로 재수정. 2차 수정도
   첫 시도는 백트래킹 누락(`(?!`)` 부재)으로 자체 테스트에서 실패 —
   테스트가 먼저 잡았다.
4. **반복 결함 클래스 확인**: "외부 문자열 → 고정폭 칼럼 오버플로"(T2
   state/auditInfo, T4 news title/url), "실측 없이 믿은 외부 계약"(T7
   네이버 키 체계, Ollama format 준수) — 다음 페이즈 체크리스트에 반영.

## 6. 이월 사항 (다음 페이즈 게이트)

**Phase 5 착수 전 (hard gate, 트레이더/보안 패널):**
1. `max_picks_advice` DB 미저장 — "approve인데 픽 0"의 이유를 감사할 수
   없음. `analysis_runs`에 칼럼 1개 추가로 해결(마이그레이션 1건).
2. economist 파싱 실패 폴백이 "열림"(advice=max_picks) — trader 폴백
   ("닫힘", 전원 reject)과 비대칭. 스펙 §8 결정과 충돌하는 지적이라
   사용자 판단 필요(§7 참고).
3. `/analyze/status`에 타임스탬프 없음(며칠 전 succeeded가 오늘 성공처럼
   보임) + `/analyze/latest`에 `score_reference_date` 없음(픽의 데이터
   as-of 일자 불명) — Phase 5가 이 API를 소비하기 전 해소.
4. 프롬프트의 거래비용 상수(0.2~0.3%p)를 실제 비용 설정과 연동.
5. 트레이더 프롬프트에 백테스트 한계(겹침 표본 자기상관, regime
   미조건화) 명시해 confidence 과신 완화.
6. (기존) kt00018 실포지션 실측, 쓰기 엔드포인트 인증/CORS, AI 외부 추론
   재평가.

**Minor 이월(우선순위 낮음):** `/analyze/latest`·`/score/latest`
response_model(초과 필드 원천 차단), progress 직렬화 중복(3번째 서비스
등장 시 추출), `news_timeout_s` 설정 비대칭, shutdown 경로 테스트 공백,
LangSmith 가드 조립 루트 fail-fast 여부, 244920처럼 수치 인용 없는 판정
사유 방지 프롬프트 지시, 라이브 스모크 Ollama URL env 오버라이드.

## 7. 스펙과 충돌한 패널 지적 (사용자 결정 대기)

트레이더 패널 Important #1: economist 파싱 실패 시 `neutral_fallback`이
`max_picks_advice=max_picks`(열림)로 폴백하는 것은 T2에서 승인된 스펙 §8
결정("economist 실패는 neutral로 계속 진행")이지만, 트레이더는 "국면 판정
실패 시 신규 진입 전면 보류(advice=0)"가 매매상 옳다고 판단. 둘 다
일리가 있어(전자: 분석 지속성, 후자: fail-safe) 스펙 문구가 정하는 바와
패널 판단이 충돌 — **Phase 5 착수 전 사용자 결정**으로 남긴다.

## 8. 최종 상태

- 테스트: **299 passed, 10 deselected**(라이브 8 키움 + 2 분석).
- 라이브 스모크: Ollama JSON 계약·네이버 헤드라인 2/2 passed
  (`.superpowers/sdd/p4-task-7-live.txt`).
- end-to-end: POST /analyze → 65초 완주 → /analyze/latest 정합 확인.
- 산출물: `backend/app/domain/analysis/`(ports·config·parsing·prompts·
  graph·service), `backend/app/adapters/{ollama,naver}/`,
  `backend/app/store/analysis_store.py` + Alembic `0005`,
  `backend/app/api/analyze.py`, `backend/app/core/background_service.py`
  (선행 리팩터), 라이브 스모크 `backend/tests/live/test_live_analysis_smoke.py`.
