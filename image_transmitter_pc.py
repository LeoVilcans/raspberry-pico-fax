import serial
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import os
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
PORT    = '/dev/ttyACM0'
BAUD    = 115200
CHUNK   = 64   # Must not exceed the Pico's USB CDC stdin buffer (~256 bytes).
                # 128 is a safe conservative value. Don't increase above 200.
RESULTS = 'results'
# ─────────────────────────────────────────────────────────────────────────────

CHANNELS       = ('R', 'G', 'B')
CHANNEL_COLORS = {'R': '#ff4444', 'G': '#00ff88', 'B': '#4488ff'}


def serial_read_exactly(ser, n):
    buf = b""
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class FaxApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Image Transmitter")
        self.resizable(False, False)
        self.configure(bg="#0d0d0d")

        self._image_path   = None
        self._img_w        = 0
        self._img_h        = 0
        self._running      = False
        self._thread       = None
        self._start_time   = None
        self._channel_pixels = {'R': [], 'G': [], 'B': []}
        self._current_ch   = 'R'

        # Canvas display size — scaled up 2× for visibility
        self._display_w = 1
        self._display_h = 1

        self._build_ui()
        os.makedirs(RESULTS, exist_ok=True)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        PAD = 12

        title_frame = tk.Frame(self, bg="#0d0d0d")
        title_frame.pack(fill="x", padx=PAD, pady=(PAD, 0))
        tk.Label(
            title_frame, text="FAX 3000",
            font=("Courier", 14, "bold"), fg="#00ff88", bg="#0d0d0d"
        ).pack(side="left")
        self._status_dot = tk.Label(
            title_frame, text="●", font=("Courier", 14),
            fg="#333333", bg="#0d0d0d"
        )
        self._status_dot.pack(side="right")

        # Canvas — starts small, resizes when image is loaded
        canvas_frame = tk.Frame(self, bg="#1a1a1a", bd=0,
                                highlightthickness=1, highlightbackground="#333333")
        canvas_frame.pack(padx=PAD, pady=(8, 0))
        self._canvas = tk.Canvas(
            canvas_frame, width=512, height=338,
            bg="#111111", cursor="crosshair", bd=0, highlightthickness=0
        )
        self._canvas.pack()
        self._canvas_text = self._canvas.create_text(
            256, 169,
            text="no image loaded", font=("Courier", 11), fill="#444444"
        )
        self._photo = None

        # Channel progress bars
        bars_frame = tk.Frame(self, bg="#0d0d0d")
        bars_frame.pack(fill="x", padx=PAD, pady=(6, 0))
        self._prog_bars     = {}
        self._prog_canvases = {}
        for ch in CHANNELS:
            row = tk.Frame(bars_frame, bg="#0d0d0d")
            row.pack(fill="x", pady=1)
            tk.Label(row, text=ch, font=("Courier", 8, "bold"),
                     fg=CHANNEL_COLORS[ch], bg="#0d0d0d", width=2).pack(side="left")
            c = tk.Canvas(row, height=5, bg="#1a1a1a", bd=0, highlightthickness=0)
            c.pack(side="left", fill="x", expand=True)
            bar = c.create_rectangle(0, 0, 0, 5,
                                     fill=CHANNEL_COLORS[ch], outline="")
            self._prog_canvases[ch] = c
            self._prog_bars[ch]     = bar

        stats_frame = tk.Frame(self, bg="#0d0d0d")
        stats_frame.pack(fill="x", padx=PAD, pady=(4, 0))
        self._lbl_pixels = tk.Label(
            stats_frame, text="channel: -- | pixels: --",
            font=("Courier", 9), fg="#556655", bg="#0d0d0d", anchor="w"
        )
        self._lbl_pixels.pack(side="left")
        self._lbl_time = tk.Label(
            stats_frame, text="time: --",
            font=("Courier", 9), fg="#556655", bg="#0d0d0d", anchor="e"
        )
        self._lbl_time.pack(side="right")

        ctrl_frame = tk.Frame(self, bg="#0d0d0d")
        ctrl_frame.pack(padx=PAD, pady=PAD)
        btn_cfg = dict(font=("Courier", 10, "bold"), relief="flat",
                       cursor="hand2", padx=14, pady=6, bd=0)

        self._btn_load = tk.Button(
            ctrl_frame, text="[ LOAD ]",
            bg="#1a1a1a", fg="#aaaaaa",
            activebackground="#2a2a2a", activeforeground="#ffffff",
            command=self._load_image, **btn_cfg)
        self._btn_load.grid(row=0, column=0, padx=4)

        self._btn_start = tk.Button(
            ctrl_frame, text="[ START ]",
            bg="#003322", fg="#00ff88",
            activebackground="#004433", activeforeground="#00ff88",
            command=self._start, state="disabled", **btn_cfg)
        self._btn_start.grid(row=0, column=1, padx=4)

        self._btn_stop = tk.Button(
            ctrl_frame, text="[ STOP ]",
            bg="#220000", fg="#ff4444",
            activebackground="#330000", activeforeground="#ff4444",
            command=self._stop, state="disabled", **btn_cfg)
        self._btn_stop.grid(row=0, column=2, padx=4)

        self._btn_restart = tk.Button(
            ctrl_frame, text="[ RESTART ]",
            bg="#1a1a00", fg="#ffcc00",
            activebackground="#2a2a00", activeforeground="#ffcc00",
            command=self._restart, state="disabled", **btn_cfg)
        self._btn_restart.grid(row=0, column=3, padx=4)

        port_frame = tk.Frame(self, bg="#0d0d0d")
        port_frame.pack(pady=(0, PAD))
        tk.Label(port_frame, text="port:", font=("Courier", 9),
                 fg="#444444", bg="#0d0d0d").pack(side="left")
        self._port_var = tk.StringVar(value=PORT)
        tk.Entry(port_frame, textvariable=self._port_var,
                 font=("Courier", 9), bg="#1a1a1a", fg="#888888",
                 insertbackground="#00ff88", relief="flat", width=18, bd=2
                 ).pack(side="left", padx=(4, 0))

    # ── Actions ───────────────────────────────────────────────────────────────
    def _load_image(self):
        path = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.gif *.tiff"),
                       ("All", "*.*")]
        )
        if not path:
            return
        self._image_path = path

        # Determine dimensions from the actual image
        with Image.open(path) as probe:
            self._img_w, self._img_h = probe.size

        total_pixels = self._img_w * self._img_h

        # Scale for display — cap at 800px wide so it fits on screen
        scale = min(2, 800 // self._img_w)
        scale = max(scale, 1)
        self._display_w = self._img_w * scale
        self._display_h = self._img_h * scale

        # Resize canvas to match image
        self._canvas.config(width=self._display_w, height=self._display_h)
        self._canvas.coords(
            self._canvas_text, self._display_w // 2, self._display_h // 2)

        preview = Image.open(path).convert('RGB').resize(
            (self._display_w, self._display_h), Image.LANCZOS)
        self._show_image(preview)
        self._canvas.itemconfig(self._canvas_text, text="")

        self._lbl_pixels.config(
            text=f"image: {self._img_w}×{self._img_h} ({total_pixels} pixels/channel)"
        )
        self._btn_start.config(state="normal")
        self._btn_restart.config(state="disabled")
        self._set_status("idle")

    def _start(self):
        if not self._image_path:
            return
        self._channel_pixels = {'R': [], 'G': [], 'B': []}
        self._current_ch     = 'R'
        self._running        = True
        self._start_time     = time.time()
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._btn_restart.config(state="disabled")
        self._btn_load.config(state="disabled")
        self._set_status("transmitting", 'R')
        self._thread = threading.Thread(target=self._transmit, daemon=True)
        self._thread.start()
        self._poll_ui()

    def _stop(self):
        self._running = False
        self._set_status("stopped")
        self._btn_stop.config(state="disabled")
        self._btn_start.config(state="normal")
        self._btn_restart.config(state="normal")
        self._btn_load.config(state="normal")

    def _restart(self):
        self._stop()
        self.after(300, self._start)

    # ── Serial thread ─────────────────────────────────────────────────────────
    def _transmit(self):
        try:
            ser = serial.Serial(self._port_var.get(), BAUD, timeout=5)
            # 1. Give the Pico time to wake up
            time.sleep(1)
            # 2. Clear out any "garbage" bytes from previous crashes
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except serial.SerialException as e:
                self.after(0, lambda: messagebox.showerror("Serial Error", str(e)))
                self.after(0, self._stop)
                return

        total_pixels = self._img_w * self._img_h
        img   = Image.open(self._image_path).convert('RGB').resize(
            (self._img_w, self._img_h))
        planes = {
            'R': list(img.getchannel('R').getdata()),
            'G': list(img.getchannel('G').getdata()),
            'B': list(img.getchannel('B').getdata()),
        }

        ser.reset_input_buffer()
        ser.reset_output_buffer()
        # Small delay to let the Pico stabilize
        time.sleep(0.1)

        for ch in CHANNELS:
            if not self._running:
                break
            
            time.sleep(0.05)
            
            self.after(0, lambda c=ch: self._set_status("transmitting", c))
            raw_pixels = planes[ch]
            i = 0

            while self._running and i < total_pixels:
                chunk  = raw_pixels[i: i + CHUNK]
                actual = len(chunk)
                try:
                    ser.write(bytearray(chunk))
                    received = serial_read_exactly(ser, actual)
                except serial.SerialException as e:
                    self.after(0, lambda: messagebox.showerror("Serial Error", str(e)))
                    self._running = False
                    break

                if received is None:
                    self.after(0, lambda: messagebox.showwarning(
                        "Timeout",
                        "Pico stopped responding.\n"
                        "Check the connection and try Restart."
                    ))
                    self._running = False
                    break

                self._channel_pixels[ch].extend(list(received))
                i += actual
                time.sleep(0.01)

        ser.close()
        if self._running:
            self.after(0, self._on_complete)

    # ── Preview ───────────────────────────────────────────────────────────────
    def _build_preview(self):
        total_pixels = self._img_w * self._img_h
        cp = self._channel_pixels

        def get_plane(ch):
            data = cp[ch]
            if len(data) >= total_pixels:
                return data[:total_pixels]
            return data + [0] * (total_pixels - len(data))

        r, g, b = get_plane('R'), get_plane('G'), get_plane('B')
        flat = bytearray(total_pixels * 3)
        for idx in range(total_pixels):
            flat[idx * 3]     = r[idx]
            flat[idx * 3 + 1] = g[idx]
            flat[idx * 3 + 2] = b[idx]

        img = Image.frombytes('RGB', (self._img_w, self._img_h), bytes(flat))
        return img.resize((self._display_w, self._display_h), Image.NEAREST)

    # ── UI refresh ────────────────────────────────────────────────────────────
    def _poll_ui(self):
        total_pixels = self._img_w * self._img_h
        elapsed = time.time() - self._start_time if self._start_time else 0
        self._lbl_time.config(text=f"time: {elapsed:.1f}s")

        for ch in CHANNELS:
            n = len(self._channel_pixels[ch])
            self._prog_canvases[ch].update_idletasks()
            w = self._prog_canvases[ch].winfo_width()
            bar_w = int(w * min(n, total_pixels) / max(total_pixels, 1))
            self._prog_canvases[ch].coords(self._prog_bars[ch], 0, 0, bar_w, 5)

        n_cur = len(self._channel_pixels[self._current_ch])
        self._lbl_pixels.config(
            text=f"channel: {self._current_ch} | pixels: {n_cur} / {total_pixels}"
        )

        if any(len(self._channel_pixels[ch]) > 0 for ch in CHANNELS):
            preview = self._build_preview()
            if preview:
                self._show_image(preview)

        if self._running:
            self.after(120, self._poll_ui)

    def _on_complete(self):
        self._running = False
        total_pixels  = self._img_w * self._img_h
        elapsed       = time.time() - self._start_time
        self._set_status("done")
        self._lbl_time.config(text=f"time: {elapsed:.2f}s")
        self._btn_stop.config(state="disabled")
        self._btn_restart.config(state="normal")
        self._btn_start.config(state="normal")
        self._btn_load.config(state="normal")

        cp = self._channel_pixels
        if all(len(cp[ch]) >= total_pixels for ch in CHANNELS):
            r = cp['R'][:total_pixels]
            g = cp['G'][:total_pixels]
            b = cp['B'][:total_pixels]
            flat = bytearray(total_pixels * 3)
            for idx in range(total_pixels):
                flat[idx * 3]     = r[idx]
                flat[idx * 3 + 1] = g[idx]
                flat[idx * 3 + 2] = b[idx]
            final = Image.frombytes('RGB', (self._img_w, self._img_h), bytes(flat))

            # Show final high-quality render
            self._show_image(
                final.resize((self._display_w, self._display_h), Image.LANCZOS))

            # Save to results/
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path  = os.path.join(RESULTS, f"fax_{timestamp}.png")
            final.save(out_path)
            self._lbl_pixels.config(
                text=f"saved → {out_path}"
            )

    def _show_image(self, img):
        self._photo = ImageTk.PhotoImage(img)
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _set_status(self, state, channel=None):
        colors = {
            "idle":         "#555555",
            "transmitting": CHANNEL_COLORS.get(channel, "#00ff88"),
            "stopped":      "#ff4444",
            "done":         "#4488ff",
        }
        self._status_dot.config(fg=colors.get(state, "#333333"))
        ch_str = f" [{channel}]" if channel else ""
        self.title(f"Image Transmitter — {state.upper()}{ch_str}")
        if channel:
            self._current_ch = channel


if __name__ == "__main__":
    app = FaxApp()
    app.mainloop()
