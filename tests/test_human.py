"""Unit tests for human-like input simulation — Bezier curves, typing delays.

These tests exercise the pure-math helpers that power Specter's stealth
input layer.  No browser needed.
"""

import math
import random

import pytest


# ── Inline implementation (matches the planned specter.stealth.human API) ──


def _bezier_point(t: float, p0: tuple[float, float], p1: tuple[float, float],
                  p2: tuple[float, float], p3: tuple[float, float]
                  ) -> tuple[float, float]:
    """Evaluate a cubic Bezier at parameter *t* in [0, 1]."""
    u = 1 - t
    x = (u**3 * p0[0] + 3 * u**2 * t * p1[0]
         + 3 * u * t**2 * p2[0] + t**3 * p3[0])
    y = (u**3 * p0[1] + 3 * u**2 * t * p1[1]
         + 3 * u * t**2 * p2[1] + t**3 * p3[1])
    return (x, y)


def generate_bezier_curve(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 20,
    *,
    jitter: float = 0.0,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """Generate a human-like mouse path via a cubic Bezier with randomised
    control points.

    Args:
        start: (x, y) origin.
        end:   (x, y) destination.
        steps: number of intermediate points (min 2).
        jitter: pixel noise added to each point.
        seed: optional RNG seed for reproducibility.

    Returns:
        List of (x, y) tuples from *start* to *end*.
    """
    rng = random.Random(seed)
    steps = max(steps, 2)

    dx, dy = end[0] - start[0], end[1] - start[1]
    dist = math.hypot(dx, dy)
    spread = max(dist * 0.3, 20)

    cp1 = (start[0] + dx * 0.25 + rng.uniform(-spread, spread),
            start[1] + dy * 0.25 + rng.uniform(-spread, spread))
    cp2 = (start[0] + dx * 0.75 + rng.uniform(-spread, spread),
            start[1] + dy * 0.75 + rng.uniform(-spread, spread))

    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        x, y = _bezier_point(t, start, cp1, cp2, end)
        if jitter > 0:
            x += rng.uniform(-jitter, jitter)
            y += rng.uniform(-jitter, jitter)
        points.append((x, y))

    return points


def typing_delay(char: str, *, wpm: float = 80,
                 variance: float = 0.4,
                 seed: int | None = None) -> float:
    """Compute a human-like inter-keystroke delay in seconds.

    Args:
        char: the character being typed.
        wpm:  target words-per-minute (average).
        variance: relative jitter (0 = uniform, 1 = very noisy).
        seed: optional RNG seed.

    Returns:
        Delay in seconds before this keystroke.
    """
    rng = random.Random(seed)
    base = 60.0 / (wpm * 5)  # avg seconds per character

    # longer pauses after punctuation / space
    if char in ".!?\n":
        base *= rng.uniform(1.8, 3.0)
    elif char == " ":
        base *= rng.uniform(1.0, 1.6)
    elif char.isupper():
        base *= rng.uniform(1.1, 1.5)

    jitter = rng.gauss(0, base * variance)
    return max(0.01, base + jitter)


# ── Bezier curve tests ────────────────────────────────────────────


class TestBezierPoint:
    def test_endpoints(self):
        p0, p3 = (0.0, 0.0), (100.0, 100.0)
        cp1, cp2 = (25.0, 75.0), (75.0, 25.0)
        assert _bezier_point(0.0, p0, cp1, cp2, p3) == pytest.approx(p0)
        assert _bezier_point(1.0, p0, cp1, cp2, p3) == pytest.approx(p3)

    def test_midpoint_is_between(self):
        p0, p3 = (0.0, 0.0), (200.0, 0.0)
        cp1, cp2 = (50.0, 0.0), (150.0, 0.0)
        mx, my = _bezier_point(0.5, p0, cp1, cp2, p3)
        assert 0 <= mx <= 200
        assert my == pytest.approx(0.0)

    def test_straight_line(self):
        p0, p3 = (0.0, 0.0), (100.0, 0.0)
        cp1 = (33.3, 0.0)
        cp2 = (66.6, 0.0)
        for t in [0.0, 0.25, 0.5, 0.75, 1.0]:
            _, y = _bezier_point(t, p0, cp1, cp2, p3)
            assert y == pytest.approx(0.0, abs=0.1)


class TestGenerateBezierCurve:
    def test_starts_at_origin(self):
        pts = generate_bezier_curve((10, 20), (300, 400), steps=10, seed=42)
        assert pts[0] == pytest.approx((10, 20), abs=0.01)

    def test_ends_at_destination(self):
        pts = generate_bezier_curve((10, 20), (300, 400), steps=10, seed=42)
        assert pts[-1] == pytest.approx((300, 400), abs=0.01)

    def test_correct_number_of_points(self):
        pts = generate_bezier_curve((0, 0), (100, 100), steps=25, seed=1)
        assert len(pts) == 26  # steps + 1

    def test_min_steps_clamped(self):
        pts = generate_bezier_curve((0, 0), (100, 100), steps=0, seed=1)
        assert len(pts) >= 3  # at least 2 steps -> 3 points

    def test_deterministic_with_seed(self):
        a = generate_bezier_curve((0, 0), (500, 500), steps=15, seed=99)
        b = generate_bezier_curve((0, 0), (500, 500), steps=15, seed=99)
        assert a == b

    def test_different_seeds_differ(self):
        a = generate_bezier_curve((0, 0), (500, 500), steps=15, seed=1)
        b = generate_bezier_curve((0, 0), (500, 500), steps=15, seed=2)
        assert a != b

    def test_points_move_toward_destination(self):
        start, end = (0, 0), (1000, 0)
        pts = generate_bezier_curve(start, end, steps=50, seed=42)
        xs = [p[0] for p in pts]
        # general trend should be increasing (allow some wobble)
        assert xs[-1] > xs[0]
        assert xs[len(xs) // 2] > xs[0] - 200  # not wildly backwards

    def test_jitter_adds_noise(self):
        no_jitter = generate_bezier_curve((0, 0), (100, 100), steps=10,
                                         jitter=0.0, seed=7)
        with_jitter = generate_bezier_curve((0, 0), (100, 100), steps=10,
                                           jitter=5.0, seed=7)
        # midpoints should differ due to jitter
        diffs = [abs(a[0] - b[0]) + abs(a[1] - b[1])
                 for a, b in zip(no_jitter[1:-1], with_jitter[1:-1])]
        assert any(d > 0.01 for d in diffs)

    def test_short_distance(self):
        pts = generate_bezier_curve((50, 50), (52, 53), steps=5, seed=10)
        assert len(pts) == 6
        assert pts[0] == pytest.approx((50, 50), abs=0.01)
        assert pts[-1] == pytest.approx((52, 53), abs=0.01)

    def test_same_start_and_end(self):
        pts = generate_bezier_curve((100, 100), (100, 100), steps=5, seed=3)
        assert pts[0] == pytest.approx((100, 100), abs=0.01)
        assert pts[-1] == pytest.approx((100, 100), abs=0.01)


# ── Typing delay tests ───────────────────────────────────────────


class TestTypingDelay:
    def test_positive(self):
        d = typing_delay("a", seed=1)
        assert d > 0

    def test_minimum_floor(self):
        # even with extreme variance, delay is at least 10ms
        for i in range(50):
            d = typing_delay("x", wpm=300, variance=1.0, seed=i)
            assert d >= 0.01

    def test_punctuation_slower(self):
        delays_punct = [typing_delay(".", wpm=80, variance=0, seed=i)
                        for i in range(20)]
        delays_alpha = [typing_delay("a", wpm=80, variance=0, seed=i)
                        for i in range(20)]
        assert sum(delays_punct) / len(delays_punct) > \
               sum(delays_alpha) / len(delays_alpha)

    def test_space_slightly_slower(self):
        delays_space = [typing_delay(" ", wpm=80, variance=0, seed=i)
                        for i in range(30)]
        delays_alpha = [typing_delay("k", wpm=80, variance=0, seed=i)
                        for i in range(30)]
        avg_space = sum(delays_space) / len(delays_space)
        avg_alpha = sum(delays_alpha) / len(delays_alpha)
        assert avg_space >= avg_alpha

    def test_uppercase_slower(self):
        delays_upper = [typing_delay("A", wpm=80, variance=0, seed=i)
                        for i in range(30)]
        delays_lower = [typing_delay("a", wpm=80, variance=0, seed=i)
                        for i in range(30)]
        assert sum(delays_upper) / len(delays_upper) >= \
               sum(delays_lower) / len(delays_lower)

    def test_faster_wpm(self):
        slow = typing_delay("a", wpm=40, variance=0, seed=5)
        fast = typing_delay("a", wpm=120, variance=0, seed=5)
        assert fast < slow

    def test_deterministic_with_seed(self):
        a = typing_delay("e", wpm=80, seed=42)
        b = typing_delay("e", wpm=80, seed=42)
        assert a == b

    def test_variance_zero_consistent(self):
        # variance=0 still has RNG for char multipliers, but base is stable
        delays = [typing_delay("m", wpm=80, variance=0, seed=99)
                  for _ in range(5)]
        assert len(set(delays)) == 1  # same seed = same result

    def test_realistic_range(self):
        # at 80 WPM, average delay ~ 0.15s per char
        d = typing_delay("t", wpm=80, variance=0, seed=10)
        assert 0.05 < d < 1.0
