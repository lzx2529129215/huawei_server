#!/usr/bin/env python3
"""Show a Windows toast notification from WSL.  Usage: notify_windows.py TITLE MESSAGE

Uses the Windows PowerShell toast API through WSL interop; the script is passed with
-EncodedCommand (UTF-16LE base64) so Chinese text and quotes survive the command line.
"""
import base64
import subprocess
import sys

POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def esc(s):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return s.replace("'", "''")  # inside a single-quoted PowerShell string


def notify(title, message):
    ps = f"""
$ProgressPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(message)}</text></binding></visual></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, timeout=60)
    # Windows PowerShell writes in the console code page (GBK on a Chinese system).
    return r.returncode, r.stderr.decode("gbk", "replace")


if __name__ == "__main__":
    code, err = notify(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    if code != 0:
        sys.exit(err.strip() or f"powershell exited {code}")
