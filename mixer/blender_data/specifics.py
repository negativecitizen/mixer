"""Type specific helpers for Proxy load and save
"""
import logging
import traceback
from typing import ItemsView, List, TypeVar, Union

import bpy
import bpy.types as T  # noqa N812
from mixer.blender_data.blenddata import BlendData

logger = logging.getLogger(__name__)
Proxy = TypeVar("Proxy")
BpyIDProxy = TypeVar("BpyIDProxy")


def bpy_data_ctor(collection_name: str, proxy: BpyIDProxy) -> Union[T.ID, None]:
    collection = getattr(bpy.data, collection_name)
    BlendData.instance().collection(collection_name).set_dirty
    if collection_name == "images":
        is_packed = proxy.data("packed_file") is not None
        image = None
        if is_packed:
            name = proxy.data("name")
            size = proxy.data("size")
            width = size.data(0)
            height = size.data(1)
            image = collection.new(name, width, height)
            # remaning attributes will be saved from the received proxy attributes
        else:
            path = proxy.data("filepath")
            if path != "":
                image = collection.load(path)
        return image

    if collection_name == "objects":
        name = proxy.data("name")
        target_proxy = proxy.data("data")
        if target_proxy is not None:
            target = target_proxy.target()
        else:
            target = None
        object_ = collection.new(name, target)
        return object_

    if collection_name == "lights":
        name = proxy.data("name")
        light_type = proxy.data("type")
        light = collection.new(name, light_type)
        return light

    name = proxy.data("name")
    try:
        id_ = collection.new(name)
    except TypeError as e:
        logger.error(f"Exception while calling : bpy.data.{collection_name}.new({name})")
        logger.error(f"TypeError : {e}")
        return None

    return id_


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

    if isinstance(bpy_struct, T.MetaBall):
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
    return properties


def pre_save_id(proxy: Proxy, collection: T.bpy_prop_collection, key: str) -> T.Struct:
    """Process attributes that must be saved first and return a possibily updated reference to the target

    Args:
        bpy_struct: The collection that contgains the ID
        attr_name: Its key

    Returns:
        [bpy.types.ID]: a possibly new ID
    """
    target = proxy.target(collection, key)
    if isinstance(target, T.Scene):
        # Set 'use_node' to True first is the only way I know to be able to set the 'node_tree' attribute
        use_nodes = proxy.data("use_nodes")
        if use_nodes:
            target.use_nodes = True
        sequence_editor = proxy.data("sequence_editor")
        if sequence_editor is not None and target.sequence_editor is None:
            target.sequence_editor_create()
    elif isinstance(target, T.Light):
        # required first to have access to new light type attributes
        light_type = proxy.data("type")
        if light_type is not None and light_type != target.type:
            target.type = light_type
            # must reload the reference
            target = proxy.target(collection, key)
    elif isinstance(target, T.ColorManagedViewSettings):
        use_curve_mapping = proxy.data("use_curve_mapping")
        if use_curve_mapping:
            target.use_curve_mapping = True
    elif isinstance(target, bpy.types.World):
        use_nodes = proxy.data("use_nodes")
        if use_nodes:
            target.use_nodes = True
    return target


def pre_save_struct(proxy: Proxy, bpy_struct: T.Struct, attr_name: str):
    """Process attributes that must be saved first
    """
    target = getattr(bpy_struct, attr_name)
    if isinstance(target, T.ColorManagedViewSettings):
        use_curve_mapping = proxy.data("use_curve_mapping")
        if use_curve_mapping:
            target.use_curve_mapping = True


def add_element(proxy: Proxy, collection: T.bpy_prop_collection, key: str):
    """Add an element to a bpy_prop_collection using the collection specific API
    """
    bl_rna = getattr(collection, "bl_rna", None)
    if bl_rna is not None:
        if isinstance(bl_rna, type(T.KeyingSets.bl_rna)):
            idname = proxy.data("bl_idname")
            return collection.new(name=key, idname=idname)

        if isinstance(bl_rna, type(T.KeyingSetPaths.bl_rna)):
            # TODO current implementation fails
            # All keying sets paths have an empty name, and insertion with add()à failes
            # with an empty name
            target_ref = proxy.data("id")
            if target_ref is None:
                target = None
            else:
                target = target_ref.target()
            data_path = proxy.data("data_path")
            index = proxy.data("array_index")
            group_method = proxy.data("group_method")
            group_name = proxy.data("group")
            return collection.add(
                target_id=target, data_path=data_path, index=index, group_method=group_method, group_name=group_name
            )

        if isinstance(bl_rna, type(T.Nodes.bl_rna)):
            node_type = proxy.data("bl_idname")
            return collection.new(node_type)

    # try our best
    new_or_add = getattr(collection, "new", None)
    if new_or_add is None:
        new_or_add = getattr(collection, "add", None)
    try:
        return new_or_add(key)
    except Exception:
        logger.warning(f"Not implemented new or add for type {type(collection)} for {collection}[{key}] ...")
        for s in traceback.format_exc().splitlines():
            logger.warning(f"...{s}")
        return None


def truncate_collection(target: T.bpy_prop_collection, incoming_keys: List[str]):
    incoming_keys = set(incoming_keys)
    existing_keys = set(target.keys())
    truncate_keys = existing_keys - incoming_keys
    if not truncate_keys:
        return
    if isinstance(target.bl_rna, type(T.KeyingSets.bl_rna)):
        for k in truncate_keys:
            target.active_index = target.find(k)
            bpy.ops.anim.keying_set_remove()
    else:
        try:
            for k in truncate_keys:
                target.remove(target[k])
        except Exception:
            logger.warning(f"Not implemented truncate_collection for type {target.bl_rna} for {target} ...")
            for s in traceback.format_exc().splitlines():
                logger.warning(f"...{s}")