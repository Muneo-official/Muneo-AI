"""
EstimateEngine.cost_range() 단위테스트 — IQR 클램프가 중앙값을 왜곡하지 않는지 검증.

실제 사례: 22평 서울 아파트 견적 스팟체크 중, 매칭된 7개 사례의 cost_창호가
[776682, 1330000, 7080000, 8847729, 9302000, 9431000, 13480000]이었는데,
P25(1,330,000)가 유독 싼 이상치라 max_ratio=1.8 클램프가 hi를 2,394,000까지
끌어내리면서 원래 중앙값(8,847,729)까지 같이 뭉개져 "창호" 예측가가 실제
견적(8,910,000)의 1/4 수준으로 나온 버그가 있었다.
"""

from app.domain.estimate_engine import EstimateEngine


def test_cost_range_low_p25_outlier_does_not_crush_median():
    values = [776_682, 1_330_000, 7_080_000, 8_847_729, 9_302_000, 9_431_000, 13_480_000]

    r = EstimateEngine.cost_range(values, max_ratio=1.8)

    assert r["중간"] == 8_847_729  # 원본 중앙값이 클램프로 인해 깎이면 안 됨
    assert r["최소"] <= r["중간"] <= r["최대"]


def test_cost_range_still_clamps_upside_outlier():
    # P25가 정상적인데 P75만 튀는 경우엔 여전히 hi를 좁혀야 한다 (기존 목적 유지).
    values = [1_000_000, 1_100_000, 1_200_000, 1_300_000, 20_000_000]

    r = EstimateEngine.cost_range(values, max_ratio=1.8)

    assert r["최대"] < 20_000_000
    assert r["최소"] <= r["중간"] <= r["최대"]


def test_cost_range_no_clamp_when_ratio_within_bound():
    values = [1_000_000, 1_200_000, 1_400_000, 1_600_000, 1_800_000]

    r = EstimateEngine.cost_range(values, max_ratio=1.8)

    assert r == {"최소": 1_200_000, "최대": 1_600_000, "중간": 1_400_000}


def test_cost_range_empty_returns_none():
    assert EstimateEngine.cost_range([], max_ratio=1.8) is None


def test_cost_range_fewer_than_four_values_uses_min_max():
    values = [500_000, 1_500_000, 1_000_000]

    r = EstimateEngine.cost_range(values)

    assert r == {"최소": 500_000, "최대": 1_500_000, "중간": 1_000_000}


# ── 중간값에서 위아래 같은 비율까지만 ──────────────────────────────────────
# 예전에는 최소를 그대로 두고 최대만 최소 × max_ratio로 깎았다. P25가 낮으면 깎은 최대가 중간값보다 작아져
# 중간값을 최대로 쓰게 됐다 — 공종 범위 다섯 개 중 하나꼴로 중간값이 끝값과 같았다.


def test_cost_range_중간값이_최대와_같게_나오지_않는다():
    values = [776_682, 1_330_000, 7_080_000, 8_847_729, 9_302_000, 9_431_000, 13_480_000]

    r = EstimateEngine.cost_range(values, max_ratio=1.8)

    assert r["최소"] < r["중간"] < r["최대"]  # 예전에는 최대가 중간값(8,847,729)과 같았다


def test_cost_range_최대와_최소의_비율은_max_ratio를_넘지_않는다():
    for values in ([776_682, 1_330_000, 7_080_000, 8_847_729, 9_302_000, 9_431_000, 13_480_000],
                   [1_000_000, 1_100_000, 1_200_000, 1_300_000, 20_000_000],
                   [400_000, 4_000_000, 9_000_000]):
        r = EstimateEngine.cost_range(values, max_ratio=1.8)
        assert r["최대"] / r["최소"] <= 1.8 + 1e-6
        assert r["최소"] <= r["중간"] <= r["최대"]


def test_cost_range_값이_세_개뿐이어도_최소가_중간값에서_너무_멀어지지_않는다():
    # 금액이 있는 사례가 3건이면 최소·최대를 그대로 쓰는데, 한 건이 유독 낮으면 "최소 45만 / 중간 429만"이 됐다
    r = EstimateEngine.cost_range([445_714, 4_290_121, 6_000_000], max_ratio=1.8)

    assert r["중간"] == 4_290_121
    assert r["최소"] == int(4_290_121 / 1.8 ** 0.5)
    assert r["최대"] == int(4_290_121 * 1.8 ** 0.5)
