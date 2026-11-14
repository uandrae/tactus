#!/usr/bin/env python3
"""Registration and validation of options passed in the config file."""

import contextlib
import copy
import errno
import glob
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import fastjsonschema
import frozendict
import jsonref
import tomli
import tomlkit
import xmltodict
import yaml
from dicttoxml import dicttoxml as dtx
from fastjsonschema import JsonSchemaValueException
from json_schema_for_humans.generate import (
    GenerationConfiguration,
    generate_from_file_object,
)
from toml_formatter.formatter import FormattedToml

from . import GeneralConstants
from .aux_types import BaseMapping, QuasiConstant
from .datetime_utils import DatetimeConstants
from .formatters import duration_format_validator, duration_slice_format_validator
from .general_utils import recursive_unfreeze
from .logs import logger
from .os_utils import resolve_path_relative_to_package
from .toolbox import Platform

_ignored_scandir_errnos = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


def _entry_is_searchable_dir(entry: os.DirEntry) -> bool:
    """Return whether a `**`-style descent should enter `entry`.

    Mirrors `pathlib`'s recursive glob selector: it enters real directories
    (symlinks are resolved when checking, but never descended through) and
    treats the errnos that `pathlib` ignores as "not a directory".

    Arguments:
        entry (os.DirEntry): Entry returned by `os.scandir`

    Returns:
        bool: Whether the tree search should recurse into `entry`

    """
    try:
        return entry.is_dir() and not entry.is_symlink()
    except OSError as err:
        if err.errno in _ignored_scandir_errnos:
            return False
        raise


class ConfigParserDefaults(QuasiConstant):
    """Defaults related to the parsing of config files."""

    DATA_DIRECTORY = GeneralConstants.PACKAGE_DIRECTORY / "data"
    CONFIG_DIRECTORY = DATA_DIRECTORY / "config_files"
    PACKAGE_INCLUDE_DIR = CONFIG_DIRECTORY / "include"

    PACKAGE_CONFIG_PATH = (CONFIG_DIRECTORY / "config.toml").resolve(strict=True)
    # Define the default path to the config file
    try:
        CONFIG_PATH = Path(os.getenv("TACTUS_CONFIG_PATH", "config.toml"))
        CONFIG_PATH = CONFIG_PATH.resolve(strict=True)
    except FileNotFoundError:
        CONFIG_PATH = PACKAGE_CONFIG_PATH

    SCHEMAS_DIRECTORY = CONFIG_DIRECTORY / "config_file_schemas"
    MAIN_CONFIG_JSON_SCHEMA_PATH = SCHEMAS_DIRECTORY / "main_config_schema.json"
    MAIN_CONFIG_JSON_SCHEMA = json.loads(MAIN_CONFIG_JSON_SCHEMA_PATH.read_text())


class ConfigFileCache:
    """Cache of config files, so that a file with a given name is only read once.

    The two expensive steps of reading a config file are cached:

    * the search through `ConfigPaths.CONFIG_DATA_SEARCHPATHS` that turns a (usually
      relative) config file name into a full path, see `ConfigPaths.path_from_subpath`
      and `ConfigFileCache.path_key`, and
    * the raw contents of the file, see `_read_raw_config_file` and
      `ConfigFileCache.contents_key`. The key holds the modification time and the size
      of the file, so that a file that changed since it was cached is read again.

    All cases of a test read the same `include` files, and `tactus test -m` configures
    them all in a single process, hence both the searches and the reads of a given
    config file would otherwise be repeated once per case.

    Cached contents are deep copied, so that callers can neither modify what is
    cached nor share a dictionary with whoever reads the same file next.

    Caching is enabled by default and can be turned off with
    `ConfigFileCache.set_enabled(False)`, see the `CACHE_CONFIG_FILES` switch in
    `tactus/test_runner.py`. Entries are also dropped whenever a config file is saved,
    see `BasicConfig.save_dictionary_as`.
    """

    _no_entry = object()
    _lock = threading.Lock()
    _entries: Dict[Tuple, Any] = {}
    _loading: Dict[Tuple, threading.Event] = {}
    _enabled = True
    _hits = 0
    _misses = 0

    @classmethod
    def set_enabled(cls, enabled: bool):
        """Switch the caching of config files on/off, dropping anything cached.

        Args:
            enabled (bool): Whether config files may be cached

        """
        with cls._lock:
            if cls._enabled != bool(enabled) or not enabled:
                cls._entries.clear()
                cls._hits = 0
                cls._misses = 0
            cls._enabled = bool(enabled)

    @classmethod
    def is_enabled(cls) -> bool:
        """Return whether config files are cached."""
        return cls._enabled

    @classmethod
    def path_key(cls, subpath, last=False) -> Tuple:
        """Return the cache key of the search for `subpath`.

        The searchpaths are part of the key since they determine which match is
        found, e.g. `ConfigPaths.print` and the unit tests search in more than one
        set of directories.

        Args:
            subpath (Path | str): Name (or subpath) of the file to locate
            last (bool): Whether the last rather than the first match is used

        Returns:
            tuple: The cache key

        """
        searchpaths = tuple(str(p) for p in ConfigPaths.CONFIG_DATA_SEARCHPATHS)
        return ("path", searchpaths, bool(last), str(subpath))

    @classmethod
    def contents_key(cls, config_path: Path) -> Tuple:
        """Return the cache key of the contents of `config_path`.

        Args:
            config_path (Path): Full path to the config file

        Returns:
            tuple: The cache key, holding the file timestamp when it could be
                retrieved and the file size

        """
        with contextlib.suppress(OSError):
            stat = os.stat(config_path)
            return ("contents", str(config_path), stat.st_mtime_ns, stat.st_size)
        return ("contents", str(config_path), None, None)

    @classmethod
    def get(cls, key: Tuple, loader: Callable, *args, deepcopy_value=False, **kwargs):
        """Return the value of `key`, calling `loader` only when it is not cached.

        Threads asking for a value that another thread is loading wait for it, so
        that a config file is read (and searched for) once even when several cases
        are configured in parallel.

        Args:
            key (tuple): Cache key, see `path_key` and `contents_key`
            loader (Callable): Computes the value, called with `*args, **kwargs`
            *args: Arguments passed on to `loader`
            deepcopy_value (bool): Whether the value is copied in and out of the
                cache, for values that callers are allowed to modify
            **kwargs: Keyword arguments passed on to `loader`

        Returns:
            Any: The cached, or freshly loaded, value

        """
        while True:
            # The lock is not held while loading or copying, so that parallel
            # reads of different config files are not serialised
            if not cls._enabled:
                return loader(*args, **kwargs)
            with cls._lock:
                entry = cls._entries.get(key, cls._no_entry)
                if entry is not cls._no_entry:
                    cls._hits += 1
                    waiter = is_loader = None
                else:
                    waiter = cls._loading.get(key)
                    is_loader = waiter is None
                    if is_loader:
                        cls._misses += 1
                        waiter = cls._loading[key] = threading.Event()
            if entry is not cls._no_entry:
                return copy.deepcopy(entry) if deepcopy_value else entry
            if is_loader:
                return cls._load(key, waiter, loader, args, kwargs, deepcopy_value)
            waiter.wait()

    @classmethod
    def _load(cls, key, waiter, loader, args, kwargs, deepcopy_value):
        """Cache the value of `key` that `loader` reads, and wake up the waiters.

        Args:
            key (tuple): Cache key
            waiter (threading.Event): Signals the threads waiting for `key`
            loader (Callable): Computes the value, called with `args, kwargs`
            args: Positional arguments passed on to `loader`
            kwargs: Keyword arguments passed on to `loader`
            deepcopy_value (bool): Whether the value is copied into the cache

        Returns:
            Any: The loaded value

        """
        try:
            value = loader(*args, **kwargs)
        finally:
            with cls._lock:
                del cls._loading[key]
                waiter.set()
        with cls._lock:
            if cls._enabled:
                # Another thread may have cached an equivalent value in the meantime
                cls._entries.setdefault(
                    key, copy.deepcopy(value) if deepcopy_value else value
                )
        return value

    @classmethod
    def discard_file(cls, config_file: Union[Path, str]):
        """Drop everything cached about `config_file`, e.g. because it is written.

        Args:
            config_file (Path | str): Path to the config file, as used by the caller

        """
        path = Path(config_file).expanduser()
        with contextlib.suppress(OSError, ValueError):
            path = resolve_path_relative_to_package(path, ignore_errors=True)
        written = str(path.resolve())
        with cls._lock:
            stale = [
                key
                for key in cls._entries
                # Contents are keyed by full path, paths by the requested name
                if (key[0] == "contents" and key[1] == written)
                or (key[0] == "path" and written.endswith(key[3].lstrip("/")))
            ]
            for key in stale:
                del cls._entries[key]

    @classmethod
    def clear(cls):
        """Drop everything cached."""
        with cls._lock:
            cls._entries.clear()
            cls._hits = 0
            cls._misses = 0

    @classmethod
    def stats(cls) -> dict:
        """Return a summary of the cache contents and its hit rate.

        Returns:
            dict: enabled flag, number of cached entries and the hit/miss counts

        """
        with cls._lock:
            return {
                "enabled": cls._enabled,
                "entries": len(cls._entries),
                "hits": cls._hits,
                "misses": cls._misses,
            }


class ConfigPaths:
    """Support multiple path search."""

    _env_data_paths = os.getenv("TACTUS_CONFIG_DATA_DIR")
    CONFIG_DATA_SEARCHPATHS = (
        _env_data_paths.split(":") if _env_data_paths is not None else []
    )
    erroneous_paths = [
        path for path in CONFIG_DATA_SEARCHPATHS if not os.path.isabs(path)
    ]
    if len(erroneous_paths) > 0:
        raise RuntimeError(f"TACTUS_CONFIG_DATA_DIR is not absolute: {erroneous_paths}")
    CONFIG_DATA_SEARCHPATHS.append(ConfigParserDefaults.DATA_DIRECTORY)

    _env_schemas_paths = os.getenv("TACTUS_SCHEMAS_DIR")
    SCHEMAS_SEARCHPATHS = (
        _env_schemas_paths.split(":") if _env_schemas_paths is not None else []
    )
    erroneous_schema_paths = [
        path for path in SCHEMAS_SEARCHPATHS if not os.path.isabs(path)
    ]
    if len(erroneous_schema_paths) > 0:
        raise RuntimeError(
            f"TACTUS_SCHEMAS_DIR is not absolute: {erroneous_schema_paths}"
        )
    SCHEMAS_SEARCHPATHS.append(ConfigParserDefaults.SCHEMAS_DIRECTORY)

    @staticmethod
    def print(config_file=None, host=None):
        """Prints the available config directories.

        Displays the main config search paths as defined by list_paths
        in addition to the actual search paths in the config file used.

        """

        def path_info(list_paths, dirmap=tuple({})):
            """Populates the a list of search paths with found directories.

            Args:
                list_paths (list): directories to search for
                dirmap (dict): Mapping between display name and actual path

            Returns:
                mapping (dict): Dict of search result

            Raises:
                RuntimeError: In case of multiple conflicting paths detected
            """
            mapping = {}
            for dir_ in list_paths:
                rdir = dirmap.get(dir_, dir_)
                mapping[dir_] = []
                pattern = f"**/{rdir}"

                for searchpath in ConfigPaths.CONFIG_DATA_SEARCHPATHS:
                    res = list(Path(searchpath).rglob(pattern))
                    if len(res) == 1:
                        mapping[dir_].append(str(res[0]))
                    if len(res) > 1:
                        logger.error("Multiple matches found for subpath: {}", searchpath)
                        logger.error("Results: {}", res)
                        raise RuntimeError

            return mapping

        dirmap = {
            "config_file_schemas": "config_files/config_file_schemas",
        }
        list_paths = [
            "config_files",
            "config_file_schemas",
            "namelist_generation_input",
            "input",
        ]
        list_config_paths = []
        raw_config = BasicConfig.from_file(config_file)
        for _key, _value in raw_config.get("include", {}).items():
            key = f"config_file_{_key}_section"
            value = _value.replace("@HOST@", host) if host is not None else _value
            dirmap[key] = value
            if key not in list_paths:
                list_config_paths.append(key)

        path_info_main = path_info(list_paths, dirmap)
        path_info_config = path_info(list_config_paths, dirmap)

        logger.info("tactus paths for host={}", host)
        logger.info(" Package directory: {}", GeneralConstants.PACKAGE_DIRECTORY)
        logger.info(
            " Searchpaths: {}", [str(x) for x in ConfigPaths.CONFIG_DATA_SEARCHPATHS]
        )
        logger.info(
            " Data paths in search order: {}", json.dumps(path_info_main, indent=4)
        )
        logger.info(
            " Config file include paths in search order: {}",
            json.dumps(path_info_config, indent=4),
        )

    @staticmethod
    def path_from_subpath(subpath, last=False) -> Path:
        """Interface to find full path given any subpath, by searching 'searchpaths'.

        The subpaths are cached (see `ConfigFileCache`) so that the data directories
        are searched only once per subpath.

        Arguments:
            subpath (str): Subpath to search for
            last(boolan): Return last match

        Returns:
            (Path): Full path to target

        Raises:
            RuntimeRerror: Various errors
        """
        return ConfigFileCache.get(
            ConfigFileCache.path_key(subpath, last),
            ConfigPaths._search_path_from_subpath,
            subpath,
            last=last,
        )

    @staticmethod
    def _iter_subpath_matches(searchpath, subpath: str):
        """Yield matches of a plain `subpath` under `searchpath`, like rglob.

        Equivalent to `Path(searchpath).rglob(f"**/{subpath}")` for a `subpath`
        without glob magic, but walks the tree with a single `scandir` per
        directory and tests each candidate with a single `os.stat`. This avoids
        the repeated scans, selector chains and `Path` allocations of pathlib's
        recursive glob, which costs several `scandir` system calls per directory
        and per pattern component. Matches are yielded lazily, in the same
        depth-first order as `Path.rglob`, so a caller looking for the first
        match can stop after the second one.

        Arguments:
            searchpath (Path | str): Root directory to search
            subpath (str): Relative path to search for, without glob magic

        Yields:
            (Path): Full paths matching `subpath` at any depth under `searchpath`

        """
        if not os.path.isdir(searchpath):
            return

        def descend(directory):
            candidate = os.path.join(directory, subpath)
            try:
                os.stat(candidate)
            except OSError:
                pass
            else:
                yield Path(candidate)
            try:
                with os.scandir(directory) as scan:
                    entries = list(scan)
            except PermissionError:
                return
            try:
                for entry in entries:
                    if _entry_is_searchable_dir(entry):
                        yield from descend(entry.path)
            except PermissionError:
                return

        yield from descend(os.fspath(searchpath))

    @staticmethod
    def _search_path_from_subpath(subpath, last=False) -> Path:
        """Search the config data paths for the first/last match of `subpath`.

        Arguments:
            subpath (str): Subpath to search for
            last (boolean): Return last match

        Returns:
            (Path): Full path to target

        Raises:
            RuntimeError: If `subpath` could not be found
        """
        subpath = str(subpath)
        searchpaths = ConfigPaths.CONFIG_DATA_SEARCHPATHS.copy()
        for searchpath in searchpaths:
            if glob.has_magic(subpath):
                matches = Path(searchpath).rglob(f"**/{subpath}")
            else:
                matches = ConfigPaths._iter_subpath_matches(searchpath, subpath)
            if last:
                results = list(matches)
                if len(results) == 0:
                    continue
                return results[-1]
            first = next(matches, None)
            if first is None:
                continue
            if next(matches, None) is not None:
                logger.warning("Multiple matches found for subpath: {}", subpath)
                logger.warning("Selecting the first result: {}", first)
            return first

        raise RuntimeError(f"Could not find {subpath}")


class ConfigFileValidationError(Exception):
    """Error to be raised when parsing the input config file fails."""


class ConflictingValidationSchemasError(Exception):
    """Error to be raised when more than one schema is defined for a config section."""


class BasicConfig(BaseMapping):
    """Base class for configs. Arbitrary entries allowed: no validation is performed."""

    def __init__(self, *args, _metadata=None, **kwargs):
        """Initialise an instance in a `dict`-like fashion."""
        super().__init__(*args, **kwargs)
        self.metadata = _metadata

    @classmethod
    def from_file(cls, path, **kwargs):
        """Retrieve configs from a file in miscellaneous formats.

        Args:
            path (typing.Union[pathlib.Path, str]): Path to the config file.
            **kwargs: Arguments passed to the class constructor.

        Returns:
            cls: Configs retrieved from the specified path.
        """
        path = Path(path).resolve()

        configs = _read_raw_config_file(path)
        return cls(configs, _metadata={"source_file_path": path}, **kwargs)

    def save_as(self, config_file):
        """Save config file.

        Args:
            config_file (str): Path to config file

        Raises:
            TypeError: when unknown filetype as config_file is given.
        """
        BasicConfig.save_dictionary_as(self.dict(), config_file)

    @staticmethod
    def save_dictionary_as(dictionary, config_file):
        """Save config file.

        Args:
            dictionary: dictionary to save
            config_file (str): Path to config file

        Raises:
            TypeError: when unknown filetype as config_file is given.
        """
        # Make sure the new contents are not served from a stale cache
        ConfigFileCache.discard_file(config_file)

        suffix = Path(config_file).suffix

        if suffix == ".toml":
            with open(config_file, mode="w", encoding="utf8") as fh:
                tomlkit.dump(dictionary, fh)
            formatted_toml = FormattedToml.from_file(path=config_file)
            with open(config_file, mode="w", encoding="utf8") as f:
                f.write(str(formatted_toml))
        elif suffix == ".xml":
            with open(config_file, mode="wb") as fh:
                fh.write(dtx(dictionary, attr_type=False))
        elif suffix in [".yml", ".yaml"]:
            with open(config_file, mode="wb") as fh:
                yaml.dump(dictionary, fh, encoding="utf-8", default_flow_style=False)
        elif suffix == ".json":
            json_object = json.dumps(dictionary, indent=4)
            with open(config_file, "w", encoding="utf-8") as fh:
                fh.write(json_object)
        else:
            raise TypeError(f"Unknown filetype: {config_file}")

    @BaseMapping.data.setter
    def data(self, new):
        """Set the underlying data stored by the instance."""

        def needs_cleaning(obj):
            if obj is None or isinstance(obj, list):
                return True
            if isinstance(obj, dict):
                for v in obj.values():
                    if needs_cleaning(v):
                        return True
            return False

        def remove_none(obj):
            return {
                k: remove_none(v) if isinstance(v, dict) else v
                for k, v in obj.items()
                if v is not None
            }

        def lists_to_tuples(obj):
            return {
                k: (
                    lists_to_tuples(v)
                    if isinstance(v, dict)
                    else (tuple(v) if isinstance(v, list) else v)
                )
                for k, v in obj.items()
            }

        if needs_cleaning(new):
            new = remove_none(new)
            new = lists_to_tuples(new)

        BaseMapping.data.fset(self, new)

    @property
    def metadata(self):
        """Get the metadata associated with the instance."""
        return getattr(self, "_metadata", {})

    @metadata.setter
    def metadata(self, new):
        """Set the metadata associated with the instance."""
        if new is not None:
            self._metadata = recursive_unfreeze(new)

    def get(self, item, default=None):
        """Get dictionary stored at key as a BasicConfig.

        Args:
            item (str): Key to retrieve (can be nested, e.g. "general.times")
            default: Value to return if key is not found

        Returns:
            BasicConfig: data stored at key as a BasicConfig, or default if key not found
        """
        try:
            result = self[item]
        except KeyError:
            return default

        if type(result) is frozendict.frozendict:
            return BasicConfig(result)
        return result

    def get_as_dict(self, item, default=None):
        """Get dictionary stored at key as a dict.

        Args:
            item (str): Key to retrieve (can be nested, e.g. "general.times")
            default: Value to return if key is not found

        Returns:
            dict: data stored at key as a dict, or default if key ot found
        """
        try:
            result = self[item]
        except KeyError:
            return default

        return recursive_unfreeze(result)


class JsonSchema(BaseMapping):
    """Class to use for JSON schemas. Provides a `validate` method to validate data."""

    def __init__(self, *args, **kwargs):
        """Initialise instance."""
        super().__init__(*args, **kwargs)
        new_data = jsonref.replace_refs(self.data)

        # deepcopy to get rid of the jsonref.JsonRef when storing to data
        new_data = copy.deepcopy(new_data)
        BaseMapping.data.fset(self, new_data)

    @property
    def _validation_function(self):
        return _get_json_validation_function(self)

    def validate(self, data):
        """Return a copy of `data` validated against the stored JSON schema."""
        return self._validation_function(data)

    def get_markdown_doc(self):
        """Return human-readable doc for the schema in markdown format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(Path(tmpdir) / "schema.json", "w") as schema_file:
                data_copy = self.dict()
                schema_file.write(json.dumps(data_copy))

            with (
                open(Path(tmpdir) / "schema_doc.md", "w") as doc_file,
                contextlib.redirect_stdout(None),
            ):
                generate_from_file_object(
                    schema_file=schema_file,
                    result_file=doc_file,
                    config=GenerationConfiguration(
                        template_name="md",
                        show_toc=False,
                        template_md_options={
                            "show_heading_numbers": False,
                            "properties_table_columns": [
                                "Property",
                                "Pattern",
                                "Type",
                                "Definition",
                                "Title/Description",
                            ],
                            "badge_as_image": True,
                        },
                        with_footer=False,
                    ),
                )

            with open(Path(tmpdir) / "schema_doc.md", "r") as doc_file:
                schema_doc = doc_file.read()

        return schema_doc


class ParsedConfig(BasicConfig):
    """Object that holds parsed configs validated against a `json_schema`."""

    def __new__(cls, *_, **__):
        """Override `__new__` to prevent mocked calls from being used.

        Not having this, cases the unittests running after
        tests/unit/test_experiment.py::TestCaseSetup to fail as reported in
        https://github.com/pytest-dev/pytest/discussions/8009
        """
        return super(ParsedConfig, cls).__new__(cls)

    def __init__(
        self,
        *args,
        json_schema,
        include_dir=ConfigParserDefaults.CONFIG_DIRECTORY,
        host=None,
        **kwargs,
    ):
        """Initialise an instance with an arbitrary number of entries & validate them."""
        self.include_dir = include_dir
        self.json_schema = json_schema
        self.host = host
        super().__init__(*args, **kwargs)

    @BasicConfig.data.setter
    def data(self, new):
        """Set the underlying data stored by the instance.

        Skip the validation if the class is instantiated with an empty schema.

        """
        validate_json_schema = len(self.json_schema) > 0

        new, json_schema = _expand_config_include_section(
            raw_config=new,
            json_schema=self.json_schema,
            config_include_search_dir=self.include_dir,
            host=self.host,
        )
        ParsedConfig.json_schema.fset(self, json_schema, _validate_data=False)

        # Make sure all sections defined in the schema are also present in the new config
        sections_that_should_not_be_defaulted = [
            "include",
            *new,
            *json_schema.get("required", []),
        ]
        for property_name, property_schema in json_schema.get("properties", {}).items():
            if property_name in sections_that_should_not_be_defaulted:
                continue
            if property_schema.get("type", "") == "object":
                new[property_name] = {}

        if validate_json_schema:
            new = self.json_schema.validate(new)
        BasicConfig.data.fset(self, new)

    @property
    def include_dir(self):
        """Return the search dir used sections in the raw config's `include` section."""
        return self._include_dir

    @include_dir.setter
    def include_dir(self, new):
        """Set the search dir for `include` config sections."""
        self._include_dir = Path(new)

    @property
    def json_schema(self):
        """Return the instance's JSON schema."""
        return self._json_schema

    @json_schema.setter
    def json_schema(self, new, _validate_data=True):
        self._json_schema = JsonSchema(new)
        if _validate_data and self.data is not None:
            self.data = self.data

    @classmethod
    def from_file(cls, path, include_dir=None, **kwargs):
        """Do as in `BasicConfig`. If `None`, `include_dir` will become `path.parent`."""
        if include_dir is None:
            include_dir = Path(path).parent
        return super().from_file(path=path, include_dir=include_dir, **kwargs)

    def __repr__(self):
        rtn = super().__repr__().strip(")")
        rtn += f", json_schema={self.json_schema.dumps(style='json')})"
        return rtn

    def expand_macros(self, expand_all=False, protect_time=False):
        """Expand macros in config recursively.

        Args:
            expand_all (boolean): Flag to expand all macros
            protect_time (boolean): Flag to control expansion of time variables

        Returns:
            config (ParsedConfig): Parsed configuration
        """
        protect_keys = ["basetime", "validtime"]
        config = self.dict()
        if protect_time:
            time_keys = {
                key: config["general"]["times"].pop(key)
                for key in protect_keys
                if key in config["general"]["times"]
            }

        macros = config["macros"]
        if "case" in macros and not expand_all:
            macros["select"] = {"case": self["macros.case"]}
        config["macros"] = macros

        macro_platform = Platform(BasicConfig(config))
        config = macro_platform.resolve_macros(self.dict())
        config = self.copy(update=config)
        if protect_time:
            updates = {
                key: value for key, value in time_keys.items() if value is not None
            }
            config = config.copy(update=updates)

        return config


def _read_raw_config_file(config_path: Path):
    """Read raw configs from files in miscellaneous formats.

    Files are cached (see `ConfigFileCache`) so that a config file with a given
    name is only read once, unless it has changed since it was read.

    Args:
        config_path (Path): Path to the config file.

    Returns:
        dict: Configs read from the specified path.

    Raises:
        NotImplementedError: If the config file format is not supported.
    """
    config_path = resolve_path_relative_to_package(config_path)
    return ConfigFileCache.get(
        ConfigFileCache.contents_key(config_path),
        _read_raw_config_file_from_disk,
        config_path,
        deepcopy_value=True,
    )


def _read_raw_config_file_from_disk(config_path: Path):
    """Read raw configs from files in miscellaneous formats.

    Args:
        config_path (Path): Path to the config file.

    Returns:
        dict: Configs read from the specified path.

    Raises:
        NotImplementedError: If the config file format is not supported.
    """
    logger.debug("Reading configs from file <{}>", config_path)
    print(config_path)

    with open(config_path, "rb") as config_file:
        if config_path.suffix == ".toml":
            return tomli.load(config_file)

        if config_path.suffix in [".yaml", ".yml"]:
            return yaml.safe_load(config_file)

        if config_path.suffix == ".json":
            return json.load(config_file)

        if config_path.suffix == ".xml":
            return xmltodict.parse(config_file.read())

    raise NotImplementedError(f'Unsupported config file format "{config_path.suffix}"')


def _get_config_include_definitions(raw_config):
    config_includes = raw_config.get("include", {}).copy()
    overlapping_sections = [key for key in config_includes if key in raw_config]
    if overlapping_sections:
        msg = f"`[include]` section(s) [{', '.join(overlapping_sections)}] "
        msg += "already present in parent config."
        raise ValueError(msg)
    return config_includes


def _get_all_json_schemas(json_schema, schemas_paths):
    """Load and add all json schema files from a list of schema directories.

    Directories are searched in order; the first directory that defines a schema for a
    given section name wins (higher-priority paths should be placed at the front of the
    list).

    Args:
        json_schema (dict): Input json schema
        schemas_paths (list): Ordered list of paths to search for json schema files

    Returns:
        json_schema (dict): Updated json dict

    """
    exclude = ["main_config_schema.json", "default_config_schema.json"]
    # Revert schema_paths list to make first path win, since it then occurs last
    # in below iteration.
    schemas_paths = schemas_paths[::-1]

    for schemas_path in schemas_paths:
        for filename in glob.glob(f"{schemas_path}/*.json"):
            if os.path.basename(filename) in exclude:
                continue
            section_name = os.path.basename(filename).replace("_section_schema.json", "")
            updated_schema = {"$ref": f"file:{filename}"}
            json_schema["properties"].update({section_name: updated_schema})

    return json_schema


def _expand_config_include_section(
    raw_config: dict,
    json_schema: JsonSchema,
    config_include_search_dir=ConfigParserDefaults.CONFIG_DIRECTORY,
    schemas_path: Optional[List[Path] | Path] = None,
    _parent_sections: Tuple = (),
    host: Optional[str] = None,
) -> Tuple[dict, JsonSchema]:
    """Merge config includes and return new config & corresponding validation schema.

    Args:
        raw_config (dict): The raw configuration dictionary to process, potentially
            containing include directives.
        json_schema (JsonSchema): The JSON schema associated with ``raw_config``.
        config_include_search_dir: Directory in which to search for included config
            files. Defaults to ``ConfigParserDefaults.CONFIG_DIRECTORY``.
        schemas_path (Path | list[Path] | None): A single Path, a list of Paths, or
            None.  When None, defaults to ``ConfigPaths.SCHEMAS_SEARCHPATHS``
            (evaluated at call time so that callers can insert extra directories into
            that list before invoking this function). When a list is supplied,
            directories are searched in order and the first directory that contains a
            matching schema file wins.
        _parent_sections (tuple): Tuple of ancestor section names used internally to
            track nesting during recursive expansion. Should not be set by callers.
        host (str | None): Optional host identifier passed down during recursive
            expansion. Defaults to None.

    Returns:
        tuple[dict, JsonSchema]: A 2-tuple of ``(merged_config, merged_schema)`` where
            ``merged_config`` is the fully expanded configuration dictionary and
            ``merged_schema`` is the corresponding merged JSON schema.

    Raises:
        RunTimeError: If include path requires a host to be set, and no host
            input argument is provided.
        ConflictingValidationSchemasError: If a json schema for an include section
            is found the parent json schema. Such schema must be added to a
            separate file.
    """
    if schemas_path is None:
        schemas_path = ConfigPaths.SCHEMAS_SEARCHPATHS
    if not isinstance(schemas_path, list):
        # Accept a single Path/str for backward compatibility
        schemas_path = [schemas_path]

    # If the json schema is empty on arrival, keep it empty
    raw_config = recursive_unfreeze(raw_config)
    json_schema = recursive_unfreeze(json_schema)

    config_include_defs = _get_config_include_definitions(raw_config)

    if "properties" not in json_schema:
        json_schema["properties"] = {}
    config_include_search_dir = Path(config_include_search_dir).resolve()
    config_include_sections = {}
    if len(config_include_defs) == 0:
        json_schema = _get_all_json_schemas(json_schema, schemas_path)
    else:
        for section_name, include_path_ in config_include_defs.items():
            if isinstance(include_path_, str):
                if "@HOST@" in include_path_ and host is None:
                    raise RuntimeError(
                        f"include_path={include_path_} requires host to be set"
                    )
                include_path = (
                    include_path_.replace("@HOST@", host)
                    if host is not None
                    else include_path_
                )
                include_path = Path(include_path)
                if not include_path.is_absolute():
                    include_path = ConfigPaths.path_from_subpath(include_path)
                logger.info("Include: {}", include_path)
                included_config_section = _read_raw_config_file(include_path)
            else:
                included_config_section = include_path_
            _sections_traversed = (*_parent_sections, section_name)
            sections_traversed_str = " -> ".join(_sections_traversed)
            if "include" in raw_config and section_name in json_schema["properties"]:
                msg = "Validation schema for `[include]` section "
                msg += f' "{sections_traversed_str}" '
                msg += "also detected in its parent section's schema. "
                msg += "`[include]` schemas must NOT be added to their parent's schemas,"
                msg += "but rather in their own separate files."
                raise ConflictingValidationSchemasError(msg)

            schema_file = None
            for spath in schemas_path:
                candidate = Path(spath) / f"{section_name}_section_schema.json"
                if candidate.is_file():
                    schema_file = candidate
                    break
            if schema_file is None:
                logger.warning(
                    'No validation schema for config section "{}". Using default.',
                    sections_traversed_str,
                )
                # default_section_schema.json lives in the tactus schemas dir
                # (assumed to be last in list)
                schema_file = Path(schemas_path[-1]) / "default_section_schema.json"

            updated_config, updated_schema = _expand_config_include_section(
                raw_config=included_config_section,
                json_schema={"$ref": f"file:{schema_file}"},
                config_include_search_dir=config_include_search_dir,
                schemas_path=schemas_path,
                _parent_sections=_sections_traversed,
            )
            config_include_sections.update(updated_config)
            json_schema["properties"].update({section_name: updated_schema})

    raw_config.update(config_include_sections)
    if "include" in raw_config:
        raw_config.pop("include")

    return raw_config, json_schema


def _get_json_validation_function(json_schema):
    """Return a validation function compiled with schema `json_schema`."""
    if not json_schema:
        # Validation will just convert everything to dict in this case
        return lambda obj: recursive_unfreeze(obj)
    validation_func = fastjsonschema.compile(
        json_schema.dict(),
        formats={
            "duration": duration_format_validator,
            "duration_slice": duration_slice_format_validator,
        },
    )

    def validate(obj):
        try:
            return validation_func(recursive_unfreeze(obj))
        except JsonSchemaValueException as err:
            error_path = " -> ".join(err.path[1:])
            human_readable_msg = err.message.replace(err.name, "").strip()

            # Give a better err msg when times/date-times/durations don't follow ISO 8601
            human_readable_msg = human_readable_msg.replace(
                f"must match pattern {DatetimeConstants.ISO_8601_TIME_DURATION_REGEX}",
                "must be an ISO 8601 duration string",
            )
            for spec in ["date-time", "date", "time"]:
                human_readable_msg = human_readable_msg.replace(
                    f"must be {spec}", f"must be an ISO 8601 {spec} string"
                )

            raise ConfigFileValidationError(
                f'"{error_path}" {human_readable_msg}. '
                + f'Received type "{type(err.value).__name__}" with value "{err.value}".'
            ) from err

    return validate


def evaluate_dynamic_dates(config_data):
    """Replace dynamic dates like 'yesterday' with actual timestamps."""
    times_section = config_data.get("general", {}).get("times", {})

    if times_section.get("start") == "yesterday":
        yesterday = datetime.utcnow() - timedelta(days=1)
        start_time = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0)
        times_section["start"] = start_time.isoformat() + "Z"

    return config_data
