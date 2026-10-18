bl_info = {
    "name": "Open Assets",
    "description": "Unified browser for free production assets from multiple libraries",
    "author": "Magnetic Works contributors",
    "version": (0, 7, 1),
    "blender": (4, 5, 0),
    "location": "Asset Browser; 3D View > Sidebar > Open Assets",
    "category": "Import-Export",
}

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from . import browser, lifecycle, operators, ui


class PHLPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    library_path: StringProperty(
        name="Asset Library Folder",
        subtype="DIR_PATH",
        default="~/Blender/PolyHavenLibrary",
        description="Local folder registered as the Open Assets library",
    )
    default_resolution: EnumProperty(
        name="Proxy Resolution",
        description="Resolution used when an asset is first synced",
        items=(
            ("1k", "1K", "Small proxy library; recommended"),
            ("2k", "2K", "Higher quality initial proxy"),
            ("4k", "4K", "Large initial library"),
        ),
        default="1k",
    )
    concurrent_assets: IntProperty(
        name="Concurrent Assets",
        description="How many assets may download/build simultaneously",
        default=2,
        min=1,
        max=4,
    )
    verify_ssl: BoolProperty(
        name="Verify SSL Certificates",
        default=True,
        description="Disable only if a managed network causes certificate errors",
    )


    auto_cleanup_deleted_materials: BoolProperty(
        name="Auto-clean Deleted Materials",
        default=False,
        description=(
            "When a managed texture material imported during this session is deleted from the Blender file, "
            "delete its heavy local payload too while keeping metadata and restore settings"
        ),
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "library_path")
        layout.prop(self, "default_resolution")
        layout.prop(self, "concurrent_assets")
        layout.prop(self, "verify_ssl")
        layout.prop(self, "auto_cleanup_deleted_materials")
        layout.separator()
        row = layout.row(align=True)
        row.operator("phl.setup_library", icon="ASSET_MANAGER")
        row.operator("phl.diagnostics", icon="INFO")
        layout.operator("phl.clean_library_cache", icon="TRASH")


CLASSES = (PHLPreferences, browser.PHLBrowserProperties) + operators.CLASSES + ui.CLASSES


def _append_ui():
    targets = (
        ("FILEBROWSER_HT_header", ui.asset_browser_header),
        ("ASSETBROWSER_MT_editor_menus", ui.asset_browser_menu),
    )
    for type_name, draw_fn in targets:
        target = getattr(bpy.types, type_name, None)
        if target is None:
            continue
        try:
            target.append(draw_fn)
        except Exception:
            pass


def _remove_ui():
    targets = (
        ("FILEBROWSER_HT_header", ui.asset_browser_header),
        ("ASSETBROWSER_MT_editor_menus", ui.asset_browser_menu),
    )
    for type_name, draw_fn in targets:
        target = getattr(bpy.types, type_name, None)
        if target is None:
            continue
        try:
            target.remove(draw_fn)
        except Exception:
            pass


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.phl_browser = PointerProperty(type=browser.PHLBrowserProperties)
    browser.register_runtime()
    if lifecycle.depsgraph_watch not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(lifecycle.depsgraph_watch)
    _append_ui()


def unregister():
    _remove_ui()
    try:
        if lifecycle.depsgraph_watch in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(lifecycle.depsgraph_watch)
    except Exception:
        pass
    browser.unregister_runtime()
    state = getattr(bpy.types.WindowManager, "phl_browser", None)
    if state is not None:
        del bpy.types.WindowManager.phl_browser
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
