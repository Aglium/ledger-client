"""The client must not contain, or need, the server.

The two are separate products under separate licences. That only holds if the
client has no way to reach the server's code: not an import of it, and not an
import of the database libraries only the server depends on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ledger

FORBIDDEN = ("ledger_server", "sqlalchemy", "psycopg", "alembic")


def imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_the_client_imports_nothing_of_the_server():
    root = Path(ledger.__file__).parent
    found = sorted(
        f"{p.relative_to(root)}: {name}"
        for p in root.rglob("*.py") for name in imports(p)
        if name.split(".")[0] in FORBIDDEN)
    assert found == []


def test_the_client_declares_no_database_dependency():
    from importlib.metadata import requires
    declared = " ".join(requires("aglium-ledger") or []).lower()
    assert not any(name in declared for name in FORBIDDEN)


def test_the_client_ships_its_own_licence_not_the_servers():
    """The client is the part anyone may use. If it carried the repository's
    all-rights-reserved notice instead, nobody outside Aglium could."""
    text = (Path(__file__).resolve().parents[1] / "LICENSE").read_text()
    assert "PolyForm Shield License 1.0.0" in text and "Required Notice: Copyright" in text
    assert "All rights reserved" not in text
