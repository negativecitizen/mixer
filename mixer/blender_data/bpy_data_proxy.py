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
This module provides an implementation for the a proxy to the whole Blender data state, i.e the relevant members
of bpy.data.

See synchronization.md
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging
import sys
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING, Union

import bpy
import bpy.types as T  # noqa

from mixer.blender_data.blenddata import BlendData
from mixer.blender_data.changeset import Changeset, RenameChangeset
from mixer.blender_data.datablock_collection_proxy import DatablockCollectionProxy
from mixer.blender_data.datablock_proxy import DatablockProxy
from mixer.blender_data.diff import BpyBlendDiff
from mixer.blender_data.filter import SynchronizedProperties, safe_depsgraph_updates, safe_properties
from mixer.blender_data.proxy import (
    DeltaReplace,
    DeltaUpdate,
    Proxy,
    MaxDepthExceeded,
    UnresolvedRefs,
    Uuid,
)

if TYPE_CHECKING:
    from mixer.blender_data.changeset import Removal
    from mixer.blender_data.types import Path, SoaMember

logger = logging.getLogger(__name__)


class RecursionGuard:
    """
    Limits allowed attribute depth, and guards against recursion caused by unfiltered circular references
    """

    MAX_DEPTH = 30

    def __init__(self):
        self._property_stack: List[str] = []

    def push(self, name: str):
        self._property_stack.append(name)
        if len(self._property_stack) > self.MAX_DEPTH:
            property_path = ".".join([p for p in self._property_stack])
            raise MaxDepthExceeded(property_path)

    def pop(self):
        self._property_stack.pop()


@dataclass
class ProxyState:
    """
    State of a BpyDataProxy
    """

    proxies: Dict[Uuid, DatablockProxy] = field(default_factory=dict)
    """Known proxies"""

    datablocks: Dict[Uuid, T.ID] = field(default_factory=dict)
    """Known datablocks"""

    objects: Dict[Uuid, Set[Uuid]] = field(default_factory=lambda: defaultdict(set))
    """Object.data uuid : (set of uuids of Object using object.data). Mostly used for shape keys"""

    unresolved_refs: UnresolvedRefs = UnresolvedRefs()


class VisitState:
    """
    Visit state updated during the proxy structure hierarchy with local (per datablock)
    or global (inter datablock) state
    """

    class CurrentDatablockContext:
        """Context manager to keep track of the current standalone datablock"""

        def __init__(self, visit_state: VisitState, proxy: DatablockProxy, datablock: T.ID):
            self._visit_state = visit_state
            self._is_embedded_data = datablock.is_embedded_data
            self._proxy = proxy

        def __enter__(self):
            if not self._is_embedded_data:
                self._visit_state.datablock_proxy = self._proxy

        def __exit__(self, exc_type, exc_value, traceback):
            if not self._is_embedded_data:
                self._visit_state.datablock_proxy = None

    Path = List[Union[str, int]]
    """The current visit path relative to the datablock, for instance in a GreasePencil datablock
    ("layers", "MyLayer", "frames", 0, "strokes", 0, "points").
    Used to identify SoaElement buffer updates.
    Equivalent to a RNA path, parsed with indexed instead of names.
    Local state
    """

    def __init__(self):

        self.datablock_proxy: Optional[DatablockProxy] = None
        """The standalone datablock proxy being visited.

        Local state
        """

        self.path: Path = []
        """The path to the current property from the current datablock, for instance in GreasePencil
        ["layers", "fills", "frames", 0, "strokes", 1, "points", 0].

        Local state
        """

        self.recursion_guard = RecursionGuard()
        """Keeps track of the data depth and guards agains excessive depth that may be caused
        by circular references.

        Local state
        """

        self.dirty_vertex_groups: Set[Uuid] = set()
        """Uuids of the Mesh datablocks whose vertex_groups data has been updated since last loaded
        into their MeshProxy.

        Global state
        """

    def enter_datablock(self, proxy: DatablockProxy, datablock: T.ID) -> VisitState.CurrentDatablockContext:
        return VisitState.CurrentDatablockContext(self, proxy, datablock)


@dataclass
class Context:
    proxy_state: ProxyState
    """Proxy system state"""

    synchronized_properties: SynchronizedProperties
    """Controls what properties are synchronized"""

    visit_state: VisitState = field(default_factory=VisitState)
    """Current datablock operation state"""


_creation_order = {
    # anything else first
    "collections": 10,
    # Scene after Collection. Scene.collection must be up to date before Scene.view_layers can be saved
    "scenes": 20,
    # Object.data is required to create Object
    "objects": 30,
    # Key creation require Object API
    "shape_keys": 40,
}


def _creation_order_predicate(item: Tuple[str, Any]) -> int:
    # item (bpy.data collection name, delta)
    return _creation_order.get(item[0], 0)


_updates_order = {
    T.Key: 5,  # before Mesh for shape keys
    T.Mesh: 10,  # before Object for vertex_groups
    # anything else last
}


def _updates_order_predicate(datablock: T.ID) -> int:
    return _updates_order.get(type(datablock), sys.maxsize)


_removal_order = {
    # remove Object before its data otherwise data is removed at the time the Object is removed
    # and the data removal fails
    T.Object: 10,
    # anything else last
}


def _remove_order_predicate(removal: Removal) -> int:
    return _updates_order.get(removal[1], sys.maxsize)


class BpyDataProxy(Proxy):
    """Proxy to bpy.data collections

    This proxy contains a DatablockCollection proxy per synchronized bpy.data collection
    """

    def __init__(self, *args, **kwargs):

        self.state: ProxyState = ProxyState()

        self._data: Dict[str, DatablockCollectionProxy] = {
            name: DatablockCollectionProxy(name) for name in BlendData.instance().collection_names()
        }

        self._delayed_updates: Set[T.ID] = set()

    def clear(self):
        self._data.clear()
        self.state.proxies.clear()
        self.state.datablocks.clear()

    def reload_datablocks(self):
        datablocks = self.state.datablocks
        datablocks.clear()

        for collection_proxy in self._data.values():
            collection_proxy.reload_datablocks(datablocks)

    def context(self, synchronized_properties: SynchronizedProperties = safe_properties) -> Context:
        return Context(self.state, synchronized_properties)

    def get_non_empty_collections(self):
        return {key: value for key, value in self._data.items() if len(value) > 0}

    def load(self, synchronized_properties: SynchronizedProperties):
        """FOR TESTS ONLY Load the current scene into this proxy

        Only used for test. The initial load is performed by update()
        """
        diff = BpyBlendDiff()
        diff.diff(self, synchronized_properties)
        self.update(diff, set(), False, synchronized_properties)
        return self

    def find(self, collection_name: str, key: str) -> Optional[DatablockProxy]:
        # TODO not used ?
        if not self._data:
            return None
        collection_proxy = self._data.get(collection_name)
        if collection_proxy is None:
            return None
        return collection_proxy.find(key)

    def update(
        self,
        diff: BpyBlendDiff,
        updates: Set[T.ID],
        process_delayed_updates: bool,
        synchronized_properties: SynchronizedProperties = safe_properties,
    ) -> Changeset:
        """
        Process local changes, i.e. created, removed and renames datablocks as well as depsgraph updates.

        This updates the local proxy state and return a Changeset to send to the server. This method is also
        used to send the initial scene contents, which is seen as datablock creations.
        """

        # Update the bpy.data collections status and get the list of newly created bpy.data entries.
        # Updated proxies will contain the IDs to send as an initial transfer.
        # There is no difference between a creation and a subsequent update
        changeset: Changeset = Changeset()

        # Contains the bpy_data_proxy state (known proxies and datablocks), as well as visit_state that contains
        # shared state between updated datablock proxies
        context = self.context(synchronized_properties)

        deltas = sorted(diff.collection_deltas, key=_creation_order_predicate)
        for delta_name, delta in deltas:
            collection_changeset = self._data[delta_name].update(delta, context)
            changeset.creations.extend(collection_changeset.creations)
            changeset.removals.extend(collection_changeset.removals)
            changeset.renames.extend(collection_changeset.renames)

        # Everything is sorted with Object last, but the removals need to be sorted the other way round,
        # otherwise the receiver might get a Mesh remove (that removes the Object as well), then an Object remove
        # message for a non existent objjet that triggers a noisy warning, otherwise useful
        changeset.removals = sorted(changeset.removals, key=_remove_order_predicate)

        all_updates = updates
        if process_delayed_updates:
            all_updates |= self._delayed_updates
            self._delayed_updates.clear()

        sorted_updates = sorted(all_updates, key=_updates_order_predicate)

        for datablock in sorted_updates:
            if not isinstance(datablock, safe_depsgraph_updates):
                logger.info("depsgraph update: ignoring untracked type %s", datablock)
                continue
            if isinstance(datablock, T.Scene) and datablock.name == "_mixer_to_be_removed_":
                logger.error(f"Skipping scene {datablock.name} uuid: '{datablock.mixer_uuid}'")
                continue
            proxy = self.state.proxies.get(datablock.mixer_uuid)
            if proxy is None:
                # Not an error for embedded IDs.
                if not datablock.is_embedded_data:
                    logger.warning(f"depsgraph update for {datablock} : no proxy and not datablock.is_embedded_data")
                else:
                    # For instance Scene.node_tree is not a reference to a bpy.data collection element
                    # but a "pointer" to a NodeTree owned by Scene. In such a case, the update list contains
                    # scene.node_tree, then scene. We can ignore the scene.node_tree update since the
                    # processing of scene will process scene.node_tree.
                    # However, it is not obvious to detect the safe cases and remove the message in such cases
                    logger.info("depsgraph update: Ignoring embedded %s", datablock)
                continue
            delta = proxy.diff(datablock, datablock.name, None, context)
            if delta:
                logger.info("depsgraph update: update %s", datablock)
                # TODO add an apply mode to diff instead to avoid two traversals ?
                proxy.apply_to_proxy(datablock, delta, context)
                changeset.updates.append(delta)
            else:
                logger.info("depsgraph update: ignore empty delta %s", datablock)

        return changeset

    def create_datablock(
        self, incoming_proxy: DatablockProxy, synchronized_properties: SynchronizedProperties = safe_properties
    ) -> Tuple[Optional[T.ID], Optional[RenameChangeset]]:
        """
        Process a received datablock creation command, creating the datablock and updating the proxy state
        """
        bpy_data_collection_proxy = self._data.get(incoming_proxy.collection_name)
        if bpy_data_collection_proxy is None:
            logger.warning(
                f"create_datablock: no bpy_data_collection_proxy with name {incoming_proxy.collection_name} "
            )
            return None, None

        context = self.context(synchronized_properties)
        return bpy_data_collection_proxy.create_datablock(incoming_proxy, context)

    def update_datablock(
        self, update: DeltaUpdate, synchronized_properties: SynchronizedProperties = safe_properties
    ) -> Optional[T.ID]:
        """
        Process a received datablock update command, updating the datablock and the proxy state
        """
        assert isinstance(update, (DeltaUpdate, DeltaReplace))
        incoming_proxy: DatablockProxy = update.value
        bpy_data_collection_proxy = self._data.get(incoming_proxy.collection_name)
        if bpy_data_collection_proxy is None:
            logger.warning(
                f"update_datablock: no bpy_data_collection_proxy with name {incoming_proxy.collection_name} "
            )
            return None

        context = self.context(synchronized_properties)
        return bpy_data_collection_proxy.update_datablock(update, context)

    def remove_datablock(self, uuid: str):
        """
        Process a received datablock removal command, removing the datablock and updating the proxy state
        """
        proxy = self.state.proxies.get(uuid)
        if proxy is None:
            logger.error(f"remove_datablock(): no proxy for {uuid} (debug info)")
            return

        bpy_data_collection_proxy = self._data.get(proxy.collection_name)
        if bpy_data_collection_proxy is None:
            logger.warning(f"remove_datablock: no bpy_data_collection_proxy with name {proxy.collection_name} ")
            return None

        datablock = self.state.datablocks[uuid]

        if isinstance(datablock, T.Object) and datablock.data is not None:
            data_uuid = datablock.data.mixer_uuid
        else:
            data_uuid = None

        bpy_data_collection_proxy.remove_datablock(proxy, datablock)

        if data_uuid is not None:
            # removed an Object
            self.state.objects[data_uuid].remove(uuid)
        else:
            try:
                # maybe removed an Object.data pointee
                del self.state.objects[uuid]
            except KeyError:
                pass
        del self.state.proxies[uuid]
        del self.state.datablocks[uuid]

    def rename_datablocks(self, items: List[Tuple[str, str, str]]) -> RenameChangeset:
        """
        Process a received datablock rename command, renaming the datablocks and updating the proxy state.
        (receiver side)
        """
        rename_changeset_to_send: RenameChangeset = []
        renames = []
        for uuid, old_name, new_name in items:
            proxy = self.state.proxies.get(uuid)
            if proxy is None:
                logger.error(f"rename_datablocks(): no proxy for {uuid} (debug info)")
                return []

            bpy_data_collection_proxy = self._data.get(proxy.collection_name)
            if bpy_data_collection_proxy is None:
                logger.warning(f"rename_datablock: no bpy_data_collection_proxy with name {proxy.collection_name} ")
                continue

            datablock = self.state.datablocks[uuid]
            tmp_name = f"_mixer_tmp_{uuid}"
            if datablock.name != new_name and datablock.name != old_name:
                # local receives a rename, but its datablock name does not match the remote datablock name before
                # the rename. This means that one of these happened:
                # - local has renamed the datablock and remote will receive the rename command later on
                # - local has processed a rename command that remote had not yet processed, but will process later on
                # ensure that everyone renames its datablock with the **same** name
                new_name = new_name = f"_mixer_rename_conflict_{uuid}"
                logger.warning(f"rename_datablocks: conflict for existing {datablock}")
                logger.warning(f'... incoming old name "{old_name}" new name "{new_name}"')
                logger.warning(f"... using {new_name}")

                # Strangely, for collections not everyone always detect a conflict, so rename for everyone
                rename_changeset_to_send.append(
                    (
                        datablock.mixer_uuid,
                        datablock.name,
                        new_name,
                        f"Conflict bpy.data.{proxy.collection_name}[{datablock.name}] into {new_name}",
                    )
                )

            renames.append([bpy_data_collection_proxy, proxy, old_name, tmp_name, new_name, datablock])

        # The rename process is handled in two phases to avoid spontaneous renames from Blender
        # see DatablockCollectionProxy.update() for explanation
        for bpy_data_collection_proxy, proxy, _, tmp_name, _, datablock in renames:
            bpy_data_collection_proxy.rename_datablock(proxy, tmp_name, datablock)

        for bpy_data_collection_proxy, proxy, _, _, new_name, datablock in renames:
            bpy_data_collection_proxy.rename_datablock(proxy, new_name, datablock)

        return rename_changeset_to_send

    def diff(self, synchronized_properties: SynchronizedProperties) -> Optional[BpyDataProxy]:
        """Currently for tests only"""
        diff = self.__class__()
        context = self.context(synchronized_properties)
        for name, proxy in self._data.items():
            collection = getattr(bpy.data, name, None)
            if collection is None:
                logger.warning(f"Unknown, collection bpy.data.{name}")
                continue
            collection_property = bpy.data.bl_rna.properties.get(name)
            delta = proxy.diff(collection, collection_property, context)
            if delta is not None:
                diff._data[name] = diff
        if len(diff._data):
            return diff
        return None

    def update_soa(self, uuid: Uuid, path: Path, soa_members: List[SoaMember]):
        datablock_proxy = self.state.proxies[uuid]
        datablock = self.state.datablocks[uuid]
        datablock_proxy.update_soa(datablock, path, soa_members)

    def append_delayed_updates(self, delayed_updates: Set[T.ID]):
        self._delayed_updates |= delayed_updates
