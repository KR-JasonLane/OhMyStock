"""리플레이 관리 표면(/_replay — 스펙 §9 네임스페이스 격리).

- `GET /_replay/status`(R4): healthcheck + §5 speed 스탬프 + 활성 시나리오.
- `POST /_replay/faults`(R5): 시나리오 활성화 — 바디 `{scenario, params}`,
  복수 동시 활성 허용(§9). 미지 시나리오/파라미터는 fail-loud 400.
- `POST /_replay/reset`(R5): **범위 계약(개발자 I3)** — faults + account
  (예수금/보유/미체결) + matching pending 전부 초기화, **clock anchor는
  유지**(재생 진행은 리셋과 독립 — 시각까지 되돌리려면 서버 재기동).
  정책 객체는 교체가 아니라 in-place clear(아키텍트 R3 — 엔진은 생성자
  주입 1회).

인증 없음 — 127.0.0.1 바인딩 전제(§4-2, R6 compose가 명문화). 키움 재현
표면(/api/dostk, /oauth2)과 경로가 절대 겹치지 않는다.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from replay.faults import ScenarioFaultPolicy

router = APIRouter()


def _scenario_policy(request: Request) -> ScenarioFaultPolicy | None:
    faults = request.app.state.faults
    return faults if isinstance(faults, ScenarioFaultPolicy) else None


@router.get("/_replay/status")
async def status(request: Request) -> JSONResponse:
    state = request.app.state
    account = state.account
    policy = _scenario_policy(request)
    return JSONResponse({
        "replay_now": state.clock.now().isoformat(),
        "anchor": state.settings.anchor.isoformat(),
        "speed": state.clock.speed,          # §5 ① — 증거 파일 기록용
        "wall_now": state.wall_now().isoformat(),
        "symbols": len(state.store.symbols),
        "loader_skipped": state.store.skipped,
        "cash": account.cash,
        "reserved_buy": account.reserved_buy_total(),
        "holdings": {s: h.quantity for s, h in account.holdings.items()},
        "open_orders": len(account.open_orders),
        "price_missing_skips": state.engine.price_missing_skips,
        "negative_cash_events": account.negative_cash_events,
        "cost_drift_total": account.cost_drift_total,
        # 커스텀 정책 주입 시(테스트) 시나리오 요약은 제공 불가 — 침묵 대신
        # 명시 표기(§9 관리 API는 ScenarioFaultPolicy 전제)
        "faults": policy.describe() if policy else "custom-policy",
    })


@router.post("/_replay/faults")
async def activate_fault(request: Request) -> JSONResponse:
    policy = _scenario_policy(request)
    if policy is None:
        return JSONResponse(
            {"error": "fault injection unavailable — custom FaultPolicy "
                      "was injected (ScenarioFaultPolicy required)"},
            status_code=400)
    body = await request.json()
    scenario = str(body.get("scenario", ""))
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse({"error": "params must be an object"},
                            status_code=400)
    try:
        _apply_scenario(request, policy, scenario, params)
    except (KeyError, ValueError, TypeError) as exc:
        # fail-loud: 미지 시나리오/누락 파라미터가 조용히 무시되면 "결함을
        # 주입했다고 믿는" 검증이 무결함 런이 된다(§9)
        return JSONResponse(
            {"error": f"invalid scenario request: {exc}"}, status_code=400)
    return JSONResponse({"ok": True, "active": policy.describe()})


def _apply_scenario(request: Request, policy: ScenarioFaultPolicy,
                    scenario: str, params: dict) -> None:
    """§9 시나리오 → 정책 프리미티브 디스패치(모듈 독스트링 매핑 참조)."""
    if scenario == "propagation_delay":
        policy.set_propagation_delay(float(params["seconds"]))
    elif scenario == "api_fault":
        policy.set_api_fault(
            str(params["api_id"]),
            mode=str(params.get("mode", "http500")),
            count=(int(params["count"]) if "count" in params else None),
            delay_sec=float(params.get("delay_sec", 0.0)))
    elif scenario == "partial_fill":
        policy.set_fill_ratio(
            float(params["ratio"]),
            interval_sec=float(params.get("interval_sec", 1.0)))
    elif scenario == "suppress_fill":
        policy.add_suppress(
            symbol=params.get("symbol"), side=params.get("side"),
            style=params.get("style"), order_no=params.get("order_no"),
            seconds=(float(params["seconds"])
                     if "seconds" in params else None))
    elif scenario == "reject_order":
        policy.add_reject_order(
            symbol=params.get("symbol"),
            message=str(params.get("message", "주문 거부(시나리오)")),
            count=(int(params["count"]) if "count" in params else None))
    elif scenario == "reject_cancel":
        policy.add_reject_cancel(
            order_no=params.get("order_no"),
            message=str(params.get("message", "취소 거부(시나리오)")),
            count=(int(params["count"]) if "count" in params else None))
    elif scenario == "halt_symbol":
        policy.halt(str(params["symbol"]))
    elif scenario == "balance_freeze":
        account = request.app.state.account
        policy.freeze_balance(
            cash=account.cash,
            holdings=account.snapshot_holdings(),
            seconds=float(params["seconds"]))
    elif scenario == "token_invalidate":
        request.app.state.tokens.force_invalidate()
    else:
        raise ValueError(f"unknown scenario {scenario!r}")


@router.post("/_replay/reset")
async def reset(request: Request) -> JSONResponse:
    state = request.app.state
    policy = _scenario_policy(request)
    if policy is not None:
        policy.clear()          # in-place — 엔진이 쥔 참조 유지
    state.account.reset(state.settings.cash)
    state.engine.reset_pending()
    return JSONResponse({
        "ok": True,
        "cash": state.account.cash,
        "replay_now": state.clock.now().isoformat(),   # clock 유지 확인용
        "faults": policy.describe() if policy else "custom-policy",
    })
