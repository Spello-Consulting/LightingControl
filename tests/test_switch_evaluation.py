"""Tests for the switch-state priority chain and mode setters (issue #25).

These exercise ``Controller._evaluate_switch_states`` and the ``set_switch_mode`` /
``set_group_mode`` helpers, verifying that an individual switch override (webapp
*or* input) outranks the group mode, and that a group-mode change re-syncs its
member switches ("group reset clears child").
"""

from __future__ import annotations

# This test module deliberately exercises private controller internals and lives
# outside a package, so silence those two test-only lints for the whole file.
# ruff: noqa: INP001, SLF001
import sys
from pathlib import Path
from threading import RLock
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from controller import LightingController
from enumerations import AppMode, StateReasonOff, StateReasonOn, SystemState


class FakeView:
    """Minimal stand-in for the smart-device status view used during evaluation."""

    def __init__(self, inputs: dict[str, bool]) -> None:
        """Store the input states.

        Args:
            inputs: Mapping of input name to whether that input reads as on.
        """
        self._inputs = inputs

    def get_input_id(self, input_name: str | None) -> str | None:
        """Return an id for a known input name, else None.

        Args:
            input_name: The input to look up.

        Returns:
            The input name used as its id, or None if unknown.
        """
        return input_name if input_name in self._inputs else None

    def get_input_state(self, input_id: str) -> bool:
        """Return whether the given input reads as on.

        Args:
            input_id: The input id previously returned by :meth:`get_input_id`.

        Returns:
            True if the input is on.
        """
        return self._inputs.get(input_id, False)


def _toggle_event(input_name: str, on: bool) -> dict[str, Any]:
    """Build a webhook toggle event for an input.

    Args:
        input_name: The input the event belongs to.
        on: True for a toggle-on event, False for toggle-off.

    Returns:
        A webhook event dict shaped like the ones the worker yields.
    """
    return {
        "Component": {"Name": input_name},
        "Event": "input.toggle_on" if on else "input.toggle_off",
    }


def make_controller(
    *,
    group_mode: AppMode = AppMode.AUTO,
    switch_mode: AppMode = AppMode.AUTO,
    scheduled_state: str = "OFF",
    schedule_reason: str | None = None,
    input_name: str | None = None,
    input_on: bool = False,
    webhook_events: list[dict[str, Any]] | None = None,
    disable_all: bool = False,
) -> LightingController:
    """Build a Controller wired with a single group and switch for evaluation.

    ``__init__`` is bypassed so no config files, schedules, or devices are needed;
    only the attributes ``_evaluate_switch_states`` touches are populated, and the
    schedule lookups are stubbed.

    Args:
        group_mode: The group's webapp AppMode.
        switch_mode: The switch's webapp AppMode.
        scheduled_state: The schedule's evaluated state ("ON"/"OFF").
        schedule_reason: Optional schedule reason (e.g. "DatesOff").
        input_name: The switch's input name, or None for no input.
        input_on: Whether the input reads as on in the polled view.
        webhook_events: Queued webhook events to drain, if any.
        disable_all: The General.DisableAllSwitches flag.

    Returns:
        A Controller ready for ``_evaluate_switch_states`` / mode-setter calls.
    """
    ctrl = object.__new__(LightingController)
    ctrl._state_lock = RLock()
    ctrl.logger = MagicMock()
    ctrl.wake_event = MagicMock()
    ctrl._webapp_notify = None

    config = MagicMock()

    def config_get(section: str, key: str | None = None, default: Any = None) -> Any:
        if section == "General" and key == "DisableAllSwitches":
            return disable_all
        return default

    config.get.side_effect = config_get
    ctrl.config = config

    ctrl.groups = [
        {"Name": "G1", "Schedule": "sched", "AppMode": group_mode, "Switches": ["S1"]}
    ]
    ctrl.switch_states = [
        {
            "Switch": "S1",
            "Group": "G1",
            "Schedule": "sched",
            "AppMode": switch_mode,
            "Input": input_name,
        }
    ]

    view = FakeView({input_name: input_on} if input_name else {})
    worker = MagicMock()
    worker.get_latest_status.return_value = view
    events = list(webhook_events or [])
    worker.pull_webhook_event.side_effect = lambda: events.pop(0) if events else None
    ctrl.smart_device_worker = worker

    ctrl._get_schedule_by_name = lambda name: {"Name": name}  # type: ignore[method-assign]
    detail = {"state": scheduled_state, "next_change": None, "reason": schedule_reason}
    ctrl._evaluate_schedule_with_detail = (  # type: ignore[method-assign]
        lambda *_args: dict(detail)
    )
    return ctrl


def _evaluate(ctrl: LightingController) -> dict[str, Any]:
    """Run evaluation and return the single switch's resulting state.

    Args:
        ctrl: The controller to evaluate.

    Returns:
        The updated switch-state dict for "S1".
    """
    ctrl._evaluate_switch_states()
    return ctrl.switch_states[0]


# ── Priority chain ──────────────────────────────────────────────────────────


def test_group_off_webapp_switch_on_wins() -> None:
    """Group Off + webapp switch On → switch On (priority 1). Core issue #25 case."""
    state = _evaluate(make_controller(group_mode=AppMode.OFF, switch_mode=AppMode.ON))
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.WEBAPP_SWITCH_OVERRIDE
    assert state["StateReason"] == StateReasonOn.WEBAPP_SWITCH_ON


def test_group_off_input_on_wins() -> None:
    """Group Off + input On → switch On, even when the schedule is On. Core issue #25."""
    state = _evaluate(
        make_controller(
            group_mode=AppMode.OFF,
            scheduled_state="ON",
            input_name="I1",
            input_on=True,
        )
    )
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.INPUT_OVERRIDE
    assert state["StateReason"] == StateReasonOn.INPUT_SWITCH_ON


def test_group_on_webapp_switch_off_wins() -> None:
    """Group On + webapp switch Off → switch Off (priority 1)."""
    state = _evaluate(
        make_controller(
            group_mode=AppMode.ON, switch_mode=AppMode.OFF, scheduled_state="ON"
        )
    )
    assert state["DesiredState"] == "OFF"
    assert state["SystemState"] == SystemState.WEBAPP_SWITCH_OVERRIDE
    assert state["StateReason"] == StateReasonOff.WEBAPP_SWITCH_OFF


def test_input_release_falls_through_to_group_on() -> None:
    """Group On + schedule Off + input released → group On wins (no forced Off)."""
    state = _evaluate(
        make_controller(
            group_mode=AppMode.ON,
            scheduled_state="OFF",
            input_name="I1",
            input_on=False,
        )
    )
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.WEBAPP_GROUP_OVERRIDE
    assert state["StateReason"] == StateReasonOn.WEBAPP_GROUP_ON


def test_input_release_falls_through_to_group_off() -> None:
    """Group Off + input released → group Off wins."""
    state = _evaluate(
        make_controller(
            group_mode=AppMode.OFF,
            scheduled_state="ON",
            input_name="I1",
            input_on=False,
        )
    )
    assert state["DesiredState"] == "OFF"
    assert state["SystemState"] == SystemState.WEBAPP_GROUP_OVERRIDE
    assert state["StateReason"] == StateReasonOff.WEBAPP_GROUP_OFF


def test_group_auto_input_on_unchanged() -> None:
    """Group Auto + input On + schedule Off → switch On (pre-existing behaviour)."""
    state = _evaluate(
        make_controller(scheduled_state="OFF", input_name="I1", input_on=True)
    )
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.INPUT_OVERRIDE


def test_disable_all_forces_off() -> None:
    """DisableAllSwitches forces Off when no individual override is active."""
    state = _evaluate(make_controller(scheduled_state="ON", disable_all=True))
    assert state["DesiredState"] == "OFF"
    assert state["SystemState"] == SystemState.GLOBAL_OVERRIDE
    assert state["StateReason"] == StateReasonOff.GLOBAL_OVERRIDE


def test_disable_all_input_on_still_overrides() -> None:
    """An input override still beats the global DisableAllSwitches flag."""
    state = _evaluate(make_controller(disable_all=True, input_name="I1", input_on=True))
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.INPUT_OVERRIDE


def test_fresh_webhook_toggle_on_beats_stale_view() -> None:
    """A freshly-drained toggle-on wins over a polled view that still reads Off."""
    state = _evaluate(
        make_controller(
            group_mode=AppMode.OFF,
            scheduled_state="ON",
            input_name="I1",
            input_on=False,
            webhook_events=[_toggle_event("I1", on=True)],
        )
    )
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.INPUT_OVERRIDE


def test_schedule_on_when_all_clear() -> None:
    """No overrides, schedule On → switch On via the schedule (priority 5)."""
    state = _evaluate(make_controller(scheduled_state="ON"))
    assert state["DesiredState"] == "ON"
    assert state["SystemState"] == SystemState.SCHEDULED
    assert state["StateReason"] == StateReasonOn.SCHEDULED_ON


# ── Mode setters ────────────────────────────────────────────────────────────


def test_set_switch_mode_allowed_when_group_not_auto() -> None:
    """set_switch_mode now succeeds even when the group is Off (issue #25)."""
    ctrl = make_controller(group_mode=AppMode.OFF, switch_mode=AppMode.OFF)
    assert ctrl.set_switch_mode("S1", AppMode.ON) is True
    assert ctrl.switch_states[0]["AppMode"] == AppMode.ON
    ctrl.wake_event.set.assert_called()


def test_set_switch_mode_unknown_switch_returns_false() -> None:
    """set_switch_mode returns False for an unknown switch name."""
    ctrl = make_controller()
    assert ctrl.set_switch_mode("nope", AppMode.ON) is False


def test_set_group_mode_resyncs_children() -> None:
    """Changing the group mode re-syncs member switches ('group reset clears child')."""
    ctrl = make_controller(group_mode=AppMode.OFF, switch_mode=AppMode.OFF)
    # An individual override is set while the group is Off.
    ctrl.set_switch_mode("S1", AppMode.ON)
    assert ctrl.switch_states[0]["AppMode"] == AppMode.ON
    # Reverting the group to Auto clears the child override back to Auto.
    ctrl.set_group_mode("G1", AppMode.AUTO)
    assert ctrl.groups[0]["AppMode"] == AppMode.AUTO
    assert ctrl.switch_states[0]["AppMode"] == AppMode.AUTO
