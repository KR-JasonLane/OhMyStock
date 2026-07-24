from pathlib import Path


ROOT_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ROOT_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_환경예시와_compose는_운영DB_비밀번호를_명시적으로_요구한다():
    values = _env_values()
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert values["POSTGRES_PASSWORD"] == ""
    assert "${POSTGRES_PASSWORD:-ohmystock}" not in compose
    assert compose.count("${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}") == 2
    assert "DATABASE_URL: postgresql+psycopg://ohmystock@db:5432/ohmystock" in compose
    assert "DATABASE_URL: postgresql+psycopg://ohmystock:${POSTGRES_PASSWORD" not in compose
    assert "PGPASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}" in compose
    assert values["TELEGRAM_BOT_TOKEN"] == ""
    assert values["TELEGRAM_ALLOWED_USER_ID"] == ""
    assert values["TELEGRAM_ALLOWED_CHAT_ID"] == ""
