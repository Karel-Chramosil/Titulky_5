import ctypes
import os
import tkinter as tk
from pathlib import Path

from ..config import PROJECT_ROOT


def enable_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class SubtitleOverlay:
    """Průhledné okno s titulky na vrchu obrazovky."""

    def __init__(self, font_size=32, font_family="Arial"):
        self.font = (font_family, font_size, "bold")
        self.outline_px = 3
        self._outline_offsets = [
            (dx, dy)
            for dy in range(-self.outline_px, self.outline_px + 1)
            for dx in range(-self.outline_px, self.outline_px + 1)
            if dx * dx + dy * dy <= self.outline_px * self.outline_px and (dx or dy)
        ]
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "black")
        self.root.config(bg="black")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, bd=0)
        self.canvas.pack(expand=True, fill="both")

        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        import tkinter.font as tkfont
        self._line_px = tkfont.Font(self.root, font=self.font).metrics("linespace")

        self.width = screen_width // 3 + 500
        # Dva úvodní řádky musejí být nad sebou. Na vysokém DPI je písmo vyšší než původní mezera.
        intro_gap = 12
        self.height = max(210, self._line_px * 2 + intro_gap + 8 + self.outline_px)
        self.x = (screen_width - self.width) // 2
        self.y = screen_height - self.height - 120
        self.root.geometry(f"{self.width}x{self.height}+{self.x}+{self.y}")
        self.root.update_idletasks()
        self._hidden = False
        self._menu_open = False
        self._reading = False
        self._on_read_toggle = None
        self._speed_label = "1,6×"
        self._on_speed = None
        self._volume = 55
        self._on_volume = None
        self._length = 2
        self._on_length = None
        self._intro = False
        self._pending_text = ""
        self._build_gear()
        self._build_menu()
        self._click_through()
        self._make_clickable(self.gear)
        self._keep_controls_on_top()

    def _hwnd(self, widget) -> int:
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        return user32.GetParent(widget.winfo_id()) or widget.winfo_id()

    def _make_clickable(self, widget) -> None:
        """Ovládání nesmí dědit průhlednost titulků, jinak kliknutí propadne."""
        user32 = ctypes.windll.user32
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
        get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_long.restype = ctypes.c_size_t
        set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        set_long.restype = ctypes.c_size_t
        hwnd = self._hwnd(widget)
        style = get_long(hwnd, -20) & ~0x00000020
        set_long(hwnd, -20, style)
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        widget.lift()

    def _keep_controls_on_top(self) -> None:
        try:
            self.gear.lift()
            if self._menu_open:
                self.menu.lift()
        except tk.TclError:
            return
        self.root.after(300, self._keep_controls_on_top)

    def _click_through(self) -> None:
        """Kliknutí propadnou oknem. Černé pozadí zůstane průhledné, bílý text viditelný."""
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
        get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_long.restype = ctypes.c_size_t
        set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        set_long.restype = ctypes.c_size_t
        user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_uint32]
        user32.SetLayeredWindowAttributes.restype = ctypes.c_int

        hwnd = user32.GetParent(self.root.winfo_id())
        if not hwnd:
            return
        gwl_exstyle = -20
        ws_ex_layered = 0x00080000
        ws_ex_transparent = 0x00000020
        lwa_colorkey = 0x1
        style = get_long(hwnd, gwl_exstyle)
        set_long(hwnd, gwl_exstyle, style | ws_ex_layered | ws_ex_transparent)
        user32.SetLayeredWindowAttributes(hwnd, 0x00000000, 255, lwa_colorkey)
        self.root.attributes("-topmost", True)
        self.root.lift()
        if getattr(self, "gear", None) is not None:
            self._make_clickable(self.gear)

    def set_text(self, text: str):
        """Aktualizuje text. Během úvodu se titulek jen schová a ukáže po 15 sekundách."""
        text = (text or "").strip()
        if self._intro:
            if text:
                self._pending_text = text
            return
        self._render_caption(text)

    def show_intro(self) -> None:
        self._intro = True
        self._pending_text = ""
        self._paint_intro()
        self.root.after(15_000, self._end_intro)

    def _end_intro(self) -> None:
        if not self._intro:
            return
        self._intro = False
        self._render_caption(self._pending_text or "Poslouchám…")

    def _paint_intro(self) -> None:
        self.canvas.delete("all")
        x = self.width // 2
        red_y = self.height - 8
        white_y = red_y - self._line_px - 12
        self._draw_line("Program Titulky - zdarma", x, white_y, "white")
        self._draw_line("softrobot.cz", x, red_y, "#ff2a2a")

    def _render_caption(self, text: str) -> None:
        self.canvas.delete("all")
        if not text:
            return
        self._draw_line(text, self.width // 2, self.height - 8, "white")

    def _draw_line(self, text: str, x: int, y: int, fill: str) -> None:
        wrap = max(200, self.width - 40)
        for dx, dy in self._outline_offsets:
            self.canvas.create_text(
                x + dx, y + dy, text=text, fill="#010101", font=self.font,
                anchor="s", justify="center", width=wrap,
            )
        self.canvas.create_text(
            x, y, text=text, fill=fill, font=self.font,
            anchor="s", justify="center", width=wrap,
        )

    def _control_origin(self) -> tuple[int, int]:
        size = self.control_size
        return self.x + self.width + 10, self.y + self.height - size - 6

    def _build_gear(self) -> None:
        size = self.control_size = round(36 * 1.2)
        self.gear = tk.Toplevel(self.root)
        self.gear.overrideredirect(True)
        self.gear.attributes("-topmost", True)
        self.gear.configure(bg="#2a2a2a", cursor="hand2")
        gx, gy = self._control_origin()
        self.gear.geometry(f"{size}x{size}+{gx}+{gy}")
        canvas = tk.Canvas(
            self.gear, width=size, height=size, bg="#2a2a2a",
            highlightthickness=0, bd=0, cursor="hand2",
        )
        canvas.pack()
        self._gear_photo = self._photo_from_raster(
            self._icon_raster(Path(__file__).with_name("gear.png"), (19, 25, 55, 62), size),
            "#2a2a2a",
        )
        canvas.create_image(size / 2, size / 2, image=self._gear_photo)
        canvas.bind("<Button-1>", self._on_gear_click)

    def _on_gear_click(self, event=None) -> None:
        when = getattr(event, "time", None)
        if when is not None and when == getattr(self, "_last_gear_click", None):
            return
        self._last_gear_click = when
        self.toggle_menu()

    def _icon_raster(self, path: Path, crop: tuple[int, int, int, int], size: int):
        source = tk.PhotoImage(file=str(path))
        x0, y0, x1, y1 = crop
        sw, sh = x1 - x0 + 1, y1 - y0 + 1
        scale = size / max(sw, sh)
        dw, dh = max(1, round(sw * scale)), max(1, round(sh * scale))
        ox, oy = (size - dw) // 2, (size - dh) // 2
        raster = [[None] * size for _ in range(size)]
        for y in range(dh):
            sy = y0 + min(sh - 1, y * sh // dh)
            for x in range(dw):
                sx = x0 + min(sw - 1, x * sw // dw)
                red, green, blue = source.get(sx, sy)
                if red < 80 and green < 80 and blue < 80 and abs(red - green) < 18 and abs(green - blue) < 18:
                    continue
                raster[oy + y][ox + x] = (red, green, blue)
        return raster

    def _photo_from_raster(self, raster, background: str) -> tk.PhotoImage:
        size = len(raster)
        image = tk.PhotoImage(width=size, height=size)
        for y, row in enumerate(raster):
            pixels = []
            for pixel in row:
                if pixel is None:
                    pixels.append(background)
                else:
                    red, green, blue = pixel
                    pixels.append(f"#{red:02x}{green:02x}{blue:02x}")
            image.put("{" + " ".join(pixels) + "}", to=(0, y))
        return image

    def _plus_raster(self, horizontal):
        size = len(horizontal)
        plus = [[None] * size for _ in range(size)]
        for y in range(size):
            for x in range(size):
                across = horizontal[y][x]
                down = horizontal[x][y]
                if across is None:
                    plus[y][x] = down
                elif down is None or sum(across) >= sum(down):
                    plus[y][x] = across
                else:
                    plus[y][x] = down
        return plus

    def _build_menu(self) -> None:
        self.menu_w = 176
        self.menu_h = 136
        self.menu = tk.Toplevel(self.root)
        self.menu.overrideredirect(True)
        self.menu.attributes("-topmost", True)
        self.menu.configure(bg="#1e1e1e")
        self.menu.withdraw()
        folder = Path(__file__).parent
        icon_size = 36
        minus = self._icon_raster(folder / "minus.png", (19, 37, 57, 41), icon_size)
        self._menu_rasters = {
            "x": self._icon_raster(folder / "close.png", (20, 26, 58, 62), icon_size),
            "minus": minus,
            "plus": self._plus_raster(minus),
            "ear": self._icon_raster(folder / "ear.png", (26, 23, 51, 58), icon_size),
        }
        rows = [
            self._menu_row("x", "Ukončit", self.quit_app),
            self._menu_row("minus", "Skrýt", self.toggle_visibility),
            self._menu_row("ear", "Číst CZ", self.toggle_reading),
        ]
        self._build_volume()
        rows.append(self._menu_row("gift", "Váš dar", self.open_gift))
        self._menu_rows = tuple(rows)
        self._build_speeds()
        self._build_length()
        self.menu.update_idletasks()
        self.menu_w = max(210, self.menu.winfo_reqwidth())
        self.menu_h = max(180, self.menu.winfo_reqheight())

    def _menu_row(self, kind: str, label: str, command):
        row = tk.Frame(self.menu, bg="#1e1e1e", cursor="hand2")
        row.pack(fill="x")
        icon = tk.Canvas(row, width=36, height=36, bg="#1e1e1e", highlightthickness=0, bd=0, cursor="hand2")
        icon.pack(side="left", padx=(6, 4), pady=4)
        self._draw_menu_icon(icon, kind)
        text = tk.Label(row, text=label, font=("Segoe UI", 11), fg="white", bg="#1e1e1e", cursor="hand2")
        text.pack(side="left")

        def paint(color: str) -> None:
            row.configure(bg=color)
            icon.configure(bg=color)
            text.configure(bg=color)
            self._show_menu_icon(icon, color)

        def row_color(hover: bool) -> str:
            if kind == "ear" and self._reading:
                return "#1d4e89" if hover else "#1565c0"
            return "#3a3a3a" if hover else "#1e1e1e"

        def invoke(_event=None) -> None:
            command()

        for widget in (row, icon, text):
            widget.bind("<Enter>", lambda _event: paint(row_color(True)))
            widget.bind("<Leave>", lambda _event: paint(row_color(False)))
            widget.bind("<Button-1>", invoke)
        return icon, text, kind

    def _build_speeds(self) -> None:
        """Stejné stupně jako ve starším programu: 0,8×, 1×, 1,3× a 1,6×."""
        self._speeds = (("0,8×", -2), ("1×", 0), ("1,3×", 3), ("1,6×", 6))
        box = tk.Frame(self.menu, bg="#1e1e1e")
        box.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(
            box, text="Rychlost čtení", font=("Segoe UI", 10),
            fg="#d0d0d0", bg="#1e1e1e", anchor="w",
        ).pack(fill="x", pady=(2, 4))
        row = tk.Frame(box, bg="#1e1e1e")
        row.pack(fill="x")
        self._speed_buttons: dict[str, tk.Label] = {}
        for label, rate in self._speeds:
            button = tk.Label(
                row, text=label, font=("Segoe UI", 10), fg="white", bg="#2a2a2a",
                padx=6, pady=3, cursor="hand2",
            )
            button.pack(side="left", padx=(0, 4))
            button.bind("<Button-1>", lambda _event, name=label, value=rate: self.set_speed(name, value))
            self._speed_buttons[label] = button
        self._paint_speeds()

    def set_on_speed(self, callback) -> None:
        self._on_speed = callback

    def _build_volume(self) -> None:
        """Posuvník 0–100 %. Po startu 55 %, stejně jako ve starším programu."""
        box = tk.Frame(self.menu, bg="#1e1e1e")
        box.pack(fill="x", padx=8, pady=(0, 8))
        head = tk.Frame(box, bg="#1e1e1e")
        head.pack(fill="x", pady=(2, 4))
        tk.Label(
            head, text="Hlasitost", font=("Segoe UI", 10),
            fg="#d0d0d0", bg="#1e1e1e", anchor="w",
        ).pack(side="left")
        self._volume_value = tk.Label(
            head, text=f"{self._volume} %", font=("Segoe UI", 10),
            fg="white", bg="#1e1e1e", anchor="e",
        )
        self._volume_value.pack(side="right")
        self._volume_track = tk.Canvas(
            box, width=190, height=22, bg="#1e1e1e",
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self._volume_track.pack(fill="x")
        self._volume_track.bind("<Configure>", lambda _event: self._paint_volume())
        self._volume_track.bind("<Button-1>", self._volume_from_event)
        self._volume_track.bind("<B1-Motion>", self._volume_from_event)
        self._paint_volume()

    def set_on_volume(self, callback) -> None:
        self._on_volume = callback

    def _volume_from_event(self, event) -> None:
        width = self._volume_track.winfo_width()
        if width <= 1:
            width = 190
        pad = 8
        span = max(1, width - 2 * pad)
        percent = int(round((event.x - pad) / span * 100))
        percent = max(0, min(100, percent))
        if percent == self._volume:
            return
        self._volume = percent
        self._paint_volume()
        if self._on_volume is not None:
            self._on_volume(percent)

    def _paint_volume(self) -> None:
        canvas = self._volume_track
        width = canvas.winfo_width()
        if width <= 1:
            width = 190
        canvas.delete("all")
        pad = 8
        y = 11
        canvas.create_line(pad, y, width - pad, y, fill="#2a2a2a", width=4, capstyle="round")
        span = max(1, width - 2 * pad)
        x = pad + int(span * self._volume / 100)
        if self._volume:
            canvas.create_line(pad, y, x, y, fill="#1565c0", width=4, capstyle="round")
        canvas.create_oval(x - 7, y - 7, x + 7, y + 7, fill="white", outline="")
        self._volume_value.configure(text=f"{self._volume} %")

    def set_speed(self, label: str, rate: int) -> None:
        self._speed_label = label
        self._paint_speeds()
        if self._on_speed is not None:
            self._on_speed(rate)

    def _paint_speeds(self) -> None:
        for label, button in self._speed_buttons.items():
            button.configure(bg="#1565c0" if label == self._speed_label else "#2a2a2a")

    def _build_length(self) -> None:
        box = tk.Frame(self.menu, bg="#1e1e1e")
        box.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(
            box, text="Délka konverzace", font=("Segoe UI", 10),
            fg="#d0d0d0", bg="#1e1e1e", anchor="w",
        ).pack(fill="x", pady=(2, 4))
        row = tk.Frame(box, bg="#1e1e1e")
        row.pack(fill="x")
        self._length_down = self._arrow_chip(row, "down", lambda: self._step_length(-1))
        self._length_value = tk.Label(
            row, text="2 s", font=("Segoe UI", 11), fg="white", bg="#1e1e1e", width=5,
        )
        self._length_value.pack(side="left", padx=6)
        self._length_up = self._arrow_chip(row, "up", lambda: self._step_length(1))
        self._paint_length()

    def _arrow_chip(self, parent: tk.Frame, direction: str, command):
        chip = tk.Canvas(
            parent, width=28, height=28, bg="#2a2a2a",
            highlightthickness=0, bd=0, cursor="hand2",
        )
        chip.pack(side="left")
        if direction == "up":
            points = (6, 18, 22, 18, 14, 8)
        else:
            points = (6, 10, 22, 10, 14, 20)
        chip.arrow = chip.create_polygon(*points, fill="white", outline="")
        chip.bind("<Button-1>", lambda _event: command())
        chip.bind("<Enter>", lambda _event: chip.configure(bg="#3a3a3a"))
        chip.bind("<Leave>", lambda _event: self._paint_length())
        return chip

    def set_on_length(self, callback) -> None:
        self._on_length = callback

    def _step_length(self, delta: int) -> None:
        new = max(1, min(12, self._length + delta))
        if new == self._length:
            return
        self._length = new
        self._paint_length()
        if self._on_length is not None:
            self._on_length(new)

    def _paint_length(self) -> None:
        self._length_value.configure(text=f"{self._length} s")
        for chip, limit in ((self._length_down, 1), (self._length_up, 12)):
            active = self._length != limit
            chip.configure(bg="#2a2a2a")
            chip.itemconfigure(chip.arrow, fill="white" if active else "#666666")

    def _draw_menu_icon(self, canvas: tk.Canvas, kind: str) -> None:
        canvas.delete("all")
        canvas.kind = kind
        if kind == "gift":
            canvas.raster = None
            canvas.image_id = None
            self._draw_gift(canvas)
            return
        canvas.raster = self._menu_rasters[kind]
        canvas.image_id = None
        self._show_menu_icon(canvas, canvas["bg"])

    def _show_menu_icon(self, canvas: tk.Canvas, background: str) -> None:
        if getattr(canvas, "kind", "") == "gift":
            canvas.configure(bg=background)
            self._draw_gift(canvas)
            return
        photo = self._photo_from_raster(canvas.raster, background)
        canvas.photo = photo
        if canvas.image_id is None:
            canvas.image_id = canvas.create_image(18, 18, image=photo)
        else:
            canvas.itemconfigure(canvas.image_id, image=photo)

    def _draw_gift(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        canvas.create_rectangle(9, 16, 27, 29, outline="white", width=1.25)
        canvas.create_line(18, 16, 18, 29, fill="white", width=1.25)
        canvas.create_line(9, 21, 27, 21, fill="white", width=1.25)
        canvas.create_line(18, 16, 12, 10, fill="white", width=1.25, capstyle="round")
        canvas.create_line(18, 16, 24, 10, fill="white", width=1.25, capstyle="round")

    def open_gift(self) -> None:
        self._close_menu()
        path = PROJECT_ROOT / "docs" / "dar.pdf"
        try:
            os.startfile(path)
        except OSError:
            if not self._intro:
                self._render_caption("Soubor dar.pdf nejde otevřít.")

    def toggle_menu(self) -> None:
        if self._menu_open:
            self._close_menu()
            return
        kind = "plus" if self._hidden else "minus"
        label = "Zobrazit" if self._hidden else "Skrýt"
        icon, text, _current = self._menu_rows[1]
        self._draw_menu_icon(icon, kind)
        text.configure(text=label)
        gx, gy = self._control_origin()
        mx = gx + self.control_size - self.menu_w
        my = gy - self.menu_h - 6
        self.menu.geometry(f"{self.menu_w}x{self.menu_h}+{mx}+{my}")
        self.menu.deiconify()
        self._menu_open = True
        self._make_clickable(self.menu)
        self._make_clickable(self.gear)

    def _close_menu(self) -> None:
        self.menu.withdraw()
        self._menu_open = False

    def toggle_visibility(self) -> None:
        self._close_menu()
        if self._hidden:
            self._hidden = False
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.update_idletasks()
            self._click_through()
            self.gear.lift()
            return
        self._hidden = True
        self.root.withdraw()
        self.gear.lift()

    def set_on_read_toggle(self, callback) -> None:
        self._on_read_toggle = callback

    def set_reading(self, enabled: bool) -> None:
        self._reading = enabled
        if len(getattr(self, "_menu_rows", ())) < 3:
            return
        icon, text, _kind = self._menu_rows[2]
        color = "#1565c0" if enabled else "#1e1e1e"
        row = icon.master
        row.configure(bg=color)
        icon.configure(bg=color)
        text.configure(bg=color)
        self._show_menu_icon(icon, color)

    def toggle_reading(self) -> None:
        self.set_reading(not self._reading)
        if self._on_read_toggle is not None:
            self._on_read_toggle(self._reading)

    def quit_app(self) -> None:
        self._close_menu()
        self.root.quit()

    def close(self):
        for window in (getattr(self, "menu", None), getattr(self, "gear", None), self.root):
            if window is None:
                continue
            try:
                window.destroy()
            except tk.TclError:
                pass

    def run(self):
        """Spustí event loop (blokující)."""
        self.root.mainloop()
