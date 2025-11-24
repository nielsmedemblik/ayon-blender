import json
from pathlib import Path
from typing import List, Optional

import bpy
import pyblish.api

from ayon_core.pipeline import publish
from ayon_core.pipeline.publish import KnownPublishError

from ayon_blender.api import plugin, lib
from ayon_blender.api.look import slugify_name


class ExtractLook(
    plugin.BlenderExtractor, publish.OptionalPyblishPluginMixin
):
    """Export material resources and cbId manifest."""

    label = "Extract Look"
    hosts = ["blender"]
    families = ["look"]
    optional = False

    def process(self, instance):
        if not self.is_active(instance.data):
            return

        look_data = instance.data.get("lookData") or {}
        materials: List[bpy.types.Material] = look_data.get("materials") or []
        assignments: List[Dict] = look_data.get("assignments") or []

        if not materials or not assignments:
            raise KnownPublishError(
                "Look instance is missing collected materials/assignments."
            )

        stagingdir = Path(self.staging_dir(instance))

        exported_materials = {}
        manifest = []
        for assignment in assignments:
            material_name = assignment["material_name"]
            export_info = exported_materials.get(material_name)
            if export_info is None:
                export_info = {}
                exported_materials[material_name] = export_info

            entry = {
                "object_name": assignment["object_name"],
                "material_name": material_name,
            }
            if assignment.get("cbid"):
                entry["cbid"] = assignment["cbid"]
            manifest.append(entry)

        folder_name = instance.data["folderEntity"]["name"]
        product_name = instance.data["productName"]
        manifest_name = f"{folder_name}_{product_name}.json"
        manifest_path = stagingdir / manifest_name

        with manifest_path.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)

        materials_filename = self._export_material_library(
            materials, stagingdir, folder_name, product_name
        )

        representation = {
            "name": "json",
            "ext": "json",
            "files": manifest_name,
            "stagingDir": str(stagingdir),
        }
        instance.data.setdefault("representations", []).append(representation)

        resources_repr = {
            "name": "materials",
            "ext": "blend",
            "files": materials_filename,
            "stagingDir": str(stagingdir),
        }
        instance.data["representations"].append(resources_repr)

    def _export_material_library(
        self,
        materials: List[bpy.types.Material],
        staging_dir: Path,
        folder_name: str,
        product_name: str,
    ) -> str:
        safe_name = slugify_name(f"{folder_name}_{product_name}_materials")
        blend_filename = f"{safe_name}.blend"
        blend_path = staging_dir / blend_filename

        data_blocks: set = set(materials)
        data_blocks.update(self._collect_material_images(materials))

        bpy.data.libraries.write(str(blend_path), data_blocks, compress=False)
        return blend_filename

    def _collect_material_images(
        self, materials: List[bpy.types.Material]
    ) -> set[bpy.types.Image]:
        images = set()
        for material in materials:
            if not material or not material.use_nodes:
                continue
            tree = material.node_tree
            if not tree:
                continue
            for node in tree.nodes:
                if node.bl_idname != 'ShaderNodeTexImage':
                    continue
                if node.image:
                    images.add(node.image)
        return images
