# Tildagon USB Keyboard

Use the official [Keebdexpansion](https://github.com/emfcamp/Keebdexpansion)
(solder.party KeebDeck) keyboard hexpansion as a real **USB HID keyboard** for
your computer: plug the Tildagon into a laptop over USB-C, open this app, and
type.

## How it works

- The keyboard hexpansion carries a TCA8418 keypad-matrix controller on the
  hexpansion I2C bus (address `0x34`) with a key-event FIFO and an interrupt
  line on the hexpansion's third high-speed pin. This app configures the
  matrix and reads key events on interrupt, the same way the stock
  [Keebdexpansion driver](https://github.com/sodoku/tildagon-keebdexpansion)
  does.
- Tildagon OS is built on MicroPython 1.28 with `machine.USBDevice` support
  (runtime USB devices) enabled on the badge's ESP32-S3 native USB port. The
  app attaches a standard HID keyboard interface alongside the built-in
  CDC serial (REPL) interface using the vendored
  [micropython-lib `usb-device` packages](https://github.com/micropython/micropython-lib/tree/master/micropython/usb)
  (MIT, © Angus Gratton).
- Raw matrix key numbers are mapped to USB HID keycodes and sent as 6-key
  rollover reports with proper modifier handling (both shifts, ctrl, both
  alts) — so shortcuts like Ctrl+C and Alt+Tab work as on a normal keyboard.

## Install

App store: search for "USB Keyboard" (once published).

Manual install with [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html):

```sh
mpremote mkdir :/apps/usb_keyboard || true
mpremote cp app.py tildagon.toml metadata.json :/apps/usb_keyboard/
mpremote mkdir :/apps/usb_keyboard/usb :/apps/usb_keyboard/usb/device || true
mpremote cp usb/device/*.py :/apps/usb_keyboard/usb/device/
mpremote reset
```

## Use

1. Plug the keyboard hexpansion into any port.
2. Connect the badge to the computer over USB-C.
3. Launch **Apps → USB Keyboard**. The first launch re-enumerates USB (a
   serial console connected to the badge will briefly drop and reconnect);
   the host then sees a keyboard device named "TiLDAGON".
4. Type. Keys are only sent to USB while the app is open; press **CANCEL**
   on the badge to exit and stop the keyboard bridge.

### Key mapping

- The six badge-glyph keys (square, triangle, cross, circle, cloud, diamond)
  send **F1–F6**.
- The solder.party key (next to CTRL) sends **Super/GUI** (Windows/Cmd).
- **FN + number row** sends F1–F10 (FN+`-` = F11, FN+`=` = F12), **FN +
  arrows** sends Home/End/PgUp/PgDn, **FN + backspace** sends Delete.

## Notes

- While this app is running it takes over the keyboard's interrupt line, so
  keys go to USB instead of to badge text inputs. Exiting the app releases
  the interrupt line and stops sending keys; to get the stock badge keyboard
  driver working again afterwards, unplug and replug the hexpansion so the
  badge relaunches its driver.
- The HID interface stays enumerated until the badge reboots, even after the
  app is closed (no keys are sent while the app is not running).

## License

MIT. Vendored `usb/device/*.py` files are from micropython-lib,
MIT license, © 2023–2024 Angus Gratton.
