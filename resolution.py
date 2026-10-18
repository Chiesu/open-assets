import re
from pathlib import Path

import bpy

from . import network
from .inventory import available_resolutions, resolution_files, resolution_is_local, resolution_size
from .library import addon_preferences, library_root, load_info

_RES_SUFFIX = re.compile(r"_(?:1|2|4|8|16)k$", re.IGNORECASE)


def _normalize_name(path):
    stem = Path(path).stem
    return _RES_SUFFIX.sub("", stem).lower()


def _node_images(tree, seen_trees=None):
    if tree is None:
        return []
    seen_trees = seen_trees or set()
    ptr = tree.as_pointer() if hasattr(tree, "as_pointer") else id(tree)
    if ptr in seen_trees:
        return []
    seen_trees.add(ptr)
    images = []
    for node in tree.nodes:
        image = getattr(node, "image", None)
        if image is not None and image.filepath:
            images.append(image)
        sub = getattr(node, "node_tree", None)
        if sub is not None:
            images.extend(_node_images(sub, seen_trees))
    return images


def _collection_trees(collection):
    trees = []
    for obj in collection.all_objects:
        for slot in obj.material_slots:
            mat = slot.material
            if mat and mat.use_nodes:
                trees.append(mat.node_tree)
    return trees


def detect_active_asset(context):
    obj = context.active_object
    if obj is not None:
        coll = getattr(obj, "instance_collection", None)
        if coll is not None and coll.get("phl_slug"):
            return coll.get("phl_slug"), int(coll.get("phl_type", 2)), coll, _collection_trees(coll)
        if obj.get("phl_slug"):
            trees = []
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    trees.append(slot.material.node_tree)
            return obj.get("phl_slug"), int(obj.get("phl_type", 2)), obj, trees
        mat = getattr(obj, "active_material", None)
        if mat is not None and mat.get("phl_slug"):
            trees = [mat.node_tree] if mat.use_nodes else []
            return mat.get("phl_slug"), int(mat.get("phl_type", 1)), mat, trees

    world = context.scene.world
    if world is not None and world.get("phl_slug"):
        trees = [world.node_tree] if world.use_nodes else []
        return world.get("phl_slug"), int(world.get("phl_type", 0)), world, trees
    return None


def _target_for_image(info, asset_type, target_resolution, image_path):
    files = info["files"]
    if asset_type == 0:
        entry = files["hdri"][target_resolution].get("hdr") or files["hdri"][target_resolution].get("exr")
        return (Path(entry["url"]).name, entry) if entry else (None, None)

    includes = files["blend"][target_resolution]["blend"].get("include", {})
    current = _normalize_name(image_path)
    exact = []
    loose = []
    for sub_path, entry in includes.items():
        norm = _normalize_name(sub_path)
        if norm == current:
            exact.append((sub_path, entry))
        elif Path(norm).name == Path(current).name:
            loose.append((sub_path, entry))
    matches = exact or loose
    return matches[0] if matches else (None, None)


def build_plan_for(context, slug, asset_type, datablock, trees, target_resolution):
    root = library_root(context)
    info = load_info(root, slug)
    if info is None:
        raise RuntimeError(f"Missing local metadata for {slug}")
    if target_resolution not in available_resolutions(info):
        raise RuntimeError(f"{target_resolution.upper()} is not available for {slug}")

    images = []
    for tree in trees:
        images.extend(_node_images(tree))
    unique = {}
    for image in images:
        unique[image.as_pointer()] = image

    image_targets = []
    downloads = {}
    for image in unique.values():
        target_rel, file_info = _target_for_image(info, asset_type, target_resolution, image.filepath)
        if not target_rel or not file_info:
            continue
        destination = root / slug / target_rel
        image_targets.append((image, destination))
        downloads[str(destination)] = (file_info["url"], destination, file_info.get("md5"))

    if asset_type == 0 and not image_targets:
        files = info["files"]["hdri"][target_resolution]
        file_info = files.get("hdr") or files.get("exr")
        if file_info:
            destination = root / slug / Path(file_info["url"]).name
            downloads[str(destination)] = (file_info["url"], destination, file_info.get("md5"))

    prefs = addon_preferences(context)
    return {
        "slug": slug,
        "asset_type": int(asset_type),
        "datablock": datablock,
        "info": info,
        "root": root,
        "resolution": target_resolution,
        "image_targets": image_targets,
        "downloads": list(downloads.values()),
        "verify_ssl": prefs.verify_ssl if prefs else True,
    }


def build_plan(context, target_resolution):
    detected = detect_active_asset(context)
    if detected is None:
        raise RuntimeError("Select an imported managed asset first")
    slug, asset_type, datablock, trees = detected
    return build_plan_for(context, slug, asset_type, datablock, trees, target_resolution)


def material_plan(context, material, target_resolution):
    if material is None or not material.get("phl_slug"):
        raise RuntimeError("Material is not a managed material")
    slug = material.get("phl_slug")
    if int(material.get("phl_type", 1)) != 1:
        raise RuntimeError("Only standalone managed texture materials are supported in batch mode")
    trees = [material.node_tree] if material.use_nodes else []
    return build_plan_for(context, slug, 1, material, trees, target_resolution)


def imported_texture_materials():
    output = []
    for material in bpy.data.materials:
        try:
            if material.get("phl_slug") and int(material.get("phl_type", 1)) == 1:
                output.append(material)
        except Exception:
            continue
    return output


def download_plan(plan, task_state=None):
    downloads = plan["downloads"]
    if task_state:
        task_state.set_total(len(downloads))
    for url, destination, md5 in downloads:
        if task_state and task_state.cancel_event.is_set():
            raise InterruptedError("Download cancelled")
        network.download_file(
            url,
            destination,
            md5,
            verify_ssl=plan["verify_ssl"],
            cancel_event=task_state.cancel_event if task_state else None,
        )
        if task_state:
            task_state.advance(destination.name, downloaded=True)
    return True


def apply_plan(plan):
    changed = 0
    for image, destination in plan["image_targets"]:
        image.filepath = str(destination)
        image.name = destination.name
        try:
            image.reload()
        except RuntimeError:
            pass
        changed += 1

    datablock = plan["datablock"]
    datablock["phl_resolution"] = plan["resolution"]

    if plan["asset_type"] == 2 and hasattr(datablock, "all_objects"):
        for obj in datablock.all_objects:
            obj["phl_resolution"] = plan["resolution"]
            for slot in obj.material_slots:
                if slot.material:
                    slot.material["phl_resolution"] = plan["resolution"]
    return changed


def resolution_rows(context):
    detected = detect_active_asset(context)
    if detected is None:
        return []
    slug, _asset_type, datablock, _trees = detected
    root = library_root(context)
    info = load_info(root, slug)
    if not info:
        return []
    current = str(datablock.get("phl_resolution", info.get("phl_base_resolution", "1k")))
    return [
        {
            "resolution": res,
            "size": resolution_size(info, res),
            "local": resolution_is_local(root, slug, info, res),
            "current": res == current,
        }
        for res in available_resolutions(info)
    ]


def expected_local_files(context, target_resolution):
    detected = detect_active_asset(context)
    if not detected:
        return []
    slug, _asset_type, _datablock, _trees = detected
    root = library_root(context)
    info = load_info(root, slug)
    if not info:
        return []
    return resolution_files(root, slug, info, target_resolution)
