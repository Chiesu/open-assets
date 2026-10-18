# Open Assets licensing and compatibility decisions

- Public release **0.7.1** removes CGTrader browsing/download code, including optional authenticated-token entry, because free/royalty-free is not CC0.
- Supported sources: Poly Haven, ambientCG, CC0-filtered Blendkit, CGBookcase, Kenney.
- The Blendkit adapter independently checks returned `cc_zero` licensing and free availability as well as adding server-side filters.
- The add-on source is GPL-3.0-or-later; downloaded 3D models/materials/textures retain their original provider terms. Assets themselves are **not** distributed in the source repository or release ZIP.
- **Internal ID remains `polyhaven_lite`**, by design. Renaming this can break upgrades and existing asset state.
- Provider APIs and their metadata are external dependencies and can change; verify licensing metadata and download behavior again in Blender before publishing.
- The original local catalog/cache can still contain old CGTrader results from a previous version. This release does not actively delete old local user files; new searches do not include CGTrader.
