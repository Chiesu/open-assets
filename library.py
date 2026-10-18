import json
import uuid
from pathlib import Path

import bpy

from .constants import (
    CATALOG_NAMESPACE,
    CATALOG_PATHS,
    LIBRARY_NAME,
    RUNTIME_STATE_FILE,
    TYPE_NAMES,
)

_NAMESPACE = uuid.UUID(CATALOG_NAMESPACE)


def addon_preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def library_root(context=None):
    prefs = addon_preferences(context)
    if prefs is None:
        raise RuntimeError("Open Assets preferences are unavailable")
    return Path(bpy.path.abspath(prefs.library_path)).expanduser().resolve()


def find_asset_library(context=None):
    context = context or bpy.context
    root = library_root(context)
    libraries = context.preferences.filepaths.asset_libraries
    for lib in libraries:
        try:
            lib_path = Path(bpy.path.abspath(lib.path)).expanduser().resolve()
        except Exception:
            lib_path = None
        if lib.name == LIBRARY_NAME or lib_path == root:
            return lib
    return None


def ensure_asset_library(context=None):
    context = context or bpy.context
    root = library_root(context)
    root.mkdir(parents=True, exist_ok=True)
    write_catalog_file(root)

    existing = find_asset_library(context)
    if existing:
        existing.name = LIBRARY_NAME
        existing.path = str(root)
        if hasattr(existing, "import_method"):
            existing.import_method = "APPEND"
        return existing

    libraries = context.preferences.filepaths.asset_libraries
    created = None
    if hasattr(libraries, "new"):
        try:
            created = libraries.new(name=LIBRARY_NAME, directory=str(root))
        except Exception:
            created = None

    if created is None:
        before = {lib.as_pointer() for lib in libraries}
        bpy.ops.preferences.asset_library_add(directory=str(root))
        for lib in libraries:
            if lib.as_pointer() not in before:
                created = lib
                break
        if created is None and libraries:
            created = libraries[-1]
        if created:
            created.name = LIBRARY_NAME
            created.path = str(root)

    if created is None:
        raise RuntimeError("Could not register the Blender asset library")

    if hasattr(created, "import_method"):
        created.import_method = "APPEND"
    return created


def catalog_id_for_path(path):
    return str(uuid.uuid5(_NAMESPACE, str(path).strip("/")))


def _has(categories, name):
    target = name.casefold()
    return any(str(item).casefold() == target for item in categories)


def catalog_path_for_asset(asset_type, categories):
    categories = [str(x) for x in (categories or [])]
    root = TYPE_NAMES[int(asset_type)]

    if int(asset_type) == 0:
        if _has(categories, "Indoor"):
            path = f"{root}/Indoor"
            for branch in ("Natural Light", "Artificial Light", "Studio"):
                if _has(categories, branch):
                    path += f"/{branch}"
                    break
            for detail in ("Low Contrast", "Medium Contrast", "High Contrast"):
                if _has(categories, detail):
                    path += f"/{detail}"
                    break
            return path
        if _has(categories, "Outdoor"):
            path = f"{root}/Outdoor"
            for branch in ("Nature", "Urban", "Skies"):
                if _has(categories, branch):
                    path += f"/{branch}"
                    break
            for detail in ("Midday", "Morning-afternoon", "Night", "Sunrise-sunset"):
                if _has(categories, detail):
                    path += f"/{detail}"
                    break
            return path

    if int(asset_type) == 1:
        if _has(categories, "Terrain"):
            path = f"{root}/Terrain"
            for detail in ("Aerial", "Rock", "Sand", "Snow"):
                if _has(categories, detail):
                    return f"{path}/{detail}"
            return path
        for branch in ("Rock", "Roofing", "Wood", "Brick", "Fabric", "Metal", "Plaster-concrete"):
            if _has(categories, branch):
                return f"{root}/{branch}"

    if int(asset_type) == 2:
        if _has(categories, "Props"):
            path = f"{root}/Props"
            for detail in ("Appliances", "Electronics", "Tools"):
                if _has(categories, detail):
                    return f"{path}/{detail}"
            return path
        if _has(categories, "Nature"):
            path = f"{root}/Nature"
            if _has(categories, "Ground Cover"):
                path += "/Ground Cover"
                if _has(categories, "Grass"):
                    path += "/Grass"
                return path
            for detail in ("Food", "Plants", "Potted Plants", "Rocks"):
                if _has(categories, detail):
                    return f"{path}/{detail}"
            return path
        if _has(categories, "Furniture"):
            path = f"{root}/Furniture"
            for detail in ("Seating", "Shelves", "Table"):
                if _has(categories, detail):
                    return f"{path}/{detail}"
            return path
        for branch in ("Industrial", "Lighting"):
            if _has(categories, branch):
                return f"{root}/{branch}"

    if categories:
        return f"{root}/{categories[0]}"
    return root


def catalog_id_for_info(info):
    path = catalog_path_for_asset(int(info.get("type", 0)), info.get("categories", []))
    return catalog_id_for_path(path), path


def _existing_catalog_paths(root):
    catalog_file = Path(root) / "blender_assets.cats.txt"
    paths = set()
    if not catalog_file.exists():
        return paths
    try:
        for line in catalog_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("VERSION"):
                continue
            parts = line.split(":", 2)
            if len(parts) >= 2 and parts[1].strip():
                paths.add(parts[1].strip())
    except OSError:
        pass
    return paths


def write_catalog_file(root, assets=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # Preserve dynamically created provider categories across per-asset downloads.
    paths = set(CATALOG_PATHS) | _existing_catalog_paths(root)
    if assets:
        for info in assets:
            try:
                paths.add(catalog_path_for_asset(int(info.get("type", 0)), info.get("categories", [])))
            except Exception:
                continue

    expanded = set()
    for path in paths:
        parts = path.split("/")
        for idx in range(1, len(parts) + 1):
            expanded.add("/".join(parts[:idx]))

    lines = ["VERSION 1", ""]
    for path in sorted(expanded, key=lambda item: (item.count("/"), item.casefold())):
        label = path.replace("/", "-")
        lines.append(f"{catalog_id_for_path(path)}:{path}:{label}")
    lines.append("")
    (root / "blender_assets.cats.txt").write_text("\n".join(lines), encoding="utf-8")


def info_path(root, slug):
    return Path(root) / slug / "info.json"


def load_info(root, slug):
    path = info_path(root, slug)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_info(root, slug, info):
    path = info_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")


def runtime_state_path(root):
    return Path(root) / RUNTIME_STATE_FILE


def load_runtime_state(root):
    path = runtime_state_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_runtime_state(root, data):
    path = runtime_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
