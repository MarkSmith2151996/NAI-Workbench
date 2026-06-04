import tkinter as tk
import math
import random

root = tk.Tk()
root.title("Graph Demo")
root.configure(bg="#1e1e2e")

W, H = 600, 400
MARGIN = 50

c = tk.Canvas(root, width=W, height=H, bg="#1e1e2e", highlightthickness=0)
c.pack(fill="both", expand=True)

# Generate data
data1 = [math.sin(i * 0.3) * 40 + 50 + random.uniform(-5, 5) for i in range(20)]
data2 = [math.cos(i * 0.25) * 30 + 60 + random.uniform(-5, 5) for i in range(20)]

min_val = min(min(data1), min(data2)) - 10
max_val = max(max(data1), max(data2)) + 10

def to_screen(i, val):
    x = MARGIN + i * (W - 2 * MARGIN) / (len(data1) - 1)
    y = H - MARGIN - (val - min_val) / (max_val - min_val) * (H - 2 * MARGIN)
    return x, y

# Grid lines
for i in range(5):
    val = min_val + i * (max_val - min_val) / 4
    _, y = to_screen(0, val)
    c.create_line(MARGIN, y, W - MARGIN, y, fill="#313244", dash=(2, 4))
    c.create_text(MARGIN - 8, y, text=f"{val:.0f}", fill="#6c7086", font=("Arial", 9), anchor="e")

# X axis labels
for i in range(0, len(data1), 3):
    x, _ = to_screen(i, 0)
    c.create_text(x, H - MARGIN + 15, text=str(i), fill="#6c7086", font=("Arial", 9))

# Axes
c.create_line(MARGIN, MARGIN, MARGIN, H - MARGIN, fill="#585b70", width=2)
c.create_line(MARGIN, H - MARGIN, W - MARGIN, H - MARGIN, fill="#585b70", width=2)

# Plot line 1 (blue)
pts1 = [coord for i, v in enumerate(data1) for coord in to_screen(i, v)]
c.create_line(*pts1, fill="#89b4fa", width=3, smooth=True)
for i, v in enumerate(data1):
    x, y = to_screen(i, v)
    c.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#89b4fa", outline="")

# Plot line 2 (green)
pts2 = [coord for i, v in enumerate(data2) for coord in to_screen(i, v)]
c.create_line(*pts2, fill="#a6e3a1", width=3, smooth=True)
for i, v in enumerate(data2):
    x, y = to_screen(i, v)
    c.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#a6e3a1", outline="")

# Legend
c.create_rectangle(W - 150, 15, W - 20, 55, fill="#181825", outline="#313244")
c.create_line(W - 140, 28, W - 115, 28, fill="#89b4fa", width=3)
c.create_text(W - 110, 28, text="Revenue", fill="#cdd6f4", font=("Arial", 10), anchor="w")
c.create_line(W - 140, 45, W - 115, 45, fill="#a6e3a1", width=3)
c.create_text(W - 110, 45, text="Growth", fill="#cdd6f4", font=("Arial", 10), anchor="w")

# Title
c.create_text(W // 2, 20, text="Quarterly Metrics", fill="#cdd6f4", font=("Arial", 14, "bold"))

root.mainloop()
