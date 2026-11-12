# love4wallpaper, made because I like my wallpaper and hate windows hiding it
# ARGS WHEN OPENING WITH COMMAND LINE:


# no args / --show | start background if needed, then open the settings ui
# --ui | settings ui only
# --background | tray + foreground monitor (what the run key launches)
# --debug | verbose log, use with --background

import argparse
import ctypes
import hmac
import json
import logging
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import winreg # required!!
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.platform != "win32":
    sys.exit("Love4wallpaper only runs on windows atm")

try:
    import win32api
    import win32con
    import win32event
    import win32gui
    import win32process
    import winerror
except ImportError:
    ctypes.windll.user32.MessageBoxW(0, "pywin32 is missing.\n\nRun: pip install pywin32", "Love4wallpaper", 0x10)
    sys.exit(1)

APP = "Love4wallpaper"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP
CONFIG_PATH = DATA_DIR / "config.json"
RUNTIME_PATH = DATA_DIR / "runtime.json"  # port + token of the running bg process
LOG_PATH = DATA_DIR / "love4wallpaper.log"
SCRIPT = Path(__file__).resolve()
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_BG = f"Local\\{APP}.Background"
MUTEX_UI = f"Local\\{APP}.UI"

DEFAULTS = {
    "enabled": True,
    "ignore_fullscreen": True,
    "ignore_minimized": True,
    "ignore_self": True,
    "exclusions": [],
}

# stuff that grabs foreground for a sec (alt+tab, start, taskbar). never counts as "the app you switched to" or antyhing
SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "XamlExplorerHostIslandWindow", "MultitaskingViewFrame", "ForegroundStaging",
    "TaskListThumbnailWnd", "TopLevelWindowForOverflowXamlIsland",
    "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow", "TaskSwitcherWnd",
    "TaskSwitcherOverlayWnd", "Shell_InputSwitchTopLevelWindow", "#32768",
}
SYSTEM_EXES = {
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "lockapp.exe", "logonui.exe",
    "dwm.exe", "sihost.exe",
}

SETTLE_MS = 120  # wait for foreground to stop flapping before acting
TIMER_SETTLE = 1
WM_TRAY = win32con.WM_USER + 20
ID_OPEN, ID_TOGGLE, ID_EXIT = 1001, 1002, 1003
GA_ROOTOWNER = 3
DWMWA_CLOAKED = 14
SW_SHOWMINNOACTIVE = 7
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)
# no argtypes = 64-bit handles get chopped to c_int, so declare everything we call
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                   WinEventProc, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

log = logging.getLogger(APP)


def guarded(fn):
    # exceptions in hook/wndproc callbacks vanish under pythonw, so log them ourselves
    def wrapper(*args):
        try:
            return fn(*args) or 0
        except Exception:
            log.exception("handler %s blew up", getattr(fn, "__name__", "?"))
            return 0
    return wrapper


# ------------------------------------------------------------- config / ipc

def normalize_exe(value):
    name = Path(value.strip().strip('"')).name.lower()
    if not name:
        return ""
    return name if name.endswith(".exe") else name + ".exe"


def _atomic_write(path, text):
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_config():
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        for key, default in DEFAULTS.items():
            if isinstance(data.get(key), type(default)):
                cfg[key] = data[key]
        names = {normalize_exe(e) for e in cfg["exclusions"] if isinstance(e, str)}
        cfg["exclusions"] = sorted(names - {""})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("bad config, using defaults: %s", exc)
    return cfg


def save_config(cfg):
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2))


def ipc(cmd, timeout=0.5, **payload):
    # one line json each way, returns None if bg isnt reachable
    try:
        rt = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", rt["port"]), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps({"cmd": cmd, "token": rt["token"], **payload}) + "\n").encode())
            reply = json.loads(s.makefile("r", encoding="utf-8").readline())
        return reply if reply.get("ok") else None
    except (OSError, ValueError, KeyError, AttributeError):
        return None


# ------------------------------------------------------------- process helpers

def pythonw_path():
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return str(windowed if windowed.exists() else exe)


def spawn(*args):
    return subprocess.Popen(
        [pythonw_path(), str(SCRIPT), *args],
        creationflags=0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP,  # DETACHED_PROCESS
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def ensure_background():
    if ipc("ping"):
        return
    spawn("--background")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ipc("ping"):
            return
        time.sleep(0.15)
    raise RuntimeError(f"Background process did not start. See {LOG_PATH}")


def startup_command():
    return f'"{pythonw_path()}" "{SCRIPT}" --background'


def startup_state():
    # on / off / stale (registered but exe or script is gone)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP)
    except FileNotFoundError:
        return "off"
    except OSError as exc:
        log.warning("cant read run key: %s", exc)
        return "off"
    try:
        paths = [p.strip('"') for p in shlex.split(value, posix=False)[:2]]
    except ValueError:
        return "stale"
    return "on" if len(paths) == 2 and all(Path(p).exists() for p in paths) else "stale"


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass


def fatal(message):
    log.error(message)
    user32.MessageBoxW(0, f"{message}\n\nLog: {LOG_PATH}", APP, 0x10)


# ------------------------------------------------------------ window inspection

def exe_name(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)  # query_limited_information
    if not handle:
        return ""  # protected process, cant open it
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_cloaked(hwnd):
    value = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return bool(value.value)


def is_fullscreen(hwnd):
    # covers the whole monitor AND doesnt look like a normal framed window.
    # a regular maximized window has a caption and its rect overshoots the monitor by the
    # invisible resize border so it never matches exactly. borderless games have no caption,
    # f11 style fullscreen keeps the caption but matches the monitor exactly
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        m_left, m_top, m_right, m_bottom = win32api.GetMonitorInfo(monitor)["Monitor"]
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    except win32api.error:
        return False
    if not (left <= m_left and top <= m_top and right >= m_right and bottom >= m_bottom):
        return False
    if (style & win32con.WS_CAPTION) != win32con.WS_CAPTION:
        return True
    return (left, top, right, bottom) == (m_left, m_top, m_right, m_bottom)


def is_transient(hwnd):
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return (
        win32gui.GetClassName(hwnd) in SHELL_CLASSES
        or pid == os.getpid()
        or exe_name(pid) in SYSTEM_EXES
    )


def describe(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return f"{exe_name(pid) or '?'} '{win32gui.GetWindowText(hwnd)[:40]}' ({hwnd})"
    except win32gui.error:
        return f"gone ({hwnd})"


# ------------------------------------------------------------------- background

class Background:
    def __init__(self):
        self.cfg = load_config()
        self.ui_pid = 0
        self.current = None  # (root owner hwnd, fg hwnd) of the last settled window
        self.hwnd = None
        self.hook = None
        self.sock = None
        self.token = secrets.token_hex(16)
        self._hook_proc = WinEventProc(guarded(self.on_foreground_event))  # keep the ref or gc eats it

    def run(self):
        self.create_window()
        self.add_tray()
        self.start_server()
        self.current = self.snapshot(win32gui.GetForegroundWindow())
        self.hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._hook_proc, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
        )
        if not self.hook:
            raise OSError(f"SetWinEventHook failed ({ctypes.get_last_error()})")
        log.info("background running, pid %s, hook %s, cfg %s", os.getpid(), self.hook, self.cfg)
        try:
            win32gui.PumpMessages()  # out-of-context hooks get delivered through this loop
        finally:
            self.cleanup()

    def cleanup(self):
        if self.hook:
            user32.UnhookWinEvent(self.hook)
            self.hook = None
        if self.sock:
            self.sock.close()  # also kicks the ipc thread out of accept
        RUNTIME_PATH.unlink(missing_ok=True)
        log.info("background stopped")

    # hidden window + tray
    def create_window(self):
        self.taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = f"{APP}Window"
        wc.lpfnWndProc = {
            win32con.WM_TIMER: guarded(self.on_timer),
            WM_TRAY: guarded(self.on_tray),
            win32con.WM_COMMAND: guarded(self.on_command),
            win32con.WM_CLOSE: guarded(lambda h, m, w, l: win32gui.DestroyWindow(h)),
            win32con.WM_DESTROY: guarded(self.on_destroy),
            self.taskbar_created: guarded(lambda h, m, w, l: self.add_tray()),  # explorer restarted
        }
        atom = win32gui.RegisterClass(wc)
        # not message-only, those dont get the TaskbarCreated broadcast
        self.hwnd = win32gui.CreateWindowEx(0, atom, APP, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)

    def add_tray(self):
        icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (self.hwnd, 0, flags, WM_TRAY, icon, APP))
        except win32gui.error as exc:
            log.warning("tray icon not added, will retry on TaskbarCreated: %s", exc)

    def on_destroy(self, hwnd, msg, wparam, lparam):
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
        except win32gui.error:
            pass
        win32gui.PostQuitMessage(0)

    def on_tray(self, hwnd, msg, wparam, lparam):
        if lparam == win32con.WM_LBUTTONUP:
            spawn("--ui")
        elif lparam == win32con.WM_RBUTTONUP:
            self.show_menu()

    def show_menu(self):
        menu = win32gui.CreatePopupMenu()
        checked = win32con.MF_CHECKED if self.cfg["enabled"] else 0
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_OPEN, f"Open {APP}")
        win32gui.AppendMenu(menu, win32con.MF_STRING | checked, ID_TOGGLE, "Minimize on focus loss")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, ID_EXIT, "Exit")
        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.hwnd)  # menu wont close on outside click otherwise
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON, x, y, 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def on_command(self, hwnd, msg, wparam, lparam):
        command = win32api.LOWORD(wparam)
        if command == ID_OPEN:
            spawn("--ui")
        elif command == ID_TOGGLE:
            cfg = load_config()
            cfg["enabled"] = not cfg["enabled"]
            try:
                save_config(cfg)
            except OSError:
                log.exception("couldnt save config")
            self.cfg = cfg
        elif command == ID_EXIT:
            win32gui.DestroyWindow(self.hwnd)

    # ipc, one thread, blocking accept beats polling
    def start_server(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        port = self.sock.getsockname()[1]
        _atomic_write(RUNTIME_PATH, json.dumps({"port": port, "token": self.token, "pid": os.getpid()}))
        threading.Thread(target=self.serve, name="ipc", daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # socket closed, shutting down
            close_after = False
            with conn:
                try:
                    conn.settimeout(2)
                    req = json.loads(conn.makefile("r", encoding="utf-8").readline())
                    if hmac.compare_digest(str(req.get("token", "")), self.token):
                        reply, close_after = self.handle(req)
                    else:
                        reply = {"ok": False, "error": "auth"}
                    conn.sendall((json.dumps(reply) + "\n").encode())
                except (OSError, ValueError, AttributeError):
                    log.exception("ipc request failed")
            if close_after:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

    def handle(self, req):
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid(), "enabled": self.cfg["enabled"], "ui_pid": self.ui_pid}, False
        if cmd == "reload":
            self.cfg = load_config()
            log.info("config reloaded: %s", self.cfg)
            return {"ok": True}, False
        if cmd == "ui_pid":
            self.ui_pid = int(req.get("pid", 0))
            return {"ok": True}, False
        if cmd == "shutdown":
            return {"ok": True}, True
        return {"ok": False, "error": f"unknown command {cmd!r}"}, False

    # foreground handling
    def on_foreground_event(self, *_):
        log.debug("foreground event")
        # re-arming the timer restarts the debounce
        if not user32.SetTimer(self.hwnd, TIMER_SETTLE, SETTLE_MS, None):
            log.error("SetTimer failed (%s)", ctypes.get_last_error())

    def on_timer(self, hwnd, msg, wparam, lparam):
        user32.KillTimer(hwnd, TIMER_SETTLE)
        self.settle()

    @staticmethod
    def snapshot(hwnd):
        if not hwnd or not win32gui.IsWindow(hwnd) or is_transient(hwnd):
            return None
        return (user32.GetAncestor(hwnd, GA_ROOTOWNER) or hwnd, hwnd)

    def settle(self):
        fg = win32gui.GetForegroundWindow()
        now = self.snapshot(fg)
        if now is None:
            log.debug("fg %s is shell/transient, ignoring", fg)
            return  # keep last real window as current
        prev, self.current = self.current, now
        log.debug("settled on %s", describe(now[1]))
        if not self.cfg["enabled"]:
            log.debug("disabled, not minimizing")
            return
        if prev is None or prev[0] == now[0]:
            return  # nothing before, or same app (dialog / owned window)
        owner, top = prev
        target = owner if win32gui.IsWindowVisible(owner) else top  # some apps hide their owner
        reason = self.skip_reason(target)
        if reason:
            log.debug("not minimizing %s: %s", describe(target), reason)
            return
        log.info("minimizing %s", describe(target))
        # async + noactivate: no hang on frozen apps, no focus steal, no retrigger
        user32.ShowWindowAsync(target, SW_SHOWMINNOACTIVE)

    def skip_reason(self, hwnd):
        cfg = self.cfg
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return "gone or invisible"
        if cfg["ignore_minimized"] and win32gui.IsIconic(hwnd):
            return "already minimized"
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if not style & win32con.WS_MINIMIZEBOX:
            return "no minimize box"
        if exstyle & win32con.WS_EX_TOOLWINDOW:
            return "tool window"
        if win32gui.GetClassName(hwnd) in SHELL_CLASSES:
            return "shell class"
        if is_cloaked(hwnd):
            return "cloaked"
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == os.getpid() or (cfg["ignore_self"] and pid == self.ui_pid):
            return "our own window"
        exe = exe_name(pid)
        if exe in cfg["exclusions"] or exe in SYSTEM_EXES:
            return f"excluded ({exe})"
        if cfg["ignore_fullscreen"] and is_fullscreen(hwnd):
            return "fullscreen"
        return None


def run_background():
    mutex = win32event.CreateMutex(None, False, MUTEX_BG)  # hold the handle for the whole process life
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        log.info("background already running, exiting")
        return
    # per-monitor v2 so window rects and monitor rects live in the same pixel space
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    Background().run()
    del mutex


# --------------------------------------------------------------------- frontend

def run_ui():
    import tkinter as tk  # lazy so the bg process doesnt load tk
    from tkinter import filedialog, messagebox, ttk

    mutex = win32event.CreateMutex(None, False, MUTEX_UI)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        existing = win32gui.FindWindow("TkTopLevel", APP)
        if existing:
            if win32gui.IsIconic(existing):
                win32gui.ShowWindow(existing, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(existing)
            except win32gui.error:
                pass  # foreground lock, at least its restored
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass

    cfg = load_config()
    root = tk.Tk()
    root.title(APP)
    root.resizable(False, False)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    style.configure(".", font=("Segoe UI", 10))
    root.option_add("*Font", "{Segoe UI} 10")

    v_enabled = tk.BooleanVar(value=cfg["enabled"])
    v_startup = tk.BooleanVar(value=startup_state() == "on")
    v_fullscreen = tk.BooleanVar(value=cfg["ignore_fullscreen"])
    v_minimized = tk.BooleanVar(value=cfg["ignore_minimized"])
    v_self = tk.BooleanVar(value=cfg["ignore_self"])
    v_status = tk.StringVar()
    v_bg_button = tk.StringVar()
    v_startup_note = tk.StringVar()
    v_exe = tk.StringVar()

    def change(**updates):
        cfg.update(load_config())  # tray might have flipped something since we last looked
        cfg.update(updates)
        try:
            save_config(cfg)
        except OSError as exc:
            messagebox.showerror(APP, f"Couldn't save settings:\n{exc}")
            return
        ipc("reload")
        fill_list()

    def toggle_startup():
        try:
            set_startup(v_startup.get())
        except OSError as exc:
            messagebox.showerror(APP, f"Couldn't update startup entry:\n{exc}")
        state = startup_state()
        v_startup.set(state == "on")
        v_startup_note.set("Registered path is invalid - tick to repair." if state == "stale" else "")

    def add_exclusion(raw):
        name = normalize_exe(raw)
        if name:
            change(exclusions=sorted(set(load_config()["exclusions"]) | {name}))
            v_exe.set("")

    def browse():
        path = filedialog.askopenfilename(filetypes=[("Executables", "*.exe")])
        if path:
            add_exclusion(path)

    def remove_selected():
        picked = {listbox.get(i) for i in listbox.curselection()}
        if picked:
            change(exclusions=[e for e in load_config()["exclusions"] if e not in picked])

    def fill_list():
        listbox.delete(0, tk.END)
        for name in cfg["exclusions"]:
            listbox.insert(tk.END, name)

    def update_status():
        info = ipc("ping")
        if info:
            v_status.set(f"Running (PID {info['pid']})")
            v_bg_button.set("Stop")
            v_enabled.set(info["enabled"])
            if info.get("ui_pid") != os.getpid():
                ipc("ui_pid", pid=os.getpid())  # also fixes it after a bg restart
        else:
            v_status.set("Not running")
            v_bg_button.set("Start")

    def tick():
        update_status()
        root.after(2000, tick)

    def toggle_background():
        if ipc("ping"):
            ipc("shutdown")
            for _ in range(20):
                if not ipc("ping", timeout=0.2):
                    break
                time.sleep(0.1)
        else:
            try:
                ensure_background()
            except RuntimeError as exc:
                messagebox.showerror(APP, str(exc))
        update_status()

    def close():
        ipc("ui_pid", pid=0)
        root.destroy()  # bg keeps running

    pad = {"padx": 12, "pady": 3}
    body = ttk.Frame(root, padding=(0, 10, 0, 12))
    body.grid()

    top = ttk.Frame(body)
    top.grid(row=0, column=0, sticky="ew", **pad)
    ttk.Label(top, text="Background:").pack(side="left")
    ttk.Label(top, textvariable=v_status).pack(side="left", padx=6)
    ttk.Button(top, textvariable=v_bg_button, width=7, command=toggle_background).pack(side="right")

    ttk.Checkbutton(body, text="Minimize windows when they lose focus", variable=v_enabled,
                    command=lambda: change(enabled=v_enabled.get())).grid(row=1, sticky="w", **pad)
    ttk.Checkbutton(body, text="Launch on Windows startup", variable=v_startup,
                    command=toggle_startup).grid(row=2, sticky="w", **pad)
    ttk.Label(body, textvariable=v_startup_note, foreground="#b42318").grid(row=3, sticky="w", padx=12)

    opts = ttk.LabelFrame(body, text="Settings", padding=8)
    opts.grid(row=4, sticky="ew", padx=12, pady=(8, 4))
    for text, var, key in (
        ("Ignore fullscreen applications", v_fullscreen, "ignore_fullscreen"),
        ("Ignore windows that are already minimized", v_minimized, "ignore_minimized"),
        (f"Ignore {APP}'s own windows", v_self, "ignore_self"),
    ):
        ttk.Checkbutton(opts, text=text, variable=var,
                        command=lambda v=var, k=key: change(**{k: v.get()})).pack(anchor="w", pady=1)

    ex = ttk.LabelFrame(body, text="Excluded executables", padding=8)
    ex.grid(row=5, sticky="ew", padx=12, pady=(4, 0))
    list_row = ttk.Frame(ex)
    list_row.pack(fill="x")
    listbox = tk.Listbox(list_row, height=6, width=34, activestyle="none", exportselection=False,
                         selectmode=tk.EXTENDED, relief="solid", borderwidth=1)
    scroll = ttk.Scrollbar(list_row, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=scroll.set)
    listbox.pack(side="left", fill="x", expand=True)
    scroll.pack(side="left", fill="y")
    add_row = ttk.Frame(ex)
    add_row.pack(fill="x", pady=(6, 0))
    entry = ttk.Entry(add_row, textvariable=v_exe)
    entry.pack(side="left", fill="x", expand=True)
    entry.bind("<Return>", lambda _e: add_exclusion(v_exe.get()))
    ttk.Button(add_row, text="Add", width=6, command=lambda: add_exclusion(v_exe.get())).pack(side="left", padx=(6, 0))
    ttk.Button(add_row, text="Browse...", command=browse).pack(side="left", padx=(6, 0))
    ttk.Button(ex, text="Remove selected", command=remove_selected).pack(anchor="e", pady=(6, 0))

    fill_list()
    if startup_state() == "stale":
        v_startup_note.set("Registered path is invalid - tick to repair.")
    root.protocol("WM_DELETE_WINDOW", close)
    root.update_idletasks()
    root.lift()
    root.focus_force()
    tick()
    root.mainloop()
    del mutex


# ------------------------------------------------------------------------- main

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="Love4wallpaper")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--ui", action="store_true")
    mode.add_argument("--show", action="store_true")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()

    handler = RotatingFileHandler(LOG_PATH, maxBytes=200_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if args.debug else logging.INFO)

    try:
        if args.background:
            run_background()
        elif args.ui:
            run_ui()
        else:
            ensure_background()
            run_ui()
    except Exception as exc:
        log.exception("fatal error")
        fatal(f"{APP} crashed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
