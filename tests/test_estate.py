"""Which evidence makes a repository a party to a contract.

Every case here came off the real estate the rule was calibrated against. Pure
functions, so the cost of covering them is nothing and the thing they protect
is every ownership answer the product gives.
"""

from __future__ import annotations

import pytest

from ledger.core.estate import is_test_path
from ledger.core.estate import (
    NONE, OWN, WEAK, duplicates, placeholder, same_lineage, strength, is_test_double,
    vendored, weight,
)


class TestVendoredCodeIsNotOurs:
    @pytest.mark.parametrize("path", [
        "node_modules/@stripe/stripe-js/dist/api.d.ts",
        "vendor/guzzlehttp/guzzle/src/Client.php",
        "third_party/googleapis/admin.json",
        "docker/google-api-mock/routes.js",
        "tools/stripe_mock/handlers.py",
        ".venv/lib/python3.14/site-packages/boto3/client.py",
    ])
    def test_it_is_vendored(self, path):
        assert vendored(path)

    @pytest.mark.parametrize("path", [
        "app/services/google.py", "src/api/client.ts", "docker/compose.yaml",
        "app/mockingbird/service.py",          # a word that starts with mock
    ])
    def test_our_own_source_is_not(self, path):
        assert not vendored(path)

    def test_a_mocked_third_party_api_is_that_party_s_contract(self):
        """`/admin/directory/v1/users` looked like a shared Acme contract
        because two repositories both mock Google's Admin SDK."""
        assert weight("/admin/directory/v1/users", "docker/google-api-mock/admin.js") == NONE


class TestTestsProveShapeNotOwnership:
    @pytest.mark.parametrize("path", [
        "tests/test_invoices_list.py",
        "libs/acme_sdk/tests/test_security_headers_middleware.py",
        "src/mocks/handlers/config.ts",
        "__mocks__/api.ts",
        "cypress/e2e/login.cy.ts",
        "app/invoice/invoice_test.go",
        "src/api/client.spec.ts",
        "conftest.py",
        "perf/vendor_stub.py",
        "app/clients/billing_mock.py",
        "src/stub_server.js",
    ])
    def test_it_is_a_test_double(self, path):
        assert is_test_double(path)

    @pytest.mark.parametrize("path", [
        "app/views.py", "src/api/invoices.ts", "app/Controller/InvoiceController.php",
        "src/latest/index.ts",                  # contains "test" as letters
        "app/contests/views.py",
        "app/stubborn.py",
        "app/performance.py",
    ])
    def test_real_source_is_not(self, path):
        assert not is_test_double(path)

    def test_a_route_its_tests_call_is_weak_not_absent(self):
        """A response-body assertion is often the only place a dynamically
        built route appears as a literal. Good evidence of the shape, none at
        all of who serves it."""
        assert weight("/api/v1/invoices", "tests/test_invoices_list.py") == WEAK

    def test_source_is_not_diluted_by_a_test_naming_it_too(self):
        assert strength("/api/v1/invoices", ["tests/test_invoices.py", "app/views.py"]) == OWN

    def test_tests_alone_stay_weak(self):
        assert strength("/api/v1/invoices", ["tests/test_invoices.py", "tests/test_other.py"]) == WEAK


class TestPlaceholdersAreNotContracts:
    @pytest.mark.parametrize("name", [
        "/api/v1/invoices/some-id",
        "/api/v1/invoices/some-id/documents/doc-id/url",
        "/api/v1/teams/nonexistent-id/members",
        "/api/v1/jobs/nonexistent",
        "/api/v1/asset-hooks/fake-id",
        "/api/v1/assets/nonexistent",
    ])
    def test_it_is_a_placeholder(self, name):
        assert placeholder(name) and weight(name, "app/views.py") == NONE

    @pytest.mark.parametrize("name", [
        "/api/v1/invoices/{id}/documents",         # a route parameter is a real route
        "/api/v1/invoices/:invoice_id",
        "/api/v1/email-templates/{id}/test-send",
        "/api/v1/invoices",
    ])
    def test_a_real_route_is_not(self, name):
        assert not placeholder(name)

    def test_only_paths_are_judged(self):
        """A table or topic may perfectly well be named after one of these
        words. `invalid_login_attempt` is a table."""
        assert not placeholder("invalid_login_attempt")
        assert not placeholder("invoice.test_mode_changed.v1")


class TestCopiesCountOnce:
    def test_a_lifecycle_suffix_over_a_matching_contract_set(self):
        """Real: `acme-people-legacy` shared 191 of its 223 contracts with
        `acme-people`."""
        shared = {f"c{i}" for i in range(191)}
        got = duplicates({"acme-people": set(shared),
                          "acme-people-legacy": shared | {f"x{i}" for i in range(32)}})
        assert got == {"acme-people-legacy": "acme-people"}

    def test_identical_sets_with_unrelated_names(self):
        """Real: `apex-data-ingest` and `acme-data-ingest`, 25 contracts
        each and every one the same. No name relation to find."""
        same = {f"c{i}" for i in range(25)}
        got = duplicates({"apex-data-ingest": set(same), "acme-data-ingest": set(same)})
        assert got in ({"apex-data-ingest": "acme-data-ingest"},
                       {"acme-data-ingest": "apex-data-ingest"})

    def test_it_picks_the_same_one_every_time(self):
        same = {f"c{i}" for i in range(25)}
        runs = {tuple(sorted(duplicates({"a-ingest": set(same), "b-ingest": set(same)}).items()))
                for _ in range(5)}
        assert len(runs) == 1

    def test_two_real_services_that_merely_overlap_are_two_services(self):
        """Real: `acme-billing` and `acme-payments` share 104 of 184 contracts
        and are not copies of each other."""
        common = {f"c{i}" for i in range(104)}
        assert duplicates({"acme-billing": common | {f"l{i}" for i in range(32)},
                           "acme-payments": common | {f"m{i}" for i in range(48)}}) == {}

    def test_a_suffix_without_the_contracts_to_back_it_is_not_a_copy(self):
        assert duplicates({"svc": {"a", "b", "c", "d", "e", "f"},
                           "svc-v2": {"g", "h", "i", "j", "k", "l"}}) == {}

    def test_two_tiny_identical_sets_are_a_coincidence(self):
        assert duplicates({"one": {"a", "b"}, "two": {"a", "b"}}) == {}

    def test_a_chain_resolves_to_its_end(self):
        s = {f"c{i}" for i in range(10)}
        got = duplicates({"svc": set(s), "svc-old": set(s) | {"x"}, "svc-old-archive": set(s) | {"y"}})
        assert set(got.values()) == {"svc"}

    def test_nothing_to_compare_is_not_a_copy(self):
        assert duplicates({"a": set(), "b": set()}) == {}


class TestTheCheapLineageTest:
    """`same_lineage` is for callers holding two names and no contract sets --
    the divergence rule, which must not report a repository and its fork as
    two writers of one table."""

    @pytest.mark.parametrize("a,b", [
        ("acme-people", "acme-people-legacy"),
        ("svc-v2", "svc"),
        ("orders_old", "orders"),
        ("api-archive", "api"),
    ])
    def test_related(self, a, b):
        assert same_lineage(a, b) and same_lineage(b, a)

    @pytest.mark.parametrize("a,b", [
        ("acme-billing", "acme-payments"),
        ("apex-data-ingest", "acme-data-ingest"),
        ("acme-api-gateway", "acme-api-python"),
        ("orders", "orders"),
    ])
    def test_unrelated(self, a, b):
        assert not same_lineage(a, b)


class TestIsTestPath:
    """What a change's tests are, for deciding what the change touches."""

    @pytest.mark.parametrize("path", [
        "tests_functional/schema.sql",
        "tests_functional/api/test_robot_claim_entry_facts.py",
        "tests/Unit/AppBundle/Service/WizzairClientTest.php",
        "integration-tests/orders.go",
        "e2e-tests/login.ts",
        "pkg/orders/orders_test.go",
        "src/features/x.test.ts",
        "conftest.py",
        "src/__mocks__/api.ts",
        "perf/vendor_stub.py",
    ])
    def test_tests(self, path):
        assert is_test_path(path)

    @pytest.mark.parametrize("path", [
        "src/AppBundle/Service/Wizzair/WizzairClient.php",
        "src/features/new-version/audience.ts",
        "app/testimonials/view.py",
        "app/config/parameters.yml.dist",
        "contest/rules.py",
        "src/latest/x.py",
    ])
    def test_shipped_code(self, path):
        assert not is_test_path(path)
