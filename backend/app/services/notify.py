"""Immediate user notifications for agent events (CAPTCHA walls, failures, wins).

User rule (2026-11-02): when the agent cannot finish a step on its own —
above all a CAPTCHA — it must raise a flag and notify the user IMMEDIATELY so
they can clear it manually. Every notification is persisted (survives restarts,
drives the in-app bell) and ALSO pushed to the desktop right now:

  1. native Windows balloon notification (pywin32 Shell_NotifyIcon)
  2. audible alert (winsound) as the always-available fallback

Desktop pushes are best-effort: a missing display / import failure can never
break the agent loop.
"""
from __future__ import annotations

from ..db.models import Notification

APP_NAME = "jobflow"


def _balloon(title: str, detail: str, urgent: bool) -> bool:
    """Native Windows balloon via pywin32. Returns True when shown."""
    # pywin32 312 dropped NIF_*/NIM_* from win32con — they are fixed
    # Windows SDK values, so define them here.
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
    try:
        import win32api
        import win32con
        import win32gui
    except Exception:
        return False
    try:
        hinst = win32api.GetModuleHandle(None)
        cb_id = win32con.WM_USER + 20

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == cb_id:
                try:
                    win32gui.Shell_NotifyIcon(
                        NIM_DELETE, (hwnd, 0, 0, cb_id, 0, ""))
                except Exception:
                    pass
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "jobflow.notifier"
        atom = win32gui.RegisterClass(wc)
        hwnd = win32gui.CreateWindow(
            atom, "jobflow", win32con.WS_OVERLAPPED, 0, 0,
            win32con.CW_USEDEFAULT, win32con.CW_USEDEFAULT, 0, 0, hinst, None)
        try:
            hicon = win32gui.LoadIcon(
                0, win32con.IDI_WARNING if urgent else win32con.IDI_APPLICATION)
            flags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            win32gui.Shell_NotifyIcon(
                NIM_ADD, (hwnd, 0, flags, cb_id, hicon, APP_NAME))
            win32gui.Shell_NotifyIcon(
                NIM_MODIFY,
                (hwnd, 0, NIF_INFO, cb_id, hicon,
                 f"{APP_NAME} alert", f"{title} \\u2014 {detail}"[:249], 10000))
            return True
        finally:
            try:
                win32gui.Shell_NotifyIcon(
                    NIM_DELETE, (hwnd, 0, 0, cb_id, 0, ""))
                win32gui.DestroyWindow(hwnd)
                win32gui.UnregisterClass(atom, hinst)
            except Exception:
                pass
    except Exception:
        return False


def _desktop_alert(title: str, detail: str, urgent: bool) -> None:
    """Balloon if possible, audible alert as fallback. Never raises."""
    if _balloon(title, detail, urgent):
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION if urgent
                             else winsound.MB_ICONINFORMATION)
    except Exception:
        pass


def notify(db, kind: str, title: str, detail: str = "",
           link: str | None = None) -> Notification:
    """Persist a notification + push it to the desktop immediately."""
    note = Notification(kind=kind, title=title[:300],
                        detail=(detail or "")[:2000], link=link)
    db.add(note)
    db.commit()
    db.refresh(note)
    _desktop_alert(title, detail, urgent=(kind == "action"))
    return note
