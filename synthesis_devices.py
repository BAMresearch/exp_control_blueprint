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
    # TODO work in progress
    # proper limits against faulty numbers, prevent crashes and display correct values
    def __init__(self, port, diameter, max_vol=10, name="AladdinPump"):
        self.name = name
        self.volume = 0
        self.max_vol = max_vol
        self.ser = serial.Serial(port, 9600, timeout=0.5, bytesize=8, parity="N", stopbits=1)
        self.ser.write(b"VOL ML\r")
        time.sleep(0.1)
        self.ser.write(f"DIA{diameter}\r".encode())
        time.sleep(0.1)
        self.ser.write(b"CLD\r")
        time.sleep(0.1)

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
        m = re.fullmatch(r"I([0-9]*\.?[0-9]+)W([0-9]*\.?[0-9]+)(UL|ML)", data)
        if not m:
            raise ValueError(f"Unexpected DIS response: {text!r}")

        infused = float(m.group(1))
        withdrawn = float(m.group(2))
        units = m.group(3)

        return {
            "address": address,
            "status": status,
            "infused": infused,
            "withdrawn": withdrawn,
            "units": units,
        }
    
    def send_cmd(self, command):
        self.ser.reset_input_buffer()
        self.ser.write(f"{command}\r".encode())
        data = self.ser.read_until(b"\x03")
        print(repr(data))
        if str(command).upper() == "DIS":
            print(self.parse_dis_response(data))

    def run(self, vol, rate, direction):
        if vol > self.max_vol: # sanity check for upper volume limit
            print(f"{vol} ml is too much! Pump can only handle {self.max_vol} ml")
            return 0
        elif vol == 0: # 0 cannot be handled by pump's interpreter
            print("0 doesn't work")
            return 0
        # adjust object's values (even though not in real time)
        if str(direction).upper() == "WDR":
            self.volume += vol
        elif str(direction).upper() == "INF":
            self.volume -= vol
        # prevent ejection beyond lower limit
        if str(direction).upper() == "INF" and self.volume <= 0:
            self.ser.write(b"CLD\r")
            return 0
        
        safe_rate = min(rate, 95.0) # catch faulty user values
        cmds = [f"VOL{vol}", f"RAT{safe_rate} MM", f"DIR {direction}", "RUN"] # actual communication with pump
        # direction: WDR (aufziehen/withdraw) and INF (auspusten/inflate)
        for c in cmds:
            self.ser.write((c + "\r").encode())
            time.sleep(0.1)
            response = self.ser.read_until(b"\x03")
            print(response)

        duration_sec = (vol / safe_rate) * 60.0 # keeping track of pump's movement
        t_elapsed = 0
        step = 0.5 # update every 0.5s
        while t_elapsed < duration_sec:
            self.ser.write(b"DIS\r")
            time.sleep(0.1)
            response = self.ser.read_until(b"\x03")
            print("running")
            # TODO bekommt eine komische antwort und crasht hier
            #reply_dict = self.parse_dis_response(response)
            #self.volume = reply_dict["infused"] if direction == "INF" else reply_dict["withdrawn"]
            print(response)
            t_elapsed += step
            time.sleep(step)
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
    def __init__(self, diameter, name="simAladdinPump"):
        # definition "create_pump..."
        self.name = name
        self.volume = 0
        self.diameter = diameter

    def run(self, vol, rate, direction):
        safe_rate = min(rate, 95.0) # catch faulty user values
        cmds = [f"VOL{vol}", f"RAT{safe_rate} MM", f"DIR {direction}", "RUN"] # actual communication with pump
        # direction: WDR (remove/withdraw) and INF (fill/inflate)
        if direction == "WDR":
            self.volume = self.volume + vol
        elif direction == "INF":
            self.volume = self.volume - vol
        
        duration_sec = (vol / safe_rate) * 60.0 # keeping track of pump's movement
        with tqdm(total=vol, desc=f"Pump {direction}", unit="mL", leave=False, 
                  bar_format='{l_bar}{bar}| {n:.2f}/{total:.2f} mL') as pbar: # progressbar
            step = 0.2; elapsed = 0
            while elapsed < duration_sec: # progressbar
                actual_step = min(step, duration_sec - elapsed)
                time.sleep(actual_step)
                elapsed += actual_step
                step = actual_step
                pbar.update((safe_rate/60.0)*step) # aktuelles volumen während bewegung?
            if duration_sec - elapsed > 0: time.sleep(duration_sec - elapsed)
            pbar.n = vol; pbar.refresh()
    
    def stop(self):
        print("Pump stopped")

    def disconnect(self):
        print(f"Pump {self.name} disconnected!")

if __name__ == "__main__":
    alpump = AladdinPump("COM4", 26, "Alpump")
    command = input("New command:\n")
    while command != "exit":
        alpump.send_cmd(command)
        command = input("New command:\n")
