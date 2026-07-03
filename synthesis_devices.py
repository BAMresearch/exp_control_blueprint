import time
import serial
from tqdm import tqdm
import re


class ViciActuator:
    def __init__(self, port, name="Vici_Valve"):
        self.name = name
        self.position = 0
        self.ser = serial.Serial(port, 9600, timeout=0.5)
        self.ser.write(b"AM3\r"); time.sleep(0.1)
        self.ser.write(b"NP10\r"); time.sleep(0.1)

    def move_to_position(self, position):
        if position not in range(1, 11):
            print(f"Only valve position from 1 to 10 are valid! {position} is not ok")
            return 0
        time.sleep(1)
        self.ser.write(f"GO{position}\r".encode())
        self.position = position
        time.sleep(1.5)

    def disconnect(self):
        self.ser.close()

class simViciValve:
    def __init__(self, name="simVici_Valve"):
        self.name = name
        self.position = 0

    def move_to_position(self, position):
        if position not in range(1, 11):
            print(f"Only valve position from 1 to 10 are valid! {position} is not ok")
            return 0
        time.sleep(1)
        self.position = position
        time.sleep(1.5)

    def disconnect(self):
        print(f"ViciValve {self.name} disconnected!")

class AladdinPump:
    def __init__(self, port, diameter, max_vol=10, name="AladdinPump", initial_volume=0):
        self.name = name
        self.max_vol = float(max_vol)
        self.volume = float(initial_volume)
        self.total_infused_ml = 0.0
        self.total_withdrawn_ml = 0.0
        self.last_dis_response = None
        self.dis_readings = []
        if not 0 <= self.volume <= self.max_vol:
            raise ValueError(
                f"Initial pump volume must be between 0 and {self.max_vol} ml, "
                f"got {self.volume} ml"
            )
        self.ser = serial.Serial(port, 9600, timeout=0.5, bytesize=8, parity="N", stopbits=1)
        self.ser.write(b"VOL ML\r")
        time.sleep(0.1)
        self.ser.write(f"DIA{diameter}\r".encode())
        time.sleep(0.1)
        self.ser.write(b"CLD\r")
        time.sleep(0.1)

    @property
    def position(self):
        return self.volume

    @position.setter
    def position(self, value):
        target_volume = float(value)
        if not 0 <= target_volume <= self.max_vol:
            raise ValueError(
                f"Pump position must be between 0 and {self.max_vol} ml, "
                f"got {target_volume} ml"
            )
        self.volume = target_volume

    def _normalize_direction(self, direction):
        normalized = str(direction).upper()
        if normalized not in {"WDR", "INF"}:
            return None
        return normalized

    def _planned_volume(self, vol, direction):
        if direction == "WDR":
            return self.volume + vol
        return self.volume - vol

    def can_run(self, vol, rate, direction):
        try:
            target_vol = float(vol)
            target_rate = float(rate)
        except (TypeError, ValueError):
            return False, "Pump volume and rate must be numbers"

        normalized_direction = self._normalize_direction(direction)
        if normalized_direction is None:
            return False, f"Syringe pump direction must be 'WDR' or 'INF', got {direction!r}"
        if target_vol <= 0:
            return False, "Pump volume must be greater than 0 ml"
        if target_rate <= 0:
            return False, "Pump rate must be greater than 0 ml/min"

        planned_volume = self._planned_volume(target_vol, normalized_direction)
        if planned_volume > self.max_vol:
            return (
                False,
                f"{target_vol} ml would exceed pump capacity: "
                f"{self.volume} + {target_vol} = {planned_volume} ml "
                f"> {self.max_vol} ml",
            )
        if planned_volume < 0:
            return (
                False,
                f"{target_vol} ml would move below pump position 0: "
                f"{self.volume} - {target_vol} = {planned_volume} ml",
            )

        return True, ""

    def parse_dis_response(self, resp: bytes):
        # strip STX/ETX
        if resp.startswith(b"\x02"):
            resp = resp[1:]
        if resp.endswith(b"\x03"):
            resp = resp[:-1]
        # disect bytes
        text = resp.decode("ascii")
        address = text[:2]
        status = text[2]
        data = text[3:]
        # locate interesting bits
        m = re.fullmatch(r"I([0-9]+\.[0-9]{2})W([0-9]+\.[0-9]{2})ML", data)
        if not m:
            raise ValueError(f"Unexpected DIS response: {text!r}")

        infused = float(m.group(1))
        withdrawn = float(m.group(2))
        units = "ML"

        return {
            "address": address,
            "status": status,
            "infused": infused,
            "withdrawn": withdrawn,
            "units": units,
        }

    def update_dis_totals(self, resp: bytes):
        dis_response = self.parse_dis_response(resp)
        self.total_infused_ml = dis_response["infused"]
        self.total_withdrawn_ml = dis_response["withdrawn"]
        self.last_dis_response = dis_response
        self.dis_readings.append(dis_response)
        return dis_response
    
    def send_cmd(self, command):
        self.ser.reset_input_buffer()
        self.ser.write(f"{command}\r".encode())
        data = self.ser.read_until(b"\x03")
        print(repr(data))
        if str(command).upper() == "DIS":
            print(self.update_dis_totals(data))

    def run(self, vol, rate, direction):
        approved, message = self.can_run(vol, rate, direction)
        if not approved:
            print(message)
            return 0

        target_vol = float(vol)
        safe_rate = min(float(rate), 95.0) # catch faulty user values
        direction = self._normalize_direction(direction)

        cmds = [f"VOL{target_vol}", f"RAT{safe_rate} MM", f"DIR {direction}", "RUN"] # actual communication with pump
        # direction: WDR (aufziehen/withdraw) and INF (auspusten/inflate)
        for c in cmds:
            self.ser.write((c + "\r").encode())
            time.sleep(0.1)
            response = self.ser.read_until(b"\x03")
            # print(response)

        # adjust object's values (even though not in real time)
        if direction == "WDR":
            self.volume += target_vol
        elif direction == "INF":
            self.volume -= target_vol

        duration_sec = (target_vol / safe_rate) * 60.0 # keeping track of pump's movement
        t_elapsed = 0
        step = 1.0 # update DIS counters every second
        while t_elapsed < duration_sec:
            self.ser.write(b"DIS\r")
            time.sleep(0.1)
            response = self.ser.read_until(b"\x03")
            self.update_dis_totals(response)
            actual_step = min(step, duration_sec - t_elapsed)
            t_elapsed += actual_step
            time.sleep(actual_step)
            # print(f"vol vorher: {self.volume}")
            # if str(direction).upper() == "WDR":
            #     self.volume += (safe_rate/60.0)*t_elapsed
            # elif str(direction).upper() == "INF":
            #     self.volume -= (safe_rate/60.0)*t_elapsed
            # print(f"vol danach: {self.volume}")
            
        # with tqdm(total=vol, desc=f"Pump {direction}", unit="mL", leave=False, 
        #           bar_format='{l_bar}{bar}| {n:.2f}/{total:.2f} mL') as pbar: # progressbar
        #     step = 0.2; elapsed = 0
        #     while elapsed < duration_sec: # progressbar
        #         time.sleep(step)
        #         elapsed += step
        #         pbar.update((safe_rate/60.0)*step)
        #     if duration_sec - elapsed > 0: time.sleep(duration_sec - elapsed)
        #     pbar.n = vol; pbar.refresh()
    
    def stop(self):
        self.ser.write(b"STP\r")

    def disconnect(self):
        # "disconnect"
        self.ser.close()

class simAladdinPump:
    def __init__(
        self,
        diameter,
        name="simAladdinPump",
        max_vol=10,
        initial_volume=0,
        simulate_time=True,
    ):
        # definition "create_pump..."
        self.name = name
        self.volume = float(initial_volume)
        self.max_vol = float(max_vol)
        self.diameter = diameter
        self.simulate_time = simulate_time
        self.command_history = []
        if not 0 <= self.volume <= self.max_vol:
            raise ValueError(
                f"Initial pump volume must be between 0 and {self.max_vol} ml, "
                f"got {self.volume} ml"
            )

    @property
    def position(self):
        return self.volume

    @position.setter
    def position(self, value):
        target_volume = float(value)
        if not 0 <= target_volume <= self.max_vol:
            raise ValueError(
                f"Pump position must be between 0 and {self.max_vol} ml, "
                f"got {target_volume} ml"
            )
        self.volume = target_volume

    def _normalize_direction(self, direction):
        normalized = str(direction).upper()
        if normalized not in {"WDR", "INF"}:
            return None
        return normalized

    def _planned_volume(self, vol, direction):
        if direction == "WDR":
            return self.volume + vol
        return self.volume - vol

    def can_run(self, vol, rate, direction):
        try:
            target_vol = float(vol)
            target_rate = float(rate)
        except (TypeError, ValueError):
            return False, "Pump volume and rate must be numbers"

        normalized_direction = self._normalize_direction(direction)
        if normalized_direction is None:
            return False, f"Syringe pump direction must be 'WDR' or 'INF', got {direction!r}"
        if target_vol <= 0:
            return False, "Pump volume must be greater than 0 ml"
        if target_rate <= 0:
            return False, "Pump rate must be greater than 0 ml/min"

        planned_volume = self._planned_volume(target_vol, normalized_direction)
        if planned_volume > self.max_vol:
            return (
                False,
                f"{target_vol} ml would exceed pump capacity: "
                f"{self.volume} + {target_vol} = {planned_volume} ml "
                f"> {self.max_vol} ml",
            )
        if planned_volume < 0:
            return (
                False,
                f"{target_vol} ml would move below pump position 0: "
                f"{self.volume} - {target_vol} = {planned_volume} ml",
            )

        return True, ""

    def run(self, vol, rate, direction):
        approved, message = self.can_run(vol, rate, direction)
        if not approved:
            print(message)
            return 0

        target_vol = float(vol)
        safe_rate = min(float(rate), 95.0) # catch faulty user values
        direction = self._normalize_direction(direction)
        cmds = [f"VOL{target_vol}", f"RAT{safe_rate} MM", f"DIR {direction}", "RUN"] # actual communication with pump
        self.command_history.extend(cmds)
        # direction: WDR (remove/withdraw) and INF (fill/inflate)
        if direction == "WDR":
            self.volume = self.volume + target_vol
        elif direction == "INF":
            self.volume = self.volume - target_vol
        
        duration_sec = (target_vol / safe_rate) * 60.0 # keeping track of pump's movement
        if not self.simulate_time:
            return 1

        with tqdm(total=target_vol, desc=f"Pump {direction}", unit="mL", leave=False, 
                  bar_format='{l_bar}{bar}| {n:.2f}/{total:.2f} mL') as pbar: # progressbar
            step = 0.2; elapsed = 0
            while elapsed < duration_sec: # progressbar
                actual_step = min(step, duration_sec - elapsed)
                time.sleep(actual_step)
                elapsed += actual_step
                step = actual_step
                pbar.update((safe_rate/60.0)*step) # aktuelles volumen während bewegung?
            if duration_sec - elapsed > 0: time.sleep(duration_sec - elapsed)
            pbar.n = target_vol; pbar.refresh()
    
    def stop(self):
        print("Pump stopped")

    def disconnect(self):
        print(f"Pump {self.name} disconnected!")

if __name__ == "__main__":
    # alpump = simAladdinPump(26, "Alpump", 50, 0, False)
    alpump = AladdinPump("COM25", 26, 60 ,"Alpump", 50)
    command = 0
    while command != "exit":
        command = input("WDR")
        alpump.run(command, 95, "WDR")
        print(alpump.volume)
        command = input("INF")
        alpump.run(command, 95, "INF")
        print(alpump.volume)
