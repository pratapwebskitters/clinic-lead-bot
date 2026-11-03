import gzip
import json
import logging
from threading import Event, Lock
from unittest.mock import MagicMock

import httpx
import pytest

from app import clinics, db, pipeline
from app.services import llm, mailer, notify, recovery, workers
from tests.conftest import lead_payload


class Body(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    def __iter__(self):
        yield self.data

    def close(self):
        self.closed = True


class Trickle(Body):
    def __init__(self, clock, data=b" "):
        super().__init__(data)
        self.clock = clock
        self.chunks = 0

    def __iter__(self):
        while True:
            self.clock["now"] += 1
            self.chunks += 1
            yield self.data[(self.chunks - 1) % len(self.data):self.chunks % len(self.data) or len(self.data)]


@pytest.fixture
def clock(monkeypatch):
    value = {"now": 100.0}
    monkeypatch.setattr(llm.time, "monotonic", lambda: value["now"])
    return value


@pytest.fixture
def stream_server(monkeypatch):
    clients = []

    def setup(body, headers=None):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, stream=body, headers=headers)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(client)
        monkeypatch.setattr(llm.httpx, "stream", client.stream)
        return requests

    yield setup
    for client in clients:
        client.close()


def save(index=1):
    return db.save_lead(**lead_payload(service=None, phone=str(1234567 + index),
                                     email=f"patient{index}@example.com"))


@pytest.mark.parametrize("compressed", [False, True])
def test_trickling_provider_aborts_and_closes_response(clock, stream_server, compressed):
    body = Trickle(clock, gzip.compress(b"response")[:10] if compressed else b" ")
    requests = stream_server(body, {"Content-Encoding": "gzip"} if compressed else None)
    with pytest.raises(llm.DeadlineExceeded):
        llm.call_provider("system", "lead", deadline=105)
    assert clock["now"] == 105 and body.chunks == 5
    assert body.closed
    assert requests[0].extensions["timeout"] == {name: 5 for name in ("connect", "read", "write", "pool")}


def test_expired_provider_deadline_never_starts_request(clock, monkeypatch):
    request = MagicMock()
    monkeypatch.setattr(llm.httpx, "stream", request)
    with pytest.raises(llm.DeadlineExceeded):
        llm.call_provider("system", "lead", deadline=100)
    request.assert_not_called()


def test_deadline_exceeded_is_not_retried(fake_llm):
    calls = fake_llm(llm.DeadlineExceeded("expired"))
    result = llm.qualify({}, clinics.load_clinic("demo_clinic"))
    assert calls["n"] == 1
    assert result.llm_failed and result.needs_human


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_provider_success_and_remaining_timeout(clock, stream_server, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    data = ({"content": [{"type": "text", "text": "hello"}]} if provider == "anthropic" else
            {"choices": [{"message": {"content": "hello"}}]})
    body = Body(json.dumps(data).encode())
    requests = stream_server(body)
    assert llm.call_provider("system", "lead", timeout=2, deadline=107) == "hello"
    assert requests[0].extensions["timeout"] == {name: 2 for name in ("connect", "read", "write", "pool")}
    assert body.closed


@pytest.mark.parametrize("extra,compressed", [(0, False), (1, False), (1, True)])
def test_response_body_limit(clock, stream_server, extra, compressed):
    data = json.dumps({"content": [{"type": "text", "text": "hello"}]}).encode()
    data += b" " * (llm.MAX_RESPONSE_BYTES - len(data) + extra)
    body = Body(gzip.compress(data) if compressed else data)
    stream_server(body, {"Content-Encoding": "gzip"} if compressed else None)
    if extra:
        with pytest.raises(ValueError, match="64 KB"):
            llm.call_provider("system", "lead", deadline=110)
    else:
        assert llm.call_provider("system", "lead", deadline=110) == "hello"
    assert body.closed


def test_trickling_provider_releases_worker(clock, stream_server, sent, auto_mode):
    db.init_db()
    lead_id = save()
    body = Trickle(clock)
    requests = stream_server(body)
    workers.start(1)
    try:
        future = workers.submit_lead(lead_id)
        future.result(timeout=5)
        workers.wait_for_idle()
        assert len(requests) == 1 and body.closed and body.chunks == pipeline.DEADLINE
        assert workers.stats()["active"] == 0 and workers.stats()["queued"] == 0
        lead = db.get_lead(lead_id)
        assert lead["status"] == "needs_human" and lead["needs_human"] == 1
        assert len(sent["email"]) == 1
    finally:
        workers.stop()


def test_full_pool_alerts_once_and_refills_saved_ninth_lead(clock, sent, auto_mode, monkeypatch):
    db.init_db()
    ids = [save(index) for index in range(1, 10)]
    started, release, ninth_sent, lock = Event(), Event(), Event(), Lock()
    calls = {"n": 0}

    def provider(system, user, timeout=None, deadline=None):
        with lock:
            calls["n"] += 1
            if calls["n"] == 4:
                started.set()
        assert release.wait(timeout=5)
        return json.dumps({"score": "cold", "reply": "Thanks for contacting us."})

    original_send = mailer.send_email

    def send(**kwargs):
        result = original_send(**kwargs)
        if kwargs["to"] == "patient9@example.com":
            ninth_sent.set()
        return result

    monkeypatch.setattr(llm, "call_provider", provider)
    monkeypatch.setattr(mailer, "send_email", send)
    workers.start(4)
    try:
        for lead_id in ids[:4]:
            assert workers.submit_lead(lead_id) is not None
        assert started.wait(timeout=5)
        for lead_id in ids[4:8]:
            assert workers.submit_lead(lead_id) is not None
        assert workers.submit_lead(ids[8]) is None
        assert workers.submit_lead(ids[8]) is None
        assert db.get_lead(ids[8])["status"] == "received"
        assert db.get_lead(ids[8])["processing_backlog"] == 1
        assert sent["telegram"] == [("admin-chat", "processing backlog: 5 leads waiting")]
        stats = workers.stats()
        assert stats["active"] == 4 and stats["queued"] == 4
        assert stats["oldest_active_start_time"] == 100
        release.set()
        assert ninth_sent.wait(timeout=5)
        workers.wait_for_idle()
        assert db.get_lead(ids[8])["status"] == "replied"
        assert all(db.get_lead(lead_id)["status"] == "replied" for lead_id in ids)
        assert len(sent["email"]) == 9 and len(sent["telegram"]) == 1
    finally:
        release.set()
        workers.stop()


def test_backlog_alert_is_rate_limited_for_five_minutes(clock, sent, monkeypatch):
    db.init_db()
    lead_id = save()
    monkeypatch.setattr(workers, "_last_backlog_alert", None)
    workers._report_backlog(lead_id)
    clock["now"] = 399
    workers._report_backlog(lead_id)
    assert len(sent["telegram"]) == 1
    clock["now"] = 400
    workers._report_backlog(lead_id)
    assert len(sent["telegram"]) == 2


def test_received_backlog_is_selected_oldest_first():
    db.init_db()
    first, second = save(1), save(2)
    with db.connect() as conn:
        conn.execute("UPDATE leads SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (second,))
    db.record_backlog(first)
    assert db.received_leads(2) == [second, first]
    assert [lead["id"] for lead in db.recovery_candidates(db.utcnow())] == [second, first]


@pytest.mark.parametrize("claim_first", [True, False])
def test_watchdog_marks_abandoned_and_alerts_once(clock, sent, monkeypatch, caplog, claim_first):
    db.init_db()
    lead_id = save()
    started, release = Event(), Event()

    def blocked(number):
        if claim_first:
            assert db.claim_status(number, "received", "processing")
            workers.bind_claim(number, db.get_lead(number)["claim_token"])
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(pipeline, "process_lead", blocked)
    workers.start(1)
    try:
        future = workers.submit_lead(lead_id)
        assert started.wait(timeout=5)
        clock["now"] = 175
        workers.watchdog()
        assert sent["telegram"] == []
        clock["now"] = 176
        workers.watchdog()
        workers.watchdog()
        recovery.run_recovery()
        state = workers.stats()
        assert state["active"] == 1 and state["abandoned"] == 1
        assert state["workers"][0]["abandoned"] is True
        lead = db.get_lead(lead_id)
        assert lead["worker_abandoned"] == 1 and lead["status"] == "needs_human"
        assert lead["needs_human"] == 1
        assert len(sent["telegram"]) == 1 and "abandoned" in sent["telegram"][0][1]
        assert any(record.levelno == logging.ERROR and "abandoned" in record.getMessage()
                   for record in caplog.records)
        release.set()
        future.result(timeout=5)
        assert db.get_lead(lead_id)["worker_abandoned"] == 1
    finally:
        release.set()
        workers.stop()


def test_telegram_remaining_time_caps_all_phases(clock, stream_server, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    body = Body(b'{"ok":true}')
    requests = stream_server(body)
    assert notify.send_telegram("owner", "Alert", deadline=103)
    assert requests[0].extensions["timeout"] == {name: 3 for name in ("connect", "read", "write", "pool")}
    assert body.closed


def test_trickling_telegram_aborts_at_deadline(clock, stream_server, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    body = Trickle(clock)
    stream_server(body)
    assert not notify.send_telegram("owner", "Alert", deadline=103)
    assert clock["now"] == 103 and body.closed


def test_expired_telegram_deadline_skips_request(clock, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    request = MagicMock()
    monkeypatch.setattr(notify.httpx, "stream", request)
    assert not notify.send_telegram("owner", "Alert", deadline=100)
    request.assert_not_called()


def test_smtp_remaining_timeout_is_checked_between_stages(clock, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    monkeypatch.setenv("SMTP_USER", "test-user")
    monkeypatch.setenv("SMTP_USE_TLS", "1")
    server = MagicMock()
    timeouts = []

    def connect(host, port, timeout):
        timeouts.append(timeout)
        clock["now"] += 2
        return server

    def advance(*args, **kwargs):
        clock["now"] += 3

    server.starttls.side_effect = advance
    server.login.side_effect = advance
    monkeypatch.setattr(mailer.smtplib, "SMTP", connect)
    assert mailer.send_email("patient@example.com", "Enquiry", "Thanks", deadline=110) is mailer.DeliveryResult.SENT
    assert timeouts == [10]
    assert [call.args[0] for call in server.sock.settimeout.call_args_list[:3]] == [8, 5, 2]
    server.send_message.assert_called_once()


@pytest.mark.parametrize("stage", ["connect", "tls", "login"])
def test_smtp_does_not_transmit_after_setup_exhausts_budget(clock, monkeypatch, stage):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    monkeypatch.setenv("SMTP_USER", "test-user")
    monkeypatch.setenv("SMTP_USE_TLS", "1")
    server = MagicMock()

    def expire(*args, **kwargs):
        clock["now"] = 111
        return server

    factory = MagicMock(return_value=server)
    if stage == "connect":
        factory.side_effect = expire
    elif stage == "tls":
        server.starttls.side_effect = expire
    else:
        server.login.side_effect = expire
    monkeypatch.setattr(mailer.smtplib, "SMTP", factory)
    assert mailer.send_email("patient@example.com", "Enquiry", "Thanks", deadline=110) is mailer.DeliveryResult.FAILED
    server.send_message.assert_not_called()
