"""리플레이 R5(결함 주입) — 스펙 §9 시나리오 표 13종 전수 + 관리 API 계약.

각 테스트 독스트링에 §9 표의 대응 행을 명시한다(faults.py 모듈 독스트링의
매핑 표가 소스). 시간 창은 전부 벽시계(§5 — 배속 무관)."""

import time
from datetime import timedelta

from fastapi.testclient import TestClient

from replay.faults import FaultPolicy

from .conftest import TimeCtl, issue_token, make_app, tr


def fault(client, scenario, **params):
    response = client.post("/_replay/faults",
                           json={"scenario": scenario, "params": params})
    assert response.status_code == 200, response.text
    return response.json()


def setup(tmp_path, **kwargs):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl, **kwargs))
    return ctl, client, issue_token(client)


# ── §9: ka10075 전파 지연 확대 ─────────────────────────────────────────


def test_전파_지연_확대(tmp_path):
    """§9 "ka10075 전파 지연 확대(N초)" — poll_unfilled C1 3중 방어 검증용
    창 확장."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "propagation_delay", seconds=10.0)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
        "ord_uv": "99000"})
    ctl.wall += timedelta(seconds=2)      # 기본 1.5s였다면 이미 노출
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert data["oso"] == []
    ctl.wall += timedelta(seconds=9)      # 총 11s > 10s
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert len(data["oso"]) == 1


# ── §9: 조회 TR 간헐 500 / 429 레이트리밋 ──────────────────────────────


def test_api_fault_500은_지정_횟수만_발생(tmp_path):
    """§9 "조회 TR 간헐 500/타임아웃" — 어댑터 재시도·quote_failure 경로."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "api_fault", api_id="ka10095", mode="http500", count=2)
    for _ in range(2):
        _, response = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                         {"stk_cd": "005930"})
        assert response.status_code == 500
    data, response = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                        {"stk_cd": "005930"})
    assert response.status_code == 200 and data["return_code"] == 0


def test_api_fault_429는_주문_TR에도_적용(tmp_path):
    """§9 "429 레이트리밋" — 어댑터 429 백오프 경로(주문 버킷)."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "api_fault", api_id="kt10000", mode="http429", count=1)
    _, response = tr(client, headers, "kt10000", "/api/dostk/ordr",
                     {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert response.status_code == 429
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert data["return_code"] == 0


def test_api_fault_delay는_지연_후_정상_응답(tmp_path):
    """§9 "조회 TR 간헐 500/타임아웃" — delay 모드. ⚠️ 실제 타임아웃
    유발은 delay_sec > 어댑터 HTTP 타임아웃이어야 한다(§9 사용 규율 —
    여기서는 지연 자체의 발동만 검증, R7이 어댑터 타임아웃 초과 값으로
    타임아웃 경로를 검증)."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "api_fault", api_id="kt00001", mode="delay",
          delay_sec=0.2, count=1)
    started = time.perf_counter()
    data, response = tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    elapsed = time.perf_counter() - started
    assert response.status_code == 200 and data["return_code"] == 0
    assert elapsed >= 0.2                      # 지연이 실제로 걸렸다
    data, response = tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    assert response.status_code == 200         # count 소진 — 정상 복귀


# ── §9: 부분체결 ───────────────────────────────────────────────────────


def test_부분체결_ratio는_벽시계_래칫으로_진행(tmp_path):
    """§9 "부분체결(지정가 x%만)" — 6a 부분체결 계약·잔량 취소 검증용.
    진행은 벽시계 interval당 1청크(트레이더 R5 I1 — 폴링 횟수와 분리:
    엔진이 더 자주 폴링해도 결함이 빨리 해소되지 않는다)."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "partial_fill", ratio=0.4, interval_sec=1.0)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    # 제출 시 1청크: floor(10×0.4)=4. 래칫 창 내 연속 폴링은 진행 없음
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    ctl.wall += timedelta(seconds=2)   # 전파 지연 경과 + 래칫 창 밖
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    row = data["oso"][0]
    assert row["ord_no"] == order["ord_no"]
    assert row["cntr_qty"] == "6" and row["oso_qty"] == "4"  # 4 + 2(1청크)
    # 래칫 창 내 재폴링 — 추가 진행 없음(6/4 유지)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    row = data["oso"][0]
    assert row["cntr_qty"] == "6" and row["oso_qty"] == "4"


# ── §9: 취소 거부 / 신규 주문 거부 ─────────────────────────────────────


def test_취소_거부(tmp_path):
    """§9 "취소 거부(rc!=0)" — 이중매매 가드(폴백 중단) 검증용."""
    ctl, client, headers = setup(tmp_path)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
                   "ord_uv": "99000"})
    fault(client, "reject_cancel", message="취소 거부(시나리오)")
    data, _ = tr(client, headers, "kt10003", "/api/dostk/ordr",
                 {"orig_ord_no": order["ord_no"], "cncl_qty": "0"})
    assert data["return_code"] == 20 and "취소 거부" in data["return_msg"]


def test_신규_주문_거부는_횟수_소진_후_정상(tmp_path):
    """§9 "신규 주문 거부(rc!=0, 비취소)" — 진입/청산 발주 실패 재시도."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "reject_order", symbol="005930",
          message="주문 거부(시나리오)", count=1)
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert data["return_code"] == 20 and "주문 거부" in data["return_msg"]
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert data["return_code"] == 0


# ── §9: fill 억제(익절/진입 지정가·상하한가 락·VI) ─────────────────────


def test_익절_지정가_fill_억제와_해제_재개(tmp_path):
    """§9 "익절 지정가 fill 억제(N초)" — exit_limit_timeout→폴백 실경로.
    해제 후 재개 체결가는 §8 마켓터블 재평가(현재가)."""
    ctl, client, headers = setup(tmp_path)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    fault(client, "suppress_fill", side="sell", style="limit", seconds=5.0)
    sell, _ = tr(client, headers, "kt10001", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
                  "ord_uv": "99000"})   # 마켓터블(현재가 100,000)이지만 억제
    assert sell["return_code"] == 0
    ctl.wall += timedelta(seconds=2)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert data["oso"][0]["oso_qty"] == "10"    # 억제로 미체결 잔존
    ctl.wall += timedelta(seconds=4)            # 창(5s) 만료
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"] == []  # 재개 체결 — 전량 청산


def test_진입_지정가_fill_억제와_해제_재개(tmp_path):
    """§9 "진입 지정가 fill 억제(N초)"(트레이더 R4 추가 행) —
    limit_order_timeout→취소→시장가 재발주 폴백 + poll_unfilled C1의
    유일한 실행 수단(§8 진입 대칭 함정: ask 기반 진입 지정가는 정상
    매칭으로는 항상 즉시 체결). 익절 케이스와 대칭."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "suppress_fill", side="buy", style="limit", seconds=5.0)
    buy, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
                 "ord_uv": "100100"})   # ask(현재가+1틱) — 마켓터블이지만 억제
    assert buy["return_code"] == 0
    ctl.wall += timedelta(seconds=2)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert data["oso"][0]["oso_qty"] == "10"    # 억제로 미체결 잔존
    ctl.wall += timedelta(seconds=4)            # 창(5s) 만료 — 재개
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    row = data["acnt_evlt_remn_indv_tot"][0]
    assert row["pur_pric"] == "000000000100000"  # §8 재평가 — 현재가 체결
    assert row["rmnd_qty"] == "000000000000010"


def test_상하한가_락_시장가_미체결_잔존(tmp_path):
    """§9 "상/하한가 락" — 시장가 매도가 미체결로 잔존(pending 추적·취소
    금지 계약·EXIT_FAILED 검증용). 무기한 억제(해제는 reset)."""
    ctl, client, headers = setup(tmp_path)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    fault(client, "suppress_fill", symbol="005930", style="market",
          side="sell")
    sell, _ = tr(client, headers, "kt10001", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    assert sell["return_code"] == 0
    ctl.wall += timedelta(seconds=60)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert data["oso"][0]["oso_qty"] == "10"    # 여전히 잔존


def test_VI_흉내_연속_미체결_후_재개(tmp_path):
    """§9 "연속 미체결(VI 흉내)" — recommended_delay 백오프·즉시체결 가정
    비적용 구간."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "suppress_fill", symbol="005930", seconds=3.0)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "5", "trde_tp": "3"})
    assert order["return_code"] == 0
    ctl.wall += timedelta(seconds=2)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert data["oso"][0]["oso_qty"] == "5"
    ctl.wall += timedelta(seconds=2)            # 창 만료 — 재개
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"][0]["rmnd_qty"].endswith("5")


# ── §9: 거래정지 ───────────────────────────────────────────────────────


def test_거래정지_halt는_빈_행과_체결_억제(tmp_path):
    """§9 "거래정지 상태 전환" — 모니터의 '시세 결측 지속' 관측(ka10095
    빈 행) + 해당 심볼 체결 무기한 억제."""
    ctl, client, headers = setup(tmp_path)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "5", "trde_tp": "0",
                   "ord_uv": "99000"})   # 스탠딩
    fault(client, "halt_symbol", symbol="005930")
    data, _ = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                 {"stk_cd": "005930|069500"})
    codes = [r["stk_cd"] for r in data["atn_stk_infr"]]
    assert codes == ["", "069500"]       # 정지 심볼만 빈 행
    ctl.mono += 90.0                      # 크로스 시각 경과에도
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"] == []   # 체결 억제 유지


# ── §9: 잔고 반영 지연 ─────────────────────────────────────────────────


def test_잔고_동결_창_동안_체결이_잔고에_안_보인다(tmp_path):
    """§9 "잔고 반영 지연" — 유령 판정 2회 확인(P5-T7 C2)·하드 게이트
    재오픈 검증용: 동결 창 내 체결은 kt00018에 미반영, 창 만료 후 반영."""
    ctl, client, headers = setup(tmp_path)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    fault(client, "balance_freeze", seconds=5.0)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "5", "trde_tp": "3"})   # 창 내 체결
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"][0]["rmnd_qty"].endswith("10")
    ctl.wall += timedelta(seconds=6)
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"][0]["rmnd_qty"].endswith("15")


# ── §9: 토큰 8005 무효화 ───────────────────────────────────────────────


def test_토큰_무효화_후_재발급으로_복구(tmp_path):
    """§9 "토큰 8005 무효화" — TokenManager 재발급 경로(Phase 2 사고를
    임의 시점 재현)."""
    ctl, client, headers = setup(tmp_path)
    fault(client, "token_invalidate")
    data, response = tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    assert response.status_code == 200 and "[8005" in data["return_msg"]
    headers = issue_token(client)         # 재발급 — 복구
    data, _ = tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    assert data["return_code"] == 0


# ── 관리 API 계약 ──────────────────────────────────────────────────────


def test_reset은_계좌와_faults를_초기화하고_clock은_유지(tmp_path):
    """§9 reset 범위 계약(개발자 I3): faults+account+pending 초기화,
    clock anchor 유지(재생 진행은 리셋과 독립)."""
    ctl, client, headers = setup(tmp_path)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    fault(client, "partial_fill", ratio=0.5)
    ctl.mono += 90.0                      # 재생 시각 진행
    reset = client.post("/_replay/reset").json()
    assert reset["ok"] and reset["cash"] == 10_000_000
    assert reset["faults"]["fill_ratio"] is None      # in-place clear
    assert "09:01:30" in reset["replay_now"]          # clock 유지(리셋 아님)
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"] == []      # 보유 초기화
    data, _ = tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    assert data["entr"] == "000000010000000"          # 예수금 초기화
    # 리셋 후 신규 주문 정상 + 주문번호 시퀀스는 유지(재사용 금지)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert order["return_code"] == 0 and order["ord_no"] != "0000001"


def test_미지_시나리오와_잘못된_파라미터는_400(tmp_path):
    """§9 fail-loud — 오타 시나리오가 조용히 무시되면 '결함을 주입했다고
    믿는' 검증이 무결함 런이 된다."""
    ctl, client, headers = setup(tmp_path)
    response = client.post("/_replay/faults",
                           json={"scenario": "no_such", "params": {}})
    assert response.status_code == 400
    response = client.post("/_replay/faults",
                           json={"scenario": "partial_fill",
                                 "params": {"ratio": 1.5}})
    assert response.status_code == 400
    response = client.post("/_replay/faults",
                           json={"scenario": "api_fault",
                                 "params": {"api_id": "ka10095",
                                            "mode": "weird"}})
    assert response.status_code == 400
    # 시간 파라미터 상한(보안 R5 — 단위 착각이 sleep 장기 점유를 만드는
    # 것 차단): 300s 초과 거부
    response = client.post("/_replay/faults",
                           json={"scenario": "api_fault",
                                 "params": {"api_id": "ka10095",
                                            "mode": "delay",
                                            "delay_sec": 100000}})
    assert response.status_code == 400
    response = client.post("/_replay/faults",
                           json={"scenario": "suppress_fill",
                                 "params": {"seconds": 100000}})
    assert response.status_code == 400
    # count=0("0회" 의도)은 소비 로직상 1회 발동하는 함정 — 사전 거부
    response = client.post("/_replay/faults",
                           json={"scenario": "reject_order",
                                 "params": {"count": 0}})
    assert response.status_code == 400


def test_커스텀_정책_주입_시_관리_API는_400(tmp_path):
    """테스트가 자체 FaultPolicy를 주입한 조립에서는 시나리오 관리 불가 —
    침묵 무시 대신 명시 거부."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl, faults=FaultPolicy()))
    response = client.post("/_replay/faults",
                           json={"scenario": "partial_fill",
                                 "params": {"ratio": 0.5}})
    assert response.status_code == 400
    status = client.get("/_replay/status").json()
    assert status["faults"] == "custom-policy"


def test_status는_활성_시나리오를_보고한다(tmp_path):
    ctl, client, headers = setup(tmp_path)
    fault(client, "partial_fill", ratio=0.5)
    fault(client, "halt_symbol", symbol="069500")
    status = client.get("/_replay/status").json()
    assert status["faults"]["fill_ratio"] == 0.5
    assert status["faults"]["halted"] == ["069500"]
