import os
from pathlib import Path

from .constants import RESOLUTION_ORDER
from . import providers
from .library import load_info


def human_size(num_bytes):
    value = float(max(0, num_bytes or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def asset_changed(old, new):
    if old is None:
        return "new"
    for field in ("files_hash", "categories", "tags", "authors", "name"):
        if old.get(field) != new.get(field):
            return "updated"
    return "same"


def compare_manifest(root, assets):
    new_count = 0
    updated_count = 0
    for slug, info in assets.items():
        status = asset_changed(load_info(root, slug), info)
        if status == "new":
            new_count += 1
        elif status == "updated":
            updated_count += 1
    return new_count, updated_count


def _branch(info):
    asset_type = int(info.get("type", 0))
    return info.get("files", {}).get("hdri" if asset_type == 0 else "blend", {})


def available_resolutions(info):
    provider = str((info or {}).get("provider") or "polyhaven")
    if provider != "polyhaven":
        return providers.available_resolutions(info)
    branch = _branch(info)
    return [r for r in RESOLUTION_ORDER if r in branch] + [r for r in branch if r not in RESOLUTION_ORDER]


def resolution_size(info, resolution):
    provider = str((info or {}).get("provider") or "polyhaven")
    if provider != "polyhaven":
        entry = (info.get("download_variants") or {}).get(resolution) or {}
        return int(entry.get("size", 0) or 0)
    asset_type = int(info.get("type", 0))
    files = info.get("files", {})
    if asset_type == 0:
        entry = files.get("hdri", {}).get(resolution, {})
        file_info = entry.get("hdr") or entry.get("exr") or {}
        return int(file_info.get("size", 0) or 0)
    blend = files.get("blend", {}).get(resolution, {}).get("blend", {})
    size = int(blend.get("size", 0) or 0)
    for entry in blend.get("include", {}).values():
        size += int(entry.get("size", 0) or 0)
    return size


def resolution_files(root, slug, info, resolution, include_blend=False):
    root = Path(root)
    provider = str((info or {}).get("provider") or "polyhaven")
    if provider != "polyhaven":
        rels = (info.get("local_resolution_files") or {}).get(resolution) or []
        return [root / slug / rel for rel in rels]

    asset_type = int(info.get("type", 0))
    files = info.get("files", {})
    output = []
    if asset_type == 0:
        entry = files.get("hdri", {}).get(resolution, {})
        file_info = entry.get("hdr") or entry.get("exr")
        if file_info:
            output.append(root / slug / Path(file_info["url"]).name)
        return output

    blend = files.get("blend", {}).get(resolution, {}).get("blend", {})
    if include_blend and blend:
        output.append(root / slug / f"{slug}.blend")
    for sub_path in blend.get("include", {}):
        output.append(root / slug / sub_path)
    return output


def resolution_is_local(root, slug, info, resolution):
    if str((info or {}).get("phl_base_resolution") or "") == resolution and (Path(root) / slug / f"{slug}.blend").exists():
        return True
    paths = resolution_files(root, slug, info, resolution)
    return bool(paths) and all(path.exists() for path in paths)


def folder_size(path):
    total = 0
    path = Path(path)
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except OSError:
                pass
    return total


def asset_folder_size(root, slug):
    return folder_size(Path(root) / slug)


def clean_asset_high_res(root, slug, keep_resolution=None):
    info = load_info(root, slug)
    if not info:
        return 0, 0
    keep = keep_resolution or info.get("phl_base_resolution", "1k")
    removed_files = 0
    removed_bytes = 0
    for resolution in available_resolutions(info):
        if resolution == keep:
            continue
        for path in resolution_files(root, slug, info, resolution):
            if not path.exists():
                continue
            try:
                size = path.stat().st_size
                path.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError:
                pass
    return removed_files, removed_bytes


def clean_library_high_res(root):
    root = Path(root)
    files = 0
    bytes_removed = 0
    if not root.exists():
        return files, bytes_removed
    for child in root.iterdir():
        if not child.is_dir():
            continue
        info = load_info(root, child.name)
        if not info:
            continue
        count, size = clean_asset_high_res(root, child.name, info.get("phl_base_resolution", "1k"))
        files += count
        bytes_removed += size
    return files, bytes_removed


def restore_state_path(root, slug):
    return Path(root) / slug / "restore.json"


def save_restore_state(root, slug, data):
    import json

    path = restore_state_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data or {})
    payload["slug"] = slug
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_restore_state(root, slug):
    import json

    path = restore_state_path(root, slug)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def delete_asset_payload(root, slug, keep_thumbnail=True):
    """Delete heavy local asset payload while retaining metadata/restore state.

    The folder is intentionally kept as a cloud placeholder. info.json lets the add-on
    restore the asset without losing its provider identity; restore.json stores the
    user's last managed settings.
    """
    asset_dir = Path(root) / slug
    if not asset_dir.exists():
        return 0, 0

    keep_names = {"info.json", "restore.json"}

    removed_files = 0
    removed_bytes = 0
    for path in sorted(asset_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file():
            if path.parent == asset_dir and (path.name in keep_names or (keep_thumbnail and path.name.startswith("thumbnail."))):
                continue
            try:
                removed_bytes += path.stat().st_size
            except OSError:
                pass
            try:
                path.unlink()
                removed_files += 1
            except OSError:
                pass
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return removed_files, removed_bytes
