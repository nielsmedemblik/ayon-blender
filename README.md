# Blender addon
Blender integration for AYON.

## Look workflow

- Use the new `Look` creator to author instances with the shaded geometry you want to capture.
- Publish the look to generate a JSON manifest plus a `*.blend` material library (no intermediate FBX); meshes must carry a `cbId` custom property so they can be re-identified later.
- Load the published look by selecting the target assets in Blender and running the `Load Look` loader. Materials are assigned by matching `cbId`s (with name matching as a fallback) and texture paths are remapped to the packaged resources.
