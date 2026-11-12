# Titulky_5

Přepis zvuku z počítače do českých titulků.

## Co program dělá

Poslouchá zvuk, který hraje ve Windows, řeč přepíše a přeloží do češtiny. Titulky se ukazují v průhledném okně dole na obrazovce a průběžně se zapisují do dvou souborů:

- `C:\Titulky_5\titulky.srt` — český překlad
- `C:\Titulky_5\original.srt` — záznam v originálním jazyku

Po startu programu se oba soubory vyprázdní a záznam začne znovu. Text, který si chcete nechat, zkopírujte ještě před dalším spuštěním.

Se soubory můžete pracovat. Vložte je do AI a nechte si vypsat stručný obsah, děj filmu nebo text písně.

Překlad běží v počítači, nepřipojuje se na internetovou službu. Zdrojové jazyky jsou angličtina, němčina, francouzština, španělština, italština, polština, čínština a japonština. Bez volby jazyka ho program pozná sám.

Po startu se na 15 sekund ukáže nápis **Program Tituly - zdarma** a pod ním červeně softrobot.cz. Pak začnou běžné titulky.

## Spuštění a ukončení

Poklepejte na `spustit.bat`.

Dávka před instalací pozná počítač, připraví prostředí uv a program spustí. Terminál se po startu schová do hlavního panelu. Obnovíte ho odtud. Ukončení je Ctrl+C v tom okně.

Nové prostředí se zakládá s Pythonem 3.12.2. Už hotové prostředí se při dalším spuštění nepřepisuje.

Ruční spuštění z adresáře projektu:

```bat
set PYTHONPATH=src
.venv\Scripts\python.exe -m muj_projekt.windous
```

Další volby:

```bat
.venv\Scripts\python.exe -m muj_projekt.windous --devices
.venv\Scripts\python.exe -m muj_projekt.windous --lang en
```

`--devices` jen vypíše zvuková zařízení. `--lang` podrží zdrojový jazyk (`en`, `de`, `fr`, `es`, `it`, `pl`, `zh`, `ja`).

## Jaký počítač se pozná

**NVIDIA, i když je v počítači zároveň Intel grafika**  
Přepis běží na kartě NVIDIA modelem large-v3. Nainstalují se i CUDA knihovny.

**AMD a Radeon, včetně AMD AI**  
Přepis běží na procesoru modelem small. CUDA knihovny se neinstalují.

**Intel Nova Lake, Intel Arc nebo NPU**  
Přepis běží na procesoru. Tuto kartu a NPU program nepoužívá.

**Běžný Intel, třeba s 32 GB paměti**  
Přepis běží na procesoru modelem small.

Překlad do češtiny je na všech počítačích na procesoru. Doporučeno je 32 GB paměti a asi 10 GB volného místa. Modely se stahují jen jednou.

## Ovládání v okně

Vpravo od titulků je ozubené kolečko. Kliknutí otevře nabídku.

| Položka | Co udělá |
| --- | --- |
| Ukončit | zavře program |
| Skrýt | schová titulky, přepis dál běží |
| Zobrazit | titulky znovu ukáže |
| Číst CZ | čte české titulky nahlas |
| Váš dar | otevře `docs\dar.pdf` |
| Rychlost čtení | 0,8×, 1×, 1,3×, 1,6× |
| Délka konverzace | šipka dolů a nahoru, po jedné sekundě |

Po startu je rychlost čtení 1,3× a délka konverzace 2 sekundy. Délka jde od 1 do 12 sekund. Na 1 s je šipka dolů neaktivní, na 12 s je neaktivní šipka nahoru.

Kratší úsek ukáže text dřív a hodí se k poslechu. Delší úsek nechá větu doběhnout a hodí se k záznamu do `titulky.srt` a `original.srt`.

Kolečko zůstane na místě i po skrytí titulků, aby šly znovu zapnout.

## Čtení česky

Položka Číst CZ je v nabídce u ucha. Zapnutá je modře.

Originál pak nejde do reproduktorů, ale do virtuálního kabelu VB-Audio. Do reproduktorů hraje jen český hlas. Překlad dál poslouchá originál z kabelu, takže se neztratí.

Vypnutí Číst CZ, nebo ukončení programu, vrátí zvuk do reproduktorů.

Je potřeba nainstalovaný VB-Audio Virtual Cable a ve Windows český hlas Jakub. Při rychlé angličtině čtení nemusí stačit tempu, protože česká věta je delší než původní. Titulky na obrazovce přitom běží dál.

## Soubory

| Soubor | Účel |
| --- | --- |
| `spustit.bat` | spuštění a instalace |
| `rozpoznat.ps1` | rozpoznání počítače |
| `requirements.txt` | společné knihovny |
| `requirements-cuda.txt` | CUDA knihovny, jen pro NVIDIA |
| `titulky.srt` | český překlad, po startu se maže |
| `original.srt` | originál, po startu se maže |
| `docs\dar.pdf` | text k položce Váš dar |
| `docs\Navod.txt` | tento návod v prostém textu |
