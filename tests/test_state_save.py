"""Tests for state-file saving under concurrent shutdown.

Regression tests for the production failure where a SIGTERM caused both the
controller thread (run loop exit) and ``ThreadManager.stop_all()`` (main thread)
to call ``LightingController.shutdown()`` at the same time. The two concurrent
``_save_state()`` calls raced on the shared ``.tmp`` file and the loser failed
with a blank ``"Failed to save state file: "`` fatal error.
"""

from __future__ import annotations

# This test module deliberately exercises private controller internals and lives
# outside a package, so silence those two test-only lints for the whole file.
# ruff: noqa: INP001, SLF001
import asyncio
import sys
import threading
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sc_foundation import JSONEncoder, RestartPolicy, ThreadManager

import controller as controller_module
from controller import LightingController, describe_exception

THREADS = 4
ROUNDS = 25


def make_controller(state_path: Path) -> LightingController:
    """Build a LightingController with just enough state for ``_save_state``.

    Args:
        state_path: Where the controller should write its state file.

    Returns:
        A controller instance created without running ``__init__``.
    """
    ctl = object.__new__(LightingController)
    config = MagicMock()
    config.get.side_effect = lambda *_args, default=None: default
    ctl.config = config
    ctl.logger = MagicMock()
    ctl.state_filepath = state_path
    ctl.dusk_dawn = {}
    ctl.offset_cache = {"Switch1": 5}
    # Enough data that a write takes long enough for an unserialised race to show up.
    ctl.switch_events = [{"Date": f"2099-01-{i % 28 + 1:02d}", "Events": [{"Time": j} for j in range(50)]} for i in range(100)]
    ctl.switch_states = []
    ctl.groups = []
    ctl._state_lock = RLock()
    ctl._save_lock = Lock()
    ctl._shutdown_lock = Lock()
    ctl._shutdown_done = False
    ctl._trim_switch_events = MagicMock()
    return ctl


def _run_concurrently(target: Any, count: int) -> None:
    """Start ``count`` threads on ``target`` behind a barrier and wait for them.

    Args:
        target: Zero-argument callable each thread runs.
        count: Number of threads.
    """
    barrier = threading.Barrier(count)

    def worker() -> None:
        barrier.wait()
        target()

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


@pytest.fixture(autouse=True)
def _no_web_viewer_post() -> Any:
    """Stop ``_save_state`` from posting to the web viewer.

    Yields:
        The mock standing in for ``post_state_to_web_viewer``.
    """
    with patch.object(controller_module, "post_state_to_web_viewer") as mock_post:
        yield mock_post


def test_concurrent_saves_do_not_fail(tmp_path: Path) -> None:
    """Many threads saving at once all succeed and leave a valid state file."""
    ctl = make_controller(tmp_path / "system_state.json")

    for _ in range(ROUNDS):
        _run_concurrently(ctl._save_state, THREADS)

    ctl.logger.log_fatal_error.assert_not_called()
    assert ctl.state_filepath is not None
    state = JSONEncoder.read_from_file(ctl.state_filepath)
    assert isinstance(state, dict)
    assert state["StateFileType"] == "LightingControl"
    assert not (tmp_path / "system_state.tmp").exists()


def test_concurrent_shutdown_saves_once(tmp_path: Path) -> None:
    """Concurrent ``shutdown()`` calls (controller thread + stop_all) save exactly once."""
    ctl = make_controller(tmp_path / "system_state.json")

    with patch.object(JSONEncoder, "save_to_file", wraps=JSONEncoder.save_to_file) as spy:
        _run_concurrently(ctl.shutdown, 2)
        ctl.shutdown()  # a later call is also a no-op

    assert spy.call_count == 1
    ctl.logger.log_fatal_error.assert_not_called()
    shutdown_msgs = [c for c in ctl.logger.log_message.call_args_list if "Shutting down" in c.args[0]]
    assert len(shutdown_msgs) == 1


def test_save_failure_logs_underlying_cause(tmp_path: Path) -> None:
    """A failed save logs the chained cause instead of a blank message."""
    ctl = make_controller(tmp_path / "system_state.json")

    def failing_save(_data: object, _path: Path) -> bool:
        try:
            raise FileNotFoundError(2, "No such file or directory")
        except OSError as e:
            raise RuntimeError from e

    with patch.object(JSONEncoder, "save_to_file", side_effect=failing_save):
        ctl._save_state()

    ctl.logger.log_fatal_error.assert_called_once()
    message = ctl.logger.log_fatal_error.call_args.args[0]
    assert "FileNotFoundError" in message
    assert "No such file or directory" in message
    assert str(ctl.state_filepath) in message


def _raise_chained() -> None:
    """Raise a message-less RuntimeError chained from a ValueError, like JSONEncoder does.

    Raises:
        RuntimeError: Always, chained from a ValueError.
    """
    inner = ValueError("bad value")
    raise RuntimeError from inner


def test_describe_exception_formats_chain() -> None:
    """``describe_exception`` walks the cause chain and omits empty messages."""
    with pytest.raises(RuntimeError) as exc_info:
        _raise_chained()
    assert describe_exception(exc_info.value) == "RuntimeError <- ValueError: bad value"
    assert describe_exception(OSError("disk full")) == "OSError: disk full"


def test_notifier_ignores_closed_event_loop() -> None:
    """The webapp notifier is a no-op once its event loop has closed (SIGTERM shutdown)."""
    from webapp import _WebAppNotifier  # noqa: PLC0415, PLC2701

    loop = asyncio.new_event_loop()
    notifier = _WebAppNotifier()
    notifier.bind(loop, asyncio.Queue(maxsize=1))
    loop.close()

    notifier.notify()  # previously raised RuntimeError: Event loop is closed


def test_notify_webapp_failure_does_not_crash_controller(tmp_path: Path) -> None:
    """A failing webapp notifier is logged as a warning, not raised to the controller thread."""
    ctl = make_controller(tmp_path / "system_state.json")
    ctl._webapp_notify = MagicMock(side_effect=RuntimeError("Event loop is closed"))

    ctl._notify_webapp()

    ctl.logger.log_message.assert_called_once()
    message, level = ctl.logger.log_message.call_args.args
    assert level == "warning"
    assert "Event loop is closed" in message


def test_stop_all_calls_stop_not_shutdown(tmp_path: Path) -> None:
    """ThreadManager.stop_all() picks the wake-only stop(), never shutdown()."""
    ctl = make_controller(tmp_path / "system_state.json")
    ctl.wake_event = threading.Event()
    ctl._webapp_notify = None
    ctl.shutdown = MagicMock()  # type: ignore[method-assign]

    tm = ThreadManager(MagicMock(), global_stop=threading.Event())
    # Any bound method of the controller exercises stop_all()'s target.__self__ lookup.
    tm.add(name="controller", target=ctl._notify_webapp, restart=RestartPolicy(mode="never"))
    tm.start_all()
    tm.join_all(timeout_per_thread=5.0)
    tm.stop_all()

    assert ctl.wake_event.is_set()
    ctl.shutdown.assert_not_called()


def test_sigterm_mid_tick_shuts_down_on_controller_thread_after_tick(tmp_path: Path) -> None:
    """If stop is requested mid-tick, shutdown waits for the tick and runs on the controller thread."""
    ctl = make_controller(tmp_path / "system_state.json")
    ctl.wake_event = threading.Event()
    ctl.check_interval = 60
    ctl.config_last_check = None
    ctl.config.check_for_config_changes.return_value = None
    ctl.logger.ping_heartbeat.return_value = True

    tick_started = threading.Event()
    release_tick = threading.Event()
    calls: list[tuple[str, str]] = []

    def blocking_change_switch_states() -> None:
        tick_started.set()
        assert release_tick.wait(timeout=5.0)
        calls.append(("tick", threading.current_thread().name))

    def record_save() -> None:
        calls.append(("save", threading.current_thread().name))

    for name in ("_print_to_console", "_summarise_schedule_evaluations", "_evaluate_switch_states",
                 "_summarise_switch_states", "_notify_webapp", "_trim_logfile_if_needed"):
        setattr(ctl, name, MagicMock())
    ctl._refresh_device_status = MagicMock(return_value=True)  # type: ignore[method-assign]
    ctl._change_switch_states = blocking_change_switch_states  # type: ignore[method-assign]
    ctl._save_state = record_save  # type: ignore[method-assign]

    stop_event = threading.Event()
    tm = ThreadManager(MagicMock(), global_stop=stop_event)
    tm.add(name="controller", target=ctl.run, kwargs={"stop_event": stop_event}, restart=RestartPolicy(mode="never"))
    tm.start_all()
    assert tick_started.wait(timeout=5.0)

    # Simulate the SIGTERM handler, then main's finally: block, while the tick is still running.
    stop_event.set()
    ctl.wake_event.set()
    tm.stop_all()
    assert not ctl._shutdown_done, "shutdown() must not run while a tick is in progress"

    release_tick.set()
    tm.join_all(timeout_per_thread=5.0)

    assert ctl._shutdown_done
    # Tick work (and its own save) completes first; the final shutdown save comes last,
    # on the controller thread, not the main thread.
    assert calls == [("tick", "controller"), ("save", "controller"), ("save", "controller")]
