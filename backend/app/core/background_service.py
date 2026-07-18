"""백그라운드 단일 실행 서비스 공통 스캐폴딩 — 원자적 start(중복/충돌 거부),
태스크 강참조+예외 로깅 콜백, `_running` 수명주기. 서브클래스는 `_run()`만
구현한다. P3까지 CollectionService/ScoringService에 두 번 복제됐던 패턴의
단일 출처다 (P3 spec §9 — "세 번째 동형 서비스가 생기는 Phase 4가 추출 적기").

비즈니스 로직이 없는 오케스트레이션 스캐폴딩이므로 CLAUDE.md §3의 core/
정의("scheduling primitives")에 부합한다 — domain/store/adapters를 알지
못한다."""

import asyncio
import logging
from collections.abc import Callable

_default_logger = logging.getLogger(__name__)


class BackgroundRunService:
    """단일 실행(at-most-one) 백그라운드 서비스의 오케스트레이션 베이스.

    서브클래스는 `_run()`에 파이프라인 본문을 구현한다. `_running` 수명주기,
    원자적 시작, 태스크 강참조, done 콜백 예외 로깅은 이 클래스가 전담한다.
    """

    def __init__(self, task_label: str,
                 conflict_check: Callable[[], bool] | None = None,
                 logger: logging.Logger | None = None) -> None:
        """task_label: done 콜백 예외 로그 문구에 쓰는 서비스 이름
        (예: "collection", "scoring") — 서브클래스별 로그 메시지를 그대로
        유지하기 위한 최소한의 커스터마이즈 지점이다.

        logger: done 콜백 로깅에 쓸 로거. 생략하면 이 모듈
        (`app.core.background_service`)의 로거를 쓴다. 서브클래스는 보통
        자기 모듈의 로거(`logging.getLogger(__name__)`)를 넘겨 기존 로그
        네임스페이스(및 그에 걸린 테스트의 caplog/로거-활성화 픽스처)를
        그대로 유지한다.

        conflict_check: 동시 실행 금지 콜러블 — 진행 중이면 시작 거부.
        """
        self._task_label = task_label
        self._conflict_check = conflict_check
        self._logger = logger or _default_logger
        self._running = False
        self._task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self._running

    def current_task(self) -> asyncio.Task | None:
        return self._task

    def start(self) -> asyncio.Task | None:
        """원자적 시작: 이미 실행 중이거나 충돌하면 None. 태스크 강참조는
        서비스가 보유한다.

        check(self._running)와 set(self._running = True) 사이에 await가 없어
        원자적이다 — 별도 락 없이도 동시 호출 중 하나만 시작을 얻는다
        (TOCTOU 없음). 태스크 자체를 여기서 보유하므로 호출자가 GC로 태스크를
        잃어버릴 걱정도 없다.

        계약: 이 메서드를 오버라이드하는 서브클래스는 super()의 가드(중복/충돌
        검사) 통과가 확인되기 전에 관찰 가능한 상태를 변형해서는 안 된다 —
        대신 `_on_accepted()` 훅을 구현할 것.
        """
        if self._running:
            return None
        if self._conflict_check is not None and self._conflict_check():
            return None
        self._running = True
        self._on_accepted()
        self._task = asyncio.create_task(self._execute())
        self._task.add_done_callback(self._log_task_exception)
        return self._task

    async def run(self) -> None:
        """단독 호출용 진입점 (테스트/스크립트). API는 start()를 쓴다.

        계약: 이 메서드를 오버라이드하는 서브클래스는 super()의 가드(중복/충돌
        검사) 통과가 확인되기 전에 관찰 가능한 상태를 변형해서는 안 된다 —
        대신 `_on_accepted()` 훅을 구현할 것.
        """
        if self._running:
            raise RuntimeError(f"{self._task_label} already running")
        if self._conflict_check is not None and self._conflict_check():
            raise RuntimeError("conflicting run in progress")
        self._running = True
        self._on_accepted()
        await self._execute()

    async def _execute(self) -> None:
        """`_run()`을 실행하고 결과와 무관하게 `_running`을 되돌린다.

        `_run()` 내부 어디에서 예외가 나든(초기화 단계 포함) 이 finally가
        구조적으로 `_running`을 복원한다 — 서브클래스가 각자 try/finally를
        중복할 필요가 없다.
        """
        try:
            await self._run()
        finally:
            self._running = False

    async def _run(self) -> None:
        raise NotImplementedError

    def _on_accepted(self) -> None:
        """가드(_running/conflict_check) 통과 직후, 실행 시작 전에 호출되는 훅.

        서브클래스가 실행별 상태(예: warning)를 세팅하는 유일한 안전 지점이다.
        계약: start()/run() 오버라이드는 super() 가드 통과가 확인되기 전에
        관찰 가능한 상태를 변형해서는 안 된다 — 대신 이 훅을 구현할 것.
        추가 계약: 이 훅은 예외를 던지지 않아야 한다 — 호출 시점이 _running=True
        직후·_execute() 진입 전이라, 예외 시 _running이 고착된다(단순 필드
        대입만 수행할 것. 검증 로직이 필요해지면 베이스에 try/except 방어를
        먼저 추가할 것 — P4-pre 아키텍트 패널 잔여 Minor)."""

    def _log_task_exception(self, task: asyncio.Task) -> None:
        """start()로 생성한 태스크의 done 콜백 — 취소가 아니면서 예외로
        끝났다면 로깅한다.

        fire-and-forget 태스크(asyncio.create_task)는 예외를 조회하지 않으면
        조용히 삼켜지고 "Task exception was never retrieved" 경고만 남는다.
        `_run()` 내부에서 이미 실패 상태를 store에 기록하지만, 이 콜백은
        태스크 자체의 예외를 놓치지 않기 위한 마지막 안전망이다.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.error("%s task failed: %s", self._task_label, exc)
