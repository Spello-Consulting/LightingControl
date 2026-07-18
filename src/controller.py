import copy
import datetime as dt
import json
import operator
import random
import re
from collections.abc import Callable
from pathlib import Path
from threading import Event, RLock

from sc_foundation import (
    DateHelper,
    JSONEncoder,
    SCCommon,
    SCConfigManager,
    SCLogger,
)
from sc_smart_device import (
    DeviceSequenceRequest,
    DeviceStep,
    SmartDeviceWorker,
    StepKind,
)

from enumerations import AppMode, StateReasonOff, StateReasonOn, SystemState
from post_state_to_web_viewer import post_state_to_web_viewer

SCHEMA_VERSION = 2  # Version of the system_state schema we expect
WEEKDAY_ABBREVIATIONS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TRIM_LOGFILE_INTERVAL = dt.timedelta(hours=2)

# Keys that must be present in a valid state file
_REQUIRED_STATE_KEYS = {"SchemaVersion", "StateFileType", "RandomOffsets", "SwitchEvents"}


class LightingController:
    def __init__(self, config: SCConfigManager, logger: SCLogger, smart_device_worker: SmartDeviceWorker, wake_event: Event):
        self.config = config
        self.logger = logger
        self.logger_last_trim: dt.datetime | None = None
        self.dusk_dawn = {}
        self.groups: list[dict] = []        # Config-level groups with member switches
        self.switch_states: list[dict] = []  # Per-switch state, includes group and webapp metadata
        self.config_last_check = DateHelper.now()
        self.wake_event = wake_event
        self.smart_device_worker = smart_device_worker

        # Webapp notifier callback — set by main after webapp is created
        self._webapp_notify: Callable[[], None] | None = None

        # Protects groups and switch_states for webapp reads/writes
        self._state_lock = RLock()

        # Internal maps (rebuilt by _initialise)
        self._schedule_map: dict[str, str] = {}   # switch_name -> schedule_name
        self._input_map: dict[str, str | None] = {}  # switch_name -> input_name | None

        self.state_filepath: Path | None = None
        self.offset_cache: dict = {}
        self.switch_events: list = []
        self.check_interval = 5

        self._initialise()
        self._evaluate_switch_states()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_webapp_notifier(self, notify: Callable[[], None] | None) -> None:
        """Set the webapp notifier callback. The webapp will call this to provide a callback that triggers a WS broadcast when notify() is called.

        Args:
            notify: A callable that takes no arguments and returns None. The controller will call this to trigger a webapp update when state changes. If None, disables notifications.
        """
        self._webapp_notify = notify

    def set_group_mode(self, group_name: str, mode: AppMode) -> bool:
        """Set the webapp override mode for a group. Thread-safe.

        When a group mode is set to ON or OFF, all member switch modes are
        set to match and disabled. When reverted to AUTO, member switches are
        also set to AUTO.

        Args:
            group_name: The name of the group to update.
            mode: The AppMode to set for the group.

        Returns:
            True if the group was found.
        """
        with self._state_lock:
            group = self._find_group(group_name)
            if group is None:
                return False
            group["AppMode"] = mode
            for sw_name in group["Switches"]:
                sw = self._find_switch_state(sw_name)
                if sw is not None:
                    sw["AppMode"] = mode
        self._notify_webapp()
        self.wake_event.set()
        return True

    def set_switch_mode(self, switch_name: str, mode: AppMode) -> bool:
        """Set the webapp override mode for an individual switch. Thread-safe.

        Only has effect when the switch's group is in AUTO mode.

        Args:
            switch_name: The name of the switch to update.
            mode: The AppMode to set for the switch.

        Returns:
            True if the switch was found.
        """
        with self._state_lock:
            sw = self._find_switch_state(switch_name)
            if sw is None:
                return False
            group = self._find_group(sw.get("Group", ""))
            if group and group["AppMode"] != AppMode.AUTO:
                return False  # Group override takes precedence; client should not allow this
            sw["AppMode"] = mode
        self._notify_webapp()
        self.wake_event.set()
        return True

    def is_valid_group(self, group_name: str) -> bool:
        """Check if a group name is valid. Thread-safe.

        Args:
            group_name: The name of the group to check.

        Returns:
            True if the group name is valid, False otherwise.
        """
        with self._state_lock:
            return self._find_group(group_name) is not None

    def is_valid_switch(self, switch_name: str) -> bool:
        """Check if a switch name is valid. Thread-safe.

        Args:
            switch_name: The name of the switch to check.

        Returns:
            True if the switch name is valid, False otherwise.
        """
        with self._state_lock:
            return self._find_switch_state(switch_name) is not None

    def get_webapp_data(self) -> dict:
        """Return a snapshot of the current state for the webapp. Thread-safe.

        Returns:
            A dictionary representing the current state, including groups and switches with their modes and states.
        """
        with self._state_lock:
            groups_out = {}
            for group in self.groups:
                g_name = group["Name"]
                switches_out = {}
                for sw_name in group["Switches"]:
                    sw = self._find_switch_state(sw_name)
                    if sw is None:
                        continue
                    is_on = sw.get("OutputState") == "ON"
                    switches_out[sw_name] = {
                        "name": sw_name,
                        "id": _make_id(sw_name),
                        "is_on": is_on,
                        "mode": str(sw.get("AppMode", AppMode.AUTO)),
                        "system_state": str(sw.get("SystemState", "")),
                        "reason": str(sw.get("StateReason", "")),
                        "group_controls_mode": group["AppMode"] != AppMode.AUTO,
                    }
                groups_out[g_name] = {
                    "name": g_name,
                    "id": _make_id(g_name),
                    "schedule": group.get("Schedule", ""),
                    "scheduled_state": group.get("ScheduledState", ""),
                    "next_change": _fmt_time(group.get("NextChange")),
                    "mode": str(group["AppMode"]),
                    "switches": switches_out,
                }
            return {"groups": groups_out}

    def shutdown(self):
        """Shutdown the controller, performing any necessary cleanup."""
        self.logger.log_message("Shutting down Lighting Controller...", "summary")
        self._save_state()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _initialise(self):
        """Initialise (or re-initialise) controller state from config."""
        self.dusk_dawn = self._get_dusk_dawn_times()
        self._build_groups_and_maps()
        state_file = self.config.get("Files", "SavedStateFile", default="system_state.json")
        self.state_filepath = SCCommon.select_file_location(state_file)  # type: ignore[attr-defined]
        self._load_state()
        self.check_interval = self.config.get("General", "CheckInterval", default=5) or 5
        self.logger.log_message("Lighting Controller initialised successfully.", "debug")

    def _get_dusk_dawn_times(self) -> dict:
        """Get the dawn and dusk times based on the location returned from the specified shelly switch or the manually configured location configuration.

        Returns:
            dict: A dictionary with 'dawn' and 'dusk' times.
        """
        name = "LightingControl"  # noqa: F841
        loc_conf = self.config.get("Location", default={})
        assert isinstance(loc_conf, dict), "Location configuration must be a dictionary"

        astral_info = DateHelper.dawn_dusk_times(loc_conf)   # Issue 80

        return_obj = {
            "dawn": astral_info["dawn"].time(),
            "dusk": astral_info["dusk"].time(),
        }
        return return_obj

    def _build_groups_and_maps(self):  # noqa: PLR0912, PLR0914, PLR0915
        """Build self.groups, self._schedule_map, and self._input_map from config.

        Preserves existing AppMode overrides when called during reload.
        """
        view = self.smart_device_worker.get_latest_status()

        # Build device-level group map: group_name -> [switch_names]
        # Read Group directly from config — _normalize_component only copies known
        # fields, so custom keys like Group are absent from the view snapshot.
        device_group_map: dict[str, list[str]] = {}
        smart_devices_cfg = self.config.get("SCSmartDevices", default={})
        for device in (smart_devices_cfg or {}).get("Devices", []):
            for out_cfg in device.get("Outputs", []):
                out_name: str | None = out_cfg.get("Name")
                dev_group: str | None = out_cfg.get("Group")
                if out_name and dev_group:
                    device_group_map.setdefault(dev_group, []).append(out_name)

        all_output_names: set[str] = {out["Name"] for out in view.snapshot.outputs if "Name" in out}

        # Capture existing AppMode overrides before rebuilding
        old_group_modes: dict[str, AppMode] = {}
        old_switch_modes: dict[str, AppMode] = {}
        with self._state_lock:
            for g in self.groups:
                old_group_modes[g["Name"]] = g.get("AppMode", AppMode.AUTO)
            for sw in self.switch_states:
                old_switch_modes[sw["Switch"]] = sw.get("AppMode", AppMode.AUTO)

        lighting_controls = self.config.get("LightingControl", default=[])
        if not isinstance(lighting_controls, list):
            lighting_controls = []

        # Build a full schedule assignment: switch_name -> schedule_name
        default_schedule = next(
            (c["Schedule"] for c in lighting_controls if c.get("Type", "").lower() == "default"),
            None,
        )
        schedule_map: dict[str, str | None] = dict.fromkeys(all_output_names)

        new_groups: list[dict] = []

        # Default group — collects any switch not explicitly assigned below
        default_group: dict = {
            "Name": "Default",
            "Schedule": default_schedule or "",
            "Type": "default",
            "AppMode": old_group_modes.get("Default", AppMode.AUTO),
            "ScheduledState": "",
            "NextChange": None,
            "Switches": [],
        }

        for control in lighting_controls:
            ctrl_type = control.get("Type", "").lower()
            target = control.get("Target")
            schedule = control.get("Schedule", "")

            if ctrl_type == "default":
                continue
            elif ctrl_type == "switch":  # noqa: RET507
                if target in schedule_map:
                    if schedule_map[target] is None:
                        schedule_map[target] = schedule
                    else:
                        self.logger.log_message(f"⚠️ Switch '{target}' already assigned to a schedule", "warning")
                group: dict = {
                    "Name": target,
                    "Schedule": schedule,
                    "Type": "switch",
                    "AppMode": old_group_modes.get(target, AppMode.AUTO),
                    "ScheduledState": "",
                    "NextChange": None,
                    "Switches": [target] if target else [],
                }
                new_groups.append(group)
            elif ctrl_type == "switch group":
                members = device_group_map.get(target, [])
                for sw in members:
                    if sw in schedule_map:
                        if schedule_map[sw] is None:
                            schedule_map[sw] = schedule
                        else:
                            self.logger.log_message(f"⚠️ Switch '{sw}' (in group '{target}') already assigned", "warning")
                group = {
                    "Name": target,
                    "Schedule": schedule,
                    "Type": "switch group",
                    "AppMode": old_group_modes.get(target, AppMode.AUTO),
                    "ScheduledState": "",
                    "NextChange": None,
                    "Switches": members,
                }
                new_groups.append(group)

        # Fill unassigned switches with the default schedule and add to Default group
        for sw_name, sched in schedule_map.items():
            if sched is None:
                schedule_map[sw_name] = default_schedule
                default_group["Switches"].append(sw_name)

        if default_group["Switches"]:
            new_groups.insert(0, default_group)

        # Build input map
        input_map: dict[str, str | None] = dict.fromkeys(all_output_names)
        input_controls = self.config.get("InputControls", default=[])
        if isinstance(input_controls, list):
            default_input = None
            for control in input_controls:
                ctrl_type = control.get("Type", "").lower()
                target = control.get("Target")
                input_name = control.get("Input")
                if ctrl_type == "default":
                    default_input = input_name
                    continue
                if ctrl_type == "switch" and target in input_map:
                    if input_map[target] is None:
                        input_map[target] = input_name
                elif ctrl_type == "switch group":
                    for sw in device_group_map.get(target, []):
                        if sw in input_map and input_map[sw] is None:
                            input_map[sw] = input_name
            if default_input:
                for sw in input_map:  # noqa: PLC0206
                    if input_map[sw] is None:
                        input_map[sw] = default_input

        # Build the switch_states list, preserving existing AppMode values
        new_switch_states: list[dict] = []
        for group in new_groups:
            for sw_name in group["Switches"]:
                new_switch_states.append({
                    "Switch": sw_name,
                    "Group": group["Name"],
                    "Schedule": schedule_map.get(sw_name, default_schedule) or "",
                    "ScheduledState": "",
                    "NextChange": None,
                    "Input": input_map.get(sw_name),
                    "InputState": None,
                    "OutputState": None,
                    "AppMode": old_switch_modes.get(sw_name, AppMode.AUTO),
                    "SystemState": SystemState.SCHEDULED,
                    "StateReason": StateReasonOff.SCHEDULED_OFF,
                })

        with self._state_lock:
            self.groups = new_groups
            self._schedule_map = {sw: sched for sw, sched in schedule_map.items() if sched}  # type: ignore[misc]
            self._input_map = input_map
            self.switch_states = new_switch_states

    def _load_state(self) -> bool:
        """Load random offsets and switch events from the saved state file.

        Returns:
            True if loaded successfully.
        """
        assert isinstance(self.state_filepath, Path), "State file path must be a Path object"
        if not self.state_filepath.exists():
            return False
        try:
            state = JSONEncoder.read_from_file(self.state_filepath)
            if not isinstance(state, dict):
                return False

            # Schema validation: check required keys are present
            if not _REQUIRED_STATE_KEYS.issubset(state.keys()):
                missing = _REQUIRED_STATE_KEYS - state.keys()
                self.logger.log_message(f"State file missing required keys {missing}, ignoring.", "warning")
                return False

            schema_version = state.get("SchemaVersion")
            if not schema_version or schema_version < SCHEMA_VERSION:
                self.logger.log_fatal_error(f"State file schema version {schema_version} does not match expected {SCHEMA_VERSION}.")

            if state.get("StateFileType") != "LightingControl":
                self.logger.log_fatal_error(f"Invalid state file type '{state.get('StateFileType')}', cannot load {self.state_filepath}")

            self.offset_cache = state.get("RandomOffsets", {})
            self.switch_events = state.get("SwitchEvents", [])

            # Initialise the group app modes from the saved state
            saved_groups = state.get("Groups", [])
            if isinstance(saved_groups, list):
                for saved_group in saved_groups:
                    group_name = saved_group.get("Name")
                    group_mode = saved_group.get("AppMode", AppMode.AUTO)
                    self.set_group_mode(group_name, group_mode)

            # Now initialise the switch_states from the saved state
            saved_switches = state.get("SwitchStates", [])
            if isinstance(saved_switches, list):
                for saved_switch in saved_switches:
                    switch_name = saved_switch.get("Switch")
                    switch_state = saved_switch.get("AppMode", AppMode.AUTO)
                    self.set_switch_mode(switch_name, switch_state)

        except json.JSONDecodeError as e:
            self.logger.log_fatal_error(f"Failed to load state file {self.state_filepath}: {e}")
        else:
            return True
        return False

    def _save_state(self):
        """Save current state (random offsets, switch events) to the state file."""
        self._trim_switch_events()
        assert isinstance(self.state_filepath, Path), "State file path must be a Path object"
        try:
            config_schedules = self.config.get("Schedules", default=[])
            schedules = copy.deepcopy(config_schedules) if isinstance(config_schedules, list) else []

            with self._state_lock:
                switch_states_copy = copy.deepcopy(self.switch_states)
                groups_copy = copy.deepcopy(self.groups)

            state_data = {
                "SchemaVersion": SCHEMA_VERSION,
                "StateFileType": "LightingControl",
                "LastStateSaveTime": DateHelper.now(),
                "DeviceType": "LightingController",
                "DeviceName": self.config.get("General", "AppName", default="LightingControl"),
                "LastStatusMessage": "State saved successfully.",
                "Dawn": self.dusk_dawn.get("dawn"),  # type: ignore  # noqa: PGH003
                "Dusk": self.dusk_dawn.get("dusk"),  # type: ignore  # noqa: PGH003
                "RandomOffsets": self.offset_cache,
                "SwitchStates": switch_states_copy,
                "Groups": groups_copy,
                "Schedules": schedules,
                "SwitchEvents": self.switch_events,
            }
            JSONEncoder.save_to_file(state_data, self.state_filepath)
        except (OSError, TypeError, ValueError, RuntimeError) as e:
            self.logger.log_fatal_error(f"Failed to save state file: {e}")
            return

        post_state_to_web_viewer(self.config, self.logger, state_data)

    def _trim_switch_events(self):
        """Trim switch events to the last N days."""
        max_days = self.config.get("Files", "MaxDaysSwitchChangeHistory", default=30)
        assert isinstance(max_days, int), "MaxDaysSwitchChangeHistory must be an integer"
        assert max_days > 0, "MaxDaysSwitchChangeHistory must be a positive integer"
        cutoff_date = DateHelper.today_add_days(-max_days)
        self.switch_events = [d for d in self.switch_events if d["Date"] >= cutoff_date]
        self.switch_events.sort(key=operator.itemgetter("Date"))
        for day_event in self.switch_events:
            day_event["Events"].sort(key=operator.itemgetter("Time"))

    # ── Schedule evaluation ───────────────────────────────────────────────────

    def _evaluate_switch_states(self) -> list:  # noqa: PLR0912, PLR0915
        """Evaluate desired state for every switch using the 4-level priority chain.

        Priority (highest first):
          1. Webapp switch override (AppMode.ON / OFF)
          2. Webapp group override
          3. Input switch override
          4. Global override (DisableAllSwitches)
          5. Scheduled state

        Updates self.switch_states in-place with ScheduledState, SystemState,
        StateReason, InputState, and NextChange. Does NOT change physical devices.

        Returns:
            The updated switch_states list.
        """
        now = DateHelper.now()
        weekday_str = WEEKDAY_ABBREVIATIONS[now.weekday()]
        view = self.smart_device_worker.get_latest_status()

        with self._state_lock:
            # First pass: update per-group ScheduledState and NextChange
            for group in self.groups:
                schedule_name = group.get("Schedule", "")
                schedule = self._get_schedule_by_name(schedule_name)
                if schedule:
                    detail = self._evaluate_schedule_with_detail(schedule, now, weekday_str)
                    group["ScheduledState"] = detail["state"]
                    group["NextChange"] = detail.get("next_change")

            # Second pass: evaluate each switch
            for state in self.switch_states:
                sw_name = state["Switch"]
                schedule_name = state["Schedule"]
                schedule = self._get_schedule_by_name(schedule_name)
                if not schedule:
                    self.logger.log_fatal_error(f"Schedule '{schedule_name}' not found for switch '{sw_name}'.")
                    continue

                detail = self._evaluate_schedule_with_detail(schedule, now, weekday_str)
                scheduled_state = detail["state"]
                state["ScheduledState"] = scheduled_state
                state["NextChange"] = detail.get("next_change")

                # Read input state from view
                input_name = state.get("Input")
                input_state: str | None = None
                if input_name:
                    input_id = view.get_input_id(input_name)
                    if input_id:
                        input_state = "ON" if view.get_input_state(input_id) else "OFF"
                state["InputState"] = input_state

                # Read webhook events for this switch's input
                webhook_event = self._drain_webhook_events_for(input_name)

                group = self._find_group(state.get("Group", ""))
                group_mode = group["AppMode"] if group else AppMode.AUTO
                switch_mode = state.get("AppMode", AppMode.AUTO)

                # Priority 1: webapp switch override
                if switch_mode == AppMode.ON:
                    state["SystemState"] = SystemState.WEBAPP_SWITCH_OVERRIDE
                    state["StateReason"] = StateReasonOn.WEBAPP_SWITCH_ON
                    state["DesiredState"] = "ON"
                elif switch_mode == AppMode.OFF:
                    state["SystemState"] = SystemState.WEBAPP_SWITCH_OVERRIDE
                    state["StateReason"] = StateReasonOff.WEBAPP_SWITCH_OFF
                    state["DesiredState"] = "OFF"
                # Priority 2: webapp group override
                elif group_mode == AppMode.ON:
                    state["SystemState"] = SystemState.WEBAPP_GROUP_OVERRIDE
                    state["StateReason"] = StateReasonOn.WEBAPP_GROUP_ON
                    state["DesiredState"] = "ON"
                elif group_mode == AppMode.OFF:
                    state["SystemState"] = SystemState.WEBAPP_GROUP_OVERRIDE
                    state["StateReason"] = StateReasonOff.WEBAPP_GROUP_OFF
                    state["DesiredState"] = "OFF"
                # Priority 3: input override
                elif input_state == "ON" and (scheduled_state == "OFF" or self.config.get("General", "DisableAllSwitches")):
                    state["SystemState"] = SystemState.INPUT_OVERRIDE
                    state["StateReason"] = StateReasonOn.INPUT_SWITCH_ON
                    state["DesiredState"] = "ON"
                elif input_state == "OFF" and webhook_event is None and scheduled_state == "OFF" and detail.get("reason") != "DatesOff" and not self.config.get("General", "DisableAllSwitches"):
                    # Input went OFF and schedule is OFF: revert
                    state["SystemState"] = SystemState.SCHEDULED
                    state["StateReason"] = StateReasonOff.SCHEDULED_OFF
                    state["DesiredState"] = "OFF"
                # Priority 4: global override (issue 18)
                elif self.config.get("General", "DisableAllSwitches"):
                    state["SystemState"] = SystemState.GLOBAL_OVERRIDE
                    state["StateReason"] = StateReasonOff.GLOBAL_OVERRIDE
                    state["DesiredState"] = "OFF"
                # Priority 5: schedule
                elif detail.get("reason") == "DatesOff":
                    state["SystemState"] = SystemState.DATE_OFF
                    state["StateReason"] = StateReasonOff.DATE_OFF
                    state["DesiredState"] = "OFF"
                elif scheduled_state == "ON":
                    state["SystemState"] = SystemState.SCHEDULED
                    state["StateReason"] = StateReasonOn.SCHEDULED_ON
                    state["DesiredState"] = "ON"
                else:
                    state["SystemState"] = SystemState.SCHEDULED
                    state["StateReason"] = StateReasonOff.SCHEDULED_OFF
                    state["DesiredState"] = "OFF"

        return self.switch_states

    def _drain_webhook_events_for(self, input_name: str | None) -> str | None:
        """Pull all pending webhook events and return the last relevant one for input_name.

        Args:
            input_name: The name of the input to check for events.

        Returns:
            "ON", "OFF", or None if no relevant event was found.
        """
        last_event = None
        while True:
            event = self.smart_device_worker.pull_webhook_event()
            if not event:
                break
            event_input = event.get("Component", {}).get("Name")
            if event_input == input_name:
                if event.get("Event") == "input.toggle_on":
                    last_event = "ON"
                elif event.get("Event") == "input.toggle_off":
                    last_event = "OFF"
        return last_event

    def _refresh_device_status(self) -> bool:
        """Refresh all device statuses via the worker.

        Returns:
            bool: True on success, False on failure.
        """
        req_id = self.smart_device_worker.request_refresh_status()
        if not self.smart_device_worker.wait_for_result(req_id, timeout=30.0):
            self.logger.log_message("Device status refresh timed out.", "error")
            return False
        return True

    def _change_switch_states(self):
        """Apply CHANGE_OUTPUT requests for any switch whose physical state differs from desired.

        Assumes _refresh_device_status() has already been called this tick so
        the view reflects current hardware state.
        """
        view = self.smart_device_worker.get_latest_status()

        with self._state_lock:
            for state in self.switch_states:
                sw_name = state["Switch"]
                desired = state.get("DesiredState", state.get("ScheduledState", "OFF"))

                output_id = view.get_output_id(sw_name)
                if not output_id:
                    self.logger.log_message(f"Output '{sw_name}' not found in device view.", "warning")
                    continue

                device_id = view.get_output_device_id(output_id)
                if not view.get_device_online(device_id):
                    self.logger.log_message(f"Switch '{sw_name}' is offline, skipping.", "debug")
                    state["OutputState"] = None
                    state["SystemState"] = SystemState.SCHEDULED
                    state["StateReason"] = StateReasonOff.DEVICE_OFFLINE
                    continue

                current = "ON" if view.get_output_state(output_id) else "OFF"
                state["OutputState"] = current

                if desired != current:
                    req = DeviceSequenceRequest(
                        steps=[DeviceStep(StepKind.CHANGE_OUTPUT, {"output_identity": sw_name, "state": desired == "ON"})],
                        label=f"set {sw_name} {desired}",
                    )
                    req_id = self.smart_device_worker.submit(req)
                    done = self.smart_device_worker.wait_for_result(req_id, timeout=10.0)
                    if done:
                        state["OutputState"] = desired
                        self.logger.log_message(
                            f"Changed '{sw_name}' from {current} to {desired} "
                            f"({state['SystemState']}: {state['StateReason']})", "detailed"
                        )
                        self._record_switch_event(
                            switch=sw_name,
                            state=desired,
                            schedule_name=state.get("Schedule"),
                            input_name=state.get("Input"),
                        )
                    else:
                        self.logger.log_message(f"Timed out changing switch '{sw_name}'.", "error")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self, stop_event: Event | None = None):
        """Run the controller loop until stop_event is set.

        Args:
            stop_event: Optional threading.Event to signal when to stop the loop. If None, runs indefinitely until externally interrupted.
        """
        assert isinstance(self.check_interval, int), "CheckInterval must be an integer"
        assert self.check_interval > 0, "CheckInterval must be a positive integer"

        while stop_event is None or not stop_event.is_set():
            time_now = DateHelper.now()
            console_msg = f"Main tick at {time_now.strftime('%H:%M:%S')}"
            self._print_to_console(console_msg)

            config_timestamp = self.config.check_for_config_changes(self.config_last_check)
            if config_timestamp:
                self._reload_config()

            self._summarise_schedule_evaluations()
            if not self._refresh_device_status():
                self.wake_event.clear()
                self.wake_event.wait(timeout=self.check_interval)
                continue
            self._evaluate_switch_states()
            self._change_switch_states()
            self._summarise_switch_states()
            self._save_state()
            self._notify_webapp()

            self.wake_event.clear()
            self.wake_event.wait(timeout=self.check_interval)

            if not self.logger.ping_heartbeat():
                self.logger.log_message("Heartbeat ping failed.", "error")
            self._trim_logfile_if_needed()

        self.shutdown()

    def _reload_config(self):
        """Re-apply updated configuration settings."""
        self.logger.log_message("Reloading configuration...", "detailed")
        try:
            logger_settings = self.config.get_logger_settings()
            self.logger.initialise_settings(logger_settings)
            email_settings = self.config.get_email_settings()
            if email_settings:
                self.logger.register_email_settings(email_settings)
            smart_switch_settings = self.config.get("SCSmartDevices")
            if smart_switch_settings is None:
                self.logger.log_fatal_error("No smart device settings found in the configuration file.")
                return
            assert isinstance(smart_switch_settings, dict)
            self.smart_device_worker.reinitialise_settings(device_settings=smart_switch_settings)
        except RuntimeError as e:
            self.logger.log_fatal_error(f"Error reloading configuration: {e}")
            return
        else:
            self._initialise()
            self.config_last_check = DateHelper.now()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_group(self, name: str) -> dict | None:
        """Find a group by name. Thread-safe if called with _state_lock held.

        Args:
            name: The name of the group to find.

        Returns:
            The group dict if found, or None if not found.
        """
        for g in self.groups:
            if g["Name"] == name:
                return g
        return None

    def _find_switch_state(self, switch_name: str) -> dict | None:
        """Find a switch state by switch name. Thread-safe if called with _state_lock held.

        Args:
            switch_name: The name of the switch to find.

        Returns:
            The switch state dict if found, or None if not found.
        """
        for sw in self.switch_states:
            if sw["Switch"] == switch_name:
                return sw
        return None

    def _notify_webapp(self):
        """Notify the web application of state changes."""
        if self._webapp_notify:
            self._webapp_notify()

    def _get_schedule_by_name(self, name: str) -> dict | None:
        """Get a schedule by name.

        Args:
            name: The name of the schedule to find.

        Returns:
            The schedule dict if found, or None if not found.
        """
        schedules = self.config.get("Schedules", default=[])
        if not isinstance(schedules, list):
            return None
        for schedule in schedules:
            if schedule.get("Name") == name:
                return schedule
        return None

    def _summarise_schedule_evaluations(self):
        """Summarise the evaluations of all schedules."""
        if not self._schedule_map:
            return
        unique_schedules = set(self._schedule_map.values())
        now = DateHelper.now()
        weekday_str = WEEKDAY_ABBREVIATIONS[now.weekday()]
        evaluations = []
        for schedule_name in sorted(unique_schedules):
            schedule = self._get_schedule_by_name(schedule_name)
            if schedule:
                detail = self._evaluate_schedule_with_detail(schedule, now, weekday_str)
                if detail["state"] == "ON":
                    evaluations.append(f"{schedule_name}: {detail['state']} (event {detail.get('event_idx')}: {detail.get('on_time')}-{detail.get('off_time')})")
                else:
                    evaluations.append(f"{schedule_name}: {detail['state']}")
        if evaluations:
            self.logger.log_message(f"Schedule evaluations - {', '.join(evaluations)}", "debug")

    def _summarise_switch_states(self):
        """Summarise the current states of all switches."""
        if not self.switch_states:
            return
        with self._state_lock:
            summaries = [
                f"   {s['Switch']} ({s['SystemState']}"
                + (f": {s['Schedule']}" if s.get("SystemState") == SystemState.SCHEDULED and s.get("Schedule") else "")
                + f"): {s.get('OutputState', '?')}"
                for s in self.switch_states
            ]
        self.logger.log_message(f"Current switch states - \n{'\n'.join(summaries)}", "debug")

    def _evaluate_schedule_with_detail(self, schedule: dict, now: dt.datetime, weekday_str: str) -> dict:
        """Evaluate a schedule and return state details including next change time.

        Args:
            schedule: The schedule dict to evaluate.
            now: The current datetime.
            weekday_str: The current weekday as a string (e.g. "Mon").

        Returns:
            Dict with keys: state ("ON"/"OFF"), next_change (dt.time | None), reason (str | None).
        """
        for idx, event in enumerate(schedule.get("Events", [])):
            days = event.get("DaysOfWeek", "All")
            if days != "All" and weekday_str not in [d.strip() for d in days.split(",")]:
                continue

            dates_off_list = event.get("DatesOff", [])
            if dates_off_list:
                for rng in dates_off_list:
                    try:
                        start = rng["StartDate"]
                        end = rng["EndDate"]
                        if start <= DateHelper.today() <= end:
                            return {"state": "OFF", "reason": "DatesOff"}
                    except (KeyError, TypeError) as e:
                        self.logger.log_message(f"Invalid DatesOff range {rng}: {e}", "error")
                        continue

            on_time = self._parse_time(event["TurnOn"], event.get("RandomOffset"), schedule["Name"], idx, "On")
            off_time = self._parse_time(event["TurnOff"], event.get("RandomOffset"), schedule["Name"], idx, "Off")

            if on_time is None or off_time is None:
                continue

            if on_time < off_time:
                if on_time <= now.time() < off_time:
                    return {
                        "state": "ON", "event_idx": idx, "on_time": on_time, "off_time": off_time,
                        "next_change": off_time,
                    }
            elif now.time() >= on_time or now.time() < off_time:
                return {
                    "state": "ON", "event_idx": idx, "on_time": on_time, "off_time": off_time,
                    "next_change": off_time,
                }

        # Determine next ON time
        next_on = self._find_next_on_time(schedule, now, weekday_str)
        return {"state": "OFF", "reason": "no matching events", "next_change": next_on}

    def _find_next_on_time(self, schedule: dict, now: dt.datetime, weekday_str: str) -> dt.time | None:
        """Return the next TurnOn time for today's schedule, or None if none found.

        Args:
            schedule: The schedule dict to evaluate.
            now: The current datetime.
            weekday_str: The current weekday as a string (e.g. "Mon").

        Returns:
            The next TurnOn time as a datetime.time object, or None if no future ON events are found for today.
        """
        candidates = []
        for event in schedule.get("Events", []):
            days = event.get("DaysOfWeek", "All")
            if days != "All" and weekday_str not in [d.strip() for d in days.split(",")]:
                continue
            on_time = self._parse_time(event["TurnOn"], event.get("RandomOffset"), schedule["Name"], 0, "On")
            if on_time and on_time > now.time():
                candidates.append(on_time)
        return min(candidates) if candidates else None

    def _print_to_console(self, message: str):
        """Print a message to the console if PrintToConsole is enabled.

        Args:
            message (str): The message to print.
        """
        if self.config.get("General", "PrintToConsole", default=False):
            print(message)

    @staticmethod
    def _generate_offset_key(schedule_name: str, event_index: int, mode: str) -> str:
        """Generate a unique key for caching random offsets based on schedule, event, and mode.

        Args:
            schedule_name: The name of the schedule.
            event_index: The index of the event within the schedule.
            mode: "On" or "Off" to differentiate between turn-on and turn-off offsets.

        Returns:
            A string key for caching the random offset.
        """
        today_str = DateHelper.today().isoformat()
        return f"{today_str}|{schedule_name}|{event_index}|{mode}"

    def _parse_time(self, time_str, offset_minutes, schedule_name, event_index, mode) -> dt.time | None:
        """Parse a time string (HH:MM, dawn[±HH:MM], dusk[±HH:MM]) and apply random offset.

        Args:
            time_str: The time string to parse.
            offset_minutes: The maximum random offset in minutes (int or None).
            schedule_name: The name of the schedule (for logging).
            event_index: The index of the event within the schedule (for logging).
            mode: "On" or "Off" to differentiate between turn-on and turn-off times (for logging).

        Returns:
            The resolved time, or None if parsing fails.
        """
        local_tz = dt.datetime.now().astimezone().tzinfo

        if not self.dusk_dawn:
            self.logger.log_fatal_error(f"Dawn/Dusk times not set for schedule '{schedule_name}', event {event_index}")
            return None

        if time_str.lower().startswith(("dawn", "dusk")):
            if time_str.lower().startswith("dawn"):
                base_time = self.dusk_dawn["dawn"]
                offset_part = time_str[4:]
            else:
                base_time = self.dusk_dawn["dusk"]
                offset_part = time_str[4:]

            if offset_part:
                try:
                    match = re.match(r"^([+-])(\d{2}):(\d{2})$", offset_part)
                    if match:
                        sign, hours, minutes = match.groups()
                        total_minutes = int(hours) * 60 + int(minutes)
                        if sign == "-":
                            total_minutes = -total_minutes
                        base_datetime = dt.datetime.combine(DateHelper.today(), base_time)
                        base_time = (base_datetime + dt.timedelta(minutes=total_minutes)).time()
                    else:
                        self.logger.log_fatal_error(f"Invalid dawn/dusk offset in '{schedule_name}': '{time_str}'")
                        return None
                except (ValueError, TypeError, OSError):
                    self.logger.log_fatal_error(f"Invalid dawn/dusk offset in '{schedule_name}': '{time_str}'")
                    return None
        else:
            try:
                base_time = dt.datetime.strptime(time_str, "%H:%M").replace(tzinfo=local_tz).time()
            except ValueError:
                self.logger.log_fatal_error(f"Invalid time format in '{schedule_name}': '{time_str}'")
                return None

        if offset_minutes:
            key = self._generate_offset_key(schedule_name, event_index, mode)
            if key not in self.offset_cache:
                self.offset_cache[key] = random.randint(-offset_minutes, offset_minutes)
            return (dt.datetime.combine(DateHelper.today(), base_time) + dt.timedelta(minutes=self.offset_cache[key])).time()
        return base_time

    def _record_switch_event(self, switch: str, state: str, schedule_name: str | None = None, input_name: str | None = None, webhook_state: str | None = None):
        """Record a switch state change event in the switch_events log.

        Args:
            switch: The name of the switch that changed.
            state: The new state of the switch ("ON" or "OFF").
            schedule_name: The name of the schedule that triggered the change, if applicable.
            input_name: The name of the input that triggered the change, if applicable.
            webhook_state: The state from a webhook event that triggered the change, if applicable.
        """
        event_date = DateHelper.today()
        for day_event in self.switch_events:
            if day_event["Date"] == event_date:
                day_event["Events"].append({
                    "Time": DateHelper.now().time(),
                    "Switch": switch,
                    "Schedule": schedule_name,
                    "Input": input_name,
                    "Webhook": webhook_state,
                    "State": state,
                })
                return
        self.switch_events.append({
            "Date": event_date,
            "Events": [{
                "Time": DateHelper.now().time(),
                "Switch": switch,
                "Schedule": schedule_name,
                "Input": input_name,
                "Webhook": webhook_state,
                "State": state,
            }],
        })

    def _trim_logfile_if_needed(self) -> None:
        """Trim the logfile if the configured interval has passed since the last trim."""
        if not self.logger_last_trim or (DateHelper.now() - self.logger_last_trim) >= TRIM_LOGFILE_INTERVAL:
            self.logger.trim_logfile()
            self.logger_last_trim = DateHelper.now()
            self.logger.log_message("Logfile trimmed.", "debug")


# ── Module helpers ─────────────────────────────────────────────────────────────

def _make_id(name: str) -> str:
    """Convert a display name to a URL-safe lowercase identifier.

    Returns:
        Lowercase string with non-alphanumeric characters replaced by hyphens.
    """
    return re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")


def _fmt_time(t: dt.time | None) -> str:
    """Format a time object as HH:MM, or return empty string if None.

    Args:
        t: The time object to format.

    Returns:
        A string representing the time in HH:MM format, or an empty string if t is None.
    """
    if t is None:
        return ""
    return t.strftime("%H:%M")
