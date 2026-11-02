"""이익공유(profit-sharing) 배분 시뮬레이터 테스트."""
import pytest

from src.optimizer.profit_sharing import InvalidShareError, compute_profit_sharing


def test_compute_profit_sharing_splits_amounts_correctly():
    result = compute_profit_sharing(100_000.0, owner_pct=50.0, operator_pct=30.0, platform_pct=20.0)
    assert result["total_savings_krw"] == 100_000.0
    assert result["amounts_krw"] == {"owner": 50_000.0, "operator": 30_000.0, "platform": 20_000.0}
    assert "note" in result


def test_compute_profit_sharing_negative_savings_treated_as_zero():
    result = compute_profit_sharing(-500.0, owner_pct=50.0, operator_pct=30.0, platform_pct=20.0)
    assert result["total_savings_krw"] == 0.0
    assert result["amounts_krw"] == {"owner": 0.0, "operator": 0.0, "platform": 0.0}


def test_compute_profit_sharing_rejects_shares_not_summing_to_100():
    with pytest.raises(InvalidShareError):
        compute_profit_sharing(100_000.0, owner_pct=50.0, operator_pct=30.0, platform_pct=30.0)


def test_compute_profit_sharing_rejects_negative_share():
    with pytest.raises(InvalidShareError):
        compute_profit_sharing(100_000.0, owner_pct=-10.0, operator_pct=90.0, platform_pct=20.0)
