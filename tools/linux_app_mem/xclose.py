#!/usr/bin/env python3
"""Ask an X window to close itself (WM_DELETE_WINDOW), like clicking its title-bar close button.

There is no window manager on the Xvfb screen, so apps that only quit from the title bar
(EdrawMax) can be closed gracefully this way and save their settings.
Usage: DISPLAY=:99 python3 xclose.py WINDOW_ID
"""
import ctypes
import sys

ClientMessage = 33


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class XEvent(ctypes.Union):
    _fields_ = [("xclient", XClientMessageEvent), ("pad", ctypes.c_long * 24)]


def close_window(wid):
    x = ctypes.CDLL("libX11.so.6")
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(XEvent)]
    x.XFlush.argtypes = x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x.XOpenDisplay(None)
    if not dpy:
        raise RuntimeError("cannot open display")
    ev = XEvent()
    ev.xclient.type = ClientMessage
    ev.xclient.window = wid
    ev.xclient.message_type = x.XInternAtom(dpy, b"WM_PROTOCOLS", 0)
    ev.xclient.format = 32
    ev.xclient.data[0] = x.XInternAtom(dpy, b"WM_DELETE_WINDOW", 0)
    ev.xclient.data[1] = 0  # CurrentTime
    x.XSendEvent(dpy, wid, 0, 0, ctypes.byref(ev))
    x.XFlush(dpy)
    x.XCloseDisplay(dpy)


if __name__ == "__main__":
    close_window(int(sys.argv[1]))
