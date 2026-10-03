"""车地无线链路质量：打分、可信度衰减与退避。"""

from __future__ import annotations

import pytest

from psdline.config import default_config
from psdline.link import LinkSample, LinkPolicy
from psdline.link.policy import WindowAggregate

POLICY = LinkPolicy(default_config().link)


def _sample(rssi=-55.0, snr=25.0, rtt=100.0, loss=0.01):
    return LinkSample(
        train_id="T01", seq=1, epoch="e1", sent_at=0.0, at=0.0,
        rssi_dbm=rssi, snr_db=snr, rtt_ms=rtt, packet_loss=loss,
    )


def test_perfect_sample_scores_one():
    assert sample_score(_sample()) == 1.0


def test_worst_sample_scores_zero():
    worst = _sample(rssi=-100.0, snr=0.0, rtt=1000.0, loss=0.5)
    assert sample_score(worst) == 0.0


def test_midrange_sample_between_bounds():
    mid = _sample(
        rssi=(-65 - 90) / 2, snr=(20 + 5) / 2,
        rtt=(150 + 800) / 2, loss=(0.02 + 0.20) / 2,
    )
    score = sample_score(mid)
    assert 0.4 < score < 0.6


def sample_score(sample, policy=POLICY):
    from psdline.link.sample import sample_score as sc
    return sc(sample, policy)


def test_confidence_is_one_while_fresh_and_good():
    assert POLICY.confidence(age_s=0.5, window_score=1.0, has_contact=True) == 1.0


def test_confidence_decays_with_outage_duration():
    # 可信度以断线时长为主：断得越久越低，到 stale_s 归零。
    fresh = POLICY.confidence(1.0, 1.0, True)
    middle = POLICY.confidence((2 + 12) / 2, 1.0, True)
    gone = POLICY.confidence(12.0, 1.0, True)
    assert fresh == 1.0
    assert 0.0 < middle < 1.0
    assert gone == 0.0


def test_confidence_zero_without_contact_even_if_window_good():
    # 纯近窗口径的致命缺陷（断线窗口冻结）在这里不成立：无接触必为 0。
    assert POLICY.confidence(100.0, 1.0, has_contact=False) == 0.0


def test_poor_window_caps_confidence_even_when_fresh():
    # 近窗质量为辅：帧虽新鲜但链路差，可信度被窗口分压。
    score = POLICY.confidence(0.5, window_score=0.4, has_contact=True)
    assert score == 0.4
    assert score < default_config().link.confidence_auto


def test_backoff_grows_then_caps_and_is_deterministic():
    d0 = POLICY.backoff_delay(0, "T01", 1)
    d1 = POLICY.backoff_delay(1, "T01", 1)
    d2 = POLICY.backoff_delay(2, "T01", 1)
    d_big = POLICY.backoff_delay(20, "T01", 1)
    assert d0 < d1 < d2
    assert d_big <= default_config().link.backoff_max_s
    # 确定性抖动：同输入重放一致，不同车/尝试可有不同抖动。
    assert POLICY.backoff_delay(3, "T01", 7) == POLICY.backoff_delay(3, "T01", 7)


def test_window_aggregate_counts_good():
    agg = POLICY.window([(0.0, 1.0), (1.0, 0.2), (2.0, 0.9)])
    assert agg.samples == 3
    assert agg.good == 2
    assert 0.6 < agg.score < 0.8
    assert POLICY.window([]) == WindowAggregate(score=0.0, samples=0, good=0)
