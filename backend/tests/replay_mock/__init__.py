"""리플레이 목 서버 테스트.

⚠️ 디렉토리명이 `replay`가 아니라 `replay_mock`인 이유(개발자/아키텍트 R2 —
개명 근거 기록): tests/에는 `__init__.py`가 없어 pytest가 tests/를 sys.path
삽입점으로 잡는데, 이 디렉토리가 `tests/replay/`이면 패키지명 `replay`가
피검 대상 `backend/replay/`를 **섀도잉**해 `ModuleNotFoundError:
No module named 'replay.account'`가 난다(실재현 확인). "이름 일관성"을
이유로 `tests/replay/`로 되돌리지 말 것 — 동일 충돌이 재발한다.
계획서상 `backend/replay/tests/` 배치도 pyproject testpaths=["tests"]라
기본 수집이 안 되어 이 위치를 택했다(계획서 R2에 사유 기재)."""
