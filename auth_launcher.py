"""PyInstaller entry for agentsii-auth.exe (Codex token helper)."""
import sys

from agentsii.auth_helper import main

if __name__ == "__main__":
    sys.exit(main())
