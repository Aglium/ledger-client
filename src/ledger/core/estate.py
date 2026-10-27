"""Which evidence makes a repository a party to a contract.

The index records every place a contract name was found. That is the right
thing for it to record and the wrong thing to answer questions from: a third
of the shared-contract evidence on a real estate of 172 repositories came from
test files and mocked third-party APIs, and 39% of the contracts that looked
shared were shared only because two repositories' tests named the same string.

Measured, on the estate this was written against:

    shared-contract pairs (repo x contract)              1623
      only test-double evidence                           546   33%
    contracts "shared" only because tests name them    268/696   39%
    contract names that are test placeholders              92

`/api/v1/invoices/some-id` is not a route; it is an argument a test passed.
`/admin/directory/v1/users` is Google's Admin SDK, and it looked like an
in-house contract shared by two services because both mock it under
`docker/google-api-mock/`. Neither belongs in an answer about who owns what.

The policy lives here rather than in the pipeline because the index must stay
a faithful record of what was found and where. What counts as ownership is a
judgement that changes; re-syncing 172 repositories because it changed is not
acceptable, so it is applied when edges are read.
"""

from __future__ import annotations

import re

#: Somebody else's code, copied into the tree. Whatever contract it names
#: belongs to its vendor: `node_modules/@stripe/...` does not make this
#: repository a party to Stripe's API, and a directory mocking an external
#: service (`docker/google-api-mock/`) is that service's contract, not ours.
_VENDORED = re.compile(
    r"(^|/)(node_modules|vendor|vendors|third[_-]?party|thirdparty|site-packages|Pods"
    r"|bower_components|\.venv|venv|\.yarn|external|externals)(/|$)"
    r"|(^|/)[a-z0-9][a-z0-9._-]*[-_]mocks?(/|$)",
    re.I,
)

#: This repository's own test doubles. Real evidence of the *shape* of a
#: contract -- a response-body assertion is often the only place a dynamically
#: built route appears as a literal -- and no evidence at all of ownership.
#: A service does not publish a route because its tests call it.
_TEST_DOUBLE = re.compile(
    r"(^|/)(__mocks__|__tests__|mock|mocks|stub|stubs|fixture|fixtures|testdata"
    r"|test|tests|spec|specs|e2e|cypress|playwright|features|perf|benchmarks?)(/|$)"
    r"|(^|/)(test|conftest)[_.][^/]*$"
    r"|[_.](test|spec)\.[a-z]+$"
    r"|_test\.[a-z]+$"
    # A double named in the filename rather than the directory. Real:
    # `perf/vendor_stub.py` served `/warehouses` as a party to it.
    r"|[_.-](stub|stubs|mock|mocks|fake|fakes|dummy|double)\.[a-z]+$"
    r"|(^|/)(stub|mock|fake)[_.-][^/]*$",
    re.I,
)

#: A stand-in a test passed as an argument, which the discovery pass then read
#: as part of the name. Every one of these came off a real estate:
#: `/api/v1/invoices/some-id/documents`, `/api/v1/teams/nonexistent-id/members`,
#: `/api/v1/jobs/nonexistent`, `/api/v1/asset-hooks/fake-id`.
#:
#: A real placeholder is written `{id}` or `:id` and is deliberately NOT
#: matched here -- that is a route parameter and the route is real.
_PLACEHOLDER = re.compile(
    r"(^|[-/_])(some|any|no|non|none|nonexistent|non-existent|missing|unknown|invalid"
    r"|dummy|fake|bogus|foo|bar|baz|qux|xxx|todo|changeme|placeholder)"
    r"([-_](id|uuid|key|slug|name|ref))?([-/_]|$)",
    re.I,
)

#: How an edge counts. `own` is this repository's own source, `weak` is its
#: tests, `none` is not its contract at all.
OWN, WEAK, NONE = "own", "weak", "none"


def vendored(path: str | None) -> bool:
    return bool(_VENDORED.search(path or ""))


def is_test_double(path: str | None) -> bool:
    return bool(_TEST_DOUBLE.search(path or ""))


#: Where a change's tests live, for deciding what a change TOUCHES. Wider than
#: `_TEST_DOUBLE` in the directory names it accepts (`tests_functional/`,
#: `integration-tests/`, `e2e-tests/`) and in the file names (`FooTest.php`,
#: `foo_test.go`), narrower in that it leaves out `features/` and `perf/`,
#: which are source directories as often as test ones.
#:
#: Measured on a week of labelled briefs: seven noise verdicts came from names
#: that only a test file contained -- a seed value read as a topic, a column in
#: a functional test's schema dump read as a shared table.
_TEST_PATH = re.compile(
    r"(^|/)(__tests__|__mocks__|mocks?|stubs?|tests?|specs?|e2e|fixtures?|testdata|test-data"
    r"|cypress|playwright)"
    r"([_-][\w-]*)?/"
    r"|(^|/)[\w-]+[_-](tests?|specs?)/"
    r"|(^|/)(test|conftest)[_.][^/]*$"
    r"|[_.-](test|spec)s?\.[a-z]+$"
    r"|[_.-](stub|stubs|mock|mocks|fake|fakes)\.[a-z]+$",
    re.I,
)
#: `WizzairClientTest.php`, `OrderServiceTests.cs`: the class-name convention.
_TEST_CLASS = re.compile(r"[a-z0-9]Tests?\.(php|java|kt|kts|cs|scala|swift|groovy)$")


def is_test_path(path: str | None) -> bool:
    """Is this file part of a change's tests rather than what it ships?"""
    p = path or ""
    return bool(_TEST_PATH.search(p) or _TEST_CLASS.search(p))


def placeholder(name: str | None) -> bool:
    """Is this name a test's argument rather than a contract?

    Only ever asked of a path-shaped name. A topic or table called
    `invalid_login_attempt` is a perfectly good contract, and a word like
    `test` or `none` inside one is not a placeholder -- it is a noun.
    """
    n = name or ""
    if not n.startswith("/"):
        return False
    return bool(_PLACEHOLDER.search(n))


def weight(name: str | None, path: str | None) -> str:
    """What one piece of evidence proves about this repository and contract."""
    if placeholder(name) or vendored(path):
        return NONE
    return WEAK if is_test_double(path) else OWN


def strength(name: str | None, paths) -> str:
    """The best any of these pieces of evidence proves.

    A repository with the route in `app/views.py` and again in
    `tests/test_views.py` owns it; the test does not dilute the source.
    """
    best = NONE
    for p in paths:
        w = weight(name, p)
        if w == OWN:
            return OWN
        if w == WEAK:
            best = WEAK
    return best


#: Below this, two repositories with related names are two repositories.
#: `acme-people` and `acme-people-legacy` shared 191 of 223 contracts;
#: `acme-billing` and `acme-payments` shared 104 of 184 and are two real
#: services, so the line sits between them and not in the middle.
FORK_OVERLAP = 0.75

#: An identical contract set is only evidence when there is a set to compare.
#: Two repositories that each name three contracts match by accident.
COPY_FLOOR = 5

_LIFECYCLE = re.compile(
    r"[-_](legacy|old|older|oldest|new|next|v\d+|\d+|fork|copy|clone|mirror|dup|duplicate"
    r"|deprecated|archive|archived|backup|bak|orig|original|rewrite|migrated|tmp|temp)$",
    re.I,
)


def same_lineage(a: str, b: str) -> bool:
    """Is one of these names the other plus a lifecycle suffix?

    The cheap half of `duplicates`, for callers that have two repository
    names and not their contract sets. Enough to stop `acme-people` and
    `acme-people-legacy` being reported as two independent writers of one
    table, and it cannot be wrong in a way that matters: two services where
    one is named after the other with `-legacy` on the end are a fork.
    """
    return a != b and (_LIFECYCLE.sub("", a) == b or _LIFECYCLE.sub("", b) == a)


def _overlap(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def duplicates(contracts_by_repo: dict[str, set]) -> dict[str, str]:
    """Repositories that are copies of each other: `{copy: principal}`.

    Two independent tests, because each finds a case the other misses on a
    real estate:

      a lifecycle suffix over the other's name, plus a contract set that
      mostly agrees -- `acme-people-legacy` over `acme-people`, 191 of
      223 contracts shared;

      contract sets that are *identical* and not trivially small --
      `apex-data-ingest` and `acme-data-ingest`, 25 contracts each and
      every one the same. Their names are not related at all, so the suffix
      test cannot see them, and 25-for-25 is not a coincidence.

    What is deliberately NOT used: a shared root commit, which is the signal
    this should rest on and does not work. Checked on all 175 mirrors of the
    estate it was written against -- every candidate pair had a different
    root, the confirmed `acme-people` / `acme-people-legacy` fork
    included. A legacy split here is a copy of the files and a fresh
    `git init`, not a clone, so there is no shared history to find.

    The cost of having no such evidence is a real miss, recorded rather than
    guessed at: `apex-data-ingest` and `acme-data-ingest` share 25 of
    29 contracts and no name relation, and are almost certainly one service
    twice. Catching it would mean trusting set overlap alone between
    unrelated names, and the threshold that caught it (0.86) would also
    collapse `acme-api-gateway` and `acme-api-python` at 0.76, which may
    well be a gateway and the service behind it. A rule tuned to split two
    cases by a tenth is not a rule.

    Naming the principal: the unsuffixed name wins, because that is what a
    lifecycle suffix means. Where neither name is suffixed there is nothing to
    go on, so the larger contract set wins and ties break on the name -- the
    point is to count a copy once and deterministically, not to guess which
    twin is alive. Callers that answer ownership questions should say both.
    """
    names = sorted(contracts_by_repo)
    found: dict[str, str] = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            A, B = contracts_by_repo[a], contracts_by_repo[b]
            if not A or not B:
                continue
            base_a, base_b = _LIFECYCLE.sub("", a), _LIFECYCLE.sub("", b)
            if base_b == a and b != base_b:
                copy, principal = b, a
            elif base_a == b and a != base_a:
                copy, principal = a, b
            elif A == B and len(A) >= COPY_FLOOR:
                copy, principal = ((b, a) if (len(A), b) >= (len(B), a) else (a, b))
            else:
                continue
            if _overlap(A, B) < FORK_OVERLAP:
                continue
            found[copy] = principal
    # A chain -- x copied from y copied from z -- resolves to the end of it.
    for copy in list(found):
        seen = {copy}
        at = found[copy]
        while at in found and at not in seen:
            seen.add(at)
            at = found[at]
        found[copy] = at
    return {c: p for c, p in found.items() if c != p}
