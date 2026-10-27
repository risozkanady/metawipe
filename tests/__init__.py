import atexit
import os
import shutil
import tempfile

# the TUI reads and writes its settings there: tests must never see or change the user's own
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="mw-test-config-")
atexit.register(shutil.rmtree, os.environ["XDG_CONFIG_HOME"], True)
