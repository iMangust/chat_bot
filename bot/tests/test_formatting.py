"""Юнит-тесты формул баланса (без БД)."""
from __future__ import annotations

from app.utils.formatting import apply_xp, clamp, progress_bar, xp_needed_for_level


def test_xp_curve_grows():
    assert xp_needed_for_level(1) == 50
    assert xp_needed_for_level(2) > xp_needed_for_level(1)
    assert xp_needed_for_level(10) > xp_needed_for_level(5)


def test_apply_xp_no_levelup():
    level, xp, new = apply_xp(1, 10, 20)
    assert (level, xp, new) == (1, 30, [])


def test_apply_xp_exact_boundary():
    level, xp, new = apply_xp(1, 0, 50)
    assert level == 2 and xp == 0 and new == [2]


def test_apply_xp_multilevel_overflow():
    level, xp, new = apply_xp(1, 0, 120)
    assert level == 2 and xp == 70 and new == [2]
    level, xp, new = apply_xp(1, 0, 200)
    assert level == 3 and xp == 9 and new == [2, 3]


def test_progress_bar_bounds():
    assert progress_bar(0, 100) == "▱" * 10
    assert progress_bar(100, 100) == "▰" * 10
    assert progress_bar(500, 100) == "▰" * 10
    assert progress_bar(-5, 100) == "▱" * 10


def test_clamp():
    assert clamp(150) == 100.0
    assert clamp(-10) == 0.0
    assert clamp(42.5) == 42.5
