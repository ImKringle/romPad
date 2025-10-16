# ROMpad
Controller-first ROM browser and downloader with a living-room friendly UI. Browse platforms, search, and fetch ROMs over SFTP using only a gamepad! Includes multi-select downloads, a virtual on-screen keyboard, and real-time progress with ETA.

### Demo:

---
![Demo](assets/gif.gif)

---

## Highlights

- Controller-native UX (DualSense-style layout via Pygame)
- Search across platform folders on a remote SFTP server
- Multi-select and batch downloads
- Fullscreen, simple list UI with notifications
- Virtual keyboard for text input from the couch
- Per-file progress, MB/s speed, and ETA
- Safe cancellation and partial-file cleanup
- Remembers your last platform and query during a session

## Requirements

- Python 3.12+
- An SFTP server exposing a ROMs directory (see “Remote layout”)
- Gamepad supported by Pygame (keyboard supported as fallback)

Install dependencies:
```bash
pip install -r requirements.txt
```

## Quick Start

* Create a .env file in the project folder:
```
SFTP_CONNECTION_STRING=sftp://username:password@hostname:22
DEST_DIR=./downloads
```

* Run:
```bash
python main.py
```
Use your controller or keyboard to select a platform, search, and download.

## Using the prebuilt .exe (no Python required)

If you downloaded the precompiled executable from Releases:
- Place romPad.exe anywhere you like (e.g., your Desktop or a games tools folder).
- In the same folder as romPad.exe, create a .env file with:
  ```
  SFTP_CONNECTION_STRING=sftp://username:password@hostname:22
  DEST_DIR=./downloads
  ```
  Notes:
  - The connection string must start with sftp:// and include your credentials and host. Port is optional (defaults to 22).
  - DEST_DIR can be absolute (e.g., D:\ROMs\Downloads) or relative to the EXE’s folder. The folder will be created if it doesn’t exist.

Running:
- Double-click romPad.exe to launch the fullscreen UI.
- Use your controller or keyboard per the Controls section.

Tips for Windows:
- SmartScreen may warn on first run. Click “More info” → “Run anyway.”
  - Portable: No installer or registry use. To update, replace romPad.exe and keep your .env.

> Optional: bake the .env into the EXE during build (for distribution). If you do that, you don’t need to ship a separate .env next to the EXE.

## Remote layout

- The app expects platforms under /roms on the SFTP server:
  - /roms/NES
  - /roms/SNES
  - /roms/PSX
  - etc.
- The app expects a directory for each platform to be readable by the SFTP user.
- Search is performed within the chosen platform directory.

## Controls

Gamepad (DualSense-style mapping via Pygame):
- D-pad: Navigate lists and virtual keyboard
- Cross (X): Confirm / select
- Circle (O): Back / cancel
- Square (■): Toggle Multi-Select mode
- Triangle (▲): Start Multi-Select download

Keyboard:
- Up/Down or W/S: Navigate
- Left/Right or A/D: Move on virtual keyboard
- Enter: Confirm
- Esc: Back / cancel
- L: Toggle Multi-Select mode
- R: Start Multi-Select download

Tips:
- The first controller that sends input becomes the primary controller for the session.
- D-pad and analog stick navigation both work; held directions auto-repeat for quick scrolling.

## Download behavior

- Files are saved under DEST_DIR/<Platform>/...
- Per-file download screen shows percentage, speed (MB/s), and ETA
- Cancelling a download removes any partial file
- Batch mode downloads selected items in order, then lets you continue browsing the same results

## Packaging (optional)

PyInstaller is included if you want to build a standalone executable:

```bash
pyinstaller --onefile --noconsole main.py
```
Optionally, add `--add-data "<Path to the source>.env;."` to your command to bake the .env into the executable.

Adjust flags to your needs and platform.

## Troubleshooting

- Controller is not responding:
  - Ensure it’s recognized by your OS and by Pygame
  - Press any button once to “claim” the controller as primary
- “Missing SFTP_CONNECTION_STRING”:
  - Verify .env is in the same folder as the executable/script
  - Ensure the string starts with sftp://
- No platforms listed:
  - Verify your SFTP server has /roms and the platform directories are readable
- romPad.exe does not open:
  - Ensure the .env is baked into the executable or placed in the same folder as the EXE

## Legal

Only download and use ROMs you legally own and are permitted to copy in your jurisdiction. The authors are not responsible for misuse.

## License

This project is licensed under the MIT License. See the LICENSE file for details.
