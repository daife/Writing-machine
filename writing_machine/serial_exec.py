from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Callable

import serial
from serial.tools import list_ports

from .models import MachineConfig


LogCallback = Callable[[str], None]
DoneCallback = Callable[[bool, str], None]


def available_ports() -> list[str]:
    return [port.device for port in list_ports.comports()]


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

