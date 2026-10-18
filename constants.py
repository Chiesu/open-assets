POLYHAVEN_API = "https://api.polyhaven.com"
AMBIENTCG_API = "https://ambientcg.com/api/v2/full_json"
BLENDKIT_API = "https://www.blenderkit.com/api/v1/search/"
CGBOOKCASE_API = "https://www.cgbookcase.com/api/textures"
KENNEY_BASE = "https://kenney.nl"
LIBRARY_NAME = "Open Assets"
USER_AGENT = "Blender Open Assets/0.7.1"
RUNTIME_STATE_FILE = ".polyhaven_lite_state.json"  # legacy filename kept for upgrade compatibility
CATALOG_NAMESPACE = "6b53af7c-2f8b-4f8c-a1ca-1ce0b9dc8ac8"
RECENT_LIMIT = 20

PROVIDERS = {
    "polyhaven": {"label": "Poly Haven", "badge": "PH"},
    "ambientcg": {"label": "ambientCG", "badge": "ACG"},
    "blendkit": {"label": "Blendkit", "badge": "BK"},
    "cgbookcase": {"label": "CGBookcase", "badge": "CGB"},
    "kenney": {"label": "Kenney", "badge": "KNY"},
}

TYPE_NAMES = {
    0: "HDRIs",
    1: "Textures",
    2: "Models",
}

RESOLUTION_ORDER = ("1k", "2k", "4k", "8k", "16k")

QUALITY_PRESETS = {
    "PREVIEW": {"label": "Preview", "resolution": "1k", "lod": "LOW"},
    "WORKING": {"label": "Working", "resolution": "2k", "lod": "MEDIUM"},
    "FINAL": {"label": "Final", "resolution": "4k", "lod": "HIGH"},
}

# Production-friendly base taxonomy. Provider-specific categories are added dynamically.
CATALOG_PATHS = (
    "HDRIs",
    "HDRIs/Indoor",
    "HDRIs/Outdoor",
    "HDRIs/Studio",
    "Textures",
    "Textures/Nature",
    "Textures/Rock",
    "Textures/Terrain",
    "Textures/Wood",
    "Textures/Brick",
    "Textures/Fabric",
    "Textures/Metal",
    "Textures/Concrete",
    "Models",
    "Models/Props",
    "Models/Industrial",
    "Models/Lighting",
    "Models/Nature",
    "Models/Nature/Plants",
    "Models/Nature/Rocks",
    "Models/Furniture",
)
