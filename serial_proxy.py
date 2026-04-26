import argparse
import signal
import sys
import threading
import time
from pathlib import Path

import serial
from serial.tools import list_ports


DEFAULT_BAUD = 115200
stop_event = threading.Event()
log_lock = threading.Lock()


def hex_dump(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def ascii_dump(data: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in data)


def list_available_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return

    print("Available serial ports:")
    for port in ports:
        print(f"  {port.device}: {port.description}")


def log_line(log_handle, direction: str, data: bytes) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{timestamp}] {direction} {len(data)} bytes: "
        f"{hex_dump(data)}    |{ascii_dump(data)}|"
    )
    with log_lock:
        print(line)
        log_handle.write(line + "\n")
        log_handle.flush()


def forward(src: serial.Serial, dst: serial.Serial, direction: str, log_handle) -> None:
    while not stop_event.is_set():
        try:
            data = src.read(src.in_waiting or 1)
            if not data:
                continue

            log_line(log_handle, direction, data)
            dst.write(data)
            dst.flush()
        except serial.SerialException as exc:
            print(f"{direction} serial error: {exc}")
            stop_event.set()
        except OSError as exc:
            print(f"{direction} OS error: {exc}")
            stop_event.set()


def open_port(name: str, baud: int) -> serial.Serial:
    return serial.Serial(
        port=name,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.01,
        write_timeout=1,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bidirectional serial proxy/logger for capturing a writing machine protocol."
    )
    parser.add_argument("--virtual-port", default="COM2", help="COM port opened by official PC software.")
    parser.add_argument("--real-port", default="COM13", help="Real COM port connected to the device.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Serial baud rate.")
    parser.add_argument("--log", default="serial_log.txt", help="Path to append captured traffic.")
    parser.add_argument("--list-ports", action="store_true", help="List detected serial ports and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_ports:
        list_available_ports()
        return 0

    def handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Opening virtual side {args.virtual_port} at {args.baud} baud.")
    print(f"Opening real device {args.real_port} at {args.baud} baud.")

    try:
        with (
            open_port(args.virtual_port, args.baud) as ser_virtual,
            open_port(args.real_port, args.baud) as ser_real,
            log_path.open("a", encoding="utf-8") as log_handle,
        ):
            threads = [
                threading.Thread(
                    target=forward,
                    args=(ser_virtual, ser_real, "PC -> DEVICE", log_handle),
                    daemon=True,
                ),
                threading.Thread(
                    target=forward,
                    args=(ser_real, ser_virtual, "DEVICE -> PC", log_handle),
                    daemon=True,
                ),
            ]

            for thread in threads:
                thread.start()

            print("Serial proxy started. Press Ctrl+C to stop.")
            while not stop_event.is_set():
                time.sleep(0.2)

    except serial.SerialException as exc:
        print(f"Failed to open serial port: {exc}", file=sys.stderr)
        print("Tip: run with --list-ports and confirm the virtual COM pair exists.", file=sys.stderr)
        return 1

    print("Serial proxy stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
