import json
import sys
import threading
from types import SimpleNamespace

import pytest

from iptv_pilot.background.main import periodic
from scripts import deploy


def test_service_references_reject_shell_and_option_injection():
    assert deploy.service_images(["encoder=registry.example/app:v1"]) == {
        "encoder": "registry.example/app:v1"}
    for value in ["--bad=image", "encoder=--option", "encoder=image;command"]:
        with pytest.raises(ValueError):
            deploy.service_images([value])


@pytest.mark.parametrize("pull_policy", ["always", "missing"])
def test_scoped_upgrade_waits_preserves_rollback_and_only_restarts_target(tmp_path, monkeypatch, pull_policy):
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    (tmp_path / ".env").write_text("PRIVATE_SETTING=local-only")
    calls = []

    def command(args):
        if "config" in args:
            return json.dumps({"services": {"encoder": {}, "asr": {}, "web": {}}})
        if "ps" in args:
            return "container"
        if "image" in args:
            return json.dumps([{"Id": "new-image"}])
        if "inspect" in args:
            return json.dumps([{"Image": "new-image" if any("up" in c for c in calls) else "old-image",
                                "State": {"Running": True}}])
        raise AssertionError(args)

    active_polls = []

    def api(url, token, payload=None):
        if payload:
            calls.append(["maintenance", str(payload["draining"])])
        if url.endswith("/health"):
            return {"ok": True}
        if not payload:
            active_polls.append(True)
        return {"encoder": {"active": 1 if len(active_polls) == 2 else 0, "draining": False}}

    monkeypatch.setattr(deploy, "command", command)
    monkeypatch.setattr(deploy, "api", api)
    monkeypatch.setattr(deploy.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["deploy", "--project-dir", str(tmp_path),
                                     "--service", "encoder=registry.example/app:new", "--apply",
                                     "--pull", pull_policy])
    deploy.main()
    ups = [c for c in calls if "up" in c]
    assert len(ups) == 1 and ups[0][-1] == "encoder" and "--no-deps" in ups[0]
    pulls = [c for c in calls if "pull" in c]
    assert pulls == ([["docker", "pull", "registry.example/app:new"]] if pull_policy == "always" else [])
    assert len(active_polls) >= 3
    state_file = next((tmp_path / ".deploy-history").glob("*/state.json"))
    state = json.loads(state_file.read_text())
    assert state["rollback_images"]["encoder"].startswith("iptv-pilot-rollback:encoder-")
    assert (state_file.parent / ".env").read_text() == "PRIVATE_SETTING=local-only"
    assert calls[-1] == ["maintenance", "False"]


def test_periodic_job_recovers_from_failure(monkeypatch):
    updates = []
    database = SimpleNamespace(runtime_jobs=SimpleNamespace(
        update_one=lambda query, update, **kw: updates.append(update)))
    monkeypatch.setattr("iptv_pilot.background.main.is_draining", lambda *_: False)
    stop = threading.Event()
    attempts = []

    def job():
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError("transient failure")
        stop.set()

    periodic(database, "test", job, 0, stop)
    assert len(attempts) == 2
    assert any("last_success_at" in u["$set"] for u in updates)
    assert updates[-1]["$set"] == {"active": False}
