"""리플레이 R4(키움 엔드포인트 1세트) — 스펙 §7 실측 형태 재현 검증.

형태의 1차 소스는 정제 픽스처(fixtures/*.json — 실측 캡처의 필드셋·패딩
보존판)이고, 여기서는 서버 응답을 픽스처와 필드 단위 대조한다(형태
드리프트 방지 — 스펙 §7 확정)."""

import logging
from datetime import timedelta

from fastapi.testclient import TestClient

from replay.config import ReplaySettings
from replay.faults import FaultPolicy
from replay.main import create_replay_app

from .conftest import DEFAULT_ROWS, T0, TimeCtl, make_minutes_sqlite

SECRET = "SK-SENSITIVE-1234567890"


def make_app(tmp_path, ctl, cash=10_000_000, faults=None, speed=1.0):
    db = make_minutes_sqlite(tmp_path / "m.sqlite",
                             {"005930": DEFAULT_ROWS,
                              "069500": DEFAULT_ROWS})  # 069500=ETF 설정
    settings = ReplaySettings(anchor=T0, speed=speed, data_path=db,
                              cash=cash, etf_symbols=("069500",))
    return create_replay_app(settings, faults=faults,
                             monotonic=ctl.monotonic, wall_now=ctl.wall_now)


def issue_token(client) -> dict:
    body = client.post("/oauth2/token",
                       json={"grant_type": "client_credentials",
                             "appkey": "AK-TEST",
                             "secretkey": SECRET}).json()
    return {"authorization": f"Bearer {body['token']}", "api-id": ""}


def tr(client, headers, api_id, path, body) -> tuple[dict, object]:
    response = client.post(path, json=body,
                           headers={**headers, "api-id": api_id})
    return response.json(), response


# ── 토큰 수명(단일 활성·8005) ──────────────────────────────────────────


def test_토큰_발급_형태와_TR_인증(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    body = client.post("/oauth2/token", json={"appkey": "a",
                                              "secretkey": "b"}).json()
    assert body["return_code"] == 0
    assert len(body["expires_dt"]) == 14 and body["expires_dt"].isdigit()
    headers = {"authorization": f"Bearer {body['token']}"}
    data, response = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                        {"stk_cd": "005930"})
    assert data["return_code"] == 0
    assert response.headers["cont-yn"] == "N"
    assert response.headers["x-replay-speed"] == "1.0"


def test_재발급은_기존_토큰을_8005로_무효화한다(tmp_path):
    """Phase 2 실측 사고 재현: 두 번째 발급이 첫 토큰을 무효화 — HTTP 200 +
    return_code=3 + '[8005' 포함 return_msg(401 아님)."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    old = issue_token(client)
    new = issue_token(client)
    data, response = tr(client, old, "ka10095", "/api/dostk/stkinfo",
                        {"stk_cd": "005930"})
    assert response.status_code == 200          # 401이 아니다(실측)
    assert data["return_code"] == 3 and "[8005" in data["return_msg"]
    # speed 스탬프는 오류 응답에도(§5 ① — 아키텍트 R4 Minor)
    assert response.headers["x-replay-speed"] == "1.0"
    data, _ = tr(client, new, "ka10095", "/api/dostk/stkinfo",
                 {"stk_cd": "005930"})
    assert data["return_code"] == 0


def test_revoke_후_토큰은_무효(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    body = client.post("/oauth2/token", json={}).json()
    assert client.post("/oauth2/revoke",
                       json={"appkey": "a", "secretkey": "b",
                             "token": body["token"]}
                       ).json()["return_code"] == 0
    headers = {"authorization": f"Bearer {body['token']}"}
    data, _ = tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    assert "[8005" in data["return_msg"]


def test_시크릿은_로그와_응답에_나타나지_않는다(tmp_path, caplog):
    """§7 무로그 계약 — appkey/secretkey는 수신 즉시 폐기. token/revoke
    **두 엔드포인트 모두** 회귀로 고정(보안 R4 — revoke도 시크릿을 수신
    하는 표면이므로 향후 감사 로깅 추가가 계약을 깨면 여기서 잡힌다)."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    with caplog.at_level(logging.DEBUG):
        issued = client.post("/oauth2/token",
                             json={"appkey": "AK-TEST",
                                   "secretkey": SECRET})
        revoked = client.post("/oauth2/revoke",
                              json={"appkey": "AK-TEST",
                                    "secretkey": SECRET,
                                    "token": issued.json()["token"]})
    assert SECRET not in caplog.text
    assert SECRET not in issued.text and SECRET not in revoked.text
    assert revoked.json()["return_code"] == 0


def test_revoke_깨진_바디는_500_없이_관용(tmp_path):
    """보안 R4 Minor — 파싱 실패 예외 경로 제거(스택트레이스 유출면 차단)."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    response = client.post("/oauth2/revoke", content=b"{broken",
                           headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.json()["return_code"] == 0


# ── ka10095 (§7 — G1 실측) ─────────────────────────────────────────────


def test_ka10095_파이프_구분과_합성_호가(tmp_path, fixture_json):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    data, _ = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                 {"stk_cd": "005930|069500"})
    rows = data["atn_stk_infr"]
    assert [r["stk_cd"] for r in rows] == ["005930", "069500"]
    # 필드셋 = 실측 63필드(정제 픽스처 대조)
    assert set(rows[0]) == set(fixture_json("ka10095_row.json")["row"])
    # 09:00 close 100,000 — kospi 틱 100: 합성 호가 ±1~5틱, ±부호 표기
    assert rows[0]["cur_prc"] == "+100000"
    assert rows[0]["sel_bid"] == "+100100" and rows[0]["buy_bid"] == "+99900"
    assert rows[0]["sel_5th_bid"] == "+100500"
    assert rows[0]["buy_5th_bid"] == "+99500"
    # ETF 틱 5원 — 시장 구분이 합성 호가에 반영
    assert rows[1]["sel_bid"] == "+100005"
    # 등락률 실값(broker-api R4 — 스펙 §7 "엔진 소비분 실값"): 전일(07-09)
    # 종가 95,000 대비 100,000 = +5.26%, 기준가=전일종가
    assert rows[0]["base_pric"] == "95000"
    assert rows[0]["pred_pre"] == "+5000"
    assert rows[0]["pred_pre_sig"] == "2"
    assert rows[0]["flu_rt"] == "+5.26"


def test_ka10095_비파이프_구분자와_프리픽스는_빈_행(tmp_path):
    """실측: 세미콜론/콤마/공백 결합·KRX: 프리픽스는 미바인딩 — rc=0 + 빈 행."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    for raw in ("005930;069500", "005930,069500", "005930 069500",
                "KRX:005930"):
        data, _ = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                     {"stk_cd": raw})
        assert data["return_code"] == 0
        assert [r["stk_cd"] for r in data["atn_stk_infr"]] == [""]


def test_ka10095_미지_코드는_빈_행_상한_101은_rc5(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    data, _ = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                 {"stk_cd": "005930|000000|069500"})
    codes = [r["stk_cd"] for r in data["atn_stk_infr"]]
    assert codes == ["005930", "", "069500"]      # 부분 실패 = 빈 행(실측)
    over = "|".join(f"{i:06d}" for i in range(101))
    data, _ = tr(client, headers, "ka10095", "/api/dostk/stkinfo",
                 {"stk_cd": over})
    assert data["return_code"] == 5               # 실측: 101종목 → rc=5
    assert "atn_stk_infr" not in data


# ── 주문 TR (§7 — G2/G3 실측) ──────────────────────────────────────────


def test_kt10000_시장가_체결과_kt00018_형태(tmp_path, fixture_json):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"dmst_stex_tp": "KRX", "stk_cd": "005930",
                  "ord_qty": "10", "trde_tp": "3"})
    assert data["return_code"] == 0
    assert data["return_msg"] == "모의투자 매수주문완료"
    assert len(data["ord_no"]) == 7 and data["ord_no"].isdigit()  # 실측 형태
    fixture = fixture_json("kt00018.json")
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt",
                 {"qry_tp": "1", "dmst_stex_tp": "KRX"})
    assert set(data) == set(fixture["top_level_keys"])   # 최상위 11키 실측
    row = data["acnt_evlt_remn_indv_tot"][0]
    assert set(row) == set(fixture["row"])               # 행 23필드 실측
    assert row["stk_cd"] == "A005930"                    # A 프리픽스
    assert row["pur_pric"] == "000000000100000"          # 15자리 제로패딩
    assert row["cur_prc"] == "000000100000"              # 12자리(실측 폭)
    assert row["rmnd_qty"] == "000000000000010"


def test_kt00018_음수_손익_패딩과_포지션_0건_최상위_필수(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    # 포지션 0건 — tot_* 최상위 필드가 그래도 존재(어댑터 하드 인덱싱 계약)
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["tot_evlt_amt"] == "0".zfill(15)
    assert data["tot_evlt_pl"] == "0".zfill(15)
    assert data["acnt_evlt_remn_indv_tot"] == []
    # 100,000 매수 후 재생 09:01:30(현재가 98,500) — 음수 손익 부호 폭 실측형
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    ctl.mono += 90.0
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    row = data["acnt_evlt_remn_indv_tot"][0]
    assert row["evltv_prft"] == "-00000000015000"   # 부호 포함 15폭(실측형)
    assert row["prft_rt"].startswith("-") and len(row["prft_rt"]) == 12
    assert row["pred_close_pric"] == "000000095000"  # 전일 종가 실값(12폭)


def test_kt10000_지정가_틱_위반은_RC4003(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "0",
                  "ord_uv": "100050"})
    assert data["return_code"] == 20 and "RC4003" in data["return_msg"]


def test_kt10000_시장가에_ord_uv_동봉과_두자리_trde_tp는_거부(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3",
                  "ord_uv": "100000"})
    assert data["return_code"] == 20
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "03",
                  "ord_uv": "100000"})
    assert data["return_code"] == 20   # 실측: 한 자리만 유효("00"/"03" 오류)


def test_kt10003_전량취소와_실측_필드(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
                   "ord_uv": "99000"})
    data, _ = tr(client, headers, "kt10003", "/api/dostk/ordr",
                 {"dmst_stex_tp": "KRX", "orig_ord_no": order["ord_no"],
                  "stk_cd": "005930", "cncl_qty": "0"})
    assert data["return_code"] == 0
    assert data["return_msg"] == "모의투자 취소주문완료"
    assert data["base_orig_ord_no"] == order["ord_no"]
    assert data["cncl_qty"] == "10"
    again, _ = tr(client, headers, "kt10003", "/api/dostk/ordr",
                  {"orig_ord_no": order["ord_no"], "cncl_qty": "0"})
    assert again["return_code"] == 20      # 이미 취소된 주문
    partial, _ = tr(client, headers, "kt10003", "/api/dostk/ordr",
                    {"orig_ord_no": order["ord_no"], "cncl_qty": "5"})
    assert partial["return_code"] == 20    # 부분취소 미실측 — fail-loud


# ── ka10075 (§7 — 전파 지연·io_tp_nm) ──────────────────────────────────


def test_ka10075_전파_지연과_행_형태(tmp_path, fixture_json):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    order, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                  {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
                   "ord_uv": "99000"})
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt",
                 {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"})
    assert data["oso"] == []               # 전파 지연 창(§8 — C1 재현)
    ctl.wall += timedelta(seconds=2)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt",
                 {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"})
    row = data["oso"][0]
    assert set(row) == set(fixture_json("ka10075_row.json")["row_fields"])
    assert row["ord_no"] == order["ord_no"]
    assert row["ord_stt"] == "접수"
    assert row["ord_pric"] == "99000"      # 실측: 패딩 없는 표기
    assert row["stk_cd"] == "005930"       # 실측: ka10075는 A 프리픽스 없음
    assert "매수" in row["io_tp_nm"]        # containment 계약


def test_ka10075_매도_io_tp_nm은_실측_원문(tmp_path):
    """실측(-매도): 정확일치 파서를 깨뜨리는 접두 부분문자열 형태 유지."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    tr(client, headers, "kt10001", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
        "ord_uv": "103000"})
    ctl.wall += timedelta(seconds=2)
    data, _ = tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    assert data["oso"][0]["io_tp_nm"] == "-매도"


# ── kt00001 (§7 — 예약 차감 ord_alow_amt) ──────────────────────────────


def test_kt00001_예약_차감_주문가능금액(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    data, _ = tr(client, headers, "kt00001", "/api/dostk/acnt",
                 {"qry_tp": "3"})
    assert data["entr"] == "000000010000000"
    assert data["ord_alow_amt"] == "000000010000000"
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
        "ord_uv": "99000"})
    data, _ = tr(client, headers, "kt00001", "/api/dostk/acnt",
                 {"qry_tp": "3"})
    assert data["entr"] == "000000010000000"            # 예수금은 미차감
    assert data["ord_alow_amt"] == "000000009006535"    # 990,000+수수료 예약


# ── 조회 TR 계약(check_fills)·통합 ─────────────────────────────────────


def test_모든_TR_진입마다_check_fills_1회(tmp_path):
    """플랜 R4 계약(아키텍트 R4로 주문 TR까지 확대) — 호출 시점이
    배선마다 다르면 체결 재현이 흔들린다."""
    ctl = TimeCtl()
    app = make_app(tmp_path, ctl)
    calls = []
    original = app.state.engine.check_fills
    app.state.engine.check_fills = lambda: (calls.append(1), original())[1]
    client = TestClient(app)
    headers = issue_token(client)
    tr(client, headers, "ka10095", "/api/dostk/stkinfo",
       {"stk_cd": "005930"})
    tr(client, headers, "ka10075", "/api/dostk/acnt", {})
    tr(client, headers, "kt00001", "/api/dostk/acnt", {})
    tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert len(calls) == 5


def test_조회_없는_연속_제출도_체결이_반영된다(tmp_path):
    """아키텍트 R4 회귀 — 스탠딩 매수가 체결됐어야 할 시각 이후, 조회 TR
    없이 바로 매도를 제출해도 오거부되지 않아야 한다(주문 TR 진입
    check_fills 계약)."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
        "ord_uv": "99000"})           # 09:00 미크로스 — 스탠딩
    ctl.mono += 90.0                  # 09:01:30 — 마켓터블(98,500) 체결 시각
    data, _ = tr(client, headers, "kt10001", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "3"})
    assert data["return_code"] == 0   # 오거부(insufficient holdings) 금지
    assert data["return_msg"] == "모의투자 매도주문완료"


def test_통합_스탠딩_지정가가_시각_진행_후_조회에서_체결된다(tmp_path):
    """end-to-end: 접수 → 재생 시계 전진 → 조회 TR 진입 계약이 체결을
    구체화 → kt00018에 포지션(§8 마켓터블 재평가 — 현재가 98,500)."""
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl))
    headers = issue_token(client)
    tr(client, headers, "kt10000", "/api/dostk/ordr",
       {"stk_cd": "005930", "ord_qty": "10", "trde_tp": "0",
        "ord_uv": "99000"})
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    assert data["acnt_evlt_remn_indv_tot"] == []   # 아직 미체결
    ctl.mono += 90.0                               # 재생 09:01:30
    data, _ = tr(client, headers, "kt00018", "/api/dostk/acnt", {})
    row = data["acnt_evlt_remn_indv_tot"][0]
    assert row["pur_pric"] == "000000000098500"    # 마켓터블 재평가 체결가
    assert row["rmnd_qty"] == "000000000000010"


def test_결함_주입이_엔드포인트_경로에도_작동한다(tmp_path):
    """§9 seam이 api 계층까지 관통하는지 — reject_order가 kt10000 거부로."""
    class Reject(FaultPolicy):
        def reject_order(self, symbol):
            return "주문 거부(시나리오)"

    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl, faults=Reject()))
    headers = issue_token(client)
    data, _ = tr(client, headers, "kt10000", "/api/dostk/ordr",
                 {"stk_cd": "005930", "ord_qty": "1", "trde_tp": "3"})
    assert data["return_code"] == 20 and "주문 거부" in data["return_msg"]


def test_미지원_TR과_상태_표면(tmp_path):
    ctl = TimeCtl()
    client = TestClient(make_app(tmp_path, ctl, speed=2.0))
    headers = issue_token(client)
    data, _ = tr(client, headers, "ka10081", "/api/dostk/stkinfo", {})
    assert data["return_code"] == 1        # 미지원 TR fail-loud(§7 — 2차)
    status = client.get("/_replay/status")
    assert status.json()["speed"] == 2.0   # §5 ① speed 스탬프
    assert status.headers["x-replay-speed"] == "2.0"
