# this is an example implementation of a setup with simulated devices.
# it can be run as is, to test python compatibilities of your system
# or can be used as a blueprint and be connected to actual hardware 
# as a starting point for your experiments.

# GUI import
from PySide6.QtWidgets import QApplication
# classes from exp_conrol package
from chemistry_gui import ChemistryMainWindow, GuiContext
from exp_recipe import ExperimentRunner, ExperimentQueueRunner, build_recipe
from exp_topology import build_topology
from basic_commands import attach_data_logger
#simulated device controller
from synthesis_devices import simViciValve
from magnetic_stirrer import MockMagneticStirrer as mokka
from Longer_3 import simPUMP
from data_logger import DataLogger


class DummyCable:
    """
    creation of a decoy cable object for testing purposes
    
    Args:
        name (str): device name for references
    """
    def __init__(self, name):
        self.name = name
        self.cts = True
        self.rts = False
    def __getattr__(self, name):
        def method(*args, **kwargs):
            print(f"I'm a DummyCable!\nFunction '{name}' called with args: {args} and kwargs: {kwargs}")
        return method

# 2 multi-way valves for the simulated topology
valve1 = simViciValve(name="valve1")
valve2 = simViciValve(name="valve2")
# one peristaltic pump for liquid transfer
transfer_pump = simPUMP(name="pump1")
# another peristaltic pump for measurements (cirulating liquid through the cell)
circulation_pump = simPUMP(name="pump2")
# hotplate connected to the reactor
plate = mokka(name="plate1")
# artifical trigger cable for data synchronization. it will not do anything here, but act as a placeholder
trigger_cable = DummyCable(name="myspot_cable")
# optional: a data logger for the hotplate with custom settings: name, logging interval, hardware refresh rate
# and the attached trigger cable
logger = DataLogger(plate, "reactor_logger", 3, 20, cable=trigger_cable)

# first example topology; featuring one device of each kind
setup_1 = {
    "devices": {
    "reactor": {"kind": "reactor", "volume_ml": 0, "max_volume_ml": 250},
    "plate1": {"kind": "stirrer", "handle": plate},
    "pump1": {"kind": "peristaltic_pump", "CW_flow": (999, 120), "CCW_flow": (999, 100), "handle": transfer_pump},
    "valve1": {"kind": "valve", "handle": valve1},
    "trigger_cable": {"kind": "serial_cable", "handle": trigger_cable}
    },
    "endpoints": {
        "reaction_solution": {"kind": "source", "contents": "brine_solution","volume_ml": 200,},
        "water": {"kind": "source", "contents": "water", "volume_ml": 1000,},
        "waste": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},
        "air": {"kind": "source", "contents": "air", "volume_ml": 10000}
    },
    "valve_ports": {
        "valve1": {"reaction_solution": 1, "water": 2, "waste": 3, "air": 4,},
    },
    "connections": [
        ("pump1", "valve1", {"valve_port_role": "common"}),
        # port role common indicates a connection to the center-port of the valve; this is not a possible endpoint
        ("reactor", "pump1"),
        ("reactor", "plate1"),
    ],
}

# second example topology; featuring all initiated devices
setup_2 = {
    "devices": {
        "reactor": {"kind": "reactor", "volume_ml": 0, "max_volume_ml": 250},
        "flow_cell": {"kind": "capillary", "volume_ml": 0},
        "pump1": {"kind": "peristaltic_pump", "handle": transfer_pump},
        "pump2": {"kind": "peristaltic_pump", "handle": circulation_pump},
        "valve1": {"kind": "valve", "handle": valve1},
        "valve2": {"kind": "valve", "handle": valve2},
        "plate1": {"kind": "stirrer", "handle": plate},
        "trigger_cable": {"kind": "serial_cable", "handle": trigger_cable}
    },
    "endpoints": {
        "water_1": {"kind": "source", "contents": "water","volume_ml": 1000,},
        "air_1": {"kind": "source", "contents": "air","volume_ml": 10000,},
        "waste_1": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},

        "water_2": {"kind": "source", "contents": "water","volume_ml": 1000,},
        "air_2": {"kind": "source", "contents": "air","volume_ml": 10000,},
        "waste_2": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},

        "reaction_solution": {"kind": "source", "contents": "brine_solution","volume_ml": 100,},
    },
    "valve_ports": {
        "valve1": {"reactor": 1, "water_1": 2, "air_1": 3, "waste_1": 4,},
        "valve2": {"reaction_solution": 1, "water_2": 2, "air_2": 3, "waste_2": 4,},
    },
    "connections": [
        ("valve1", "pump1", {"valve_port_role": "common"}),
        ("pump1", "valve2", {"valve_port_role": "common"}),
        # the pump is connected to both valves via the center port
        # this enables paths from all endpoints of one valve to all others
        ("reactor", "plate1"),
        ("reactor", "pump2"),
        ("pump2", "flow_cell"),
        ("flow_cell", "reactor"),
    ],
}

# first example experiment procedure; to be used in conjunction with setup_1
recipe_1 = {
        "name": "simulated_experiment_1",
        "steps": [
            {"action": "fill_reactor", "source": "reaction_solution", "ml": 50, "execute": True},
            {"action": "start_data_logging", "hotplate": "plate1"},
            {"action": "stir_reactor", "reactor": "reactor", "rpm": 500, "label": "start stirring"},
            {"action": "heat_reactor", "reactor": "reactor", "temp": 50, "label": "start heating"},
            {"action": "wait", "duration_s": 10, "label": "hold warm"},
            {"action": "stop_reactor_heating", "reactor": "reactor"},
            {"action": "wait", "duration_s": 10, "label": "cool while stirring"},
            {"action": "stop_data_logging", "hotplate": "plate1"},
            {"action": "cleaning", "reactor": "reactor", "cycles": 1,
             "water_source": "water", "waste_destination": "waste", "wait_seconds": 5, "execute": True},
        ],
    }

# second example experiment; to be used in conjunction with setup_2
recipe_2 = {
        "name": "simulated_experiment_2",
        "steps": [
            {"action": "fill_reactor", "source": "reaction_solution", "ml": 100, "execute": True},
            {"action": "start_circulation", "pump": "pump2", "duration_hours": 1},
            {"action": "start_data_logging", "hotplate": "plate1"},
            {"action": "start_stirring", "hotplate": "plate1", "rpm": 500, "label": "start stirring"},
            {"action": "start_heating", "hotplate": "plate1", "temp": 50, "label": "start heating"},
            {"action": "wait", "duration_s": 10, "label": "hold warm"},
            {"action": "stop_heating", "hotplate": "plate1"},
            {"action": "wait", "duration_s": 10, "label": "cool while stirring"},
            {"action": "stop_data_logging", "hotplate": "plate1"},
            {"action": "cleaning", "reactor": "reactor", "cycles": 2,
             "water_source": "water_2", "waste_destination": "waste_2", "wait_seconds": 5, "execute": True},
        ],
    }

TOPOLOGY = build_topology(setup_2)
SIMULATED_EXPERIMENT_A = build_recipe(recipe_1)
SIMULATED_EXPERIMENT_B = build_recipe(recipe_2)
# objects created from the written dictionaries (instructions made "readable" for the runners)
RECIPE_1_RUNNER = ExperimentRunner(SIMULATED_EXPERIMENT_A, topology=TOPOLOGY)
RECIPE_2_RUNNER = ExperimentRunner(SIMULATED_EXPERIMENT_B, topology=TOPOLOGY)
RECIPE_QUEUE = ExperimentQueueRunner([SIMULATED_EXPERIMENT_A, SIMULATED_EXPERIMENT_A],
                                        topology=TOPOLOGY, logger_hotplate="plate1")
# mutiple experiments can be assembled in a queue and ran back to back if needed
attach_data_logger(logger=logger, hotplate="plate1", topology=TOPOLOGY)
# attaching the logger; this is optional as every hotplate will be equipped with a default data logger.
# however, if you want specific parameters for logging data or include the trigger cable,
# this is how it should be done.

def run_demo() -> None:
    # runners can be defined here to automatically start experiments at launch.
    # recipes can be loaded into the GUI and be safely started from there making this workflow optional
    """runner1 = ExperimentRunner(
        SIMULATED_EXPERIMENT_A,
        topology=TOPOLOGY,
        wait_poll_s=0.2,
    )
    runner2 = ExperimentRunner(
        SIMULATED_EXPERIMENT_B,
        topology=TOPOLOGY,
        wait_poll_s=0.2,
    )"""

    app = QApplication.instance() or QApplication([])
    context = GuiContext(
        topology=TOPOLOGY,
        experiment_runners={"run_B": RECIPE_2_RUNNER,},
        # decide by commenting which experiments you want to run. single recipes need to be
        # started manually and individually while a Q will run through all recipes from start to finish

        # experiment_runners={"run_Q": RECIPE_QUEUE},
        expert_mode=False,
    )
    window = ChemistryMainWindow(context)
    window.show()

    app.exec()
    # isntantly launching recipes after loading the GUI. again: this is not necessary if you
    # have access to the GUI.
    """runner1.start()
    print("Started:", runner1.snapshot())

    while runner1.is_active:
        print("Running:", runner1.snapshot())
        time.sleep(1)

    runner1.wait_until_finished(1)
    print("Finished:", runner1.snapshot())

    if runner1.last_error is not None:
        print(f"Recipe failed: {runner1.last_error}")
    else:
        print("Recipe completed cleanly.")"""


if __name__ == "__main__":
    run_demo()
