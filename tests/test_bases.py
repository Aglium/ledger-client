import pytest

from ledger.core.bases import choose_base


def test_picks_the_base_with_fewest_unique_commits():
    """The real case: 415 commits beyond main, none beyond stage."""
    assert choose_base({"main": 415, "stage": 0}, "main") == ("stage", 0)


def test_ties_go_to_the_default_branch():
    assert choose_base({"main": 3, "stage": 3}, "main") == ("main", 3)


def test_ties_among_non_default_bases_are_deterministic():
    assert choose_base({"main": 5, "stage": 2, "develop": 2}, "main") == ("develop", 2)
    assert choose_base({"main": 5, "develop": 2, "stage": 2}, "main") == ("develop", 2)


def test_default_alone():
    assert choose_base({"master": 7}, "master") == ("master", 7)


def test_no_candidates_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        choose_base({}, "main")
