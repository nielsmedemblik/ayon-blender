"""Load a model asset in Blender."""

from collections import defaultdict
from pathlib import Path
from pprint import pformat
from typing import Dict, List, Optional, Tuple

import json
import bpy
from ayon_api import get_representations
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

    def _process(self, libpath, container_name, objects, context):
        with open(libpath, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        materials_path = self._get_materials_library_path(context)
        material_lookup, imported_materials = self._load_material_library(
            materials_path, container_name
        )

        target_meshes = self._gather_target_meshes(objects)
        cbid_to_meshes = defaultdict(list)
        name_to_meshes = defaultdict(list)
        for mesh in target_meshes:
            cbid = get_cbid_from_node(mesh)
            if cbid:
                cbid_to_meshes[cbid].append(mesh)
            base_name = mesh.name.split(':')[0]
            name_to_meshes[base_name].append(mesh)

        for entry in data:
            material_name = entry.get("material_name")
            material = material_lookup.get(material_name)
            if not material:
                self.log.warning(
                    "Material '%s' missing from library, skipping.", material_name
                )
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

    def _get_materials_library_path(self, context: dict) -> str:
        version_id = context["representation"]["versionId"]
        project_name = context["project"]["name"]
        materials_repres = list(get_representations(
            project_name,
            representation_names={"blend", "materials"},
            version_ids={version_id},
            fields={
                "id",
                "name",
                "context",
                "files",
                "attrib",
                "data",
                "versionId",
            },
        ))
        if not materials_repres:
            raise RuntimeError("Look materials representation not found.")
        materials_repr = next(
            (rep for rep in materials_repres if rep.get("name") == "blend"),
            materials_repres[0],
        )
        materials_context = dict(context)
        materials_context["representation"] = materials_repr
        path = self.filepath_from_context(materials_context)
        if not path:
            raise RuntimeError("Failed to resolve materials representation path.")
        if hasattr(path, "normalized"):
            path = path.normalized()
        return str(path)

    def _load_material_library(
        self, materials_path: str, container_name: str
    ) -> Tuple[Dict[str, bpy.types.Material], List[bpy.types.Material]]:
        material_lookup: Dict[str, bpy.types.Material] = {}
        imported_materials: List[bpy.types.Material] = []
        with bpy.data.libraries.load(materials_path, link=False, relative=False) as (
            data_from,
            data_to,
        ):
            data_to.materials = data_from.materials
            data_to.images = data_from.images

        for material in data_to.materials:
            if material is None:
                continue
            original_name = material.name
            material_lookup[original_name] = material
            material.name = f"{original_name}:{container_name}"
            imported_materials.append(material)
        return material_lookup, imported_materials

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

        materials, objects = self._process(
            libpath, container_name, selected, context
        )

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
            libpath, container_name, collection_metadata['objects'], context
        )

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
