"""Cross-platform discovery of user browser binaries and authenticated profile directories."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BrowserKind = Literal["brave", "chrome", "edge", "firefox"]


@dataclass(frozen=True)
class BrowserProfileInfo:
    kind: BrowserKind
    display_name: str
    executable_path: str | None
    user_data_dir: Path
    supports_cdp: bool

    def build_cdp_launch_args(self, cdp_port: int = 9222) -> list[str]:
        if not self.executable_path:
            raise FileNotFoundError(f"{self.display_name} executable was not found on this machine.")
        if not self.supports_cdp:
            return [self.executable_path]
        return [
            self.executable_path,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]


def discover_browser_profile(kind: BrowserKind = "brave") -> BrowserProfileInfo:
    """Resolve binary path and live user profile directory for Brave, Chrome, Edge, or Firefox."""
    home = Path.home()

    if sys.platform == "win32":
        local_app = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming_app = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        prog_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        prog_x86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))

        table: dict[BrowserKind, tuple[str, list[Path], Path, bool]] = {
            "brave": (
                "Brave Browser",
                [
                    local_app / "BraveSoftware/Brave-Browser/Application/brave.exe",
                    prog_files / "BraveSoftware/Brave-Browser/Application/brave.exe",
                    prog_x86 / "BraveSoftware/Brave-Browser/Application/brave.exe",
                ],
                local_app / "BraveSoftware/Brave-Browser/User Data",
                True,
            ),
            "chrome": (
                "Google Chrome",
                [
                    prog_files / "Google/Chrome/Application/chrome.exe",
                    local_app / "Google/Chrome/Application/chrome.exe",
                ],
                local_app / "Google/Chrome/User Data",
                True,
            ),
            "edge": (
                "Microsoft Edge",
                [
                    prog_x86 / "Microsoft/Edge/Application/msedge.exe",
                    prog_files / "Microsoft/Edge/Application/msedge.exe",
                ],
                local_app / "Microsoft/Edge/User Data",
                True,
            ),
            "firefox": (
                "Mozilla Firefox",
                [prog_files / "Mozilla Firefox/firefox.exe"],
                roaming_app / "Mozilla/Firefox/Profiles",
                False,
            ),
        }
        disp, candidates, data_dir, cdp_ok = table[kind]
        exe = next((str(p) for p in candidates if p.exists()), shutil.which(kind))
        return BrowserProfileInfo(kind, disp, exe, data_dir, cdp_ok)

    if sys.platform == "darwin":
        app_sup = home / "Library/Application Support"
        mac_table: dict[BrowserKind, tuple[str, str, Path, bool]] = {
            "brave": (
                "Brave Browser",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                app_sup / "BraveSoftware/Brave-Browser",
                True,
            ),
            "chrome": (
                "Google Chrome",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                app_sup / "Google/Chrome",
                True,
            ),
            "edge": (
                "Microsoft Edge",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                app_sup / "Microsoft Edge",
                True,
            ),
            "firefox": (
                "Mozilla Firefox",
                "/Applications/Firefox.app/Contents/MacOS/firefox",
                app_sup / "Firefox/Profiles",
                False,
            ),
        }
        disp, app_bin, data_dir, cdp_ok = mac_table[kind]
        exe = app_bin if Path(app_bin).exists() else shutil.which(kind)
        return BrowserProfileInfo(kind, disp, exe, data_dir, cdp_ok)

    # Linux (X11 & Wayland)
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    linux_table: dict[BrowserKind, tuple[str, list[str], Path, bool]] = {
        "brave": (
            "Brave Browser",
            ["brave-browser", "brave-browser-stable", "brave"],
            cfg / "BraveSoftware/Brave-Browser",
            True,
        ),
        "chrome": (
            "Google Chrome",
            ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
            cfg / "google-chrome",
            True,
        ),
        "edge": (
            "Microsoft Edge",
            ["microsoft-edge", "microsoft-edge-stable"],
            cfg / "microsoft-edge",
            True,
        ),
        "firefox": (
            "Mozilla Firefox",
            ["firefox"],
            home / ".mozilla/firefox",
            False,
        ),
    }
    disp, bins, data_dir, cdp_ok = linux_table[kind]
    exe = next((shutil.which(b) for b in bins if shutil.which(b)), None)
    return BrowserProfileInfo(kind, disp, exe, data_dir, cdp_ok)
