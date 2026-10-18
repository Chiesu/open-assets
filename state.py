import threading
import time

from .constants import RECENT_LIMIT


class TaskState:
    def __init__(self):
        self._lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.reset()

    def reset(self):
        with getattr(self, "_lock", threading.Lock()):
            self.running = False
            self.done = False
            self.total = 0
            self.completed = 0
            self.downloaded = 0
            self.skipped = 0
            self.current = ""
            self.error = ""
            self.cancelled = False
            self.result = None
        if hasattr(self, "cancel_event"):
            self.cancel_event.clear()

    def begin(self):
        self.reset()
        with self._lock:
            self.running = True

    def set_total(self, total):
        with self._lock:
            self.total = int(total)

    def advance(self, current="", downloaded=False, skipped=False):
        with self._lock:
            self.completed += 1
            self.current = current
            if downloaded:
                self.downloaded += 1
            if skipped:
                self.skipped += 1

    def set_current(self, current):
        with self._lock:
            self.current = current

    def fail(self, message):
        with self._lock:
            self.error = str(message)
            self.running = False
            self.done = True

    def finish(self, cancelled=False, result=None):
        with self._lock:
            self.running = False
            self.done = True
            self.cancelled = bool(cancelled)
            self.result = result

    def cancel(self):
        self.cancel_event.set()

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running,
                "done": self.done,
                "total": self.total,
                "completed": self.completed,
                "downloaded": self.downloaded,
                "skipped": self.skipped,
                "current": self.current,
                "error": self.error,
                "cancelled": self.cancelled,
                "result": self.result,
            }


class RuntimeState:
    def __init__(self):
        self._lock = threading.Lock()
        self.new_assets = 0
        self.updated_assets = 0
        self.last_check = 0.0
        self.recent = []
        self.loaded_root = ""

    def hydrate(self, root, data):
        with self._lock:
            self.loaded_root = str(root)
            self.new_assets = int(data.get("new_assets", 0))
            self.updated_assets = int(data.get("updated_assets", 0))
            self.last_check = float(data.get("last_check", 0.0))
            recent = data.get("recent", [])
            self.recent = recent[:RECENT_LIMIT] if isinstance(recent, list) else []

    def ensure_root(self, root, data=None):
        if str(root) == self.loaded_root:
            return
        self.hydrate(root, data or {})

    def set_update_counts(self, new_assets, updated_assets):
        with self._lock:
            self.new_assets = int(new_assets)
            self.updated_assets = int(updated_assets)
            self.last_check = time.time()

    def add_recent(self, slug, name=None, resolution=None):
        item = {
            "slug": slug,
            "name": name or slug.replace("_", " ").title(),
            "resolution": resolution or "",
            "time": time.time(),
        }
        with self._lock:
            self.recent = [x for x in self.recent if x.get("slug") != slug]
            self.recent.insert(0, item)
            del self.recent[RECENT_LIMIT:]

    def snapshot(self):
        with self._lock:
            return {
                "new_assets": self.new_assets,
                "updated_assets": self.updated_assets,
                "last_check": self.last_check,
                "recent": list(self.recent),
            }


class SessionState:
    """Ephemeral list of assets imported during the current Blender session.

    Entries are grouped by managed asset key/type instead of by instance. This keeps the
    sidebar useful even when the same model is placed several times.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    @staticmethod
    def _key(slug, asset_type):
        return f"{int(asset_type)}:{slug}"

    def add_import(self, record):
        if not record:
            return
        slug = str(record.get("slug", ""))
        if not slug:
            return
        asset_type = int(record.get("asset_type", 0))
        key = self._key(slug, asset_type)
        now = time.time()
        with self._lock:
            previous = dict(self._items.get(key, {}))
            count = int(previous.get("count", 0)) + 1
            merged = previous
            merged.update(dict(record))
            merged.update(
                slug=slug,
                asset_type=asset_type,
                count=count,
                first_import=previous.get("first_import", now),
                last_import=now,
                status="LIVE",
            )
            self._items[key] = merged

    def update(self, slug, asset_type=None, **changes):
        with self._lock:
            keys = [
                key
                for key, item in self._items.items()
                if item.get("slug") == slug and (asset_type is None or int(item.get("asset_type", -1)) == int(asset_type))
            ]
            for key in keys:
                self._items[key].update(changes)

    def merge(self, record):
        if not record or not record.get("slug"):
            return
        slug = str(record.get("slug"))
        asset_type = int(record.get("asset_type", 0))
        key = self._key(slug, asset_type)
        with self._lock:
            current = dict(self._items.get(key, {"slug": slug, "asset_type": asset_type, "count": 0}))
            current.update(dict(record))
            self._items[key] = current

    def mark_removed(self, slug, asset_type=None, cloud=False):
        self.update(slug, asset_type, status="CLOUD" if cloud else "REMOVED", count=0)

    def get(self, slug, asset_type=None):
        with self._lock:
            for item in self._items.values():
                if item.get("slug") == slug and (asset_type is None or int(item.get("asset_type", -1)) == int(asset_type)):
                    return dict(item)
        return None

    def items(self):
        with self._lock:
            return sorted(
                (dict(item) for item in self._items.values()),
                key=lambda item: float(item.get("last_import", 0.0)),
                reverse=True,
            )

    def count(self):
        with self._lock:
            return len(self._items)

    def clear(self):
        with self._lock:
            self._items.clear()


SYNC = TaskState()
CHECK = TaskState()
DOWNLOAD = TaskState()
BROWSER = TaskState()
IMPORT = TaskState()
BATCH = TaskState()
RUNTIME = RuntimeState()
SESSION = SessionState()
