"""PyInstaller entry for the main app."""
import os
import sys

# Any copy of a PyInstaller onefile app we start (tray service, auth helper)
# must unpack its own runtime instead of reusing ours, otherwise this process
# cannot exit cleanly. See PyInstaller docs: PYINSTALLER_RESET_ENVIRONMENT.
if getattr(sys, "frozen", False):
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

from agentsii.main import main

if __name__ == "__main__":
    sys.exit(main())
