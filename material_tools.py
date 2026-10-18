import math
from pathlib import Path

import bmesh
import bpy

from .library import library_root, load_info


def active_texture_material(context):
    obj = context.active_object
    mat = getattr(obj, "active_material", None) if obj else None
    if mat and mat.get("phl_slug") and int(mat.get("phl_type", 1)) == 1:
        return obj, mat
    return None, None


def _dimensions_mm(context, material):
    dims = material.get("phl_dimensions")
    if not dims:
        slug = material.get("phl_slug")
        info = load_info(library_root(context), slug) if slug else None
        dims = info.get("dimensions") if info else None
    if not dims or len(dims) < 2:
        raise RuntimeError("This texture has no physical size metadata")
    try:
        width = float(dims[0])
        height = float(dims[1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid physical size metadata") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError("Texture physical size must be greater than zero")
    return width, height


def _polygon_uv_area(points):
    if len(points) < 3:
        return 0.0
    area = 0.0
    for idx, current in enumerate(points):
        nxt = points[(idx + 1) % len(points)]
        area += current.x * nxt.y - nxt.x * current.y
    return abs(area) * 0.5


def _areas(obj):
    if obj.type != "MESH":
        raise RuntimeError("Real scale requires an active mesh object")
    if not obj.data.uv_layers or obj.data.uv_layers.active is None:
        raise RuntimeError("The active object has no UV map")

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.transform(obj.matrix_world)
        surface = sum(face.calc_area() for face in bm.faces)
    finally:
        bm.free()

    uv_layer = obj.data.uv_layers.active.data
    uv_area = 0.0
    for poly in obj.data.polygons:
        points = [uv_layer[loop_index].uv for loop_index in poly.loop_indices]
        uv_area += _polygon_uv_area(points)

    if surface <= 0 or uv_area <= 0:
        raise RuntimeError("Cannot calculate a usable surface/UV area for this object")
    return surface, uv_area


def _set_mapping_scale(mapping, x, y):
    scale = mapping.inputs.get("Scale")
    if scale is None:
        raise RuntimeError("Blender Mapping node has no Scale input")
    scale.default_value[0] = x
    scale.default_value[1] = y
    scale.default_value[2] = 1.0


def apply_real_scale(context):
    obj, material = active_texture_material(context)
    if material is None:
        raise RuntimeError("Select a mesh using an imported managed texture material")
    width_mm, height_mm = _dimensions_mm(context, material)
    surface_area, uv_area = _areas(obj)

    tex_width = width_mm / 1000.0
    tex_height = height_mm / 1000.0
    tex_area = tex_width * tex_height
    aspect = tex_height / tex_width
    root_multiplier = math.sqrt(surface_area / (tex_area * uv_area))
    aspect_root = math.sqrt(aspect)
    scale_x = root_multiplier * aspect_root
    scale_y = root_multiplier / aspect_root

    tree = material.node_tree
    if tree is None:
        raise RuntimeError("Material has no node tree")

    changed = 0
    for image_node in [node for node in tree.nodes if node.type == "TEX_IMAGE"]:
        vector_input = image_node.inputs.get("Vector")
        if vector_input is None:
            continue

        existing = None
        if vector_input.is_linked:
            source = vector_input.links[0].from_node
            if source.type == "MAPPING" and source.get("phl_real_scale"):
                existing = source

        mapping = existing or tree.nodes.new("ShaderNodeMapping")
        mapping.name = mapping.name if existing else f"PHL Real Scale - {image_node.name}"
        mapping.label = "PHL Real Scale"
        mapping["phl_real_scale"] = True
        mapping.location = (image_node.location.x - 220, image_node.location.y)
        _set_mapping_scale(mapping, scale_x, scale_y)

        if existing is None:
            source_socket = None
            if vector_input.is_linked:
                old_link = vector_input.links[0]
                source_socket = old_link.from_socket
                tree.links.remove(old_link)
            else:
                uv = tree.nodes.new("ShaderNodeUVMap")
                uv.name = f"PHL UV - {image_node.name}"
                uv.label = "PHL UV"
                uv["phl_real_scale"] = True
                uv.location = (mapping.location.x - 180, mapping.location.y)
                source_socket = uv.outputs.get("UV")
            if source_socket is not None:
                tree.links.new(source_socket, mapping.inputs["Vector"])
            tree.links.new(mapping.outputs["Vector"], vector_input)
        changed += 1

    if not changed:
        raise RuntimeError("No image texture nodes were found in this material")

    material["phl_real_scale_applied"] = True
    material["phl_real_scale_x"] = scale_x
    material["phl_real_scale_y"] = scale_y
    return changed, scale_x, scale_y


def _height_image_node(material):
    candidates = []
    for node in material.node_tree.nodes:
        if node.type != "TEX_IMAGE" or node.image is None:
            continue
        label = " ".join((node.name, node.label, node.image.name, Path(node.image.filepath).name)).casefold()
        score = 0
        if "displacement" in label:
            score += 5
        if "disp" in label:
            score += 4
        if "height" in label:
            score += 3
        if score:
            candidates.append((score, node))
    if not candidates:
        raise RuntimeError("No displacement/height image could be identified in this material")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _material_output(material):
    for node in material.node_tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and getattr(node, "is_active_output", True):
            return node
    return next((node for node in material.node_tree.nodes if node.type == "OUTPUT_MATERIAL"), None)


def _principled(material):
    return next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)


def _set_material_displacement(material, mode):
    values = ("BUMP",) if mode == "BUMP" else ("DISPLACEMENT", "BOTH")
    for owner in (material, getattr(material, "cycles", None)):
        if owner is None or not hasattr(owner, "displacement_method"):
            continue
        for value in values:
            try:
                owner.displacement_method = value
                return True
            except Exception:
                continue
    return False


def _ensure_subsurf(obj, levels=2):
    modifier = next((m for m in obj.modifiers if m.type == "SUBSURF" and m.get("phl_displacement")), None)
    if modifier is None:
        modifier = obj.modifiers.new("PHL Tessellation", "SUBSURF")
        modifier["phl_displacement"] = True
    modifier.subdivision_type = "SIMPLE"
    modifier.levels = min(levels, 3)
    modifier.render_levels = levels
    return modifier


def setup_displacement(context, mode="BUMP", strength=0.1, midlevel=0.5, subdivisions=2):
    obj = context.active_object
    material = getattr(obj, "active_material", None) if obj else None
    if obj is None or material is None or not material.use_nodes:
        raise RuntimeError("Select a mesh with an active node material")

    tree = material.node_tree
    height = _height_image_node(material)

    if mode == "BUMP":
        principled = _principled(material)
        if principled is None:
            raise RuntimeError("No Principled BSDF found for bump setup")
        normal = principled.inputs.get("Normal")
        existing = normal.links[0].from_node if normal and normal.is_linked else None
        if existing is not None and not existing.get("phl_displacement"):
            raise RuntimeError("Principled Normal is already driven by a custom node; refusing to overwrite it")
        bump = existing if existing and existing.type == "BUMP" else tree.nodes.new("ShaderNodeBump")
        bump.name = "PHL Bump"
        bump.label = "PHL Bump"
        bump["phl_displacement"] = True
        bump.inputs["Strength"].default_value = strength
        bump.inputs["Distance"].default_value = max(0.0001, strength)
        if not bump.inputs["Height"].is_linked:
            tree.links.new(height.outputs["Color"], bump.inputs["Height"])
        if normal and not normal.is_linked:
            tree.links.new(bump.outputs["Normal"], normal)
        _set_material_displacement(material, "BUMP")
        material["phl_displacement_mode"] = "BUMP"
        material["phl_displacement_strength"] = float(strength)
        material["phl_displacement_midlevel"] = float(midlevel)
        material["phl_displacement_subdivisions"] = int(subdivisions)
        return "BUMP"

    output = _material_output(material)
    if output is None:
        raise RuntimeError("Material Output node not found")
    disp_input = output.inputs.get("Displacement")
    existing = disp_input.links[0].from_node if disp_input and disp_input.is_linked else None
    if existing is not None and existing.type != "DISPLACEMENT" and not existing.get("phl_displacement"):
        raise RuntimeError("Material Displacement is already driven by a custom node; refusing to overwrite it")

    displacement = existing if existing and existing.type == "DISPLACEMENT" else tree.nodes.new("ShaderNodeDisplacement")
    displacement.name = "PHL Displacement"
    displacement.label = "PHL Displacement"
    displacement["phl_displacement"] = True
    displacement.inputs["Scale"].default_value = strength
    displacement.inputs["Midlevel"].default_value = midlevel
    if not displacement.inputs["Height"].is_linked:
        tree.links.new(height.outputs["Color"], displacement.inputs["Height"])
    if disp_input and not disp_input.is_linked:
        tree.links.new(displacement.outputs["Displacement"], disp_input)

    _set_material_displacement(material, mode)
    _ensure_subsurf(obj, levels=subdivisions)

    if mode == "ADAPTIVE":
        cycles = getattr(obj, "cycles", None)
        if cycles is not None and hasattr(cycles, "use_adaptive_subdivision"):
            cycles.use_adaptive_subdivision = True
        if hasattr(context.scene, "cycles") and hasattr(context.scene.cycles, "feature_set"):
            try:
                context.scene.cycles.feature_set = "EXPERIMENTAL"
            except Exception:
                pass

    material["phl_displacement_mode"] = mode
    material["phl_displacement_strength"] = float(strength)
    material["phl_displacement_midlevel"] = float(midlevel)
    material["phl_displacement_subdivisions"] = int(subdivisions)
    return mode
