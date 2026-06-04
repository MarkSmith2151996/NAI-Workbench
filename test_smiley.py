import tkinter as tk
import math

root = tk.Tk()
root.title("Smiley")
root.configure(bg="#1e1e2e")

c = tk.Canvas(root, width=500, height=500, bg="#1e1e2e", highlightthickness=0)
c.pack(fill="both", expand=True)

# Face
c.create_oval(50, 50, 450, 450, fill="#f9e2af", outline="#f5c211", width=4)

# Left eye
c.create_oval(150, 150, 210, 230, fill="white", outline="#1e1e2e", width=2)
c.create_oval(165, 170, 195, 210, fill="#1e1e2e")

# Right eye
c.create_oval(290, 150, 350, 230, fill="white", outline="#1e1e2e", width=2)
c.create_oval(305, 170, 335, 210, fill="#1e1e2e")

# Smile
points = []
for i in range(0, 181):
    angle = math.radians(i)
    x = 250 + 120 * math.cos(angle)
    y = 280 + 100 * math.sin(angle)
    points.extend([x, y])
c.create_line(*points, fill="#1e1e2e", width=6, smooth=True)

root.mainloop()
