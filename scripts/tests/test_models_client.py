"""Offline self-check for the non-trivial bit of models_client (truncation).

Run: python -m scripts.tests.test_models_client   (no network, no MODELS_TOKEN needed)
"""

from scripts.models_client import truncate_diff


def test_truncate():
    assert truncate_diff("abc", 10) == "abc"  # under cap → untouched
    out = truncate_diff("x" * 100, 10)
    assert out.startswith("x" * 10)
    assert "truncated" in out  # marker present so the model knows it's partial


if __name__ == "__main__":
    test_truncate()
    print("ok")
