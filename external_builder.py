"""Background Blender builder for non-Poly-Haven provider assets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


def _argv():
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :]
    if len(argv) != 6:
        raise RuntimeError("Expected info_path out_file thumbnail source_file catalog_id catalog_path")
    return argv


def _candidate_material(name):
    lowered = name.casefold()
    for material in bpy.data.materials:
        if lowered in material.name.casefold() or material.name.casefold() in lowered:
            return material
    return bpy.data.materials[0] if bpy.data.materials else None


def _candidate_collection(name):
    lowered = name.casefold()
    candidates = list(bpy.data.collections)
    for collection in candidates:
        if lowered in collection.name.casefold() or collection.name.casefold() in lowered:
            return collection
    candidates.sort(key=lambda c: len(c.all_objects), reverse=True)
    return candidates[0] if candidates else None


def _candidate_world(name):
    lowered = name.casefold()
    for world in bpy.data.worlds:
        if lowered in world.name.casefold() or world.name.casefold() in lowered:
            return world
    return bpy.data.worlds[0] if bpy.data.worlds else None


def _image_node(nodes, path, label, noncolor=False):
    image = bpy.data.images.load(str(path), check_existing=True)
    if noncolor:
        try:
            image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
    node = nodes.new("ShaderNodeTexImage")
    node.image = image
    node.label = label
    node.name = f"OA_{label}"
    return node


def _find_map(files, terms, exclude=()):
    candidates = []
    for path in files:
        name = path.name.casefold()
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".webp", ".tga", ".bmp"}:
            continue
        if any(term in name for term in terms) and not any(term in name for term in exclude):
            candidates.append(path)
    candidates.sort(key=lambda p: (len(p.name), p.name.casefold()))
    return candidates[0] if candidates else None


def _build_material(slug, info, source_path):
    # Blendkit material source .blend is already open in the background process.
    if str(info.get("provider")) == "blendkit" and bpy.data.materials:
        material = _candidate_material(str(info.get("name") or slug))
        if material:
            material.name = slug
            return material

    root = Path(source_path) if source_path and source_path.exists() else Path(info.get("_payload", "."))
    files = list(root.rglob("*")) if root.is_dir() else []
    material = bpy.data.materials.new(slug)
    material.use_nodes = True
    nt = material.node_tree
    nt.nodes.clear()
    nodes = nt.nodes
    links = nt.links
    output = nodes.new("ShaderNodeOutputMaterial")
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (0, 0)
    output.location = (420, 0)
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    color = _find_map(files, ("basecolor", "base_color", "base-color", "albedo", "diffuse", "_color", "_col"), ("normal", "rough", "metal", "height", "opacity"))
    rough = _find_map(files, ("roughness", "rough"))
    metal = _find_map(files, ("metalness", "metallic"))
    provider = str(info.get("provider") or "")
    normal_is_dx = False
    if provider == "cgbookcase":
        # CGBookcase publishes DirectX normals. Blender expects OpenGL normals,
        # so the green channel is inverted below before the Normal Map node.
        normal = _find_map(files, ("normal",), ("normalgl", "normal_gl"))
        normal_is_dx = bool(normal)
    else:
        normal = _find_map(files, ("normalgl", "normal_gl", "normal"), ("normaldx", "normal_dx", "directx"))
    height = _find_map(files, ("displacement", "height", "disp"))
    opacity = _find_map(files, ("opacity", "alpha"))
    ao = _find_map(files, ("ambientocclusion", "ambient_occlusion", "_ao", "ao_"))

    if not any((color, rough, metal, normal, height, opacity, ao)):
        raise RuntimeError(f"No usable PBR texture maps were found for {info.get('name') or slug}")

    y = 260
    if color:
        node = _image_node(nodes, color, "Base Color")
        node.location = (-760, y)
        if ao:
            ao_node = _image_node(nodes, ao, "Ambient Occlusion", noncolor=True)
            ao_node.location = (-760, y - 180)
            mix = nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.inputs[0].default_value = 1.0
            mix.location = (-340, y)
            links.new(node.outputs["Color"], mix.inputs[1])
            links.new(ao_node.outputs["Color"], mix.inputs[2])
            links.new(mix.outputs["Color"], principled.inputs["Base Color"])
        else:
            links.new(node.outputs["Color"], principled.inputs["Base Color"])
        y -= 220
    if rough:
        node = _image_node(nodes, rough, "Roughness", noncolor=True)
        node.location = (-650, y)
        links.new(node.outputs["Color"], principled.inputs["Roughness"])
        y -= 220
    if metal:
        node = _image_node(nodes, metal, "Metallic", noncolor=True)
        node.location = (-650, y)
        links.new(node.outputs["Color"], principled.inputs["Metallic"])
        y -= 220
    if normal:
        tex = _image_node(nodes, normal, "Normal", noncolor=True)
        tex.location = (-760, y)
        normal_node = nodes.new("ShaderNodeNormalMap")
        normal_node.name = "OA_Normal"
        normal_node.location = (-260, y)
        if normal_is_dx:
            separate = nodes.new("ShaderNodeSeparateColor")
            separate.mode = "RGB"
            separate.location = (-560, y)
            invert_g = nodes.new("ShaderNodeMath")
            invert_g.operation = "SUBTRACT"
            invert_g.inputs[0].default_value = 1.0
            invert_g.location = (-390, y - 40)
            combine = nodes.new("ShaderNodeCombineColor")
            combine.mode = "RGB"
            combine.location = (-120, y - 10)
            links.new(tex.outputs["Color"], separate.inputs["Color"])
            links.new(separate.outputs["Red"], combine.inputs["Red"])
            links.new(separate.outputs["Green"], invert_g.inputs[1])
            links.new(invert_g.outputs[0], combine.inputs["Green"])
            links.new(separate.outputs["Blue"], combine.inputs["Blue"])
            links.new(combine.outputs["Color"], normal_node.inputs["Color"])
            normal_node.location = (100, y)
        else:
            links.new(tex.outputs["Color"], normal_node.inputs["Color"])
        links.new(normal_node.outputs["Normal"], principled.inputs["Normal"])
        y -= 220
    if height:
        tex = _image_node(nodes, height, "Height", noncolor=True)
        tex.location = (-650, y)
        disp = nodes.new("ShaderNodeDisplacement")
        disp.name = "OA_Displacement"
        disp.location = (120, -260)
        disp.inputs["Scale"].default_value = 0.1
        links.new(tex.outputs["Color"], disp.inputs["Height"])
        links.new(disp.outputs["Displacement"], output.inputs["Displacement"])
    if opacity:
        tex = _image_node(nodes, opacity, "Opacity", noncolor=True)
        tex.location = (-650, y - 220)
        links.new(tex.outputs["Color"], principled.inputs["Alpha"])
    return material


def _import_model_file(source_path):
    source_path = Path(source_path)
    before = {obj.as_pointer() for obj in bpy.data.objects}
    suffix = source_path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(source_path))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(source_path))
    elif suffix == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(source_path))
        else:
            bpy.ops.import_scene.obj(filepath=str(source_path))
    else:
        raise RuntimeError(f"Unsupported model format: {suffix}")
    return [obj for obj in bpy.data.objects if obj.as_pointer() not in before]



def _pack_model_files(root):
    root = Path(root)
    groups = (
        list(root.rglob("*.glb")),
        list(root.rglob("*.gltf")),
        list(root.rglob("*.fbx")),
        list(root.rglob("*.obj")),
    )
    for files in groups:
        files = [p for p in files if p.is_file()]
        if files:
            files.sort(key=lambda p: str(p).casefold())
            return files
    return []


def _build_model_pack(slug, info, source_path):
    files = _pack_model_files(source_path)
    if not files:
        raise RuntimeError("No supported model files were found in the Kenney pack")

    pack = bpy.data.collections.new(slug)
    bpy.context.scene.collection.children.link(pack)
    children = []
    # Kenney packs can contain many models. A generous guard prevents a malformed
    # archive from turning background conversion into an unbounded import.
    for source in files[:500]:
        try:
            imported = _import_model_file(source)
        except Exception as exc:
            print(f"WARN: Failed to import {source}: {exc}")
            continue
        if not imported:
            continue
        name = source.stem.replace("_", " ").strip() or "Model"
        child = bpy.data.collections.new(name)
        pack.children.link(child)
        for obj in imported:
            if obj.name not in child.objects:
                child.objects.link(obj)
            for owner in list(obj.users_collection):
                if owner != child:
                    try:
                        owner.objects.unlink(obj)
                    except Exception:
                        pass
        children.append(child)
    if not children:
        raise RuntimeError("Kenney pack contained no importable models")
    info["pack_asset_count"] = len(children)
    return pack, children

def _build_model(slug, info, source_path):
    if bpy.data.collections and source_path and Path(source_path).suffix.lower() == ".blend":
        collection = _candidate_collection(str(info.get("name") or slug))
        if collection:
            collection.name = slug
            return collection

    imported = _import_model_file(source_path)
    if not imported:
        raise RuntimeError("No objects were imported from the provider model")
    collection = bpy.data.collections.new(slug)
    bpy.context.scene.collection.children.link(collection)
    for obj in imported:
        if obj.name not in collection.objects:
            collection.objects.link(obj)
        for owner in list(obj.users_collection):
            if owner != collection:
                try:
                    owner.objects.unlink(obj)
                except Exception:
                    pass
    return collection


def _build_world(slug, info, source_path):
    path = Path(source_path) if source_path else None
    if path and path.suffix.lower() in {".hdr", ".exr"}:
        world = bpy.data.worlds.get(slug) or bpy.data.worlds.new(slug)
        world.use_nodes = True
        nt = world.node_tree
        nt.nodes.clear()
        output = nt.nodes.new("ShaderNodeOutputWorld")
        background = nt.nodes.new("ShaderNodeBackground")
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(str(path), check_existing=True)
        nt.links.new(env.outputs["Color"], background.inputs["Color"])
        nt.links.new(background.outputs["Background"], output.inputs["Surface"])
        bpy.context.scene.world = world
        return world
    world = _candidate_world(str(info.get("name") or slug))
    if world:
        world.name = slug
        return world
    raise RuntimeError("No world/HDR image found in provider asset")


def _tag(datablock, slug, info, resolution, catalog_path):
    datablock["phl_slug"] = slug
    datablock["phl_type"] = int(info.get("type", 0))
    datablock["phl_resolution"] = resolution
    datablock["phl_provider"] = str(info.get("provider") or "")
    datablock["phl_source"] = str(info.get("provider_name") or info.get("provider") or "Open Assets")
    datablock["phl_catalog_path"] = catalog_path
    if info.get("dimensions"):
        datablock["phl_dimensions"] = info["dimensions"]


def _mark_asset(asset, info, thumbnail, catalog_id):
    asset.asset_mark()
    if getattr(asset, "asset_data", None):
        asset.asset_data.catalog_id = catalog_id
        asset.asset_data.author = str(info.get("provider_name") or "Open Assets")
        asset.asset_data.description = str(info.get("name") or asset.name)
        for tag in list(info.get("categories") or [])[:12] + list(info.get("tags") or [])[:20]:
            try:
                asset.asset_data.tags.new(str(tag), skip_if_exists=True)
            except Exception:
                pass
        if hasattr(asset.asset_data, "preferred_import_method"):
            try:
                asset.asset_data.preferred_import_method = "APPEND"
                asset.asset_data.use_preferred_import_method = True
            except Exception:
                pass
    if thumbnail and Path(thumbnail).exists():
        try:
            with bpy.context.temp_override(id=asset):
                bpy.ops.ed.lib_id_load_custom_preview(filepath=str(thumbnail))
        except Exception:
            pass


def _make_relative(out_file):
    out_file = Path(out_file)
    for image in bpy.data.images:
        if not image.filepath:
            continue
        try:
            absolute = Path(bpy.path.abspath(image.filepath)).resolve()
            relative = absolute.relative_to(out_file.parent.resolve())
            image.filepath = f"//{relative.as_posix()}"
        except Exception:
            pass


def main():
    info_path, out_file, thumbnail, source_file, catalog_id, catalog_path = _argv()
    info_path = Path(info_path)
    out_file = Path(out_file)
    info = json.loads(info_path.read_text(encoding="utf-8"))
    slug = info_path.parent.name
    resolution = str(info.get("phl_base_resolution") or "1k")
    source = None if source_file == "NONE" else Path(source_file)
    thumb = None if thumbnail == "NONE" else Path(thumbnail)
    asset_type = int(info.get("type", 0))

    extra_assets = []
    if asset_type == 0:
        asset = _build_world(slug, info, source)
    elif asset_type == 1:
        asset = _build_material(slug, info, source)
    elif str(info.get("provider") or "") == "kenney" and source and source.is_dir():
        asset, extra_assets = _build_model_pack(slug, info, source)
    else:
        asset = _build_model(slug, info, source)
    if asset is None:
        raise RuntimeError("Provider asset did not produce a Blender datablock")

    _tag(asset, slug, info, resolution, catalog_path)
    _mark_asset(asset, info, thumb, catalog_id)
    for child in extra_assets:
        _tag(child, slug, info, resolution, catalog_path)
        _mark_asset(child, info, thumb, catalog_id)
    _make_relative(out_file)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(out_file), compress=True, relative_remap=False)


if __name__ == "__main__":
    main()
