# GUI import
from PySide6.QtWidgets import QApplication
# classes from exp_conrol package
from chemistry_gui import ChemistryMainWindow, GuiContext
from exp_recipe import ExperimentRunner, ExperimentQueueRunner, build_recipe
from exp_topology import build_topology
from basic_commands import attach_data_logger
# device controller
from synthesis_devices import simAladdinPump, simViciValve
from magnetic_stirrer import MockMagneticStirrer as mokka
from Longer_3 import simPUMP
from data_logger import DataLogger
# generic classes
import time
import serial

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

valve1 = simViciValve(name="valve1")
valve2 = simViciValve(name="valve2")
#transfer_pump = simAladdinPump(diameter=26, name="pump1")
transfer_pump = simPUMP(name="pump1")
circulation_pump = simPUMP(name="pump2")
plate = mokka(name="plate1")
#logger = DataLogger(plate, "reactor_logger")
# cable = DummyCable(name="myspot_cable")

old_test = {
        "devices": {
            "valve1": {"kind": "valve", "handle": valve1},
            "valve2": {"kind": "valve", "handle": valve2},
            "pump1": {"kind": "peristaltic_pump", "handle": transfer_pump},
            "reactor": {"kind": "reactor", "volume_ml": 0, "max_volume_ml": 250},
            "plate1": {"kind": "stirrer", "handle": plate},
        },
        "endpoints": {
            "water": {"kind": "source","contents": "water","volume_ml": 1000,},
            "waste": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},
            "solutionA": {"kind": "source", "contents": "reaction solutionA", "volume_ml": 200},
            "extractionA": {"kind": "container", "volume_ml": 0, "max_volume_ml": 500},
        },
        "valve_ports": {
            "valve1": {"water": 1, "waste": 2, "reactor": 3, "solutionA": 4, "extractionA": 5,},
        },
        "connections": [
            ("pump1", "valve1", {"valve_port_role": "common"}),
            ("reactor", "plate1"),
        ],
    }

setup_1 = {
    "devices": {
    "reactor": {"kind": "reactor", "volume_ml": 0, "max_volume_ml": 250},
    "plate1": {"kind": "stirrer", "handle": plate},
    "pump1": {"kind": "peristaltic_pump", "CW_flow": (999, 120), "CCW_flow": (999, 100), "handle": transfer_pump},
    #TODO volume flow depending on tube; make calibration and store in txtfile
    "valve1": {"kind": "valve", "handle": valve1},
    # "myspot_cable": {"kind": "serial_cable", "handle": cable}
    },
    "endpoints": {
        "reaction_solution": {"kind": "source", "contents": "salty_solution","volume_ml": 200,},
        "water": {"kind": "source", "contents": "water","volume_ml": 1000,},
        "waste": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},
    },
    "valve_ports": {
        "valve1": {"reaction_solution": 1, "water": 2, "waste": 3,},
    },
    "connections": [
        ("pump1", "valve1", {"valve_port_role": "common"}),
        ("reactor", "pump1"),
        ("reactor", "plate1"),
    ],
}

setup_2 = {
    "devices": {
        "reactor": {"kind": "reactor", "volume_ml": 0, "max_volume_ml": 250},
        "flow_cell": {"kind": "capillary", "volume_ml": 0},
        "pump1": {"kind": "peristaltic_pump", "handle": transfer_pump},
        "pump2": {"kind": "peristaltic_pump", "handle": circulation_pump},
        "valve1": {"kind": "valve", "handle": valve1},
        "valve2": {"kind": "valve", "handle": valve2},
        "plate1": {"kind": "stirrer", "handle": plate},
    },
    "endpoints": {
        "water_1": {"kind": "source", "contents": "water","volume_ml": 1000,},
        "air_1": {"kind": "source", "contents": "air","volume_ml": 999999999,},
        "waste_1": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},

        "water_2": {"kind": "source", "contents": "water","volume_ml": 1000,},
        "air_2": {"kind": "source", "contents": "air","volume_ml": 999999999,},
        "waste_2": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},

        "reaction_solution": {"kind": "source", "contents": "salty_solution","volume_ml": 100,},
        #"water2": {"kind": "source", "contents": "water","volume_ml": 1000,},
        #"waste2": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},
    },
    "valve_ports": {
        "valve1": {"reactor": 1, "water_1": 2, "air_1": 3, "waste_1": 4,},
        "valve2": {"reaction_solution": 1, "water_2": 2, "waste_2": 3, "air_2": 4,},
    },
    "connections": [
        ("valve1", "pump1", {"valve_port_role": "common"}),
        ("pump1", "valve2", {"valve_port_role": "common"}),
        ("reactor", "plate1"),
        ("reactor", "pump2"),
        ("pump2", "flow_cell"),
        ("flow_cell", "reactor"),
    ],
}

old_recipe = {
        "name": "simulated_experiment",
        "steps": [
            {"action": "fill_reactor", "source": "solutionA", "ml": 100},
            {"action": "start_stirring", "hotplate": "plate1", "rpm": 500, "label": "start stirring"},
            {"action": "start_heating", "hotplate": "plate1", "temp": 50, "label": "start heating"},
            {"action": "wait", "duration_s": 10, "label": "hold warm"},
            {"action": "stop_heating", "hotplate": "plate1"},
            {"action": "wait", "duration_s": 10, "label": "cool while stirring"},
            {"action": "empty_reactor", "destination": "waste"},
            {"action": "stop_stirring", "hotplate": "plate1"},
            {"action": "cleaning", "reactor": "reactor", "cycles": 2},
        ],
    }

recipe_1 = {
        "name": "simulated_experiment_1",
        "steps": [
            {"action": "fill_reactor", "source": "reaction_solution", "ml": 50, "execute": True},
            {"action": "stir_reactor", "reactor": "reactor", "rpm": 500, "label": "start stirring"},
            {"action": "heat_reactor", "reactor": "reactor", "temp": 50, "label": "start heating"},
            {"action": "wait", "duration_s": 10, "label": "hold warm"},
            {"action": "stop_reactor_heating", "reactor": "reactor"},
            {"action": "wait", "duration_s": 10, "label": "cool while stirring"},
            {"action": "empty_reactor", "destination": "waste", "execute": True},
            {"action": "cleaning", "reactor": "reactor", "cycles": 1,
             "water_source": "water", "waste_destination": "waste", "wait_seconds": 5, "execute": True},
             #TODO cleaning needs it's own stirring parameters
            #{"action": "stop_stirring", "hotplate": "plate1"},
        ],
    }

recipe_1_2 = {
        "name": "simulated_experiment_1_2",
        "steps": [
            {"action": "fill_reactor", "source": "reaction_solution", "ml": 50, "execute": True},
            {"action": "start_stirring", "hotplate": "plate1", "rpm": 500, "label": "start stirring"},
            {"action": "start_heating", "hotplate": "plate1", "temp": 70, "label": "start heating"},
            {"action": "wait", "duration_s": 10, "label": "hold warm"},
            {"action": "stop_heating", "hotplate": "plate1"},
            {"action": "wait", "duration_s": 10, "label": "cool while stirring"},
            {"action": "empty_reactor", "destination": "waste", "execute": True},
            {"action": "stop_stirring", "hotplate": "plate1"},
            {"action": "cleaning", "reactor": "reactor", "cycles": 1,
             "water_source": "water", "waste_destination": "waste", "wait_seconds": 5, "execute": True},
        ],
    }

recipe_2 = {
        "name": "simulated_experiment_2",
        "steps": [
            {"action": "fill_reactor", "source": "reaction_solution", "ml": 100, "execute": True},
            {"action": "start_stirring", "hotplate": "plate1", "rpm": 500, "label": "start stirring"},
            {"action": "start_heating", "hotplate": "plate1", "temp": 50, "label": "start heating"},
            {"action": "wait", "duration_s": 10, "label": "hold warm"},
            {"action": "stop_heating", "hotplate": "plate1"},
            {"action": "wait", "duration_s": 10, "label": "cool while stirring"},
            {"action": "empty_reactor", "destination": "waste", "execute": True},
            {"action": "stop_stirring", "hotplate": "plate1"},
            {"action": "cleaning", "reactor": "reactor", "cycles": 2,
             "water_source": "water", "waste_destination": "waste", "wait_seconds": 5, "execute": True},
        ],
    }

TOPOLOGY = build_topology(setup_2)
SIMULATED_EXPERIMENT_A = build_recipe(recipe_1)
SIMULATED_EXPERIMENT_B = build_recipe(recipe_1_2)
SIMULATED_QUEUE = ExperimentQueueRunner([SIMULATED_EXPERIMENT_A, SIMULATED_EXPERIMENT_B],
                                        topology=TOPOLOGY, logger_hotplate="plate1")
#attach_data_logger(logger=logger, hotplate="plate1", topology=TOPOLOGY)

def run_demo() -> None:
    """runner1 = ExperimentRunner(
        SIMULATED_EXPERIMENT,
        topology=TOPOLOGY,
        wait_poll_s=0.2,
    )
    runner2 = ExperimentRunner(
        SIMULATED_EXPERIMENT,
        topology=TOPOLOGY,
        wait_poll_s=0.2,
    )"""

    app = QApplication.instance() or QApplication([])
    context = GuiContext(
        topology=TOPOLOGY,
        # experiment_runners={"sim_A": SIMULATED_EXPERIMENT_A},
        # experiment_runners={"sim_B": SIMULATED_EXPERIMENT_A},
        experiment_runners={"run_Q": SIMULATED_QUEUE},
        expert_mode=False,
    )
    window = ChemistryMainWindow(context)
    window.show()

    app.exec()
    """runner.start()
    print("Started:", runner.snapshot())

    while runner.is_active:
        print("Running:", runner.snapshot())
        time.sleep(1)

    runner.wait_until_finished(1)
    print("Finished:", runner.snapshot())

    if runner.last_error is not None:
        print(f"Recipe failed: {runner.last_error}")
    else:
        print("Recipe completed cleanly.")"""

    #TOPOLOGY.print_values(["water", "reactor", "waste", "plate1"])


if __name__ == "__main__":
    run_demo()
