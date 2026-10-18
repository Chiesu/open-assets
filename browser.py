import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from . import network, providers
from .constants import RESOLUTION_ORDER, TYPE_NAMES

MANIFEST_CACHE = ".open_assets_manifest.json"
LEGACY_MANIFEST_CACHE = ".polyhaven_lite_manifest.json"
THUMB_DIR = ".open_assets_thumbs_v2"
PAGE_SIZE = 6


class RemoteCatalog:
    def __init__(self):
        self._lock = threading.Lock()
        self.assets = {}
        self.loaded_at = 0.0
        self.source = ""
        self.error = ""
        self.meta = {}

    def set_assets(self, assets, source="remote", meta=None):
        with self._lock:
            self.assets = dict(assets or {})
            self.loaded_at = time.time()
            self.source = source
            self.error = ""
            self.meta = dict(meta or {})

    def set_error(self, message):
        with self._lock:
            self.error = str(message)

    def get(self, slug):
        with self._lock:
            item = self.assets.get(slug)
            return dict(item) if item else None

    def snapshot(self):
        with self._lock:
            return {
                "assets": dict(self.assets),
                "loaded_at": self.loaded_at,
                "source": self.source,
                "error": self.error,
                "meta": dict(self.meta),
            }

    def count(self):
        with self._lock:
            return len(self.assets)


REMOTE = RemoteCatalog()
_PREVIEWS = None
_EXECUTOR = None
_PENDING = set()
_COMPLETED = set()
_THUMB_LOCK = threading.Lock()
_LOCAL_COUNT_CACHE = {"root": "", "time": 0.0, "count": 0}
_CATEGORY_ITEMS = [("ALL", "All Categories", "Show every category")]


def manifest_path(root):
    return Path(root) / MANIFEST_CACHE


def thumbnail_dir(root):
    path = Path(root) / THUMB_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def thumbnail_path(root, slug, info=None):
    url = thumbnail_url(slug, info or {})
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".webp"
    return thumbnail_dir(root) / f"{slug}{suffix}"


def thumbnail_url(slug, info):
    info = info or {}
    if info.get("thumbnail_url"):
        return str(info["thumbnail_url"])
    provider = str(info.get("provider") or "polyhaven")
    if provider == "polyhaven":
        provider_id = info.get("provider_id") or slug
        return f"https://cdn.polyhaven.com/asset_img/thumbs/{provider_id}.png?width=512&height=512&format=webp"
    return ""


def save_manifest(root, assets, meta=None):
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"saved_at": time.time(), "assets": assets, "meta": meta or {}}
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_manifest(root):
    candidates = (manifest_path(root), Path(root) / LEGACY_MANIFEST_CACHE)
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            assets = payload.get("assets", payload)
            if isinstance(assets, dict) and assets:
                REMOTE.set_assets(assets, source="cache", meta=payload.get("meta") or {})
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


def ensure_cached(root):
    if REMOTE.count():
        return True
    return load_manifest(root)


def _filter_update(self, context):
    self.page = 0
    available = {item[0] for item in category_items(self, context)}
    if self.category and self.category not in available:
        self.category = "ALL"


def _category_update(self, context):
    self.page = 0


def category_items(self, context):
    global _CATEGORY_ITEMS
    assets = REMOTE.snapshot()["assets"]
    categories = set()
    selected_type = getattr(self, "asset_type", "ALL")
    selected_source = getattr(self, "source", "ALL")
    type_value = {"HDRIS": 0, "TEXTURES": 1, "MODELS": 2}.get(selected_type)
    provider_value = {"POLYHAVEN": "polyhaven", "AMBIENTCG": "ambientcg", "BLENDKIT": "blendkit", "CGBOOKCASE": "cgbookcase", "KENNEY": "kenney"}.get(selected_source)
    for info in assets.values():
        if type_value is not None and int(info.get("type", -1)) != type_value:
            continue
        if provider_value and str(info.get("provider") or "polyhaven") != provider_value:
            continue
        for category in info.get("categories", []) or []:
            text = str(category).strip()
            if text:
                categories.add(text)
    _CATEGORY_ITEMS = [("ALL", "All Categories", "Show every category")]
    _CATEGORY_ITEMS.extend((value, value, f"Show {value}") for value in sorted(categories, key=str.casefold))
    return _CATEGORY_ITEMS


class PHLBrowserProperties(bpy.types.PropertyGroup):
    search: StringProperty(
        name="Search",
        description="Search asset names, tags and categories. Press Search to query all enabled sources.",
        default="",
        update=_filter_update,
    )
    source: EnumProperty(
        name="Source",
        items=(
            ("ALL", "All", "Search every supported source"),
            ("POLYHAVEN", "PH", "Poly Haven"),
            ("AMBIENTCG", "ACG", "ambientCG"),
            ("BLENDKIT", "BK", "Blendkit"),
            ("CGBOOKCASE", "CGB", "CGBookcase"),
            ("KENNEY", "KNY", "Kenney 3D packs"),
        ),
        default="ALL",
        update=_filter_update,
    )
    asset_type: EnumProperty(
        name="Type",
        items=(
            ("ALL", "All", "All asset types"),
            ("MODELS", "Models", "3D models"),
            ("TEXTURES", "Materials", "Materials and textures"),
            ("HDRIS", "HDRIs", "Environment maps"),
        ),
        default="ALL",
        update=_filter_update,
    )
    category: EnumProperty(name="Category", items=category_items, update=_category_update)
    sort: EnumProperty(
        name="Sort",
        items=(
            ("RELEVANCE", "Relevance", "Keep provider relevance ordering"),
            ("POPULAR", "Popular", "Sort by downloads when available"),
            ("NEW", "Newest", "Newest published assets first"),
            ("NAME", "Name", "Alphabetical"),
        ),
        default="RELEVANCE",
        update=_filter_update,
    )
    availability: EnumProperty(
        name="Availability",
        items=(
            ("ANY", "Cloud + Local", "Show all matching assets"),
            ("LOCAL", "Local", "Only assets already downloaded"),
            ("CLOUD", "Cloud", "Only assets not downloaded yet"),
        ),
        default="ANY",
        update=_filter_update,
    )
    creation_method: EnumProperty(
        name="Method",
        items=(
            ("ANY", "Any Method", "Do not filter by creation method"),
            ("SCANNED", "Scanned", "Photogrammetry and scanned assets"),
            ("PROCEDURAL", "Procedural", "Procedural/generated assets when identified by the source"),
        ),
        default="ANY",
        update=_filter_update,
    )
    min_resolution: EnumProperty(
        name="Min Resolution",
        items=(
            ("ANY", "Any Resolution", "Do not require a minimum source resolution"),
            ("2k", "2K+", "Only assets with a 2K or higher source option"),
            ("4k", "4K+", "Only assets with a 4K or higher source option"),
            ("8k", "8K+", "Only assets with an 8K or higher source option"),
        ),
        default="ANY",
        update=_filter_update,
    )
    deep_search: BoolProperty(
        name="Deep Search",
        description="Ask each source for a larger result set. Slower, useful when researching a specific asset.",
        default=False,
    )
    show_filters: BoolProperty(name="Advanced Filters", default=False)
    page: IntProperty(name="Page", default=0, min=0)
    selected_slug: StringProperty(name="Selected Asset", default="")
    show_active_details: BoolProperty(
        name="Show Active Asset Details",
        description="Expand controls for the managed asset active in the scene",
        default=False,
    )
    quality: EnumProperty(
        name="Quality",
        items=(
            ("PREVIEW", "Preview", "Prefer 1K and low model LOD"),
            ("WORKING", "Working", "Prefer 2K and medium model LOD"),
            ("FINAL", "Final", "Prefer 4K and high model LOD"),
        ),
        default="WORKING",
    )
    view_mode: EnumProperty(
        name="View",
        items=(
            ("BROWSE", "Browse", "Search the supported cloud libraries"),
            ("SESSION", "Session", "Manage assets imported during this Blender session"),
        ),
        default="BROWSE",
    )
    batch_resolution: EnumProperty(
        name="Batch Resolution",
        items=(
            ("1k", "1K", "Switch compatible imported materials to 1K"),
            ("2k", "2K", "Switch compatible imported materials to 2K"),
            ("4k", "4K", "Switch compatible imported materials to 4K"),
            ("8k", "8K", "Switch compatible imported materials to 8K where available"),
        ),
        default="2k",
    )


def search_params(props):
    return {
        "query": props.search.strip(),
        "source": props.source,
        "asset_type": props.asset_type,
        "sort": props.sort,
        "creation_method": props.creation_method,
        "min_resolution": props.min_resolution,
        "deep_search": bool(props.deep_search),
    }


def _search_blob(slug, info):
    values = [slug, info.get("provider_id", ""), info.get("name", "")]
    values.extend(str(x) for x in info.get("categories", []) or [])
    values.extend(str(x) for x in info.get("tags", []) or [])
    return " ".join(values).casefold()


def _sort_key(item, mode):
    slug, info = item
    name = str(info.get("name") or slug.replace("_", " "))
    if mode == "NAME":
        return name.casefold()
    if mode == "NEW":
        return str(info.get("date_published") or info.get("date") or "")
    if mode == "POPULAR":
        value = info.get("download_count", info.get("downloads", 0))
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def filtered_assets(props, root=None):
    assets = list(REMOTE.snapshot()["assets"].items())
    type_value = {"HDRIS": 0, "TEXTURES": 1, "MODELS": 2}.get(props.asset_type)
    provider_value = {"POLYHAVEN": "polyhaven", "AMBIENTCG": "ambientcg", "BLENDKIT": "blendkit", "CGBOOKCASE": "cgbookcase", "KENNEY": "kenney"}.get(props.source)
    query = props.search.strip().casefold()
    category = props.category

    filtered = []
    for slug, info in assets:
        if type_value is not None and int(info.get("type", -1)) != type_value:
            continue
        if provider_value and str(info.get("provider") or "polyhaven") != provider_value:
            continue
        if category and category != "ALL":
            categories = {str(x) for x in info.get("categories", []) or []}
            if category not in categories:
                continue
        if query and not all(token in _search_blob(slug, info) for token in query.split()):
            continue
        if props.creation_method != "ANY" and str(info.get("method") or "OTHER") != props.creation_method:
            continue
        if props.min_resolution != "ANY" and props.min_resolution in RESOLUTION_ORDER:
            threshold = RESOLUTION_ORDER.index(props.min_resolution)
            available = providers.available_resolutions(info)
            if available and not any(r in RESOLUTION_ORDER and RESOLUTION_ORDER.index(r) >= threshold for r in available):
                continue
        if root is not None and props.availability != "ANY":
            local = is_local(root, slug)
            if props.availability == "LOCAL" and not local:
                continue
            if props.availability == "CLOUD" and local:
                continue
        filtered.append((slug, info))

    if props.sort != "RELEVANCE":
        reverse = props.sort in {"POPULAR", "NEW"}
        filtered.sort(key=lambda item: _sort_key(item, props.sort), reverse=reverse)
    return filtered


def _paginate(items, selected_slug="", page_size=PAGE_SIZE, columns=2):
    columns = max(1, int(columns))
    page_size = max(columns, int(page_size))
    items = list(items)
    if columns <= 1 or not selected_slug:
        return [items[i : i + page_size] for i in range(0, len(items), page_size)] or [[]]

    selected_index = next((i for i, item in enumerate(items) if item[0] == selected_slug), None)
    if selected_index is None:
        return [items[i : i + page_size] for i in range(0, len(items), page_size)] or [[]]

    selected_page_index = selected_index // page_size
    page_start = selected_page_index * page_size
    before = items[:page_start]
    nominal = list(items[page_start : page_start + page_size])
    after = list(items[page_start + page_size :])
    pages = [before[i : i + page_size] for i in range(0, len(before), page_size)]

    rel = next(i for i, item in enumerate(nominal) if item[0] == selected_slug)
    row_start = (rel // columns) * columns
    row_end = min(row_start + columns, len(nominal))
    row = nominal[row_start:row_end]
    carry = [item for item in row if item[0] != selected_slug]
    selected_item = nominal[rel]
    selected_page = nominal[:row_start] + [selected_item] + nominal[row_end:]
    pages.append(selected_page)
    remainder = carry + after
    pages.extend(remainder[i : i + page_size] for i in range(0, len(remainder), page_size))
    return pages or [[]]


def page_assets(props, root=None, page_size=PAGE_SIZE, columns=2):
    items = filtered_assets(props, root=root)
    total = len(items)
    partitioned = _paginate(items, props.selected_slug, page_size=page_size, columns=columns)
    pages = max(1, len(partitioned))
    page = min(max(0, int(props.page)), pages - 1)
    return partitioned[page] if partitioned else [], total, page, pages


def page_for_slug(props, slug, root=None, page_size=PAGE_SIZE, columns=2):
    partitioned = _paginate(filtered_assets(props, root=root), props.selected_slug, page_size=page_size, columns=columns)
    for index, page in enumerate(partitioned):
        if any(item_slug == slug for item_slug, _info in page):
            return index
    return 0


def selected_info(props):
    if not props.selected_slug:
        return None, None
    return props.selected_slug, REMOTE.get(props.selected_slug)


def is_local(root, slug):
    return (Path(root) / slug / "info.json").exists() and (Path(root) / slug / f"{slug}.blend").exists()


def invalidate_local_count():
    _LOCAL_COUNT_CACHE["time"] = 0.0


def local_count(root):
    root = Path(root)
    now = time.time()
    if _LOCAL_COUNT_CACHE["root"] == str(root) and now - _LOCAL_COUNT_CACHE["time"] < 3.0:
        return _LOCAL_COUNT_CACHE["count"]
    count = 0
    try:
        for child in root.iterdir():
            if child.is_dir() and (child / "info.json").exists() and (child / f"{child.name}.blend").exists():
                count += 1
    except OSError:
        pass
    _LOCAL_COUNT_CACHE.update(root=str(root), time=now, count=count)
    return count


def _download_thumb(root, slug, info, verify_ssl):
    path = thumbnail_path(root, slug, info)
    try:
        url = thumbnail_url(slug, info)
        if url:
            network.download_file(url, path, verify_ssl=verify_ssl, retries=2)
    finally:
        with _THUMB_LOCK:
            _PENDING.discard(slug)
            _COMPLETED.add(slug)


def request_thumbnail(root, slug, info, verify_ssl=True):
    path = thumbnail_path(root, slug, info)
    if path.exists():
        return path
    if not thumbnail_url(slug, info):
        return path
    global _EXECUTOR
    if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
        return path
    if _EXECUTOR is None:
        return path
    with _THUMB_LOCK:
        if slug in _PENDING:
            return path
        _PENDING.add(slug)
    _EXECUTOR.submit(_download_thumb, Path(root), slug, dict(info or {}), bool(verify_ssl))
    return path


def icon_id(root, slug, info, verify_ssl=True):
    global _PREVIEWS
    path = request_thumbnail(root, slug, info, verify_ssl)
    if _PREVIEWS is None or not path.exists():
        return 0
    key = f"oa:{slug}"
    try:
        if key not in _PREVIEWS:
            _PREVIEWS.load(key, str(path), "IMAGE")
        return _PREVIEWS[key].icon_id
    except Exception:
        return 0


def clear_preview(slug):
    if _PREVIEWS is None:
        return
    key = f"oa:{slug}"
    try:
        if key in _PREVIEWS:
            del _PREVIEWS[key]
    except Exception:
        pass


def _redraw_all():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def _thumbnail_timer():
    completed = False
    with _THUMB_LOCK:
        if _COMPLETED:
            _COMPLETED.clear()
            completed = True
    if completed:
        _redraw_all()
    return 0.35


def register_runtime():
    global _PREVIEWS, _EXECUTOR
    if _PREVIEWS is None:
        _PREVIEWS = bpy.utils.previews.new()
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="oa-thumb")
    try:
        if not bpy.app.timers.is_registered(_thumbnail_timer):
            bpy.app.timers.register(_thumbnail_timer, first_interval=0.35, persistent=True)
    except Exception:
        pass


def unregister_runtime():
    global _PREVIEWS, _EXECUTOR
    try:
        if bpy.app.timers.is_registered(_thumbnail_timer):
            bpy.app.timers.unregister(_thumbnail_timer)
    except Exception:
        pass
    if _EXECUTOR is not None:
        _EXECUTOR.shutdown(wait=False, cancel_futures=True)
        _EXECUTOR = None
    if _PREVIEWS is not None:
        try:
            bpy.utils.previews.remove(_PREVIEWS)
        except Exception:
            pass
        _PREVIEWS = None
    with _THUMB_LOCK:
        _PENDING.clear()
        _COMPLETED.clear()


def type_label(info):
    asset_type = int((info or {}).get("type", -1))
    return TYPE_NAMES.get(asset_type, "Assets")[:-1] if asset_type in TYPE_NAMES else "Asset"
