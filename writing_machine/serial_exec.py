from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Callable

import serial
from serial.tools import list_ports

from .models import MachineConfig, SerialProxyConfig


LogCallback = Callable[[str], None]
DoneCallback = Callable[[bool, str], None]


def available_ports() -> list[str]:
    return [port.device for port in list_ports.comports()]


def hex_dump(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def ascii_dump(data: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in data)


def open_proxy_port(name: str, baud: int) -> serial.Serial:
    return serial.Serial(
        port=name,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.01,
        write_timeout=1,
    )


class SerialProxyRunner:
    def __init__(
        self,
        config: SerialProxyConfig,
        on_log: LogCallback | None = None,
        on_done: DoneCallback | None = None,
    ) -> None:
        self.config = config
        self.on_log = on_log or (lambda _msg: None)
        self.on_done = on_done or (lambda _ok, _msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_lock = threading.Lock()
        self._virtual: serial.Serial | None = None
        self._real: serial.Serial | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for port in (self._virtual, self._real):
            try:
                if port and port.is_open:
                    port.close()
            except Exception:
                pass

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _write_log_line(self, log_handle, direction: str, data: bytes) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {direction} {len(data)} bytes: {hex_dump(data)}    |{ascii_dump(data)}|"
        with self._log_lock:
            self.on_log(line)
            log_handle.write(line + "\n")
            log_handle.flush()

    def _forward(self, src: serial.Serial, dst: serial.Serial, direction: str, log_handle) -> None:
        while not self._stop.is_set():
            try:
                data = src.read(src.in_waiting or 1)
                if not data:
                    continue
                self._write_log_line(log_handle, direction, data)
                dst.write(data)
                dst.flush()
            except (serial.SerialException, OSError) as exc:
                if not self._stop.is_set():
                    self.on_log(f"{direction} 转发失败：{exc}")
                self._stop.set()
                return

    def _run(self) -> None:
        try:
            log_path = Path(self.config.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.on_log(f"打开虚拟端 {self.config.virtual_port} @ {self.config.baud}")
            self.on_log(f"打开真实设备 {self.config.real_port} @ {self.config.baud}")
            with (
                open_proxy_port(self.config.virtual_port, self.config.baud) as ser_virtual,
                open_proxy_port(self.config.real_port, self.config.baud) as ser_real,
                log_path.open("a", encoding="utf-8") as log_handle,
            ):
                self._virtual = ser_virtual
                self._real = ser_real
                threads = [
                    threading.Thread(target=self._forward, args=(ser_virtual, ser_real, "PC -> DEVICE", log_handle), daemon=True),
                    threading.Thread(target=self._forward, args=(ser_real, ser_virtual, "DEVICE -> PC", log_handle), daemon=True),
                ]
                for thread in threads:
                    thread.start()
                self.on_log("串口代理已启动。")
                while not self._stop.is_set():
                    time.sleep(0.2)
            self.on_done(True, "串口代理已停止。")
        except Exception as exc:
            self.on_done(False, f"串口代理失败：{exc}")
        finally:
            self._virtual = None
            self._real = None


class GCodeRunner:
    def __init__(
        self,
        lines: list[str],
        config: MachineConfig,
        on_log: LogCallback | None = None,
        on_done: DoneCallback | None = None,
        dry_run: bool = False,
    ) -> None:
        self.lines = lines
        self.config = config
        self.on_log = on_log or (lambda _msg: None)
        self.on_done = on_done or (lambda _ok, _msg: None)
        self.dry_run = dry_run
        self._pause = threading.Event()
        self._pause.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial: serial.Serial | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._pause.clear()
        self.on_log("暂停：停止继续发送新命令，已请求抬笔。")
        self._write_immediate(self.config.pen_up)

    def resume(self) -> None:
        self._pause.set()
        self.on_log("继续发送。")

    def stop(self) -> None:
        self._stop.set()
        self._pause.set()
        self.on_log("终止：停止队列并请求抬笔。")
        self._write_immediate(self.config.pen_up)

    def _write_immediate(self, command: str) -> None:
        try:
            if self._serial and self._serial.is_open:
                self._serial.write((command + "\n").encode("ascii"))
                self._serial.flush()
        except Exception as exc:
            self.on_log(f"立即命令发送失败：{exc}")

    def _run(self) -> None:
        try:
            if self.dry_run:
                for index, line in enumerate(self.lines, start=1):
                    if self._stop.is_set():
                        self.on_done(False, "已终止 dry-run。")
                        return
                    self._pause.wait()
                    self.on_log(f"[DRY {index}/{len(self.lines)}] {line}")
                    time.sleep(0.003)
                self.on_done(True, "dry-run 完成。")
                return

            with serial.Serial(
                self.config.port,
                self.config.baud,
                timeout=0.2,
                write_timeout=1,
            ) as ser:
                self._serial = ser
                self.on_log(f"已连接 {self.config.port} @ {self.config.baud}")
                for index, line in enumerate(self.lines, start=1):
                    if self._stop.is_set():
                        self.on_done(False, "已终止。")
                        return
                    self._pause.wait()
                    self.on_log(f"[{index}/{len(self.lines)}] -> {line}")
                    ser.write((line + "\n").encode("ascii"))
                    ser.flush()
                    response = self._wait_ok(ser)
                    self.on_log(f"<- {response}")
                self.on_done(True, "执行完成。")
        except Exception as exc:
            self.on_done(False, f"执行失败：{exc}")
        finally:
            self._serial = None

    def _wait_ok(self, ser: serial.Serial) -> str:
        buffer = bytearray()
        while not self._stop.is_set():
            chunk = ser.read(1)
            if not chunk:
                continue
            buffer.extend(chunk)
            text = buffer.decode("ascii", errors="ignore")
            if "ok" in text.lower() or "error" in text.lower():
                return text.strip()
        return "stopped"
