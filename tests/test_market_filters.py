"""v3.4 market_filters 단위 테스트."""
from datetime import datetime
from unittest.mock import patch


def test_spread_filter_blocks_high_spread():
    from market_filters import SpreadFilter
    f = SpreadFilter(max_spread_pct=0.15)
    # 0.2% spread → 차단
    blocked, reason = f.should_block(orderbook={"best_bid": 100, "best_ask": 100.2})
    assert blocked
    assert "Spread" in reason


def test_spread_filter_passes_low_spread():
    from market_filters import SpreadFilter
    f = SpreadFilter(max_spread_pct=0.15)
    blocked, _ = f.should_block(orderbook={"best_bid": 100, "best_ask": 100.05})
    assert not blocked


def test_spread_filter_passes_no_data():
    """orderbook 없으면 fail-open (통과)."""
    from market_filters import SpreadFilter
    f = SpreadFilter()
    blocked, _ = f.should_block(orderbook=None)
    assert not blocked


def test_regime_gate_blocks_transition_zone():
    """ADX 20-25 전환기는 모든 매수 차단."""
    from market_filters import RegimeGate
    g = RegimeGate(ranging_max=20, trending_min=25)
    for adx in [20, 22, 25]:
        blocked, reason = g.should_block(adx=adx, signal_type="unknown")
        assert blocked, f"ADX {adx} should be blocked"
        assert "전환기" in reason


def test_regime_gate_blocks_mean_reversion_in_trend():
    from market_filters import RegimeGate
    g = RegimeGate()
    blocked, reason = g.should_block(adx=30, signal_type="mean_reversion")
    assert blocked
    assert "추세장" in reason


def test_regime_gate_blocks_trend_in_ranging():
    from market_filters import RegimeGate
    g = RegimeGate()
    blocked, reason = g.should_block(adx=15, signal_type="trend")
    assert blocked
    assert "횡보장" in reason


def test_regime_gate_allows_matched_strategy():
    from market_filters import RegimeGate
    g = RegimeGate()
    # 추세장 + 트렌드추종 = OK
    blocked, _ = g.should_block(adx=30, signal_type="trend")
    assert not blocked
    # 횡보장 + 평균회귀 = OK
    blocked, _ = g.should_block(adx=15, signal_type="mean_reversion")
    assert not blocked


def test_time_window_blocks_korean_dawn():
    """KST 02-06시 매수 차단."""
    from market_filters import TimeWindowFilter
    f = TimeWindowFilter(block_start_hour=2, block_end_hour=6)

    class FakeDT:
        def __init__(self, h): self.hour = h

    # mock datetime.now to return KST 03:00
    with patch("market_filters.datetime") as m:
        m.now.return_value = FakeDT(3)
        blocked, reason = f.should_block()
        assert blocked
        assert "저유동성" in reason


def test_time_window_passes_daytime():
    from market_filters import TimeWindowFilter
    f = TimeWindowFilter(block_start_hour=2, block_end_hour=6)

    class FakeDT:
        def __init__(self, h): self.hour = h

    with patch("market_filters.datetime") as m:
        m.now.return_value = FakeDT(14)  # 14:00
        blocked, _ = f.should_block()
        assert not blocked


def test_fear_greed_blocks_extreme_greed():
    """API 모킹으로 score 85 → 차단."""
    from market_filters import FearGreedFilter
    f = FearGreedFilter(max_greed=75)

    with patch.object(f, "get_score", return_value=85):
        blocked, reason = f.should_block()
        assert blocked
        assert "85" in reason


def test_fear_greed_passes_neutral():
    from market_filters import FearGreedFilter
    f = FearGreedFilter(max_greed=75)
    with patch.object(f, "get_score", return_value=50):
        blocked, _ = f.should_block()
        assert not blocked


def test_fear_greed_fail_open_on_api_error():
    """API 실패 시 매수 차단하지 않음 (fail-open)."""
    from market_filters import FearGreedFilter
    f = FearGreedFilter()
    with patch.object(f, "get_score", return_value=None):
        blocked, _ = f.should_block()
        assert not blocked


def test_filter_chain_collects_all_reasons():
    """체인이 모든 차단 사유 수집."""
    from market_filters import FilterChain, SpreadFilter, RegimeGate

    chain = FilterChain([
        SpreadFilter(max_spread_pct=0.15),
        RegimeGate(),
    ])
    blocked, reasons = chain.check(
        orderbook={"best_bid": 100, "best_ask": 100.5},  # spread 0.5% > 0.15%
        adx=22,                                            # 전환기
        signal_type="unknown",
    )
    assert blocked
    assert len(reasons) == 2  # 두 필터 모두 차단


def test_filter_chain_passes_when_all_clear():
    from market_filters import FilterChain, SpreadFilter, RegimeGate

    chain = FilterChain([
        SpreadFilter(),
        RegimeGate(),
    ])
    blocked, reasons = chain.check(
        orderbook={"best_bid": 100, "best_ask": 100.05},  # spread 0.05% OK
        adx=30,                                            # 추세장
        signal_type="trend",                               # 매칭
    )
    assert not blocked
    assert reasons == []
