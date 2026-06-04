#!/usr/bin/env python3
"""Test tkinter GUI for sandbox noVNC demo."""
import tkinter as tk
from tkinter import ttk
import time
import math

class SandboxDemo(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sandbox Demo")
        self.configure(bg="#1e1e2e")

        # Header
        header = tk.Frame(self, bg="#181825", pady=8)
        header.pack(fill="x")
        tk.Label(header, text="Sandbox Desktop Demo",
                 font=("Arial", 18, "bold"), fg="#a6e3a1", bg="#181825").pack()
        tk.Label(header, text="Running via noVNC in Docker",
                 font=("Arial", 10), fg="#6c7086", bg="#181825").pack()

        # Main content
        main = tk.Frame(self, bg="#1e1e2e", padx=16, pady=8)
        main.pack(fill="both", expand=True)

        # Left: controls
        left = tk.Frame(main, bg="#1e1e2e")
        left.pack(side="left", fill="y", padx=(0, 12))

        tk.Label(left, text="Controls", font=("Arial", 13, "bold"),
                 fg="#cdd6f4", bg="#1e1e2e").pack(anchor="w", pady=(0, 8))

        self.click_count = 0
        self.count_label = tk.Label(left, text="Clicks: 0",
                                     font=("Arial", 12), fg="#89b4fa", bg="#1e1e2e")
        self.count_label.pack(anchor="w", pady=4)

        btn_frame = tk.Frame(left, bg="#1e1e2e")
        btn_frame.pack(anchor="w", pady=4)
        tk.Button(btn_frame, text="Click Me!", command=self._on_click,
                  bg="#22c55e", fg="#1e1e2e", font=("Arial", 11, "bold"),
                  relief="flat", padx=12, pady=4).pack(side="left", padx=(0, 6))
        tk.Button(btn_frame, text="Reset", command=self._on_reset,
                  bg="#f38ba8", fg="#1e1e2e", font=("Arial", 11),
                  relief="flat", padx=12, pady=4).pack(side="left")

        # Color picker
        tk.Label(left, text="Canvas Color:", font=("Arial", 11),
                 fg="#cdd6f4", bg="#1e1e2e").pack(anchor="w", pady=(12, 4))
        colors = [("#f9e2af", "Yellow"), ("#a6e3a1", "Green"),
                  ("#89b4fa", "Blue"), ("#f38ba8", "Pink"),
                  ("#cba6f7", "Purple")]
        color_frame = tk.Frame(left, bg="#1e1e2e")
        color_frame.pack(anchor="w")
        self.ball_color = "#f9e2af"
        for color, name in colors:
            tk.Button(color_frame, bg=color, width=3, height=1, relief="flat",
                      command=lambda c=color: self._set_color(c)).pack(side="left", padx=2)

        # Slider
        tk.Label(left, text="Ball Speed:", font=("Arial", 11),
                 fg="#cdd6f4", bg="#1e1e2e").pack(anchor="w", pady=(12, 4))
        self.speed_var = tk.DoubleVar(value=2.0)
        tk.Scale(left, from_=0.5, to=8.0, resolution=0.5, orient="horizontal",
                 variable=self.speed_var, bg="#1e1e2e", fg="#cdd6f4",
                 troughcolor="#313244", highlightthickness=0, length=180).pack(anchor="w")

        # Entry
        tk.Label(left, text="Message:", font=("Arial", 11),
                 fg="#cdd6f4", bg="#1e1e2e").pack(anchor="w", pady=(12, 4))
        self.msg_var = tk.StringVar(value="Bazinga!")
        tk.Entry(left, textvariable=self.msg_var, font=("Arial", 11),
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 relief="flat", width=20).pack(anchor="w")

        # Right: canvas
        right = tk.Frame(main, bg="#1e1e2e")
        right.pack(side="right", fill="both", expand=True)

        self.canvas = tk.Canvas(right, bg="#11111b", highlightthickness=1,
                                highlightbackground="#313244")
        self.canvas.pack(fill="both", expand=True)

        # Ball animation state
        self.ball_x = 100
        self.ball_y = 100
        self.ball_dx = 2
        self.ball_dy = 1.5
        self.ball_r = 20
        self.trail = []

        # Status bar
        status = tk.Frame(self, bg="#181825", pady=3)
        status.pack(fill="x", side="bottom")
        self.status_label = tk.Label(status, text="Ready", font=("Arial", 9),
                                      fg="#6c7086", bg="#181825")
        self.status_label.pack(side="left", padx=8)
        self.time_label = tk.Label(status, text="", font=("Arial", 9),
                                    fg="#6c7086", bg="#181825")
        self.time_label.pack(side="right", padx=8)

        self._animate()

    def _on_click(self):
        self.click_count += 1
        self.count_label.config(text=f"Clicks: {self.click_count}")
        self.status_label.config(text=f"Clicked! Total: {self.click_count}", fg="#22c55e")

    def _on_reset(self):
        self.click_count = 0
        self.count_label.config(text="Clicks: 0")
        self.trail.clear()
        self.status_label.config(text="Reset!", fg="#f38ba8")

    def _set_color(self, color):
        self.ball_color = color
        self.status_label.config(text=f"Color: {color}", fg=color)

    def _animate(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w > 1 and h > 1:
            speed = self.speed_var.get()
            self.ball_x += self.ball_dx * speed
            self.ball_y += self.ball_dy * speed

            if self.ball_x - self.ball_r <= 0 or self.ball_x + self.ball_r >= w:
                self.ball_dx *= -1
            if self.ball_y - self.ball_r <= 0 or self.ball_y + self.ball_r >= h:
                self.ball_dy *= -1

            self.ball_x = max(self.ball_r, min(w - self.ball_r, self.ball_x))
            self.ball_y = max(self.ball_r, min(h - self.ball_r, self.ball_y))

            self.trail.append((self.ball_x, self.ball_y))
            if len(self.trail) > 30:
                self.trail.pop(0)

            self.canvas.delete("all")

            # Draw trail
            for i, (tx, ty) in enumerate(self.trail):
                alpha = i / len(self.trail)
                r = self.ball_r * alpha * 0.6
                self.canvas.create_oval(tx - r, ty - r, tx + r, ty + r,
                                        fill="#313244", outline="")

            # Draw ball
            r = self.ball_r
            self.canvas.create_oval(self.ball_x - r, self.ball_y - r,
                                    self.ball_x + r, self.ball_y + r,
                                    fill=self.ball_color, outline="")

            # Draw message
            msg = self.msg_var.get()
            self.canvas.create_text(w // 2, 24, text=msg,
                                    fill="#a6e3a1", font=("Arial", 14, "bold"))

        self.time_label.config(text=time.strftime("%H:%M:%S"))
        self.after(33, self._animate)


if __name__ == "__main__":
    app = SandboxDemo()
    app.mainloop()
