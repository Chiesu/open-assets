import re
from pathlib import Path

import bpy

_LOD_RE = re.compile(r"^(.*)_LOD(\d+)$", re.IGNORECASE)


def _find_tagged(collection, slug):
    for datablock in collection:
        try:
            if datablock.get("phl_slug") == slug:
                return datablock
        except Exception:
            pass
    return None


def _choose_collection(names, slug, preset):
    lods = []
    for name in names:
        match = _LOD_RE.match(name)
        if match and slug.casefold() in match.group(1).casefold():
            lods.append((int(match.group(2)), name))
    lods.sort(key=lambda item: item[0])
    if lods:
        if preset == "FINAL":
            return lods[0][1]
        if preset == "PREVIEW":
            return lods[-1][1]
        return lods[len(lods) // 2][1]
    for preferred in (f"{slug}_static", slug):
        if preferred in names:
            return preferred
    for name in names:
        if slug.casefold() in name.casefold():
            return name
    return names[0] if names else ""


def _load_collection(blend_path, slug, preset, desired_resolution=""):
    with bpy.data.libraries.load(str(blend_path), link=False) as (data_from, _data_to):
        target = _choose_collection(list(data_from.collections), slug, preset)
    if not target:
        raise RuntimeError(f"No collection found in {blend_path.name}")

    existing = bpy.data.collections.get(target)
    if (
        existing is not None
        and existing.get("phl_slug") == slug
        and (not desired_resolution or str(existing.get("phl_resolution", "")) == str(desired_resolution))
    ):
        return existing

    with bpy.data.libraries.load(str(blend_path), link=False) as (data_from, data_to):
        if target not in data_from.collections:
            raise RuntimeError(f"Collection {target} is missing from {blend_path.name}")
        data_to.collections = [target]
    loaded = data_to.collections[0] if data_to.collections else None
    if loaded is None:
        raise RuntimeError(f"Could not append collection for {slug}")
    return loaded


def _load_material(blend_path, slug, desired_resolution=""):
    existing = _find_tagged(bpy.data.materials, slug)
    if existing is not None and (not desired_resolution or str(existing.get("phl_resolution", "")) == str(desired_resolution)):
        return existing
    with bpy.data.libraries.load(str(blend_path), link=False) as (data_from, data_to):
        names = list(data_from.materials)
        target = slug if slug in names else next((n for n in names if slug.casefold() in n.casefold()), "")
        if not target and names:
            target = names[0]
        if not target:
            raise RuntimeError(f"No material found in {blend_path.name}")
        data_to.materials = [target]
    material = data_to.materials[0] if data_to.materials else None
    if material is None:
        raise RuntimeError(f"Could not append material for {slug}")
    return material


def _load_world(blend_path, slug, desired_resolution=""):
    existing = _find_tagged(bpy.data.worlds, slug)
    if existing is not None and (not desired_resolution or str(existing.get("phl_resolution", "")) == str(desired_resolution)):
        return existing
    with bpy.data.libraries.load(str(blend_path), link=False) as (data_from, data_to):
        names = list(data_from.worlds)
        target = slug if slug in names else next((n for n in names if slug.casefold() in n.casefold()), "")
        if not target and names:
            target = names[0]
        if not target:
            raise RuntimeError(f"No world found in {blend_path.name}")
        data_to.worlds = [target]
    world = data_to.worlds[0] if data_to.worlds else None
    if world is None:
        raise RuntimeError(f"Could not append world for {slug}")
    return world


def _target_mesh(context, target_object_name=""):
    if target_object_name:
        obj = bpy.data.objects.get(target_object_name)
        if obj is not None and getattr(obj, "type", "") == "MESH":
            return obj
    obj = context.active_object
    return obj if obj is not None and getattr(obj, "type", "") == "MESH" else None



def _iter_nodes(tree):
    if tree is None:
        return
    for node in tree.nodes:
        yield node
        child = getattr(node, "node_tree", None)
        if child is not None and child is not tree:
            yield from _iter_nodes(child)


def _validate_texture_material(material, provider):
    """Reject silently broken cached map-based materials.

    The old external builder could save a valid Material datablock with no image
    nodes at all. That is worse than an explicit import error because Blender
    appears to have assigned the material successfully.
    """
    if provider not in {"ambientcg", "cgbookcase"}:
        return
    tree = getattr(material, "node_tree", None)
    image_nodes = [node for node in _iter_nodes(tree) if getattr(node, "type", "") == "TEX_IMAGE"]
    if not image_nodes:
        raise RuntimeError(
            "This cached material contains no texture maps. Re-import it so Open Assets can rebuild the local material cache."
        )
    missing_images = [node.name for node in image_nodes if getattr(node, "image", None) is None]
    if len(missing_images) == len(image_nodes):
        raise RuntimeError("The material texture images could not be resolved from the local asset cache")

def import_local_asset(
    context,
    root,
    slug,
    info,
    preset="WORKING",
    location=None,
    target_object_name="",
):
    root = Path(root)
    blend_path = root / slug / f"{slug}.blend"
    if not blend_path.exists():
        raise RuntimeError(f"Local asset file is missing for {slug}")
    asset_type = int(info.get("type", 0))
    asset_name = str(info.get("name") or slug.replace("_", " ").title())
    provider = str(info.get("provider") or "polyhaven")
    provider_name = str(info.get("provider_name") or "Poly Haven")

    if asset_type == 2:
        collection = _load_collection(blend_path, slug, preset, str(info.get("phl_base_resolution", "")))
        instance = bpy.data.objects.new(collection.name, None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        instance.location = tuple(location) if location is not None else context.scene.cursor.location
        instance["phl_slug"] = slug
        instance["phl_type"] = 2
        instance["phl_provider"] = provider
        instance["phl_source"] = provider_name
        instance["phl_resolution"] = collection.get("phl_resolution", info.get("phl_base_resolution", "1k"))
        instance["phl_lod"] = collection.name
        target_collection = context.collection or context.scene.collection
        target_collection.objects.link(instance)
        for obj in context.selected_objects:
            obj.select_set(False)
        instance.select_set(True)
        context.view_layer.objects.active = instance
        return {
            "message": f"Added {collection.name}",
            "slug": slug,
            "name": asset_name,
            "provider": provider,
            "provider_name": provider_name,
            "asset_type": 2,
            "quality": preset,
            "resolution": str(instance.get("phl_resolution", "1k")),
            "lod": collection.name,
            "object_name": instance.name,
            "location": list(instance.location),
        }

    if asset_type == 1:
        material = _load_material(blend_path, slug, str(info.get("phl_base_resolution", "")))
        _validate_texture_material(material, provider)
        obj = _target_mesh(context, target_object_name)
        if obj is not None:
            if len(obj.data.materials) == 0:
                obj.data.materials.append(material)
                obj.active_material_index = 0
            elif getattr(obj, "active_material_index", -1) >= 0:
                obj.data.materials[obj.active_material_index] = material
            else:
                obj.data.materials.append(material)
            for selected in list(context.selected_objects):
                selected.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            return {
                "message": f"Assigned {material.name} to {obj.name}",
                "slug": slug,
                "name": asset_name,
                "provider": provider,
                "provider_name": provider_name,
                "asset_type": 1,
                "quality": preset,
                "resolution": str(material.get("phl_resolution", info.get("phl_base_resolution", "1k"))),
                "datablock_name": material.name,
                "target_object": obj.name,
            }
        return {
            "message": f"Imported material {material.name}; select a mesh to assign it",
            "slug": slug,
            "name": asset_name,
            "provider": provider,
            "provider_name": provider_name,
            "asset_type": 1,
            "quality": preset,
            "resolution": str(material.get("phl_resolution", info.get("phl_base_resolution", "1k"))),
            "datablock_name": material.name,
            "target_object": "",
        }

    world = _load_world(blend_path, slug, str(info.get("phl_base_resolution", "")))
    context.scene.world = world
    return {
        "message": f"Set world to {world.name}",
        "slug": slug,
        "name": asset_name,
        "provider": provider,
        "provider_name": provider_name,
        "asset_type": 0,
        "quality": preset,
        "resolution": str(world.get("phl_resolution", info.get("phl_base_resolution", "1k"))),
        "datablock_name": world.name,
    }


def replace_material_resolution(root, slug, resolution, old_materials):
    """Load a rebuilt local material version and remap existing material users.

    Used by the multi-provider batch resolution tool. This runs on Blender's main
    thread after the provider download/background build has completed.
    """
    blend_path = Path(root) / slug / f"{slug}.blend"
    if not blend_path.exists():
        raise RuntimeError(f"Local asset file is missing for {slug}")
    old_materials = [mat for mat in old_materials if mat is not None]
    new_material = _load_material(blend_path, slug, str(resolution))

    for old in old_materials:
        if old == new_material:
            continue
        remapped = False
        try:
            old.user_remap(new_material)
            remapped = True
        except Exception:
            pass
        if not remapped:
            for obj in bpy.data.objects:
                slots = getattr(getattr(obj, "data", None), "materials", None)
                if slots is None:
                    continue
                for index, material in enumerate(slots):
                    if material == old:
                        slots[index] = new_material
        try:
            if old.users == 0:
                bpy.data.materials.remove(old)
        except Exception:
            pass
    return new_material
