import os
import sys
import threading
import time
import paramiko
import pygame
from pygame.locals import *
from urllib.parse import urlparse
from dotenv import load_dotenv
import traceback
from datetime import datetime
import stat

# --- Load the .env (Baked or local file) ---
if getattr(sys, "frozen", False):
    frozen_dir = getattr(sys, "_MEIPASS", None)
    exe_dir = os.path.dirname(sys.executable)
else:
    frozen_dir = None
    exe_dir = os.path.dirname(os.path.abspath(__file__))
loaded = False
if frozen_dir:
    baked_env = os.path.join(frozen_dir, ".env")
    if os.path.exists(baked_env):
        load_dotenv(baked_env)
        loaded = True
if not loaded:
    local_env = os.path.join(exe_dir, ".env")
    if os.path.exists(local_env):
        load_dotenv(local_env)
        loaded = True

SFTP_CONNECTION_STRING = os.getenv("SFTP_CONNECTION_STRING")
DEST_DIR = os.getenv("DEST_DIR", "./downloads")

if not SFTP_CONNECTION_STRING:
    print("❌ Missing SFTP_CONNECTION_STRING in .env")
    sys.exit(1)

parsed = urlparse(SFTP_CONNECTION_STRING)
if parsed.scheme != "sftp":
    print("❌ Invalid SFTP_CONNECTION_STRING (must start with sftp://)")
    sys.exit(1)

host = parsed.hostname
port = parsed.port or 22
username = parsed.username
password = parsed.password
remote_root = "/roms"

# --- Pygame Setup ---
pygame.init()
pygame.joystick.init()
joysticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
for j in joysticks:
    j.init()

# --- Unified Input Handling ---
PRIMARY_JOY_ID = None            # first joystick that sends an event
BUTTON_DEBOUNCE = 0.22           # seconds
_last_button_time = {}

# Some controllers use HAT for D-pad; we support both hat and button mapping
# Also provide a consistent directional repeat behavior
REPEAT_DELAY = 0.12              # seconds until first repeat (was 0.28)
REPEAT_RATE = 0.045              # seconds between repeats while held (was 0.08)

class DirectionRepeater:
    def __init__(self):
        self.held = None
        self.hold_start = 0.0
        self.last_repeat = 0.0

    def press(self, direction):
        now = time.time()
        # Start a new hold if different direction
        if self.held != direction:
            self.held = direction
            self.hold_start = now
            self.last_repeat = now
            return True  # trigger immediate move
        # If same direction pressed again, treat as debounced immediate move
        self.last_repeat = now
        return False

    def release(self, direction=None):
        if direction is None or self.held == direction:
            self.held = None

    def tick(self):
        if not self.held:
            return None
        now = time.time()
        # After initial delay, repeat at fixed rate
        if (now - self.hold_start) >= REPEAT_DELAY and (now - self.last_repeat) >= REPEAT_RATE:
            self.last_repeat = now
            return self.held
        return None

def _allow_joy(event, button=None):
    global PRIMARY_JOY_ID
    now = time.time()
    # Lock to the first joystick that sends any event
    if PRIMARY_JOY_ID is None and hasattr(event, "joy"):
        PRIMARY_JOY_ID = event.joy
    # Ignore events from other joysticks
    if hasattr(event, "joy") and event.joy != PRIMARY_JOY_ID:
        return False
    # Debounce by button id if provided
    if button is not None:
        last = _last_button_time.get(button, 0.0)
        if (now - last) < BUTTON_DEBOUNCE:
            return False
        _last_button_time[button] = now
    return True

SCREEN_W, SCREEN_H = pygame.display.Info().current_w, pygame.display.Info().current_h
screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.FULLSCREEN)
pygame.display.set_caption("ROM Search")

font = pygame.font.SysFont("DejaVu Sans", 32)
small_font = pygame.font.SysFont("DejaVu Sans", 22)

BG_COLOR = (20, 20, 30)
TEXT_COLOR = (230, 230, 240)
HIGHLIGHT = (100, 200, 255)
SCROLLBAR_COLOR = (60, 60, 80)
PROGRESS_BG = (50, 50, 70)
PROGRESS_FILL = (100, 200, 255)

clock = pygame.time.Clock()

# --- Controller IDs Dualsense ---

# Face Buttons
CROSS       = 0   # ×
CIRCLE      = 1   # ○
SQUARE      = 2   # ■
TRIANGLE    = 3   # ▲

# Bumpers / Triggers
L1          = 4
R1          = 5
L2          = 6
R2          = 7

# Stick Clicks
L3          = 8   # Press the left stick
R3          = 9   # Press the right stick

# System Buttons
OPTIONS     = 10
DPAD_UP     = 11
DPAD_DOWN   = 12
DPAD_LEFT   = 13
DPAD_RIGHT  = 14

AXIS_THRESHOLD = 0.5  # treat analog stick past this as a direction

# --- Globals ---
downloading = False
download_progress = 0
download_speed = 0
download_eta = 0
stop_download = False

# Multi-select state
multi_select_enabled = False
selected_results = set()

last_platform = None
last_query = None
search_results = []

# --- Notification System ---
class Notification:
    def __init__(self, message, notification_type="error"):
        self.message = message
        self.type = notification_type  # "error", "info", "success"
        self.created_at = time.time()
        self.lifetime = 10.0  # seconds

    def is_expired(self):
        return (time.time() - self.created_at) > self.lifetime

    def get_alpha(self):
        """Fade out in the last 2 seconds"""
        elapsed = time.time() - self.created_at
        if elapsed > self.lifetime - 2:
            return int(255 * (self.lifetime - elapsed) / 2)
        return 255

notifications = []
_notifications_lock = threading.Lock()

def show_notification(message, notification_type="error", duration=3):
    """Add a notification to be displayed in the UI for a limited time (thread-safe)"""
    with _notifications_lock:
        notification = Notification(message, notification_type)
        notifications.append(notification)

    # Keep the console message for debugging/logs
    prefix = "ERROR" if notification_type == "error" else "OK" if notification_type == "success" else "INFO"
    print(f"[{prefix}] {message}")

    # Remove notification after `duration` seconds
    def remove_notification():
        with _notifications_lock:
            if notification in notifications:
                notifications.remove(notification)

    threading.Timer(duration, remove_notification).start()

def draw_notifications():
    """Draw all active notifications in the top right corner (main-thread only)"""
    # Take a snapshot for drawing to avoid holding the lock while rendering
    with _notifications_lock:
        snapshot = list(notifications)

    margin = 20
    notification_width = 400
    notification_height = 80
    y_offset = margin

    for notification in snapshot:
        if notification.is_expired():
            continue

        # Choose a color based on type
        if notification.type == "error":
            bg_color = (180, 40, 40)
        elif notification.type == "success":
            bg_color = (40, 180, 80)
        else:  # info
            bg_color = (40, 120, 180)

        # Calculate position (top right)
        x = SCREEN_W - notification_width - margin
        y = y_offset

        # Create surface with alpha for fade effect
        notification_surface = pygame.Surface((notification_width, notification_height))
        notification_surface.set_alpha(notification.get_alpha())
        notification_surface.fill(bg_color)

        # Draw notification background
        screen.blit(notification_surface, (x, y))

        # Draw border
        pygame.draw.rect(screen, (255, 255, 255), (x, y, notification_width, notification_height), 2)

        # Word wrap the message
        words = notification.message.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = current_line + (" " if current_line else "") + word
            if small_font.size(test_line)[0] < notification_width - 20:  # full width minus padding
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)

        # Draw text lines
        for i, line in enumerate(lines[:3]):  # allow up to 3 lines
            text_surface = small_font.render(line, True, TEXT_COLOR)
            screen.blit(text_surface, (x + 10, y + 10 + i * 25))

        y_offset += notification_height + 10

    # Prune expired notifications once per frame under the lock
    with _notifications_lock:
        notifications[:] = [n for n in notifications if not n.is_expired()]

# Add a simple exception logger that uses datetime and traceback for diagnostics
def log_exception(context, exc):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {context}: {exc}")
    traceback.print_exc()

# --- SFTP ---
def connect_sftp():
    try:
        t = paramiko.Transport((host, port))
        # Prefer faster modern ciphers (often higher throughput, lower CPU)
        try:
            sec = t.get_security_options()
            sec.ciphers = [
                "chacha20-poly1305@openssh.com",
                "aes128-gcm@openssh.com",
                "aes256-gcm@openssh.com",
                "aes128-ctr",
                "aes256-ctr",
            ]
        except Exception:
            # Fallback silently if is not supported by the current Paramiko / OpenSSH combo
            pass
        t.connect(username=username, password=password)
        return paramiko.SFTPClient.from_transport(t), t
    except Exception as e:
        show_notification(f"SFTP connection failed: {e}", "error")
        log_exception("SFTP connection failed", e)
        return None, None

def list_platforms(sftp):
    try:
        items = sftp.listdir_attr(remote_root)
        # Robust dir check using st_mode instead of longname parsing
        return [i.filename for i in items if stat.S_ISDIR(i.st_mode)]
    except Exception as e:
        show_notification(f"Could not list /roms/: {e}", "error")
        log_exception("List platforms failed", e)
        return []

# Lightweight safe close helper
def safe_close_sftp(sftp, transport):
    try:
        if sftp:
            sftp.close()
    except Exception:
        pass
    try:
        if transport:
            transport.close()
    except Exception:
        pass

# Recursive remote walk with basic error handling and symlink skip
def sftp_walk(sftp, top):
    try:
        entries = sftp.listdir_attr(top)
    except Exception as e:
        show_notification(f"Cannot access {top}: {e}", "error")
        return

    dirs, files = [], []
    for entry in entries:
        mode = entry.st_mode
        name = entry.filename
        if stat.S_ISLNK(mode):
            # skip symlinks to avoid loops
            continue
        if stat.S_ISDIR(mode):
            dirs.append(name)
        else:
            files.append(name)

    yield top, dirs, files

    for d in dirs:
        new_top = f"{top.rstrip('/')}/{d}"
        yield from sftp_walk(sftp, new_top)

def search_remote(sftp, base_dir, query, limit=2000):
    q = (query or "").strip().lower()
    results = []
    if not q:
        return results
    count = 0
    for root, _dirs, files in sftp_walk(sftp, base_dir):
        for fname in files:
            if q in fname.lower():
                results.append(f"{root.rstrip('/')}/{fname}")
                count += 1
                if count >= limit:
                    return results
    return results

# --- Download ---
def download_file(sftp, remote_path, local_path):
    global downloading, download_progress, download_speed, download_eta, stop_download
    downloading = True
    stop_download = False
    download_progress = 0
    download_speed = 0
    download_eta = 0
    success = False
    size = 0
    read_bytes = 0
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        size = sftp.stat(remote_path).st_size
        with open(local_path, "wb") as f, sftp.open(remote_path, "rb") as remote_f:
            # Enable pipelining and prefetch to keep the wire busy
            try:
                # Allow multiple outstanding requests
                remote_f.set_pipelined(True)
            except Exception:
                pass
            try:
                # Ask Paramiko to prefetch the file (it will issue parallel SFTP read requests internally)
                remote_f.prefetch()
            except Exception:
                pass

            read_bytes = 0
            start_time = time.time()
            last_time = start_time
            last_bytes = 0

            # Larger read size for higher throughput (tune: 512*1024 to 4*1024*1024)
            block_size = 1024 * 1024  # 1 MiB

            while True:
                if stop_download:
                    show_notification("Download aborted by user", "info")
                    break
                data = remote_f.read(block_size)
                if not data:
                    break
                f.write(data)
                read_bytes += len(data)
                download_progress = read_bytes / max(1, size)

                now = time.time()
                elapsed = now - last_time
                if elapsed >= 0.5:
                    download_speed = (read_bytes - last_bytes) / elapsed / (1024 * 1024)
                    remaining_bytes = max(0, size - read_bytes)
                    download_eta = remaining_bytes / (max(download_speed, 1e-6) * 1024 * 1024)
                    last_bytes = read_bytes
                    last_time = now

        success = (not stop_download) and (size == 0 or read_bytes >= size)
        if success:
            show_notification(f"Download complete: {os.path.basename(local_path)}", "success")
        else:
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
                    show_notification("Removed partial file", "info")
            except Exception as rm_err:
                show_notification(f"Failed to remove partial file: {rm_err}", "error")
    except Exception as e:
        show_notification(f"Download failed: {e}", "error")
        log_exception(f"Download failed for {remote_path}", e)
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
                show_notification("Removed partial file after error", "info")
        except Exception as rm_err:
            show_notification(f"Failed to remove partial file: {rm_err}", "error")
    downloading = False


# --- UI ---
def draw_text_centered(text, y, highlight=False, color=None, font_obj=None, x=None):
    if font_obj is None:
        font_obj = font
    if color is None:
        color = HIGHLIGHT if highlight else TEXT_COLOR
    surface = font_obj.render(text, True, color)
    rect = surface.get_rect(center=((x if x else SCREEN_W // 2), y))
    screen.blit(surface, rect)

def format_eta(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, min = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {min}m"
    days, hr = divmod(hours, 24)
    return f"{days}d {hr}h"

def draw_footer(message):
    footer_y = SCREEN_H - 50
    surface = small_font.render(message, True, TEXT_COLOR)
    rect = surface.get_rect(center=(SCREEN_W // 2, footer_y))
    screen.blit(surface, rect)

# --- Menu ---
def draw_menu(title, options, selected_index, scroll_offset=0, footer_msg=None, selected_set=None, multi_enabled=False):
    screen.fill(BG_COLOR)
    draw_text_centered(title, 100, font_obj=font)
    visible_count = (SCREEN_H - 200) // 50
    start = scroll_offset
    end = min(len(options), scroll_offset + visible_count)
    for idx, i in enumerate(range(start, end)):
        y = 200 + idx * 50
        label = options[i]

        # Prefix with checkmark when multi-select is enabled and item is selected
        if multi_enabled and selected_set and label in selected_set and label != "< Back":
            display_text = f"[x] {label}"
        else:
            display_text = label

        if i == selected_index:
            pygame.draw.rect(screen, HIGHLIGHT, (100, y - 20, SCREEN_W - 200, 40))
            draw_text_centered(display_text, y, color=BG_COLOR)
        else:
            draw_text_centered(display_text, y)

    # Static-length scrollbar that moves per item
    if len(options) > 1:
        track_x, track_y = SCREEN_W - 40, 200
        track_w, track_h = 20, SCREEN_H - 400
        pygame.draw.rect(screen, SCROLLBAR_COLOR, (track_x, track_y, track_w, track_h))

        # Static bar height (25% of track height, clamped)
        bar_height = max(60, min(200, int(track_h * 0.25)))
        usable = max(1, len(options) - 1)
        step = (track_h - bar_height) / usable
        bar_y = track_y + step * min(max(0, selected_index), len(options) - 1)

        pygame.draw.rect(screen, HIGHLIGHT, (track_x, int(bar_y), track_w, int(bar_height)))

    if footer_msg:
        draw_footer(footer_msg)

    # Draw notifications on top of everything
    draw_notifications()

    pygame.display.flip()

def menu_select(title, options, selected_index=0, scroll_offset=0, allow_back=True, *, allow_multi_controls=False, selected_set=None, multi_enabled=None):
    global multi_select_enabled, selected_results, stop_download
    visible_count = (SCREEN_H - 200) // 50
    keys_down = set()

    # Resolve current multi-select state and selected set
    if multi_enabled is None:
        multi_enabled = multi_select_enabled
    if selected_set is None:
        selected_set = selected_results

    repeater = DirectionRepeater()
    unknown_button_hint_shown = False

    while True:
        if selected_index < scroll_offset:
            scroll_offset = selected_index
        elif selected_index >= scroll_offset + visible_count:
            scroll_offset = selected_index - visible_count + 1

        # Dynamic footer with multi status
        footer_msg = "D-pad = Navigate | X = Select"
        if allow_back:
            footer_msg += " | O = Back"
        if allow_multi_controls:
            footer_msg += " | ■ = Toggle Multi-Select | ▲ = Start Multi-Select Download"
            status = "ON" if multi_enabled else "OFF"
            if multi_enabled:
                footer_msg += f" | Multi: {status} ({len(selected_set)} selected)"
            else:
                footer_msg += f" | Multi: {status}"

        # Draw with selection markers when multi-select is active
        draw_menu(
            title,
            options,
            selected_index,
            scroll_offset,
            footer_msg,
            selected_set=selected_set if allow_multi_controls and multi_enabled else None,
            multi_enabled=allow_multi_controls and multi_enabled
        )

        for event in pygame.event.get():
            if event.type == QUIT:
                stop_download = True
                handle_exit()

            # Keyboard
            elif event.type == KEYDOWN:
                keys_down.add(event.key)
                if event.key in (K_RETURN, K_KP_ENTER):
                    label = options[selected_index]
                    if allow_multi_controls and multi_enabled:
                        if label == "< Back":
                            return "< Back"
                        if label in selected_set:
                            selected_set.discard(label)
                            show_notification(f"Unselected: {label}", "info")
                        else:
                            selected_set.add(label)
                            show_notification(f"Selected: {label}", "info")
                        continue
                    return options[selected_index]

                elif allow_back and event.key == K_ESCAPE:
                    return "__BACK__"

                elif event.key in (K_UP, K_w):
                    if repeater.press("up"):
                        selected_index = (selected_index - 1) % len(options)
                elif event.key in (K_DOWN, K_s):
                    if repeater.press("down"):
                        selected_index = (selected_index + 1) % len(options)

                # Optional keyboard helpers
                elif allow_multi_controls and event.key == K_l:
                    multi_select_enabled = not multi_select_enabled
                    multi_enabled = multi_select_enabled
                    if not multi_enabled and selected_set:
                        selected_set.clear()
                        show_notification("Multi Select OFF — selections cleared", "info")
                    else:
                        show_notification("Multi Select ON — choose entries, Press ▲ to Start Download", "info")

                elif allow_multi_controls and event.key == K_r:
                    if multi_enabled:
                        if not selected_set:
                            show_notification("No entries selected", "info")
                            continue
                        return "__MULTI_START__"

            elif event.type == KEYUP:
                keys_down.discard(event.key)
                if event.key in (K_UP, K_w) and repeater.held == "up":
                    repeater.release("up")
                elif event.key in (K_DOWN, K_s) and repeater.held == "down":
                    repeater.release("down")

            # Gamepad
            elif event.type == JOYBUTTONDOWN:
                if not _allow_joy(event, event.button):
                    continue

                handled = False

                if allow_multi_controls and not handled and event.button == SQUARE:
                    multi_select_enabled = not multi_select_enabled
                    multi_enabled = multi_select_enabled
                    if not multi_enabled and selected_set:
                        selected_set.clear()
                        show_notification("Multi Select OFF — selections cleared", "info")
                    else:
                        show_notification("Multi Select ON — choose entries, press ▲ to start", "info")
                    handled = True

                # Start Download: Triangle
                if allow_multi_controls and not handled and event.button == TRIANGLE:
                    if multi_enabled:
                        if not selected_set:
                            show_notification("No entries selected", "info")
                        else:
                            return "__MULTI_START__"
                    handled = True

                if handled:
                    continue

                # Confirm / Back
                if event.button == CROSS:  # Confirm (X)
                    label = options[selected_index]
                    if allow_multi_controls and multi_enabled:
                        if label == "< Back":
                            return "< Back"
                        if label in selected_set:
                            selected_set.discard(label)
                            show_notification(f"Unselected: {label}", "info")
                        else:
                            selected_set.add(label)
                            show_notification(f"Selected: {label}", "info")
                        continue
                    return options[selected_index]

                elif allow_back and event.button == CIRCLE:  # Back (O)
                    return "__BACK__"

                # D-pad via mapped buttons
                elif event.button == DPAD_UP:
                    if repeater.press("up"):
                        selected_index = (selected_index - 1) % len(options)
                elif event.button == DPAD_DOWN:
                    if repeater.press("down"):
                        selected_index = (selected_index + 1) % len(options)
                else:
                    # One-time hint to help map your controller
                    if allow_multi_controls and not unknown_button_hint_shown:
                        unknown_button_hint_shown = True
                        show_notification(f"Button: {event.button} is not mapped. Read the Tooltip!", "error")

            elif event.type == JOYBUTTONUP:
                if hasattr(event, "button"):
                    if event.button == DPAD_UP and repeater.held == "up":
                        repeater.release("up")
                    elif event.button == DPAD_DOWN and repeater.held == "down":
                        repeater.release("down")

            elif event.type == JOYHATMOTION:
                if not _allow_joy(event):
                    continue
                # event.value is (x, y); y: 1 up, -1 down
                hat_x, hat_y = event.value
                if hat_y == 1:
                    if repeater.press("up"):
                        selected_index = (selected_index - 1) % len(options)
                elif hat_y == -1:
                    if repeater.press("down"):
                        selected_index = (selected_index + 1) % len(options)
                else:
                    if repeater.held in ("up", "down"):
                        repeater.release()

            elif event.type == JOYAXISMOTION:
                if not _allow_joy(event):
                    continue
                # axis 1: up/down; negative is up, positive is down
                if event.axis == 1:
                    if event.value <= -AXIS_THRESHOLD:
                        if repeater.press("up"):
                            selected_index = (selected_index - 1) % len(options)
                    elif event.value >= AXIS_THRESHOLD:
                        if repeater.press("down"):
                            selected_index = (selected_index + 1) % len(options)
                    else:
                        if repeater.held in ("up", "down"):
                            repeater.release()

        # Handle held-direction repeating
        rep = repeater.tick()
        if rep == "up":
            selected_index = (selected_index - 1) % len(options)
        elif rep == "down":
            selected_index = (selected_index + 1) % len(options)

        clock.tick(60)


# --- Virtual Keyboard ---
VIRTUAL_KEYS = [
    list("ABCDEFGHIJ"),
    list("KLMNOPQRST"),
    list("UVWXYZ0123"),
    list("456789-_.<"),
    ["SPACE", "ENTER", "BACK"]
]

def virtual_keyboard_input(prompt):
    input_text = ""
    selected_row, selected_col = 0, 0
    footer_msg = "D-pad = Move | X = Select | O = Back"

    # Separate repeaters for horizontal and vertical movement
    v_rep = DirectionRepeater()
    h_rep = DirectionRepeater()

    def move_cursor(direction):
        nonlocal selected_row, selected_col
        if direction == "up":
            selected_row = max(selected_row - 1, 0)
            selected_col = min(selected_col, len(VIRTUAL_KEYS[selected_row]) - 1)
        elif direction == "down":
            selected_row = min(selected_row + 1, len(VIRTUAL_KEYS) - 1)
            selected_col = min(selected_col, len(VIRTUAL_KEYS[selected_row]) - 1)
        elif direction == "left":
            selected_col = max(selected_col - 1, 0)
        elif direction == "right":
            selected_col = min(selected_col + 1, len(VIRTUAL_KEYS[selected_row]) - 1)

    while True:
        screen.fill(BG_COLOR)
        draw_text_centered(prompt, 50)
        draw_text_centered("Input: " + input_text + "_", 120, color=HIGHLIGHT)
        start_y = 200
        for r, row in enumerate(VIRTUAL_KEYS):
            y = start_y + r * 80
            x_spacing = SCREEN_W // (len(row) + 1)
            for c, key in enumerate(row):
                x = (c + 1) * x_spacing
                is_selected = (r == selected_row and c == selected_col)
                if is_selected:
                    pygame.draw.rect(screen, HIGHLIGHT, (x - 30, y - 30, 60, 60))
                    text_color = BG_COLOR
                else:
                    text_color = TEXT_COLOR
                surface = font.render(key, True, text_color)
                rect = surface.get_rect(center=(x, y))
                screen.blit(surface, rect)
        draw_footer(footer_msg)
        # Ensure notifications are visible on the keyboard screen
        draw_notifications()
        pygame.display.flip()

        for event in pygame.event.get():
            if event.type == QUIT:
                stop_download = True
                handle_exit()

            # Keyboard
            elif event.type == KEYDOWN:
                if event.key in (K_RETURN, K_KP_ENTER):
                    return input_text.strip()
                elif event.key == K_BACKSPACE:
                    input_text = input_text[:-1]
                elif event.key in (K_UP, K_w):
                    if v_rep.press("up"):
                        move_cursor("up")
                elif event.key in (K_DOWN, K_s):
                    if v_rep.press("down"):
                        move_cursor("down")
                elif event.key in (K_LEFT, K_a):
                    if h_rep.press("left"):
                        move_cursor("left")
                elif event.key in (K_RIGHT, K_d):
                    if h_rep.press("right"):
                        move_cursor("right")

            elif event.type == KEYUP:
                if event.key in (K_UP, K_w) and v_rep.held == "up":
                    v_rep.release("up")
                elif event.key in (K_DOWN, K_s) and v_rep.held == "down":
                    v_rep.release("down")
                elif event.key in (K_LEFT, K_a) and h_rep.held == "left":
                    h_rep.release("left")
                elif event.key in (K_RIGHT, K_d) and h_rep.held == "right":
                    h_rep.release("right")

            # Gamepad
            elif event.type == JOYBUTTONDOWN:
                if not _allow_joy(event, event.button):
                    continue
                if event.button == 0:  # X / Cross -> select key
                    key = VIRTUAL_KEYS[selected_row][selected_col]
                    if key == "ENTER":
                        return input_text.strip()
                    elif key == "BACK":
                        input_text = input_text[:-1]
                    elif key == "SPACE":
                        input_text += " "
                    else:
                        input_text += key
                elif event.button == 1:  # O / Circle -> back to platform select
                    return "__BACK__"
                elif event.button == DPAD_UP:
                    if v_rep.press("up"):
                        move_cursor("up")
                elif event.button == DPAD_DOWN:
                    if v_rep.press("down"):
                        move_cursor("down")
                elif event.button == DPAD_LEFT:
                    if h_rep.press("left"):
                        move_cursor("left")
                elif event.button == DPAD_RIGHT:
                    if h_rep.press("right"):
                        move_cursor("right")

            elif event.type == JOYBUTTONUP:
                if hasattr(event, "button"):
                    if event.button == DPAD_UP and v_rep.held == "up":
                        v_rep.release("up")
                    elif event.button == DPAD_DOWN and v_rep.held == "down":
                        v_rep.release("down")
                    elif event.button == DPAD_LEFT and h_rep.held == "left":
                        h_rep.release("left")
                    elif event.button == DPAD_RIGHT and h_rep.held == "right":
                        h_rep.release("right")

            elif event.type == JOYHATMOTION:
                if not _allow_joy(event):
                    continue
                hat_x, hat_y = event.value
                # Vertical
                if hat_y == 1:
                    if v_rep.press("up"):
                        move_cursor("up")
                elif hat_y == -1:
                    if v_rep.press("down"):
                        move_cursor("down")
                else:
                    if v_rep.held in ("up", "down"):
                        v_rep.release()
                # Horizontal
                if hat_x == -1:
                    if h_rep.press("left"):
                        move_cursor("left")
                elif hat_x == 1:
                    if h_rep.press("right"):
                        move_cursor("right")
                else:
                    if h_rep.held in ("left", "right"):
                        h_rep.release()
            elif event.type == JOYAXISMOTION:
                if not _allow_joy(event):
                    continue
                # axis 0: left/right, axis 1: up/down
                if event.axis == 1:
                    if event.value <= -AXIS_THRESHOLD:
                        if v_rep.press("up"):
                            move_cursor("up")
                    elif event.value >= AXIS_THRESHOLD:
                        if v_rep.press("down"):
                            move_cursor("down")
                    else:
                        if v_rep.held in ("up", "down"):
                            v_rep.release()
                elif event.axis == 0:
                    if event.value <= -AXIS_THRESHOLD:
                        if h_rep.press("left"):
                            move_cursor("left")
                    elif event.value >= AXIS_THRESHOLD:
                        if h_rep.press("right"):
                            move_cursor("right")
                    else:
                        if h_rep.held in ("left", "right"):
                            h_rep.release()

        # Handle held-direction repeating
        rep_v = v_rep.tick()
        if rep_v:
            move_cursor(rep_v)
        rep_h = h_rep.tick()
        if rep_h:
            move_cursor(rep_h)

        clock.tick(60)

# --- Download Screen ---
def download_screen(filename, index=None, total=None):
    global downloading, download_progress, download_speed, download_eta, stop_download
    while downloading:
        screen.fill(BG_COLOR)
        title = f"Downloading: {filename}"
        if index is not None and total is not None:
            title = f"Downloading {index}/{total}: {filename}"
        draw_text_centered(title, 100)
        pygame.draw.rect(screen, PROGRESS_BG, (100, 300, SCREEN_W - 200, 50))
        pygame.draw.rect(screen, PROGRESS_FILL, (100, 300, int((SCREEN_W - 200) * download_progress), 50))
        draw_text_centered(
            f"{download_progress * 100:.1f}% | {download_speed:.2f} MB/s | ETA {format_eta(download_eta)}", 400)
        draw_footer("Press O to cancel download")

        # Draw notifications on top of everything
        draw_notifications()

        pygame.display.flip()
        for event in pygame.event.get():
            if event.type == QUIT:
                stop_download = True
                handle_exit()
            elif event.type == KEYDOWN and event.key == K_ESCAPE:
                stop_download = True
                return
            elif event.type == JOYBUTTONDOWN and event.button == 1:
                stop_download = True
                return
        clock.tick(30)


# --- Helpers ---
def handle_exit():
    pygame.quit()
    print("🔒 Exiting...")
    sys.exit(0)

# --- Main ---
def search_and_download():
    global last_platform, last_query, search_results, multi_select_enabled, selected_results, stop_download
    sftp, transport = connect_sftp()
    if not sftp:
        print("❌ Failed to connect to SFTP.")
        handle_exit()

    # Used when creating local destination paths
    os.makedirs(DEST_DIR, exist_ok=True)

    # Persisted across "Choose Another File"
    results = None
    display_items = None
    label_to_remote = None
    remote_base = None

    while True:
        # 1) Platform select (only when not already chosen)
        if not last_platform:
            # When changing platform, reset cached search state
            results = None
            display_items = None
            label_to_remote = None
            remote_base = None
            selected_results.clear()
            multi_select_enabled = False

            platforms = list_platforms(sftp)
            if not platforms:
                print("⚠️ No platforms found on SFTP.")
                safe_close_sftp(sftp, transport)
                handle_exit()

            choice = menu_select("Select Platform", platforms, allow_back=True)
            if choice == "__BACK__":
                confirm = menu_select(
                    "Are you sure you want to exit the Downloader?",
                    ["No", "Yes"], allow_back=False
                )
                if confirm == "Yes":
                    safe_close_sftp(sftp, transport)
                    handle_exit()
                else:
                    # Go back to platform select
                    continue
            last_platform = choice

        # 2) Ask for search query unless we already have results to reuse
        if not results:
            query = virtual_keyboard_input(f"Search in {last_platform} (ENTER to confirm)")
            if query == "__BACK__":
                # Reset platform and go back to platform selection
                last_platform = None
                continue
            last_query = query

            # 3) Execute search
            remote_base = f"{remote_root.rstrip('/')}/{last_platform}"
            try:
                results = search_remote(sftp, remote_base, query, limit=2000)
            except Exception as e:
                log_exception("Search failed", e)
                results = []

            # 4) If no results, offer next steps
            if not results:
                show_notification("No results found.", "info")
                action = menu_select("No results. What next?", ["New Search", "Change Platform", "Exit"], allow_back=False)
                if action == "Exit":
                    safe_close_sftp(sftp, transport)
                    handle_exit()
                elif action == "Change Platform":
                    last_platform = None
                    continue
                else:
                    # New Search on same platform
                    results = None
                    continue

            # Build mapped display list once; persist for "Choose Another File"
            display_items = [p.replace(remote_base + "/", "") for p in results]
            label_to_remote = {label: full for label, full in zip(display_items, results)}
            display_items.append("< Back")

        # 5) Results loop (reused for "Choose Another File")
        while True:
            sel = menu_select(
                "Results",
                display_items,
                allow_back=True,
                allow_multi_controls=True,
                selected_set=selected_results,
                multi_enabled=multi_select_enabled
            )

            # Handle backing out to enter a new search
            if sel == "__BACK__" or sel == "< Back":
                # Reset cached results so we re-prompt for a new query
                results = None
                display_items = None
                label_to_remote = None
                selected_results.clear()
                multi_select_enabled = False
                break  # back to query input

            # Handle Multi-Select batch start
            if sel == "__MULTI_START__":
                if not selected_results:
                    show_notification("No entries selected", "info")
                    continue
                total = len(selected_results)
                i = 0
                # Download each selected label in the preserved order as they appear in display_items
                for label in [lbl for lbl in display_items if lbl in selected_results]:
                    if label == "< Back":
                        continue
                    remote_path = label_to_remote.get(label)
                    if not remote_path:
                        show_notification(f"Missing remote path for {label}", "error")
                        continue
                    rel_name = label.replace("/", os.sep)
                    local_dir = os.path.join(DEST_DIR, last_platform)
                    local_path = os.path.join(local_dir, rel_name)

                    try:
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        i += 1
                        t = threading.Thread(target=download_file, args=(sftp, remote_path, local_path), daemon=True)
                        t.start()
                        download_screen(os.path.basename(rel_name), index=i, total=total)
                        t.join(timeout=0.1)
                        if stop_download:
                            # Reset stop flag so future downloads can proceed
                            stop_download = False
                            break
                    except Exception as e:
                        log_exception("Failed to start batch download item", e)
                        show_notification(f"Failed to start download: {e}", "error")

                # Clear selections after batch completes or aborts
                selected_results.clear()
                multi_select_enabled = False
                show_notification("Multi Select OFF — selections cleared", "info")

                # After batch, ask next action but KEEP the same results available
                next_action = menu_select(
                    "What next?",
                    ["Choose Another File", "New Search", "Change Platform", "Exit"],
                    allow_back=False
                )
                if next_action == "Exit":
                    safe_close_sftp(sftp, transport)
                    handle_exit()
                elif next_action == "Change Platform":
                    last_platform = None
                    # Also clear cached results because platform will change
                    results = None
                    display_items = None
                    label_to_remote = None
                    break  # to platform selection
                elif next_action == "New Search":
                    # Clear cached results to prompt a new search in same platform
                    results = None
                    display_items = None
                    label_to_remote = None
                    break  # to query input
                else:
                    # Choose Another File: stay in the same results loop (reuse results)
                    continue

            # Single-selection path (when Multi-Select is OFF, or user pressed A/Enter when OFF)
            # Determine chosen remote path safely
            try:
                if sel not in label_to_remote:
                    show_notification("Invalid selection.", "error")
                    continue
                remote_path = label_to_remote[sel]
            except Exception:
                show_notification("Invalid selection.", "error")
                continue

            # 6) Compute local destination path
            rel_name = sel.replace("/", os.sep)
            local_dir = os.path.join(DEST_DIR, last_platform)
            local_path = os.path.join(local_dir, rel_name)

            # 7) Confirm and download
            confirm = menu_select(f"Download '{rel_name}'?", ["No", "Yes"], allow_back=False)
            if confirm == "Yes":
                try:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    t = threading.Thread(target=download_file, args=(sftp, remote_path, local_path), daemon=True)
                    t.start()
                    # Show with single-file title (no index/total)
                    download_screen(os.path.basename(rel_name))
                    t.join(timeout=0.1)
                except Exception as e:
                    log_exception("Failed to start download", e)
                    show_notification(f"Failed to start download: {e}", "error")

            # 8) After single download (or cancel/No), ask next action — KEEP results if "Choose Another File"
            next_action = menu_select(
                "What next?",
                ["Choose Another File", "New Search", "Change Platform", "Exit"],
                allow_back=False
            )
            if next_action == "Exit":
                safe_close_sftp(sftp, transport)
                handle_exit()
            elif next_action == "Change Platform":
                last_platform = None
                # Also clear cached results because platform will change
                results = None
                display_items = None
                label_to_remote = None
                break  # to platform selection
            elif next_action == "New Search":
                # Clear cached results to prompt a new search in same platform
                results = None
                display_items = None
                label_to_remote = None
                break  # to query input
            else:
                # Choose Another File: loop again and reuse exact same results
                continue

def main():
    try:
        search_and_download()
    except SystemExit:
        # allow sys.exit to work normally
        raise
    except Exception as e:
        # Ensure traceback and datetime are used for any unexpected crash
        log_exception("Fatal error in main loop", e)
    finally:
        try:
            pygame.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()