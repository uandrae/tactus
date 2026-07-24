import os
from pathlib import Path
import json
from tactus.config_parser import ParsedConfig
from tactus.host_actions import set_tactus_home
import shutil

TARGET_DIR = Path("/perm/snh/git/tactus_io_track/task_data")

NON_COPY_CONFIG = ["platform"]

input_files = [
"/scratch/snh/tactus/io_tracker_AROME_20260720T00/20260720_0000/mbr000/InterpolSstSic_storage.json",
"/scratch/snh/tactus/io_tracker_AROME_20260720T00/20260720_0000/mbr000/Forecast_storage.json"
]

for input_file in input_files:
 with open(input_file, "r", encoding="utf-8") as f:
    print("read", input_file)
    input_data = json.load(f)

 taskname = os.path.basename(input_file).replace("_storage.json", "")


 config = ParsedConfig.from_file(input_data["config"], json_schema={})
 config = config.copy(update={"platform": {"tactus_home": set_tactus_home(config)}})
 config = config.expand_macros(True,protect_time=False)

 target_dir = TARGET_DIR / config["general.case"] / taskname
 os.makedirs(target_dir, exist_ok=True)

 cd = {}

 for source, val in input_data["input"].items():
    splits = source.split(".")
    key = splits.pop()
    header = ".".join(splits)
    if header in NON_COPY_CONFIG:
        value = config[source]
        for files in val.values():
            for f in files:
                source, _ = f.popitem()
                print(" use", source)
    else:
        value = str(target_dir)

        for files in val.values():
            for f in files:
                source, target = f.popitem()
                target = str(target_dir / os.path.basename(source))
                if not os.path.isfile(target):
                    print("  cp", source, target)
                    shutil.copy(source, target_dir / target)
                else:
                    print("exists", target)
    if header not in cd:
        cd[header] = {key: value}
    else:
        cd[header][key] = value

 config_update = ParsedConfig(cd, json_schema={})
 config_update.save_as(target_dir / "config_update.toml")
