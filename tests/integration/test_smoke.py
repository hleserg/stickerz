"""Integration smoke test placeholder."""

from __future__ import annotations

import pytest

from projectname import __version__


@pytest.mark.integration
def test_version_present() -> None:
    assert __version__
