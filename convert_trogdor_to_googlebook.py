#!/usr/bin/env python3
# Copyright 2026 Yajat Kumar
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
googlebook-trogdor — convert_trogdor_to_googlebook.py

Independent hardware/OS interoperability tool (17 U.S.C. § 1201(f)) that combines
a user-supplied LineageOS 24 Trogdor build with user-supplied Googlebook OS (mica)
system partitions into a bootable raw disk image for Qualcomm SC7180 (Trogdor) Chromebooks.

Disclaimer:
- Bring Your Own Files (BYOF): This repository contains ZERO proprietary binaries,
  firmware, keys, or copyrighted OS images. Users must supply their own lawfully
  obtained partition/recovery images locally.
- Independent research project; not affiliated with, endorsed by, or sponsored by
  Google LLC or Qualcomm Incorporated.
"""

import os
import sys
import glob
import time
import shutil
import struct
import zipfile
import argparse
import subprocess
from pathlib import Path

# Defaults
DEFAULT_GBOOK_DIR = Path(__file__).resolve().parent
SUPER_OFFSET = 204800 * 512  # 104,857,600 bytes
SUPER_MAX_SIZE = 12440305664

MISC_SETUP_SCRIPT = """#!/system/bin/sh
# Setup /dev/block/by-name block device symlinks synchronously + tune USB flash storage I/O
mkdir -p /dev/block/by-name
mkdir -p /dev/block/bootdevice
ln -sf /dev/block/by-name /dev/block/bootdevice/by-name

for disk in /dev/block/sd[a-z] /dev/block/mmcblk[0-9] /dev/block/nvme[0-9]n[0-9]; do
    if [ -b "${disk}1" ]; then
        ln -sf "${disk}1" /dev/block/by-name/misc
        ln -sf "${disk}2" /dev/block/by-name/boot_a
        ln -sf "${disk}2" /dev/block/by-name/boot
        ln -sf "${disk}2" /dev/block/by-name/KERN-A
        ln -sf "${disk}3" /dev/block/by-name/metadata
        ln -sf "${disk}4" /dev/block/by-name/super
        ln -sf "${disk}5" /dev/block/by-name/userdata
        break
    elif [ -b "${disk}p1" ]; then
        ln -sf "${disk}p1" /dev/block/by-name/misc
        ln -sf "${disk}p2" /dev/block/by-name/boot_a
        ln -sf "${disk}p2" /dev/block/by-name/boot
        ln -sf "${disk}p2" /dev/block/by-name/KERN-A
        ln -sf "${disk}p3" /dev/block/by-name/metadata
        ln -sf "${disk}p4" /dev/block/by-name/super
        ln -sf "${disk}p5" /dev/block/by-name/userdata
        break
    fi
done

# Eliminate blocking SCSI SYNCHRONIZE CACHE (0x35) / FUA stalls on consumer USB flash storage
for c in /sys/block/sd*/device/scsi_disk/*/cache_type; do
    [ -w "$c" ] && echo "temporary none" > "$c" 2>/dev/null
done

# Tune USB block queues for throughput & low latency
for q in /sys/block/sd*/queue; do
    [ -w "$q/rotational" ] && echo 0 > "$q/rotational" 2>/dev/null
    [ -w "$q/read_ahead_kb" ] && echo 1024 > "$q/read_ahead_kb" 2>/dev/null
    [ -w "$q/nr_requests" ] && echo 256 > "$q/nr_requests" 2>/dev/null
    [ -w "$q/add_random" ] && echo 0 > "$q/add_random" 2>/dev/null
    [ -w "$q/iostats" ] && echo 0 > "$q/iostats" 2>/dev/null
done
"""

DISABLE_SETUPWIZARD_SYSCONFIG = """<?xml version="1.0" encoding="utf-8"?>
<config>
    <!-- Prevent SetupWizard & DesktopSetupWizard from being installed in User 0 or HSUM User 10 on first boot -->
    <install-in-user-type package="com.google.android.setupwizard">
        <do-not-install-in user-type="SYSTEM" />
        <do-not-install-in user-type="FULL" />
        <do-not-install-in user-type="PROFILE" />
    </install-in-user-type>
    <install-in-user-type package="com.google.android.desktop.setupwizard">
        <do-not-install-in user-type="SYSTEM" />
        <do-not-install-in user-type="FULL" />
        <do-not-install-in user-type="PROFILE" />
    </install-in-user-type>
</config>
"""

GOOGLEBOOK_COMPAT_RC = """on early-init
    stop android.system.desktop.security.gscd
    stop vendor.secretkeeper.trusty

on init
    setprop ro.vendor.apex.com.android.hardware.keymint.strongbox.desktop none
    setprop persist.sys.settings.enable_desktop_ui true
    setprop ro.hardware.type CLAMSHELL
    setprop ro.lockscreen.disable.default true
    setprop persist.sys.lockscreen.disable true
    setprop ro.setupwizard.mode DISABLED

    # ADB debugging
    setprop service.adb.tcp.port 5555
    setprop persist.sys.usb.config adb

on post-fs
    # Buffer dirty writes in RAM to prevent USB flash I/O saturation
    write /proc/sys/vm/dirty_background_ratio 20
    write /proc/sys/vm/dirty_ratio 40
    write /proc/sys/vm/dirty_expire_centisecs 6000
    write /proc/sys/vm/dirty_writeback_centisecs 1500

    # Setup by-name block devices synchronously
    mkdir /dev/block/by-name 0755 root root
    mkdir /dev/block/bootdevice 0755 root root
    symlink /dev/block/by-name /dev/block/bootdevice/by-name

    symlink /dev/block/sda1 /dev/block/by-name/misc
    symlink /dev/block/sda2 /dev/block/by-name/boot_a
    symlink /dev/block/sda2 /dev/block/by-name/boot
    symlink /dev/block/sda2 /dev/block/by-name/KERN-A
    symlink /dev/block/sda3 /dev/block/by-name/metadata
    symlink /dev/block/sda4 /dev/block/by-name/super
    symlink /dev/block/sda5 /dev/block/by-name/userdata

    symlink /dev/block/mmcblk0p1 /dev/block/by-name/misc
    symlink /dev/block/mmcblk0p2 /dev/block/by-name/boot_a
    symlink /dev/block/mmcblk0p3 /dev/block/by-name/metadata
    symlink /dev/block/mmcblk0p4 /dev/block/by-name/super
    symlink /dev/block/mmcblk0p5 /dev/block/by-name/userdata

    exec_start misc_setup

on post-fs-data
    setprop service.adb.tcp.port 5555
    setprop sys.usb.config adb
    start adbd

on boot
    write /proc/sys/vm/dirty_background_ratio 20
    write /proc/sys/vm/dirty_ratio 40
    write /proc/sys/vm/dirty_expire_centisecs 6000
    write /proc/sys/vm/dirty_writeback_centisecs 1500

on property:sys.boot_completed=1
    write /proc/sys/vm/dirty_background_ratio 20
    write /proc/sys/vm/dirty_ratio 40
    write /proc/sys/vm/dirty_expire_centisecs 6000
    write /proc/sys/vm/dirty_writeback_centisecs 1500
    exec_start misc_setup

service misc_setup /system/bin/sh /vendor/bin/misc_setup.sh
    class core
    user root
    group root system
    oneshot
"""

def patch_init_binary(init_path):
    print(f"[*] Binary-patching Googlebook init for SELinux permissive mode: {init_path}")
    with open(init_path, "rb") as f:
        data = bytearray(f.read())

    # 1. SelinuxSetEnforcement at 0x101f40 - 0x101f4c:
    # 0x101f40: 1f040071 (cmp w0, #1) -> nop (1f2003d5)
    # 0x101f44: 00010054 (b.eq 101f64) -> nop (1f2003d5)
    # 0x101f48: 20008052 (mov w0, #1) -> mov w0, #0 (00008052)
    # 0x101f4c: 2d2f0694 (bl security_setenforce@plt)
    if data[0x101f48:0x101f4c] == bytes.fromhex("20008052"):
        data[0x101f40:0x101f44] = bytes.fromhex("1f2003d5")
        data[0x101f44:0x101f48] = bytes.fromhex("1f2003d5")
        data[0x101f48:0x101f4c] = bytes.fromhex("00008052")
        print("    [+] Patched SelinuxSetEnforcement to execute security_setenforce(0) [PERMISSIVE]")
    elif data[0x101f48:0x101f4c] == bytes.fromhex("00008052"):
        print("    [+] SelinuxSetEnforcement already patched to permissive")
    else:
        print(f"    [!] Warning: unexpected bytes at 0x101f48: {data[0x101f48:0x101f4c].hex()}")

    # 2. ComputeContextFromExecutable at 0xb1114:
    # 0xb1114: 600d0034 (cbz w0, 0xb12c0) -> nop (1f2003d5)
    if data[0xb1114:0xb1118] == bytes.fromhex("600d0034"):
        data[0xb1114:0xb1118] = bytes.fromhex("1f2003d5")
        print("    [+] Patched ComputeContextFromExecutable to bypass domain transition checks")
    elif data[0xb1114:0xb1118] == bytes.fromhex("1f2003d5"):
        print("    [+] ComputeContextFromExecutable already patched")
    else:
        print(f"    [!] Warning: unexpected bytes at 0xb1114: {data[0xb1114:0xb1118].hex()}")

    with open(init_path, "wb") as f:
        f.write(data)
    os.chmod(init_path, 0o755)

def patch_system_init_rc(init_rc_path):
    print(f"[*] Patching system init.rc: {init_rc_path}")
    rc_content = init_rc_path.read_text()

    early_symlinks = """
    # Setup by-name block devices synchronously for USB / removable boot
    mkdir /dev/block/by-name 0755 root root
    mkdir /dev/block/bootdevice 0755 root root
    symlink /dev/block/by-name /dev/block/bootdevice/by-name

    symlink /dev/block/sda1 /dev/block/by-name/misc
    symlink /dev/block/sda2 /dev/block/by-name/boot_a
    symlink /dev/block/sda2 /dev/block/by-name/boot
    symlink /dev/block/sda2 /dev/block/by-name/KERN-A
    symlink /dev/block/sda3 /dev/block/by-name/metadata
    symlink /dev/block/sda4 /dev/block/by-name/super
    symlink /dev/block/sda5 /dev/block/by-name/userdata

    symlink /dev/block/mmcblk0p1 /dev/block/by-name/misc
    symlink /dev/block/mmcblk0p2 /dev/block/by-name/boot_a
    symlink /dev/block/mmcblk0p2 /dev/block/by-name/boot
    symlink /dev/block/mmcblk0p2 /dev/block/by-name/KERN-A
    symlink /dev/block/mmcblk0p3 /dev/block/by-name/metadata
    symlink /dev/block/mmcblk0p4 /dev/block/by-name/super
    symlink /dev/block/mmcblk0p5 /dev/block/by-name/userdata
"""
    # 1. Inject into on early-init
    early_marker = "bootchart start\n"
    if early_marker in rc_content:
        early_section = rc_content[rc_content.find("on early-init"):rc_content.find("on init")]
        if "/dev/block/sda1" not in early_section:
            rc_content = rc_content.replace(early_marker, early_marker + early_symlinks)
            print("    [+] Injected early block device symlinks into 'on early-init'")

    init_rc_path.write_text(rc_content)
    os.chmod(init_rc_path, 0o644)

def parse_super_metadata(stream, super_offset):
    stream.seek(super_offset)
    super_head = stream.read(131072)
    geom = super_head[4096:8192]
    sz = struct.unpack_from('<I', geom, 40)[0]
    meta = super_head[12288:12288+sz]
    header_size = struct.unpack_from('<I', meta, 8)[0]
    po, pc, ps = struct.unpack_from('<III', meta, 80)
    eo, ec, es = struct.unpack_from('<III', meta, 92)

    partitions = {}
    for i in range(pc):
        p = header_size + po + i * ps
        p_data = meta[p:p+ps]
        name = p_data[:36].split(b'\0')[0].decode('utf-8', 'replace')
        first_extent, num_extents, group_idx = struct.unpack_from('<III', p_data, 40)
        
        for e_idx in range(first_extent, first_extent + num_extents):
            ep = header_size + eo + e_idx * es
            num_sec, target_type, target_data, target_source = struct.unpack_from('<QIQI', meta, ep)
            partitions[name] = {
                'sector_in_super': target_data,
                'sectors': num_sec,
                'byte_offset': super_offset + target_data * 512,
                'byte_size': num_sec * 512,
            }
    return partitions

def unsparse_image(sparse_path, dst_stream, dst_offset):
    print(f"[*] Unsparsing {sparse_path.name} to disk offset {dst_offset}...")
    with open(sparse_path, "rb") as sf:
        hdr = sf.read(28)
        magic, maj, min_v, file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks, crc = struct.unpack("<I4H4I", hdr)
        assert magic == 0xed26ff3a, f"Invalid sparse magic: {hex(magic)}"
        
        dst_stream.seek(dst_offset)
        bytes_written = 0
        
        for _ in range(total_chunks):
            chdr = sf.read(chunk_hdr_sz)
            c_type, c_res, c_blocks, c_total_sz = struct.unpack("<HHII", chdr)
            data_sz = c_blocks * blk_sz
            
            if c_type == 0xcac1:  # Raw
                rem = data_sz
                while rem > 0:
                    n = min(rem, 4 * 1024 * 1024)
                    b = sf.read(n)
                    dst_stream.write(b)
                    rem -= len(b)
                bytes_written += data_sz
            elif c_type == 0xcac2:  # Fill
                fill_val = sf.read(4)
                rem = data_sz
                fill_buf = fill_val * (min(rem, 4 * 1024 * 1024) // 4)
                while rem > 0:
                    n = min(rem, len(fill_buf))
                    dst_stream.write(fill_buf[:n])
                    rem -= n
                bytes_written += data_sz
            elif c_type == 0xcac3:  # Don't care
                dst_stream.seek(data_sz, 1)
                bytes_written += data_sz
            elif c_type == 0xcac4:  # CRC32
                sf.read(4)
                
        dst_stream.flush()
    return bytes_written

def disable_init_service(rc_path):
    if rc_path.exists():
        rc_path.write_text("# disabled on non-GSC Trogdor hardware\n", encoding="utf-8")
        os.chmod(rc_path, 0o644)
        print(f"    [+] Neutralized unsupported hardware init script: {rc_path.name}")

def patch_services_jar(services_jar_path, build_work):
    print(f"[*] Patching {services_jar_path.name} (Watchdog 10m + DropBox/UserManager/HSUM User 10 unlock)...")
    orig_dir = build_work / "orig_system_files"
    orig_dir.mkdir(parents=True, exist_ok=True)
    orig_jar = orig_dir / "services.jar"
    legacy_orig = services_jar_path.with_suffix(".jar.orig")
    if not orig_jar.exists():
        if legacy_orig.exists():
            shutil.copy2(legacy_orig, orig_jar)
        else:
            shutil.copy2(services_jar_path, orig_jar)
    if legacy_orig.exists():
        legacy_orig.unlink()
    shutil.copy2(orig_jar, services_jar_path)
    os.chmod(services_jar_path, 0o644)

    # Remove fsverity metadata and stale prebuilt oat artifacts
    fsv = services_jar_path.with_name("services.jar.fsv_meta")
    if fsv.exists():
        fsv.unlink()
    for oat_f in services_jar_path.parent.glob("oat/arm64/services.*"):
        oat_f.unlink()

    work_dir = build_work / "services_patch"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Extract classes.dex, classes2.dex, and classes3.dex
    subprocess.run(["unzip", "-q", str(services_jar_path), "classes.dex", "classes2.dex", "classes3.dex", "-d", str(work_dir)], check=True)

    # 1. Disassemble classes.dex (preserve API 29+ / DEX 039 for Java 8+ interface methods)
    smali1 = work_dir / "smali1"
    subprocess.run(["baksmali", "disassemble", "-a", "29", str(work_dir / "classes.dex"), "-o", str(smali1)], check=True)

    # Patch Watchdog.smali (extend timeout from 60s to 10 minutes: 0x927c0 ms)
    wd_path = smali1 / "com/android/server/Watchdog.smali"
    wd_text = wd_path.read_text()
    wd_text = wd_text.replace("DEFAULT_TIMEOUT:J = 0xea60L", "DEFAULT_TIMEOUT:J = 0x927c0L")
    wd_text = wd_text.replace(
        "const-wide/32 v1, 0xea60\n\n    iput-wide v1, p0, Lcom/android/server/Watchdog;->mWatchdogTimeoutMillis:J",
        "const-wide/32 v1, 0x927c0\n\n    iput-wide v1, p0, Lcom/android/server/Watchdog;->mWatchdogTimeoutMillis:J"
    )
    old_update = """    sget-boolean v0, Landroid/os/Build;->IS_USERDEBUG:Z

    if-nez v0, :cond_c

    const-wide/16 v0, 0x4e20

    cmp-long v0, p1, v0

    if-gtz v0, :cond_c

    .line 620
    const-wide/16 p1, 0x4e21

    .line 622
    :cond_c
    iput-wide p1, p0, Lcom/android/server/Watchdog;->mWatchdogTimeoutMillis:J"""
    new_update = """    const-wide/32 v0, 0x927c0

    cmp-long v2, p1, v0

    if-gez v2, :cond_c

    move-wide p1, v0

    :cond_c
    iput-wide p1, p0, Lcom/android/server/Watchdog;->mWatchdogTimeoutMillis:J"""
    if old_update in wd_text:
        wd_text = wd_text.replace(old_update, new_update, 1)
    wd_path.write_text(wd_text)
    print("    [+] Patched Watchdog.smali timeout to 600,000ms (10 minutes)")

    # Patch ActivityManagerService.smali (prevent DropBox WTF infinite storm before dropbox is ready)
    ams_path = smali1 / "com/android/server/am/ActivityManagerService.smali"
    ams_text = ams_path.read_text()
    target_ams = ":try_start_14\n    iget-object v0, v1, Lcom/android/server/am/ActivityManagerService;->mContext:Landroid/content/Context;\n\n    const-class v5, Landroid/os/DropBoxManager;"
    assert target_ams in ams_text, "Failed to find target in ActivityManagerService.smali"
    replacement_ams = """    const-string/jumbo v0, "dropbox"

    invoke-static {v0}, Landroid/os/ServiceManager;->checkService(Ljava/lang/String;)Landroid/os/IBinder;

    move-result-object v0

    if-nez v0, :cond_dropbox_ready

    return-void

    :cond_dropbox_ready
""" + target_ams
    ams_text = ams_text.replace(target_ams, replacement_ams, 1)

    # Patch ActivityManagerService handleBindApplicationTimeoutHard & Soft to eliminate cold startup ANRs on USB
    target_hard = ".method private handleBindApplicationTimeoutHard(Lcom/android/server/am/ProcessRecord;)V\n    .registers 5\n    .param p1, \"app\"    # Lcom/android/server/am/ProcessRecord;"
    replacement_hard = ".method private handleBindApplicationTimeoutHard(Lcom/android/server/am/ProcessRecord;)V\n    .registers 5\n    .param p1, \"app\"    # Lcom/android/server/am/ProcessRecord;\n    return-void"
    if target_hard in ams_text:
        ams_text = ams_text.replace(target_hard, replacement_hard, 1)
        print("    [+] Patched ActivityManagerService.smali (disabled handleBindApplicationTimeoutHard)")

    target_soft = ".method private handleBindApplicationTimeoutSoft(Lcom/android/server/am/ProcessRecord;I)V\n    .registers 13\n    .param p1, \"app\"    # Lcom/android/server/am/ProcessRecord;\n    .param p2, \"softTimeoutMillis\"    # I"
    replacement_soft = ".method private handleBindApplicationTimeoutSoft(Lcom/android/server/am/ProcessRecord;I)V\n    .registers 13\n    .param p1, \"app\"    # Lcom/android/server/am/ProcessRecord;\n    .param p2, \"softTimeoutMillis\"    # I\n    return-void"
    if target_soft in ams_text:
        ams_text = ams_text.replace(target_soft, replacement_soft, 1)
        print("    [+] Patched ActivityManagerService.smali (disabled handleBindApplicationTimeoutSoft)")

    ams_path.write_text(ams_text)
    print("    [+] Patched ActivityManagerService.smali (guarded early addErrorToDropBox against recursion)")

    # Patch UserController.smali:
    # Keep stock mAllowUserUnlocking guard during early boot, and ensure HSUM foreground User 10
    # transitions STATE_BOOTING (0) -> STATE_RUNNING_LOCKED (1) -> STATE_RUNNING_UNLOCKED (3) on boot complete
    # even when WindowManagerService.moveUserToForeground skips duplicate HOME launch during HSUM boot.
    uc_path = smali1 / "com/android/server/am/UserController.smali"
    uc_text = uc_path.read_text()

    target_uc_boot = """    if-nez v4, :cond_37

    .line 3571
    invoke-direct {p0, v3, p1}, Lcom/android/server/am/UserController;->finishUserBoot(Lcom/android/server/am/UserState;Landroid/content/IIntentReceiver;)V

    goto :goto_3d

    .line 3573
    :cond_37
    invoke-direct {p0, p1, v1}, Lcom/android/server/am/UserController;->sendLockedBootCompletedBroadcast(Landroid/content/IIntentReceiver;I)V"""

    replacement_uc_boot = """    if-nez v4, :cond_37

    :cond_finish_hsum_boot
    .line 3571
    invoke-direct {p0, v3, p1}, Lcom/android/server/am/UserController;->finishUserBoot(Lcom/android/server/am/UserState;Landroid/content/IIntentReceiver;)V

    goto :goto_3d

    .line 3573
    :cond_37
    iget v4, v3, Lcom/android/server/am/UserState;->state:I

    if-nez v4, :cond_hsum_already_booted

    goto :cond_finish_hsum_boot

    :cond_hsum_already_booted
    invoke-direct {p0, p1, v1}, Lcom/android/server/am/UserController;->sendLockedBootCompletedBroadcast(Landroid/content/IIntentReceiver;I)V"""

    assert target_uc_boot in uc_text, "Failed to find onBootComplete target in UserController.smali"
    uc_text = uc_text.replace(target_uc_boot, replacement_uc_boot, 1)

    target_uc_maybe = """    .line 2508
    :cond_42
    new-instance v0, Lcom/android/server/utils/TimingsTraceAndSlog;"""

    replacement_uc_maybe = """    .line 2508
    :cond_42
    iget v0, v2, Lcom/android/server/am/UserState;->state:I

    if-nez v0, :cond_state_booted

    invoke-direct {p0, v2}, Lcom/android/server/am/UserController;->finishUserBoot(Lcom/android/server/am/UserState;)V

    const/4 v0, 0x1

    return v0

    :cond_state_booted
    new-instance v0, Lcom/android/server/utils/TimingsTraceAndSlog;"""

    assert target_uc_maybe in uc_text, "Failed to find maybeUnlockUser :cond_42 target in UserController.smali"
    uc_text = uc_text.replace(target_uc_maybe, replacement_uc_maybe, 1)

    # Native user provisioning inside UserController (marks device_provisioned & user_setup_complete directly in system_server)
    target_fub = """    const/16 v1, 0x757e

    invoke-static {v1, v0}, Landroid/util/EventLog;->writeEvent(II)I"""
    replacement_fub = target_fub + "\n\n    invoke-direct {p0, v0}, Lcom/android/server/am/UserController;->ensureUserProvisioned(I)V"
    assert target_fub in uc_text, "Failed to find finishUserBoot EventLog target in UserController.smali"
    uc_text = uc_text.replace(target_fub, replacement_fub, 1)

    target_fuuc = """    const/16 v0, 0x7578

    invoke-static {v0, v3}, Landroid/util/EventLog;->writeEvent(II)I"""
    replacement_fuuc = target_fuuc + "\n\n    invoke-direct {v1, v3}, Lcom/android/server/am/UserController;->ensureUserProvisioned(I)V"
    assert target_fuuc in uc_text, "Failed to find finishUserUnlockedCompleted EventLog target in UserController.smali"
    uc_text = uc_text.replace(target_fuuc, replacement_fuuc, 1)

    provision_helper = """
.method private ensureUserProvisioned(I)V
    .registers 5
    .param p1, "userId"    # I

    :try_start_prov
    iget-object v0, p0, Lcom/android/server/am/UserController;->mInjector:Lcom/android/server/am/UserController$Injector;

    invoke-virtual {v0}, Lcom/android/server/am/UserController$Injector;->getContext()Landroid/content/Context;

    move-result-object v0

    if-eqz v0, :cond_prov_done

    invoke-virtual {v0}, Landroid/content/Context;->getContentResolver()Landroid/content/ContentResolver;

    move-result-object v0

    if-eqz v0, :cond_prov_done

    const/4 v1, 0x1

    const-string v2, "device_provisioned"

    invoke-static {v0, v2, v1}, Landroid/provider/Settings$Global;->putInt(Landroid/content/ContentResolver;Ljava/lang/String;I)Z

    const-string/jumbo v2, "user_setup_complete"

    invoke-static {v0, v2, v1, p1}, Landroid/provider/Settings$Secure;->putIntForUser(Landroid/content/ContentResolver;Ljava/lang/String;II)Z

    const-string/jumbo v2, "tv_user_setup_complete"

    invoke-static {v0, v2, v1, p1}, Landroid/provider/Settings$Secure;->putIntForUser(Landroid/content/ContentResolver;Ljava/lang/String;II)Z

    const/16 v1, 0xa

    const-string/jumbo v2, "user_setup_personalization_state"

    invoke-static {v0, v2, v1, p1}, Landroid/provider/Settings$Secure;->putIntForUser(Landroid/content/ContentResolver;Ljava/lang/String;II)Z

    const/4 v1, 0x0

    const-string v2, "anr_show_background"

    invoke-static {v0, v2, v1, p1}, Landroid/provider/Settings$Secure;->putIntForUser(Landroid/content/ContentResolver;Ljava/lang/String;II)Z

    const/4 v1, 0x7

    const-string/jumbo v2, "stay_on_while_plugged_in"

    invoke-static {v0, v2, v1}, Landroid/provider/Settings$Global;->putInt(Landroid/content/ContentResolver;Ljava/lang/String;I)Z
    :try_end_prov
    .catchall {:try_start_prov .. :try_end_prov} :catchall_prov

    :cond_prov_done
    :goto_prov_done
    return-void

    :catchall_prov
    move-exception v0

    goto :goto_prov_done
.end method

# virtual methods
"""
    assert "\n# virtual methods\n" in uc_text, "Failed to find '# virtual methods' section in UserController.smali"
    uc_text = uc_text.replace("\n# virtual methods\n", provision_helper, 1)

    uc_path.write_text(uc_text)
    print("    [+] Patched UserController.smali (HSUM User 10 unlock + native Settings provisioning)")

    # 2. Disassemble classes2.dex
    smali2 = work_dir / "smali2"
    subprocess.run(["baksmali", "disassemble", "-a", "29", str(work_dir / "classes2.dex"), "-o", str(smali2)], check=True)

    # Patch DisplayManagerService.smali (prevent premature getSystemService('user') WTF)
    dms_path = smali2 / "com/android/server/display/DisplayManagerService.smali"
    dms_text = dms_path.read_text()
    old_gum = """.method private getUserManager()Landroid/os/UserManager;
    .registers 3

    .line 4418
    iget-object v0, p0, Lcom/android/server/display/DisplayManagerService;->mContext:Landroid/content/Context;

    const-class v1, Landroid/os/UserManager;

    invoke-virtual {v0, v1}, Landroid/content/Context;->getSystemService(Ljava/lang/Class;)Ljava/lang/Object;

    move-result-object v0

    check-cast v0, Landroid/os/UserManager;

    return-object v0
.end method"""
    assert old_gum in dms_text, "Failed to find old_gum in DisplayManagerService.smali"
    new_gum = """.method private getUserManager()Landroid/os/UserManager;
    .registers 3

    const-string/jumbo v0, "user"

    invoke-static {v0}, Landroid/os/ServiceManager;->checkService(Ljava/lang/String;)Landroid/os/IBinder;

    move-result-object v0

    if-nez v0, :cond_user_ok

    const/4 v0, 0x0

    return-object v0

    :cond_user_ok
    iget-object v0, p0, Lcom/android/server/display/DisplayManagerService;->mContext:Landroid/content/Context;

    const-class v1, Landroid/os/UserManager;

    invoke-virtual {v0, v1}, Landroid/content/Context;->getSystemService(Ljava/lang/Class;)Ljava/lang/Object;

    move-result-object v0

    check-cast v0, Landroid/os/UserManager;

    return-object v0
.end method"""
    dms_text = dms_text.replace(old_gum, new_gum, 1)
    dms_path.write_text(dms_text)
    print("    [+] Patched DisplayManagerService.smali (guarded getUserManager before UserManager ready)")

    # 3. Disassemble classes3.dex
    smali3 = work_dir / "smali3"
    subprocess.run(["baksmali", "disassemble", "-a", "29", str(work_dir / "classes3.dex"), "-o", str(smali3)], check=True)

    # Patch KeyguardServiceDelegate.smali (immediately signal onDrawn if keyguard service is null)
    ksd_path = smali3 / "com/android/server/policy/keyguard/KeyguardServiceDelegate.smali"
    ksd_text = ksd_path.read_text()
    target_ksd = """:cond_15
    const-string/jumbo v0, "onScreenTurningOn(): no keyguard service!"

    invoke-static {v1, v0}, Landroid/util/Slog;->w(Ljava/lang/String;Ljava/lang/String;)I"""
    replacement_ksd = """:cond_15
    const-string/jumbo v0, "onScreenTurningOn(): no keyguard service!"

    invoke-static {v1, v0}, Landroid/util/Slog;->w(Ljava/lang/String;Ljava/lang/String;)I

    iget-object v0, p0, Lcom/android/server/policy/keyguard/KeyguardServiceDelegate;->mCallback:Lcom/android/server/policy/keyguard/KeyguardServiceDelegate$StateCallback;

    if-eqz v0, :cond_drawn_done

    invoke-interface {v0}, Lcom/android/server/policy/keyguard/KeyguardServiceDelegate$StateCallback;->onDrawn()V

    :cond_drawn_done"""
    assert target_ksd in ksd_text, "Failed to find target in KeyguardServiceDelegate.smali"
    ksd_text = ksd_text.replace(target_ksd, replacement_ksd, 1)
    ksd_path.write_text(ksd_text)
    print("    [+] Patched KeyguardServiceDelegate.smali (immediate onDrawn when no keyguard service)")

    # 4. Assemble all three dex files with API 29 (emits dex\n039\x00, preventing IncompatibleClassChangeError)
    subprocess.run(["smali", "assemble", "-a", "29", str(smali1), "-o", str(work_dir / "classes.dex")], check=True)
    subprocess.run(["smali", "assemble", "-a", "29", str(smali2), "-o", str(work_dir / "classes2.dex")], check=True)
    subprocess.run(["smali", "assemble", "-a", "29", str(smali3), "-o", str(work_dir / "classes3.dex")], check=True)
    for dex_name in ("classes.dex", "classes2.dex", "classes3.dex"):
        dex_hdr = (work_dir / dex_name).read_bytes()[:8]
        assert dex_hdr == b"dex\n039\x00", f"{dex_name} has unexpected DEX header {dex_hdr!r} (expected dex\\n039\\x00)"

    # 5. Update services.jar
    subprocess.run(["zip", "-q", "-u", str(services_jar_path), "classes.dex", "classes2.dex", "classes3.dex"], cwd=str(work_dir), check=True)
    print(f"    [+] Successfully updated {services_jar_path.name} (verified DEX 039 headers)")

def patch_desktop_services_jar(jar_path, build_work):
    print(f"[*] Patching {jar_path.name} (disable blocking waitForService in DesktopRepairModeService)...")
    orig_dir = build_work / "orig_system_files"
    orig_dir.mkdir(parents=True, exist_ok=True)
    orig_jar = orig_dir / "desktop-services.jar"
    legacy_orig = jar_path.with_suffix(".jar.orig")
    if not orig_jar.exists():
        if legacy_orig.exists():
            shutil.copy2(legacy_orig, orig_jar)
        else:
            shutil.copy2(jar_path, orig_jar)
    if legacy_orig.exists():
        legacy_orig.unlink()
    shutil.copy2(orig_jar, jar_path)
    os.chmod(jar_path, 0o644)

    # Remove fsverity metadata and stale odex/vdex
    fsv = jar_path.with_name("desktop-services.jar.fsv_meta")
    if fsv.exists():
        fsv.unlink()
    for oat_f in jar_path.parent.glob("oat/arm64/desktop-services.*"):
        oat_f.unlink()

    work_dir = build_work / "desktop_services_patch"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(["unzip", "-q", str(jar_path), "classes.dex", "-d", str(work_dir)], check=True)
    subprocess.run(["baksmali", "disassemble", "-a", "29", str(work_dir / "classes.dex"), "-o", str(work_dir / "smali")], check=True)

    repair_path = work_dir / "smali/com/android/server/desktop/repairmode/DesktopRepairModeService.smali"
    if repair_path.exists():
        content = repair_path.read_text(encoding="utf-8")
        target = ".method public onBootPhase(I)V\n    .registers 4\n    .param p1, \"phase\"    # I"
        replacement = ".method public onBootPhase(I)V\n    .registers 4\n    .param p1, \"phase\"    # I\n    return-void"
        if target in content:
            content = content.replace(target, replacement, 1)

        target2 = ".method private final readPostManufacturingConfig()Lcom/google/android/factory/base/proto/postmanufacturing/Config;\n    .registers 9"
        replacement2 = ".method private final readPostManufacturingConfig()Lcom/google/android/factory/base/proto/postmanufacturing/Config;\n    .registers 9\n    const/4 v0, 0x0\n    return-object v0"
        if target2 in content:
            content = content.replace(target2, replacement2, 1)
        repair_path.write_text(content, encoding="utf-8")
        print("    [+] Disabled blocking DesktopRepairModeService onBootPhase")

    # Replace any waitForService with checkService across desktop-services
    for smali_file in (work_dir / "smali").rglob("*.smali"):
        txt = smali_file.read_text(encoding="utf-8")
        if "Landroid/os/ServiceManager;->waitForService(Ljava/lang/String;)Landroid/os/IBinder;" in txt:
            txt = txt.replace("Landroid/os/ServiceManager;->waitForService(Ljava/lang/String;)Landroid/os/IBinder;", "Landroid/os/ServiceManager;->checkService(Ljava/lang/String;)Landroid/os/IBinder;")
            smali_file.write_text(txt, encoding="utf-8")

    subprocess.run(["smali", "assemble", "-a", "29", str(work_dir / "smali"), "-o", str(work_dir / "classes.dex")], check=True)
    dex_hdr = (work_dir / "classes.dex").read_bytes()[:8]
    assert dex_hdr == b"dex\n039\x00", f"desktop-services classes.dex has unexpected DEX header {dex_hdr!r}"
    subprocess.run(["zip", "-q", "-u", str(jar_path), "classes.dex"], cwd=str(work_dir), check=True)
    print(f"    [+] Successfully updated {jar_path.name} (verified DEX 039 header)")

EXTRA_FILE_CONTEXTS = """
/dev/block/by-name/super                          u:object_r:super_block_device:s0
/dev/block/by-name/metadata                       u:object_r:metadata_block_device:s0
/dev/block/by-name/userdata                       u:object_r:userdata_block_device:s0
/dev/block/by-name/boot(_[ab])?                   u:object_r:boot_block_device:s0
/dev/block/bootdevice/by-name/misc                u:object_r:misc_block_device:s0
/dev/block/bootdevice/by-name/super               u:object_r:super_block_device:s0
/dev/block/bootdevice/by-name/metadata            u:object_r:metadata_block_device:s0
/dev/block/bootdevice/by-name/userdata            u:object_r:userdata_block_device:s0
/dev/block/bootdevice/by-name/boot(_[ab])?         u:object_r:boot_block_device:s0
/dev/block/sd[a-z]1                               u:object_r:misc_block_device:s0
/dev/block/sd[a-z]2                               u:object_r:boot_block_device:s0
/dev/block/sd[a-z]3                               u:object_r:metadata_block_device:s0
/dev/block/sd[a-z]4                               u:object_r:super_block_device:s0
/dev/block/sd[a-z]5                               u:object_r:userdata_block_device:s0
/dev/block/mmcblk[0-9]p1                          u:object_r:misc_block_device:s0
/dev/block/mmcblk[0-9]p2                          u:object_r:boot_block_device:s0
/dev/block/mmcblk[0-9]p3                          u:object_r:metadata_block_device:s0
/dev/block/mmcblk[0-9]p4                          u:object_r:super_block_device:s0
/dev/block/mmcblk[0-9]p5                          u:object_r:userdata_block_device:s0
/(vendor|system/vendor)/bin/init_by_name\\.sh      u:object_r:vendor_shell_exec:s0
/(vendor|system/vendor)/bin/misc_setup\\.sh        u:object_r:vendor_shell_exec:s0
"""


def resolve_tool(name, explicit_path, gbook_dir):
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    candidates.append(gbook_dir / "tools_tmp/usr/bin" / name)
    which_hit = shutil.which(name)
    if which_hit:
        candidates.append(Path(which_hit))
    candidates.append(Path.home() / "Desktop/lineage-24/out/host/linux-x86/bin" / name)
    for c in candidates:
        if c and c.exists() and os.access(c, os.X_OK):
            return c.resolve()
    return None


def find_gpt_partition_offset(stream, target_name):
    stream.seek(512)
    hdr = stream.read(88)
    if hdr[:8] != b"EFI PART":
        raise RuntimeError("Invalid GPT header on disk image")
    part_lba, num_parts, part_sz = struct.unpack_from("<QII", hdr, 72)
    stream.seek(part_lba * 512)
    for _ in range(num_parts):
        ent = stream.read(part_sz)
        if not ent or ent[:16] == b"\x00" * 16:
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", ent, 32)
        name = ent[56:128].decode("utf-16le", errors="ignore").split("\x00")[0]
        if name == target_name:
            return first_lba * 512, (last_lba - first_lba + 1) * 512
    raise RuntimeError(f"GPT partition '{target_name}' not found in image")


def extract_mica_partitions_if_needed(gbook_dir, mica_image_arg):
    needed = ["system_a.img", "product_a.img", "system_ext_a.img"]
    if all((gbook_dir / n).exists() for n in needed) and not mica_image_arg:
        return

    mica_src = Path(mica_image_arg).resolve() if mica_image_arg else (gbook_dir / "mica-recovery.zip")
    if not mica_src.exists():
        missing = [n for n in needed if not (gbook_dir / n).exists()]
        print(f"[-] Error: Missing user-supplied mica partitions: {', '.join(missing)}")
        print("    Provide system_a.img, product_a.img, and system_ext_a.img in the workspace,")
        print("    or pass --mica-image <path-to-mica-recovery.zip-or-raw-bin>.")
        sys.exit(1)

    print(f"[*] Extracting system_a, product_a, and system_ext_a from {mica_src.name}...")

    def dump_from_stream(stream):
        super_off, _ = find_gpt_partition_offset(stream, "super")
        parts = parse_super_metadata(stream, super_off)
        for part_key in ("system_a", "product_a", "system_ext_a"):
            assert part_key in parts, f"Dynamic partition {part_key} not found in mica super!"
            out_file = gbook_dir / f"{part_key}.img"
            if out_file.exists() and not mica_image_arg:
                continue
            print(f"    [*] Extracting {part_key}.img ({parts[part_key]['byte_size'] / (1024*1024):.2f} MB)...")
            stream.seek(parts[part_key]["byte_offset"])
            rem = parts[part_key]["byte_size"]
            with open(out_file, "wb") as out:
                while rem > 0:
                    chunk = stream.read(min(rem, 8 * 1024 * 1024))
                    if not chunk:
                        break
                    out.write(chunk)
                    rem -= len(chunk)

    if zipfile.is_zipfile(mica_src):
        with zipfile.ZipFile(mica_src) as zf:
            bin_entries = [n for n in zf.namelist() if n.endswith((".bin", ".img", ".raw"))]
            assert bin_entries, f"No disk image found inside {mica_src}"
            with zf.open(bin_entries[0]) as stream:
                dump_from_stream(stream)
    else:
        with open(mica_src, "rb") as stream:
            dump_from_stream(stream)


def build_combined_file_contexts(out_fc_path, gbook_sys_dir, trogdor_vendor_dir, gbook_sys_ext_img, gbook_product_img, dump_erofs, fsck_erofs, build_work):
    chunks = []
    plat_fc = gbook_sys_dir / "system/etc/selinux/plat_file_contexts"
    if plat_fc.exists():
        chunks.append(plat_fc.read_text(encoding="utf-8", errors="ignore"))

    def read_erofs_fc(img_path, rel_path, label):
        if dump_erofs:
            res = subprocess.run(
                [str(dump_erofs), "--cat", f"--path={rel_path}", str(img_path)],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
        tmp_dir = build_work / f"fc_{label}"
        tmp_dir.mkdir(exist_ok=True)
        subprocess.run([str(fsck_erofs), f"--extract={tmp_dir}", str(img_path)], capture_output=True)
        target = tmp_dir / rel_path.lstrip("/")
        txt = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return txt

    chunks.append(read_erofs_fc(gbook_sys_ext_img, "/etc/selinux/system_ext_file_contexts", "sys_ext"))
    chunks.append(read_erofs_fc(gbook_product_img, "/etc/selinux/product_file_contexts", "product"))

    for v_fc in (
        trogdor_vendor_dir / "etc/selinux/vendor_file_contexts",
        trogdor_vendor_dir / "odm/etc/selinux/odm_file_contexts",
    ):
        if v_fc.exists():
            chunks.append(v_fc.read_text(encoding="utf-8", errors="ignore"))

    chunks.append(EXTRA_FILE_CONTEXTS)
    out_fc_path.write_text("\n".join(c.strip() for c in chunks if c.strip()) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Build a bootable Googlebook OS raw disk image for Qualcomm SC7180 (Trogdor) Chromebooks")
    parser.add_argument("--lineage-zip", help="Path to LineageOS 24 Trogdor zip file")
    parser.add_argument("--mica-image", help="Optional path to mica recovery zip/bin (if system_a.img, product_a.img, system_ext_a.img are not yet extracted)")
    parser.add_argument("--output", help="Output raw disk image path")
    parser.add_argument("--gbook-dir", default=str(DEFAULT_GBOOK_DIR), help="Workspace directory containing input images")
    parser.add_argument("--lpmake", help="Path to lpmake binary")
    parser.add_argument("--mkfs-erofs", help="Path to mkfs.erofs binary")
    parser.add_argument("--fsck-erofs", help="Path to fsck.erofs binary")
    args = parser.parse_args()

    gbook_dir = Path(args.gbook_dir).resolve()
    fsck_erofs = resolve_tool("fsck.erofs", args.fsck_erofs, gbook_dir)
    mkfs_erofs = resolve_tool("mkfs.erofs", args.mkfs_erofs, gbook_dir)
    dump_erofs = resolve_tool("dump.erofs", None, gbook_dir)
    lpmake_bin = resolve_tool("lpmake", args.lpmake, gbook_dir)

    missing_tools = []
    if not fsck_erofs:
        missing_tools.append("fsck.erofs (erofs-utils)")
    if not mkfs_erofs:
        missing_tools.append("mkfs.erofs (erofs-utils)")
    if not lpmake_bin:
        missing_tools.append("lpmake (android-sdk-libsparse-utils / lpunpack)")
    for req_cmd in ("baksmali", "smali", "zip", "unzip", "sgdisk"):
        if not shutil.which(req_cmd):
            missing_tools.append(req_cmd)
    if missing_tools:
        print(f"[-] Missing required host tools: {', '.join(missing_tools)}")
        sys.exit(1)

    extract_mica_partitions_if_needed(gbook_dir, args.mica_image)
    gbook_product_img = gbook_dir / "product_a.img"
    gbook_sys_ext_img = gbook_dir / "system_ext_a.img"

    # 1. Locate Lineage 24 zip
    if args.lineage_zip:
        lineage_zip_path = Path(args.lineage_zip).resolve()
    else:
        desktop_zips = sorted(glob.glob(str(gbook_dir / "lineage-24-trogdor-*.zip"))) or sorted(
            glob.glob(str(Path.home() / "Desktop/lineage-24-trogdor-*.zip"))
        )
        if not desktop_zips:
            print("[-] Error: No lineage-24-trogdor-*.zip found! Pass --lineage-zip explicitly.")
            sys.exit(1)
        lineage_zip_path = Path(desktop_zips[-1])

    print(f"[+] Using LineageOS 24 build: {lineage_zip_path}")
    assert lineage_zip_path.exists(), f"File not found: {lineage_zip_path}"

    # Output path
    if args.output:
        out_img_path = Path(args.output).resolve()
    else:
        date_str = time.strftime("%Y%m%d")
        out_img_path = gbook_dir / f"googlebook-trogdor-{date_str}.img"

    build_work = gbook_dir / "build_work"
    build_work.mkdir(exist_ok=True)
    file_contexts = build_work / "combined_file_contexts"

    # 2. Extract hardware partitions from Lineage zip
    print("[*] Inspecting Lineage dynamic partition metadata...")
    with zipfile.ZipFile(lineage_zip_path) as z:
        img_name = [n for n in z.namelist() if n.endswith(".img")][0]
        print(f"[*] Reading disk image inside zip: {img_name}")
        with z.open(img_name) as f:
            parts = parse_super_metadata(f, SUPER_OFFSET)
            print(f"[+] Found {len(parts)} dynamic partitions in Lineage super:")
            for p, d in parts.items():
                print(f"    - {p:15s}: {d['byte_size'] / (1024*1024):.2f} MB")

            def dump_part(name, out_file):
                print(f"[*] Extracting {name} ({parts[name]['byte_size'] / (1024*1024):.2f} MB)...")
                f.seek(parts[name]['byte_offset'])
                with open(out_file, "wb") as out:
                    rem = parts[name]['byte_size']
                    while rem > 0:
                        chunk = f.read(min(rem, 8 * 1024 * 1024))
                        if not chunk:
                            break
                        out.write(chunk)
                        rem -= len(chunk)

            lineage_vendor_img = build_work / "trogdor_vendor_raw.img"
            lineage_vdlkm_img = build_work / "trogdor_vendor_dlkm.img"
            lineage_sdlkm_img = build_work / "trogdor_system_dlkm.img"

            dump_part("vendor_a", lineage_vendor_img)
            dump_part("vendor_dlkm_a", lineage_vdlkm_img)
            dump_part("system_dlkm_a", lineage_sdlkm_img)

            # Copy pre-super boot header (MBR, GPT, misc, KERN-A, metadata)
            print(f"[*] Creating base disk image: {out_img_path}...")
            f.seek(0)
            pre_super_data = f.read(SUPER_OFFSET)
            with open(out_img_path, "wb") as out:
                out.write(pre_super_data)

    # 3. Prepare Googlebook system and ensure native Googlebook SurfaceFlinger & graphics libs
    print("[*] Preparing Googlebook system (ensuring native SurfaceFlinger & graphics stack)...")
    gbook_sys_dir = gbook_dir / "extracted_system"
    if not (gbook_sys_dir / "system/build.prop").exists():
        print("    [*] Extracting system_a.img to extracted_system...")
        gbook_sys_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(fsck_erofs), f"--extract={gbook_sys_dir}", str(gbook_dir / "system_a.img")], check=True)

    # Ensure pristine baseline copies of mutable config files in build_work/orig_system_files
    orig_sys_files = build_work / "orig_system_files"
    if not (orig_sys_files / "etc/init/hw/init.rc").exists() or not (orig_sys_files / "build.prop").exists():
        print("    [*] Extracting pristine baseline system configs from system_a.img...")
        tmp_orig = build_work / "gbook_orig_extract"
        tmp_orig.mkdir(exist_ok=True)
        subprocess.run([str(fsck_erofs), f"--extract={tmp_orig}", str(gbook_dir / "system_a.img")], capture_output=True)
        (orig_sys_files / "etc/init/hw").mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_orig / "system/build.prop", orig_sys_files / "build.prop")
        shutil.copy2(tmp_orig / "system/etc/init/hw/init.rc", orig_sys_files / "etc/init/hw/init.rc")
        shutil.copy2(tmp_orig / "system/framework/services.jar", orig_sys_files / "services.jar")
        shutil.copy2(tmp_orig / "system/framework/desktop-services.jar", orig_sys_files / "desktop-services.jar")
        shutil.rmtree(tmp_orig, ignore_errors=True)

    # Restore pristine init.rc and build.prop before applying deterministic patches
    init_rc_path = gbook_sys_dir / "system/etc/init/hw/init.rc"
    system_prop_path = gbook_sys_dir / "system/build.prop"
    shutil.copy2(orig_sys_files / "etc/init/hw/init.rc", init_rc_path)
    os.chmod(init_rc_path, 0o644)
    shutil.copy2(orig_sys_files / "build.prop", system_prop_path)
    os.chmod(system_prop_path, 0o644)

    # Ensure native Googlebook surfaceflinger and graphics libraries are preserved
    # (Using Lineage's surfaceflinger caused LayerState readParcelable BAD_TYPE parcel rejections)
    gbook_native_sf = gbook_sys_dir / "system/bin/surfaceflinger"
    gbook_native_libgui = gbook_sys_dir / "system/lib64/libgui.so"
    gbook_native_sf_prop = gbook_sys_dir / "system/lib64/libSurfaceFlingerProp.so"

    needs_restore = False
    if not gbook_native_sf.exists() or os.path.getsize(gbook_native_sf) > 9600000:
        needs_restore = True
    if not gbook_native_libgui.exists() or b"_ZTVN7android4view14SurfaceControlE" not in gbook_native_libgui.read_bytes():
        needs_restore = True
    if not gbook_native_sf_prop.exists() or os.path.getsize(gbook_native_sf_prop) > 60000:
        needs_restore = True

    if needs_restore:
        print("    [*] Restoring native Googlebook surfaceflinger and graphics libs from system_a.img...")
        tmp_extract = build_work / "gbook_tmp"
        tmp_extract.mkdir(exist_ok=True)
        subprocess.run([str(fsck_erofs), f"--extract={tmp_extract}", str(gbook_dir / "system_a.img")], check=True)
        shutil.copy2(tmp_extract / "system/bin/surfaceflinger", gbook_native_sf)
        os.chmod(gbook_native_sf, 0o755)
        shutil.copy2(tmp_extract / "system/lib64/libgui.so", gbook_native_libgui)
        os.chmod(gbook_native_libgui, 0o644)
        shutil.copy2(tmp_extract / "system/lib64/libui.so", gbook_sys_dir / "system/lib64/libui.so")
        os.chmod(gbook_sys_dir / "system/lib64/libui.so", 0o644)
        shutil.copy2(tmp_extract / "system/lib64/libSurfaceFlingerProp.so", gbook_native_sf_prop)
        os.chmod(gbook_native_sf_prop, 0o644)
        print("    [+] Native Googlebook surfaceflinger & graphics stack restored cleanly")

    # Clean up any leftover incompatible or obsolete artifacts
    for unwanted in [
        gbook_sys_dir / "system/lib64/libgfl.so",
        gbook_sys_dir / "system/lib64/libprotobuf-cpp-lite-6.33.5.so",
        gbook_sys_dir / "system/lib64/libprotobuf-cpp-full-6.33.5.so",
        gbook_sys_dir / "system/bin/boot_logger_native",
    ]:
        if unwanted.exists():
            unwanted.unlink()
            print(f"    [-] Removed obsolete artifact: {unwanted.name}")

    # 4b. Binary-patch Googlebook init for SELinux permissive mode
    init_bin = gbook_sys_dir / "system/bin/init"
    patch_init_binary(init_bin)

    # 4c. Inject early block-device symlinks into system init.rc
    if init_rc_path.exists():
        patch_system_init_rc(init_rc_path)

    # 4d. Patch services.jar (Watchdog 10m + DropBox/UserManager/HSUM User 10 unlock)
    services_jar_path = gbook_sys_dir / "system/framework/services.jar"
    patch_services_jar(services_jar_path, build_work)

    # 4d2. Patch desktop-services.jar (disable blocking waitForService in DesktopRepairModeService)
    desktop_services_jar_path = gbook_sys_dir / "system/framework/desktop-services.jar"
    if desktop_services_jar_path.exists():
        patch_desktop_services_jar(desktop_services_jar_path, build_work)

    # 4e. Disable unsupported Google Security Chip / TPM / Trusty daemons in system/etc/init
    for unsupported_rc in [
        "timberslide.rc",
        "udsattestation.rc",
        "gscd.rc",
        "gscutil.rc",
        "gscutil-executor.rc",
        "pinweaver_storaged.rc",
    ]:
        disable_init_service(gbook_sys_dir / "system/etc/init" / unsupported_rc)

    # 4e2. Suppress SetupWizard & DesktopSetupWizard installation across all user types via SystemConfig
    sys_disable_suw = gbook_sys_dir / "system/etc/sysconfig/z-disable-setupwizard.xml"
    sys_disable_suw.parent.mkdir(parents=True, exist_ok=True)
    sys_disable_suw.write_text(DISABLE_SETUPWIZARD_SYSCONFIG, encoding="utf-8")
    os.chmod(sys_disable_suw, 0o644)
    print("    [+] Injected /system/etc/sysconfig/z-disable-setupwizard.xml (zero-frame SetupWizard suppression)")

    # 4f. Optimize system build.prop (skip slow first-boot dexopt, disable USB zram writeback, SQLite WAL+OFF, SetupWizard disabled)
    if system_prop_path.exists():
        prop_content = system_prop_path.read_text(encoding="utf-8", errors="ignore")
        prop_content = prop_content.replace("pm.dexopt.first-boot=verify", "pm.dexopt.first-boot=skip")
        prop_content = prop_content.replace("pm.dexopt.boot-after-ota=verify", "pm.dexopt.boot-after-ota=skip")
        prop_content = prop_content.replace("pm.dexopt.boot-after-mainline-update=verify", "pm.dexopt.boot-after-mainline-update=skip")
        prop_content = prop_content.replace("pm.dexopt.post-boot=verify", "pm.dexopt.post-boot=skip")
        prop_content = prop_content.replace("trusty.security_vm.enabled=1", "trusty.security_vm.enabled=0")
        prop_content = prop_content.replace("mmd.zram.writeback.enabled=true", "mmd.zram.writeback.enabled=false")
        prop_content += (
            "\n# USB flash storage I/O optimization, SQLite async WAL, & SetupWizard/lockscreen bypass\n"
            "ro.hw_timeout_multiplier=10\n"
            "ro.lockscreen.disable.default=true\n"
            "persist.sys.lockscreen.disable=true\n"
            "keyguard.no_require_sim=true\n"
            "ro.setupwizard.mode=DISABLED\n"
            "debug.sqlite.journalmode=WAL\n"
            "debug.sqlite.syncmode=OFF\n"
            "debug.sqlite.wal.syncmode=OFF\n"
        )
        system_prop_path.write_text(prop_content, encoding="utf-8")
        os.chmod(system_prop_path, 0o644)
        print("    [+] Optimized system build.prop (skip dexopt, SQLite WAL+sync=OFF, SetupWizard=DISABLED)")

    # Ensure all binaries in system/bin are executable (fix logd 0550 EACCES under --all-root)
    for bin_path in (gbook_sys_dir / "system/bin").glob("*"):
        if bin_path.is_file():
            try:
                bin_path.chmod(0o755)
            except Exception:
                pass

    # 5. Unpack Trogdor vendor & dynamically assemble combined SELinux file_contexts from user-supplied images
    print("[*] Unpacking Trogdor vendor and generating combined SELinux file_contexts...")
    trogdor_vendor_dir = build_work / "trogdor_vendor"
    if trogdor_vendor_dir.exists():
        shutil.rmtree(trogdor_vendor_dir)
    trogdor_vendor_dir.mkdir(exist_ok=True)
    subprocess.run([str(fsck_erofs), f"--extract={trogdor_vendor_dir}", str(lineage_vendor_img)], check=True)

    build_combined_file_contexts(
        file_contexts,
        gbook_sys_dir,
        trogdor_vendor_dir,
        gbook_sys_ext_img,
        gbook_product_img,
        dump_erofs,
        fsck_erofs,
        build_work,
    )

    patched_system_img = build_work / "system_a_patched.img"
    print(f"[*] Packaging patched Googlebook system into {patched_system_img.name} (EROFS)...")
    subprocess.run([
        str(mkfs_erofs),
        f"--file-contexts={file_contexts}",
        "--mount-point=/",
        "--all-root",
        "-zlz4hc",
        str(patched_system_img),
        str(gbook_sys_dir)
    ], check=True)

    # Inject misc_setup
    misc_setup_path = trogdor_vendor_dir / "bin/misc_setup.sh"
    misc_setup_path.write_text(MISC_SETUP_SCRIPT)
    os.chmod(misc_setup_path, 0o755)

    # Inject init_by_name (for compatibility)
    init_by_name_path = trogdor_vendor_dir / "bin/init_by_name.sh"
    init_by_name_path.write_text(MISC_SETUP_SCRIPT)
    os.chmod(init_by_name_path, 0o755)

    # Inject googlebook-compat.rc (defines misc_setup, properties, and VM dirty writeback tuning)
    compat_rc_path = trogdor_vendor_dir / "etc/init/googlebook-compat.rc"
    compat_rc_path.write_text(GOOGLEBOOK_COMPAT_RC)
    os.chmod(compat_rc_path, 0o644)

    # Inject block device symlinks into init.trogdor.rc (without duplicating service definitions)
    init_trogdor_rc = trogdor_vendor_dir / "etc/init/init.trogdor.rc"
    if init_trogdor_rc.exists():
        rc_text = init_trogdor_rc.read_text()
        if "/dev/block/sda1" not in rc_text:
            post_fs_block = """on post-fs
    # Setup by-name block devices synchronously
    mkdir /dev/block/by-name 0755 root root
    mkdir /dev/block/bootdevice 0755 root root
    symlink /dev/block/by-name /dev/block/bootdevice/by-name

    symlink /dev/block/sda1 /dev/block/by-name/misc
    symlink /dev/block/sda2 /dev/block/by-name/boot_a
    symlink /dev/block/sda2 /dev/block/by-name/boot
    symlink /dev/block/sda2 /dev/block/by-name/KERN-A
    symlink /dev/block/sda3 /dev/block/by-name/metadata
    symlink /dev/block/sda4 /dev/block/by-name/super
    symlink /dev/block/sda5 /dev/block/by-name/userdata

    symlink /dev/block/mmcblk0p1 /dev/block/by-name/misc
    symlink /dev/block/mmcblk0p2 /dev/block/by-name/boot_a
    symlink /dev/block/mmcblk0p3 /dev/block/by-name/metadata
    symlink /dev/block/mmcblk0p4 /dev/block/by-name/super
    symlink /dev/block/mmcblk0p5 /dev/block/by-name/userdata
"""
            rc_text = rc_text.replace("on post-fs\n", post_fs_block, 1)
            init_trogdor_rc.write_text(rc_text)
            print("    [+] Injected by-name symlinks into init.trogdor.rc")

    # Create /vendor/etc/init/hw/init.trogdor.rc so Googlebook init.rc (line 10) finds it
    vendor_hw_dir = trogdor_vendor_dir / "etc/init/hw"
    vendor_hw_dir.mkdir(exist_ok=True)
    shutil.copy2(init_trogdor_rc, vendor_hw_dir / "init.trogdor.rc")
    print("    [+] Created /vendor/etc/init/hw/init.trogdor.rc for init.rc import compatibility")

    # Inject vendor sysconfig SetupWizard suppression
    vendor_disable_suw = trogdor_vendor_dir / "etc/sysconfig/z-disable-setupwizard.xml"
    vendor_disable_suw.parent.mkdir(parents=True, exist_ok=True)
    vendor_disable_suw.write_text(DISABLE_SETUPWIZARD_SYSCONFIG, encoding="utf-8")
    os.chmod(vendor_disable_suw, 0o644)

    # Configure fstab.trogdor to use USB-optimized ext4 (no online discard/TRIM, no barrier/da_alloc stalls)
    fstab_path = trogdor_vendor_dir / "etc/fstab.trogdor"
    if fstab_path.exists():
        fstab_content = fstab_path.read_text()
        lines = []
        for line in fstab_content.splitlines():
            if "/dev/block/by-name/userdata" in line and "f2fs" in line:
                continue
            if "/dev/block/by-name/userdata" in line and "ext4" in line:
                line = "/dev/block/by-name/userdata                             /data           ext4    noatime,nodiratime,nobarrier,commit=60,data=ordered,user_xattr   latemount,wait,formattable,fileencryption=aes-256-xts:aes-256-cts:v2,keydirectory=/metadata/vold/metadata_encryption,quota"
            elif "/dev/block/by-name/metadata" in line and "ext4" in line:
                line = "/dev/block/by-name/metadata                             /metadata       ext4    noatime,nodiratime,nosuid,nodev,nobarrier,commit=60              wait,formattable,first_stage_mount,check"
            lines.append(line)
        fstab_path.write_text("\n".join(lines) + "\n")
        print("    [+] Configured fstab.trogdor /data & /metadata for USB flash (removed discard/noauto_da_alloc/barrier=1)")

    # Disable casefolding & SetupWizard in vendor build.prop
    vendor_prop_path = trogdor_vendor_dir / "build.prop"
    if vendor_prop_path.exists():
        prop_content = vendor_prop_path.read_text()
        prop_content = prop_content.replace("external_storage.casefold.enabled=1", "external_storage.casefold.enabled=0")
        if "external_storage.casefold.enabled=0" not in prop_content:
            prop_content += "\nexternal_storage.casefold.enabled=0\n"
        if "persist.sys.casefold.enabled.override=0" not in prop_content:
            prop_content += "persist.sys.casefold.enabled.override=0\n"
        if "persist.traced.enable=0" not in prop_content:
            prop_content += "persist.traced.enable=0\n"
        if "persist.debug.framework_watchdog.fatal_ignore=true" not in prop_content:
            prop_content += "persist.debug.framework_watchdog.fatal_ignore=true\n"
        if "ro.setupwizard.mode=DISABLED" not in prop_content:
            prop_content += "ro.setupwizard.mode=DISABLED\n"
        vendor_prop_path.write_text(prop_content)
        print("    [+] Updated vendor build.prop (casefold=0, traced=0, SetupWizard=DISABLED)")

    # Update expand_userdata.sh to resize ext4
    expand_sh = trogdor_vendor_dir / "bin/expand_userdata.sh"
    if expand_sh.exists():
        sh_text = expand_sh.read_text()
        sh_text = sh_text.replace('if [ -x /system/bin/resize.f2fs ]; then\n    echo y | /system/bin/resize.f2fs "$USERDATA_DEV"\nelif [ -x /system/bin/resize2fs ]; then\n    /system/bin/resize2fs "$USERDATA_DEV"\nfi',
                                  'if [ -x /system/bin/resize2fs ]; then\n    /system/bin/resize2fs "$USERDATA_DEV"\nelif [ -x /system/bin/resize.f2fs ]; then\n    echo y | /system/bin/resize.f2fs "$USERDATA_DEV"\nfi')
        expand_sh.write_text(sh_text)

    patched_vendor_img = build_work / "trogdor_vendor_patched.img"
    print(f"[*] Packaging patched Trogdor vendor into {patched_vendor_img.name} (EROFS)...")
    subprocess.run([
        str(mkfs_erofs),
        f"--file-contexts={file_contexts}",
        "--mount-point=/vendor",
        "--all-root",
        "-zlz4hc",
        str(patched_vendor_img),
        str(trogdor_vendor_dir)
    ], check=True)

    # 6. Repack super partition with lpmake
    super_sparse = build_work / "super_googlebook.sparse.img"

    print(f"[*] Building dynamic super partition ({super_sparse.name}) with lpmake...")
    lpmake_cmd = [
        str(lpmake_bin),
        "--device-size", str(SUPER_MAX_SIZE),
        "--metadata-size", "65536",
        "--metadata-slots", "3",
        "--alignment", "4096",
        "--virtual-ab",
        "--group", "trogdor_dynamic_partitions_a:12433031168",
        "--group", "trogdor_dynamic_partitions_b:12433031168",
        f"--partition", f"system_a:readonly:{os.path.getsize(patched_system_img)}:trogdor_dynamic_partitions_a",
        f"--image", f"system_a={patched_system_img}",
        f"--partition", f"system_ext_a:readonly:{os.path.getsize(gbook_sys_ext_img)}:trogdor_dynamic_partitions_a",
        f"--image", f"system_ext_a={gbook_sys_ext_img}",
        f"--partition", f"product_a:readonly:{os.path.getsize(gbook_product_img)}:trogdor_dynamic_partitions_a",
        f"--image", f"product_a={gbook_product_img}",
        f"--partition", f"vendor_a:readonly:{os.path.getsize(patched_vendor_img)}:trogdor_dynamic_partitions_a",
        f"--image", f"vendor_a={patched_vendor_img}",
        f"--partition", f"vendor_dlkm_a:readonly:{os.path.getsize(lineage_vdlkm_img)}:trogdor_dynamic_partitions_a",
        f"--image", f"vendor_dlkm_a={lineage_vdlkm_img}",
        f"--partition", f"system_dlkm_a:readonly:{os.path.getsize(lineage_sdlkm_img)}:trogdor_dynamic_partitions_a",
        f"--image", f"system_dlkm_a={lineage_sdlkm_img}",
        "--sparse",
        "--output", str(super_sparse)
    ]
    subprocess.run(lpmake_cmd, check=True)

    # 7. Assemble final disk image
    print(f"[*] Unsparsing super partition into final disk image {out_img_path.name}...")
    with open(out_img_path, "r+b") as dst:
        unsparse_image(super_sparse, dst, SUPER_OFFSET)

    # Append GPT backup if needed to match 15.5GB image size
    total_expected_size = 16642998272
    cur_size = os.path.getsize(out_img_path)
    if cur_size < total_expected_size:
        print(f"[*] Expanding image to full GPT size ({total_expected_size / (1024*1024*1024):.2f} GB)...")
        with open(out_img_path, "a+b") as f:
            f.truncate(total_expected_size)
        subprocess.run(["sgdisk", "-e", str(out_img_path)], capture_output=True)

    # Ensure partition 5 (userdata) starts with clean zeroes so vold sees it as fresh/formattable
    userdata_offset = 24502272 * 512
    with open(out_img_path, "r+b") as f:
        f.seek(userdata_offset)
        f.write(b'\0' * (32 * 1024 * 1024))
    print("    [+] Zeroed first 32MB of userdata partition to ensure clean ext4 format")

    print("\n" + "="*70)
    print(f"[SUCCESS] Googlebook OS disk image generated successfully!")
    print(f"Target Image: {out_img_path} ({os.path.getsize(out_img_path)/(1024*1024*1024):.2f} GB)")
    print(f"SurfaceFlinger: Pure native Googlebook graphics engine (SDK 37)")
    print(f"Provisioning: Native UserController provisioning + sysconfig SetupWizard suppression")
    print(f"Ready to flash to USB drive using balenaEtcher or dd!")
    print("="*70 + "\n")

if __name__ == "__main__":
    main()
