"""Titulky_5 — přepis zvuku z počítače do českých titulků (SRT).

Zdrojové jazyky: angličtina (USA), evropské jazyky, čínština a japonština.
Překlad běží lokálně přes NLLB-200 → čeština. Na procesoru přepisuje Whisper small.

Spuštění:
  spustit.bat
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
import time
from collections import deque

import numpy as np

from .config import (
    CPU_WHISPER_MODEL,
    MODELS_DIR,
    NLLB_SOURCES,
    NLLB_TARGET,
    SRT_PATH,
    TARGET_RATE,
    WHISPER_MODEL,
)
from .files import SrtWriter
from .print import print_devices
from .windows import SubtitleOverlay, enable_dpi

log = logging.getLogger("titulky")


class LoopbackDevice:
    def __init__(self, index: int, name: str, channels: int, rate: int) -> None:
        self.index = index
        self.name = name
        self.channels = channels
        self.rate = rate

    @property
    def label(self) -> str:
        clean = self.name.replace("[Loopback]", "").strip()
        return clean[:42] + "…" if len(clean) > 43 else clean


def resample_mono(audio: np.ndarray, source_rate: int) -> np.ndarray:
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if audio.size == 0 or source_rate == TARGET_RATE:
        return audio
    new_len = max(1, int(round(audio.size * TARGET_RATE / source_rate)))
    old_x = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    new_x = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
    return np.ascontiguousarray(np.interp(new_x, old_x, audio), dtype=np.float32)


def to_whisper_audio(pcm: bytes, channels: int, source_rate: int) -> np.ndarray:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return samples
    samples /= 32768.0
    if channels > 1:
        usable = (samples.size // channels) * channels
        block = samples[:usable].reshape(-1, channels)
        samples = block.mean(axis=1)
    return resample_mono(samples, source_rate)


class AudioCapture:
    def __init__(self) -> None:
        import pyaudiowpatch as pyaudio
        self.error: str | None = None
        self.devices: list[LoopbackDevice] = []
        self.default_index: int | None = None
        self._pa: object | None = None
        self._stream: object | None = None
        self._running = False
        self._on_frame = None
        self._channels = 2
        self._rate = 48_000

        try:
            self._pa = pyaudio.PyAudio()
            self.load_devices()
        except Exception as exc:
            self.error = f"Zvuk nejde otevřít: {exc}"
            self._pa = None

    def load_devices(self) -> None:
        import pyaudiowpatch as pyaudio
        if self._pa is None:
            return
        try:
            wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            self.error = "WASAPI není dostupné."
            return
        default_name = ""
        try:
            out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            default_name = str(out.get("name") or "")
        except Exception:
            pass

        devices: list[LoopbackDevice] = []
        chosen: int | None = None
        for info in self._pa.get_loopback_device_info_generator():
            channels = int(info.get("maxInputChannels") or 0) or 2
            rate = int(info.get("defaultSampleRate") or 48_000)
            device = LoopbackDevice(int(info["index"]), str(info["name"]), channels, rate)
            devices.append(device)
            if chosen is None and default_name and default_name in device.name:
                chosen = device.index
        self.devices = devices
        self.default_index = chosen or (devices[0].index if devices else None)
        if not devices:
            self.error = "Nenašel jsem loopback zařízení."

    def cable_index(self) -> int | None:
        """Smyčka kabelu, kam při čtení odchází originál místo reproduktorů."""
        ranked: list[tuple[int, int]] = []
        for device in self.devices:
            name = device.name.casefold()
            if "cable in" in name or "cable input" in name:
                rank = 0
            elif "vb-audio" in name or "virtual cable" in name:
                rank = 1
            else:
                continue
            ranked.append((rank, device.index))
        if not ranked:
            return None
        ranked.sort()
        return ranked[0][1]

    def speaker_index(self, restored_name: str = "") -> int | None:
        """Smyčka skutečných reproduktorů, ne virtuálního kabelu."""
        if restored_name:
            folded = restored_name.casefold()
            for device in self.devices:
                if folded in device.name.casefold():
                    return device.index
        for device in self.devices:
            name = device.name.casefold()
            if any(word in name for word in ("cable", "vb-audio", "digital output", "nvidia", "hdmi")):
                continue
            if "reproduktory" in name or "speakers" in name:
                return device.index
        for device in self.devices:
            name = device.name.casefold()
            if "cable" not in name and "vb-audio" not in name:
                return device.index
        return self.default_index

    def start(self, index: int | None, on_frame) -> bool:
        import pyaudiowpatch as pyaudio
        self.stop()
        if self._pa is None or not self.devices:
            return False
        device = next((d for d in self.devices if d.index == index), self.devices[0])
        self._on_frame = on_frame
        self._running = True
        try:
            self._channels, self._rate = self._mix_format(device)
            frames = max(160, int(self._rate * 0.01))
            self._stream = self._pa.open(
                format=pyaudio.paInt16, channels=self._channels, rate=self._rate,
                frames_per_buffer=frames, input=True, input_device_index=device.index,
                stream_callback=self._on_chunk,
            )
        except Exception:
            self.stop()
            return False
        return True

    def _mix_format(self, device: LoopbackDevice) -> tuple[int, int]:
        channels = device.channels if device.channels > 0 else 2
        rate = device.rate if device.rate >= 8_000 else 48_000
        return channels, rate

    def _on_chunk(self, in_data, _frame_count, _time_info, _status):
        import pyaudiowpatch as pyaudio
        callback = self._on_frame
        try:
            frame = to_whisper_audio(in_data, self._channels, self._rate)
            if frame.size and callback is not None:
                callback(frame)
        except Exception:
            self.stop()
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    def stop(self) -> None:
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class Translator:
    def __init__(self) -> None:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained("facebook/nllb-200-distilled-600M", cache_dir=str(MODELS_DIR / "hf"))
        self.model = AutoModelForSeq2SeqLM.from_pretrained("facebook/nllb-200-distilled-600M", cache_dir=str(MODELS_DIR / "hf")).to(self.device)
        self.model.generation_config.max_length = None

    def translate(self, text: str, source_lang: str) -> str:
        import torch
        src, _ = NLLB_SOURCES[source_lang]
        self.tokenizer.src_lang = src
        ids = self.tokenizer(text.strip(), return_tensors="pt").to(self.device)
        words = max(1, len(text.split()))
        token_limit = 64 if words <= 16 else min(160, 64 + (words - 16) * 3)
        with torch.no_grad():
            out = self.model.generate(**ids, forced_bos_token_id=self.tokenizer.convert_tokens_to_ids(NLLB_TARGET), max_new_tokens=token_limit, num_beams=1, do_sample=False)
        return self.tokenizer.decode(out[0], skip_special_tokens=True).strip()


def enable_cuda_dlls() -> None:
    """Windows jinak knihovny CUDA z prostředí nenajde."""
    import importlib.util
    from pathlib import Path
    for pkg in ("nvidia.cublas", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc", "nvidia.cudnn"):
        spec = importlib.util.find_spec(pkg)
        locations = list(spec.submodule_search_locations) if spec and spec.submodule_search_locations else []
        for location in locations:
            bin_dir = Path(location) / "bin"
            if not bin_dir.is_dir():
                continue
            try:
                os.add_dll_directory(str(bin_dir))
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def minimize_console() -> None:
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 6)


def gpu_available() -> bool:
    try:
        enable_cuda_dlls()
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def create_whisper(gpu_only: bool = False) -> tuple[str, object]:
    enable_cuda_dlls()
    import ctranslate2
    from faster_whisper import WhisperModel
    cpu_threads = max(4, min(8, os.cpu_count() or 4))
    cuda_ok = False
    if ctranslate2.get_cuda_device_count() > 0:
        try:
            ctypes.WinDLL("cublas64_12.dll")
            cuda_ok = True
        except OSError:
            print("Grafická karta se nepoužije: v prostředí není cublas64_12.dll. Přepis běží na procesoru.")
    attempts: list[tuple[str, str, str]] = []
    if cuda_ok:
        attempts.extend([(WHISPER_MODEL, "cuda", "float16"), (WHISPER_MODEL, "cuda", "int8")])
    if not gpu_only:
        attempts.append((CPU_WHISPER_MODEL, "cpu", "int8"))
    last_error: Exception | None = None
    for name, device, compute in attempts:
        try:
            model = WhisperModel(name, device=device, compute_type=compute, cpu_threads=cpu_threads if device == "cpu" else 4, num_workers=1, download_root=str(MODELS_DIR))
            return f"Whisper {name} {device}/{compute}", model
        except Exception as exc:
            last_error = exc
            log.warning("Whisper %s %s/%s nejde použít: %s", name, device, compute, exc)
            continue
    raise RuntimeError(f"Whisper load failed: {last_error}")


class AudioBuffer:
    """Drží zachycený zvuk. Když přepis nestíhá, zahodí se nejstarší, ne právě mluvené."""

    def __init__(self, max_seconds: float = 20.0) -> None:
        self._items: deque[tuple[np.ndarray, float]] = deque()
        self._samples = 0
        self._max_samples = int(TARGET_RATE * max_seconds)
        self._lock = threading.Lock()

    def push(self, frame: np.ndarray) -> None:
        if frame.size == 0:
            return
        now = time.monotonic()
        with self._lock:
            self._items.append((frame, now))
            self._samples += int(frame.size)
            while self._samples > self._max_samples and self._items:
                old, _at = self._items.popleft()
                self._samples -= int(old.size)

    def pop(self, min_seconds: float, max_seconds: float) -> tuple[np.ndarray, float] | None:
        min_samples = int(TARGET_RATE * min_seconds)
        max_samples = int(TARGET_RATE * max_seconds)
        with self._lock:
            if self._samples < min_samples or not self._items:
                return None
            frames: list[np.ndarray] = []
            taken = 0
            first_at = self._items[0][1]
            while self._items and taken < max_samples:
                frame, _at = self._items.popleft()
                frames.append(frame)
                taken += int(frame.size)
                self._samples -= int(frame.size)
        if not frames:
            return None
        return np.concatenate(frames).astype(np.float32, copy=False), first_at


def is_spelled_noise(text: str) -> bool:
    """Whisper občas při šumu vyhláskuje jedno písmeno pořád dokola: E-E-E-E."""
    parts = [part.strip(" .,!?:;\"'").upper() for part in text.replace("—", "-").split("-")]
    parts = [part for part in parts if part]
    if len(parts) < 8:
        return False
    letters = [part for part in parts if len(part) == 1 and part.isalpha()]
    if len(letters) < len(parts) * 0.8:
        return False
    top = max(letters.count(letter) for letter in set(letters))
    return top >= 8 and top >= len(letters) * 0.7


class ChunkSeconds:
    """Délka jednoho úseku řeči. Delší úsek dá překladu celou větu."""

    def __init__(self, seconds: int = 2) -> None:
        self._seconds = seconds
        self._lock = threading.Lock()

    def get(self) -> int:
        with self._lock:
            return self._seconds

    def set(self, seconds: int) -> None:
        with self._lock:
            self._seconds = max(1, min(12, int(seconds)))


def run_processing(cap, translator, srt, lang_arg, model, audio: AudioBuffer, subtitle_queue: queue.Queue, chunk_seconds: ChunkSeconds):
    origin: float | None = None
    locked_lang = lang_arg
    try:
        while True:
            seconds = float(chunk_seconds.get())
            item = audio.pop(seconds, seconds)
            if item is None:
                time.sleep(0.05)
                continue
            chunk, first_at = item
            if origin is None:
                origin = first_at
            offset = max(0.0, first_at - origin)
            try:
                segments, info = model.transcribe(
                    chunk,
                    language=locked_lang,
                    beam_size=1,
                    best_of=1,
                    temperature=0.0,
                    vad_filter=True,
                    without_timestamps=False,
                    word_timestamps=False,
                    condition_on_previous_text=False,
                )
            except Exception as exc:
                log.error("Přepis selhal: %s", exc)
                continue
            if locked_lang is None and (getattr(info, "language_probability", 0) or 0) >= 0.6:
                locked_lang = (info.language or "").lower() or None
            detected = (locked_lang or (info.language or "")).lower()
            chunk_end = offset + len(chunk) / TARGET_RATE
            for seg in segments:
                text = (seg.text or "").strip()
                if not text or is_spelled_noise(text):
                    if text:
                        log.warning("Přeskočen přelud přepisu: %s", text[:80])
                    continue
                start = offset + float(seg.start)
                end = min(chunk_end, offset + float(seg.end))
                if end <= start:
                    end = start + 0.4
                try:
                    translated = translator.translate(text, detected) if detected in NLLB_SOURCES else text
                except Exception as exc:
                    log.error("Překlad selhal: %s", exc)
                    translated = text
                shown = translated or text
                if is_spelled_noise(shown):
                    log.warning("Přeskočen přelud přepisu: %s", shown[:80])
                    continue
                srt.add(start, end, shown)
                subtitle_queue.put(shown)
    except Exception as exc:
        log.error("Zpracování skončilo: %s", exc)
    finally:
        cap.stop()
        srt.flush()


class CzechReader:
    """Přečte hotový český titulek. Samotný překlad se nemění."""

    def __init__(self, cap: AudioCapture, on_frame, overlay: SubtitleOverlay) -> None:
        from .windows.speak import Speaker

        self.cap = cap
        self.on_frame = on_frame
        self.overlay = overlay
        self.speaker = Speaker()
        self.speaker.set_rate(3)
        self.overlay.set_on_speed(self.speaker.set_rate)
        self.enabled = False
        self._last = ""
        self._generation = 0
        self._capture_before = cap.default_index

    def toggle(self, enabled: bool) -> None:
        self._generation += 1
        threading.Thread(
            target=self._apply,
            args=(enabled, self._generation),
            name="cist-cz",
            daemon=True,
        ).start()

    def say(self, text: str) -> None:
        cleaned = " ".join((text or "").split())
        if not self.enabled or len(cleaned) < 2 or cleaned == self._last:
            return
        self._last = cleaned
        self.speaker.say(cleaned)

    def shutdown(self) -> None:
        was_on = self.enabled
        self.enabled = False
        self._generation += 1
        self.speaker.shutdown()
        if not was_on:
            return
        try:
            from .windows.audio_route import restore_playback

            restore_playback()
        except Exception:
            log.exception("vrácení reproduktorů")

    def _apply(self, enabled: bool, generation: int) -> None:
        from .windows.audio_route import restore_playback, switch_to_cable

        if enabled:
            error = ""
            try:
                switch_to_cable()
            except Exception as exc:
                error = str(exc)
            if generation != self._generation:
                try:
                    restore_playback()
                except Exception:
                    pass
                return
            self.overlay.root.after(0, lambda: self._finish_on(generation, error))
            return
        self.enabled = False
        self.speaker.enabled = False
        self.speaker.clear()
        restored = ""
        error = ""
        try:
            restored = restore_playback()
        except Exception as exc:
            error = str(exc)
        if generation != self._generation:
            return
        self.overlay.root.after(0, lambda: self._finish_off(generation, restored, error))

    def _finish_on(self, generation: int, error: str) -> None:
        from .windows.audio_route import restore_playback
        from .windows.speak import list_playback_devices, prefer_playback

        if generation != self._generation:
            return
        if error:
            self._fail(error)
            return
        index = self.cap.cable_index()
        if index is None:
            try:
                restore_playback()
            except Exception:
                pass
            self._fail("CABLE In ve Windows není. Nainstalujte VB-Audio Virtual Cable.")
            return
        if not self.cap.start(index, self.on_frame):
            try:
                restore_playback()
            except Exception:
                pass
            if self._capture_before is not None:
                self.cap.start(self._capture_before, self.on_frame)
            self._fail("Zvuk z kabelu nejde číst. Originál zůstává v reproduktorech.")
            return
        cable_name = next((device.name for device in self.cap.devices if device.index == index), "cable")
        devices = list_playback_devices()
        chosen = prefer_playback(devices, cable_name) if devices else None
        if chosen is not None and ("cable" in chosen.name.casefold() or "vb-audio" in chosen.name.casefold()):
            for device in devices:
                text = device.name.casefold()
                if any(word in text for word in ("cable", "vb-audio", "hdmi", "displayport", "nvidia")):
                    continue
                if "reproduktory" in text or "speakers" in text:
                    chosen = device
                    break
        if chosen is None or "cable" in chosen.name.casefold() or "vb-audio" in chosen.name.casefold():
            try:
                restore_playback()
            except Exception:
                pass
            if self._capture_before is not None:
                self.cap.start(self._capture_before, self.on_frame)
            self._fail("Nenašel jsem reproduktory pro české čtení.")
            return
        if not self.speaker.voice_id:
            try:
                restore_playback()
            except Exception:
                pass
            if self._capture_before is not None:
                self.cap.start(self._capture_before, self.on_frame)
            self._fail("Chybí český hlas. Ve Windows přidejte češtinu a hlas Jakub.")
            return
        self.speaker.set_device(chosen.index, chosen.rate)
        self.speaker.enabled = True
        self.enabled = True
        log.warning("Číst CZ: originál jde do kabelu, hlas do %s", chosen.name)

    def _finish_off(self, generation: int, restored: str, error: str) -> None:
        if generation != self._generation:
            return
        index = self.cap.speaker_index(restored)
        if index is not None:
            self.cap.start(index, self.on_frame)
        if error:
            self.overlay.set_text(error)
        self.overlay.set_reading(False)

    def _fail(self, message: str) -> None:
        self.enabled = False
        self.speaker.enabled = False
        self.speaker.clear()
        self.overlay.set_reading(False)
        self.overlay.set_text(message)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if sys.platform != "win32":
        raise SystemExit("Windows only.")
    print(f"Python {sys.version.split()[0]}  {sys.executable}")
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", action="store_true")
    parser.add_argument("--lang", choices=sorted(NLLB_SOURCES), default=None)
    args = parser.parse_args()

    cap = AudioCapture()
    if cap.error or not cap.devices:
        raise SystemExit(cap.error or "No devices.")
    if args.devices:
        raise SystemExit(print_devices(cap))

    enable_dpi()
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("GPU: %s", "yes" if gpu_available() else "no")
    print("Načítám modely, chvíli to trvá...")
    label, model = create_whisper(gpu_only=False)
    print(label)
    translator = Translator()

    overlay = SubtitleOverlay()
    overlay.show_intro()
    audio = AudioBuffer(max_seconds=30.0)
    chunk_seconds = ChunkSeconds()
    overlay.set_on_length(chunk_seconds.set)
    subtitle_queue: queue.Queue = queue.Queue()

    def on_frame(frame):
        audio.push(frame)

    if not cap.start(cap.default_index, on_frame):
        raise SystemExit("Audio start failed")

    reader = CzechReader(cap, on_frame, overlay)
    overlay.set_on_read_toggle(reader.toggle)

    srt = SrtWriter(SRT_PATH)
    print(f"Titulky se ukládají do {SRT_PATH}")
    print("Terminál se minimalizuje. Obnovíte ho z hlavního panelu, zastavíte klávesami Ctrl+C.")
    minimize_console()

    proc_thread = threading.Thread(
        target=run_processing,
        args=(cap, translator, srt, args.lang, model, audio, subtitle_queue, chunk_seconds),
        daemon=True,
    )
    proc_thread.start()

    def update_overlay():
        try:
            while not subtitle_queue.empty():
                text = subtitle_queue.get_nowait()
                overlay.set_text(text)
                reader.say(text)
        except Exception:
            pass
        overlay.root.after(100, update_overlay)

    overlay.root.after(100, update_overlay)
    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        reader.shutdown()
        proc_thread.join(timeout=1)
        overlay.close()


if __name__ == "__main__":
    main()
