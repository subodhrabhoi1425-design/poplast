#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JARVIS - a free voice-and-text assistant for Windows, macOS, Linux and Android (Termux).

* No API keys, no paid services, no LLM subscriptions.
* Visual interface : python jarvis.py  (app window; speech runs in Chrome/Edge, no pip needed)
* Terminal voice   : desktop = pyttsx3 + SpeechRecognition, Android = Termux:API
* Search answers   : DuckDuckGo Instant Answer + Wikipedia REST API (plain urllib)
* News             : Google News RSS (plain urllib + ElementTree)

Run:  python jarvis.py             (J.A.R.V.I.S. app window; needs jarvis_hud.html
                                    in the same folder)
      python jarvis.py --browser   (open in a normal browser tab instead)
      python jarvis.py --terminal  (plain terminal interface)
"""

import difflib
import html
import json
import os
import platform
import random
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

# =============================================================================
# CONFIG
# =============================================================================
ASSISTANT_NAME = "Jarvis"
USER_AGENT = "Mozilla/5.0 (compatible; JarvisAssistant/1.0)"
NEWS_HEADLINES = 5
# Google News RSS edition. Change hl / gl / ceid for another country,
# e.g. hl=en-US&gl=US&ceid=US:en
NEWS_RSS_URL = "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en"
NEWS_WEB_URL = "https://news.google.com/"
APP_CONNECT_TIMEOUT = 20  # seconds to wait for the window before using a normal browser
GOOGLE_SEARCH_URL = "https://www.google.com/search?q="


# =============================================================================
# PLATFORM DETECTION  (Windows or Android-Termux only)
# =============================================================================
def detect_platform():
    """Return 'android', 'windows', 'mac', 'linux' or None (unsupported)."""
    in_termux = (
        "com.termux" in os.environ.get("PREFIX", "")
        or os.path.isdir("/data/data/com.termux")
        or shutil.which("termux-tts-speak") is not None
    )
    if in_termux:
        return "android"
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(platform.system())


# =============================================================================
# SMALL HELPERS
# =============================================================================
def http_get(url, timeout=10):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(url, timeout=10):
    try:
        return json.loads(http_get(url, timeout).decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError, OSError):
        return None


def normalize(text):
    """Lower-case, strip punctuation noise and the 'Jarvis' wake word."""
    t = text.lower().strip()
    t = re.sub(r"[?!,;\"]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".")
    t = re.sub(r"^(?:hey |ok |okay )?jarvis\b[ ,]*", "", t)
    t = re.sub(r"[ ,]*\bjarvis$", "", t)
    return t.strip()


def best_match(name, candidates):
    """Pick the candidate that best matches `name` (exact > shortest substring match > fuzzy)."""
    name = name.lower().strip()
    lowered = [c.lower() for c in candidates]
    best, best_score = None, 0
    for original, low in zip(candidates, lowered):
        if low == name:
            score = 4
        elif name in low:  # prefix or substring; the shortest name wins the tie
            score = 2
        else:
            continue
        if score > best_score or (score == best_score and best and len(original) < len(best)):
            best, best_score = original, score
    if best:
        return best
    close = difflib.get_close_matches(name, lowered, n=1, cutoff=0.75)
    return candidates[lowered.index(close[0])] if close else None


def tidy_for_speech(text, max_sentences=3, max_chars=450):
    """Remove citations/parentheses and keep the first few sentences."""
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\s*\([^)]*\)", "", text)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out = ""
    for s in sentences[:max_sentences]:
        if out and len(out) + len(s) > max_chars:
            break
        out += (" " if out else "") + s
    return out.strip()


# =============================================================================
# VOICE BACKENDS
# =============================================================================
class VoiceError(Exception):
    """Raised when speech input is impossible (no internet, no mic...)."""


class DesktopVoice:
    """Windows / macOS / Linux: pyttsx3 for speech output, SpeechRecognition (free Google Web Speech) for input."""

    def __init__(self):
        self.engine = None
        self.sr = None
        self.recognizer = None
        self.mic_ready = False
        try:
            import pyttsx3

            self.engine = pyttsx3.init()
            self.engine.setProperty("rate", 175)
            voices = self.engine.getProperty("voices")
            for pref in ("george", "david", "mark", "ryan"):  # deeper, "Jarvis-ish"
                match = next((v for v in voices if pref in v.name.lower()), None)
                if match:
                    self.engine.setProperty("voice", match.id)
                    break
        except Exception as exc:
            print(f"[warn] Text-to-speech unavailable ({exc}). Run: pip install pyttsx3")

    def init_input(self):
        """Calibrate the microphone. Returns True if voice input works."""
        if self.mic_ready:
            return True
        try:
            import speech_recognition as sr

            self.sr = sr
            self.recognizer = sr.Recognizer()
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.pause_threshold = 0.8
            with sr.Microphone() as source:
                print("[info] Calibrating microphone for background noise...")
                self.recognizer.adjust_for_ambient_noise(source, duration=0.8)
            self.mic_ready = True
        except Exception as exc:
            print(f"[warn] Microphone unavailable ({exc}).")
            print("       Install with: pip install SpeechRecognition PyAudio")
            print("       (Linux: sudo apt install portaudio19-dev espeak-ng | macOS: brew install portaudio)")
        return self.mic_ready

    def speak(self, text):
        if not self.engine:
            return
        try:
            self.engine.say(text)
            self.engine.runAndWait()
        except Exception:
            pass

    def listen(self):
        """Return recognised text, or None if nothing was understood."""
        sr = self.sr
        with sr.Microphone() as source:
            print("[listening...]")
            try:
                audio = self.recognizer.listen(source, timeout=6, phrase_time_limit=12)
            except sr.WaitTimeoutError:
                return None
        try:
            return self.recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return None
        except sr.RequestError as exc:
            raise VoiceError(f"The speech service could not be reached ({exc}).")


class TermuxVoice:
    """Termux:API based speech output and input."""

    def __init__(self):
        self.has_tts = shutil.which("termux-tts-speak") is not None
        self.has_stt = shutil.which("termux-speech-to-text") is not None
        if not (self.has_tts and self.has_stt):
            print("[warn] Termux:API tools missing. Run: pkg install termux-api")
            print("       and install the Termux:API app from F-Droid.")

    def init_input(self):
        return self.has_stt

    def speak(self, text):
        if not self.has_tts:
            return
        try:
            # Text goes through stdin so long answers and quotes are safe.
            subprocess.run(["termux-tts-speak"], input=text, text=True, timeout=180)
        except (subprocess.SubprocessError, OSError):
            pass

    def listen(self):
        print("[listening... speak when the Google prompt appears]")
        try:
            result = subprocess.run(
                ["termux-speech-to-text"], capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            raise VoiceError(f"termux-speech-to-text failed ({exc}).")
        text = result.stdout.strip()
        return text or None


# =============================================================================
# BROWSER
# =============================================================================
def open_url(url, plat):
    """Open a URL in the default browser. Returns True on (probable) success."""
    try:
        if plat == "android":
            if shutil.which("termux-open-url"):
                subprocess.run(["termux-open-url", url], timeout=15)
            else:
                subprocess.run(
                    ["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                    capture_output=True, timeout=15,
                )
            return True
        if webbrowser.open(url, new=2):
            return True
        if hasattr(os, "startfile"):  # Windows fallback if no browser is registered
            os.startfile(url)
            return True
        opener = {"mac": "open", "linux": "xdg-open"}.get(plat)
        if opener and shutil.which(opener):
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        return False
    except Exception:
        return False


# =============================================================================
# KNOWLEDGE LOOKUP (DuckDuckGo Instant Answer + Wikipedia, no keys)
# =============================================================================
HOWTO_RE = re.compile(r"^(?:how (?:to|do i|do you|can i|should i|would i))\s+")
LEAD_RE = re.compile(
    r"^(?:(?:can|could|would) you )?(?:please )?"
    r"(?:tell me about|tell me|explain to me|explain|define|describe|"
    r"who (?:is|was|are|were)|what (?:is|are|was|were)|what's|whats|"
    r"meaning of|search for|search|google|look up|find out about|find)\s+"
)


def parse_question(text):
    """Return (kind, subject). kind is 'howto' or 'general'."""
    t = normalize(text)
    if HOWTO_RE.match(t):
        return "howto", HOWTO_RE.sub("", t, count=1).strip() or t
    subject = LEAD_RE.sub("", t, count=1)
    subject = re.sub(r"^(?:a |an |the |about )", "", subject).strip()
    return "general", subject or t


def ddg_answer(query):
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1, "t": "jarvis"}
    )
    data = http_json(url)
    if not data:
        return None
    for key in ("AbstractText", "Answer", "Definition"):
        value = data.get(key)
        if value and isinstance(value, str):
            return value
    for topic in data.get("RelatedTopics", []):
        if isinstance(topic, dict) and topic.get("Text"):
            return topic["Text"]
    return None


def wikipedia_answer(query):
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query,
         "srlimit": 4, "format": "json"}
    )
    data = http_json(url)
    if not data:
        return None
    for hit in data.get("query", {}).get("search", []):
        title = hit["title"].replace(" ", "_")
        summary = http_json(
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(title, safe="")
        )
        if not summary or summary.get("type") == "disambiguation":
            continue
        if summary.get("extract"):
            return summary["extract"]
    return None


def quick_answer(subject, full_question):
    """Try DuckDuckGo, then Wikipedia (subject), then Wikipedia (full question)."""
    for fetch, query in (
        (ddg_answer, subject),
        (wikipedia_answer, subject),
        (wikipedia_answer, full_question),
    ):
        try:
            result = fetch(query)
        except Exception:
            result = None
        if result:
            source = "DuckDuckGo" if fetch is ddg_answer else "Wikipedia"
            return tidy_for_speech(result), source
    return None, None


# =============================================================================
# NEWS (Google News RSS)
# =============================================================================
def fetch_headlines(limit=NEWS_HEADLINES):
    root = ET.fromstring(http_get(NEWS_RSS_URL, timeout=12))
    headlines = []
    for item in root.iter("item"):
        raw = html.unescape(item.findtext("title") or "").strip()
        if not raw:
            continue
        title, _, source = raw.rpartition(" - ")
        headlines.append((title or raw, source if title else ""))
        if len(headlines) >= limit:
            break
    return headlines


# =============================================================================
# APP LAUNCHER
# =============================================================================
# Sites used as a friendly fallback when the real app is not installed.
WEB_FALLBACKS = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
    "twitter": "https://x.com", "x": "https://x.com",
    "linkedin": "https://www.linkedin.com", "github": "https://github.com",
    "reddit": "https://www.reddit.com", "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com", "amazon": "https://www.amazon.in",
    "maps": "https://maps.google.com", "drive": "https://drive.google.com",
    "telegram": "https://web.telegram.org", "pinterest": "https://www.pinterest.com",
    "snapchat": "https://www.snapchat.com", "photos": "https://photos.google.com",
}

# ---- Windows built-ins (always present, launched through `start`) -----------
WINDOWS_BUILTIN = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "command prompt": "cmd", "cmd": "cmd",
    "powershell": "powershell", "task manager": "taskmgr",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "settings": "ms-settings:", "control panel": "control",
    "camera": "microsoft.windows.camera:", "clock": "ms-clock:",
    "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
}

# ---- Android package names / intents ---------------------------------------
ANDROID_PACKAGES = {
    "whatsapp": ["com.whatsapp", "com.whatsapp.w4b"],
    "youtube": ["com.google.android.youtube"],
    "chrome": ["com.android.chrome"],
    "browser": ["com.android.chrome", "com.sec.android.app.sbrowser"],
    "instagram": ["com.instagram.android"],
    "spotify": ["com.spotify.music"],
    "facebook": ["com.facebook.katana"],
    "messenger": ["com.facebook.orca"],
    "telegram": ["org.telegram.messenger"],
    "twitter": ["com.twitter.android"], "x": ["com.twitter.android"],
    "snapchat": ["com.snapchat.android"],
    "netflix": ["com.netflix.mediaclient"],
    "gmail": ["com.google.android.gm"],
    "maps": ["com.google.android.apps.maps"],
    "google maps": ["com.google.android.apps.maps"],
    "photos": ["com.google.android.apps.photos"],
    "gallery": ["com.google.android.apps.photos", "com.sec.android.gallery3d"],
    "play store": ["com.android.vending"],
    "playstore": ["com.android.vending"],
    "calculator": ["com.google.android.calculator", "com.android.calculator2",
                   "com.sec.android.app.popupcalculator", "com.miui.calculator"],
    "clock": ["com.google.android.deskclock", "com.android.deskclock"],
    "contacts": ["com.google.android.contacts", "com.android.contacts"],
    "messages": ["com.google.android.apps.messaging", "com.android.mms"],
    "files": ["com.google.android.apps.nbu.files", "com.android.documentsui"],
    "drive": ["com.google.android.apps.docs"],
    "linkedin": ["com.linkedin.android"],
    "reddit": ["com.reddit.frontpage"],
    "pinterest": ["com.pinterest"],
    "zoom": ["us.zoom.videomeetings"],
    "discord": ["com.discord"],
    "amazon": ["in.amazon.mShop.android.shopping", "com.amazon.mShop.android.shopping"],
    "paytm": ["net.one97.paytm"],
    "phonepe": ["com.phonepe.app"],
    "google pay": ["com.google.android.apps.nbu.paisa.user"],
    "gpay": ["com.google.android.apps.nbu.paisa.user"],
}
ANDROID_INTENTS = {  # name -> intent action
    "camera": "android.media.action.STILL_IMAGE_CAMERA",
    "settings": "android.settings.SETTINGS",
    "wifi": "android.settings.WIFI_SETTINGS",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "phone": "android.intent.action.DIAL",
    "dialer": "android.intent.action.DIAL",
}


class AppLauncher:
    def __init__(self, plat):
        self.plat = plat
        self._start_apps = None  # cached Windows Start-menu list
        self._packages = None    # cached Android package list
        self._desktop = None     # cached Linux .desktop entries

    # ---- public entry point ----------------------------------------------
    def open(self, raw_name):
        """Try to open an app. Returns a human-readable result sentence."""
        name = re.sub(r"\b(the|app|application|please)\b", " ", raw_name.lower())
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            return "Which app would you like me to open?"
        display = name.title()

        # "open google.com" style requests
        if re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", name):
            open_url("https://" + name, self.plat)
            return f"Opening {name} in your browser."

        opener = {"windows": self._open_windows, "android": self._open_android,
                  "mac": self._open_mac, "linux": self._open_linux}[self.plat]
        launched = opener(name)
        if launched:
            return f"Opening {display}."

        # ---- fallbacks: web version, then plain Google search ----------------
        if name in WEB_FALLBACKS:
            open_url(WEB_FALLBACKS[name], self.plat)
            return f"I couldn't find {display} installed, so I've opened its website instead."
        open_url(GOOGLE_SEARCH_URL + urllib.parse.quote_plus(name + " app"), self.plat)
        return f"I couldn't find {display} on this device, so I've searched for it in your browser."

    # ---- Windows -----------------------------------------------------------
    def _open_windows(self, name):
        if name in WINDOWS_BUILTIN:  # fixed, trusted dictionary values only
            subprocess.Popen(f'start "" "{WINDOWS_BUILTIN[name]}"', shell=True)
            return True

        # Dynamic Start-menu lookup (classic apps + Store apps)
        apps = self._windows_start_apps()
        if apps:
            match = best_match(name, list(apps))
            if match:
                try:
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{apps[match]}"])
                    return True
                except OSError:
                    pass

        # Optional second opinion from the AppOpener library
        try:
            from AppOpener import open as app_open

            app_open(name, match_closest=True, output=False, throw_error=True)
            return True
        except Exception:
            return False

    def _windows_start_apps(self):
        if self._start_apps is not None:
            return self._start_apps
        self._start_apps = {}
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Get-StartApps | ConvertTo-Json -Compress")
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8", timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]
            self._start_apps = {a["Name"]: a["AppID"] for a in data if a.get("Name")}
        except Exception:
            pass
        return self._start_apps

    # ---- macOS ---------------------------------------------------------------
    def _open_mac(self, name):
        alias = {"settings": "System Settings", "preferences": "System Settings",
                 "system preferences": "System Settings", "camera": "Photo Booth",
                 "browser": "Safari", "files": "Finder", "text editor": "TextEdit"}
        target = alias.get(name, name)
        apps = {}
        for folder in ("/Applications", "/Applications/Utilities", "/System/Applications",
                       "/System/Applications/Utilities", os.path.expanduser("~/Applications")):
            try:
                for entry in os.listdir(folder):
                    if entry.endswith(".app"):
                        apps[entry[:-4]] = os.path.join(folder, entry)
            except OSError:
                pass
        match = best_match(target, list(apps))
        cmd = ["open", apps[match]] if match else ["open", "-a", target]
        try:
            return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # ---- Linux ---------------------------------------------------------------
    LINUX_ALIASES = {
        "calculator": ["gnome-calculator", "kcalc", "galculator", "mate-calc", "qalculate-gtk", "xcalc"],
        "files": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm", "caja"],
        "file manager": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm", "caja"],
        "terminal": ["gnome-terminal", "konsole", "xfce4-terminal", "x-terminal-emulator", "kitty", "alacritty"],
        "settings": ["gnome-control-center", "systemsettings", "xfce4-settings-manager", "cinnamon-settings"],
        "text editor": ["gnome-text-editor", "gedit", "kate", "mousepad", "xed", "pluma"],
        "notepad": ["gnome-text-editor", "gedit", "kate", "mousepad", "xed", "pluma"],
    }

    def _open_linux(self, name):
        for cmd in self.LINUX_ALIASES.get(name, []):
            if shutil.which(cmd) and self._spawn([cmd]):
                return True
        entries = self._linux_desktop_entries()
        match = best_match(name, list(entries))
        if not match:
            return False
        path = entries[match]
        for cmd in (["gtk-launch", os.path.basename(path)], ["gio", "launch", path]):
            if shutil.which(cmd[0]) and self._spawn(cmd):
                return True
        return False

    @staticmethod
    def _spawn(cmd):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            try:
                return p.wait(timeout=4) == 0
            except subprocess.TimeoutExpired:
                return True  # still running = the app opened
        except OSError:
            return False

    def _linux_desktop_entries(self):
        """Map 'App Name' -> .desktop file path for every installed launcher."""
        if self._desktop is not None:
            return self._desktop
        self._desktop = {}
        dirs = ["/usr/share/applications", "/usr/local/share/applications",
                os.path.expanduser("~/.local/share/applications"),
                "/var/lib/flatpak/exports/share/applications",
                os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
                "/var/lib/snapd/desktop/applications"]
        for folder in dirs:
            try:
                files = os.listdir(folder)
            except OSError:
                continue
            for fn in files:
                if not fn.endswith(".desktop"):
                    continue
                path, name, hidden, in_entry = os.path.join(folder, fn), None, False, False
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("["):
                                in_entry = line == "[Desktop Entry]"
                            elif in_entry and line.startswith("Name=") and name is None:
                                name = line[5:]
                            elif in_entry and line in ("NoDisplay=true", "Hidden=true"):
                                hidden = True
                except OSError:
                    continue
                if name and not hidden:
                    self._desktop[name] = path
        return self._desktop

    # ---- Android -----------------------------------------------------------
    def _open_android(self, name):
        if name in ANDROID_INTENTS:
            if self._android_intent(ANDROID_INTENTS[name]):
                return True
        for pkg in ANDROID_PACKAGES.get(name, []):
            if self._android_package(pkg):
                return True
        # Dynamic lookup across everything installed
        key = name.replace(" ", "")
        if len(key) >= 3:
            matches = [p for p in self._android_installed() if key in p.lower()]
            for pkg in sorted(matches, key=len):
                if self._android_package(pkg):
                    return True
        return False

    @staticmethod
    def _android_package(pkg):
        """Launch by package using monkey, then `am start` as a backup."""
        attempts = [
            ["monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
            ["am", "start", "--user", "0", "-a", "android.intent.action.MAIN",
             "-c", "android.intent.category.LAUNCHER", "-p", pkg],
        ]
        for cmd in attempts:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                continue
            out = (r.stdout + r.stderr).lower()
            if r.returncode == 0 and not any(
                bad in out for bad in ("no activities found", "aborted", "error", "unable to resolve")
            ):
                return True
        return False

    @staticmethod
    def _android_intent(action):
        try:
            r = subprocess.run(["am", "start", "-a", action],
                               capture_output=True, text=True, timeout=15)
            return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()
        except (OSError, subprocess.SubprocessError):
            return False

    def _android_installed(self):
        if self._packages is None:
            self._packages = []
            try:
                r = subprocess.run(["pm", "list", "packages"],
                                   capture_output=True, text=True, timeout=20)
                self._packages = [ln.split(":", 1)[1].strip()
                                  for ln in r.stdout.splitlines() if ln.startswith("package:")]
            except (OSError, subprocess.SubprocessError):
                pass
        return self._packages


# =============================================================================
# PERSONALITY
# =============================================================================
JOKES = [
    "I would tell you a UDP joke, but you might not get it.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I'd tell you a joke about recursion, but first I'd have to tell you a joke about recursion.",
    "There are 10 kinds of people: those who understand binary and those who don't.",
    "I tried to catch some fog earlier. I mist.",
]
SEARCH_LINES = [
    "Pulling that up in your browser now.",
    "On it. Your browser is about to get busy.",
    "Right away. Opening the results while I dig up a summary.",
]
CAPABILITIES = (
    "I can open apps, search the web and read you a summary, bring you today's headlines, "
    "tell you the time, date and day, shut down, restart or sleep your computer (after asking you first), "
    "and switch between voice and text mode. "
    "Try: 'open calculator', 'search for football', 'who is Albert Einstein', or 'latest news'."
)


# =============================================================================
# COMPUTER POWER CONTROL (shutdown / restart / sleep)
# =============================================================================
class PowerControl:
    """Schedules a power action a few seconds ahead so it can still be cancelled."""
    DELAY = 10  # seconds the user has to say "cancel" (sleep uses 3)

    def __init__(self, plat):
        self.plat = plat
        self.proc = None
        self.scheduled = None  # (action, time it will fire)

    def active(self):
        return bool(self.scheduled and time.time() < self.scheduled[1])

    def _unix_command(self, action):
        if self.plat == "mac":
            if action == "sleep":
                return ["pmset", "sleepnow"]
            verb = "shut down" if action == "shutdown" else "restart"
            return ["osascript", "-e", f'tell application "System Events" to {verb}']
        return ["systemctl", {"shutdown": "poweroff", "restart": "reboot", "sleep": "suspend"}[action]]

    def run(self, action):
        """Schedule the action. Returns (ok, message to say)."""
        delay = 3 if action == "sleep" else self.DELAY
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if self.plat == "windows":
                if action == "sleep":
                    cmd = (f"ping -n {delay + 1} 127.0.0.1 >nul & "
                           "rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
                    self.proc = subprocess.Popen(cmd, shell=True, creationflags=flags)
                else:
                    flag = "/s" if action == "shutdown" else "/r"
                    subprocess.run(["shutdown", flag, "/t", str(delay)], check=True,
                                   capture_output=True, creationflags=flags)
            else:
                cmd = self._unix_command(action)
                if not shutil.which(cmd[0]):
                    return False, f"I can't do that here: '{cmd[0]}' isn't available on this system."
                line = f"sleep {delay}; exec " + " ".join(shlex.quote(c) for c in cmd)
                self.proc = subprocess.Popen(["sh", "-c", line], start_new_session=True,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"I couldn't do that: {exc}"
        self.scheduled = (action, time.time() + delay)
        text = {"shutdown": "Shutting down", "restart": "Restarting", "sleep": "Putting the computer to sleep"}[action]
        return True, f"{text} in {delay} seconds. Say 'cancel' to stop me."

    def cancel(self):
        """Abort a scheduled action. Returns True if something was cancelled."""
        if not self.active():
            self.scheduled = None
            return False
        action = self.scheduled[0]
        self.scheduled = None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            if self.plat == "windows":
                if action != "sleep":
                    subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=flags)
                if self.proc:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True, creationflags=flags)
            elif self.proc:
                os.killpg(self.proc.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            return False
        self.proc = None
        return True


# =============================================================================
# THE ASSISTANT
# =============================================================================
class Jarvis:
    def __init__(self, plat, hud=False):
        self.plat = plat
        self.web = None  # list of replies when running behind the HUD
        self.user_name = ""  # set from the HUD "Account" panel
        # In HUD mode the browser handles speech, so no local voice engine is needed.
        self.voice = None if hud else (TermuxVoice() if plat == "android" else DesktopVoice())
        self.launcher = AppLauncher(plat)
        self.power = PowerControl(plat)
        self.pending = None  # (action, time asked) while waiting for "yes"
        self.mode = "text"
        self.speak_enabled = False
        self.silent_misses = 0

    # ---- output ------------------------------------------------------------
    def say(self, text, spoken=None):
        """Print a reply and, when enabled, speak it (optionally a shorter version)."""
        if self.web is not None:  # HUD mode: collect replies for the browser
            self.web.append({"text": text, "spoken": spoken or text})
            return
        print(f"\n{ASSISTANT_NAME}: {text}\n")
        if self.speak_enabled:
            self.voice.speak(spoken or text)

    # ---- modes -------------------------------------------------------------
    def set_mode(self, mode, announce=True):
        if self.web is not None:  # HUD: the browser owns the microphone
            self.mode, self.speak_enabled = mode, mode == "voice"
            if announce:
                self.say("Voice mode engaged. I'm all ears." if mode == "voice"
                         else "Text mode on. Type away.")
            return
        if mode == "voice":
            if not self.voice.init_input():
                self.mode, self.speak_enabled = "text", False
                if announce:
                    self.say("Voice input isn't available right now, so I'll stay in text mode. "
                             "Check the setup notes for your platform.")
                return
            self.mode, self.speak_enabled = "voice", True
            if announce:
                self.say("Voice mode engaged. I'm all ears.")
        else:
            self.mode, self.speak_enabled = "text", False
            if announce:
                self.say("Text mode on. Type away. Say 'unmute' if you'd like me to speak replies.")

    def choose_startup_mode(self):
        print("=" * 56)
        print(f"  {ASSISTANT_NAME.upper()} - your free personal assistant")
        print("=" * 56)
        print("  [1] Voice mode  (talk to me, I talk back)")
        print("  [2] Text mode   (type to me)")
        while True:
            choice = input("Choose 1 or 2: ").strip().lower()
            if choice in ("1", "voice", "v"):
                self.set_mode("voice", announce=False)
                return
            if choice in ("2", "text", "t"):
                self.set_mode("text", announce=False)
                return
            print("Please enter 1 or 2.")

    # ---- input -------------------------------------------------------------
    def get_input(self):
        if self.mode == "text":
            return input("You: ").strip()
        try:
            heard = self.voice.listen()
        except VoiceError as exc:
            self.say(f"{exc} Switching to text mode.")
            self.set_mode("text", announce=False)
            return None
        if not heard:
            self.silent_misses += 1
            if self.silent_misses % 4 == 0:
                self.say("I'm not catching anything. Say 'switch to text' if the microphone is being shy.")
            return None
        self.silent_misses = 0
        print(f"You (voice): {heard}")
        return heard

    # ---- main loop ---------------------------------------------------------
    def run(self):
        self.choose_startup_mode()
        hour = datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        self.say(f"Good {part}. {ASSISTANT_NAME} online and fully operational. "
                 "How may I help? Say 'help' to hear what I can do.")
        try:
            while True:
                text = self.get_input()
                if not text:
                    continue
                if not self.handle(text):
                    break
        except (KeyboardInterrupt, EOFError):
            print()
        self.say("Powering down. Do call if you need me.")

    # ---- command router ----------------------------------------------------
    def handle(self, raw):
        """Process one command. Returns False when Jarvis should exit."""
        t = normalize(raw)
        if not t:
            self.say("Yes? I'm listening.")
            return True
        words = t.split()

        # 0. Waiting for a yes/no after a shutdown or restart request
        if self.pending:
            action, asked = self.pending
            self.pending = None
            if time.time() - asked < 30:
                if re.fullmatch(r"(?:yes|yeah|yep|yup|confirm|confirmed|do it|go ahead|proceed|affirmative)"
                                r"(?: please)?", t):
                    return self._power_do(action)
                if re.fullmatch(r"(?:no|nope|cancel|stop|never ?mind|don'?t|abort|no thanks)", t):
                    self.say("Understood. Standing down.")
                    return True
        if self.power.active() and re.fullmatch(
                r"(?:please )?(?:cancel|stop|abort|wait|never ?mind)(?: (?:it|that|the .*))?", t):
            self._handle_power("cancel")
            return True

        # 1. Exit
        if re.fullmatch(r"(?:please )?(exit|quit|bye|goodbye|good bye|go to sleep|"
                        r"sleep|stop|that'?s all|that is all|see you|see you later)", t):
            return False

        # 2. Mode switching and speech toggles
        if re.search(r"\b(?:switch|change|go)(?: over)? to (?:the )?(?:text|typing|keyboard)\b"
                     r"|\btext mode\b|\bstart typing\b", t):
            self.set_mode("text")
            return True
        if re.search(r"\b(?:switch|change|go)(?: over)? to (?:the )?(?:voice|speech|talking|microphone)\b"
                     r"|\bvoice mode\b", t):
            self.set_mode("voice")
            return True
        if re.search(r"\b(unmute|speak replies|read (?:it )?aloud|turn on (?:the )?voice)\b", t):
            self.speak_enabled = True
            self.say("Voice replies enabled.")
            return True
        if re.search(r"\b(mute|be quiet|stop talking|silence)\b", t):
            self.say("Muted. I'll keep my thoughts on screen.")
            self.speak_enabled = False
            return True

        # 2b. Computer power: shutdown / restart (asks first) and sleep
        action = self._power_request(t)
        if action:
            self._handle_power(action)
            return True

        # 3. Small talk
        if self._small_talk(t, words):
            return True

        # 4. News (before date/time because "today's news" contains "today")
        if re.search(r"\b(news|headlines)\b", t) and not re.search(r"\b(about|on|regarding|of)\b", t):
            self.do_news()
            return True

        # 5. Time / date / day
        if self._time_date_day(t):
            return True

        # 6. Open an app or website
        m = re.match(r"^(?:please )?(?:open|launch|start|run)\s+(.+)$", t)
        if m:
            self.say(self.launcher.open(m.group(1)))
            return True

        # 7. Explicit search: "search for X", "search X", "google X", "look up X"
        if re.fullmatch(r"(?:please )?(?:search|google|look up|lookup)(?: for)?", t):
            self.say("What would you like me to search for?")
            return True
        m = re.match(r"^(?:please )?(?:search(?: google| the web| online)?(?: for)?|google|look up|lookup)\s+(.+)$", t)
        if m:
            self.do_search(m.group(1).strip())
            return True

        # 8. A genuine question ("who is...", "what is...", "how to...") -> browser + summary
        if len(words) >= 2 and re.match(
                r"^(who|whom|whose|what|what's|whats|when|where|why|how|which|explain|define|"
                r"describe|tell me about|meaning of)\b", t):
            self.do_search(t)
            return True

        # 9. Anything else: do NOT open the browser
        if re.fullmatch(r"(?:o+h+|a+h+|u+h+|u+m+|h+m+|e+h+|ok|okay|yeah|yes|no|huh|hey|wow|oh yeah|oh okay)", t):
            self.say(random.choice(["I'm here whenever you need me.", "Standing by.", "Yes? I'm listening."]))
        else:
            self.say(random.choice([
                "I'm not sure what you mean by that. Try 'open Chrome' or 'search for' followed by a topic.",
                "That one got past me. You can ask a question, say 'open' and an app name, or 'search for' something.",
            ]))
        return True

    # ---- computer power ----------------------------------------------------
    @staticmethod
    def _power_request(t):
        """Return 'shutdown' / 'restart' / 'sleep' / 'cancel' if t asks for it, else None."""
        if re.match(r"^(how|what|why|when|where|who|which|can|does|is|explain|tell me)\b", t):
            return None  # a question ABOUT shutting down, not a command
        target = r"\b(?:pc|computer|laptop|system|machine|windows|macbook|mac)\b"
        has_target = re.search(target, t) is not None
        if re.search(r"\b(?:cancel|abort)\b.*\b(?:shut ?down|restart|reboot|sleep)\b", t):
            return "cancel"
        if re.search(r"\b(?:shut ?down|power off|turn off)\b", t) and (
                has_target or re.fullmatch(r"(?:please )?(?:shut ?down|power off)(?: now)?", t)):
            return "shutdown"
        if re.search(r"\b(?:restart|reboot)\b", t) and (
                has_target or re.fullmatch(r"(?:please )?(?:restart|reboot)(?: now)?", t)):
            return "restart"
        if re.search(r"\b(?:sleep|hibernate|suspend)\b", t) and has_target:
            return "sleep"
        return None

    def _handle_power(self, action):
        if action == "cancel":
            self.say("Cancelled. Your computer is staying on." if self.power.cancel()
                     else "Nothing is scheduled right now.")
        elif self.plat == "android":
            self.say("Android doesn't let apps power off, restart or sleep the phone without root "
                     "access, so that one is beyond me.")
        elif action == "sleep":
            self._power_do("sleep")
        else:
            self.pending = (action, time.time())
            label = "shut down" if action == "shutdown" else "restart"
            self.say(f"Are you sure you want me to {label} the computer? "
                     "Say or type 'yes' to confirm, or 'no' to cancel.")

    def _power_do(self, action):
        ok, message = self.power.run(action)
        self.say(message)
        return True

    # ---- command implementations ------------------------------------------
    def _small_talk(self, t, words):
        if len(words) > 7:
            return False
        if re.search(r"\bwho are you\b|\byour name\b|\bwhat are you\b", t):
            self.say("I'm Jarvis: Just A Rather Very Intelligent System. Less dramatic than the "
                     "movie version, but I'll open your apps without complaining.")
        elif re.search(r"\bhow are you\b|\bhow do you do\b|\bhow'?s it going\b", t):
            self.say("Running at full capacity, with no coffee required. And you?")
        elif re.match(r"^(hello|hi|hey|yo|hola|namaste|good (morning|afternoon|evening|night))\b", t):
            hour = datetime.now().hour
            part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
            who = f", {self.user_name}" if self.user_name else ""
            self.say(random.choice([
                f"Hello{who}! Good {part}. What shall we do today?",
                f"Good {part}{who}. Jarvis at your service.",
                "Hello there. Ask me anything, or have me open something.",
            ]))
        elif re.search(r"\bwho am i\b|\bmy name\b", t):
            self.say(f"You're {self.user_name}, of course." if self.user_name else
                     "I don't know your name yet. Open Account (bottom left) and tell me.")
        elif re.search(r"\bthank(?:s| you)\b", t):
            self.say(random.choice(["My pleasure.", "Always happy to help.", "Anytime."]))
        elif re.search(r"\bwhat can you do\b|^help$|\bwhat do you do\b|\bcommands\b", t):
            self.say(CAPABILITIES)
        elif re.search(r"\b(joke|make me laugh)\b", t):
            self.say(random.choice(JOKES))
        else:
            return False
        return True

    def _time_date_day(self, t):
        now = datetime.now()
        time_str = now.strftime("%I:%M %p").lstrip("0")
        date_str = f"{now.strftime('%B')} {now.day}, {now.year}"
        day_str = now.strftime("%A")

        want_time = re.search(r"\bwhat(?:'s| is)? the (?:current )?time\b|\bwhat time is it\b|"
                              r"\bcurrent time\b|\btell me the time\b|^time$|\btime now\b|"
                              r"\btime please\b|\bthe time\b", t)
        want_date = re.search(r"\btoday'?s date\b|\bwhat(?:'s| is)? the date\b|\bwhat date\b|"
                              r"\bcurrent date\b|\bdate today\b|^date$|\btell me the date\b", t)
        want_day = re.search(r"\bwhat day (?:is it|is today|today)\b|\bwhich day (?:is it|is today)\b|"
                             r"\bday today\b|\bday of the week\b|^day$|\btoday'?s day\b", t)
        if not (want_time or want_date or want_day):
            return False

        parts = []
        if want_time:
            parts.append(f"The time is {time_str}")
        if want_day:
            parts.append(f"Today is {day_str}")
        if want_date:
            parts.append(f"The date is {day_str}, {date_str}")
        self.say(". ".join(parts) + ".")
        return True

    def do_search(self, query):
        kind, subject = parse_question(query)
        self.say(random.choice(SEARCH_LINES))
        open_url(GOOGLE_SEARCH_URL + urllib.parse.quote_plus(query), self.plat)  # open FIRST

        answer, source = quick_answer(subject, query)
        if answer:
            intro = ("Here's some background while your browser loads step-by-step guides: "
                     if kind == "howto" else "")
            self.say(intro + answer + f"\n(Source: {source})", spoken=intro + answer)
        else:
            self.say("I couldn't fetch a quick summary for that, but the full results "
                     "are waiting in your browser.")

    def do_news(self):
        self.say("Opening Google News and fetching the top headlines.")
        open_url(NEWS_WEB_URL, self.plat)
        try:
            items = fetch_headlines()
        except Exception:
            items = []
        if not items:
            self.say("I couldn't read the headlines just now, but Google News is open in your browser.")
            return
        printed = "\n".join(f"  {i}. {title}" + (f"  [{src}]" if src else "")
                            for i, (title, src) in enumerate(items, 1))
        spoken = "Here are today's top headlines. " + " ".join(
            f"Number {i}. {title}." for i, (title, _) in enumerate(items, 1))
        self.say("Here are today's top headlines:\n" + printed, spoken=spoken)


# =============================================================================
# STANDALONE APP WINDOW (Windows)
# =============================================================================
def find_app_browser():
    """Chrome or Edge (they have working speech recognition), used for a chrome-less app window."""
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            for root in roots:
                if root and os.path.isfile(os.path.join(root, rel)):
                    return os.path.join(root, rel)
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium"):
            path = f"/Applications/{app}.app/Contents/MacOS/{app}"
            if os.path.isfile(path):
                return path
    else:
        for exe in ("google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable",
                    "chromium", "chromium-browser"):
            if shutil.which(exe):
                return shutil.which(exe)
    return None


def open_app_window(url):
    """Open the HUD in its own app window. Returns False if no suitable browser exists."""
    exe = find_app_browser()
    if not exe:
        return False
    print(f"[info] Opening the Jarvis window with: {exe}")
    try:
        subprocess.Popen([exe, f"--app={url}", "--window-size=860,930", "--no-first-run"])
        return True
    except OSError as exc:
        print(f"[warn] Could not start the app window ({exc}).")
        return False


# =============================================================================
# HUD SERVER  (python jarvis.py --hud)
# =============================================================================
def run_hud(plat, port=8765):
    """Serve the J.A.R.V.I.S. interface on localhost and open it in the browser."""
    jarvis = Jarvis(plat, hud=True)
    jarvis.web = []
    token = secrets.token_urlsafe(16)  # blocks other websites from sending commands
    lock = threading.Lock()
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_hud.html")
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    seen = {"ping": None, "bye": None}  # lets the app close itself when its window closes

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(403, b"Forbidden", "text/plain")
            if self.path.split("?")[0] not in ("/", "/index.html"):
                return self._send(404, b"Not found", "text/plain")
            try:
                with open(page_path, encoding="utf-8") as f:
                    page = f.read().replace("__TOKEN__", token)
            except OSError:
                return self._send(500, b"jarvis_hud.html must be next to jarvis.py", "text/plain")
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            path, _, query = self.path.partition("?")
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(403, b"{}")
            if path == "/api/bye" and urllib.parse.parse_qs(query).get("t") == [token]:
                seen["bye"] = time.time()  # window closing (or reloading: a ping cancels this)
                return self._send(200, b"{}")
            if path == "/api/ping" and self.headers.get("X-Token") == token:
                seen["ping"] = time.time()
                return self._send(200, b"{}")
            if path != "/api/command" or self.headers.get("X-Token") != token:
                return self._send(403, b"{}")
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, b"{}")
            with lock:
                jarvis.web.clear()
                jarvis.user_name = re.sub(r"[^\w .'-]", "", str(data.get("name", "")))[:30].strip()
                if data.get("mode") in ("voice", "text") and data["mode"] != jarvis.mode:
                    jarvis.mode, jarvis.speak_enabled = data["mode"], data["mode"] == "voice"
                keep = jarvis.handle(str(data.get("text", ""))[:500])
                out = {"replies": list(jarvis.web), "mode": jarvis.mode,
                       "speak": jarvis.speak_enabled, "exit": not keep}
            self._send(200, json.dumps(out).encode("utf-8"))
            if not keep:
                threading.Thread(target=server.shutdown, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"

    def show():
        """Standalone app window on desktops (unless --browser); normal browser otherwise."""
        if plat != "android" and "--browser" not in sys.argv and open_app_window(url):
            return
        open_url(url, plat)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True  # never wait for a half-finished request on exit
    except OSError:
        print(f"[info] Port {port} is busy - Jarvis is probably already running. Showing it.")
        show()
        return

    def watchdog():
        while True:
            time.sleep(1.5)
            now = time.time()
            closed = seen["bye"] and now - seen["bye"] > 3 and (not seen["ping"] or seen["ping"] < seen["bye"])
            lost = seen["ping"] and now - seen["ping"] > 600
            if closed or lost:
                try:
                    print("[info] Jarvis window closed - shutting down.", flush=True)
                except Exception:
                    pass
                os._exit(0)  # stop at once, even if an answer is still being prepared

    def connect_check():
        """If the window never loads the page, fall back to the default browser."""
        time.sleep(APP_CONNECT_TIMEOUT)
        if not seen["ping"]:
            print("[warn] The Jarvis window did not connect - opening your normal browser instead.")
            print(f"       If that fails too, open this address yourself: {url}")
            open_url(url, plat)

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=connect_check, daemon=True).start()
    print(f"[info] Jarvis server started at {url}  (Ctrl+C to quit)")
    show()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


# =============================================================================
# ENTRY POINT
# =============================================================================
def main():
    try:  # keep Windows consoles from choking on non-ASCII headlines
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    plat = detect_platform()
    if plat is None:
        print("Jarvis supports Windows, macOS, Linux and Android (via Termux).")
        sys.exit(1)
    print(f"[info] Jarvis v2 (visual interface build) | Python {platform.python_version()}")
    print(f"[info] Detected platform: {dict(android='Android (Termux)', windows='Windows', mac='macOS', linux='Linux')[plat]}")
    hud_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_hud.html")
    if "--terminal" in sys.argv:
        Jarvis(plat).run()
    elif os.path.isfile(hud_file):
        run_hud(plat)  # the visual J.A.R.V.I.S. interface is the default
    else:
        print("[info] jarvis_hud.html not found next to jarvis.py - starting terminal mode.")
        Jarvis(plat).run()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        import traceback

        details = traceback.format_exc()
        print("\n[ERROR] Jarvis could not start:\n" + details)
        try:
            log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_log.txt")
            with open(log, "w", encoding="utf-8") as f:
                f.write(details)
            print(f"(This error was also saved to {log})")
        except OSError:
            pass
        try:
            input("Press Enter to close...")
        except (EOFError, RuntimeError):
            pass
