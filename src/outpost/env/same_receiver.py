from __future__ import annotations

import asyncio
import math
import os
import shutil
from array import array
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from outpost.clock import Clock
from outpost.config import SameConfig

from .same import SameService

ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


class SameReceiverError(RuntimeError):
    pass


class SameReceiver:
    """Supervise the rtl_fm → samedec pipeline without involving a shell."""

    def __init__(
        self,
        service: SameService,
        config: SameConfig,
        clock: Clock,
        *,
        process_factory: ProcessFactory | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        self.service, self.config, self.clock = service, config, clock
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._on_progress = on_progress
        self._rtl: asyncio.subprocess.Process | None = None
        self._decoder: asyncio.subprocess.Process | None = None
        self._audio_event = asyncio.Event()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._last_progress_at = 0.0
        self.state = "disabled" if not config.enabled else "stopped"
        self.started_at: int | None = None
        self.next_restart_at: int | None = None
        self.restart_count = 0
        self.last_exit_code: int | None = None
        self.last_error: str | None = None

    @staticmethod
    def _executable(command: str) -> str:
        if os.sep in command:
            path = Path(command)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise SameReceiverError(f"decoder executable is unavailable: {command}")
            return str(path)
        resolved = shutil.which(command)
        if resolved is None:
            raise SameReceiverError(f"decoder executable is unavailable: {command}")
        return resolved

    def commands(self) -> tuple[list[str], list[str]]:
        rtl = [
            self._executable(self.config.rtl_fm_path),
            "-d",
            self.config.device,
            "-f",
            f"{self.config.frequency_mhz:.3f}M",
            "-M",
            "fm",
            "-s",
            str(self.config.sample_rate),
            "-o",
            str(self.config.oversampling),
            "-E",
            "dc",
        ]
        if self.config.gain_db is not None:
            rtl.extend(["-g", f"{self.config.gain_db:g}"])
        rtl.extend(
            [
                "-p",
                str(self.config.ppm),
                "-F",
                "9",
                "-",
            ]
        )
        decoder = [
            self._executable(self.config.samedec_path),
            "--rate",
            str(self.config.sample_rate),
        ]
        return rtl, decoder

    def _progress(self, *, force: bool = False) -> None:
        now = self.clock.monotonic()
        if force or now - self._last_progress_at >= 5:
            self._last_progress_at = now
            if self._on_progress is not None:
                self._on_progress()

    async def run(self) -> None:
        if not self.config.enabled:
            self.state = "disabled"
            await asyncio.Event().wait()
        self.service.start_monitoring()
        failures = 0
        while True:
            self.state = "starting"
            self.started_at = int(self.clock.now().timestamp())
            self.next_restart_at = None
            self._progress(force=True)
            started = self.clock.monotonic()
            try:
                await self._run_once()
                raise SameReceiverError("decoder pipeline exited unexpectedly")
            except asyncio.CancelledError:
                self.state = "stopped"
                await asyncio.shield(self._stop_processes())
                raise
            except Exception as error:
                await self._stop_processes()
                if self.clock.monotonic() - started >= 60:
                    failures = 0
                failures += 1
                self.restart_count += 1
                delay = min(
                    self.config.restart_initial_seconds * (2 ** min(failures - 1, 16)),
                    self.config.restart_max_seconds,
                )
                self.last_error = str(error)[:240] or type(error).__name__
                self.next_restart_at = int(self.clock.now().timestamp()) + delay
                self.state = "backoff"
                print(
                    f"Outpost SAME receiver restart in {delay}s: {self.last_error}",
                    flush=True,
                )
                self._progress(force=True)
                try:
                    await self.clock.sleep(delay)
                except asyncio.CancelledError:
                    self.state = "stopped"
                    await asyncio.shield(self._stop_processes())
                    raise

    async def _run_once(self) -> None:
        rtl_command, decoder_command = self.commands()
        self._rtl = await self._process_factory(
            *rtl_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            self._decoder = await self._process_factory(
                *decoder_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            await self._stop_process(self._rtl)
            self._rtl = None
            raise
        assert self._rtl.stdout is not None
        assert self._decoder.stdin is not None
        assert self._decoder.stdout is not None
        assert self._rtl.stderr is not None
        assert self._decoder.stderr is not None
        self.state = "listening"
        self.last_error = None
        self.last_exit_code = None
        self._progress(force=True)
        tasks = {
            asyncio.create_task(self._pump_audio(), name="same-audio-pump"),
            asyncio.create_task(self._watch_audio_stall(), name="same-audio-watchdog"),
            asyncio.create_task(self._read_headers(), name="same-header-reader"),
            asyncio.create_task(
                self._read_stderr("rtl_fm", self._rtl.stderr), name="same-rtl-stderr"
            ),
            asyncio.create_task(
                self._read_stderr("samedec", self._decoder.stderr), name="same-decoder-stderr"
            ),
        }
        rtl_wait = asyncio.create_task(self._rtl.wait(), name="same-rtl-wait")
        decoder_wait = asyncio.create_task(self._decoder.wait(), name="same-decoder-wait")
        watched = {rtl_wait, decoder_wait, *tasks}
        try:
            done, _pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if rtl_wait in done:
                self.last_exit_code = rtl_wait.result()
                raise SameReceiverError(f"rtl_fm exited with status {self.last_exit_code}")
            if decoder_wait in done:
                self.last_exit_code = decoder_wait.result()
                raise SameReceiverError(f"samedec exited with status {self.last_exit_code}")
            completed = next(iter(done))
            error = completed.exception()
            if error is not None:
                raise SameReceiverError(str(error)) from error
            raise SameReceiverError(f"{completed.get_name()} ended unexpectedly")
        finally:
            for task in watched:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*watched, return_exceptions=True)
            await self._stop_processes()

    async def _pump_audio(self) -> None:
        assert self._rtl is not None and self._rtl.stdout is not None
        assert self._decoder is not None and self._decoder.stdin is not None
        while chunk := await self._rtl.stdout.read(8192):
            samples = array("h")
            samples.frombytes(chunk[: len(chunk) - len(chunk) % 2])
            rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))
            self.service.record_audio(rms)
            self._audio_event.set()
            self._progress()
            try:
                self._decoder.stdin.write(chunk)
                await self._decoder.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise SameReceiverError("samedec closed its audio input") from error

    async def _watch_audio_stall(self) -> None:
        while True:
            self._audio_event.clear()
            try:
                await asyncio.wait_for(
                    self._audio_event.wait(), timeout=self.config.audio_stall_seconds
                )
            except TimeoutError as error:
                raise SameReceiverError(
                    f"rtl_fm produced no audio for {self.config.audio_stall_seconds}s"
                ) from error

    async def _read_headers(self) -> None:
        assert self._decoder is not None and self._decoder.stdout is not None
        while line := await self._decoder.stdout.readline():
            text = line.decode("ascii", errors="replace").strip()
            if text.startswith("ZCZC-"):
                try:
                    message, created = await self.service.ingest(text)
                except ValueError as error:
                    self._stderr_tail.append(f"samedec: rejected output: {error}")
                    continue
                state = "new" if created else "duplicate"
                print(
                    f"Outpost SAME {state}: {message.event_code} "
                    f"counties={','.join(message.location_codes)} test={message.is_test}",
                    flush=True,
                )
                self._progress(force=True)
            elif text == "NNNN":
                self.service.record_signal()
                self._progress(force=True)

    async def _read_stderr(self, name: str, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr_tail.append(f"{name}: {text[:180]}")

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _stop_processes(self) -> None:
        decoder, rtl = self._decoder, self._rtl
        self._decoder = self._rtl = None
        await asyncio.gather(
            self._stop_process(decoder), self._stop_process(rtl), return_exceptions=True
        )

    def health(self) -> dict[str, Any]:
        signal = self.service.health()
        status = signal["status"] if self.state == "listening" else self.state
        return {
            **signal,
            "status": status,
            "runtime_state": self.state,
            "frequency_mhz": self.config.frequency_mhz,
            "device": self.config.device,
            "sample_rate": self.config.sample_rate,
            "restart_count": self.restart_count,
            "next_restart_at": self.next_restart_at,
            "last_exit_code": self.last_exit_code,
            "last_error": self.last_error,
            "stderr_tail": list(self._stderr_tail),
        }
