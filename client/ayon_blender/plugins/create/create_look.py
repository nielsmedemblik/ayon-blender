"""Create a look product."""

import bpy

from ayon_blender.api import plugin, lib


class CreateLook(plugin.BlenderCreator):
    """Surface look consisting of material assignments"""

    identifier = "io.ayon.creators.blender.look"
    label = "Look"
    description = __doc__
    product_type = "look"
    icon = "brush"

    def create(
        self, product_name: str, instance_data: dict, pre_create_data: dict
    ):
        collection = super().create(
            product_name, instance_data, pre_create_data
        )

        if not pre_create_data.get("use_selection"):
            return collection

        selected = lib.get_selection(include_collections=True)
        for item in selected:
            if isinstance(item, bpy.types.Object):
                collection.objects.link(item)
            elif isinstance(item, bpy.types.Collection):
                collection.children.link(item)

        return collection
