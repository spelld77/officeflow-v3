from officeflow.application.resource_policy import (
    IdleBackoff,
    ResourcePolicy,
    ResourceSnapshot,
    ResourceState,
)


def sample(at, ratio=None, low=False):
    return ResourceSnapshot(
        at,
        commit_bytes=round(ratio * 1000) if ratio is not None else None,
        commit_limit=1000 if ratio is not None else None,
        low_memory=low,
    )


def test_pressure_hysteresis_unknown_expiry_and_recovery():
    policy = ResourcePolicy()
    assert policy.observe(sample(0, 0.5)) == ResourceState.NORMAL
    assert policy.observe(sample(30, 0.93)) == ResourceState.NORMAL
    assert policy.observe(sample(60, 0.93)) == ResourceState.PRESSURE
    assert policy.observe(sample(90, 0.8)) == ResourceState.PRESSURE
    assert policy.observe(sample(120, 0.8)) == ResourceState.PRESSURE
    assert policy.observe(sample(150, 0.8)) == ResourceState.NORMAL
    assert policy.observe(sample(180, 0.98)) == ResourceState.PRESSURE
    assert policy.observe(sample(210)) == ResourceState.PRESSURE
    assert policy.observe(sample(300)) == ResourceState.UNKNOWN
    assert policy.observe(sample(330, 0.5)) == ResourceState.NORMAL


def test_low_signal_unknown_and_invalid_ratio_are_not_zero_usage():
    policy = ResourcePolicy()
    assert policy.observe(sample(0)) == ResourceState.UNKNOWN
    assert policy.observe(sample(30, 0.5, True)) == ResourceState.NORMAL
    assert policy.observe(sample(60, 0.5, True)) == ResourceState.PRESSURE
    assert sample(1, 1.2).commit_ratio is None
    assert sample(1, -0.1).commit_ratio is None


def test_idle_backoff_is_bounded_and_progress_resets():
    backoff = IdleBackoff()
    assert [backoff.next_seconds(progressed=False) for _ in range(8)] == [
        1,
        2,
        5,
        15,
        30,
        60,
        60,
        60,
    ]
    assert backoff.next_seconds(progressed=True) == 1
    assert backoff.next_seconds(progressed=False, due_in=3600) == 1
    for _ in range(10):
        backoff.next_seconds(progressed=False)
    assert backoff.next_seconds(progressed=False, due_in=4) == 4
