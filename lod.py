import re
from pathlib import Path

import bpy

from .library import library_root
from .resolution import detect_active_asset

_LOD_RE = re.compile(r"^(.*)_LOD(\d+)$", re.IGNORECASE)


def _source_path(context, slug, datablock):
    ref = getattr(datablock, "library_weak_reference", None)
    if ref and getattr(ref, "filepath", ""):
        return Path(bpy.path.abspath(ref.filepath))
    return library_root(context) / slug / f"{slug}.blend"


def available_lods(context):
    detected = detect_active_asset(context)
    if not detected:
        return []
    slug, asset_type, datablock, _trees = detected
    if asset_type != 2:
        return []
    source = _source_path(context, slug, datablock)
    if not source.exists():
        return []
    names = []
    try:
        with bpy.data.libraries.load(str(source), link=False) as (data_from, _data_to):
            names = list(data_from.collections)
    except Exception:
        return []

    lods = []
    for name in names:
        match = _LOD_RE.match(name)
        if not match:
            continue
        if slug.casefold() not in match.group(1).casefold():
            continue
        lods.append((int(match.group(2)), name))
    lods.sort(key=lambda item: item[0])
    return [name for _idx, name in lods]


def current_lod(context):
    obj = context.active_object
    coll = getattr(obj, "instance_collection", None) if obj else None
    if coll:
        match = _LOD_RE.match(coll.name)
        if match:
            return coll.name
    return ""


def _find_loaded_collection(source_path, name):
    source_resolved = Path(source_path).resolve()
    for collection in bpy.data.collections:
        if collection.name != name:
            continue
        ref = getattr(collection, "library_weak_reference", None)
        if not ref or not getattr(ref, "filepath", ""):
            return collection
        try:
            if Path(bpy.path.abspath(ref.filepath)).resolve() == source_resolved:
                return collection
        except Exception:
            pass
    return None


def switch_lod(context, target_name):
    detected = detect_active_asset(context)
    if not detected:
        raise RuntimeError("Select a managed model instance first")
    slug, asset_type, datablock, _trees = detected
    if asset_type != 2:
        raise RuntimeError("LOD switching is only available for models")
    obj = context.active_object
    if obj is None or getattr(obj, "instance_collection", None) is None:
        raise RuntimeError("LOD switching requires a collection instance")

    available = available_lods(context)
    if target_name not in available:
        raise RuntimeError(f"LOD collection {target_name} is not available")

    source = _source_path(context, slug, datablock)
    collection = _find_loaded_collection(source, target_name)
    if collection is None:
        with bpy.data.libraries.load(str(source), link=False) as (data_from, data_to):
            if target_name not in data_from.collections:
                raise RuntimeError(f"Collection {target_name} is missing from source file")
            data_to.collections = [target_name]
        collection = _find_loaded_collection(source, target_name) or bpy.data.collections.get(target_name)

    if collection is None:
        raise RuntimeError(f"Could not load {target_name}")

    obj.instance_collection = collection
    obj.name = collection.name
    obj["phl_slug"] = slug
    obj["phl_type"] = 2
    obj["phl_lod"] = target_name
    return target_name


def preset_lod(context, quality):
    lods = available_lods(context)
    if not lods:
        return ""
    if quality == "HIGH":
        return lods[0]
    if quality == "LOW":
        return lods[-1]
    return lods[len(lods) // 2]
