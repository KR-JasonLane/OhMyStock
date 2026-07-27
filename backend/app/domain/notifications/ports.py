"""알림 도메인이 소비하는 외부 경계.

구현체는 FastAPI/SQLAlchemy/Telegram DTO를 이 모듈 밖에 둔다.  Task 5는
공용 운영 제어만 구체화하며, broker나 주문 포트를 직접 노출하지 않는다.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from app.domain.notifications.analysis_summary import MorningAnalysisSummary


class AccountSnapshotDeferred(RuntimeError):
    """저우선 digest가 새 broker snapshot을 시작하지 않았다는 명시 계약."""


class OperationsControlPort(Protocol):
    def scheduler_fingerprint(self) -> str: ...

    async def system_status(self) -> Any: ...
    async def account_summary(self, priority: str = "interactive") -> Any: ...
    async def open_positions_summary(self) -> Any: ...
    async def liquidation_preview(self) -> Any: ...
    async def pause_scheduler(self) -> Any: ...
    async def resume_scheduler(self, expected: str | None = None) -> Any: ...
    async def stop_new_entries(self, intent_id: str) -> Any: ...
    async def liquidate_managed(self, intent_id: str, targets: Any,
                                *, expected_run_id: int | None = None) -> Any: ...
    async def reconcile_control_intent(self, intent_id: str, targets: Any = ()) -> Any: ...


class AnalysisReportQueryPort(Protocol):
    """저장된 성공 분석의 읽기 모델 경계."""

    def latest_analysis(self) -> MorningAnalysisSummary | None: ...


class DigestReportQueryPort(Protocol):
    """보존 기간 안의 거래 다이제스트를 읽는 별도 read model 경계."""

    def latest_digest(self) -> tuple[Mapping[str, object], tuple[str, ...]] | None: ...
