# Plan — Issue #25: Cannot override a switch when group mode is not Auto

**Issue:** [Spello-Consulting/LightingControl#25](https://github.com/Spello-Consulting/LightingControl/issues/25)

## Problem

When a switch group is set to **On** or **Off** (rather than **Auto**):

- **A.** The individual child switches cannot be overridden to On/Off via the webapp
  — the child controls are disabled and the child modes are force-synced to the group.
- **B.** Any **input switch** override (physical smart-switch / webhook) is ignored.

Desired behaviour: an **individual switch override** — whether from the webapp *or*
from a physical input — must be able to override the group mode. E.g. a group set to
Off should still allow one child switch to be forced On.

## Root cause

The whole codebase encodes one assumption: *"group mode forces and locks its member
switches."* It lives in **four** places:

| # | Location | Behaviour today | Contributes to |
|---|----------|-----------------|----------------|
| 1 | `get_webapp_data` — [controller.py:174](../src/controller.py) | `group_controls_mode = group.AppMode != AUTO` → template disables child buttons | Symptom A |
| 2 | `set_switch_mode` — [controller.py:120-121](../src/controller.py) | Rejects a switch-mode change when the group is not Auto | Symptom A |
| 3 | `set_group_mode` — [controller.py:94-98](../src/controller.py) | Force-overwrites every member switch `AppMode` to the group mode | Symptom A (by design — see decision) |
| 4 | `_evaluate_switch_states` — [controller.py:543-556](../src/controller.py) | Group override is **priority 2**, input override is **priority 3** | Symptom B |

Note: the webapp **switch** override is already priority 1 (above group), so the
controller *would* honour an explicit individual switch override if it ever received
one. The blockers for Symptom A are purely #1 and #2 (the UI/API never let the
override through). Symptom B is the genuine priority-chain bug (#4).

## Design decision (agreed)

**"Group reset clears child."** Individual switch overrides win over the group mode
while both are set, but changing the group mode re-syncs *all* children to the new
group mode, clearing their per-switch overrides. This keeps `set_group_mode`'s current
"apply to all members" behaviour (#3 stays as-is) and gives the simplest mental model.

## Target priority chain for `_evaluate_switch_states`

Individual overrides outrank the group override:

| Priority | Condition | Result |
|----------|-----------|--------|
| 1 | Webapp **switch** override (`switch_mode` ON/OFF) | ON / OFF |
| 2 | **Input** override (physical / webhook) | ON (additive) / fall through |
| 3 | Webapp **group** override (`group_mode` ON/OFF) | ON / OFF |
| 4 | Global `DisableAllSwitches` | OFF |
| 5 | Schedule (DatesOff, scheduled on/off) | ON / OFF |

## Changes

### 1. `set_switch_mode` (controller.py:103-125)
Remove the `group.AppMode != AUTO` rejection (lines 120-121). Setting an individual
switch mode is now always allowed. Update the docstring ("Only has effect when the
switch's group is in AUTO mode" is no longer true).

### 2. `get_webapp_data` (controller.py:174)
Child controls should no longer be disabled by group mode. Either drop
`group_controls_mode` (always `False`) or remove the field and the template's use of
it. The child mode buttons stay enabled at all times. (When a group mode is set, the
child modes are re-synced by `set_group_mode`, so the buttons still visually reflect
the group mode — consistent with the agreed model.)

### 3. `home.html` (lines 59-72)
Follow #2: stop emitting `disabled` / `.disabled` from `group_controls_mode`.

### 4. `_evaluate_switch_states` (controller.py:534-579) — the priority chain
Reorder so **input** is evaluated **before** group, and generalise the input-ON
firing condition so it also fires when the group forces the baseline off:

- Introduce `baseline_off = scheduled_state == "OFF" or group_mode == AppMode.OFF or DisableAllSwitches`.
- **Input ON** (`input_state == "ON" and baseline_off`) → force ON, `INPUT_OVERRIDE` /
  `INPUT_SWITCH_ON`. This now wins over a group set to Off (the reported case).
- **Input OFF (release):** the current branch (line 557) hard-codes `DesiredState = OFF`.
  Moving input above group makes that unsafe — with `group_mode == ON, schedule OFF,
  input OFF` it would wrongly force OFF over the group's ON. The input-OFF case must
  **fall through** to the lower priorities (group → global → schedule) instead of
  forcing OFF, so the release reverts to whatever the baseline now dictates. Preserve
  the existing webhook-event guard so a fresh toggle event isn't stomped in the same tick.
- Group ON/OFF, global, and schedule branches follow unchanged, just lower in the chain.

`SystemState` / `StateReason` enums in [enumerations.py](../src/enumerations.py) already
cover all these cases — no new values needed.

## Edge cases to verify

1. Group Off + webapp switch On → switch ON (priority 1). ✅ core ask
2. Group Off + input On → switch ON (priority 2, via `baseline_off`). ✅ core ask
3. Group On + webapp switch Off → switch OFF (priority 1).
4. Group On + schedule Off + input Off (release) → switch ON (falls through to group On).
5. Group Auto + input On + schedule Off → switch ON (unchanged from today).
6. Change group Auto→Off then back to Auto → all children re-sync to Auto (decision).
7. `DisableAllSwitches` still forces OFF unless a webapp switch/input override is active
   — confirm intended interaction (today input-ON already overrides DisableAllSwitches).

## Tests

There is currently **no** coverage of `_evaluate_switch_states` or the mode setters
(only `tests/test_webapp_access_key.py`). Add `tests/test_switch_evaluation.py`
(pytest) covering the priority table and edge cases 1-7 above, plus `set_switch_mode`
now succeeding when the group is non-Auto, and `set_group_mode` re-syncing children.

## Out of scope

- The broader "child sticks until Auto'd" model was considered and rejected in favour
  of "group reset clears child".
- No changes to schedule evaluation, device I/O, or webhook draining beyond the
  input-OFF fall-through above.
