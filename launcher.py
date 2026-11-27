"""PyInstaller entry for the main app."""
import sys

from agentsii.main import main

if __name__ == "__main__":
    sys.exit(main())
