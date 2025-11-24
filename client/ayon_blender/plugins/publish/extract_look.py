import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import bpy
import pyblish.api
from bpy.path import abspath

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
        resources_dir = stagingdir / "resources"
        resources_dir.mkdir(parents=True, exist_ok=True)

        exported_materials = {}
        resource_files = set()
        for material in materials:
            export_info = self._export_material(material, resources_dir)
            exported_materials[material.name_full] = export_info
            resource_files.update(export_info["files"])

        manifest = []
        for assignment in assignments:
            material_name = assignment["material_name"]
            export_info = exported_materials.get(material_name)
            if not export_info:
                self.log.warning(
                    "Material '%s' missing export info, skipping assignment.",
                    material_name,
                )
                continue

            entry = {
                "object_name": assignment["object_name"],
                "material_name": material_name,
                "fbx_filename": export_info["fbx_filename"],
            }
            if assignment.get("cbid"):
                entry["cbid"] = assignment["cbid"]
            if export_info.get("texture_filename"):
                entry["tga_filename"] = export_info["texture_filename"]
            manifest.append(entry)

        folder_name = instance.data["folderEntity"]["name"]
        product_name = instance.data["productName"]
        manifest_name = f"{folder_name}_{product_name}.json"
        manifest_path = stagingdir / manifest_name

        with manifest_path.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)

        files = [manifest_name]
        files.extend(sorted(resource_files))

        representation = {
            "name": "json",
            "ext": "json",
            "files": files,
            "stagingDir": str(stagingdir),
        }
        instance.data.setdefault("representations", []).append(representation)

    def _export_material(
        self, material: bpy.types.Material, resources_dir: Path
    ) -> Dict[str, Optional[str]]:
        safe_name = slugify_name(material.name)

        fbx_filename = self._unique_filename(resources_dir, f"{safe_name}.fbx")
        fbx_path = resources_dir / fbx_filename

        mesh = bpy.data.meshes.new(name=f"{safe_name}_mesh")
        mesh.from_pydata(
            points=[(0, 0, 0), (0, 1, 0), (1, 0, 0)],
            edges=[],
            faces=[(0, 1, 2)],
        )
        temp_obj = bpy.data.objects.new(f"{safe_name}_material", mesh)
        mesh.materials.append(material)
        bpy.context.scene.collection.objects.link(temp_obj)

        with lib.maintained_selection():
            plugin.deselect_all()
            temp_obj.select_set(True)
            bpy.context.view_layer.objects.active = temp_obj
            override = plugin.create_blender_context(
                active=temp_obj, selected=[temp_obj]
            )
            with bpy.context.temp_override(**override):
                bpy.ops.export_scene.fbx(
                    filepath=str(fbx_path),
                    use_selection=True,
                    object_types={'MESH'},
                    use_active_collection=False,
                    add_leaf_bones=False,
                    bake_anim=False,
                    apply_unit_scale=True,
                    apply_scale_options='FBX_SCALE_UNITS',
                )

        bpy.context.scene.collection.objects.unlink(temp_obj)
        bpy.data.objects.remove(temp_obj)
        bpy.data.meshes.remove(mesh)

        texture_filename = self._export_material_texture(
            material, resources_dir, safe_name
        )

        files = [os.path.join("resources", fbx_filename)]
        if texture_filename:
            files.append(os.path.join("resources", texture_filename))

        return {
            "fbx_filename": fbx_filename,
            "texture_filename": texture_filename,
            "files": files,
        }

    def _unique_filename(self, directory: Path, filename: str) -> str:
        name = Path(filename).stem
        suffix = Path(filename).suffix or ""
        candidate = filename
        index = 1
        while (directory / candidate).exists():
            candidate = f"{name}_{index:02d}{suffix}"
            index += 1
        return candidate

    def _export_material_texture(
        self, material: bpy.types.Material, resources_dir: Path, safe_name: str
    ) -> Optional[str]:
        image = self._get_base_color_image(material)
        if image is None:
            return None

        src_path = Path(abspath(image.filepath)) if image.filepath else None
        extension = (
            src_path.suffix if src_path and src_path.suffix else ".png"
        )
        texture_filename = self._unique_filename(
            resources_dir, f"{safe_name}{extension}"
        )
        destination = resources_dir / texture_filename

        if image.packed_file or not src_path or not src_path.exists():
            image.filepath_raw = str(destination)
            image.save_render(filepath=str(destination))
        else:
            shutil.copy2(src_path, destination)

        return texture_filename

    def _get_base_color_image(
        self, material: bpy.types.Material
    ) -> Optional[bpy.types.Image]:
        if not material.use_nodes:
            return None
        tree = material.node_tree
        if not tree:
            return None
        principled = next(
            (node for node in tree.nodes if node.type == 'BSDF_PRINCIPLED'),
            None,
        )
        if not principled:
            return None
        color_input = principled.inputs.get("Base Color")
        if not color_input or not color_input.links:
            return None
        image_node = color_input.links[0].from_node
        if image_node.bl_idname != 'ShaderNodeTexImage':
            return None
        return image_node.image
