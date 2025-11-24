"""Load a model asset in Blender."""

from collections import defaultdict
from pathlib import Path
from pprint import pformat
from typing import Dict, List, Optional

import os
import json
import bpy

from ayon_blender.api import plugin, lib
from ayon_blender.api.pipeline import containerise_existing
from ayon_blender.api.constants import (
    AYON_PROPERTY,
    VALID_EXTENSIONS,
)
from ayon_blender.api.look import get_cbid_from_node


class BlendLookLoader(plugin.BlenderLoader):
    """Load models from a .blend file.

    Because they come from a .blend file we can simply link the collection that
    contains the model. There is no further need to 'containerise' it.
    """

    product_types = {"look"}
    representations = {"json"}

    label = "Load Look"
    icon = "code-fork"
    color = "orange"

    def get_all_children(self, obj):
        children = list(obj.children)

        for child in children:
            children.extend(child.children)

        return children

    def _process(self, libpath, container_name, objects):
        with open(libpath, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        base_path = os.path.dirname(libpath)
        materials_path = os.path.join(base_path, "resources")

        target_meshes = self._gather_target_meshes(objects)
        cbid_to_meshes = defaultdict(list)
        name_to_meshes = defaultdict(list)
        for mesh in target_meshes:
            cbid = get_cbid_from_node(mesh)
            if cbid:
                cbid_to_meshes[cbid].append(mesh)
            base_name = mesh.name.split(':')[0]
            name_to_meshes[base_name].append(mesh)

        material_cache = {}
        imported_materials = []

        for entry in data:
            material = self._material_from_entry(
                entry, materials_path, container_name, material_cache, imported_materials
            )
            if material is None:
                continue

            assigned = False
            cbid = entry.get("cbid")
            if cbid:
                assigned = self._assign_material(
                    cbid_to_meshes.get(str(cbid), []), material
                )

            if not assigned:
                target_name = entry.get("object_name") or entry.get("material_name")
                if target_name:
                    base = target_name.split(':')[0]
                    assigned = self._assign_material(
                        name_to_meshes.get(base, []), material
                    )

            if not assigned:
                fallback_name = material.name.split(':')[0]
                self._assign_material(
                    name_to_meshes.get(fallback_name, []), material
                )

        return imported_materials, objects

    def _material_from_entry(
        self,
        entry: Dict,
        materials_path: str,
        container_name: str,
        cache: Dict[str, bpy.types.Material],
        imported_materials: List[bpy.types.Material],
    ) -> Optional[bpy.types.Material]:
        fbx_filename = entry.get("fbx_filename")
        if not fbx_filename:
            self.log.warning("Look entry missing fbx filename: %s", entry)
            return None

        material = cache.get(fbx_filename)
        if material is None:
            fbx_path = os.path.join(materials_path, fbx_filename)
            if not os.path.exists(fbx_path):
                self.log.error("Look resource not found: %s", fbx_path)
                return None
            material = self._import_material(fbx_path, container_name)
            cache[fbx_filename] = material
            imported_materials.append(material)

        texture_file = entry.get("tga_filename")
        if texture_file:
            texture_path = os.path.join(materials_path, texture_file)
            self._assign_texture(material, texture_path)

        return material

    def _import_material(
        self,
        filepath: str,
        container_name: str,
    ) -> bpy.types.Material:
        with lib.maintained_selection():
            plugin.deselect_all()
            bpy.ops.import_scene.fbx(filepath=filepath)
            imported_meshes = [
                obj for obj in bpy.context.selected_objects
                if obj.type == 'MESH'
            ]
            if not imported_meshes:
                raise RuntimeError(f"FBX import did not create a mesh: {filepath}")
            mesh = imported_meshes[0]
            if not mesh.data.materials:
                raise RuntimeError(
                    f"No materials found in imported mesh from {filepath}"
                )
            material = mesh.data.materials[0]
            base_name = material.name.split(':')[0]
            material.name = f"{base_name}:{container_name}"
            bpy.data.objects.remove(mesh)
        return material

    def _assign_texture(self, material: bpy.types.Material, texture_path: str):
        if not os.path.exists(texture_path):
            self.log.warning("Look texture missing: %s", texture_path)
            return

        if not material.use_nodes or material.node_tree is None:
            return

        node_tree = material.node_tree
        principled = node_tree.nodes.get('Principled BSDF')
        if not principled:
            return
        base_color = principled.inputs.get("Base Color")
        if not base_color or not base_color.links:
            return
        tex_node = base_color.links[0].from_node
        if tex_node.bl_idname != 'ShaderNodeTexImage' or tex_node.image is None:
            return

        tex_node.image.filepath = texture_path
        tex_node.image.reload()

    def _assign_material(
        self, meshes: Optional[List[bpy.types.Object]], material: bpy.types.Material
    ) -> bool:
        if not meshes:
            return False

        assigned = False
        for mesh in meshes:
            if not isinstance(mesh, bpy.types.Object) or mesh.type != 'MESH':
                continue
            mesh.data.materials.clear()
            mesh.data.materials.append(material)
            assigned = True
        return assigned

    def _gather_target_meshes(self, objects: List[bpy.types.Object]) -> List[bpy.types.Object]:
        meshes = []
        seen = set()
        for obj in objects:
            if not isinstance(obj, bpy.types.Object):
                continue
            for candidate in self._iter_object_and_children(obj):
                if candidate.type != 'MESH':
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                meshes.append(candidate)
        return meshes

    def _iter_object_and_children(self, obj: bpy.types.Object):
        yield obj
        for child in obj.children_recursive:
            yield child

    def process_asset(
        self, context: dict, name: str, namespace: Optional[str] = None,
        options: Optional[Dict] = None
    ) -> Optional[List]:
        """
        Arguments:
            name: Use pre-defined name
            namespace: Use pre-defined namespace
            context: Full parenthood of representation to load
            options: Additional settings dictionary
        """

        libpath = self.filepath_from_context(context)
        folder_name = context["folder"]["name"]
        product_name = context["product"]["name"]

        lib_container = plugin.prepare_scene_name(
            folder_name, product_name
        )
        unique_number = plugin.get_unique_number(
            folder_name, product_name
        )
        namespace = namespace or f"{folder_name}_{unique_number}"
        container_name = plugin.prepare_scene_name(
            folder_name, product_name, unique_number
        )

        container = bpy.data.collections.new(lib_container)
        container.name = container_name
        containerise_existing(
            container,
            name,
            namespace,
            context,
            self.__class__.__name__,
        )

        metadata = container.get(AYON_PROPERTY)

        metadata["libpath"] = libpath
        metadata["lib_container"] = lib_container

        selected = [o for o in bpy.context.scene.objects if o.select_get()]

        materials, objects = self._process(libpath, container_name, selected)

        # Save the list of imported materials in the metadata container
        metadata["objects"] = objects
        metadata["materials"] = materials

        metadata["parent"] = context["representation"]["versionId"]
        metadata["product_type"] = context["product"]["productType"]
        metadata["project_name"] = context["project"]["name"]

        nodes = list(container.objects)
        nodes.append(container)
        self[:] = nodes
        return nodes

    def update(self, container: Dict, context: Dict):
        collection = bpy.data.collections.get(container["objectName"])
        repre_entity = context["representation"]
        libpath = Path(self.filepath_from_context(context))
        extension = libpath.suffix.lower()

        self.log.info(
            "Container: %s\nRepresentation: %s",
            pformat(container, indent=2),
            pformat(repre_entity, indent=2),
        )

        assert collection, (
            f"The asset is not loaded: {container['objectName']}"
        )
        assert not (collection.children), (
            "Nested collections are not supported."
        )
        assert libpath, (
            "No existing library file found for {container['objectName']}"
        )
        assert libpath.is_file(), (
            f"The file doesn't exist: {libpath}"
        )
        assert extension in VALID_EXTENSIONS, (
            f"Unsupported file: {libpath}"
        )

        collection_metadata = collection.get(AYON_PROPERTY)
        collection_libpath = collection_metadata["libpath"]

        normalized_collection_libpath = (
            str(Path(bpy.path.abspath(collection_libpath)).resolve())
        )
        normalized_libpath = (
            str(Path(bpy.path.abspath(str(libpath))).resolve())
        )
        self.log.debug(
            "normalized_collection_libpath:\n  %s\nnormalized_libpath:\n  %s",
            normalized_collection_libpath,
            normalized_libpath,
        )
        if normalized_collection_libpath == normalized_libpath:
            self.log.info("Library already loaded, not updating...")
            return

        for obj in collection_metadata['objects']:
            for child in self.get_all_children(obj):
                child.data.materials.clear()

        for material in collection_metadata['materials']:
            bpy.data.materials.remove(material)

        namespace = collection_metadata['namespace']
        name = collection_metadata['name']

        container_name = f"{namespace}_{name}"

        materials, objects = self._process(
            libpath, container_name, collection_metadata['objects'])

        collection_metadata["objects"] = objects
        collection_metadata["materials"] = materials
        collection_metadata["libpath"] = str(libpath)
        collection_metadata["representation"] = repre_entity["id"]
        collection_metadata["project_name"] = context["project"]["name"]

    def remove(self, container: Dict) -> bool:
        collection = bpy.data.collections.get(container["objectName"])
        if not collection:
            return False

        collection_metadata = collection.get(AYON_PROPERTY)

        for obj in collection_metadata['objects']:
            for child in self.get_all_children(obj):
                child.data.materials.clear()

        for material in collection_metadata['materials']:
            bpy.data.materials.remove(material)

        bpy.data.collections.remove(collection)

        return True
