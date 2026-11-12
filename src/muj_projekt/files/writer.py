from pathlib import Path

def srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

class SrtWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.blocks: list[tuple[str, str, str]] = []  # (start, end, text)
        self.path.write_text("", encoding="utf-8")

    def add(self, start: float, end: float, text: str) -> None:
        if not text.strip():
            return
        self.blocks.append((srt_timestamp(start), srt_timestamp(max(end, start + 0.8)), text))
        self.flush()

    def flush(self) -> None:
        lines = []
        for i, (start, end, text) in enumerate(self.blocks, start=1):
            lines.append(f"{i}\n{start} --> {end}\n{text}\n")
        self.path.write_text("\n".join(lines), encoding="utf-8")
