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
def pipeline(tmp_path, monkeypatch, request):
    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    clock = [1800000000.0]
    monkeypatch.setattr("hermes_cli.discord_blocker_observer.time.time", lambda: clock[0])
    home = tmp_path / "otto"
    home.mkdir()
    (home / "config.yaml").write_text("custom_providers:\n  - name: 9router\n    base_url: http://router.invalid/v1\n    key_env: NINEROUTER_API_KEY\n")
    p = home / "cron" / "discord-now-state.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"version": 1, "guild_id": "700", "thread_ids": ["100"], "channel_id": "900", "message_id": "901", "bot_id": "777",
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
            data = msg(max(300, *(int(m["id"]) for m in env["messages"])) + 1, RADAR, json.loads(req.data)["content"])
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
        request.node.user_properties.append(("sequence", json.dumps(env.get("sequence", []))))


def run(pipeline, **kwargs):
    e = pipeline
    inputs = {"state": json.loads(e["path"].read_text()), "list": e["list"],
              "messages": json.loads(json.dumps(e["messages"])), "time": e["clock"][0],
              "fail_send": e["fail_send"], "fail_readback": e["fail_readback"], "result": e["result"]}
    models, posts = len(e["models"]), len(e["posts"])
    report = run_discord_blocker_hook(str(e["path"]), target_bot_id="777", **kwargs)
    e.setdefault("sequence", []).append({"input": inputs, "model_requests": len(e["models"]) - models,
        "post_attempts": len(e["posts"]) - posts, "posts": e["posts"][posts:],
        "errors": report.errors, "settled": report.settled_topics})
    return report


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


def test_shared_channel_selects_exact_topic_and_context(pipeline):
    e = pipeline
    s = json.loads(e["path"].read_text())
    s["evidence"]["B"] = {"channel_id": "100", "message_id": "203", "note": "B completed"}
    e["path"].write_text(json.dumps(s))
    e["list"] = "**Zablokowane**\n☐ A — czeka → <#100>\n**Ostatnio zrobione**\n☑ B → <#100>"
    r = run(e)
    assert not r.errors and r.evaluated_items == ["A"]
    assert len(e["models"]) == len(e["posts"]) == 1
    context = json.loads(e["models"][0]["messages"][1]["content"])
    assert [m["id"] for m in context["messages"]] == ["201"]
    # An unmatched title sharing that channel is unknown, never either sibling.
    e["list"] = "**Zablokowane**\n☐ Unmapped topic → <#100>"
    assert run(e).errors
    assert len(e["models"]) == len(e["posts"]) == 1


@pytest.mark.parametrize("pending", [False, True])
def test_material_revision_precedes_delivery_and_reopens_episode(pipeline, pending):
    e = pipeline; e["fail_send"] = pending
    first = run(e)
    assert first.model_calls == 1
    old = history(e)["A"]
    e["clock"][0] += 600
    e["messages"].insert(0, e["msg"](500, "999", "ACK, but correction: certificate is available; account access is now revoked."))
    s = json.loads(e["path"].read_text())
    s["policy"] = "Do not request another certificate; investigate revoked account access."
    e["path"].write_text(json.dumps(s))
    e["result"] = {"status": "known", "reason": "Account revoked", "action": "Ask operator about account access", "evidence_ids": ["500"]}
    e["fail_send"] = False
    second = run(e)
    assert not second.errors and second.model_calls == 1
    assert "account access" in e["posts"][-1]["content"]
    rec = history(e)["A"]
    assert rec["revision"] != old["revision"]
    if pending:
        assert rec["revisions"][0]["pending_suggestion"] == old["pending_suggestion"]
    else:
        assert rec["revisions"][0]["delivery_receipt"] == old["delivery_receipt"]
    assert run(e).model_calls == 0
    assert history(e)["A"]["delivery_receipt"] == rec["delivery_receipt"]
    e["clock"][0] += 1200
    assert len(run(e).suggestions) == 1
    assert "account access" in e["posts"][-1]["content"]
    assert not run(e).suggestions
    e["list"] = "**Ostatnio zrobione**\n☑ A → <#100>"
    assert not run(e).evaluated_items
    assert history(e)["A"]["eligible"] is False
    e["list"] = "**Zablokowane**\n☐ A — czeka → <#100>"
    assert run(e).model_calls == 1
    assert history(e)["A"]["episode"] == rec["episode"] + 1
    assert not run(e).model_calls
    assert len(e["models"]) == 3 and len(e["posts"]) == 4


def test_unknown_ack_echo_and_mixed_correction_restart(pipeline):
    e = pipeline
    e["result"] = {"status": "unknown", "reason": "Missing evidence", "evidence_ids": []}
    assert run(e).model_calls == 1
    before = history(e)["A"]
    assert run(e).model_calls == 0
    e["messages"].insert(0, e["msg"](400, RADAR, "observer echo"))
    assert run(e).model_calls == 0
    e["messages"].insert(0, e["msg"](500, "777", "ACK"))
    assert run(e).model_calls == 0
    rec = history(e)["A"]
    assert rec["revision"] == before["revision"] and rec["analysis"] == before["analysis"]
    assert rec["disposition"]["kind"] == "response_observed_not_action"
    assert rec["disposition"]["message_id"] == "500"
    assert rec["disposition"]["author_id"] == "777"
    assert rec["disposition"]["timestamp"] == e["messages"][0]["timestamp"]
    e["messages"].insert(0, e["msg"](501, "777", "ACK, but the certificate is ready; access is revoked."))
    assert run(e).model_calls == 1
    assert not run(e).model_calls and not e["posts"]
    assert len(e["models"]) == 2


def test_pending_read_failure_is_not_removal_or_delivery(pipeline, monkeypatch):
    e = pipeline; e["fail_send"] = True
    assert run(e).errors
    before = history(e)["A"]
    original = __import__("urllib.request", fromlist=["urlopen"]).urlopen
    def failed_history(req, **kwargs):
        if "/messages?" in req.full_url:
            raise OSError("read unavailable")
        return original(req, **kwargs)
    monkeypatch.setattr("urllib.request.urlopen", failed_history)
    e["fail_send"] = False
    assert run(e).errors
    assert history(e)["A"] == before
    assert len(e["posts"]) == len(e["models"]) == 1
    monkeypatch.setattr("urllib.request.urlopen", original)
    assert not run(e).errors
    assert len(e["posts"]) == 2 and len(e["models"]) == 1
    # Observed removal cancels retry, and reappearance is a fresh episode.
    e["list"] = "**Ostatnio zrobione**\n☑ A → <#100>"
    assert not run(e).errors
    e["list"] = "**Zablokowane**\n☐ A — czeka → <#100>"
    assert run(e).model_calls == 1
    assert len(e["posts"]) == 3


def test_v2_receipts_fail_visibly_without_reinterpreting_or_deleting(pipeline):
    e = pipeline
    sidecar = e["path"].parent / "discord-blocker-observer-state.json"
    old = {"A": {"version": 2, "delivery_receipt": {"id": "300", "delivered_at": e["clock"][0]},
                 "pending_suggestion": {"content": "old advice"}}}
    sidecar.write_text(json.dumps(old))
    before = sidecar.read_bytes()
    assert run(e).errors
    assert not e["models"] and not e["posts"]
    assert sidecar.read_bytes() == before


def test_shared_channel_reply_revision_survives_restart_without_sibling_context(pipeline):
    e = pipeline
    s = json.loads(e["path"].read_text())
    s["evidence"]["B"] = {"channel_id": "100", "message_id": "203", "note": "B completed"}
    e["path"].write_text(json.dumps(s))
    assert run(e).model_calls == 1
    old_id = history(e)["A"]["delivery_receipt"]["id"]
    e["clock"][0] += 600
    correction = e["msg"](500, "999", "ACK, but certificate is ready; access is revoked")
    correction["message_reference"] = {"channel_id": "100", "message_id": old_id}
    e["messages"].insert(0, correction)
    e["result"] = {"status": "known", "reason": "Access revoked", "action": "Check account access", "evidence_ids": ["500"]}
    e["clock"][0] += 600
    assert run(e).model_calls == 1
    e["messages"].insert(0, e["msg"](600, "999", "Unrelated sibling conversation"))
    assert run(e).model_calls == 0
    assert run(e).model_calls == 0
    context = json.loads(e["models"][-1]["messages"][1]["content"])
    assert [m["id"] for m in context["messages"]] == ["201", "500"]
    assert history(e)["A"]["disposition"]["suggestion_message_id"] == old_id
    assert len(e["models"]) == len(e["posts"]) == 2


@pytest.mark.parametrize("damage", ["revision", "pending_binding"])
def test_invalid_revision_state_cannot_resume_pending(pipeline, damage):
    e = pipeline; e["fail_send"] = True
    assert run(e).errors
    sidecar = e["path"].parent / "discord-blocker-observer-state.json"
    saved = history(e)
    if damage == "revision":
        del saved["A"]["revision"]
    else:
        saved["A"]["pending_suggestion"]["revision"] = "0" * 64
    sidecar.write_text(json.dumps(saved))
    before = sidecar.read_bytes()
    e["fail_send"] = False
    assert run(e).errors
    assert len(e["posts"]) == len(e["models"]) == 1
    assert sidecar.read_bytes() == before


def test_removal_suppresses_unsent_proposal_until_new_episode(pipeline):
    e = pipeline; e["fail_send"] = True
    assert run(e).errors
    pending = history(e)["A"]["pending_suggestion"]
    e["fail_send"] = False
    e["list"] = "**Ostatnio zrobione**\n☑ A → <#100>"
    assert not run(e).errors and not run(e).model_calls
    assert len(e["models"]) == len(e["posts"]) == 1
    e["list"] = "**Zablokowane**\n☐ A — czeka → <#100>"
    assert run(e).model_calls == 1
    assert e["posts"][-1]["content"] != pending["content"]
    assert history(e)["A"]["revisions"][0]["pending_suggestion"] == pending


def test_current_blocked_entry_reaches_evaluator_and_revision_return_is_new_delivery(pipeline):
    e = pipeline
    assert run(e).model_calls == 1
    first = history(e)["A"]["delivery_receipt"]
    original = e["list"]
    e["list"] = "**Zablokowane**\n☐ A — corrected blocker: account access revoked → <#100>"
    e["clock"][0] += 600
    assert run(e).model_calls == 1
    context = json.loads(e["models"][-1]["messages"][1]["content"])
    assert "account access revoked" in context["current_blocked_entry"]
    e["list"] = original
    e["clock"][0] += 600
    assert run(e).model_calls == 1
    assert len(e["posts"]) == 3
    assert history(e)["A"]["delivery_receipt"]["id"] != first["id"]
    assert history(e)["A"]["delivery_receipt"]["delivered_at"] == e["clock"][0]
    assert not run(e).model_calls


@pytest.mark.parametrize("followup", [False, True])
def test_r1_late_reply_tracks_archived_receipt_ancestry(pipeline, monkeypatch, tmp_path, followup):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(pipeline["home"]))
    e = pipeline
    s = json.loads(e["path"].read_text())
    s["evidence"]["B"] = {"channel_id": "100", "message_id": "203", "note": "B completed"}
    e["path"].write_text(json.dumps(s))
    assert run(e).model_calls == 1
    old = history(e)["A"]["delivery_receipt"]
    if followup:
        e["clock"][0] += 1200
        assert not run(e).model_calls
        old = history(e)["A"]["followup_receipt"]
    e["clock"][0] += 20
    correction = e["msg"](500, "999", "Correction: certificate ready; account access revoked.")
    correction["message_reference"] = {"channel_id": "100", "message_id": old["id"]}
    e["messages"].insert(0, correction)
    e["result"] = {"status": "known", "reason": "Access revoked", "action": "Check account access", "evidence_ids": ["500"]}
    assert run(e).model_calls == 1
    latest = history(e)["A"]["delivery_receipt"]
    assert latest["id"] != old["id"]
    assert not run(e).model_calls
    for mid, parent in [(600, old["id"]), (601, "600")]:
        e["clock"][0] += 20
        reply = e["msg"](mid, "777", "ACK")
        reply["message_reference"] = {"channel_id": "100", "message_id": parent}
        e["messages"].insert(0, reply)
        assert not run(e).model_calls
        before = history(e)["A"]["disposition"]
        assert before["message_id"] == str(mid)
        assert before["suggestion_message_id"] == old["id"]
        assert before["kind"] == "response_observed_not_action"
        assert not run(e).model_calls
        after = history(e)["A"]["disposition"]
        assert after == before
        e["sequence"][-1].update(response_before=before, response_after=after)
    key = "followup_receipt" if followup else "delivery_receipt"
    assert history(e)["A"]["revisions"][0][key] == old
    assert len(e["models"]) == 2 and len(e["posts"]) == 2 + int(followup)
    assert all(not tick["settled"] for tick in e["sequence"])


@pytest.mark.parametrize("correlation", ["explicit", "retained", "unknown"])
def test_r1_unknown_restart_preserves_known_response_not_inferred_association(pipeline, monkeypatch, tmp_path, correlation):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(pipeline["home"]))
    e = pipeline
    assert run(e).model_calls == 1
    old = history(e)["A"]["delivery_receipt"]
    e["clock"][0] += 20
    correction = e["msg"](500, "999", "Correction: certificate ready; previous advice obsolete; cause unknown.")
    correction["message_reference"] = {"channel_id": "100", "message_id": old["id"] if correlation == "explicit" else "201"}
    e["messages"].insert(0, correction)
    if correlation == "retained":
        # Existing v3 state already knows response500 -> proposal301, as in Gauge B.
        # A replay with only the topic anchor must not erase that retained fact.
        saved = history(e)
        saved["A"]["disposition"] = {"kind": "response_observed_not_action", "message_id": "500",
            "author_id": "999", "timestamp": correction["timestamp"], "suggestion_message_id": old["id"]}
        (e["path"].parent / "discord-blocker-observer-state.json").write_text(json.dumps(saved))
    e["result"] = {"status": "unknown", "reason": "Need new evidence", "evidence_ids": ["500"]}
    assert run(e).model_calls == 1
    before = history(e)["A"]["disposition"]
    expected = None if correlation == "unknown" else old["id"]
    assert before["message_id"] == "500" and before["suggestion_message_id"] == expected
    e["clock"][0] += 2400
    assert not run(e).model_calls
    after = history(e)["A"]["disposition"]
    e["sequence"][-1].update(response_before=before, response_after=after)
    assert after == before
    assert history(e)["A"]["revisions"][0]["delivery_receipt"] == old
    assert not history(e)["A"].get("delivery_receipt")
    assert len(e["models"]) == 2 and len(e["posts"]) == 1
    assert all(not tick["settled"] for tick in e["sequence"])
