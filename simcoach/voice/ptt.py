"""Push-to-talk: a steering wheel button (any DirectInput device) or a key.

Wheel buttons are read directly with pygame/SDL - no SimHub/JoyToKey mapping
needed. If you'd rather map the button elsewhere (SimHub "Controls and
events", JoyToKey, your wheel software), map it to a key such as F13 and use
``ptt: keyboard``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class PushToTalk:
    """Calls ``on_start`` when the driver starts talking and ``on_stop`` when done."""

    def __init__(self, on_start: Callable[[], None], on_stop: Callable[[], None], mode: str = "hold"):
        self.on_start, self.on_stop = on_start, on_stop
        self.mode = mode
        self.active = False
        self._prev = False
        self._thread = threading.Thread(target=self._loop, name="ptt", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _pressed(self) -> bool:
        raise NotImplementedError

    def _setup(self) -> None:
        pass

    def _loop(self) -> None:
        self._setup()
        while True:
            try:
                now = self._pressed()
            except Exception:
                log.exception("push-to-talk read failed")
                time.sleep(1.0)
                continue
            if self.mode == "toggle":
                if now and not self._prev:
                    self._set(not self.active)
            elif now != self.active:
                self._set(now)
            self._prev = now
            time.sleep(0.01)

    def _set(self, active: bool) -> None:
        self.active = active
        (self.on_start if active else self.on_stop)()


class KeyboardPTT(PushToTalk):
    def __init__(self, key: str, **kw):
        import keyboard  # global hotkeys; works while iRacing has focus

        self._kb = keyboard
        self.key = key
        super().__init__(**kw)

    def _pressed(self) -> bool:
        return bool(self._kb.is_pressed(self.key))


class JoystickPTT(PushToTalk):
    def __init__(self, name_contains: str, button: int, **kw):
        # Read the wheel even when iRacing (not us) has window focus.
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        self.name_contains = name_contains.lower()
        self.button = button
        self.joy = None
        super().__init__(**kw)

    def _setup(self) -> None:
        import pygame

        self._pg = pygame
        pygame.init()
        pygame.joystick.init()
        self._open()

    def _open(self) -> None:
        pg = self._pg
        for i in range(pg.joystick.get_count()):
            j = pg.joystick.Joystick(i)
            if not self.name_contains or self.name_contains in j.get_name().lower():
                if self.button < j.get_numbuttons():
                    self.joy = j
                    log.info("Push-to-talk: %s button %d", j.get_name(), self.button)
                    return
        log.warning("No joystick matching %r with button %d found", self.name_contains, self.button)

    def _pressed(self) -> bool:
        self._pg.event.pump()
        if self.joy is None:
            time.sleep(2.0)
            self._pg.joystick.quit()
            self._pg.joystick.init()
            self._open()
            return False
        return bool(self.joy.get_button(self.button))


def list_devices() -> list[tuple[str, int]]:
    """Names and button counts of connected game controllers."""
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    pygame.init()
    pygame.joystick.init()
    out = []
    for i in range(pygame.joystick.get_count()):
        j = pygame.joystick.Joystick(i)
        out.append((j.get_name(), j.get_numbuttons()))
    return out


def watch_buttons(seconds: float = 20.0) -> None:
    """Print button presses so the user can find their PTT button index."""
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    pygame.init()
    pygame.joystick.init()
    joys = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    print(f"Press buttons on your wheel for {seconds:.0f}s...")
    end = time.time() + seconds
    while time.time() < end:
        for ev in pygame.event.get():
            if ev.type == pygame.JOYBUTTONDOWN:
                j = next((x for x in joys if x.get_instance_id() == ev.instance_id), None)
                print(f"  {j.get_name() if j else ev.instance_id}: button {ev.button}")
        time.sleep(0.01)
