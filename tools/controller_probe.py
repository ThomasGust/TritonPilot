import pygame
import tkinter as tk
from tkinter import ttk

UPDATE_MS = 100  # GUI refresh rate (ms)

# --- Pygame setup -------------------------------------------------
pygame.init()
pygame.joystick.init()

if pygame.joystick.get_count() == 0:
    raise RuntimeError("No controller found")

js = pygame.joystick.Joystick(0)
js.init()

NUM_AXES = js.get_numaxes()
NUM_BUTTONS = js.get_numbuttons()
NUM_HATS = js.get_numhats()


# --- Tkinter GUI --------------------------------------------------
root = tk.Tk()
root.title(f"Gamepad Inspector - {js.get_name()}")

# use a nice font size
DEFAULT_FONT = ("Segoe UI", 10)

title_lbl = tk.Label(root, text=f"Controller: {js.get_name()}", font=("Segoe UI", 11, "bold"))
title_lbl.pack(pady=(8, 4))

container = tk.Frame(root)
container.pack(padx=10, pady=10)

# ---- Axes frame ----
axes_frame = tk.LabelFrame(container, text="Axes", font=DEFAULT_FONT)
axes_frame.grid(row=0, column=0, sticky="nw", padx=(0, 10))

axis_bars = []
axis_labels = []

for i in range(NUM_AXES):
    row = i
    lbl = tk.Label(axes_frame, text=f"Axis {i}", width=10, anchor="w", font=DEFAULT_FONT)
    lbl.grid(row=row, column=0, sticky="w", pady=2)

    # scale from -1..1 -> 0..200
    bar = ttk.Progressbar(axes_frame, orient="horizontal", length=160, mode="determinate", maximum=200)
    bar.grid(row=row, column=1, sticky="w", pady=2, padx=(5, 5))

    val_lbl = tk.Label(axes_frame, text="0.000", width=7, anchor="e", font=DEFAULT_FONT)
    val_lbl.grid(row=row, column=2, sticky="e")

    axis_bars.append(bar)
    axis_labels.append(val_lbl)

# ---- Buttons frame ----
buttons_frame = tk.LabelFrame(container, text="Buttons", font=DEFAULT_FONT)
buttons_frame.grid(row=0, column=1, sticky="nw")

button_labels = []
for i in range(NUM_BUTTONS):
    lbl = tk.Label(buttons_frame, text=f"{i:2d}: .", width=8, anchor="w", font=DEFAULT_FONT)
    lbl.grid(row=i // 4, column=i % 4, sticky="w", padx=4, pady=2)
    button_labels.append(lbl)

# ---- Hats frame ----
hats_frame = tk.LabelFrame(container, text="Hats (D-pad)", font=DEFAULT_FONT)
hats_frame.grid(row=1, column=0, sticky="nw", pady=(10, 0))

hat_labels = []
for i in range(NUM_HATS):
    lbl = tk.Label(hats_frame, text=f"Hat {i}: (0, 0)", anchor="w", font=DEFAULT_FONT)
    lbl.grid(row=i, column=0, sticky="w", pady=2)
    hat_labels.append(lbl)

status_lbl = tk.Label(root, text="Move/press anything…", font=DEFAULT_FONT)
status_lbl.pack(pady=(5, 10))


# --- Update loop --------------------------------------------------
def update_inputs():
    # read current joystick state
    pygame.event.pump()

    # axes
    for i in range(NUM_AXES):
        v = js.get_axis(i)  # -1 .. 1
        # progressbar wants positive: map -1..1 -> 0..200
        pct = int((v + 1) * 100)
        axis_bars[i]["value"] = pct
        axis_labels[i]["text"] = f"{v: .3f}"

    # buttons
    for i in range(NUM_BUTTONS):
        v = js.get_button(i)
        button_labels[i]["text"] = f"{i:2d}: {'X' if v else '.'}"

    # hats
    for i in range(NUM_HATS):
        v = js.get_hat(i)
        hat_labels[i]["text"] = f"Hat {i}: {v}"

    root.after(UPDATE_MS, update_inputs)


# start loop
update_inputs()
root.mainloop()
