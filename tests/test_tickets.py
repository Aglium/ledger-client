import pytest

from ledger.core.tickets import extract_tickets, validate_keys

KEYS = validate_keys(["ABC", "XYZ"])


class TestExtract:
    def test_realistic_branch_names(self):
        # Shaped like real disciplined branch naming: {type}/{key}-{n}-{description}.
        cases = {
            "feat/abc-321-export-attribution-and-billing-address": ["ABC-321"],
            "fix/abc-321-bind-documents-by-core-customer-id": ["ABC-321"],
            "test/abc-321-pin-the-intake-contract-against-the-consumer": ["ABC-321"],
            "feat/abc-234-late-signatures-on-the-intake-topic": ["ABC-234"],
            "feat/abc-234-announce-late-signatures": ["ABC-234"],
            "XYZ-8635 Stub the dispatcher the removal test drives through": ["XYZ-8635"],
            "XYZ-8649 Retire the two notes the LEAD merge falsified": ["XYZ-8649"],
        }
        for text, expected in cases.items():
            assert extract_tickets(text, KEYS) == expected, text

    def test_real_branches_without_tickets(self):
        for text in ("feat/config-spec-parity", "feat/config-menu", "stage", "main", "master"):
            assert extract_tickets(text, KEYS) == []

    def test_zero_is_not_a_ticket(self):
        """Reversed, on evidence. This asserted the opposite -- "XYZ-0 appears
        on four repos in the audit. Zero is a number." -- reading that spread
        as signal. It is the reverse: the spread is *why* it is worthless.

        Once commit messages were read as well as branch names, `SMT-0` turned
        up on 94 open changes across 8 repositories, because it is the house
        convention for work that has no ticket (`SMT-0 Fixed merge
        conflicts`). Joining on it links everything without a ticket to
        everything else without a ticket.
        """
        assert extract_tickets("chore/XYZ-0-bump", KEYS) == []

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("ABC-3217", ["ABC-3217"]),      # must NOT truncate to ABC-321
            ("xabc-321", []),                # must NOT match mid-token
            ("ABC-321a", []),                # trailing alnum invalidates
            ("abc_321", ["ABC-321"]),        # underscore separator tolerated
            ("ABC321", ["ABC-321"]),         # no separator tolerated
        ],
    )
    def test_boundaries(self, text, expected):
        assert extract_tickets(text, KEYS) == expected

    def test_multiple_and_dedupe_order_preserved(self):
        t = "feat/abc-234-and-abc-321-and-abc-234-again"
        assert extract_tickets(t, KEYS) == ["ABC-234", "ABC-321"]

    def test_case_insensitive_normalises_up(self):
        assert extract_tickets("Abc-321", KEYS) == ["ABC-321"]

    def test_shadow_paths(self):
        assert extract_tickets(None, KEYS) == []
        assert extract_tickets("", KEYS) == []

    def test_unconfigured_key_is_not_matched(self):
        # Portability: core/ knows no project keys of its own.
        assert extract_tickets("JIRA-99", KEYS) == []


class TestValidateKeys:
    def test_normalises_and_dedupes(self):
        assert validate_keys([" abc ", "XYZ", "abc"]) == ("ABC", "XYZ")

    @pytest.mark.parametrize("bad", ["", "1AB", "A", "has-dash", "way_too_long_a_project_key_x"])
    def test_rejects_malformed_loudly(self, bad):
        with pytest.raises(ValueError):
            validate_keys([bad])

    def test_rejects_empty_config(self):
        with pytest.raises(ValueError, match="no project keys"):
            validate_keys([])


class TestZeroIsNotATicket:
    """`SMT-0` is a house convention for work that has no ticket --
    `SMT-0-remove-old-configuration`, `SMT-0 Fixed merge conflicts`. It was on
    94 open changes across 8 repositories, so joining on it linked everything
    without a ticket to everything else without a ticket."""

    def test_zero_is_dropped(self):
        assert extract_tickets("SMT-0 fixed merge conflicts", ("SMT",)) == []
        assert extract_tickets("SMT-0-remove-old-configuration", ("SMT",)) == []
        assert extract_tickets("smt_0 and smt-000", ("SMT",)) == []

    def test_a_real_ticket_beside_it_still_matches(self):
        assert extract_tickets("SMT-0 then SMT-8544", ("SMT",)) == ["SMT-8544"]

    def test_leading_zeros_on_a_real_number_are_fine(self):
        assert extract_tickets("SMT-0042", ("SMT",)) == ["SMT-42"]
