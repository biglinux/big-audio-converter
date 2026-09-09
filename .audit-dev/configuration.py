"""Preserve preferences while making writes atomic and schema-aware."""

from pathlib import Path
from editing import ROOT, method, source_method


def apply():
    path = ROOT / "app/utils/config.py"
    text = path.read_text()
    text = text.replace("from threading import Timer", "from threading import Timer, RLock")
    if "from .config_store import" not in text:
        text = text.replace("import gettext\n", "import gettext\nfrom .config_store import ConfigStore, normalize\n")
    path.write_text(text)
    init = source_method(path, "AppConfig", "__init__")
    init = init.replace("def __init__(self):", "def __init__(self, config_dir=None):")
    first = init.index('    self.config_dir =')
    last = init.index('    # Ensure config directory', first)
    init = init[:first] + '''    root = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    if not os.path.isabs(root):
        root = str(Path.home() / ".config")
    self.config_dir = os.fspath(config_dir) if config_dir is not None else os.path.join(root, "audio-converter")
    self._lock = RLock()
    self._io_lock = RLock()
    self._closed = False

''' + init[last:]
    method(path, "AppConfig", "__init__", init)
    method(path, "AppConfig", "load_config", '''
    def load_config(self):
        try:
            return ConfigStore(self.config_file, self.defaults).read()
        except OSError as error:
            logger.warning("Preferences could not be read: %s", error)
            return self.defaults.copy()
    ''')
    method(path, "AppConfig", "save_config", '''
    def save_config(self, config=None):
        """Merge only changed keys and retain them if publication fails."""
        with self._io_lock:
            with self._lock:
                updates = dict(config) if config is not None else {key: self.config[key] for key in self.modified_keys}
            if not updates:
                return True
            try:
                saved = ConfigStore(self.config_file, self.defaults).save(updates)
            except (OSError, ValueError, TypeError) as error:
                logger.warning("Preferences could not be saved: %s", error)
                return False
            with self._lock:
                for key, value in updates.items():
                    if self.config.get(key) == value:
                        self.modified_keys.discard(key)
                for key, value in saved.items():
                    if key not in self.modified_keys:
                        self.config[key] = value
            return True
    ''')
    method(path, "AppConfig", "get", '''
    def get(self, key, default=None):
        with self._lock:
            return self.config.get(key, default)
    ''')
    method(path, "AppConfig", "set", '''
    def set(self, key, value):
        with self._lock:
            if self._closed:
                return
            self.config[key] = normalize({key: value}, self.defaults).get(key, self.defaults.get(key))
            self.modified_keys.add(key)
            self._schedule_save()
    ''')
    method(path, "AppConfig", "_schedule_save", '''
    def _schedule_save(self):
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            if self._closed:
                return
            self._save_timer = Timer(self._save_delay, self.save_config)
            self._save_timer.daemon = True
            self._save_timer.start()
    ''')
    method(path, "AppConfig", "flush", '''
    def flush(self):
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        return self.save_config()
    ''')
    method(path, "AppConfig", "close", '''
    def close(self):
        """Stop scheduling new writes and finish the final merge."""
        with self._lock:
            self._closed = True
        return self.flush()
    ''')
    tests = Path("tests/test_config.py")
    text = tests.read_text()
    first = text.index('@pytest.fixture')
    last = text.index('\n\nclass TestAppConfigGetSet', first)
    text = text[:first] + '''@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(config_dir=tmp_path / "config")
    cfg._save_delay = 0.01
    yield cfg
    cfg.close()
''' + text[last:]
    tests.write_text(text)
