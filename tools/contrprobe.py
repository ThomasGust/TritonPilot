#!/usr/bin/env python3
"""
controller_probe.py - A graphical probe for pygame joystick mappings.

What you get:
- Lists connected controllers and lets you switch between them
- Live display of:
    * axes (index, value, and a bar)
    * buttons (index, pressed state)
    * hats (index, (x, y))
- "Last change" tracker so you can map physical controls to pygame indices fast

Controls:
- Left/Right: switch controller index
- Esc / Q: quit
- R: reset the "last change" message
- Space: toggle deadzone display helper (still shows raw values)
"""

from __future__ import annotations

import time
import math
import pygame


# ---------------------------- config ---------------------------------

WINDOW_W, WINDOW_H = 1100, 750
FPS = 60

FONT_NAME = "consolas"
FONT_SIZE = 18
BIG_FONT_SIZE = 26

AXIS_BAR_W = 360
AXIS_BAR_H = 16

BUTTON_SIZE = 26
BUTTON_GAP = 8
BUTTONS_PER_ROW = 16

DEADZONE = 0.10  # purely for highlighting "near zero" axes; raw still shown


# ---------------------------- helpers --------------------------------

def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)

def fmt_float(v: float) -> str:
    # Stable formatting for twitchy axis values
    return f"{v:+.3f}"

def safe_get_guid(js: pygame.joystick.Joystick) -> str:
    # pygame 2 has get_guid; older versions may not.
    return getattr(js, "get_guid", lambda: "n/a")()


# ---------------------------- probe app -------------------------------

class ControllerProbe:
    def __init__(self) -> None:
        pygame.init()
        pygame.joystick.init()

        pygame.display.set_caption("pygame Controller Probe")
        self.screen = pygame.display.set_mode((WINDOW_W, WINDOW_H))
        self.clock = pygame.time.Clock()

        self.font = pygame.font.SysFont(FONT_NAME, FONT_SIZE)
        self.big_font = pygame.font.SysFont(FONT_NAME, BIG_FONT_SIZE, bold=True)

        self.controller_index = 0
        self.js: pygame.joystick.Joystick | None = None

        self.last_change = "No input yet."
        self.last_change_t = 0.0

        # State caches (so we can detect changes even if events are noisy)
        self.prev_axes: list[float] = []
        self.prev_buttons: list[int] = []
        self.prev_hats: list[tuple[int, int]] = []

        self.show_deadzone_helper = True

        self._open_controller(self.controller_index)

    def _open_controller(self, index: int) -> None:
        count = pygame.joystick.get_count()
        if count == 0:
            self.js = None
            self.prev_axes = []
            self.prev_buttons = []
            self.prev_hats = []
            self.last_change = "No controllers detected. Plug one in."
            self.last_change_t = time.time()
            return

        index = max(0, min(index, count - 1))
        self.controller_index = index

        # Close previous (pygame doesn't require explicit close, but we can re-init cleanly)
        self.js = pygame.joystick.Joystick(index)
        self.js.init()

        self.prev_axes = [0.0] * self.js.get_numaxes()
        self.prev_buttons = [0] * self.js.get_numbuttons()
        self.prev_hats = [(0, 0)] * self.js.get_numhats()

        self.last_change = f"Opened controller #{index}: {self.js.get_name()}"
        self.last_change_t = time.time()

    def _set_last_change(self, msg: str) -> None:
        self.last_change = msg
        self.last_change_t = time.time()

    def _handle_events(self) -> bool:
        """Return False to quit."""
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return False

            if e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_ESCAPE, pygame.K_q):
                    return False
                if e.key == pygame.K_LEFT:
                    self._open_controller(self.controller_index - 1)
                if e.key == pygame.K_RIGHT:
                    self._open_controller(self.controller_index + 1)
                if e.key == pygame.K_r:
                    self._set_last_change("Reset last-change message.")
                if e.key == pygame.K_SPACE:
                    self.show_deadzone_helper = not self.show_deadzone_helper
                    self._set_last_change(
                        f"Deadzone helper: {'ON' if self.show_deadzone_helper else 'OFF'} (raw values always shown)"
                    )

            # Hotplug in pygame 2
            if hasattr(pygame, "JOYDEVICEADDED") and e.type == pygame.JOYDEVICEADDED:
                # device_index is the index in the joystick list
                self._set_last_change(f"Device added: device_index={getattr(e, 'device_index', 'n/a')}")
                # If we had no device, open first
                if self.js is None and pygame.joystick.get_count() > 0:
                    self._open_controller(0)

            if hasattr(pygame, "JOYDEVICEREMOVED") and e.type == pygame.JOYDEVICEREMOVED:
                self._set_last_change(f"Device removed: instance_id={getattr(e, 'instance_id', 'n/a')}")
                # Re-open if current disappeared (best-effort)
                if pygame.joystick.get_count() == 0:
                    self._open_controller(0)
                else:
                    self._open_controller(min(self.controller_index, pygame.joystick.get_count() - 1))

            # Input events: show immediate info for fast probing
            if e.type == pygame.JOYAXISMOTION:
                self._set_last_change(f"JOYAXISMOTION: axis={e.axis} value={fmt_float(e.value)}")
            elif e.type == pygame.JOYBUTTONDOWN:
                self._set_last_change(f"JOYBUTTONDOWN: button={e.button}")
            elif e.type == pygame.JOYBUTTONUP:
                self._set_last_change(f"JOYBUTTONUP: button={e.button}")
            elif e.type == pygame.JOYHATMOTION:
                self._set_last_change(f"JOYHATMOTION: hat={e.hat} value={e.value}")

        return True

    def _poll_and_detect_changes(self) -> None:
        """Also detect changes by polling each frame (useful if events are missed)."""
        if self.js is None:
            return

        # Axes
        for i in range(self.js.get_numaxes()):
            v = float(self.js.get_axis(i))
            if abs(v - self.prev_axes[i]) > 0.02:  # threshold avoids spam
                self._set_last_change(f"(polled) axis {i} -> {fmt_float(v)}")
                self.prev_axes[i] = v

        # Buttons
        for i in range(self.js.get_numbuttons()):
            b = int(self.js.get_button(i))
            if b != self.prev_buttons[i]:
                self._set_last_change(f"(polled) button {i} -> {b}")
                self.prev_buttons[i] = b

        # Hats
        for i in range(self.js.get_numhats()):
            h = tuple(self.js.get_hat(i))
            if h != self.prev_hats[i]:
                self._set_last_change(f"(polled) hat {i} -> {h}")
                self.prev_hats[i] = h

    def _draw_text(self, x: int, y: int, s: str, color=(230, 230, 230), big=False) -> int:
        font = self.big_font if big else self.font
        surf = font.render(s, True, color)
        self.screen.blit(surf, (x, y))
        return y + surf.get_height() + 4

    def _draw_axis_bar(self, x: int, y: int, value: float) -> None:
        # Background
        pygame.draw.rect(self.screen, (70, 70, 70), (x, y, AXIS_BAR_W, AXIS_BAR_H), border_radius=4)
        # Midline at 0
        mid = x + AXIS_BAR_W // 2
        pygame.draw.line(self.screen, (120, 120, 120), (mid, y), (mid, y + AXIS_BAR_H), 2)

        # Value fill: map [-1..1] -> bar
        v = max(-1.0, min(1.0, value))
        if v >= 0:
            w = int((AXIS_BAR_W // 2) * v)
            pygame.draw.rect(self.screen, (180, 180, 180), (mid, y, w, AXIS_BAR_H), border_radius=4)
        else:
            w = int((AXIS_BAR_W // 2) * (-v))
            pygame.draw.rect(self.screen, (180, 180, 180), (mid - w, y, w, AXIS_BAR_H), border_radius=4)

        # Deadzone hint overlay near center
        if self.show_deadzone_helper and abs(v) < DEADZONE:
            dz_w = int((AXIS_BAR_W // 2) * DEADZONE)
            pygame.draw.rect(self.screen, (110, 90, 90), (mid - dz_w, y, dz_w * 2, AXIS_BAR_H), 2, border_radius=4)

    def _draw_buttons_grid(self, x: int, y: int, pressed: list[int]) -> int:
        # Grid of small squares labeled by button index
        for idx, val in enumerate(pressed):
            row = idx // BUTTONS_PER_ROW
            col = idx % BUTTONS_PER_ROW
            bx = x + col * (BUTTON_SIZE + BUTTON_GAP)
            by = y + row * (BUTTON_SIZE + 22)

            color = (80, 180, 80) if val else (120, 120, 120)
            pygame.draw.rect(self.screen, color, (bx, by, BUTTON_SIZE, BUTTON_SIZE), border_radius=6)

            # index label
            label = self.font.render(str(idx), True, (20, 20, 20) if val else (30, 30, 30))
            self.screen.blit(label, (bx + 6, by + 3))

        rows = math.ceil(len(pressed) / BUTTONS_PER_ROW) if pressed else 1
        return y + rows * (BUTTON_SIZE + 22)

    def _draw(self) -> None:
        self.screen.fill((25, 25, 28))

        # Header
        y = 14
        y = self._draw_text(14, y, "pygame Controller Probe", big=True)

        count = pygame.joystick.get_count()
        y = self._draw_text(14, y, f"Controllers detected: {count}   (Left/Right to switch)")

        if self.js is None:
            y = self._draw_text(14, y + 10, "No controller open.", color=(220, 120, 120))
            y = self._draw_text(14, y, self.last_change, color=(200, 200, 200))
            pygame.display.flip()
            return

        name = self.js.get_name()
        guid = safe_get_guid(self.js)
        y = self._draw_text(14, y + 8, f"Active: #{self.controller_index}  name='{name}'  guid='{guid}'")

        # Last change (fade)
        age = time.time() - self.last_change_t
        fade = max(0, min(255, int(255 - age * 70)))
        y = self._draw_text(14, y + 8, f"Last change: {self.last_change}", color=(230, 230, 120))
        y = self._draw_text(14, y, f"Deadzone helper: {'ON' if self.show_deadzone_helper else 'OFF'} (Space toggles)")

        # Poll current state
        axes = [float(self.js.get_axis(i)) for i in range(self.js.get_numaxes())]
        buttons = [int(self.js.get_button(i)) for i in range(self.js.get_numbuttons())]
        hats = [tuple(self.js.get_hat(i)) for i in range(self.js.get_numhats())]

        # Layout columns
        left_x = 14
        right_x = 560

        # Axes
        y_axes = y + 12
        y_axes = self._draw_text(left_x, y_axes, f"Axes ({len(axes)})", big=True)
        y_axes += 4
        for i, v in enumerate(axes):
            line_y = y_axes + i * 26
            label = f"{i:>2}: {fmt_float(v)}"
            self._draw_text(left_x, line_y - 2, label)
            self._draw_axis_bar(left_x + 140, line_y, v)

        # Hats
        y_hats = y_axes + max(1, len(axes)) * 26 + 18
        y_hats = self._draw_text(left_x, y_hats, f"Hats / D-pad ({len(hats)})", big=True)
        y_hats += 4
        if not hats:
            y_hats = self._draw_text(left_x, y_hats, "None")
        else:
            for i, h in enumerate(hats):
                y_hats = self._draw_text(left_x, y_hats, f"{i:>2}: {h}")

        # Buttons
        y_btn = y + 12
        y_btn = self._draw_text(right_x, y_btn, f"Buttons ({len(buttons)})", big=True)
        y_btn += 10
        y_btn = self._draw_buttons_grid(right_x, y_btn, buttons)

        # Footer tips
        foot_y = WINDOW_H - 70
        self._draw_text(14, foot_y, "Tip: Press one physical control at a time and watch 'Last change' for the index/value.")
        self._draw_text(14, foot_y + 22, "If your triggers show up as axes, you’ll see them in Axes; if they’re buttons, they’ll light up in Buttons.")

        pygame.display.flip()

    def run(self) -> None:
        while True:
            if not self._handle_events():
                break
            pygame.event.pump()
            self._poll_and_detect_changes()
            self._draw()
            self.clock.tick(FPS)

        pygame.quit()


def main() -> None:
    app = ControllerProbe()
    app.run()


if __name__ == "__main__":
    main()
