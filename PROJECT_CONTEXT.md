# Titulky_5 — kontext pro další opravy

Živé české titulky ze zvuku Windows. Na NVIDIA a AMD přepisuje Whisper large-v3 na grafice, jinde model small na procesoru. Překlad je lokální NLLB do češtiny a běží na procesoru. Hotový text jde do průhledného okna a do `titulky.srt`. Volitelně ho čte český hlas Jakub.

Tento soubor je mapa projektu k 11. 11. 2026. Starší projekt `C:\Titulky_4_PC_RTX` je jiný program. Odtud se převzalo jen čtení a přepínání zvuku.

## Spuštění

```
spustit.bat
```

Dávka přepne UTF-8, případně doinstaluje uv, založí `.venv`, rozpozná počítač, nainstaluje balíky a spustí:

```
set PYTHONPATH=src
python -m muj_projekt.windous
```

Nové prostředí se zakládá jako Python 3.12.2 (`uv venv --python 3.12.2`). Když 3.12.2 nejde stáhnout, zkusí se 3.12. Existující `.venv` se nemaže. Na stroji s RTX 3090 už běží Python 3.12.13 a má se nechat být.

Po úspěšném startu se terminál minimalizuje. Obnoví se z hlavního panelu. Zastavení je Ctrl+C v tom okně.

## Počítače a instalace

`rozpoznat.ps1` vypíše popis na stderr a na stdout jen jedno slovo profilu. Dávka smí číst jen to slovo. Názvy karet obsahují závorky a `|` a v bloku `for (...)` dávku rozbijí.

| Profil | Kdy | Co se instaluje | Přepis |
|---|---|---|---|
| NVIDIA | v názvu karty je NVIDIA, i když je v počítači i Intel grafika | `requirements.txt` a `requirements-cuda.txt` | large-v3, CUDA float16, jinak int8 |
| AMD | Radeon nebo AMD a žádná NVIDIA | `requirements.txt` a `requirements-amd.txt`, wheel ctranslate2 s ROCm | large-v3, ROCm float16, jinak int8_float16 |
| INTEL_AI | Nova Lake, Arc nebo NPU a žádná NVIDIA ani Radeon | jen `requirements.txt` | small, CPU int8 |
| INTEL | ostatní, včetně běžného Intelu s 32 GB RAM | jen `requirements.txt` | small, CPU int8 |

Ověřený stroj: Intel i7-12700K, Intel UHD 770, RTX 3090, 128 GB RAM. Profil je NVIDIA.

CUDA knihovny jsou jen pro Whisper přes ctranslate2:

- `nvidia-cublas-cu12==12.9.2.10`
- `nvidia-cuda-runtime-cu12==12.9.79`
- `nvidia-cuda-nvrtc-cu12==12.9.86`

`enable_cuda_dlls()` v `windous.py` je musí přidat na PATH dřív, než se importuje ctranslate2. Bez `cublas64_12.dll` se CUDA přeskočí a nesmí se pak znovu zkoušet cuda int8, to umí viset.

AMD nepoužívá CUDA knihovny. `instalovat_amd.ps1` stáhne ROCm runtime z `requirements-amd.txt` a nahradí PyPI ctranslate2 wheelem z vydání 4.9.0, ve kterém je jádro pro gfx1151 (Radeon 8060S). V ctranslate2 se AMD karta hlásí jako zařízení `cuda`. `enable_rocm_dlls()` přidá `hipblas.dll`. Když `cublas64_12.dll` chybí a `hipblas.dll` je, přepis jde na AMD grafiku, ne na procesor. Na počítači s NVIDIA se ROCm wheel neinstaluje a zůstává CUDA.

PyTorch se instaluje jako CPU verze a jen když v prostředí ještě není. Překlad NLLB na ní stojí. Nepřepínat ho na CUDA torch, pokud o to není výslovně požádáno. Velké stažení a může rozbít funkční překlad.

Whisper z tohoto projektu nepoužívá Intel GPU ani NPU. Na Intelu a Nova Lake proto zůstává procesor.

## Adresáře

```
C:\Titulky_5\
├── spustit.bat
├── rozpoznat.ps1
├── instalovat_amd.ps1
├── requirements.txt
├── requirements-cuda.txt
├── requirements-amd.txt
├── titulky.srt                 # jediný správný soubor titulků
├── docs\dar.pdf                # otevře položka Váš dar
├── PROJECT_CONTEXT.md
└── src\muj_projekt\
    ├── windous.py              # orchestrace, záměrný název souboru
    ├── config\config.py
    ├── files\writer.py         # SRT
    ├── print\display.py        # výpis zařízení
    ├── windows\window.py       # průhledné okno a nabídka
    ├── windows\speak.py        # SAPI + WASAPI čtení
    ├── windows\audio_route.py  # přepnutí Windows na VB kabel
    └── models\
        ├── models--Systran--faster-whisper-large-v3
        ├── models--Systran--faster-whisper-small
        └── hf\                 # NLLB facebook/nllb-200-distilled-600M
```

`MODELS_DIR` musí zůstat `src/muj_projekt/models`. Přesun large-v3 by při dalším startu na GPU znovu stahoval asi 3 GB. Model se nemazat.

`PROJECT_ROOT` je rodič `src`. SRT se píše do `C:\Titulky_5\titulky.srt`, ne do balíku.

## Tok programu

`main()` v `windous.py`:

1. `AudioCapture` otevře WASAPI loopback výchozích reproduktorů.
2. `create_whisper()` vybere model podle profilu výše.
3. `Translator` načte NLLB. V `__init__` i v `translate()` musí být `import torch`. `generation_config.max_length = None`, jinak transformers při každém titulku hlásí spor `max_length` a `max_new_tokens`.
4. `SubtitleOverlay.show_intro()` na 15 s ukáže bílý text `Program Tituly - zdarma` a pod ním červeně `softrobot.cz`. Titulky, které mezitím přijdou, se jen schovají a ukážou se po úvodu. Když žádné nepřijdou, následuje `Poslouchám…`.
5. Vlákno `run_processing` bere úseky ze `AudioBuffer`, přepíše je, přeloží a pošle text do fronty. UI ho jednou za 100 ms vykreslí a při zapnutém čtení předá `CzechReader.say()`.

Délka úseku je `ChunkSeconds`. Po startu jsou 2 s. Šipky v nabídce jdou od 1 do 12 s. Šipka dolů je neaktivní na 1 s, šipka nahoru na 12 s. Delší úsek je lepší pro souvislý záznam, kratší pro poslech. Krátká věta do 16 slov má pořád `max_new_tokens=64`. Delší věta dostane víc tokenů, nejvýš 160, aby se překlad neutnul.

`is_spelled_noise()` zahodí přelud Whisperu typu `E-E-E-E` nebo `S-S-S-S` ještě před překladem. Běžné slovo jako `E-mail` nechá být.

## Okno

`SubtitleOverlay` v `windows/window.py`.

Okno je `overrideredirect`, vždy navrchu, průhledná barva je černá. Čistá černá je tedy neviditelná. Obrys písma je `#010101`, síla 3 px, text je bílý a ukotvený dole. Šířka je třetina obrazovky, výška 200 px, spodní mezera 120 px.

Kliknutí propadají jen oknem titulků. Styl `WS_EX_TRANSPARENT` se nastavuje na rodiče `winfo_id()`, ne na vnitřní hwnd. Po nastavení layered stylu se musí znovu položit color key. Jinak text zmizí nebo okno přestane být průhledné.

Ovládání je samostatné `Toplevel` okno vpravo vedle titulků, bez `WS_EX_TRANSPARENT`, pořád navrchu. Kdyby leželo uvnitř titulkového okna, kliknutí se ztratí. Kolečko i řádky nabídky se bindují jen jednou. Vazba na plátno i na jeho rodiče kliknutí spočítá dvakrát a nabídka se hned zavře.

Nabídka kolečka:

- Ukončit
- Skrýt / Zobrazit. Skrýt schová titulky, přepis běží dál, kolečko zůstane.
- Číst CZ. Zapnuto je modře.
- Váš dar. Otevře `docs\dar.pdf`.
- Rychlost čtení: 0,8×, 1×, 1,3×, 1,6×. Po startu 1,3×, tedy SAPI rate 3.
- Délka konverzace: šipky po 1 s, rozsah 1–12, start 2 s.

Ikony kolečka, ucha, křížku a pomlčky jsou výřezy z PNG v `windows\`. Pozadí ikony se při najetí znovu složí, aby zmizel čtverec kolem kresby.

## Čtení česky

Zapnutí Číst CZ:

1. `switch_to_cable()` uloží současné reproduktory a výchozí přehrávání Windows přepne na `CABLE In 16 Ch`. Originál z reproduktorů zmizí. Je potřeba VB-Audio Virtual Cable a modul PowerShellu AudioDeviceCmdlets.
2. Smyčka se přepne na loopback `CABLE In 16 Ch`, aby překlad dál slyšel originál.
3. Český hlas se přehraje přes WASAPI do `Reproduktory (Realtek USB Audio)`, ne do kabelu. Jinak by se hlas nahrál znovu.
4. Vypnutí nebo konec programu zavolá `restore_playback()` a vrátí smyčku na reproduktory.

Hlas je Windows SAPI, v registrech `MSTTS_V110_csCZ_Jakub`, jazyk `405`. Syntéza jde do paměti a pak na zvolené zařízení. Výchozí hlasitost je 55 %. Při rychlé angličtině čtení nestíhá, protože česká věta je delší. Fronta si nechá nejvýš dvě věty a starší čekající zahodí. Oči text stíhají, hlas ne. Na to se nesahá, pokud o to není požádáno.

Kód čtení a přepínání je převzatý z `Titulky_4_PC_RTX\titulky\speak.py` a `windows_audio.py`. Překlad NLLB se při tom nemění.

## Na co nesahat bez důvodu

- Nepřesouvat a nemazat `faster-whisper-large-v3`.
- Neměnit `translate()` kromě opravy, která zachová dnešní kvalitu. Krátké věty zůstávají na `max_new_tokens=64`, `num_beams=1`, `do_sample=False`.
- Neinstalovat CUDA torch jen proto, že je v počítači NVIDIA.
- Nesestavovat znovu `.venv`, dokud běží.
- V `spustit.bat` nedávat do `echo` uvnitř `if (...)` kulaté závorky. Starší znění `echo ... (Python 3.12)` dávku hned zavřelo.
- Do `for /f (...)` nedávat surový název karty. Stačí profil `NVIDIA`, `AMD`, `INTEL_AI` nebo `INTEL`.
- Titulkové okno nenechat překrýt klikací tlačítko.
- `windous.py` je vstupní modul. Nejmenovat ho na `windows.py`, to by se srazilo s balíkem `windows`.

## Rychlá místa oprav

| Příznak | Kam |
|---|---|
| Dávka se hned zavře | `spustit.bat`, závorky v `echo` nebo `\|` a `)` z názvu karty |
| CUDA se nepoužije, chybí `cublas64_12.dll` | `requirements-cuda.txt`, `enable_cuda_dlls()` |
| AMD přepisuje na procesoru | `instalovat_amd.ps1`, `prepare_gpu()` musí vrátit `hip` |
| Titulky anglicky, v logu `torch is not defined` | `Translator.translate`, import torch i uvnitř metody |
| Pořád dokola varování max_length | `generation_config.max_length = None` |
| Text není vidět nebo okno není průhledné | color key a `WS_EX_TRANSPARENT` v `_click_through` |
| Klik na kolečko nic neudělá | tlačítko je pod průhledným oknem, nebo je klik navázaný dvakrát |
| V SRT je `E-E-E-E` | `is_spelled_noise()` |
| Čtení hraje originál dál z reproduktorů | `CzechReader` a `switch_to_cable()` |
| Čtení se přepisuje znovu | hlas nejde do kabelu, ale do Realtek reproduktorů |
| Špatná délka úseku po startu | `ChunkSeconds` i `SubtitleOverlay._length` musí být stejné |
