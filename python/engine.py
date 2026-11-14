"""Real-time audio: mic -> FX -> line out, plus the sample voices.

One thread owns the audio devices and all DSP state. Everything else (Bridge
handlers, the web server) talks to it through `post()`, so the audio thread
never takes a lock that a slow web request could be holding.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from fx import SR, InputConditioner, make_effects
from slots import MAX_PADS, SlotBank, tidy_recording

log = logging.getLogger("engine")


@dataclass
class Voice:
    pad: int
    buf: np.ndarray
    pos: int = 0
    loop: bool = False
    fade: float = 1.0  # < 1 means releasing
    releasing: bool = False


class Engine:
    def __init__(self, settings: dict, bank: SlotBank) -> None:
        self.s = settings  # shared dict, read every block (writes are atomic per key)
        self.bank = bank
        self.effects = make_effects()
        self.cond = InputConditioner()
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.voices: dict[int, Voice] = {}
        self.focus_pad: int | None = None  # last pad triggered, shown on the LED matrix
        self._fx_last = -1

        self.recording_pad: int | None = None
        self.rec_chunks: list[np.ndarray] = []
        self.rec_len = 0

        # read by the UI / LED matrix
        self.level_in = 0.0
        self.level_out = 0.0
        self.playing: frozenset[int] = frozenset()
        self.rec_progress = 0.0
        self.recent_out = np.zeros(2048, np.float32)  # for the buzzer pitch tracker
        self.device_status = {"input": "closed", "output": "closed"}
        self.on_recorded = None  # callback(pad), runs on a worker thread

        self._mic = None
        self._spk = None
        self._run = False
        self._thread: threading.Thread | None = None
        self._reopen = threading.Event()

    # -- public API (any thread) --------------------------------------------

    def post(self, *event) -> None:
        self.events.put(event)

    def focus_progress(self) -> tuple[int, float] | None:
        """(pad, 0..1 position) of the last-triggered pad while it is sounding."""
        pad = self.focus_pad
        v = self.voices.get(pad) if pad is not None else None
        if v is None or not len(v.buf):
            return None
        return pad, min(1.0, v.pos / len(v.buf))

    def start(self) -> None:
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        if self._thread:
            self._thread.join(timeout=2)
        self._close()

    def reopen_devices(self) -> None:
        self._reopen.set()

    # -- devices ---------------------------------------------------------------

    def _open(self) -> None:
        block = int(self.s.get("block", 512))
        try:
            from arduino.app_peripherals.speaker import Speaker

            self._spk = Speaker(self.s.get("output_device", Speaker.JACK_SPEAKER_1), sample_rate=SR, channels=1, format=np.int16, buffer_size=block)
            self._spk.start()
            self.device_status["output"] = "open"
        except Exception as e:
            log.warning("output device: %s", e)
            self._spk = None
            self.device_status["output"] = f"error: {e}"
        try:
            from arduino.app_peripherals.microphone import Microphone

            self._mic = Microphone(self.s.get("input_device", Microphone.JACK_MIC_1), sample_rate=SR, channels=1, format=np.int16, buffer_size=block)
            self._mic.start()
            self.device_status["input"] = "open"
        except Exception as e:
            log.warning("input device: %s", e)
            self._mic = None
            self.device_status["input"] = f"error: {e}"

    def _close(self) -> None:
        for dev in (self._mic, self._spk):
            try:
                if dev:
                    dev.stop()
            except Exception:
                pass
        self._mic = self._spk = None
        self.device_status = {"input": "closed", "output": "closed"}

    # -- audio thread ------------------------------------------------------------

    def _loop(self) -> None:
        last_try = 0.0
        while self._run:
            if self._reopen.is_set():
                self._reopen.clear()
                self._close()
                last_try = 0.0
            if (self._mic is None or self._spk is None) and time.monotonic() - last_try > 5:
                last_try = time.monotonic()
                self._close()
                self._open()
                if self._spk:
                    # a little pre-roll so the first blocks don't underrun
                    silence = np.zeros(int(self.s.get("block", 512)) * 2, np.int16)
                    self._spk.play(silence)

            block = int(self.s.get("block", 512))
            x = None
            if self._mic is not None:
                try:
                    chunk = self._mic.capture()
                except Exception as e:
                    log.warning("mic read failed: %s", e)
                    self._mic = None
                    continue
                if chunk is None:
                    time.sleep(0.001)
                    continue
                x = chunk.reshape(-1).astype(np.float32) / 32768.0
            else:
                x = np.zeros(block, np.float32)

            y = self._process(x)

            if self._spk is not None:
                try:
                    self._spk.play((y * 32767).astype(np.int16))
                except Exception as e:
                    log.warning("speaker write failed: %s", e)
                    self._spk = None
            elif self._mic is None:
                time.sleep(len(x) / SR)  # nothing is clocking us

    def _process(self, x: np.ndarray) -> np.ndarray:
        s = self.s
        self._handle_events()

        dry = self.cond.process(x, gain=10 ** (s.get("mic_gain_db", 0.0) / 20), gate_db=s.get("gate_db", -55.0))
        fx_i = int(s.get("fx_index", 0)) % len(self.effects)
        if fx_i != self._fx_last:
            self.effects[fx_i].reset()  # don't replay whatever was in its buffers last time
            self._fx_last = fx_i
        amount = 0.0 if s.get("fx_bypass") else float(s.get("fx_amount", 0.0))
        wet = self.effects[fx_i].process(dry, amount)

        if self.recording_pad is not None:
            take = wet if s.get("record_post_fx") else dry
            self.rec_chunks.append(take.copy())
            self.rec_len += len(take)
            max_len = s.get("max_rec_s", 10) * SR
            self.rec_progress = min(1.0, self.rec_len / max_len)
            if self.rec_len >= max_len:
                self._stop_recording()

        mix = self._render_voices(len(x)) * s.get("sample_level", 0.9)
        out = wet * s.get("monitor_level", 0.8) + mix
        out = np.tanh(out * s.get("master", 0.9)).astype(np.float32)  # soft clip, never wraps

        n = min(len(out), len(self.recent_out))
        self.recent_out = np.concatenate([self.recent_out[n:], out[-n:]])
        self.level_in = max(float(np.max(np.abs(dry))), self.level_in * 0.85)
        self.level_out = max(float(np.max(np.abs(out))), self.level_out * 0.85)
        return out

    def _handle_events(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "down":
                self._pad_down(ev[1])
            elif kind == "up":
                self._pad_up(ev[1])
            elif kind == "hold":
                self._pad_hold(ev[1])
            elif kind == "stop_all":
                self.voices.clear()
            elif kind == "slot_changed":
                self._slot_changed(ev[1])
            elif kind == "reset_fx":
                for fx in self.effects:
                    fx.reset()

    def _pad_down(self, pad: int) -> None:
        buf = self.bank.play_buffer(pad)
        if buf is None or self.recording_pad is not None:
            return
        mode = self.bank.slots[pad].meta.mode
        if mode == "loop" and pad in self.voices and not self.voices[pad].releasing:
            self.voices[pad].releasing = True  # second press stops the loop
            return
        self.voices[pad] = Voice(pad, buf, loop=(mode == "loop"))
        self.focus_pad = pad

    def _slot_changed(self, pad: int) -> None:
        # a looping voice picks up the edit (pitch, trim...) without restarting
        v = self.voices.get(pad)
        buf = self.bank.play_buffer(pad)
        if v is None:
            return
        if buf is None or not len(buf):
            self.voices.pop(pad)
        else:
            v.buf, v.pos = buf, v.pos % len(buf)

    def _pad_up(self, pad: int) -> None:
        if self.recording_pad == pad:
            self._stop_recording()
            return
        v = self.voices.get(pad)
        if v and self.bank.slots[pad].meta.mode == "gate":
            v.releasing = True

    def _pad_hold(self, pad: int) -> None:
        if self.recording_pad is not None or pad >= MAX_PADS:
            return
        if self.bank.slots[pad].loaded and not self.s.get("rec_overwrite", True):
            return
        self.voices.pop(pad, None)
        self.recording_pad = pad
        self.rec_chunks, self.rec_len, self.rec_progress = [], 0, 0.0
        log.info("recording pad %d", pad)

    def _stop_recording(self) -> None:
        pad, chunks = self.recording_pad, self.rec_chunks
        self.recording_pad, self.rec_chunks, self.rec_len = None, [], 0
        if pad is None or not chunks:
            return
        audio = np.concatenate(chunks)
        trim, norm = self.s.get("auto_trim", True), self.s.get("normalize", True)

        def finish() -> None:
            take = tidy_recording(audio, trim, norm)
            if len(take) < SR * 0.05:
                log.info("pad %d: recording too short, ignored", pad)
                return
            self.bank.set_audio(pad, take, name=f"REC {pad + 1:02d}")
            if self.on_recorded:
                self.on_recorded(pad)

        threading.Thread(target=finish, daemon=True).start()

    def _render_voices(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        done = []
        for pad, v in self.voices.items():
            seg = v.buf[v.pos : v.pos + n]
            if v.loop and len(seg) < n and len(v.buf):
                reps = int(np.ceil((n - len(seg)) / len(v.buf))) + 1
                seg = np.concatenate([seg, np.tile(v.buf, reps)])[:n]
                v.pos = (v.pos + n) % len(v.buf)
            else:
                v.pos += n
            if v.releasing:
                # ~10 ms release ramp
                end = max(0.0, v.fade - n / (0.01 * SR))
                seg = seg * np.linspace(v.fade, end, len(seg), dtype=np.float32)
                v.fade = end
                if end <= 0:
                    done.append(pad)
            out[: len(seg)] += seg
            if not v.loop and v.pos >= len(v.buf):
                done.append(pad)
        for pad in done:
            self.voices.pop(pad, None)
        self.playing = frozenset(self.voices)
        return out
