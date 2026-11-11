"""Přepnutí výchozího výstupu Windows, stejně jako u VoIP ve vzorovém projektu.

Edge bere výchozí zařízení. Aby hovor šel do Cable In, nastaví se přehrávání
přes AudioDeviceCmdlets. Český hlas se přehrává zvlášť, ne přes tenhle výstup.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

log = logging.getLogger("titulky.windows")

_CABLE = ("CABLE In 16 Ch", "CABLE In 16ch", "CABLE Input")
_CABLE_SPEAKERS = (
    "Reproduktory (VB-Audio Virtual Cable)",
    "Speakers (VB-Audio Virtual Cable)",
)
_CABLE_OUT = ("CABLE Output", "CABLE Out 16 Ch", "CABLE Out")
_SPEAKERS = ("Reproduktory (Realtek USB Audio)", "Reproduktory", "Speakers")
_saved_default: str | None = None
_saved_comm: str | None = None
_mic_routed = False
_mic_play: str | None = None
_mic_play_comm: str | None = None
_mic_rec: str | None = None
_mic_rec_comm: str | None = None
_lock = threading.Lock()

_SET = r"""
$ErrorActionPreference = "Stop"
Import-Module AudioDeviceCmdlets -ErrorAction Stop
$want = $env:TITULKY_DEVICE
$role = $env:TITULKY_ROLE
$kind = $env:TITULKY_TYPE
if (-not $kind) { $kind = "Playback" }
$all = @(Get-AudioDevice -List | Where-Object { $_.Type -eq $kind })
$device = $all | Where-Object { $_.Name -eq $want } | Select-Object -First 1
if (-not $device) {
    $device = $all | Where-Object { $_.Name -like "$want*" } | Select-Object -First 1
}
if (-not $device) {
    $matching = @($all | Where-Object { $_.Name -like "*$want*" })
    if ($want -match "Reproduktory|Speakers|Realtek") {
        $matching = @($matching | Where-Object {
            $_.Name -notlike "*VB-Audio*" -and $_.Name -notlike "*Virtual*" -and $_.Name -notlike "*CABLE*"
        })
    }
    $device = $matching | Select-Object -First 1
}
if (-not $device) {
    Write-Output "ERROR: Zarizeni '$want' nenalezeno"
    exit 1
}
if ($role -eq "default" -or $role -eq "both") {
    Set-AudioDevice -ID $device.ID -DefaultOnly -ErrorAction Stop | Out-Null
}
if ($role -eq "comm" -or $role -eq "both") {
    Set-AudioDevice -ID $device.ID -CommunicationOnly -ErrorAction Stop | Out-Null
}
Start-Sleep -Milliseconds 200
Write-Output "OK: $($device.Name)"
exit 0
"""

_GET = r"""
$ErrorActionPreference = "Stop"
Import-Module AudioDeviceCmdlets -ErrorAction Stop
$playback = Get-AudioDevice -Playback
$comm = Get-AudioDevice -PlaybackCommunication
Write-Output "DEFAULT:$($playback.Name)"
Write-Output "COMM:$($comm.Name)"
exit 0
"""

_GET_IN = r"""
$ErrorActionPreference = "Stop"
Import-Module AudioDeviceCmdlets -ErrorAction Stop
$recording = Get-AudioDevice -Recording
$comm = Get-AudioDevice -RecordingCommunication
Write-Output "DEFAULT:$($recording.Name)"
Write-Output "COMM:$($comm.Name)"
exit 0
"""


def current_outputs() -> tuple[str, str]:
    code, out = _powershell(_GET)
    if code != 0:
        raise RuntimeError(out or "výchozí výstup nejde zjistit")
    default = comm = ""
    for line in out.splitlines():
        if line.startswith("DEFAULT:"):
            default = line.split(":", 1)[1].strip()
        elif line.startswith("COMM:"):
            comm = line.split(":", 1)[1].strip()
    if not default:
        raise RuntimeError("výchozí výstup je prázdný")
    return default, comm


def current_inputs() -> tuple[str, str]:
    code, out = _powershell(_GET_IN)
    if code != 0:
        raise RuntimeError(out or "výchozí mikrofon nejde zjistit")
    default = comm = ""
    for line in out.splitlines():
        if line.startswith("DEFAULT:"):
            default = line.split(":", 1)[1].strip()
        elif line.startswith("COMM:"):
            comm = line.split(":", 1)[1].strip()
    if not default:
        raise RuntimeError("výchozí mikrofon je prázdný")
    return default, comm


def route_speakers_to_microphone() -> str:
    """Pošle reproduktory do CABLE a ten nastaví jako mikrofon. Zvuk není slyšet."""
    global _mic_routed, _mic_play, _mic_play_comm, _mic_rec, _mic_rec_comm
    with _lock:
        if _mic_routed:
            return _mic_play or ""
        play, play_comm = current_outputs()
        rec, rec_comm = current_inputs()
        if not _is_cable(play):
            _mic_play = play
            _mic_play_comm = play_comm
        if not _is_cable_out(rec):
            _mic_rec = rec
            _mic_rec_comm = rec_comm
        playback = ""
        for pattern in (*_CABLE_SPEAKERS, *_CABLE):
            playback = _try_set(pattern, "both") or ""
            if playback:
                break
        if not playback:
            raise RuntimeError("CABLE In ve Windows není. Nainstalujte VB-Audio Virtual Cable.")
        recording = ""
        for pattern in _CABLE_OUT:
            recording = _try_set(pattern, "both", "Recording") or ""
            if recording:
                break
        if not recording:
            if _mic_play:
                _try_set(_mic_play, "default")
            if _mic_play_comm:
                _try_set(_mic_play_comm, "comm")
            _mic_play = None
            _mic_play_comm = None
            _mic_rec = None
            _mic_rec_comm = None
            raise RuntimeError("CABLE Output ve Windows není. Nainstalujte VB-Audio Virtual Cable.")
        _mic_routed = True
        log.info("reproduktory %s jdou do mikrofonu %s", playback, recording)
        return recording


def restore_microphone_route() -> None:
    """Vrátí reproduktory a mikrofon, které byly před českým přepisem."""
    global _mic_routed, _mic_play, _mic_play_comm, _mic_rec, _mic_rec_comm
    with _lock:
        if not _mic_routed:
            return
        if _mic_play:
            _try_set(_mic_play, "default")
        if _mic_play_comm:
            _try_set(_mic_play_comm, "comm")
        if _mic_rec:
            _try_set(_mic_rec, "default", "Recording")
        if _mic_rec_comm:
            _try_set(_mic_rec_comm, "comm", "Recording")
        log.info("reproduktory a mikrofon vráceny")
        _mic_routed = False
        _mic_play = None
        _mic_play_comm = None
        _mic_rec = None
        _mic_rec_comm = None


def ensure_speakers() -> str:
    """Po startu a bez předčítání má originál hrát do reproduktorů, ne do kabelu."""
    with _lock:
        default, _comm = current_outputs()
        if not _is_cable(default):
            return default
        return _restore_playback()


def switch_to_cable() -> str:
    """Uloží současné reproduktory a nastaví Windows přehrávání na Cable In."""
    global _saved_default, _saved_comm
    with _lock:
        return _switch_to_cable()


def _switch_to_cable() -> str:
    global _saved_default, _saved_comm
    default, comm = current_outputs()
    if not _is_cable(default):
        _saved_default = default
        _saved_comm = comm
    for pattern in _CABLE:
        name = _try_set(pattern, "both")
        if name:
            log.info("Windows výstup: %s", name)
            return name
    raise RuntimeError("CABLE In ve Windows není. Nainstalujte VB-Audio Virtual Cable.")


def restore_playback() -> str:
    """Vrátí výstup, který byl zapnutý před přepnutím na kabel."""
    with _lock:
        return _restore_playback()


def _restore_playback() -> str:
    global _saved_default, _saved_comm
    if _mic_routed:
        log.info("reproduktory nechávám na CABLE, běží český přepis")
        return ""
    patterns = []
    if _saved_default and not _is_cable(_saved_default):
        patterns.append(_saved_default)
    patterns.extend(_SPEAKERS)
    seen: set[str] = set()
    restored = ""
    for pattern in patterns:
        key = pattern.casefold()
        if key in seen:
            continue
        seen.add(key)
        name = _try_set(pattern, "default")
        if name and not _is_cable(name):
            restored = name
            break
    if not restored:
        raise RuntimeError("Původní reproduktory se nepodařilo vrátit.")
    if _saved_comm:
        _try_set(_saved_comm, "comm")
    _saved_default = None
    _saved_comm = None
    log.info("Windows výstup zpět: %s", restored)
    return restored


def _is_cable(name: str) -> bool:
    text = name.casefold()
    return "cable in" in text or "cable input" in text


def _is_cable_out(name: str) -> bool:
    text = name.casefold()
    return "cable out" in text or "cable output" in text


def _try_set(pattern: str, role: str, kind: str = "Playback") -> str | None:
    env = os.environ.copy()
    env["TITULKY_DEVICE"] = pattern
    env["TITULKY_ROLE"] = role
    env["TITULKY_TYPE"] = kind
    code, out = _powershell(_SET, env)
    for line in out.splitlines():
        if line.startswith("OK:"):
            return line[3:].strip()
    if code != 0:
        log.info("výstup '%s' nejde: %s", pattern, out.strip())
    return None


def _powershell(script: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 1, "přepnutí zvuku trvalo příliš dlouho"
    out = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    return result.returncode, out
