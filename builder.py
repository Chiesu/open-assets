"""Background Blender helper used by sync.py.

Executed by Blender itself in a separate background process. It turns a downloaded
a Poly Haven source .blend/HDRI into a native Open Assets library item.
"""

import json
import sys
from pathlib import Path

import bpy

TYPE_NAMES = {0: "HDRI", 1: "Texture", 2: "Model"}


def _asset_for_type(slug, asset_type, hdri_file=None):
    if asset_type == 0:
        world = bpy.data.worlds.get(slug) or bpy.data.worlds.new(slug)
        bpy.context.scene.world = world
        world.use_nodes = True
        nt = world.node_tree
        nt.nodes.clear()
        output = nt.nodes.new("ShaderNodeOutputWorld")
        background = nt.nodes.new("ShaderNodeBackground")
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(str(hdri_file), check_existing=True)
        nt.links.new(env.outputs["Color"], background.inputs["Color"])
        nt.links.new(background.outputs["Background"], output.inputs["Surface"])
        return world

    if asset_type == 1:
        if slug in bpy.data.materials:
            return bpy.data.materials[slug]
        if len(bpy.data.materials) == 1:
            material = bpy.data.materials[0]
            material.name = slug
            return material
        for material in bpy.data.materials:
            if slug.lower() in material.name.lower():
                return material
        return bpy.data.materials[0] if bpy.data.materials else None

    preferred = (f"{slug}_LOD0", f"{slug}_static", slug)
    for name in preferred:
        if name in bpy.data.collections:
            return bpy.data.collections[name]
    for collection in bpy.data.collections:
        if slug.lower() in collection.name.lower():
            return collection
    return bpy.data.collections[0] if bpy.data.collections else None


def _tag_datablock(datablock, slug, asset_type, resolution, dimensions, catalog_path):
    if datablock is None:
        return
    datablock["phl_slug"] = slug
    datablock["phl_type"] = int(asset_type)
    datablock["phl_resolution"] = resolution
    datablock["phl_provider"] = "polyhaven"
    datablock["phl_source"] = "Poly Haven"
    datablock["phl_catalog_path"] = catalog_path
    if dimensions:
        datablock["phl_dimensions"] = dimensions


def _propagate_model_metadata(asset, slug, asset_type, resolution, dimensions, catalog_path):
    if not isinstance(asset, bpy.types.Collection):
        return
    for obj in asset.all_objects:
        _tag_datablock(obj, slug, asset_type, resolution, dimensions, catalog_path)
        for slot in getattr(obj, "material_slots", []):
            if slot.material:
                _tag_datablock(slot.material, slug, asset_type, resolution, dimensions, catalog_path)


def _make_paths_relative(out_file):
    out_dir = Path(out_file).parent.resolve()
    for image in bpy.data.images:
        if not image.filepath:
            continue
        try:
            absolute = Path(bpy.path.abspath(image.filepath)).resolve()
            relative = absolute.relative_to(out_dir)
            image.filepath = f"//{relative.as_posix()}"
        except Exception:
            pass


def main():
    argv = sys.argv[sys.argv.index("--") + 1 :]
    if len(argv) != 12:
        raise RuntimeError(f"Expected 12 arguments, got {len(argv)}")

    (
        slug,
        asset_type_raw,
        thumbnail_file,
        authors_json,
        categories_json,
        tags_json,
        dimensions_json,
        resolution,
        hdri_file,
        out_file,
        catalog_id,
        catalog_path,
    ) = argv

    asset_type = int(asset_type_raw)
    authors = json.loads(authors_json)
    categories = json.loads(categories_json)
    tags = json.loads(tags_json)
    dimensions = json.loads(dimensions_json)
    hdri_path = None if hdri_file == "NONE" else Path(hdri_file)

    asset = _asset_for_type(slug, asset_type, hdri_path)
    if asset is None:
        raise RuntimeError(f"Could not locate a datablock for {slug}")

    if asset.asset_data is None:
        asset.asset_mark()

    _tag_datablock(asset, slug, asset_type, resolution, dimensions, catalog_path)
    if asset_type == 2:
        _propagate_model_metadata(asset, slug, asset_type, resolution, dimensions, catalog_path)

    if asset.asset_data:
        asset.asset_data.author = ", ".join(authors) if authors else "Poly Haven contributors"
        asset.asset_data.description = f"{TYPE_NAMES[asset_type]} from Poly Haven"
        asset.asset_data.catalog_id = catalog_id
        for item in list(categories) + list(tags):
            if item:
                try:
                    asset.asset_data.tags.new(str(item), skip_if_exists=True)
                except Exception:
                    pass
        if hasattr(asset.asset_data, "preferred_import_method"):
            asset.asset_data.preferred_import_method = "APPEND"
            asset.asset_data.use_preferred_import_method = True

    if thumbnail_file != "NONE" and Path(thumbnail_file).exists():
        try:
            with bpy.context.temp_override(id=asset):
                bpy.ops.ed.lib_id_load_custom_preview(filepath=thumbnail_file)
        except Exception as exc:
            print(f"WARN: thumbnail preview failed: {exc}")

    _make_paths_relative(out_file)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=out_file, compress=True, relative_remap=False)


if __name__ == "__main__":
    main()
