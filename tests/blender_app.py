import logging
import time
from typing import Iterable, List, Optional, Mapping
import sys

import tests.blender_lib as bl
from tests.process import BlenderServer

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
logger = logging.getLogger(__name__)

_set_log_level = """
from mixer.bl_preferences import set_log_level
set_log_level(None, {log_level})
"""
_connect = """
import bpy
bpy.ops.mixer.connect()
"""

_disconnect = """
import bpy
bpy.ops.mixer.disconnect()
"""

_create_room = """
from mixer.connection import join_room
join_room("{room_name}", {vrtist_protocol})
"""

_keep_room_open = """
from mixer.share_data import share_data
share_data.client.set_room_keep_open("{room_name}", {keep_room_open})
"""
_set_workspace = """
from mixer.tructruc import set_workspace
set_workspace({workspace_folders})
"""
_join_room = """
from mixer.connection import join_room
from mixer.broadcaster.common import RoomAttributes
from mixer.share_data import share_data
from mixer.blender_client.client import clear_scene_content
import sys
import time

# prevent sending our contents in case of cross join. Easier to diagnose the problem
clear_scene_content()

start = time.monotonic()
max_wait = 30

def wait_joinable():
    share_data.client.send_list_rooms()
    joinable = False
    while not joinable and time.monotonic() - start < max_wait:
        time.sleep(0.1)
        share_data.client.fetch_incoming_commands()
        room_attributes = share_data.client.rooms_attributes.get("{room_name}")
        if room_attributes is not None:
            joinable = room_attributes.get(RoomAttributes.JOINABLE, False)

    return room_attributes is not None and room_attributes.get(RoomAttributes.JOINABLE, False)

if wait_joinable():
    join_room("{room_name}", {vrtist_protocol})
else:
    print(f"ERROR: Cannot join room after {max_wait} seconds. Abort")
    time.sleep(5)
    sys.exit(1)
"""


class BlenderApp:
    def __init__(self, port: int, ptvsd_port: int = None, wait_for_debugger=False):
        self._port = port
        self._ptvsd_port = ptvsd_port
        self._wait_for_debugger = wait_for_debugger
        self._blender: BlenderServer = BlenderServer(self._port, self._ptvsd_port, self._wait_for_debugger)
        self._log_level = logging.WARNING

    def set_log_level(self, log_level: int):
        self._log_level = log_level

    def setup(self, blender_args: List = None, env: Optional[Mapping[str, str]] = None):
        self._blender.start(blender_args, env)
        self._blender.connect()

    def connect_mixer(self):
        """Emit a mixer connect command"""
        if self._log_level is not None:
            set_log_level = _set_log_level.format(log_level=self._log_level)
            self._blender.send_string(set_log_level)

        self._blender.send_string(_connect)

    def create_room(
        self,
        room_name="mixer_unittest",
        keep_room_open=False,
        vrtist_protocol: bool = False,
        workspace_folders: List[str] = (),
    ):
        """Emit a mixer create room command"""
        if workspace_folders:
            set_workspaces = _set_workspace.format(workspace_folders=list(workspace_folders))
            self._blender.send_string(set_workspaces)

        create_room = _create_room.format(room_name=room_name, vrtist_protocol=vrtist_protocol)
        self._blender.send_string(create_room)

        keep_room_open = _keep_room_open.format(room_name=room_name, keep_room_open=keep_room_open)
        self._blender.send_string(keep_room_open)

    def join_room(
        self,
        room_name="mixer_unittest",
        vrtist_protocol: bool = False,
        workspace_folders: Iterable[str] = (),
    ):
        """Emit a mixer join room command"""
        if workspace_folders:
            set_workspaces = _set_workspace.format(workspace_folders=list(workspace_folders))
            self._blender.send_string(set_workspaces)
        join_room = _join_room.format(room_name=room_name, vrtist_protocol=vrtist_protocol, max_wait="{max_wait}")
        self._blender.send_string(join_room)

    def disconnect_mixer(self):
        """Emit a mixer disconnect room command"""
        self._blender.send_string(_disconnect)

    def wait(self, timeout: float = None):
        return self._blender.wait(timeout)
        # time.sleep(60)
        # self._blender.send_function(bl.quit)

    def kill(self):
        self._blender.kill()

    def send_function(self, f, *args, **kwargs):
        self._blender.send_function(f, *args, **kwargs)
        time.sleep(1)

    def send_string(self, s, sleep: float):
        self._blender.send_string(s)
        time.sleep(sleep)

    def quit(self):
        self._blender.send_function(bl.quit)

    def close(self):
        self._blender.close()
