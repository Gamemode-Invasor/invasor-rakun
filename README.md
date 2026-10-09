# invasor-rakun

An [Invasor](../invasor) module for **Rakun**, the headless service that logs in to
Epic Games, GOG, Amazon Games and Zoom Platform, installs their games and adds them to Steam.

## What it does

Three tabs:

- **Service**, top to bottom:
  - an **Installation** section: whether Rakun is installed in `~/.local/opt/rakun/` and its version
    against the latest published release; a **Download** button installs it and, when the version differs,
    **Update** installs the latest one (with the project's `install.sh`);
  - a green/red indicator: green when Rakun answers on `127.0.0.1:<port>` (checked every 3 s);
  - the **Start Rakun** slider, on while the `rakun.service` user service is running. Switching it on runs
    `~/.local/opt/rakun/rakunctl start --port <port> --web <local|network>`; off runs `~/.local/opt/rakun/rakunctl stop`;
  - the port (17999 by default), changed with − / +. While the slider is on it is read-only. It is saved in
    the module's own data (`~/.config/invasor/modules/rakun/data.json`);
  - **Reachable from the network**, a slider below the port (same rules: only while Rakun is stopped): off = `local`
    (default), on = `network`, passed to `rakunctl start` as `--web local|network`, saved with the port;
  - **Open in the browser**, which opens Rakun's web interface on that port in Steam's own browser (a
    `steam://openurl` link, so it works in Game Mode).
- **Logs**: Rakun's events, formatted (`rakunctl events` is read in the background while Rakun runs): queue
  changes, game status and download progress, with the last 200 lines and a refresh every 3 s, plus a **Clear**
  button. When Rakun stops the list ends with `-- Rakun stopped --`.
- **Credits**: mentions Rakun and links to its repository,
  <https://github.com/FranjeGueje/rakun>.


Rakun also shows a Steam notification, the way the Noty module does, when a download finishes or a game is
uninstalled, even if the Logs tab is closed (the module keeps `rakunctl events` open while Rakun runs). Progress
is not notified, and there are no settings for it.

When an installation ends it also asks Steam to show the grid images Rakun downloaded for the game (capsule, hero,
logo and wide capsule), the way the Artwork module does after applying one, so they appear without restarting
Steam. The events carry no Steam app id, so it is read from Rakun's `~/.config/rakun/steam_shortcuts.json`, and the
module waits (up to 3 minutes) for Rakun to register the game and write the images.

## Requirements

- [Rakun](https://github.com/FranjeGueje/rakun) running on the console.
- Invasor 0.1.3 or newer.
- Python 3.9+ (standard library only).

## How it works

```
rakun/
  module.json   metadata
  backend.py    open_web, install and update, the rakun switch (rakunctl) and the saved port and web access: the methods the UI calls
  session.py    the graphical session's variables, for xdg-open
  grids.py      finds the images Rakun left in Steam's grid folder
  logs.py       reads `rakunctl events` in a thread and formats the lines
  ui.ts         Service, Logs and Credits tabs
  tests/        unittest
```

## Building

```bash
python3 ../invasor/tools/pack_module.py rakun     # typecheck, build, tests -> rakun-<version>.zip
python3 ../invasor/tools/install_module.py rakun-0.1.0.zip
```

or ⚙ Settings › Install module on the console.

## License

GPL-3.0-or-later.
