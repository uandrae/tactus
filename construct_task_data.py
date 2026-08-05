import os
import sys
from pathlib import Path
import json
from tactus.config_parser import ParsedConfig
from tactus.host_actions import set_tactus_home
from tactus.os_utils import Search
import shutil

TARGET_DIR = Path("/scratch/snh//tactus_io_track/task_data")

NON_COPY_CONFIG = ["platform"]


test_tasks = ["Forecast", "C903", "InterpolSstSic"]


search_path = "/scratch/snh/tactus/io_tracker_AROME_20260720T00/20260720_0000"


print("Search in:", search_path)

input_files = Search.find_files(
    search_path, pattern=f"({'|'.join(test_tasks)})(.*)_storage.json", recursive=True
)

print(input_files)

for input_file in input_files:
    with open(input_file, "r", encoding="utf-8") as f:
        print("\nRead", input_file)
        input_data = json.load(f)

    taskname = os.path.basename(input_file).replace("_storage.json", "")

    # Read and expand the used config file
    config = ParsedConfig.from_file(input_data["config"], json_schema={})
    config_unresolved = config.copy(
        update={"platform": {"tactus_home": set_tactus_home(config)}}
    )
    config = config_unresolved.expand_macros(True, protect_time=False)

    target_dir = TARGET_DIR / config["general.case"] / taskname
    target_config_dir = f"@REFERENCES_FOLDER@/{config['general.case']}/{taskname}"
    os.makedirs(target_dir, exist_ok=True)

    cd = {"general": {"task": taskname},
          "system": {"case_suffix": f"_{taskname}"}}

    for source, val in input_data["input"].items():
        splits = source.split(".")
        key = splits.pop()
        header = ".".join(splits)
        if header in NON_COPY_CONFIG:
            value = config_unresolved[source]
            for files in val.values():
                for f in files:
                    source, _ = f.popitem()
                    print(" use", source)
        else:
            value = target_config_dir

            for files in val.values():
                for f in files:
                    source, target = f.popitem()
                    target = str(target_dir / os.path.basename(source))
                    if not os.path.isfile(target):
                        print("  cp", source, target)
                        shutil.copy(source, target_dir / target)
                    else:
                        print(" exists", target)
        if header not in cd:
            cd[header] = {key: value}
        else:
            cd[header][key] = value

    config_update = ParsedConfig(cd, json_schema={})
    config_update.save_as(target_dir / "config_update.toml")
