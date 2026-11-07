#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# robot_server.py -- on-robot bridge for a de-clouded Narwal T10.
# ===========================================================================
# RUNS ON THE ROBOT (Allwinner R16, Tina Linux, ROS1 Kinetic, Python 2.7).
#
# This is the built-in de-cloud hook. app_normal_mode.sh launches
#   /oem/DISK/bin/pita/remote_control/robot_server.py
# INSTEAD OF pita_communicator (the cloud bridge) when this file exists. So
# dropping this in place means the robot never phones home, and we expose the
# robot on the LAN instead.
#
# It (1) reads the robot's status/sensors/map, (2) forwards them to the LAN
# server over MQTT (preferred) or plain JSON-over-TCP (stdlib fallback), and
# (3) accepts start/stop/pause/dock (and later drive) commands and injects them
# back into the robot's own task layer.
#
# !!! CRITICAL (discovered 2026-11) -- THE PITA STACK HAS NO STANDARD ROS MASTER.
# pita is built on a CUSTOM framework ("pita::frame"), not vanilla roscpp/rospy
# pub/sub against a roscore. Consequences for this bridge:
#   * You most likely CANNOT just `rospy.Subscriber(...)` these topics against a
#     roscore -- there may be no roscore, and the transport is pita's own. The
#     rospy subscriptions below are written the standard way but are GATED and
#     marked UNVERIFIED: confirm on the robot whether a ROS master exists
#     (`rostopic list` / `echo $ROS_MASTER_URI`) before relying on them. If not,
#     the subscriptions must be reimplemented against pita::frame (or we read the
#     same data from the driver another way). See NEXT-STEPS.md.
#   * THE MAP IS READ FROM FILES, NOT A ROS SERVICE. maphub writes the map to
#     /userdata/usr/data/maps/ during/after an active cleaning task:
#       pita_grid_seg.map  -> the segmented grid (rooms)         [format TODO]
#       room-attr.data     -> room attributes                    [format TODO]
#       map_rotate.png     -> the robot's own rendered map image  [forward as-is]
#     This bridge watches that directory and forwards what it finds. (Note: an
#     earlier sweep found the dir EMPTY when idle -- the files appear only with a
#     cleaning task; verify timing on the robot.)
#
# DEPENDENCY POLICY (important — the robot has limited Python2 libs):
#   * rospy is assumed present (it is: the whole app is rospy/roscpp).
#   * paho-mqtt is used IF importable; otherwise we fall back to a stdlib
#     JSON-over-TCP server (socket + json only). Verify lib availability on the
#     robot before relying on MQTT -- see NEXT-STEPS.md.
#
# WIRE PROTOCOL: identical JSON payloads to the LAN server; see
# lan-server/lan_server/bridge/base.py for the authoritative definition.
#
# !!! EVERY ROBOT-INTERNAL UNKNOWN IS MARKED WITH  >>> TODO(DISCOVER) <<<  !!!
# None of those are guessed. They must be read off the live robot (watch the
# topic graph / REA pita_clean_system) before this bridge can actually command
# the robot. The sensor/odometry/drive topics below ARE documented and known.
# ===========================================================================

import base64
import json
import os
import socket
import threading
import time

import rospy

# ---------------------------------------------------------------------------
# Message types.
#
# These come from the robot's own packages (pita_msgs, etc.). Import lazily and
# tolerate absence so the file at least loads for inspection off-robot.
# ---------------------------------------------------------------------------
_MSGS_OK = True
try:
    # Drive command: pita_msgs/Velocity (linear + angular). THE drive topic.
    from pita_msgs.msg import Velocity           # noqa: F401
    # Sensor/state messages we read (names per ARCHITECTURE-and-nav-plan.md).
    from pita_msgs.msg import Odometry as PitaOdometry   # /mobile_base/info/odometry
    from pita_msgs.msg import BatteryInfo                # /mobile_base/info/battery
    # Lidar scan wrapper that contains a standard sensor_msgs/LaserScan.
    # from pita_msgs.msg import ScanIndexed            # /lidar/scan_with_pose
except Exception as _e:               # pragma: no cover - only true off-robot
    _MSGS_OK = False
    rospy.logwarn("pita_msgs import failed (off-robot?): %s" % _e) if rospy else None


# ===========================================================================
# KNOWN topics (documented in ARCHITECTURE-and-nav-plan.md — do NOT re-derive).
# ===========================================================================
TOPIC_ODOM = "/mobile_base/info/odometry"          # pita_msgs/Odometry
TOPIC_BATTERY = "/mobile_base/info/battery"        # pita_msgs/BatteryInfo
TOPIC_SENSOR_STATE = "/mobile_base/info/sensor_state"   # SensorState (bumper/cliff bits)
TOPIC_LIDAR = "/lidar/scan_with_pose"              # ScanIndexed(.scan = LaserScan)
TOPIC_REFINED_POSE = "/mapserver/refined_pose"     # pose in the map frame
TOPIC_DRIVE = "/mobile_base/wheel_velocity"        # pita_msgs/Velocity  <-- drive out

# ===========================================================================
# UNKNOWN topics/services — MUST be discovered on the live robot.
# Placeholders only; nothing here is guessed to be correct.
# ===========================================================================
# TWO WAYS TO GET THE MAP:
#  (A) LOCAL MODE (preferred): with roscore up, maphub publishes a STRUCTURED map
#      message (fields decoded from pita_msgs): status_code, height, width, map_id,
#      resolution, origin_x/y/angle, base_x/y/angle, base_station_x/y/angle, and
#      arrays room[], warning[], obs[], clean[]. i.e. a ready occupancy grid + per
#      area room ids -- NO binary-file reverse needed. >>> TODO(CONFIRM): the topic
#      name (likely under /map_hub/...) via `rostopic list | grep -i map` once in
#      local mode, and which msg carries the cell data.
#  (B) FILE MODE (cloud/no-master fallback): read maphub's files in MAPS_DIR.
#      map_rotate.png forwards as-is; pita_grid_seg.map / room-attr.data binary
#      formats are still [format TODO] -- only needed if (A) is unavailable.
#      Files exist only during/after a cleaning task (idle sweep found it EMPTY).
MAPS_DIR = os.environ.get("NARWAL_MAPS_DIR", "/userdata/usr/data/maps")
MAP_GRID_SEG = "pita_grid_seg.map"     # segmented grid (rooms)   [format TODO]
MAP_ROOM_ATTR = "room-attr.data"       # room attributes          [format TODO]
MAP_IMAGE = "map_rotate.png"           # rendered map image       [forward as-is]

# Room-segmentation JSON the robot keeps locally (room_ids + sweep/mop
# scheme_ids). Read directly. (room-attr.data in MAPS_DIR may hold the geometry;
# confirm which is authoritative.)
CLEAN_SCHEME_FILE = os.environ.get(
    "NARWAL_CLEAN_SCHEME",
    "/userdata/usr/data/communicator/clean_scheme/info")   # path seen in captures

# High-level task command interface. Service/channel name confirmed from the cloud
# protocol + firmware: "/pita_clean_system/command" taking {cmd:<int>}. The SAME
# integer codes are used on-device (the cloud bridge just forwards them). Codes
# decoded from the community T10 integration (ha-narwal-t10) + cross-checked here:
#   START/RESUME=5, PAUSE=3, STOP=4, DOCK/UNDOCK=1 (location-dependent toggle).
# In LOCAL mode (robot_server.py present) app_normal_mode.sh starts `roscore` before
# the pita nodes, so /pita_clean_system/command is reachable as a normal ROS
# service/topic. >>> TODO(CONFIRM in local mode): exact ROS type (service vs topic)
# and whether simple commands use RosFuncCommand{command,room_id,request_maps} while
# room cleans use ControllerCmd{cmd,map_id,clean_order,floor_type,pt_clean_*}.
TOPIC_CLEAN_CMD = os.environ.get("NARWAL_CLEAN_CMD_TOPIC", "/pita_clean_system/command")
CMD_CODES = {
    # Decoded 2026-11-03 from the robot's own logs (cloud -> pita_communicator ->
    # clean_system). 3 = start when given clean_order, else pause/resume TOGGLE.
    # NOTE: in LOCAL mode nothing on ROS consumes these (clean_system has no ROS
    # command input) -- the working path is the LAN fake cloud (transport=cloud).
    "start":      3,
    "pause":      3,
    "resume":     3,
    "stop":       4,
    "dock":       1,      # TOGGLE: undocks a docked robot
    "undock":     1,
    "locate":     None,
    "clean_room": 3,      # with clean_order=[room ids], floor_type=[]
}


# ===========================================================================
# Transport abstraction: MQTT if available, else stdlib JSON-over-TCP.
# ===========================================================================
class Transport(object):
    """Base: subclasses push robot->server dicts and deliver server->robot."""
    def __init__(self, on_command):
        self.on_command = on_command   # callable(cmd_str, args_dict)

    def start(self):
        raise NotImplementedError

    def send(self, obj):
        """Forward a robot->server message (dict)."""
        raise NotImplementedError

    def stop(self):
        pass


class MqttTransport(Transport):
    """Preferred transport. Uses paho-mqtt if importable on the robot."""
    def __init__(self, on_command, host, port, prefix, username=None, password=None):
        Transport.__init__(self, on_command)
        import paho.mqtt.client as mqtt   # raises if unavailable -> caller falls back
        self._mqtt = mqtt
        self._prefix = prefix.rstrip("/")
        self._client = mqtt.Client()      # paho 1.x API (likeliest on an old robot)
        if username:
            self._client.username_pw_set(username, password or "")
        # Last-will so the server marks us offline if the robot drops.
        self._client.will_set(self._prefix + "/online", "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._host, self._port = host, port

    def start(self):
        self._client.connect(self._host, self._port, 30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(self._prefix + "/command", qos=1)
        client.publish(self._prefix + "/online", "online", qos=1, retain=True)

    def _on_message(self, client, userdata, msg):
        try:
            obj = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if isinstance(obj, dict) and obj.get("type") == "command":
            self.on_command(obj.get("cmd"), obj.get("args") or {}, obj.get("id"))

    def send(self, obj):
        leaf = obj.get("type", "status")
        retain = leaf in ("map", "rooms", "status")
        self._client.publish(self._prefix + "/" + leaf, json.dumps(obj),
                             qos=1, retain=retain)

    def stop(self):
        try:
            self._client.publish(self._prefix + "/online", "offline", qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


class TcpTransport(Transport):
    """
    Stdlib fallback: a line-delimited JSON TCP SERVER. The LAN server connects
    as a client. Uses only socket + json + threading (all Python2 stdlib), so
    it works even if the robot has no third-party libraries at all.
    """
    def __init__(self, on_command, bind_host="0.0.0.0", bind_port=8765):
        Transport.__init__(self, on_command)
        self._host, self._port = bind_host, bind_port
        self._clients = []
        self._clients_lock = threading.Lock()
        self._srv = None
        self._stop = False

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self._host, self._port))
        self._srv.listen(4)
        t = threading.Thread(target=self._accept_loop)
        t.setDaemon(True)
        t.start()

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _addr = self._srv.accept()
            except Exception:
                break
            with self._clients_lock:
                self._clients.append(conn)
            rt = threading.Thread(target=self._reader, args=(conn,))
            rt.setDaemon(True)
            rt.start()

    def _reader(self, conn):
        buf = b""
        while not self._stop:
            try:
                chunk = conn.recv(65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle(line)
        with self._clients_lock:
            if conn in self._clients:
                self._clients.remove(conn)
        try:
            conn.close()
        except Exception:
            pass

    def _handle(self, line):
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line.decode("utf-8"))
        except Exception:
            return
        if isinstance(obj, dict) and obj.get("type") == "command":
            self.on_command(obj.get("cmd"), obj.get("args") or {}, obj.get("id"))

    def send(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        dead = []
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)

    def stop(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass


def build_transport(on_command):
    """MQTT if configured + importable, else TCP."""
    host = os.environ.get("NARWAL_MQTT_HOST")
    if host:
        try:
            return MqttTransport(
                on_command, host,
                int(os.environ.get("NARWAL_MQTT_PORT", "1883")),
                os.environ.get("NARWAL_BRIDGE_PREFIX", "narwal/bridge"),
                os.environ.get("NARWAL_MQTT_USER") or None,
                os.environ.get("NARWAL_MQTT_PASS") or None,
            )
        except Exception as e:
            rospy.logwarn("MQTT transport unavailable (%s); falling back to TCP" % e)
    return TcpTransport(
        on_command,
        os.environ.get("NARWAL_TCP_BIND", "0.0.0.0"),
        int(os.environ.get("NARWAL_TCP_PORT", "8765")),
    )


# ===========================================================================
# The bridge node.
# ===========================================================================
class RobotServer(object):
    def __init__(self):
        self._transport = build_transport(self._on_command)
        self._drive_pub = None
        self._clean_pub = None

        # cached pieces of status assembled from several topics
        self._status = {
            "state": "idle", "battery": None, "fan_speed": "standard",
            "error": None, "pose": {"x": 0.0, "y": 0.0, "theta": 0.0},
            "clean_area_m2": 0.0, "clean_time_s": 0,
        }
        self._map_id = "robot-" + time.strftime("%Y%m%d-%H%M%S")
        self._map_img_mtime = 0            # last-forwarded map_rotate.png mtime
        self._watch_stop = False

    # --- setup -------------------------------------------------------------
    def start(self):
        # NOTE: init_node assumes a ROS master. pita may have none (pita::frame).
        # If this raises/hangs, skip ROS entirely and rely on the file watcher +
        # whatever status source is available. See the CRITICAL note at the top.
        try:
            rospy.init_node("robot_server", anonymous=False, disable_signals=True)
            self._ros_ok = True
        except Exception as e:
            self._ros_ok = False
            print("rospy.init_node failed (%s) -- running file-only mode" % e)

        self._transport.start()

        if self._ros_ok and _MSGS_OK:
            # Drive publisher (documented topic/type — ready for the nav phase).
            self._drive_pub = rospy.Publisher(TOPIC_DRIVE, Velocity, queue_size=1)

            # >>> TODO(DISCOVER): clean-command publisher/service client once
            #     TOPIC_CLEAN_CMD + message type are known, e.g.:
            #     self._clean_pub = rospy.Publisher(TOPIC_CLEAN_CMD, RosFuncCommand, queue_size=1)

            # --- subscribe to KNOWN topics (UNVERIFIED under pita::frame) ---
            # These are the documented topics/types. They only work if a ROS
            # master + standard pub/sub exist. If not, reimplement against
            # pita::frame (NEXT-STEPS.md). We guard so a failure is non-fatal.
            try:
                # Verified in local mode (rosnode info /clean_system):
                #   /mobile_base/info/battery   sensor_msgs/BatteryState  (NOT pita BatteryInfo)
                #   /mapserver/refined_pose     geometry_msgs/Pose2D      (map frame)
                #   /map_hub/working_progress   pita_msgs/WorkingProgress {percent, area}
                from sensor_msgs.msg import BatteryState
                from geometry_msgs.msg import Pose2D
                rospy.Subscriber(TOPIC_BATTERY, BatteryState, self._cb_battery)
                rospy.Subscriber(TOPIC_REFINED_POSE, Pose2D, self._cb_refined_pose)
                rospy.Subscriber(TOPIC_ODOM, PitaOdometry, self._cb_odom)
                try:
                    from pita_msgs.msg import WorkingProgress
                    rospy.Subscriber("/map_hub/working_progress", WorkingProgress, self._cb_progress)
                except Exception as e:
                    rospy.logwarn("working_progress unavailable: %s" % e)
                # SensorState / lidar go here too (e-stop + odom for map build).
            except Exception as e:
                rospy.logwarn("topic subscribe failed (pita::frame?): %s" % e)

        # Map + rooms come from FILES regardless of ROS availability.
        self._publish_rooms_from_file()
        self._start_map_watcher()

        # Status heartbeat. Under ROS we use the Timer; otherwise a thread.
        if self._ros_ok:
            rospy.Timer(rospy.Duration(2.0), self._tick)
        else:
            t = threading.Thread(target=self._tick_loop)
            t.setDaemon(True)
            t.start()
        print("robot_server up (ros=%s, maps_dir=%s)" % (self._ros_ok, MAPS_DIR))

    def spin(self):
        if getattr(self, "_ros_ok", False):
            rospy.spin()
        else:
            try:
                while not self._watch_stop:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
        self._watch_stop = True
        self._transport.stop()

    # --- ROS callbacks (KNOWN topics) -------------------------------------
    def _cb_battery(self, msg):
        # sensor_msgs/BatteryState.percentage: ROS spec is 0..1; some drivers send 0..100.
        pct = getattr(msg, "percentage", None)
        if pct is None:
            return
        pct = float(pct)
        if pct <= 1.0:
            pct *= 100.0
        self._status["battery"] = int(round(max(0.0, min(100.0, pct))))

    def _cb_refined_pose(self, msg):
        # map-frame pose (what the map is drawn in) -- preferred over odometry.
        # mapserver emits an exact (0,0,0) while unlocalized (e.g. idle on the
        # dock); that's a placeholder, not a position -> ignore it.
        if msg.x == 0.0 and msg.y == 0.0 and msg.theta == 0.0:
            return
        self._have_map_pose = True
        self._status["pose"] = {"x": float(msg.x), "y": float(msg.y), "theta": float(msg.theta)}

    def _cb_progress(self, msg):
        try:
            self._status["clean_area_m2"] = round(float(msg.area), 2)
        except Exception:
            pass

    def _cb_odom(self, msg):
        # odometry frame -- only used until a localized map pose arrives
        if getattr(self, "_have_map_pose", False):
            return
        pose = getattr(msg, "pose", None)
        if pose is not None:
            self._status["pose"] = {
                "x": float(getattr(pose, "x", 0.0)),
                "y": float(getattr(pose, "y", 0.0)),
                "theta": float(getattr(pose, "theta", 0.0)),
            }

    # --- map from maphub files (NOT a ROS topic) --------------------------
    def _start_map_watcher(self):
        """Poll MAPS_DIR and forward the map image + (TODO) the parsed grid."""
        t = threading.Thread(target=self._map_watch_loop)
        t.setDaemon(True)
        t.start()

    def _map_watch_loop(self):
        while not self._watch_stop:
            try:
                self._poll_map_files()
            except Exception as e:
                print("map watch error: %s" % e)
            time.sleep(5.0)

    def _poll_map_files(self):
        img_path = os.path.join(MAPS_DIR, MAP_IMAGE)
        if os.path.exists(img_path):
            mtime = os.path.getmtime(img_path)
            if mtime != self._map_img_mtime:
                self._map_img_mtime = mtime
                f = open(img_path, "rb")
                raw = f.read()
                f.close()
                self._transport.send({
                    "type": "map_image", "ts": time.time(),
                    "map_id": self._map_id, "format": "png",
                    "data_b64": base64.b64encode(raw).decode("ascii"),
                })
                print("forwarded %s (%d bytes)" % (MAP_IMAGE, len(raw)))

        # NOTE (2026-11-02): the map FORMATS are now decoded on the LAN-server
        # side in lan_server/realmap.py (map.bag -> OccupancyGrid via rosbags;
        # pita_grid_seg.map:2 gzip->Boost archive = room bitmask; :3 = names).
        # Cleanest live design: forward the RAW files and let the LAN server
        # parse them (rosbags/Boost parsers don't need to run on the Py2 robot).
        # >>> TODO: forward map.bag + pita_grid_seg.map:2 + ':3' as raw blobs
        #     (base64) on their own message types, OR run the parse here if the
        #     robot has the libs (unlikely on Py2). The files appear only during/
        #     after a cleaning task — confirm the live path (/userdata/usr/data/maps/).
        seg = os.path.join(MAPS_DIR, MAP_GRID_SEG)
        if os.path.exists(seg):
            pass  # forward raw once the live path/timing is confirmed

    # --- status heartbeat --------------------------------------------------
    def _tick_loop(self):
        while not self._watch_stop:
            self._tick(None)
            time.sleep(2.0)

    def _tick(self, _evt):
        out = {"type": "status", "ts": time.time()}
        out.update(self._status)
        self._transport.send(out)

    # --- rooms from the robot's local JSON --------------------------------
    def _publish_rooms_from_file(self):
        try:
            if os.path.exists(CLEAN_SCHEME_FILE):
                f = open(CLEAN_SCHEME_FILE, "r")
                scheme = json.load(f)
                f.close()
                self._transport.send({
                    "type": "rooms", "ts": time.time(),
                    "map_id": self._map_id, "scheme": scheme,
                })
                rospy.loginfo("published room scheme from %s" % CLEAN_SCHEME_FILE)
            else:
                rospy.logwarn("clean_scheme file not found at %s" % CLEAN_SCHEME_FILE)
        except Exception as e:
            rospy.logwarn("reading clean_scheme failed: %s" % e)

    # --- command handling (server -> robot) -------------------------------
    def _on_command(self, cmd, args, cmd_id=None):
        rospy.loginfo("command: %s %s" % (cmd, args))
        ok, detail = self._dispatch(cmd, args)
        self._transport.send({
            "type": "ack", "id": cmd_id, "ok": ok, "detail": detail,
        })

    def _dispatch(self, cmd, args):
        # LAN-server 'clean' = {mode: vacuum|mop, rooms: [ids] | null}.
        # Only whole-house vacuum maps to a decoded code today (start=5). Mop and
        # room-targeted cleans go through ControllerCmd (floor_type / clean_order)
        # whose fields are not decoded yet -> refuse explicitly instead of guessing.
        if cmd == "clean":
            mode = (args or {}).get("mode") or "vacuum"
            rooms = (args or {}).get("rooms")
            if mode == "vacuum" and not rooms:
                cmd, args = "start", {}
            else:
                return (False,
                        ">>> TODO(DISCOVER): %s of rooms %s needs ControllerCmd "
                        "(floor_type/clean_order) -- not decoded yet" % (mode, rooms or "all"))
        code = CMD_CODES.get(cmd, "MISSING")
        if code == "MISSING":
            return False, "unknown command %r" % cmd
        if code is None:
            # Known command name, but its integer code is NOT yet decoded.
            return (False,
                    ">>> TODO(DISCOVER): integer code for '%s' not decoded; "
                    "see NEXT-STEPS.md" % cmd)
        if not TOPIC_CLEAN_CMD or self._clean_pub is None:
            return False, ">>> TODO(DISCOVER): clean-command topic not set"
        # Once codes + topic + msg type are known, publish here, e.g.:
        #   m = RosFuncCommand(); m.command = code
        #   if cmd == "clean_room": m.room_id = int(args.get("room_id", 0))
        #   self._clean_pub.publish(m)
        return True, "sent code %s" % code

    # --- drive (nav phase; documented + ready) ----------------------------
    def drive(self, linear, angular):
        """Publish a raw velocity. Used later by the move_base bridge."""
        if self._drive_pub is None:
            return
        v = Velocity()
        # pita_msgs/Velocity = linear + angular (per the docs).
        try:
            v.linear = float(linear)
            v.angular = float(angular)
        except Exception:
            # field names TODO-verify against the real .msg
            pass
        self._drive_pub.publish(v)


def main():
    node = RobotServer()
    node.start()
    node.spin()


if __name__ == "__main__":
    main()
