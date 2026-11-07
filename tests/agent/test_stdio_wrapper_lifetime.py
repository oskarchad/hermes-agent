import contextlib
import io
import sys
import threading
import hermes_logging
import agent.thread_scoped_output as thread_output
from agent.thread_scoped_output import thread_scoped_silence
from agent.process_bootstrap import _install_safe_stdio


def test_repeated_agent_stdio_installation_preserves_output(monkeypatch):
    """Main-agent and background-review setup must not exhaust the Python stack."""
    outputs = {attr: io.StringIO() for attr in ("stdout", "stderr")}
    for attr, output in outputs.items():
        monkeypatch.setattr(sys, attr, output)
    monkeypatch.setattr(thread_output, "_installed", {})
    monkeypatch.setattr(thread_output, "_routing_states", {})
    monkeypatch.setattr(thread_output, "_sinks", {attr: io.StringIO() for attr in outputs})
    monkeypatch.setattr(hermes_logging, "_line_buffered_stdout", None)

    for _ in range(1100):
        _install_safe_stdio()
        hermes_logging._line_buffer_piped_stdout()
        with thread_scoped_silence():
            _install_safe_stdio()
            sys.stdout.write("hidden")
            sys.stderr.write("hidden")

    for attr, output in outputs.items():
        stream = getattr(sys, attr)
        stream.write("visible")
        stream.flush()
        assert output.getvalue() == "visible"
        output.close()
        # SafeWriter must still protect callers when a headless pipe closes.
        assert stream.write("closed") == len("closed")
        stream.flush()


def test_safe_stdio_preserves_silence_across_redirect_restoration(monkeypatch):
    """An older router behind SafeWriter retains an active worker's silencing."""
    outputs = {attr: io.StringIO() for attr in ("stdout", "stderr")}
    redirected = {attr: io.StringIO() for attr in outputs}
    for attr, output in outputs.items():
        monkeypatch.setattr(sys, attr, output)
    monkeypatch.setattr(thread_output, "_installed", {})
    monkeypatch.setattr(thread_output, "_routing_states", {})
    monkeypatch.setattr(thread_output, "_sinks", {attr: io.StringIO() for attr in outputs})
    _install_safe_stdio()
    with thread_scoped_silence():
        pass
    _install_safe_stdio()
    entered, release = threading.Event(), threading.Event()

    def worker():
        with thread_scoped_silence():
            _install_safe_stdio()
            entered.set()
            assert release.wait(timeout=10)
            sys.stdout.write("hidden")
            sys.stderr.write("hidden")

    thread = threading.Thread(target=worker)
    try:
        with contextlib.redirect_stdout(redirected["stdout"]), contextlib.redirect_stderr(redirected["stderr"]):
            thread.start()
            assert entered.wait(timeout=10)
            sys.stdout.write("redirected")
            sys.stderr.write("redirected")
        _install_safe_stdio()
        with thread_scoped_silence():
            sys.stdout.write("also-hidden")
            sys.stderr.write("also-hidden")
        sys.stdout.write("visible")
        sys.stderr.write("visible")
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    for attr, output in outputs.items():
        assert output.getvalue() == "visible"
        assert redirected[attr].getvalue() == "redirected"
