"""Tests for the periodic housekeeping pass (GC + prune + FSM sweep + disk alert)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sticker_service.config import Settings
from sticker_service.maintenance import loop as maintenance


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.rejected_calls: list[int] = []

    async def gc_stale_drafts(self, *, older_than_days: int) -> int:
        self.calls.append(older_than_days)
        return 2

    async def gc_rejected_sheets(self, *, older_than_days: int) -> int:
        self.rejected_calls.append(older_than_days)
        return 4


class _FakeDb:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.config: dict[str, str] = {}

    async def prune_events(self, *, older_than_days: int, keep_events: Any) -> int:
        self.calls.append({"days": older_than_days, "keep": tuple(keep_events)})
        return 5

    async def get_config(self, key: str, default: str = "") -> str:
        return self.config.get(key, default)

    async def set_config(self, key: str, value: str) -> None:
        self.config[key] = value


class _FakeStorage:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def sweep_stale(self, *, older_than_days: int) -> int:
        self.calls.append(older_than_days)
        return 3


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(data_dir=tmp_path, _env_file=None, **overrides)  # type: ignore[call-arg]


async def _run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, used_pct: int
) -> tuple[maintenance.MaintenanceReport, list[str]]:
    monkeypatch.setattr(maintenance, "disk_used_pct", lambda _s: used_pct)
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    report = await maintenance.run_once(
        orchestrator=_FakeOrchestrator(),  # type: ignore[arg-type]
        db=_FakeDb(),  # type: ignore[arg-type]
        storage=_FakeStorage(),  # type: ignore[arg-type]
        notify=notify,
        settings=settings,
    )
    return report, sent


async def test_run_once_sweeps_everything_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, sent = await _run(_settings(tmp_path), monkeypatch, used_pct=10)
    assert (report.drafts_removed, report.rejected_removed) == (2, 4)
    assert (report.events_pruned, report.fsm_rows_swept) == (5, 3)
    assert report.disk_used_pct == 10
    assert not report.disk_alerted
    assert sent == []  # well under the threshold — no alert noise


async def test_run_once_alerts_admins_when_disk_nearly_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, sent = await _run(_settings(tmp_path), monkeypatch, used_pct=85)
    assert report.disk_alerted
    assert len(sent) == 1 and "85%" in sent[0]


async def test_disk_alert_is_edge_triggered_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The alert fires once on crossing the threshold and stays quiet on later
    # passes while still over it — only re-arming after disk drops back under.
    monkeypatch.setattr(maintenance, "disk_used_pct", lambda _s: 85)
    settings, db = _settings(tmp_path), _FakeDb()
    storage = _FakeStorage()
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    async def _pass(pct_db: _FakeDb) -> bool:
        rep = await maintenance.run_once(
            orchestrator=_FakeOrchestrator(),  # type: ignore[arg-type]
            db=pct_db,  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
            notify=notify,
            settings=settings,
        )
        return rep.disk_alerted

    assert await _pass(db) is True  # crossing → alert
    assert await _pass(db) is False  # still over → silent
    assert len(sent) == 1
    monkeypatch.setattr(maintenance, "disk_used_pct", lambda _s: 10)
    assert await _pass(db) is False  # dropped under → re-arm, no alert
    monkeypatch.setattr(maintenance, "disk_used_pct", lambda _s: 85)
    assert await _pass(db) is True  # crosses again → alert again
    assert len(sent) == 2


async def test_disk_alert_disabled_by_zero_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, disk_alert_threshold_pct=0)
    report, sent = await _run(settings, monkeypatch, used_pct=99)
    assert not report.disk_alerted
    assert sent == []


async def test_run_once_passes_retention_settings_through(tmp_path: Path) -> None:
    orchestrator, db, storage = _FakeOrchestrator(), _FakeDb(), _FakeStorage()

    async def notify(_: str) -> None:  # pragma: no cover - not triggered here
        pass

    settings = _settings(
        tmp_path,
        draft_retention_days=7,
        events_retention_days=90,
        fsm_retention_days=3,
        rejected_retention_days=5,
    )
    await maintenance.run_once(
        orchestrator=orchestrator,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        notify=notify,
        settings=settings,
    )
    assert orchestrator.calls == [7]
    assert orchestrator.rejected_calls == [5]
    assert db.calls[0]["days"] == 90
    assert "generation_done" in db.calls[0]["keep"]
    assert storage.calls == [3]


def test_disk_used_pct_measures_a_real_path(tmp_path: Path) -> None:
    pct = maintenance.disk_used_pct(_settings(tmp_path))
    assert 0 <= pct <= 100


def test_disk_used_pct_handles_missing_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "does-not-exist")
    assert maintenance.disk_used_pct(settings) == 0
