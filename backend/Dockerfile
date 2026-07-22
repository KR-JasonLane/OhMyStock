FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

# 프로덕션 백엔드(기본 대상 — compose backend.build.target=app)
FROM base AS app
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]

# 리플레이 목 서버(§4-2 — 엔트리포인트만 분리: 두 이미지 파일셋은 동일
# (COPY는 base 1회)하며, /_replay 라우트가 프로덕션에 안 섞이는 보증은
# 회귀 테스트(tests/test_replay_profile.py 라우트 부재+AST)가 담당.
# compose --profile replay 전용, 분봉 데이터(replay/data — dockerignore)는
# 볼륨 마운트로 공급)
FROM base AS replay
CMD ["uvicorn", "--factory", "replay.main:create_app", \
     "--host", "0.0.0.0", "--port", "9095"]
