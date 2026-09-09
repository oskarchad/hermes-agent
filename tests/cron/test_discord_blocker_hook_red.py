"""Real store/scheduler/scoped resolver/hook/REST serializer, network edges only mocked."""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from cron.discord_blocker_hook import run_discord_blocker_hook

RADAR = "1547333242502520872"
MODEL = "ag/gemini-3.8-flash-high"


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    clock = [1800000000.0]
    monkeypatch.setattr("hermes_cli.discord_blocker_observer.time.time", lambda: clock[0])
    home = tmp_path / "otto"
    home.mkdir()
    (home / "config.yaml").write_text("custom_providers:\n  - name: 9router\n    base_url: http://router.invalid/v1\n    key_env: NINEROUTER_API_KEY\n")
    p = home / "cron" / "discord-now-state.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"version": 1, "guild_id": "700", "channel_id": "900", "message_id": "901", "bot_id": "777",
        "operator_id": "999", "scope": "Only this Discord topic", "policy": "No restart without explicit human approval",
        "evidence": {"A": {"channel_id": "100", "message_id": "201", "note": "Missing certificate"}}}))
    def msg(mid, uid, text, ts=None):
        return {"id": str(mid), "channel_id": "100", "author": {"id": uid, "username": "otto"}, "content": text,
                "timestamp": datetime.fromtimestamp(ts or clock[0], timezone.utc).isoformat()}
    env = {"path": p, "home": home, "clock": clock, "messages": [msg(203, "555", "New blocker"), msg(201, "555", "Missing certificate")],
           "calls": [], "models": [], "posts": [], "fail_send": False, "fail_readback": False, "msg": msg,
           "list": "**Zablokowane**\n☐ A — czeka → <#100>\n**Ostatnio zrobione**\n☑ B → <#101>",
           "result": {"status": "known", "reason": "Missing certificate", "action": "Ask operator for certificate", "evidence_ids": ["201"]}}
    def discord_http(req, **kwargs):
        assert req.get_header("Authorization") == "Bot scoped-radar"
        method = req.get_method(); url = req.full_url
        env["calls"].append((method, url))
        if url.endswith("/users/@me"):
            data = {"id": RADAR, "bot": True}
        elif url.endswith("/channels/900/messages/901"):
            data = {"id": "901", "channel_id": "900", "author": {"id": "777"}, "content": env["list"]}
        elif method == "POST":
            env["posts"].append(json.loads(req.data))
            if env["fail_send"]:
                raise OSError("send failed")
            data = msg(300 + len(env["posts"]), RADAR, json.loads(req.data)["content"])
            env["messages"].insert(0, data)
        elif "/messages?" in url:
            data = env["messages"].copy()
        else:
            if env["fail_readback"]:
                raise OSError("readback failed")
            mid = url.rsplit("/", 1)[-1]
            data = next(m for m in env["messages"] if m["id"] == mid)
        response = MagicMock(); response.status = 200
        response.read.return_value = json.dumps(data).encode()
        response.__enter__.return_value = response
        return response
    def model_http(client, request, *args, **kwargs):
        assert str(request.url) == "http://router.invalid/v1/chat/completions"
        payload = json.loads(request.content)
        env["models"].append(payload)
        assert payload["model"] == MODEL
        return httpx.Response(200, request=request, json={"id": "response-1", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(env["result"])}}]})
    monkeypatch.setattr("urllib.request.urlopen", discord_http)
    monkeypatch.setattr(httpx.Client, "send", model_http)
    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(home, {"DISCORD_OBSERVER_BOT_TOKEN": "scoped-radar", "NINEROUTER_API_KEY": "scoped-router"}):
            yield env
    finally:
        set_multiplex_active(False)


def run(pipeline, **kwargs):
    return run_discord_blocker_hook(str(pipeline["path"]), target_bot_id="777", **kwargs)


def history(pipeline):
    return json.loads((pipeline["path"].parent / "discord-blocker-observer-state.json").read_text())


def test_real_pipeline_failed_send_restart_echo_ack_deadline(pipeline):
    e = pipeline; e["fail_send"] = True
    r = run(e)
    assert r.errors and len(e["models"]) == 1 and len(e["posts"]) == 1
    assert history(e)["A"]["delivery_pending"] and not history(e)["A"].get("delivery_receipt")
    e["fail_send"] = False; e["clock"][0] += 600
    assert not run(e).errors
    rec = history(e)["A"]
    assert rec["delivery_receipt"]["delivered_at"] == e["clock"][0] and rec["delivery_receipt"]["author_id"] == RADAR
    assert rec["cursor"] == "203"
    assert len(e["models"]) == 1 and len(e["posts"]) == 2
    for text in ["ACK", "Nie wykonano kanban_unblock", "Wznawiam przez kanban_unblock"]:
        e["clock"][0] += 100
        e["messages"].insert(0, e["msg"](400 + len(e["messages"]), "777", text))
        r = run(e)
        assert not r.settled_topics and not r.suggestions and not r.model_calls
    e["clock"][0] = rec["delivery_receipt"]["delivered_at"] + 1200
    assert len(run(e).suggestions) == 1
    assert len(e["posts"]) == 3 and len(e["models"]) == 1
    e["clock"][0] += 3000
    assert not run(e).suggestions and len(e["posts"]) == 3
    prompt = json.loads(e["models"][0]["messages"][1]["content"])
    assert prompt["goal"] and prompt["constraints"] and "prior" in prompt
    assert [m["id"] for m in prompt["messages"]] == ["201", "203"]


def test_readback_failure_retries_get_not_post_or_model(pipeline):
    e = pipeline; e["fail_readback"] = True
    assert run(e).errors
    assert not history(e)["A"].get("delivery_receipt")
    e["fail_readback"] = False
    assert not run(e).errors
    assert len(e["posts"]) == len(e["models"]) == 1


@pytest.mark.parametrize("content", ["", "**Zablokowane**\nBrak", "**Ostatnio zrobione**\n☑ A → <#100>"])
def test_current_list_empty_never_reads_old_topic(pipeline, content):
    pipeline["list"] = content
    r = run(pipeline)
    assert not r.evaluated_items and not pipeline["models"] and not pipeline["posts"]


def test_missing_scoped_secret_never_borrows_primary(pipeline, monkeypatch):
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    monkeypatch.setenv("DISCORD_OBSERVER_BOT_TOKEN", "foreign-primary")
    scope = set_secret_scope({})
    try:
        assert run(pipeline).errors
        assert not pipeline["calls"] and not pipeline["models"]
    finally:
        reset_secret_scope(scope)


def test_unknown_error_and_corruption(pipeline):
    e = pipeline; e["result"] = {"status": "unknown", "reason": "Insufficient evidence", "evidence_ids": []}
    assert not run(e).suggestions
    assert not run(e).model_calls and not e["posts"]
    sidecar = e["path"].parent / "discord-blocker-observer-state.json"
    sidecar.write_text("{")
    assert run(e).errors and len(e["models"]) == 1
    assert sidecar.read_text() == "{"


def test_scheduler_separate_hook_failure_ledger(pipeline):
    from cron.jobs import create_job
    from cron.executions import create_execution, get_execution, list_executions
    from cron.scheduler import _finish_completed_run, _RunDelivery
    e = pipeline
    e["result"] = {}
    job = create_job(prompt="list updater", schedule="every 10m", deliver="local", post_run_hook={
        "target": "cron.discord_blocker_hook:run_discord_blocker_hook", "kwargs": {"state_file_path": str(e["path"]), "target_bot_id": "777"}})
    execution = create_execution(job["id"], source="direct")
    assert _finish_completed_run(_RunDelivery(job=job, success=True, error=None), None, execution["id"])
    assert get_execution(execution["id"])["status"] == "completed"
    rows = list_executions(job_id=job["id"])
    hook = [r for r in rows if r["source"] == "post_run_hook:" + execution["id"]]
    assert len(hook) == 1 and hook[0]["status"] == "failed"


def test_disabled_and_dry_run_no_spend_or_state_mutation(pipeline):
    assert not run(pipeline, enabled=False).errors
    run(pipeline, dry_run=True)
    assert not pipeline["models"] and not pipeline["posts"]
    assert not (pipeline["path"].parent / "discord-blocker-observer-state.json").exists()
