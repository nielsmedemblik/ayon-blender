import bpy
import pyblish.api

from ayon_blender.api import plugin
from ayon_blender.api.look import get_cbid_from_node


class CollectLookData(plugin.BlenderInstancePlugin):
    """Collect materials and cbId assignments for look instances."""

    order = pyblish.api.CollectorOrder + 0.2
    hosts = ["blender"]
    families = ["look"]
    label = "Collect Look Data"

    def process(self, instance):

        meshes = [
            member for member in instance
            if isinstance(member, bpy.types.Object)
            and member.type == 'MESH'
        ]

        materials = {}
        assignments = []

        for mesh in meshes:
            cbid = get_cbid_from_node(mesh)
            for slot_index, slot in enumerate(mesh.material_slots):
                material = slot.material
                if material is None:
                    continue

                materials[material.name_full] = material
                assignments.append(
                    {
                        "object_name": mesh.name_full,
                        "material_name": material.name_full,
                        "cbid": cbid,
                        "slot_index": slot_index,
                    }
                )

        instance.data.setdefault("lookData", {})
        instance.data["lookData"]["materials"] = list(materials.values())
        instance.data["lookData"]["assignments"] = assignments
        instance.data["lookData"]["mesh_members"] = [
            mesh.name_full for mesh in meshes
        ]
