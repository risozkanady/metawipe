#!/usr/bin/env bash
# Builds a single-file metawipe executable for the current OS into dist/ (needs: pip install '.[tui]' pyinstaller).
# Textual and Rich load widgets/unicode tables lazily, so PyInstaller cannot see them: collect them whole.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m PyInstaller --noconfirm --clean --onefile --console --name metawipe \
  --distpath dist --workpath build/pyinstaller --specpath build/pyinstaller \
  --collect-submodules textual --collect-data textual --collect-submodules rich \
  --collect-submodules metawipe --copy-metadata metawipe \
  packaging/entry.py
