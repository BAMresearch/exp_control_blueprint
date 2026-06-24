# Dev Notes

## Core Idea

Humans define the setup as data in `exp_topology.py`.
Commands in `basic_commands.py` operate from that topology.

## Important Files

- `exp_topology.py`: graph model and pathfinding
- `basic_commands.py`: liquid, reactor, hotplate, circulation commands
- `simulated_setup.py`: working simulated demo
- `test_basic_commands.py`, `test_exp_topology.py`: regression tests

## Topology Rules

- Valve `leaf` = selectable numbered port
- Valve `common` = always-connected side, usually pump side
- Peristaltic pump = single-pass transfer
- Syringe pump = load, switch valve, eject

## Pump Direction

Peristaltic direction can be declared in connections:

```python
("pump1", "valveB", {"pump_direction": "CW"})
```

Meaning: moving from first node to second node requires CW rotation.

## Command Entry Points

- `move_liquid(...)`
- `fill_reactor(...)`
- `empty_reactor(...)`
- `cleaning(...)`
- `start_heating(...)`
- `heat_for_time(...)`
- `start_stirring(...)`
- `start_circulation(...)`

## Hotplate Pattern

Hotplate commands can take:

- topology node name, e.g. `"plate1"`
- direct device handle

After hotplate commands, topology metadata is synced from the real/simulated
plate handle.

## Circulation Pattern

Circulation uses its own peristaltic pump instance, not `move_liquid()`.

Current simulated loop:

```text
reactor -> circulation_pump -> measurement_cell -> reactor
```

## Current Simulated Devices

- `pump1`: transfer pump
- `circulation_pump`: loop pump
- `valveA`
- `plate1`
- `measurement_cell`
- `reactor`

## Common Metadata

- `volume_ml`
- `max_volume_ml`
- `contents`

Hotplate metadata:

- `target_temperature`
- `probe_temperature`
- `hotplate_sensor_temperature`
- `target_stir_rate`
- `stir_rate`
- `heating`
- `stirring`

## Sanity Checks

```powershell
python -m unittest test_basic_commands.py test_exp_topology.py
python -m py_compile basic_commands.py exp_topology.py simulated_setup.py test_basic_commands.py test_exp_topology.py
python simulated_setup.py
```
