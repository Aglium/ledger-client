import pytest


@pytest.fixture(autouse=True)
def _nothing_leaves_the_test(tmp_path, monkeypatch):
    """Tests must not touch the developer's own scan cache or config directory."""
    monkeypatch.setenv("LEDGER_CACHE_DIR", str(tmp_path / "scan-cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
