# love4wallpaper

**FULLY open sourced, local and pretty much ZERO idle cpu**

love4wallpaper is a simple project i built in python focused on windows 11.

single-file and easy to set up, but not compiled into an `.exe` (so you will need windows).

**only requirement:** `pywin32`

if you’ve ever wanted to see your wallpaper but have too many windows, love4wallpaper is built to hide all windows that aren’t currently focused, with some techniques (like ignoring fullscreen windows) to avoid this behaviour with games.

its basically unbranded atm. no icons or tagline or anything because this project is something i built for myself, but i’m happy to share.

built with default windows ui on tkinter.

## function

### foreground monitoring

feeds a 120ms debounce timer, so alt+tab overlays and taskbar flicker dont trigger anything. extremely lightweight.

### ignored windows

start, alt+tab, taskbar and desktop never count as the app you switched to, so clicking them doesnt minimize anything.

### owned windows

owned windows of the same app dont minimize their parents.

### hung applications

hung apps cant freeze it and focus doesnt move (uses `ShowWindowAsync`).

### fullscreen detection

a window counts as fullscreen ONLY if it covers its monitor and either has no caption (borderless or etc) or matches the monitor rect exactly (f11 fullscreen).

normal maximized windows overshoot the monitor by their resize border and have a caption, so they still get minimized.

its checked per-monitor (so multi-monitor support), and the process is dpi-aware!!

## ipc info

a localhost socket on an ephemeral port, with fully random tokens stored in `%localappdata%/Love4wallpaper/runtime.json`.

the window uses it to ping, push settings, stop the process, and everything it needs communication-wise.

## readme

- windows completely BLOCKS a normal process from minimizing admin windows (like elevated processes). add them to the exclusion list OR run the script elevated.
- windows with no minimize button are not touched INTENTIONALLY. this behaviour is easy to remove if you wish.
- there is ONE ipc thread in the background process, because a blocking `accept()` is cheaper than polling.
- all errors go to `love4wallpaper.log`.
