import sys
import time

from app import App
from app_components import clear_background
from events.input import Buttons, BUTTON_TYPES
from system.eventbus import eventbus
from system.hexpansion.config import HexpansionConfig
from system.scheduler.events import RequestStopAppEvent
from machine import I2C

# The KeebDeck hexpansion uses a TCA8418 keypad matrix controller.
TCA8418_ADDR = 0x34

_REG_CFG = 0x01
_REG_INT_STAT = 0x02
_REG_KEY_LCK_EC = 0x03
_REG_KEY_EVENT_A = 0x04
_REG_KP_GPIO1 = 0x1D
_REG_KP_GPIO2 = 0x1E
_REG_KP_GPIO3 = 0x1F


def _add_vendor_path():
    # Make the vendored usb/ package (micropython-lib usb-device) importable
    # regardless of where the app directory ended up.
    candidates = []
    try:
        d = __file__.rsplit("/", 1)[0]
        candidates += [d, "/" + d.lstrip("/")]
    except (NameError, AttributeError, IndexError):
        pass
    candidates.append("/apps/usb_keyboard")
    for c in candidates:
        if c and c not in sys.path:
            sys.path.append(c)


_add_vendor_path()

import usb.device  # noqa: E402
from usb.device.keyboard import KeyboardInterface, KeyCode as K  # noqa: E402

# TCA8418 raw key number -> HID keycode.
# Key numbering matches the KeebDeck matrix wiring, same table as the stock
# Keebdexpansion driver by @sodoku. Badge glyph keys map to F1-F6 and the
# solder.party key maps to GUI/Super.
HID_MAP = {
    1: K.ESCAPE,
    2: K.F1,  # SQUARE
    3: K.F2,  # TRIANGLE
    4: K.F3,  # CROSS
    5: K.F4,  # CIRCLE
    6: K.F5,  # CLOUD
    7: K.F6,  # DIAMOND
    8: K.BACKSPACE,
    9: K.N0,
    10: K.MINUS,
    11: K.GRAVE,
    12: K.N1,
    13: K.N2,
    14: K.N3,
    15: K.N4,
    16: K.N5,
    17: K.N6,
    18: K.N7,
    19: K.N8,
    20: K.N9,
    21: K.TAB,
    22: K.Q,
    23: K.W,
    24: K.E,
    25: K.R,
    26: K.T,
    27: K.Y,
    28: K.U,
    29: K.I,
    30: K.O,
    32: K.A,
    33: K.S,
    34: K.D,
    35: K.F,
    36: K.G,
    37: K.H,
    38: K.J,
    39: K.K,
    40: K.L,
    41: K.LEFT_SHIFT,
    42: K.Z,
    43: K.X,
    44: K.C,
    45: K.V,
    46: K.B,
    47: K.N,
    48: K.M,
    49: K.COMMA,
    50: K.DOT,
    51: K.LEFT,
    52: K.DOWN,
    53: K.RIGHT,
    54: K.SLASH,
    55: K.UP,
    56: K.RIGHT_SHIFT,
    57: K.SEMICOLON,
    58: K.QUOTE,
    59: K.ENTER,
    60: K.EQUAL,
    61: K.LEFT_CTRL,
    62: K.LEFT_UI,  # SOLDERPARTY key -> Super/Cmd
    63: K.LEFT_ALT,
    64: K.BACKSLASH,
    65: K.SPACE,
    66: K.SPACE,
    67: K.SPACE,
    68: K.RIGHT_ALT,
    69: K.P,
    70: K.OPEN_BRACKET,
    80: K.CLOSE_BRACKET,
}

_FN_KEY = 31

# While FN is held: number row -> F-keys, arrows -> navigation cluster,
# backspace -> delete.
FN_MAP = {
    12: K.F1,
    13: K.F2,
    14: K.F3,
    15: K.F4,
    16: K.F5,
    17: K.F6,
    18: K.F7,
    19: K.F8,
    20: K.F9,
    9: K.F10,
    10: K.F11,
    60: K.F12,
    51: K.HOME,
    53: K.END,
    55: K.PAGEUP,
    52: K.PAGEDOWN,
    8: K.DELETE,
}

# Keep the USB interface as a module global: USB enumeration survives the app
# being closed and relaunched, so only ever create/attach it once per boot.
_usb_kbd = None


def _get_usb_keyboard():
    global _usb_kbd
    if _usb_kbd is None:
        kbd = KeyboardInterface()
        usb.device.get().init(kbd, builtin_driver=True)
        _usb_kbd = kbd
    return _usb_kbd


class USBKeyboardApp(App):
    def __init__(self, config=None):
        super().__init__()
        self.button_states = Buttons(self)
        self.config = config
        self.kbd = None
        self.status = "starting"
        self.last_key = ""
        self._down = {}
        self._fn = False
        self._last_scan = 0
        self._dirty = False
        self._exiting = False
        # The scheduler runs no cleanup hook when it stops an app, and the OS
        # can stop us without going through our CANCEL handler. The pin IRQ
        # outlives the app, so listen for our own stop to always release it.
        eventbus.on(RequestStopAppEvent, self._on_stop_event, self)
        if config is not None:
            self._attach(config)

    # --- hexpansion detection -------------------------------------------

    def _scan_for_keyboard(self):
        for port in range(1, 7):
            try:
                if TCA8418_ADDR in I2C(port).scan():
                    self._attach(HexpansionConfig(port))
                    return
            except (OSError, ValueError):
                pass
        self.status = "no keyboard found"

    def _attach(self, config):
        self.config = config
        try:
            i2c = config.i2c
            i2c.writeto_mem(TCA8418_ADDR, _REG_KP_GPIO1, b"\xff")  # ROW7:0 -> matrix
            i2c.writeto_mem(TCA8418_ADDR, _REG_KP_GPIO2, b"\xff")  # COL7:0 -> matrix
            i2c.writeto_mem(TCA8418_ADDR, _REG_KP_GPIO3, b"\x03")  # COL9:8 -> matrix
            i2c.writeto_mem(TCA8418_ADDR, _REG_CFG, b"\x91")  # KE_IEN | INT_CFG | AI
            # The TCA8418 keeps latching key events (and asserting INT) even
            # with no driver attached, and it isn't reset by a badge reboot.
            # Discard anything stale: while the FIFO is non-empty, INT stays
            # low and can never produce the falling edge our IRQ triggers on.
            for _ in range(16):
                if not i2c.readfrom_mem(TCA8418_ADDR, _REG_KEY_EVENT_A, 1)[0]:
                    break
            i2c.writeto_mem(TCA8418_ADDR, _REG_INT_STAT, b"\x01")  # clear K_INT
        except OSError:
            self.config = None
            self.status = "keyboard i2c error"
            return

        try:
            self.kbd = _get_usb_keyboard()
        except Exception as e:
            self.config = None
            self.status = "USB unavailable: {}".format(e)
            return

        irq_pin = config.pin[2]
        irq_pin.init(irq_pin.IN, irq_pin.PULL_UP)
        irq_pin.irq(self._handle_irq, irq_pin.IRQ_FALLING)
        self.status = "port {}".format(config.port)
        if irq_pin.value() == 0:
            # INT already asserted (event arrived between the drain above and
            # arming): there will be no falling edge, so service it now.
            self._handle_irq(irq_pin)

    def _detach(self):
        if self.config is not None:
            try:
                # Note: irq() with NO arguments is a getter and disarms
                # nothing; handler=None must be passed explicitly.
                self.config.pin[2].irq(handler=None)
            except (OSError, ValueError):
                pass
            try:
                # Stop the TCA8418 raising interrupts while no driver is
                # listening (any next driver rewrites CFG on its own attach).
                self.config.i2c.writeto_mem(TCA8418_ADDR, _REG_CFG, b"\x00")
                self.config.i2c.writeto_mem(TCA8418_ADDR, _REG_INT_STAT, b"\x01")
            except (OSError, AttributeError):
                pass
        self.config = None
        self._last_scan = time.ticks_ms()
        self._down.clear()
        self._fn = False
        # Release all keys on the host. This may be the last report we ever
        # send (e.g. on exit), so retry rather than dropping it silently.
        for _ in range(3):
            if self._send_state(100):
                break
        self.status = "keyboard removed"

    # --- key handling ----------------------------------------------------

    def _handle_irq(self, _):
        try:
            i2c = self.config.i2c
            while True:
                event = i2c.readfrom_mem(TCA8418_ADDR, _REG_KEY_EVENT_A, 1)[0]
                if event:
                    self._handle_key(event & 0x7F, bool(event & 0x80))
                    continue
                i2c.writeto_mem(TCA8418_ADDR, _REG_INT_STAT, b"\x01")  # clear K_INT
                # An event landing between the drain and the clear leaves INT
                # asserted with no new falling edge, so it would never be
                # delivered: check the FIFO again after clearing.
                if not (i2c.readfrom_mem(TCA8418_ADDR, _REG_KEY_LCK_EC, 1)[0] & 0x0F):
                    return
        except (OSError, AttributeError):
            self._detach()

    def _handle_key(self, key, pressed):
        if key == 0:
            return
        if key == _FN_KEY:
            self._fn = pressed
            return
        if pressed:
            code = (FN_MAP.get(key) if self._fn else None) or HID_MAP.get(key)
            if code is None:
                return
            self._down[key] = code
            self.last_key = _key_name(code)
        else:
            # Release by raw key number so a key always releases the code it
            # sent, even if FN changed state in between.
            if self._down.pop(key, None) is None:
                return
        self._send_state()

    def _send_state(self, timeout_ms=20):
        # Send the current key state to the host. On failure (endpoint busy,
        # USB not mounted) leave _dirty set so update()/background_update()
        # retries; a dropped release would otherwise stick a key forever.
        if self.kbd is None:
            return True
        try:
            self._dirty = not self.kbd.send_keys(self._down.values(), timeout_ms)
        except Exception:
            self._dirty = True
        return not self._dirty

    # --- app lifecycle ---------------------------------------------------

    def _on_stop_event(self, event):
        if event.app is self:
            self._exiting = True
            self._detach()

    def update(self, delta):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            # terminate() only *requests* a stop on the event bus; block any
            # further scanning first so background_update can't re-attach the
            # IRQ (which outlives the app) before the scheduler stops us.
            self._exiting = True
            self._detach()
            self.minimise()
            self.terminate()
            return True
        if self._dirty:
            self._send_state()
        return True

    def background_update(self, delta):
        if self._exiting:
            return
        if self._dirty:
            self._send_state()
        if self.config is None:
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_scan) > 2000:
                self._last_scan = now
                self._scan_for_keyboard()

    def draw(self, ctx):
        clear_background(ctx)
        ctx.save()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        ctx.font_size = 24
        ctx.rgb(1, 1, 1).move_to(0, -70).text("USB Keyboard")

        ctx.font_size = 16
        if self.config is not None:
            ctx.rgb(0.3, 1, 0.3).move_to(0, -30).text("hexpansion: " + self.status)
            mounted = False
            try:
                mounted = self.kbd is not None and self.kbd.is_open()
            except Exception:
                pass
            if mounted:
                ctx.rgb(0.3, 1, 0.3).move_to(0, 0).text("USB: connected")
            else:
                ctx.rgb(1, 0.7, 0.2).move_to(0, 0).text("USB: waiting for host")
            ctx.rgb(0.7, 0.7, 0.7).move_to(0, 30).text("last key: " + self.last_key)
        else:
            ctx.rgb(1, 0.7, 0.2).move_to(0, -30).text(self.status)
            ctx.rgb(0.7, 0.7, 0.7).move_to(0, 0).text("insert keyboard hexpansion")

        ctx.font_size = 12
        ctx.rgb(0.5, 0.5, 0.5).move_to(0, 70).text("CANCEL: exit")
        ctx.restore()
        self.draw_overlays(ctx)


def _key_name(code):
    if code < 0:
        for name in ("LEFT_CTRL", "LEFT_SHIFT", "LEFT_ALT", "LEFT_UI",
                     "RIGHT_CTRL", "RIGHT_SHIFT", "RIGHT_ALT", "RIGHT_UI"):
            if getattr(K, name) == code:
                return name.replace("_", " ").lower()
        return "mod"
    for name in dir(K):
        if not name.startswith("_") and getattr(K, name) == code:
            return name.lower()
    return str(code)


__app_export__ = USBKeyboardApp
