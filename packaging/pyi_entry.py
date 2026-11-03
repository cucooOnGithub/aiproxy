"""PyInstaller entry point.

PyInstaller freezes a script, not `python -m aiproxy`, so this thin wrapper
just calls the real CLI. The frozen executable behaves like the `aiproxy`
console script: `aiproxy serve|purge|genkey|doctor|config ...`.
"""

import sys

from aiproxy.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
