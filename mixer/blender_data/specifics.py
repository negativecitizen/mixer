# GPLv3 License
#
# Copyright (C) 2020 Ubisoft
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Proxy helpers Blender types that have different interfaces or requirements, but do not require their own complete
Proxy implementation.


TODO Enhance this module so that it is possible to reference types that do not exist in all Blender versions or
to control behavior with plugin data.
"""

from __future__ import annotations

import array
import logging
from pathlib import Path
import traceback
from typing import Any, Callable, Dict, ItemsView, List, Optional, TYPE_CHECKING, Union

from mixer.local_data import get_resolved_file_path

import bpy
import bpy.types as T  # noqa N812
import bpy.path
import mathutils

if TYPE_CHECKING:
    from mixer.blender_data.aos_proxy import AosProxy
    from mixer.blender_data.datablock_proxy import DatablockProxy
    from mixer.blender_data.datablock_ref_proxy import DatablockRefProxy
    from mixer.blender_data.proxy import Context, Proxy

logger = logging.getLogger(__name__)


# Beware that MeshVertex must be handled as SOA although "groups" is a variable length item.
# Enums are not handled by foreach_get()
soable_collection_properties = {
    T.GPencilStroke.bl_rna.properties["points"],
    T.GPencilStroke.bl_rna.properties["triangles"],
    T.Mesh.bl_rna.properties["edges"],
    T.Mesh.bl_rna.properties["loops"],
    T.Mesh.bl_rna.properties["loop_triangles"],
    T.Mesh.bl_rna.properties["polygons"],
    T.Mesh.bl_rna.properties["vertices"],
    T.MeshFaceMapLayer.bl_rna.properties["data"],
    T.MeshLoopColorLayer.bl_rna.properties["data"],
    T.MeshUVLoopLayer.bl_rna.properties["data"],
    T.ShapeKey.bl_rna.properties["data"],
    T.Spline.bl_rna.properties["bezier_points"],
}
_resize_geometry_types = tuple(
    type(t.bl_rna)
    for t in [
        T.MeshEdges,
        T.MeshLoops,
        T.MeshLoopTriangles,
        T.MeshPolygons,
        T.MeshVertices,
    ]
)


# in sync with soa_initializers
soable_properties = (
    T.BoolProperty,
    T.IntProperty,
    T.FloatProperty,
    mathutils.Vector,
    mathutils.Color,
    mathutils.Quaternion,
)

# in sync with soable_properties
soa_initializers: Dict[type, array.array] = {
    bool: array.array("b", [0]),
    int: array.array("l", [0]),
    float: array.array("f", [0.0]),
    mathutils.Vector: array.array("f", [0.0]),
    mathutils.Color: array.array("f", [0.0]),
    mathutils.Quaternion: array.array("f", [0.0]),
}


def dispatch_rna(no_rna_impl: Callable[..., Any]):
    """Decorator to select a function implementation according to the rna of its first argument

    See test_rna_dispatch
    """
    registry: Dict[type, Callable[..., Any]] = {}

    def register_default():
        """Registers the decorated function f as the implementaton to use if the rna of the first argument
        of f was not otherwise registered"""

        def decorator(f: Callable[..., Any]):
            registry[type(None)] = f
            return f

        return decorator

    def register(class_):
        """Registers the decorated function f as the implementaton to use if the first argument
        has the same rna as class_"""

        def decorator(f: Callable[..., Any]):
            registry[class_] = f
            return f

        return decorator

    def dispatch(class_):
        # ignore "object" parent
        for cls_ in class_.mro()[:-1]:
            # KeyError exceptions are a pain when debugging in VScode, avoid them
            func = registry.get(cls_)
            if func:
                return func

        func = registry.get(type(None))
        if func is not None:
            return func

        return no_rna_impl

    def wrapper(bpy_prop_collection: T.bpy_prop_collection, *args, **kwargs):
        """Calls the function registered for bpy_prop_collection.bl_rna"""
        rna = getattr(bpy_prop_collection, "bl_rna", None)
        if rna is None:
            func = no_rna_impl
        else:
            func = dispatch(type(rna))
        return func(bpy_prop_collection, *args, **kwargs)

    # wrapper.register = register  genarates mypy error
    setattr(wrapper, "register", register)  # noqa B010
    setattr(wrapper, "register_default", register_default)  # noqa B010
    return wrapper


def dispatch_value(default_func):
    """Decorator to select a function implementation according to the value of its fist parameter"""
    registry: Dict[type, Callable[..., Any]] = {}
    default: Callable[..., Any] = default_func

    def register(value: Any):
        """Registers the decorated function f as the implementaton to use if the first argument
        has the same value a the value parameter"""

        def decorator(f: Callable[..., Any]):
            registry[value] = f
            return f

        return decorator

    def dispatch(value: Any):
        func = registry.get(value)
        if func:
            return func

        return default

    def wrapper(value: Any, *args, **kwargs):
        """Calls the function registered for value"""
        func = dispatch(value)
        return func(value, *args, **kwargs)

    # wrapper.register = register  genarates mypy error
    setattr(wrapper, "register", register)  # noqa B010
    return wrapper


def is_soable_collection(prop):
    return prop in soable_collection_properties


def is_soable_property(bl_rna_property):
    return isinstance(bl_rna_property, soable_properties)


node_tree_type = {
    "SHADER": "ShaderNodeTree",
    "COMPOSITOR": "CompositorNodeTree",
    "TEXTURE": "TextureNodeTree",
}


@dispatch_value
def bpy_data_ctor(collection_name: str, proxy: DatablockProxy, context: Any) -> Optional[T.ID]:
    """
    Create an element in a bpy.data collection.

    Contains collection-specific code is the mathod to add an element is not new(name: str)
    """
    collection = getattr(bpy.data, collection_name)
    name = proxy.data("name")
    try:
        id_ = collection.new(name)
    except Exception as e:
        logger.error(f"Exception while calling : bpy.data.{collection_name}.new({name})")
        logger.error(f"... {e!r}")
        return None

    return id_


@bpy_data_ctor.register("images")
def bpy_data_ctor_images(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    collection = getattr(bpy.data, collection_name)
    image = None
    image_name = proxy.data("name")
    filepath = proxy.data("filepath")
    resolved_filepath = get_resolved_file_path(filepath)
    packed_files = proxy.data("packed_files")
    if packed_files is not None and packed_files.length:
        name = proxy.data("name")
        width, height = proxy.data("size")
        try:
            with open(resolved_filepath, "rb") as image_file:
                buffer = image_file.read()
            image = collection.new(name, width, height)
            image.pack(data=buffer, data_len=len(buffer))
        except RuntimeError as e:
            logger.warning(
                f'Cannot load packed image original "{filepath}"", resolved "{resolved_filepath}". Exception: '
            )
            logger.warning(f"... {e}")
            return None

    else:
        try:
            image = collection.load(resolved_filepath)
            image.name = image_name
        except RuntimeError as e:
            logger.warning(f'Cannot load image original "{filepath}"", resolved "{resolved_filepath}". Exception: ')
            logger.warning(f"... {e}")
            return None

    # prevent filepath to be overwritten by the incoming proxy value as it would attempt to reload the file
    # from the incoming path that may not exist
    proxy._data["filepath"] = resolved_filepath
    proxy._data["filepath_raw"] = resolved_filepath
    return image


@bpy_data_ctor.register("objects")
def bpy_data_ctor_objects(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    from mixer.blender_data.datablock_ref_proxy import DatablockRefProxy
    from mixer.blender_data.misc_proxies import NonePtrProxy

    collection = getattr(bpy.data, collection_name)
    name = proxy.data("name")
    data_datablock = None
    data_proxy = proxy.data("data")
    if isinstance(data_proxy, DatablockRefProxy):
        data_datablock = data_proxy.target(context)
    elif isinstance(data_proxy, NonePtrProxy):
        data_datablock = None
    else:
        # error on the sender side
        logger.warning(f"bpy.data.objects[{name}].data proxy is a {data_proxy.__class__}.")
        logger.warning("... loaded as Empty")
        data_datablock = None

    object_datablock = collection.new(name, data_datablock)

    if data_datablock is not None:
        context.proxy_state.objects[data_datablock.mixer_uuid].add(proxy.mixer_uuid)

    return object_datablock


@bpy_data_ctor.register("lights")
def bpy_data_ctor_lights(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    collection = getattr(bpy.data, collection_name)
    name = proxy.data("name")
    light_type = proxy.data("type")
    light = collection.new(name, light_type)
    return light


@bpy_data_ctor.register("node_groups")
def bpy_data_ctor_node_groups(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    collection = getattr(bpy.data, collection_name)
    name = proxy.data("name")
    type_ = node_tree_type[proxy.data("type")]
    return collection.new(name, type_)


@bpy_data_ctor.register("sounds")
def bpy_data_ctor_sounds(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    collection = getattr(bpy.data, collection_name)
    filepath = proxy.data("filepath")
    # TODO what about "check_existing" ?
    id_ = collection.load(filepath)
    # we may have received an ID named xxx.001 although filepath is xxx, so fix it now
    id_.name = proxy.data("name")
    return id_


@bpy_data_ctor.register("curves")
def bpy_data_ctor_curves(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    collection = getattr(bpy.data, collection_name)
    name = proxy.data("name")
    return collection.new(name, "CURVE")


@bpy_data_ctor.register("shape_keys")
def bpy_data_ctor_shape_keys(collection_name: str, proxy: DatablockProxy, context: Context) -> Optional[T.ID]:
    user = proxy._data["user"]
    user_proxy = context.proxy_state.proxies.get(user.mixer_uuid)
    datablock = proxy.create_shape_key_datablock(user_proxy, context)
    return datablock


filter_crop_transform = [
    T.EffectSequence,
    T.ImageSequence,
    T.MaskSequence,
    T.MetaSequence,
    T.MovieClipSequence,
    T.MovieSequence,
    T.SceneSequence,
]


def conditional_properties(bpy_struct: T.Struct, properties: ItemsView) -> ItemsView:
    """Filter properties list according to a specific property value in the same ID

    This prevents loading values that cannot always be saved, such as Object.instance_collection
    that can only be saved when Object.data is None

    Args:
        properties: the properties list to filter
    Returns:

    """
    if isinstance(bpy_struct, T.ColorManagedViewSettings):
        if bpy_struct.use_curve_mapping:
            # Empty
            return properties
        filtered = {}
        filter_props = ["curve_mapping"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.Object):
        if not bpy_struct.data:
            # Empty
            return properties
        filtered = {}
        filter_props = ["instance_collection"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.Mesh):
        if not bpy_struct.use_auto_texspace:
            # Empty
            return properties
        filtered = {}
        filter_props = ["texspace_location", "texspace_size"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, (T.MetaBall, T.Curve)):
        if not bpy_struct.use_auto_texspace:
            return properties
        filter_props = ["texspace_location", "texspace_size"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.Node):
        if bpy_struct.hide:
            return properties

        # not hidden: saving width_hidden is ignored
        filter_props = ["width_hidden"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.NodeTree):
        if not bpy_struct.is_embedded_data:
            return properties

        filter_props = ["name"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.LayerCollection):
        scene = bpy_struct.id_data
        if bpy_struct.collection != scene.collection:
            return properties

        filter_props = ["exclude"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    filter_props = []
    if any(isinstance(bpy_struct, t) for t in filter_crop_transform):
        if not bpy_struct.use_crop:
            filter_props.append("crop")
        if not bpy_struct.use_translation:
            filter_props.append("transform")

    if not filter_props:
        return properties
    filtered = {k: v for k, v in properties if k not in filter_props}
    return filtered.items()


def pre_save_datablock(proxy: DatablockProxy, target: T.ID, context: Context) -> T.ID:
    """Process attributes that must be saved first and return a possibly updated reference to the target"""

    # WARNING this is called from save() and from apply()
    # When called from save, the proxy has  all the synchronized properties
    # WHen called from apply, the proxy only contains the updated properties

    if isinstance(target, T.Mesh) and proxy.requires_clear_geometry(target):
        target.clear_geometry()
    elif isinstance(target, T.Material):
        is_grease_pencil = proxy.data("is_grease_pencil")
        # will be None for a DeltaUpdate that does not modify "is_grease_pencil"
        if is_grease_pencil is not None:
            # Seems to be write once as no depsgraph update is fired
            if is_grease_pencil and not target.grease_pencil:
                bpy.data.materials.create_gpencil_data(target)
            elif not is_grease_pencil and target.grease_pencil:
                bpy.data.materials.remove_gpencil_data(target)
    elif isinstance(target, T.Scene):
        from mixer.blender_data.misc_proxies import NonePtrProxy

        sequence_editor = proxy.data("sequence_editor")
        if sequence_editor is not None:
            # NonePtrProxy or StructProxy
            if not isinstance(sequence_editor, NonePtrProxy) and target.sequence_editor is None:
                target.sequence_editor_create()
            elif isinstance(sequence_editor, NonePtrProxy) and target.sequence_editor is not None:
                target.sequence_editor_clear()
    elif isinstance(target, T.Light):
        # required first to have access to new light type attributes
        light_type = proxy.data("type")
        if light_type is not None and light_type != target.type:
            target.type = light_type
            # must reload the reference
            target = proxy.target(context)

    return target


#
# add_element
#
@dispatch_rna
def add_element(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    """Add an element to a bpy_prop_collection using the collection specific API"""
    try:
        collection.bl_rna
    except AttributeError:
        return

    try:
        collection.add()
    except Exception:
        logger.error(f"add_element: failed for {collection}")


@add_element.register_default()
def _add_element_default(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    try:
        return collection.add()
    except Exception:
        pass

    # try our best
    new_or_add = getattr(collection, "new", None)
    if new_or_add is None:
        new_or_add = getattr(collection, "add", None)
    if new_or_add is None:
        logger.warning(f"Not implemented: add_element for {collection} ...")
        return None

    try:
        return new_or_add()
    except TypeError:
        try:
            key = proxy.data("name")
            return new_or_add(key)
        except Exception:
            logger.warning(f"Not implemented: add_element for type {type(collection)} for {collection}[{key}] ...")
            for s in traceback.format_exc().splitlines():
                logger.warning(f"...{s}")
    return None


@add_element.register(T.NodeInputs)
@add_element.register(T.NodeOutputs)
@add_element.register(T.NodeTreeInputs)
@add_element.register(T.NodeTreeOutputs)
def _add_element_type_name(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    socket_type = proxy.data("type")
    name = proxy.data("name")
    return collection.new(socket_type, name)


@add_element.register(T.ObjectModifiers)
@add_element.register(T.ObjectGpencilModifiers)
@add_element.register(T.SequenceModifiers)
def _add_element_name_type(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    name = proxy.data("name")
    type_ = proxy.data("type")
    return collection.new(name, type_)


@add_element.register(T.ObjectConstraints)
@add_element.register(T.CurveSplines)
def _add_element_type(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    type_ = proxy.data("type")
    return collection.new(type_)


@add_element.register(T.SplinePoints)
@add_element.register(T.SplineBezierPoints)
def _add_element_one(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    return collection.add(1)


@add_element.register(T.MetaBallElements)
def _add_element_type_eq(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    type_ = proxy.data("type")
    return collection.new(type=type_)


@add_element.register(T.CurveMapPoints)
def _add_element_location(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    location = proxy.data("location")
    return collection.new(location[0], location[1])


@add_element.register(T.Nodes)
def _add_element_idname(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    node_type = proxy.data("bl_idname")
    return collection.new(node_type)


@add_element.register(T.UVLoopLayers)
@add_element.register(T.LoopColors)
@add_element.register(T.FaceMaps)
@add_element.register(T.VertexGroups)
def _add_element_name_eq(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    name = proxy.data("name")
    return collection.new(name=name)


@add_element.register(T.GreasePencilLayers)
def _add_element_info(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    name = proxy.data("info")
    return collection.new(name)


@add_element.register(T.GPencilFrames)
def _add_element_frame_number(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    frame_number = proxy.data("frame_number")
    return collection.new(frame_number)


@add_element.register(T.KeyingSets)
def _add_element_bl_label(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    label = proxy.data("bl_label")
    idname = proxy.data("bl_idname")
    return collection.new(name=label, idname=idname)


@add_element.register(T.KeyingSetPaths)
def _add_element_keyingset(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    # TODO current implementation fails
    # All keying sets paths have an empty name, and insertion with add() fails
    # with an empty name
    target_ref = proxy.data("id")
    if target_ref is None:
        target = None
    else:
        target = target_ref.target(context)
    data_path = proxy.data("data_path")
    index = proxy.data("array_index")
    group_method = proxy.data("group_method")
    group_name = proxy.data("group")
    return collection.add(
        target_id=target, data_path=data_path, index=index, group_method=group_method, group_name=group_name
    )


_non_effect_sequences = {"IMAGE", "SOUND", "META", "SCENE", "MOVIE", "MOVIECLIP", "MASK"}
_effect_sequences = set(T.EffectSequence.bl_rna.properties["type"].enum_items.keys()) - _non_effect_sequences


@add_element.register(T.Sequences)
def _add_element_sequence(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    type_name = proxy.data("type")
    name = proxy.data("name")
    channel = proxy.data("channel")
    frame_start = proxy.data("frame_start")
    if type_name in _effect_sequences:
        # overwritten anyway
        frame_end = frame_start + 1
        return collection.new_effect(name, type_name, channel, frame_start, frame_end=frame_end)
    if type_name == "SOUND":
        sound = proxy.data("sound")
        target = sound.target(context)
        if not target:
            logger.warning(f"missing target ID block for bpy.data.{sound.collection}[{sound.key}] ")
            return None
        filepath = target.filepath
        return collection.new_sound(name, filepath, channel, frame_start)
    if type_name == "MOVIE":
        filepath = proxy.data("filepath")
        return collection.new_movie(name, filepath, channel, frame_start)
    if type_name == "IMAGE":
        directory = proxy.data("directory")
        filename = proxy.data("elements").data(0).data("filename")
        filepath = str(Path(directory) / filename)
        return collection.new_image(name, filepath, channel, frame_start)

    logger.warning(f"Sequence type not implemented: {type_name}")
    return None


@add_element.register(T.IDMaterials)
def _add_element_material_ref(collection: T.bpy_prop_collection, proxy: Proxy, context: Context):
    material_datablock = proxy.target(context)
    return collection.append(material_datablock)


def fit_aos(target: T.bpy_prop_collection, proxy: AosProxy, context: Context):
    """
    Adjust the size of a bpy_prop_collection proxified as an array of structures (e.g. MeshVertices)
    """

    if not hasattr(target, "bl_rna"):
        return

    target_rna = target.bl_rna
    if isinstance(target_rna, _resize_geometry_types):
        existing_length = len(target)
        incoming_length = len(proxy)
        if existing_length != incoming_length:
            if existing_length != 0:
                logger.error(f"resize_geometry(): size mismatch for {target}")
                logger.error(f"... existing: {existing_length} incoming {incoming_length}")
                return
            logger.debug(f"resizing geometry: add({incoming_length}) for {target}")
            target.add(incoming_length)
        return

    if isinstance(target_rna, type(T.GPencilStrokePoints.bl_rna)):
        existing_length = len(target)
        incoming_length = proxy.length
        delta = incoming_length - existing_length
        if delta > 0:
            target.add(delta)
        else:
            while delta < 0:
                target.pop()
                delta += 1
        return

    if isinstance(target_rna, type(T.SplineBezierPoints.bl_rna)):
        existing_length = len(target)
        incoming_length = len(proxy)
        delta = incoming_length - existing_length
        if delta > 0:
            target.add(delta)
        elif delta < 0:
            logger.error("Remove not implemented for type SplineBezierPoints")
        return

    logger.error(f"Not implemented fit_aos for type {target.bl_rna} for {target} ...")


#
# must_replace
#
_object_material_slots_property = T.Object.bl_rna.properties["material_slots"]
_key_key_blocks_property = T.Key.bl_rna.properties["key_blocks"]


@dispatch_rna
def diff_must_replace(
    collection: T.bpy_prop_collection, sequence: List[DatablockProxy], collection_property: T.Property
) -> bool:
    """
    Returns True if a diff between the proxy sequence state and the Blender collection state must force a
    full collection replacement
    """

    if collection_property == _object_material_slots_property:
        # Object.material_slots has no bl_rna, so rely on the property to identify it
        # TODO should we change to a dispatch on the property value ?
        from mixer.blender_data.misc_proxies import NonePtrProxy

        if len(collection) != len(sequence):
            return True

        # The struct_collection update encoding is complex. It is way easier to handle a full replace in
        # ObjectProxy._update_material_slots, so replace all if anything has changed

        # As diff yields a complete DiffReplace or nothing, all the attributes are present in the proxy
        for bl_item, proxy in zip(collection, sequence):
            bl_material = bl_item.material
            material_proxy: Union[DatablockRefProxy, NonePtrProxy] = proxy.data("material")
            if (bl_material is None) != isinstance(material_proxy, NonePtrProxy):
                return True
            if bl_material is not None and bl_material.mixer_uuid != material_proxy.mixer_uuid:
                return True
            if bl_item.link != proxy.data("link"):
                return True

    elif collection_property == _key_key_blocks_property:
        if len(collection) != len(sequence):
            return True
        for bl_item, proxy in zip(collection, sequence):
            if bl_item.name != proxy.data("name"):
                return True
            if bl_item.relative_key.name != proxy.data("relative_key"):
                # see ShapeKeyProxy.load()
                return True

    return False


@diff_must_replace.register(T.CurveSplines)
def _diff_must_replace_always(
    collection: T.bpy_prop_collection, sequence: List[DatablockProxy], collection_property: T.Property
) -> bool:
    return True


@diff_must_replace.register(T.VertexGroups)
def _diff_must_replace_vertex_groups(
    collection: T.bpy_prop_collection, sequence: List[DatablockProxy], collection_property: T.Property
) -> bool:
    # Full replace if anything has changed is easier to cope with in ObjectProxy._update_vertex_groups()
    return (
        any((bl_item.name != proxy.data("name") for bl_item, proxy in zip(collection, sequence)))
        or any((bl_item.index != proxy.data("index") for bl_item, proxy in zip(collection, sequence)))
        or any((bl_item.lock_weight != proxy.data("lock_weight") for bl_item, proxy in zip(collection, sequence)))
    )


@diff_must_replace.register(T.GreasePencilLayers)
def _diff_must_replace_info_mismatch(
    collection: T.bpy_prop_collection, sequence: List[DatablockProxy], collection_property: T.Property
) -> bool:
    # Name mismatch (in info property). This may happen during layer swap and cause unsolicited rename
    # Easier to solve with full replace
    return any((bl_item.info != proxy.data("info") for bl_item, proxy in zip(collection, sequence)))


#
# Clear_from
#
@dispatch_rna
def clear_from(collection: T.bpy_prop_collection, sequence: List[DatablockProxy]) -> int:
    """
    Returns the index of the first item in collection that has a type that does not match the
    coresponding item in sequence
    """
    return min(len(sequence), len(collection))


@clear_from.register(T.ObjectModifiers)
@clear_from.register(T.ObjectGpencilModifiers)
@clear_from.register(T.SequenceModifiers)
def _clear_from_name(collection: T.bpy_prop_collection, sequence: List[DatablockProxy]) -> int:
    """clear_from() implementation for collections with items types are named "type" """
    for i, (proxy, item) in enumerate(zip(sequence, collection)):
        if proxy.data("type") != item.type:
            return i
    return min(len(sequence), len(collection))


@clear_from.register(T.Nodes)
def _clear_from_bl_idname(collection: T.bpy_prop_collection, sequence: List[DatablockProxy]) -> int:
    """clear_from() implementation for collections with items types are named "bl_idname" """
    for i, (proxy, item) in enumerate(zip(sequence, collection)):
        if proxy.data("bl_idname") != item.bl_idname:
            return i

    return min(len(sequence), len(collection))


#
# truncate_collection
#
@dispatch_rna
def truncate_collection(collection: T.bpy_prop_collection, size: int):
    """Truncates collection to _at most_ size elements, ensuring that items can safely be saved into
    the collection. This might clear the collection if its elements cannot be updated.

    This method is useful for bpy _prop_collections that cannot be safely be overwritten in place,
    because the items cannot be morphed."""
    return


@truncate_collection.register_default()
def _truncate_collection_remove(collection: T.bpy_prop_collection, size: int):
    try:
        while len(collection) > max(size, 0):
            collection.remove(collection[-1])
    except Exception as e:
        logger.error(f"truncate_collection {collection}: exception ...")
        logger.error(f"... {e!r}")


@truncate_collection.register(T.Nodes)
def _truncate_collection_clear(collection: T.bpy_prop_collection, size: int):
    collection.clear()


@truncate_collection.register(T.IDMaterials)
def _truncate_collection_pop(collection: T.bpy_prop_collection, size: int):
    while len(collection) > max(size, 0):
        collection.pop()


def remove_datablock(collection: T.bpy_prop_collection, datablock: T.ID):
    """Delete a datablock from its bpy.data collection"""
    if isinstance(datablock, T.Scene):
        from mixer.blender_client.scene import delete_scene

        delete_scene(datablock)
    elif isinstance(datablock, T.Key):
        # the doc labels it unsafe, use sparingly
        bpy.data.batch_remove([datablock])
    else:
        collection.remove(datablock)
