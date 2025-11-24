import bpy
import pyblish.api

from ayon_core.pipeline.publish import PublishValidationError

from ayon_blender.api import plugin
from ayon_blender.api.look import get_cbid_from_node


class ValidateLookHasCbId(plugin.BlenderInstancePlugin):
    """Ensure every mesh inside the look instance carries a cbId."""

    order = pyblish.api.ValidatorOrder
    hosts = ["blender"]
    families = ["look"]
    label = "Validate Look cbId"

    def process(self, instance):
        missing = []
        for member in instance:
            if not isinstance(member, bpy.types.Object):
                continue
            if member.type != 'MESH':
                continue
            if get_cbid_from_node(member):
                continue
            missing.append(member.name_full)

        if missing:
            joined = ", ".join(sorted(missing))
            raise PublishValidationError(
                f"Missing cbId attribute on meshes: {joined}"
            )
