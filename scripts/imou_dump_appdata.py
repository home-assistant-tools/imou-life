#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "artifacts/imou_apk/appdata"
PKG = "com.mm.android.smartlifeiot"


def run(cmd: list[str], stdout=None) -> None:
    subprocess.run(cmd, check=True, stdout=stdout)


def pull_appdata(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / "imou-appdata.tar"
    with tar_path.open("wb") as fh:
        run(
            [
                "adb",
                "exec-out",
                "run-as",
                PKG,
                "tar",
                "cf",
                "-",
                "shared_prefs",
                "databases",
                "files/mmkv",
                "no_backup",
            ],
            stdout=fh,
        )
    run(["tar", "-xf", str(tar_path), "-C", str(out_dir)])


def first_json_after(data: str, marker: str) -> dict | None:
    idx = data.find(marker)
    if idx < 0:
        return None
    start = data.find("{", idx)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for pos in range(start, len(data)):
        ch = data[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(data[start : pos + 1])
    return None


def parse_mmkv(out_dir: Path) -> dict:
    data_path = out_dir / "files/mmkv/dh_data"
    raw = data_path.read_bytes()
    text = raw.decode("utf-8", errors="ignore")
    session = None
    m = re.search(r"USER_SESSIONID.?\s*([0-9a-f]{32})", text)
    if m:
        session = m.group(1)
    username = None
    m = re.search(r"USER_NAME_HELP..(token/[A-Za-z0-9]+)", text)
    if m:
        username = m.group(1)
    user_data = first_json_after(text, "USER_DATA")
    family = first_json_after(text, "local_default_famility_info")
    return {
        "sessionId": session,
        "username": username,
        "user": user_data,
        "defaultFamily": family,
    }


def query(db: Path, sql: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def dump(out_dir: Path) -> dict:
    user_db = out_dir / "databases/13528853"
    meta = parse_mmkv(out_dir)
    cameras = query(
        user_db,
        """
        select d.deviceId,
               d.name as deviceName,
               d.deviceModel,
               d.deviceModelName,
               d.catalog,
               d.status,
               d.shareStatus,
               d.channelNum,
               c.channelId,
               c.channelName,
               c.status as channelStatus,
               c.cameraStatus
          from DHDevice d
          left join DHChannel c on c.deviceId = d.deviceId
         order by d.name, c.channelId
        """,
    )
    iot_devices = query(
        user_db,
        """
        select deviceId,
               name,
               productId,
               productModel,
               status,
               shareStatus,
               groupName,
               groupId
          from DHIot
         order by name
        """,
    )
    return {"auth": meta, "cameras": cameras, "iotDevices": iot_devices}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull", action="store_true", help="Pull fresh app data via adb run-as before dumping.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if args.pull:
        pull_appdata(args.out_dir)
    if not (args.out_dir / "files/mmkv/dh_data").exists():
        print("Missing appdata. Run with --pull after the app is logged in.", file=sys.stderr)
        return 2
    print(json.dumps(dump(args.out_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
