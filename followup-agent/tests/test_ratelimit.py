"""限流单测：只验算法与脱敏规则，不启服务、不连库。

限流是"成本闸门"，出错的两种方式都很贵：
  - 判早了：正常医护被挡在门外（本地回环豁免就是为了这个）；
  - 判晚了：一个脚本把一天额度刷完。
所以这里把边界钉住：正好第 N 次放行、第 N+1 次拒绝、窗口滑过后恢复。
"""

from __future__ import annotations

import time

from app.core.ratelimit import SlidingWindow


def test_allows_up_to_limit() -> None:
    w = SlidingWindow()
    for _ in range(5):
        ok, _ = w.check("k", limit=5, window=60)
        assert ok is True


def test_blocks_over_limit() -> None:
    w = SlidingWindow()
    for _ in range(5):
        w.check("k", limit=5, window=60)
    ok, retry = w.check("k", limit=5, window=60)
    assert ok is False
    assert 1 <= retry <= 60


def test_keys_are_isolated() -> None:
    w = SlidingWindow()
    for _ in range(3):
        w.check("user-a", limit=3, window=60)
    assert w.check("user-a", limit=3, window=60)[0] is False
    assert w.check("user-b", limit=3, window=60)[0] is True


def test_window_slides() -> None:
    """窗口滑过后应当恢复，否则被误挡的人要等到进程重启。"""
    w = SlidingWindow()
    for _ in range(2):
        w.check("k", limit=2, window=0.15)
    assert w.check("k", limit=2, window=0.15)[0] is False
    time.sleep(0.2)
    assert w.check("k", limit=2, window=0.15)[0] is True


def test_reset_clears_all() -> None:
    w = SlidingWindow()
    w.check("k", limit=1, window=60)
    assert w.check("k", limit=1, window=60)[0] is False
    w.reset()
    assert w.check("k", limit=1, window=60)[0] is True
