"""Čtení titulků hlasem Windows do zvoleného výstupu.

SAPI přehrává na výchozí zařízení. To by se smíchalo s hovorem, který se
nahrává smyčkou. Proto se věta složí do paměti a přehraje se přes WASAPI
na vybraný výstup, třeba sluchátka.
"""

from __future__ import annotations

import logging
import queue
import threading
import winreg
from dataclasses import dataclass

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger("titulky.speak")
_VOICE_ROOT = r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
_CZECH = "405"
# SAPI SpeechAudioFormatType: 48 kHz, 16 bit, stereo.
_SAFT_48K_16_STEREO = 39


@dataclass(frozen=True)
class PlaybackDevice:
    index: int
    name: str
    rate: int

    @property
    def label(self) -> str:
        clean = self.name.strip()
        if len(clean) > 32:
            clean = clean[:31] + "…"
        return f"Hlas do: {clean}"


def list_playback_devices() -> list[PlaybackDevice]:
    """Výstupní zařízení WASAPI, bez loopbacku pro nahrávání."""
    pa = pyaudio.PyAudio()
    try:
        try:
            host = int(pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"])
        except OSError:
            return []
        devices: list[PlaybackDevice] = []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if int(info["hostApi"]) != host:
                continue
            if int(info.get("maxOutputChannels") or 0) < 1:
                continue
            if info.get("isLoopbackDevice"):
                continue
            rate = int(float(info.get("defaultSampleRate") or 48_000))
            devices.append(PlaybackDevice(index, str(info["name"]), rate))
        return devices
    finally:
        pa.terminate()


def prefer_playback(devices: list[PlaybackDevice], capture_label: str) -> PlaybackDevice:
    """Výstup, který uživatel slyší. Sluchátka mají přednost, jinak reproduktory."""
    others = [device for device in devices if not same_endpoint(device.name, capture_label)]
    phones = [device for device in others if _headphones(device.name)]
    if phones:
        return phones[0]
    heard = [device for device in devices if _headphone_speakers(device.name)]
    if heard:
        return heard[0]
    usable = [
        device
        for device in others
        if not _silent_output(device.name) and not _display_output(device.name)
    ]
    if usable:
        return usable[0]
    pool = others or list(devices)
    audible = [
        device
        for device in pool
        if not _silent_output(device.name) and not _display_output(device.name)
    ]
    return audible[0] if audible else devices[0]


def _headphone_speakers(name: str) -> bool:
    """Reproduktory na sluchátkovém výstupu, ne virtuální kabel."""
    text = name.casefold()
    return "reproduktory" in text and "realtek" in text and "virtual" not in text


def _headphones(name: str) -> bool:
    text = name.casefold()
    return any(word in text for word in ("headphone", "headset", "sluchát", "earphone", "airpod"))


def _silent_output(name: str) -> bool:
    text = name.casefold()
    return any(word in text for word in ("digital output", "virtual cable", "vb-audio", "cable in"))


def _display_output(name: str) -> bool:
    text = name.casefold()
    return "nvidia high definition" in text or "hdmi" in text or "displayport" in text


def same_endpoint(playback_name: str, capture_label: str) -> bool:
    left = _compact(playback_name)
    right = _compact(capture_label)
    if not left or not right:
        return False
    return left in right or right in left


def _compact(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _czech_voice_id() -> str | None:
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _VOICE_ROOT)
    except OSError:
        return None
    try:
        count = winreg.QueryInfoKey(key)[0]
        for index in range(count):
            name = winreg.EnumKey(key, index)
            try:
                attrs = winreg.OpenKey(key, name + r"\Attributes")
                language, _typ = winreg.QueryValueEx(attrs, "Language")
            except OSError:
                continue
            if str(language).lower().startswith(_CZECH):
                return rf"HKEY_LOCAL_MACHINE\{_VOICE_ROOT}\{name}"
    finally:
        winreg.CloseKey(key)
    return None


class Speaker:
    """Fronta vět. Čte je českým hlasem, když je v systému nainstalovaný."""

    def __init__(self) -> None:
        self.enabled = False
        self.rate = 6
        self.volume = 55
        self.device_index: int | None = None
        self.device_rate = 48_000
        self._device_token = 0
        self.last_error = ""
        self.voice_id = _czech_voice_id()
        self.playing = False
        self._queue: queue.Queue[tuple[int, str]] = queue.Queue()
        self._gen = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="speak", daemon=True)
        self._thread.start()

    def set_device(self, index: int, rate: int) -> None:
        self.device_index = index
        self.device_rate = rate or 48_000
        self._device_token += 1
        self.last_error = ""

    def set_rate(self, rate: int) -> None:
        self.rate = max(-10, min(10, int(rate)))

    def set_volume(self, percent: int) -> None:
        self.volume = max(0, min(100, int(percent)))

    def say(self, text: str) -> None:
        cleaned = " ".join(text.split())
        if not self.enabled or len(cleaned) < 2:
            return
        while self._queue.qsize() >= 2:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put((self._gen, cleaned))
        log.info("čtu: %s", cleaned[:90])

    def clear(self) -> None:
        self._gen += 1
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def shutdown(self) -> None:
        self.enabled = False
        self.clear()
        self._stop.set()
        self._queue.put((self._gen, ""))

    def _loop(self) -> None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        voice = None
        pa = None
        try:
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            if self.voice_id:
                token = comtypes.client.CreateObject("SAPI.SpObjectToken")
                token.SetId(self.voice_id)
                voice.Voice = token
            pa = None
            heard = -1
            while not self._stop.is_set():
                generation, text = self._queue.get()
                if self._stop.is_set() or not text or generation != self._gen or not self.enabled:
                    continue
                if self.device_index is None:
                    self.last_error = "Není vybraný výstup pro čtení."
                    continue
                if pa is None or heard != self._device_token:
                    if pa is not None:
                        pa.terminate()
                    pa = pyaudio.PyAudio()
                    heard = self._device_token
                try:
                    pcm, source_rate, channels = _synthesize(voice, text, self.rate)
                    self._play(pa, pcm, source_rate, channels, generation)
                    if generation == self._gen:
                        self.last_error = ""
                except Exception as exc:
                    self.last_error = f"Čtení selhalo: {exc}"
                    log.warning("čtení: %s", exc)
        finally:
            if pa is not None:
                pa.terminate()
            comtypes.CoUninitialize()

    def _play(
        self,
        pa: pyaudio.PyAudio,
        pcm: bytes,
        source_rate: int,
        channels: int,
        generation: int,
    ) -> None:
        block = _pcm_block(pcm, channels)
        if block.size == 0:
            return
        rate = self.device_rate or source_rate
        if source_rate != rate:
            block = _resample_block(block, source_rate, rate)
        if block.shape[1] == 1:
            block = np.repeat(block, 2, axis=1)
        output = np.ascontiguousarray(block[:, :2], dtype=np.int16)
        self.playing = True
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=output.shape[1],
                rate=rate,
                output=True,
                output_device_index=self.device_index,
                frames_per_buffer=2048,
            )
            try:
                raw = output.tobytes()
                frame = output.shape[1] * 2
                step = max(frame, (rate // 10) * frame)
                for offset in range(0, len(raw), step):
                    if self._stop.is_set() or not self.enabled or generation != self._gen:
                        break
                    chunk = raw[offset : offset + step]
                    chunk = chunk[: len(chunk) - (len(chunk) % frame)]
                    if chunk:
                        stream.write(scale_pcm(chunk, self.volume))
            finally:
                stream.stop_stream()
                stream.close()
        finally:
            self.playing = False


def scale_pcm(pcm: bytes, percent: int) -> bytes:
    """Zeslabí hotový záznam čtení. 100 % nechá vzorky beze změny."""
    gain = max(0, min(100, int(percent))) / 100.0
    if gain >= 0.999 or not pcm:
        return pcm
    usable = len(pcm) - (len(pcm) % 2)
    if usable == 0:
        return pcm
    samples = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float32)
    scaled = np.clip(np.rint(samples * gain), -32768, 32767).astype(np.int16)
    return scaled.tobytes()


def _pcm_block(pcm: bytes, channels: int) -> np.ndarray:
    samples = np.frombuffer(pcm, dtype=np.int16)
    channels = max(1, channels)
    usable = (samples.size // channels) * channels
    if usable == 0:
        return np.zeros((0, channels), dtype=np.int16)
    return np.ascontiguousarray(samples[:usable].reshape(-1, channels))


def _resample_block(block: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if block.size == 0 or source_rate == target_rate:
        return block
    new_len = int(round(block.shape[0] * target_rate / source_rate))
    if new_len <= 0:
        return np.zeros((0, block.shape[1]), dtype=np.int16)
    old_x = np.linspace(0.0, 1.0, num=block.shape[0], endpoint=False)
    new_x = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
    columns = [np.interp(new_x, old_x, block[:, index].astype(np.float32)) for index in range(block.shape[1])]
    stacked = np.stack(columns, axis=1)
    return np.clip(np.rint(stacked), -32768, 32767).astype(np.int16)


def _synthesize(voice, text: str, rate: int) -> tuple[bytes, int, int]:
    import comtypes.client

    stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
    audio_format = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    audio_format.Type = _SAFT_48K_16_STEREO
    stream.Format = audio_format
    voice.AudioOutputStream = stream
    voice.Rate = rate
    voice.Speak(text)
    wave = stream.Format.GetWaveFormatEx()
    source_rate = int(wave.SamplesPerSec or 48_000)
    channels = int(wave.Channels or 2)
    stream.Seek(0)
    return bytes(stream.GetData()), source_rate, channels
