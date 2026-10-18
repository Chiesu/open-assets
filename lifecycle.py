import threading
import time
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

from . import inventory, material_tools, state
from .library import addon_preferences, library_root, load_info

_AUTO_PENDING = set()
_LAST_WATCH = 0.0


def _quality_from_resolution(resolution):
    value = str(resolution or "").lower()
    if value in {"1k"}:
        return "PREVIEW"
    if value in {"4k", "8k", "16k"}:
        return "FINAL"
    return "WORKING"


def _material_for_slug(slug):
    for material in bpy.data.materials:
        if material.get("phl_slug") == slug and int(material.get("phl_type", 1)) == 1:
            return material
    return None


def _model_objects(slug):
    output = []
    for obj in bpy.data.objects:
        try:
            if obj.get("phl_slug") == slug and int(obj.get("phl_type", 2)) == 2:
                output.append(obj)
                continue
            collection = getattr(obj, "instance_collection", None)
            if collection is not None and collection.get("phl_slug") == slug:
                output.append(obj)
        except Exception:
            continue
    return output


def _world_for_slug(slug):
    for world in bpy.data.worlds:
        if world.get("phl_slug") == slug:
            return world
    return None


def material_users(material):
    users = []
    if material is None:
        return users
    for obj in bpy.data.objects:
        for slot in getattr(obj, "material_slots", []):
            if slot.material == material:
                users.append(obj)
                break
    return users


def usage_count(slug, asset_type):
    asset_type = int(asset_type)
    if asset_type == 1:
        mat = _material_for_slug(slug)
        return len(material_users(mat)) if mat else 0
    if asset_type == 2:
        return len(_model_objects(slug))
    world = _world_for_slug(slug)
    return sum(1 for scene in bpy.data.scenes if scene.world == world) if world else 0


def capture_state(context, slug, asset_type):
    asset_type = int(asset_type)
    existing = state.SESSION.get(slug, asset_type) or {}
    record = dict(existing)
    record.update(slug=slug, asset_type=asset_type)
    try:
        local_info = load_info(library_root(context), slug) or {}
        record["provider"] = str(local_info.get("provider") or record.get("provider") or "polyhaven")
        record["provider_name"] = str(local_info.get("provider_name") or record.get("provider_name") or "Poly Haven")
        record["name"] = str(local_info.get("name") or record.get("name") or slug.replace("_", " ").title())
    except Exception:
        pass

    if asset_type == 1:
        material = _material_for_slug(slug)
        if material is not None:
            record["datablock_name"] = material.name
            record["resolution"] = str(material.get("phl_resolution", record.get("resolution", "1k")))
            record["quality"] = record.get("quality") or _quality_from_resolution(record["resolution"])
            record["real_scale"] = bool(material.get("phl_real_scale_applied", False))
            record["real_scale_x"] = float(material.get("phl_real_scale_x", 1.0))
            record["real_scale_y"] = float(material.get("phl_real_scale_y", 1.0))
            record["displacement_mode"] = str(material.get("phl_displacement_mode", ""))
            record["displacement_strength"] = float(material.get("phl_displacement_strength", 0.1))
            record["displacement_midlevel"] = float(material.get("phl_displacement_midlevel", 0.5))
            record["displacement_subdivisions"] = int(material.get("phl_displacement_subdivisions", 2))
            users = material_users(material)
            if users:
                record["target_object"] = users[0].name
    elif asset_type == 2:
        objects = _model_objects(slug)
        if objects:
            obj = objects[0]
            collection = getattr(obj, "instance_collection", None)
            source = collection or obj
            record["object_name"] = obj.name
            record["resolution"] = str(source.get("phl_resolution", record.get("resolution", "1k")))
            record["quality"] = record.get("quality") or _quality_from_resolution(record["resolution"])
            record["lod"] = str(source.get("phl_lod", obj.get("phl_lod", record.get("lod", ""))))
            record["location"] = list(obj.location)
    else:
        world = _world_for_slug(slug)
        if world is not None:
            record["datablock_name"] = world.name
            record["resolution"] = str(world.get("phl_resolution", record.get("resolution", "1k")))
            record["quality"] = record.get("quality") or _quality_from_resolution(record["resolution"])
    return record


def save_placeholder(context, slug, asset_type):
    root = library_root(context)
    record = capture_state(context, slug, asset_type)
    inventory.save_restore_state(root, slug, record)
    state.SESSION.merge(record)
    return record


def remove_asset_from_file(context, slug, asset_type):
    asset_type = int(asset_type)
    record = save_placeholder(context, slug, asset_type)

    if asset_type == 1:
        materials = [m for m in bpy.data.materials if m.get("phl_slug") == slug and int(m.get("phl_type", 1)) == 1]
        for material in materials:
            bpy.data.materials.remove(material, do_unlink=True)
    elif asset_type == 2:
        objects = list(_model_objects(slug))
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        collections = [c for c in bpy.data.collections if c.get("phl_slug") == slug]
        for collection in collections:
            try:
                if collection.users == 0:
                    bpy.data.collections.remove(collection)
            except Exception:
                pass
    else:
        world = _world_for_slug(slug)
        if world is not None:
            for scene in bpy.data.scenes:
                if scene.world == world:
                    scene.world = None
            try:
                bpy.data.worlds.remove(world, do_unlink=True)
            except Exception:
                pass

    state.SESSION.mark_removed(slug, asset_type, cloud=False)
    return record


def focus_asset(context, slug, asset_type):
    asset_type = int(asset_type)
    if asset_type == 1:
        material = _material_for_slug(slug)
        users = material_users(material)
        if users:
            obj = users[0]
            for selected in list(context.selected_objects):
                selected.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            for index, slot in enumerate(obj.material_slots):
                if slot.material == material:
                    obj.active_material_index = index
                    break
            return obj.name
        return material.name if material else ""
    if asset_type == 2:
        objects = _model_objects(slug)
        if objects:
            obj = objects[0]
            for selected in list(context.selected_objects):
                selected.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            return obj.name
        return ""
    world = _world_for_slug(slug)
    if world is not None:
        context.scene.world = world
        return world.name
    return ""


def apply_material_restore(context, record):
    if not record or int(record.get("asset_type", -1)) != 1:
        return
    obj = context.active_object
    mat = getattr(obj, "active_material", None) if obj else None
    if not mat or mat.get("phl_slug") != record.get("slug"):
        return
    if record.get("real_scale"):
        try:
            material_tools.apply_real_scale(context)
        except Exception:
            pass
    mode = str(record.get("displacement_mode", ""))
    if mode:
        try:
            material_tools.setup_displacement(
                context,
                mode=mode,
                strength=float(record.get("displacement_strength", 0.1)),
                midlevel=float(record.get("displacement_midlevel", 0.5)),
                subdivisions=int(record.get("displacement_subdivisions", 2)),
            )
        except Exception:
            pass


def _delete_payload_worker(root, slug, asset_type, record):
    try:
        inventory.save_restore_state(root, slug, record)
        inventory.delete_asset_payload(root, slug)
        state.SESSION.mark_removed(slug, asset_type, cloud=True)
    finally:
        _AUTO_PENDING.discard((slug, int(asset_type)))


@persistent
def depsgraph_watch(_scene, _depsgraph):
    """Optional cleanup when a session material datablock is deleted manually.

    This only watches imported texture materials and only runs when the user opts in.
    The metadata/restore record is retained, so the asset remains restorable from cloud.
    """
    global _LAST_WATCH
    now = time.time()
    if now - _LAST_WATCH < 1.0:
        return
    _LAST_WATCH = now

    try:
        prefs = addon_preferences(bpy.context)
        if prefs is None or not getattr(prefs, "auto_cleanup_deleted_materials", False):
            return
        root = library_root(bpy.context)
    except Exception:
        return

    for item in state.SESSION.items():
        if int(item.get("asset_type", -1)) != 1:
            continue
        slug = item.get("slug", "")
        if not slug or _material_for_slug(slug) is not None:
            continue
        key = (slug, 1)
        if key in _AUTO_PENDING:
            continue
        if not (Path(root) / slug / f"{slug}.blend").exists():
            continue
        record = dict(item)
        _AUTO_PENDING.add(key)
        thread = threading.Thread(
            target=_delete_payload_worker,
            args=(Path(root), slug, 1, record),
            daemon=True,
        )
        thread.start()
