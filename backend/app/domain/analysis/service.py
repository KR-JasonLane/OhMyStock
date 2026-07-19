"""AI 분석 오케스트레이션 — ScoringService/CollectionService와 동일한 실행
패턴(원자적 start(), 태스크 강참조, 예외 경계). conflict_check는 주입하지
않는다: 입력을 succeeded score run_id로 고정해 읽고(insert-only 저장),
candles는 전혀 읽지 않으며 instruments는 name 칼럼만 읽는다(단순 SELECT
JOIN — 원자적 upsert 대상이라 사실상 불변이고 이 경로에는 쓰기가 없다,
`AnalysisStore.load_candidates`). 따라서 수집/스코어링 파이프라인과 동시
실행돼도 데이터 일관성이 깨지지 않는다(스펙 §3). (이 근거는 이 모듈
docstring에만 서술한다 — `__init__`/`_run` 쪽 docstring은 각자의 관심사만
다루므로 중복 없음.)"""

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from app.core.background_service import BackgroundRunService
from app.core.market_calendar import KST
from app.domain.analysis.config import AnalysisConfig
from app.domain.analysis.graph import AnalysisPipeline
from app.domain.analysis.ports import (CandidateInput, Headline, LlmError,
                                       LlmPort, NewsError, NewsPort)
from app.domain.analysis.prompts import prompt_hash

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisProgress:
    """run_id는 오직 한 경우에만 None이다: 스코어링 런 자체가 없어
    (`latest_succeeded_score_run()` → None) `analysis_runs.score_run_id`
    (NOT NULL FK)를 채울 수 없어 run을 아예 만들지 않은 경우. 그 외 모든
    running/succeeded/failed 상태는 create_run 이후이므로 항상 실제 정수
    run_id를 갖는다 — **T6 API는 run_id가 None이면 "생성된 런 없음"으로
    표현해야 한다(계약)**."""
    run_id: int | None
    status: str  # running | succeeded | failed
    stage: str   # gate | news | economist | traders | synthesize | finished
    done: int
    total: int
    failure_reason: str | None = None
    # T1(P5pre) — `/analyze/status`가 신선도를 판별할 수 있도록 ISO
    # 문자열로 노출한다(트레이더 패널: 타임스탬프가 없어 며칠 지난
    # succeeded 런이 방금 끝난 것처럼 보이는 문제). started_at은 `_run()`
    # 진입 시 한 번만 고정해 모든 progress 스냅샷에 그대로 실어 보낸다.
    # finished_at은 run이 종결(succeeded/failed)될 때만 채워진다 — 아직
    # running인 progress에는 None으로 남아 "종료 여부"를 명확히 구분한다.
    started_at: str | None = None
    finished_at: str | None = None


class AnalysisService(BackgroundRunService):
    def __init__(self, store, llm: LlmPort, news: NewsPort | None,
                 config: AnalysisConfig | None = None,
                 today: Callable[[], date] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        """news=None이면(네이버 키 미발급) 뉴스 조회를 생략하고 경고만 남긴
        채 진행한다 — 뉴스는 판정 보조 자료이지 필수 입력이 아니다(스펙 §4).

        today: 연쇄 신선도 게이트(스코어링 결과 기준일 대비 경과일)의 "오늘"
        판정에 쓴다. 기본값은 `datetime.now(KST).date` — 스코어링과 달리
        여기서는 채점 대상 심볼별 최신 캔들 날짜가 아니라 스코어링 런
        자체의 reference_date를 오늘과 비교하므로, ScoringService처럼
        전 거래일로 당길 필요가 없다.

        now: progress의 started_at/finished_at 타임스탬프(T1, P5pre)에 쓴다.
        `today`와 별개 주입점인 이유: today는 날짜 단위 게이트 판정용이고
        now는 초 단위 감사 타임스탬프용이라 테스트에서 독립적으로 고정할
        필요가 있다. 기본값은 `datetime.now(timezone.utc)` — DB
        컬럼(DateTime(timezone=True))과 동일하게 UTC aware로 통일."""
        super().__init__(task_label="analysis", logger=logger)
        self._store = store
        self._llm = llm
        self._news = news
        self._config = config or AnalysisConfig()
        self._today = today or (lambda: datetime.now(KST).date())
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._progress: AnalysisProgress | None = None

    def progress(self) -> AnalysisProgress | None:
        return self._progress

    def latest_results(self) -> dict | None:
        """최근 succeeded 실행 결과 위임 — API가 store를 직접 알지 않도록
        분리한다(T7 패턴과 동일한 의도)."""
        return self._store.latest_results()

    async def _run(self) -> None:
        """`_running` 복원은 베이스 `_execute()`의 finally가 구조적으로
        보장한다.

        게이트 두 개는 의도적으로 비대칭이다:
        (a) 스코어링 런 자체가 없음 — `score_run_id`가 없어
            `analysis_runs.score_run_id`(NOT NULL FK, 마이그레이션 0005)를
            채울 수 없으므로 run을 만들지 않는다. `store.finish_run`을
            호출할 대상 run이 없으므로 progress만 남기고(`run_id=None`)
            경고 로그를 남긴다.
        (b) 스코어링 런은 있으나 낡음(reference_date가 `score_max_age_days`
            초과) — `score_run_id`가 이미 확보돼 있으므로 **create_run을
            먼저 호출**해 감사 이력에 남긴 뒤 `_fail`로 실패 마감한다.
            ScoringService가 실패도 run으로 기록하는 것과 대칭이며,
            "언제부터 게이트가 걸렸는지"를 DB에서 추적할 수 있다.
        이후 단계(빈 후보/빈 섹터 표 등)는 이미 run이 만들어진 뒤이므로
        전부 `_fail(run_id, stage, ...)`을 쓴다 — `_fail`은 실패 시점의
        stage를 progress에 그대로 보존한다(더 이상 "finished"로 뭉개지
        않음, T5 패널 개발자/트레이더 리뷰)."""
        cfg = self._config
        # `_run()` 진입 시 한 번만 고정한다 — 이후 모든 중간 progress
        # 스냅샷(_set)이 같은 started_at을 실어 보내고, 종결 스냅샷만
        # finished_at을 추가로 채운다(T1, 브리프 지시).
        started = self._now().isoformat()
        self._set(None, "running", "gate", 0, 0, started=started)

        gate = await asyncio.to_thread(self._store.latest_succeeded_score_run)
        if gate is None:
            logger.warning("analysis rejected: no succeeded scoring run")
            # run 자체가 만들어지지 않는 유일한 게이트라 `_fail`(finish_run
            # 호출 포함)을 거치지 않지만, progress는 여기서 이미 종결
            # 상태이므로 finished_at도 함께 남긴다 — "언제 거부됐는지"가
            # 이 경우에도 감사에 필요하다.
            self._set(None, "failed", "gate", 0, 0,
                      "no succeeded scoring run - run scoring first",
                      started=started, finished=self._now().isoformat())
            return
        score_run_id, reference_date = gate

        run_id = await asyncio.to_thread(
            self._store.create_run, score_run_id, cfg.model, prompt_hash(),
            cfg.to_json())
        # create_run 직후 progress를 즉시 동기화 — 이 시점부터 DB에 run이
        # 실재하므로 run_id=None("런 미생성") 계약을 위반하는 시간창을 없앤다
        # (T5 아키텍트 재검증 지적).
        self._set(run_id, "running", "gate", 0, 0, started=started)

        age_days = (self._today() - reference_date).days
        if age_days > cfg.score_max_age_days:
            await self._fail(
                run_id, "gate", 0,
                f"scoring results stale (reference={reference_date.isoformat()}) "
                "- run scoring first")
            return

        total = 0
        try:
            candidates = await asyncio.to_thread(
                self._store.load_candidates, score_run_id)
            if not candidates:
                await self._fail(run_id, "gate", total,
                                 "no candidates in scoring run")
                return
            total = len(candidates)

            snapshot = await asyncio.to_thread(
                self._store.market_snapshot, score_run_id)
            if snapshot.sector_table == "":
                # 트레이더 게이트(MUST) — breadth 0.0이 "데이터 없음"인지
                # "전 업종 하락"인지 구분 불가하므로 LLM을 호출하지 않고
                # 보수적으로 실패한다.
                await self._fail(
                    run_id, "gate", total,
                    "no sector aggregates in scoring run - cannot judge "
                    "market regime")
                return

            self._set(run_id, "running", "news", 0, total, started=started)
            market_headlines, symbol_headlines, news_warnings = \
                await self._collect_news(candidates)

            self._set(run_id, "running", "economist", 0, total, started=started)
            # AnalysisPipeline은 런마다 새로 만든다 — 생성자의 LangSmith
            # 환경변수 가드(RuntimeError)가 서비스 생성 시점이 아니라 실행
            # 시점 값으로 평가되게 하기 위함(P4-T2 보안 패널 의도를 유지:
            # 배치 스케줄러가 다른 프로세스에서 환경변수를 바꿔도 다음
            # 런부터 즉시 반영). 인스턴스 자체는 무상태 래퍼라 비용도
            # 무시할 수준이다(그래프 컴파일만 반복).
            try:
                pipeline = AnalysisPipeline(self._llm, cfg)
            except RuntimeError as exc:
                # graph.py의 LangSmith 텔레메트리 가드가 던지는
                # RuntimeError만 좁게 잡는다 — 아래 `except Exception`으로
                # 흘려보내면 사유가 "unexpected: RuntimeError"로 뭉개져
                # 어떤 env var가 문제인지 알 수 없게 된다(아키텍처 패널).
                # 여기서 잡으면 exc 메시지(env var 이름 포함)가 그대로
                # failure_reason에 남는다.
                await self._fail(run_id, "economist", total, str(exc))
                return
            result = await pipeline.run(snapshot, candidates,
                                        market_headlines, symbol_headlines)
            self._set(run_id, "running", "traders", total, total, started=started)

            self._set(run_id, "running", "synthesize", total, total, started=started)
            # regime/market_summary/warnings는 한 번만 추출해 로컬 변수로
            # 고정한 뒤 finish_run에 넘긴다(save_results는 result 객체
            # 전체를 받으므로 별도 추출이 필요 없다) — 이중 추출 방지
            # (T4 패널 개발자 리뷰 이월).
            regime = result.market.regime
            market_summary = result.market.summary
            # "; ".join 대신 JSON 배열 문자열로 저장 — 역파싱 안전.
            # latest_results()가 노출하는 warnings 필드는 이 JSON 문자열
            # 그대로다(carry-over #6).
            warnings = json.dumps([*result.warnings, *news_warnings],
                                  ensure_ascii=False)
            news = {"market": market_headlines, **symbol_headlines}

            await asyncio.to_thread(
                self._store.save_results, run_id, result, news)
            # max_picks_advice(T1) — economist가 조언한 최대 pick 수를
            # 그대로 감사 이력에 남긴다. picks 자체(result.picks)는 이미
            # save_results가 저장하므로, 여기서는 "권고 상한"과 "실제 선정
            # 수"를 나중에 DB만으로 비교할 수 있게 하는 것이 목적이다
            # (P4 트레이더 패널: approve가 있는데 picks가 비어도 원인이
            # advice=0 때문인지 synthesize 로직 때문인지 구분 불가했음).
            await asyncio.to_thread(
                self._store.finish_run, run_id, "succeeded", regime,
                market_summary, warnings, None,
                max_picks_advice=result.market.max_picks_advice)
            finished = self._now().isoformat()
            self._set(run_id, "succeeded", "finished", total, total,
                      started=started, finished=finished)
            logger.info(
                "analysis run %d: %d candidates, regime=%s, %d picks",
                run_id, total, regime, len(result.picks))
        except LlmError as exc:
            # LLM 접속 실패는 economist/traders 어느 단계에서 나든 전체
            # 실패로 취급한다(스펙 §8) — 이미 완료된 trader 판정 일부가
            # 있어도 부분 저장하지 않는다(mid-traders 유실, T3 이월). 그
            # 사실을 실패 사유에 명시해 운영자가 "부분 결과가 저장됐나?"
            # 헷갈리지 않게 한다. stage는 예외 발생 시점에 progress에
            # 남아있던 값을 그대로 넘긴다 — LlmError는 파이프라인 실행
            # 구간(economist/traders 노드 모두)에서만 발생 가능하고, 그
            # 구간 전체가 stage="economist"로 표시되므로(스펙 §7 addendum)
            # 항상 "economist"로 기록된다.
            await self._fail(
                run_id, self._progress.stage, total,
                f"{exc} - 부분 결과는 저장되지 않음 - 재실행 필요")
        except asyncio.CancelledError:
            await self._fail(run_id, self._progress.stage, total, "cancelled")
            raise
        except Exception as exc:
            logger.exception("analysis run %s failed unexpectedly", run_id)
            await self._fail(run_id, self._progress.stage, total,
                             f"unexpected: {type(exc).__name__}")
            raise

    async def _collect_news(
            self, candidates: Sequence[CandidateInput]
    ) -> tuple[list[Headline], dict[str, list[Headline]], list[str]]:
        """시장 키워드별 + 종목명별 헤드라인 조회. `NewsError`는 건별로 잡아
        경고로 누적하고 계속한다(스펙 §4) — 뉴스 한 건 실패로 전체 분석
        런을 실패시키지 않는다."""
        if self._news is None:
            return [], {}, ["news skipped (no naver keys)"]

        warnings: list[str] = []
        market_headlines: list[Headline] = []
        for keyword in self._config.market_keywords:
            try:
                market_headlines.extend(await self._news.search_headlines(
                    keyword, self._config.news_per_symbol))
            except NewsError as exc:
                warnings.append(f"market news failed for '{keyword}': {exc}")

        symbol_headlines: dict[str, list[Headline]] = {}
        for candidate in candidates:
            # 동음이의·일반명사 종목명("동양" 등) 단독 검색 시 무관 기사가
            # 섞이는 문제를 완화 — "주가"를 붙여 검색 특이도를 높인다
            # (스펙 §4, T3 패널 이월).
            query = f"{candidate.name} 주가"
            try:
                symbol_headlines[candidate.symbol] = \
                    await self._news.search_headlines(
                        query, self._config.news_per_symbol)
            except NewsError as exc:
                warnings.append(
                    f"symbol news failed for {candidate.symbol}: {exc}")

        return market_headlines, symbol_headlines, warnings

    async def _fail(self, run_id: int, stage: str, total: int,
                    reason: str) -> None:
        """호출자가 넘긴 `stage`(실패 시점의 진행 단계)를 그대로 progress에
        남긴다 — 이전에는 항상 stage="finished"로 덮어써서 "어느 단계에서
        실패했는지"가 최종 progress에서 사라졌다(T5 패널 개발자/트레이더
        리뷰). status는 "failed"로 고정.

        started_at은 `self._progress.started_at`에서 그대로 이어받는다 —
        `_fail`은 오직 `_run()` 내부에서, 그것도 진입 시 첫 `_set`이 이미
        started를 채운 뒤에만 호출되므로 이 시점의 `self._progress`는 항상
        존재하고 started_at도 항상 채워져 있다(계약). finished_at은 여기서
        새로 찍는다 — 실패 런도 "언제 끝났는지"를 감사할 수 있어야 한다
        (T1, 브리프 지시: "실패 런(_fail)도 finished_at 스탬프")."""
        logger.warning("analysis run %d rejected: %s", run_id, reason)
        await asyncio.to_thread(
            self._store.finish_run, run_id, "failed", None, None, None, reason)
        started = self._progress.started_at if self._progress else None
        self._set(run_id, "failed", stage, 0, total, reason,
                  started=started, finished=self._now().isoformat())

    def _set(self, run_id: int | None, status: str, stage: str, done: int,
             total: int, failure_reason: str | None = None,
             started: str | None = None, finished: str | None = None) -> None:
        self._progress = AnalysisProgress(run_id, status, stage, done, total,
                                          failure_reason, started, finished)
