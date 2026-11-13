"""Titulky_5 — přepis zvuku z počítače do českých titulků (SRT).

Zdrojové jazyky: angličtina (USA), evropské jazyky, čínština a japonština.
Překlad běží lokálně přes NLLB-200 → čeština. Na NVIDIA, AMD a Intel Arc
přepisuje Whisper large-v3 na grafice, jinak small na procesoru.

Spuštění:
  spustit.bat
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
# hf-xet na Windows spojení často shodí (WinError 10054) a model se nestáhne.
os.environ["HF_HUB_DISABLE_XET"] = "1"
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")

import queue
import re
import sys
import threading
import time
from collections import deque

import numpy as np

from .config import (
    CPU_WHISPER_MODEL,
    INTEL_WHISPER_REPO,
    MODELS_DIR,
    NLLB_SOURCES,
    NLLB_TARGET,
    ORIGINAL_SRT_PATH,
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


# Dočasná paměť generování se maže dřív, než se po čtvrt hodině začne vracet originál.
CONTEXT_RESET_SECONDS = 300


class Translator:
    def __init__(self) -> None:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained("facebook/nllb-200-distilled-600M", cache_dir=str(MODELS_DIR / "hf"))
        self.model = AutoModelForSeq2SeqLM.from_pretrained("facebook/nllb-200-distilled-600M", cache_dir=str(MODELS_DIR / "hf")).to(self.device)
        self.model.generation_config.max_length = None
        self._context_at = time.monotonic()

    def maybe_reset(self) -> None:
        if time.monotonic() - self._context_at < CONTEXT_RESET_SECONDS:
            return
        self.reset_context()
        log.warning("Vymazán dočasný stav překladu.")

    def reset_context(self) -> None:
        """Zahodí mezipaměť generování. Váhy modelu ani způsob překladu se nemění."""
        import gc
        import torch
        if hasattr(self.model, "_previous_max_cache_length"):
            delattr(self.model, "_previous_max_cache_length")
        self.model.generation_config.max_length = None
        lang = getattr(self.tokenizer, "src_lang", None)
        if lang:
            self.tokenizer.set_src_lang_special_tokens(lang)
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self._context_at = time.monotonic()

    def translate(self, text: str, source_lang: str) -> str:
        try:
            result = self._generate(text, source_lang)
        except Exception:
            self.reset_context()
            result = self._generate(text, source_lang)
        if echoes_source(result, text):
            log.warning("Překlad vrátil originál, mažu kontext a zkouším znovu.")
            self.reset_context()
            result = self._generate(text, source_lang)
        return result

    def _generate(self, text: str, source_lang: str) -> str:
        import torch
        src, _ = NLLB_SOURCES[source_lang]
        self.tokenizer.src_lang = src
        ids = self.tokenizer(text.strip(), return_tensors="pt").to(self.device)
        words = max(1, len(text.split()))
        token_limit = 64 if words <= 16 else min(160, 64 + (words - 16) * 3)
        with torch.no_grad():
            out = self.model.generate(
                **ids,
                forced_bos_token_id=self.tokenizer.convert_tokens_to_ids(NLLB_TARGET),
                max_new_tokens=token_limit,
                num_beams=1,
                do_sample=False,
            )
        return self.tokenizer.decode(out[0], skip_special_tokens=True).strip()


def _add_bin_dir(bin_dir) -> None:
    from pathlib import Path
    path = Path(bin_dir)
    if not path.is_dir():
        return
    try:
        os.add_dll_directory(str(path))
    except (OSError, AttributeError):
        pass
    os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")


def enable_cuda_dlls() -> None:
    """Windows jinak knihovny CUDA z prostředí nenajde. Bez nich se nic neděje."""
    import importlib.util
    from pathlib import Path
    if importlib.util.find_spec("nvidia") is None:
        return
    for pkg in ("nvidia.cublas", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            continue
        locations = list(spec.submodule_search_locations) if spec and spec.submodule_search_locations else []
        for location in locations:
            _add_bin_dir(Path(location) / "bin")


def enable_rocm_dlls() -> None:
    """Windows jinak knihovny ROCm z prostředí nenajde. Bez nich se nic neděje."""
    from pathlib import Path
    site = Path(sys.prefix) / "Lib" / "site-packages"
    _add_bin_dir(site / "_rocm_sdk_core" / "bin")
    for bin_dir in sorted(site.glob("_rocm_sdk_libraries*/bin")):
        _add_bin_dir(bin_dir)
    hip = os.environ.get("HIP_PATH")
    if hip:
        _add_bin_dir(Path(hip) / "bin")


def _dll_loads(name: str) -> bool:
    try:
        ctypes.WinDLL(name)
        return True
    except OSError:
        return False


def prepare_gpu() -> str | None:
    """cuda = NVIDIA, hip = AMD. missing-cublas = karta je vidět, runtime ne. Jinak None."""
    enable_cuda_dlls()
    enable_rocm_dlls()
    try:
        import ctranslate2
        devices = ctranslate2.get_cuda_device_count()
    except Exception:
        return None
    if devices <= 0:
        return None
    if _dll_loads("cublas64_12.dll"):
        return "cuda"
    if _dll_loads("hipblas.dll") or _dll_loads("amdhip64_7.dll"):
        return "hip"
    return "missing-cublas"


def minimize_console() -> None:
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 6)


_INTEL_GPU_UNSET = object()
_intel_gpu_cache: str | None | object = _INTEL_GPU_UNSET


def gpu_available() -> bool:
    return prepare_gpu() in ("cuda", "hip") or prepare_intel_gpu() is not None


def _gpu_memory_gb(core, device: str) -> float | None:
    for key in ("GPU_DEVICE_TOTAL_MEM_SIZE", "GPU_MEMORY_STATISTICS"):
        try:
            value = core.get_property(device, key)
        except Exception:
            continue
        if isinstance(value, (int, float)) and value > 0:
            return float(value) / (1024 ** 3)
        if isinstance(value, dict):
            total = value.get("max_allocated_memory") or value.get("total")
            if isinstance(total, (int, float)) and total > 0:
                return float(total) / (1024 ** 3)
    return None


def prepare_intel_gpu() -> str | None:
    """Zařízení OpenVINO pro Intel Arc. Sdílená paměť se použije sama. Jinak None."""
    global _intel_gpu_cache
    if _intel_gpu_cache is not _INTEL_GPU_UNSET:
        return _intel_gpu_cache  # type: ignore[return-value]
    try:
        import openvino as ov
    except Exception:
        _intel_gpu_cache = None
        return None
    try:
        core = ov.Core()
        names = [str(device) for device in core.available_devices if str(device).upper().startswith("GPU")]
    except Exception:
        _intel_gpu_cache = None
        return None
    if not names:
        print("OpenVINO Intel grafiku nevidí. Přepis běží na procesoru.")
        _intel_gpu_cache = None
        return None
    chosen = "GPU" if "GPU" in names else names[0]
    full = ""
    for name in names:
        try:
            full_name = str(core.get_property(name, "FULL_DEVICE_NAME"))
        except Exception:
            full_name = ""
        if "Arc" in full_name:
            chosen = "GPU" if name == "GPU" else name
            full = full_name
            break
        if not full:
            full = full_name
            chosen = "GPU" if name == "GPU" else name
    mem = _gpu_memory_gb(core, chosen)
    label = full or chosen
    if mem:
        print(f"Intel grafika: {label}, sdílená paměť {mem:.1f} GB")
    else:
        print(f"Intel grafika: {label}")
    _intel_gpu_cache = chosen
    return chosen


def _snapshot_ready(dest) -> bool:
    from pathlib import Path
    folder = Path(dest)
    needed = ("openvino_encoder_model.xml", "openvino_decoder_model.xml")
    return all((folder / name).is_file() for name in needed)


def _download_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(mark in text for mark in ("connecterror", "10054", "snapshot", "huggingface", "timed out", "connection"))


def download_snapshot(repo_id: str, dest) -> None:
    """Stáhne model. Když server spojení shodí, zkusí to znovu a naváže na rozjeté soubory."""
    from pathlib import Path
    folder = Path(dest)
    if _snapshot_ready(folder):
        return
    folder.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            snapshot_download(repo_id, local_dir=str(folder), max_workers=1)
            if _snapshot_ready(folder):
                return
            last_error = RuntimeError(f"V {folder} chybí soubory modelu {repo_id}.")
        except Exception as exc:
            last_error = exc
            log.warning("Stažení %s, pokus %s: %s", repo_id, attempt + 1, exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Model {repo_id} se nestáhl: {last_error}")


class _WhisperSegment:
    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _WhisperInfo:
    def __init__(self, language: str, language_probability: float) -> None:
        self.language = language
        self.language_probability = language_probability


def _language_code(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("<|") and text.endswith("|>"):
        return text[2:-2]
    return text


class IntelWhisper:
    """Stejné rozhraní jako faster-whisper, výpočet jede na Intel Arc přes OpenVINO."""

    def __init__(self, pipeline) -> None:
        self._pipe = pipeline

    def transcribe(self, audio, language=None, **_ignored):
        samples = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        if samples.size < TARGET_RATE // 10:
            return [], _WhisperInfo(_language_code(language or ""), 0.0)
        if float(np.sqrt(np.mean(samples * samples))) < 0.002:
            return [], _WhisperInfo(_language_code(language or ""), 0.0)
        options = {"return_timestamps": True, "task": "transcribe", "num_beams": 1}
        code = _language_code(language or "")
        if code:
            options["language"] = code
        result = self._pipe.generate(samples.tolist(), **options)
        detected = _language_code(str(getattr(result, "language", "") or code))
        segments = []
        for chunk in _whisper_chunks(result):
            text = str(getattr(chunk, "text", "") or "").strip()
            if not text:
                continue
            start = float(getattr(chunk, "start_ts", 0.0) or 0.0)
            end = float(getattr(chunk, "end_ts", -1.0))
            if end < start:
                end = start + 0.4
            segments.append(_WhisperSegment(text, start, end))
        if not segments:
            texts = list(getattr(result, "texts", None) or [])
            text = str(texts[0]).strip() if texts else ""
            if text:
                segments.append(_WhisperSegment(text, 0.0, samples.size / TARGET_RATE))
        probability = 1.0 if detected else 0.0
        return segments, _WhisperInfo(detected, probability)


def _whisper_chunks(result) -> list:
    chunks = getattr(result, "chunks", None)
    if not chunks:
        return []
    first = chunks[0]
    if hasattr(first, "text"):
        return list(chunks)
    if isinstance(first, (list, tuple)):
        return list(first)
    return list(chunks)


def load_intel_whisper(device: str) -> IntelWhisper:
    import openvino_genai as ov_genai
    dest = MODELS_DIR / "whisper-large-v3-int8-ov"
    if not _snapshot_ready(dest):
        print("Stahuji Whisper large-v3 pro Intel Arc, chvíli to trvá...")
    download_snapshot(INTEL_WHISPER_REPO, dest)
    try:
        pipe = ov_genai.WhisperPipeline(str(dest), device, PERFORMANCE_HINT="LATENCY")
    except Exception as exc:
        log.warning("OpenVINO bez PERFORMANCE_HINT: %s", exc)
        pipe = ov_genai.WhisperPipeline(str(dest), device)
    return IntelWhisper(pipe)


def create_whisper(gpu_only: bool = False) -> tuple[str, object]:
    from faster_whisper import WhisperModel
    cpu_threads = max(4, min(8, os.cpu_count() or 4))
    backend = prepare_gpu()
    if backend == "missing-cublas":
        print("Grafická karta se nepoužije: v prostředí není cublas64_12.dll. Přepis běží na procesoru.")
        backend = None
    last_error: Exception | None = None
    if backend not in ("cuda", "hip"):
        intel = prepare_intel_gpu()
        if intel:
            try:
                return f"Whisper large-v3 {intel}/int8", load_intel_whisper(intel)
            except Exception as exc:
                last_error = exc
                log.warning("Whisper Intel %s nejde použít: %s", intel, exc)
                print("Intel Arc se nepoužije. Přepis běží na procesoru.")
                if gpu_only:
                    raise RuntimeError(f"Whisper load failed: {exc}") from exc
    attempts: list[tuple[str, str, str]] = []
    if backend in ("cuda", "hip"):
        # Na AMD je v ctranslate2 stejné zařízení cuda. int8 bez cublas na NVIDIA visí, proto se tam nespouští.
        fallback = "int8" if backend == "cuda" else "int8_float16"
        attempts.extend([(WHISPER_MODEL, "cuda", "float16"), (WHISPER_MODEL, "cuda", fallback)])
    if not gpu_only:
        attempts.append((CPU_WHISPER_MODEL, "cpu", "int8"))
    for name, device, compute in attempts:
        for attempt in range(3):
            try:
                model = WhisperModel(name, device=device, compute_type=compute, cpu_threads=cpu_threads if device == "cpu" else 4, num_workers=1, download_root=str(MODELS_DIR))
                shown = "amd" if backend == "hip" and device == "cuda" else device
                return f"Whisper {name} {shown}/{compute}", model
            except Exception as exc:
                last_error = exc
                if attempt < 2 and _download_error(exc):
                    log.warning("Whisper %s %s/%s, pokus %s: %s", name, device, compute, attempt + 1, exc)
                    time.sleep(2 * (attempt + 1))
                    continue
                log.warning("Whisper %s %s/%s nejde použít: %s", name, device, compute, exc)
                break
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


_FILLER_CORE = {
    "ne", "no", "ano", "yes",
    "děkuji", "dekuji", "díky", "diky",
    "thank", "thanks",
}
_FILLER_EXTRA = _FILLER_CORE | {"vám", "vam", "you"}


def _words(text: str) -> list[str]:
    words: list[str] = []
    token: list[str] = []
    for char in text.casefold():
        if char.isalpha():
            token.append(char)
        elif token:
            words.append("".join(token))
            token = []
    if token:
        words.append("".join(token))
    return words


def is_spelled_noise(text: str) -> bool:
    """Whisper při šumu vyhláskuje písmena: E-E-E-E nebo E-N-O-S-T-O-S-T-E-N-G."""
    parts = [part.strip(" .,!?:;\"'").upper() for part in text.replace("—", "-").replace("–", "-").split("-")]
    parts = [part for part in parts if part]
    if len(parts) < 6:
        return False
    letters = [part for part in parts if len(part) == 1 and part.isalpha()]
    return len(letters) >= 6 and len(letters) >= len(parts) * 0.8


def is_repeated_characters(text: str) -> bool:
    """Řada stejného znaku, třeba Sssssssss, není titulek."""
    letters = [char for char in text.casefold() if char.isalpha()]
    if len(letters) < 8:
        return False
    run = 1
    for previous, char in zip(letters, letters[1:]):
        run = run + 1 if char == previous else 1
        if run >= 8:
            return True
    top = max(letters.count(char) for char in set(letters))
    return len(letters) >= 12 and top >= len(letters) * 0.85


def is_filler_turn(text: str) -> bool:
    """Při tichu přepis vymyslí krátkou výměnu: „- Ne, ne.“ nebo „- Děkuji vám. - Děkuji.“"""
    raw = " ".join(text.replace("—", "-").replace("–", "-").split())
    if not raw.startswith("-") and " - " not in raw:
        return False
    body = raw[1:].strip() if raw.startswith("-") else raw
    parts = [part.strip() for part in body.split(" - ") if part.strip()]
    if len(parts) < 2:
        return False
    for part in parts:
        words = _words(part)
        if not words or len(words) > 3:
            return False
        if any(word not in _FILLER_EXTRA for word in words):
            return False
        if not any(word in _FILLER_CORE for word in words):
            return False
    return True


def is_skipped_caption(text: str) -> bool:
    if is_spelled_noise(text) or is_filler_turn(text) or is_repeated_characters(text):
        return True
    words = _words(text)
    return len(words) == 1 and len(words[0]) == 1


_REPEAT_JOIN = {"a", "and", "i", "for", "pro"}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def collapse_repeated_words(text: str) -> str:
    """Z opakovaného slova nebo slovního spojení nechá jen první."""
    spans = [(match.start(), match.end()) for match in _WORD_RE.finditer(text)]
    if len(spans) < 2:
        return text.strip()
    keep = [True] * len(spans)
    words = [text[start:end].casefold() for start, end in spans]

    def drop(indexes: range) -> None:
        for index in indexes:
            keep[index] = False

    changed = True
    while changed:
        changed = False
        visible = [index for index, shown in enumerate(keep) if shown]
        for left, right in zip(visible, visible[1:]):
            if words[left] == words[right]:
                drop(range(right, right + 1))
                changed = True
                break
        if changed:
            continue
        for left, mid, right in zip(visible, visible[1:], visible[2:]):
            if words[mid] in _REPEAT_JOIN and words[left] == words[right]:
                drop(range(mid, right + 1))
                changed = True
                break
        if changed:
            continue
        visible_words = [words[index] for index in visible]
        for size in range(min(6, len(visible_words) // 2), 1, -1):
            for index in range(0, len(visible_words) - 2 * size + 1):
                if visible_words[index:index + size] != visible_words[index + size:index + 2 * size]:
                    continue
                start = visible[index + size]
                end = visible[index + 2 * size - 1]
                drop(range(start, end + 1))
                changed = True
                break
            if changed:
                break
    if all(keep):
        return text.strip()
    kept = [text[start:end] for index, (start, end) in enumerate(spans) if keep[index]]
    if not kept:
        return ""
    prefix = "- " if text.lstrip().startswith("-") else ""
    last_kept = max(index for index, shown in enumerate(keep) if shown)
    ending = ""
    for char in text[spans[last_kept][1]:]:
        if char in ".!?…":
            ending = char
    return f"{prefix}{' '.join(kept)}{ending}".strip()


def echoes_source(translated: str, source: str) -> bool:
    """Delší věta, kterou model jen opsal, není český titulek."""
    left = " ".join(_words(translated))
    right = " ".join(_words(source))
    return bool(right) and len(right.split()) >= 4 and left == right


class RecentCaptions:
    """Stejný text projde jen poprvé. Další stejná slova se už neukážou."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def accept(self, _start: float, text: str) -> bool:
        key = " ".join(_words(text))
        if not key or key in self._seen:
            return False
        self._seen.add(key)
        return True


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


def run_processing(cap, translator, srt, original, lang_arg, model, audio: AudioBuffer, subtitle_queue: queue.Queue, chunk_seconds: ChunkSeconds):
    origin: float | None = None
    locked_lang = lang_arg if lang_arg in NLLB_SOURCES else None
    heard = RecentCaptions()
    shown_recent = RecentCaptions()
    try:
        while True:
            seconds = float(chunk_seconds.get())
            item = audio.pop(seconds, seconds)
            if item is None:
                time.sleep(0.05)
                continue
            translator.maybe_reset()
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
                    initial_prompt=None,
                )
            except Exception as exc:
                log.error("Přepis selhal: %s", exc)
                continue
            if locked_lang is None and (getattr(info, "language_probability", 0) or 0) >= 0.6:
                candidate = (info.language or "").lower()
                if candidate in NLLB_SOURCES:
                    locked_lang = candidate
                    log.warning("Jazyk přepisu: %s", locked_lang)
            detected = (locked_lang or (info.language or "")).lower()
            source = detected if detected in NLLB_SOURCES else None
            chunk_end = offset + len(chunk) / TARGET_RATE
            for seg in segments:
                text = collapse_repeated_words((seg.text or "").strip())
                if not text or is_skipped_caption(text):
                    if text:
                        log.warning("Přeskočen přelud přepisu: %s", text[:80])
                    continue
                start = offset + float(seg.start)
                end = min(chunk_end, offset + float(seg.end))
                if end <= start:
                    end = start + 0.4
                if not heard.accept(start, text):
                    log.warning("Přeskočeno opakování: %s", text[:80])
                    continue
                if source is None:
                    shown = text
                else:
                    try:
                        shown = translator.translate(text, source) or text
                    except Exception as exc:
                        log.error("Překlad selhal: %s", exc)
                        continue
                    if echoes_source(shown, text):
                        log.warning("Překlad zůstal v originále: %s", text[:80])
                        continue
                shown = collapse_repeated_words(shown)
                if not shown or is_skipped_caption(shown) or not shown_recent.accept(start, shown):
                    log.warning("Přeskočen přelud přepisu: %s", shown[:80])
                    continue
                srt.add(start, end, shown)
                original.add(start, end, text)
                subtitle_queue.put(shown)
    except Exception as exc:
        log.error("Zpracování skončilo: %s", exc)
    finally:
        cap.stop()
        srt.flush()
        original.flush()


class CzechReader:
    """Přečte hotový český titulek. Samotný překlad se nemění."""

    def __init__(self, cap: AudioCapture, on_frame, overlay: SubtitleOverlay) -> None:
        from .windows.speak import Speaker

        self.cap = cap
        self.on_frame = on_frame
        self.overlay = overlay
        self.speaker = Speaker()
        self.speaker.set_rate(6)
        self.overlay.set_on_speed(self.speaker.set_rate)
        self.overlay.set_on_volume(self.speaker.set_volume)
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
    original = SrtWriter(ORIGINAL_SRT_PATH)
    print(f"Titulky se ukládají do {SRT_PATH}")
    print(f"Originál se ukládá do {ORIGINAL_SRT_PATH}")
    print("Terminál se minimalizuje. Obnovíte ho z hlavního panelu, zastavíte klávesami Ctrl+C.")
    minimize_console()

    proc_thread = threading.Thread(
        target=run_processing,
        args=(cap, translator, srt, original, args.lang, model, audio, subtitle_queue, chunk_seconds),
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
