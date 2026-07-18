# Phase 4 선행 리팩터 계획 — 백그라운드 서비스 공통 베이스 추출

> 근거: P3 spec §9 (T7 아키텍트 패널 조건부 수용 — "세 번째 동형 서비스가
> 생기는 Phase 4가 추출 적기"). 단일 태스크, 동작 보존 리팩터.

**Goal:** `CollectionService`/`ScoringService`에 두 번 복제된 오케스트레이션
스캐폴딩(원자적 start, conflict_check, 태스크 강참조+done 콜백, `_running`
수명주기)을 공통 베이스로 추출해 Phase 4의 세 번째 동형 서비스가 무중복으로
얹히게 한다.

**Architecture:** 신규 `backend/app/core/background_service.py`에
`BackgroundRunService` 베이스를 둔다 (CLAUDE.md §3의 core/ 정의 "scheduling
primitives"에 부합 — 비즈니스 로직 없음). 도메인 서비스는 `_run()`만 구현한다.

## Global Constraints

- **동작 보존**: 기존 187개 테스트가 무수정으로 통과해야 한다(리팩터 동등성
  증명). 공개 시그니처(`is_running`/`progress`/`current_task`/`start`/`run`,
  생성자 파라미터) 불변.
- 커밋 메시지 사전 승인분 사용, AI 흔적 금지 (규칙 7). 4-에이전트 패널 통과
  후 종료 (규칙 8). 증거 접두 `p4-pre-`.

## Task 1: BackgroundRunService 추출

**Files:**
- Create: `backend/app/core/background_service.py`
- Modify: `backend/app/domain/collection.py`, `backend/app/domain/scoring/service.py`
- Test: `backend/tests/test_background_service.py` (신규 — 베이스 직접 검증 소수)

**베이스 계약 (Produces):**

```python
class BackgroundRunService:
    """백그라운드 단일 실행 서비스 공통 스캐폴딩 — 원자적 start(중복/충돌 거부),
    태스크 강참조+예외 로깅 콜백, _running 수명주기. 서브클래스는 _run()만 구현.
    P3까지 두 서비스에 복제됐던 패턴의 단일 출처 (P3 spec §9)."""

    def __init__(self, conflict_check: Callable[[], bool] | None = None) -> None: ...
    def is_running(self) -> bool: ...
    def current_task(self) -> asyncio.Task | None: ...
    def start(self) -> asyncio.Task | None:
        # _running 체크 → conflict_check 체크 → set (사이에 await 없음, TOCTOU 없음)
        # asyncio.create_task(self._execute()) + done 콜백(_log_task_exception)
    async def run(self) -> None:
        # 단독 실행용 — 중복 시 RuntimeError, 충돌 시 RuntimeError("conflicting run in progress")
    async def _execute(self) -> None:
        # try: await self._run()  finally: self._running = False
        # → create_run 예외 시 _running 고착(T7 arch #2) 방어가 베이스에서 구조적으로 보장됨
    async def _run(self) -> None: raise NotImplementedError
```

- `progress()`는 서브클래스 소유 유지(반환 타입이 서비스별 dataclass —
  베이스는 `self._progress` 슬롯만 제공하지 않고 각자 유지해도 무방, 구현자가
  더 자연스러운 쪽 선택 후 보고).
- `start(warning=...)`(수집 전용 파라미터)은 CollectionService가 오버라이드로
  흡수 — 베이스 시그니처는 무파라미터 유지.
- 서브클래스에서 제거되는 것: `_log_task_exception` 복제본, `start()`/`run()`
  본문, `_run` 내 create_run try/except(`_running` 리셋)와 `finally` 블록.
  유지되는 것: 각자의 `_run()` 파이프라인 본문·`_fail`·`_set`·progress.

- [ ] Step 1: `test_background_service.py` 작성 — 더미 서브클래스로 (a) start
  원자성/중복 None, (b) conflict_check 거부, (c) `_run` 예외에도 `is_running()`
  False 복원, (d) run() 중복/충돌 RuntimeError. RED 캡처
  `> ../.superpowers/sdd/p4-pre-red.txt 2>&1`.
- [ ] Step 2: 베이스 구현 + 두 서비스 이식(공개 동작 불변).
- [ ] Step 3: 전체 스위트 GREEN 캡처 `p4-pre-green.txt` — **기존 테스트 무수정**
  통과 확인(수정이 필요해지면 BLOCKED 보고 — 동작 보존 위반 신호).
- [ ] Step 4: 커밋 `refactor(core): extract background service scaffolding`.

## 검증 기준

- 기존 187 테스트 무수정 통과 + 신규 베이스 테스트.
- 두 서비스 파일에서 중복 스캐폴딩 라인 순감소(diff로 확인).
- 4-에이전트 패널 전원 승인.
