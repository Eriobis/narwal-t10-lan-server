# Narwal T10 — robot modification write-up

What was changed on the robot so it talks to this LAN server instead of the Narwal cloud,
why each change is needed, and how to undo it. For installing and running the server, see
the **Install & use** section of [README.md](README.md).

## Summary

The robot (Allwinner R16, Tina Linux/OpenWrt, kernel 3.4.39, ROS-based "pita" stack)
**navigates and cleans entirely on-device**. The only thing that talks to the internet is the
cloud bridge, `pita_communicator`. So "de-clouding" does not mean replacing the robot's
software. It means changing **who the cloud bridge talks to**.

Three changes, all reversible:

| # | Change on the robot | Done by | Needed for |
|---|---|---|---|
| 1 | `/etc/rc.local`: root adb shell, adb over USB + TCP :5555, wakelock, optional Wi-Fi join | `scripts/first-time-setup.sh` (once) | every other step |
| 2 | Cloud endpoints rewritten to `ws://<server>:8099/cloud/…` + LAN server cert trusted | `scripts/set-server-ip.sh` | **LAN fake-cloud mode** (the normal mode) |
| 3 | *(optional)* `robot_server.py` dropped into the local-mode hook | `scripts/upload-to-robot.sh` | **Local ROS mode** (experimental) |

`scripts/restore-cloud.sh` undoes 2 and 3. Change 1 does not affect normal cloud use.

## 1. Persistent root access (`scripts/rc.local`)

The stock robot exposes adb on USB for **about one second per boot**. Pushing files works
without a login during that window. `first-time-setup.sh` loops `adb push` until it lands our
`rc.local` and this PC's adb public key (`/mnt/UDISK/adb_keys`). After one reboot,
`rc.local` runs as root and:

- points `/bin/adb_shell` at `/bin/ash`, which gives a real root shell (`/bin/login` is not touched);
- turns the USB gadget into adb-only and starts `adbd` on USB **and TCP :5555** (key-authenticated);
- holds a wakelock (`narwaldbg`). Without it the CPU suspends when idle and adb drops on both transports;
- optionally joins Wi-Fi itself (`--ssid/--psk`), so the Narwal app is not needed to get it online.

## 2. LAN fake-cloud (`scripts/set-server-ip.sh`)

`pita_communicator` reads its endpoints from two JSON files in
`/oem/DISK/bin/pita/pita_communicator/conf/`:

- `websocket_env_us.json` → `ws://<server>:8099/cloud/robot_websocket`, with
  `decrypt_incoming_messages` / `encrypt_output_messages` set to **false**, so frames are plain JSON;
- `http_env_us.json` → `http://<server>:8099/cloud`.

The originals are saved once as `*.narwal-orig`. `/oem` is read-only at boot, so the script
remounts it read-write. Then it restarts `pita_communicator`, which connects to the LAN server
(`--transport cloud`, see `lan_server/bridge/cloud.py`). The robot keeps its whole stack:
cleaning, docking, schedules and maps all behave as before.

**TLS for uploads.** The robot always sends log/bag/map uploads over HTTPS (Poco
`HTTPSClientSession`) to port **8443**, and checks the certificate's CN/SAN against the
server **IP**. So the script also:

- generates a self-signed cert for that IP in `data/maps/tls/{cert,key}.pem` (only when the IP changed);
- pushes it to the robot as `/etc/ssl/certs/narwal-lan-cloud.crt` with its OpenSSL hash link `<hash>.0`.

That is why **changing the server IP means re-running this script and restarting the server**.

Protocol, decoded from the robot's own debug logs:

```
server -> robot  {"request_id","machine_id","service","params"}
robot  -> server {"code","machine_id","request_id","result","service","type","version"}

/pita_clean_system/command  {cmd:3, clean_order:[room ids]}  start rooms
                            {cmd:3}  pause/resume toggle   {cmd:4} stop
                            {cmd:1}  dock (TOGGLE: undocks if already docked!)
/pita/config/set_sweep_strength {cmd:10, name:"silent_mode|normal_mode|powerful_mode|super_powerful_mode"}
/pita/info/all {}           status snapshot
app_net_flag {state:true}   "an app is watching" -> robot streams /pita/map (live pose) + /pita/info

robot_status: 100 idle, 300 cleaning, 301 paused, 600 returning/charging, 700 leaving dock
```

## 3. Local ROS mode (`scripts/upload-to-robot.sh`), optional

`app_normal_mode.sh` has a built-in hook. If
`/oem/DISK/bin/pita/remote_control/robot_server.py` exists, it starts `roscore` + that file
**instead of** `pita_communicator`. Uploading `robot/robot_server.py` there and rebooting takes
the robot fully off any cloud, real or fake. Standard ROS tools then work, and the server
connects with `--transport tcp --robot-host <robot-ip>`.

`robot_server.py` is still a skeleton (see the `TODO(DISCOVER)` slots and
[NEXT-STEPS.md](NEXT-STEPS.md)). A broken one does not stop the robot from cleaning; it only
means no remote control. Fake-cloud mode (2) is the one in daily use. Use one mode or the other.

## Undo

```bash
./scripts/restore-cloud.sh   # original endpoints, cert removed, hook removed, reboot
```

To also remove root access, put the stock `/etc/rc.local` back. The setup does not save a copy
of it, so keep one from your own firmware dump.

## Gotchas (do not re-derive)

- `adb exec-out` is unsupported by this 2021 adbd and always returns `error: closed`. Use `adb shell -x`.
- adb push/pull is reliable over **TCP**, flaky over USB. Shell works on both. `narwal.sh` handles this.
- A deeply idle robot will not accept adb at all. Start a task from the app or the dock button to wake it.
- The RTC battery is dead: the clock resets to 1970 each boot until NTP, so file mtimes on the robot are unreliable.
- `cmd:1` (dock) is a **toggle**. Sent while docked, it undocks.
- The robot's IP changes with DHCP. `narwal.sh` reads it over USB and caches it in `scripts/.narwal-last-ip`.
  Attach USB once per network.
