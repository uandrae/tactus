"""Compilation tasks.

The IAL compilation can run in two ways:

* As three separate tasks, ``IALClone`` -> ``TactusBundleCreate`` ->
  ``TactusBundleBuild`` (one build task per precision). This is the default.
* As one task, ``TactusCompile``, that runs all three steps in one go. It is
  selected by setting ``compile.single_task = true``.

Both forms use the same step implementations (the ``_*Step`` mixins below), so
they run the same commands. Each step is split into a ``setup_*`` method, which
only reads the config, and a ``run_*`` method, which does the work. The build
setup inspects the bundle tree made by the create step, so in the single task
it is set up only after the create step has run.
"""

import os
import shutil
import sys
from pathlib import Path

from ..logs import logger
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob

DEFAULT_PRECISION = "R64"


def _ecbundle_bin():
    """Return the ecbundle executable belonging to the running python."""
    return f"{os.path.dirname(sys.executable)}/ecbundle"


def _replace_symlink(target, link):
    """Point the symlink link at target, replacing an existing symlink.

    A broken symlink is replaced as well. A real file or directory at link is
    left alone, and os.symlink then raises FileExistsError.

    Args:
        target (str|Path): What the link should point to
        link (str|Path): The symlink to create
    """
    if os.path.islink(link):
        logger.debug("Removing old link {}", link)
        os.unlink(link)
    os.symlink(target, link)


def build_precisions(config):
    """Return the precisions to build.

    R64 is always built. R32 is added when the forecast runs in single
    precision, matching the precision families of the compilation suite.

    Args:
        config (ParsedConfig): Configuration

    Returns:
        (list): Precisions, e.g. ["R64", "R32"]
    """
    precisions = [DEFAULT_PRECISION]
    if config["submission.precision"] == "R32":
        precisions.append("R32")
    return precisions


class _CloneStep:
    """Clone IAL and check out the requested version."""

    def setup_clone(self):
        """Read the clone settings from the config."""
        self.git_ial_repo = self.config["compile.ial_git_repo"]
        self.git_ial_version = self.config["compile.ial_git_version"]
        self.git_token = self.config["compile.git_token"]
        self.ial_dir = self.platform.substitute(self.config["compile.ial_dir"])

    def run_clone(self):
        """Clone IAL unless it is already there, then check out the version."""
        batch_job = BatchJob(os.environ)
        if os.path.exists(self.ial_dir):
            logger.info("IAL dir {} already exists", self.ial_dir)
        else:
            cmd = f"git clone {self.git_ial_repo} {self.ial_dir}"
            batch_job.run(cmd.replace("[TOKEN]", self.git_token))
        batch_job.run(f"cd {self.ial_dir}; git checkout {self.git_ial_version}")


class _BundleCreateStep:
    """Create, or update, the ecbundle source bundle."""

    def setup_create(self):
        """Read the bundle create settings from the config."""
        self.compile_dir = self.platform.substitute(self.config["compile.dir"])
        self.arch_dir = self.platform.substitute(self.config["compile.arch_dir"])
        self.ial_dir = self.platform.substitute(self.config["compile.ial_dir"])
        self.ecbundle_bin = _ecbundle_bin()

        git_token = self.config["compile.git_token"]
        self.git_token_str = f"--github-token {git_token}" if git_token else ""

        self.orig_bundle_file = self.platform.substitute(
            self.config["compile.bundle_file"]
        )
        self.bundle_update = self.config["compile.bundle_update"]
        if self.bundle_update:
            self.bundle_file = self.platform.substitute("@CASEDIR@/bundle-local-ial.yaml")
            self.update_bundle_file = self.platform.substitute(
                self.config["compile.update_bundle_file"]
            )
        else:
            self.bundle_file = self.orig_bundle_file
        self.bundle_file_str = f"--bundle {self.bundle_file}"

    def run_create(self):
        """Merge the bundle files if requested, then run ecbundle create."""
        tactusmakedirs(self.compile_dir)

        # Assume git ssh access unless a token is set
        if not self.git_token_str:
            os.environ["GITHUB"] = "git@github.com:"
        os.environ["IAL_DIR"] = self.ial_dir

        batch_job = BatchJob(os.environ)
        if self.bundle_update:
            batch_job.run(
                f"cd {self.compile_dir}; {self.ecbundle_bin} merge "
                + f"{self.orig_bundle_file} {self.update_bundle_file} "
                + f"-o {self.bundle_file}"
            )

        batch_job.run(
            f"cd {self.compile_dir}; {self.ecbundle_bin} create "
            + f"{self.git_token_str} {self.bundle_file_str} --update "
            + f"--arch-dir {self.arch_dir}"
        )


class _BundleBuildStep:
    """Build the bundle for one precision and optionally install it."""

    def setup_build(self, precision):
        """Read the build settings from the config.

        Needs the bundle tree made by the create step, to find the install
        subpath when compile.install is set.

        Args:
            precision (str): Precision to build, "R64" or "R32"
        """
        self.precision = precision
        self.bundle_dir = self.platform.substitute(self.config["compile.dir"])
        self.ecbundle_bin = _ecbundle_bin()
        self.compiler = self.platform.substitute("@COMPILER@")
        self.case_dir = self.platform.substitute("@CASEDIR@")
        self.arch = self.platform.substitute(self.config["submission.arch"])
        self.install = self.config["compile.install"]

        self.local_install_dir = f"{self.case_dir}/install/{precision}"
        self.exp_builddir = f"{self.case_dir}/build/{precision}"

        if self.install:
            self.git_ial_branch = self.config["compile.ial_git_version"]
            self.install_dir_root = self.platform.substitute(
                f"@INSTALL_DIR@/{self.git_ial_branch}/{precision}/{self.compiler}"
            )
            install_subpath = self.get_install_subpath()
            if install_subpath is None:
                self.install_dir = self.install_dir_root
            else:
                self.install_dir = f"{self.install_dir_root}/{install_subpath}"
            self.install_dir_latest = self.platform.substitute("@INSTALL_DIR@/latest")
            self.latest_install = self.platform.substitute(
                f"@INSTALL_DIR@/{self.git_ial_branch}"
            )
        else:
            self.install_dir = self.local_install_dir

        self.exp_bindir = self.install_dir
        self.skip_build = self.config["compile.skip_build"] and os.path.exists(
            f"{self.exp_bindir}/bin/MASTERODB"
        )

        self.ninja_arg = "--ninja " if self.config["compile"].get("ninja") else ""
        self.rebuild_args = "--clean" if self.config["compile.clean_build"] else ""
        self.prec_arg = "--without-double-precision" if precision == "R32" else ""
        forecast_only = self.config.get("compile.forecast_only", False)
        self.forecast_only_flag = "--forecast-only " if forecast_only else ""

    def get_install_subpath(self):
        """Build the install subpath from the arch directory.

        The `arch` build directory (``<bundle_dir>/source/arch/<arch>``) may
        be a symlink pointing at the build actually used, e.g. one selected by
        ecbundle based on compiler/toolchain. It is resolved, and the path
        components after the compiler name are returned, giving the subpath
        under which the build is installed (e.g. ``<toolchain>/<build_type>``).

        Returns:
            Path|None: Subpath to append to the install directory, or None
                if the compiler name is not part of the arch path.

        """
        arch_dir = Path(f"{self.bundle_dir}/source/arch/{self.arch}")
        if arch_dir.exists() and arch_dir.is_symlink():
            arch_dir = arch_dir.resolve()

        parts = arch_dir.parts
        if self.compiler in parts:
            compiler_idx = parts.index(self.compiler)
            return Path(*parts[compiler_idx + 1 :])
        return None

    def make_install_arch_symlink(self):
        """Creates a "default" symlink in the install tree.

        Creates a symlink named default in the compilers installation folder
        to point to the full path of the compiler's default architecture if
        the architecture provided was a symlink.

        """
        arch_dir = Path(f"{self.bundle_dir}/source/arch/{self.arch}")
        default_root_link = Path(self.install_dir_root) / "default"

        if arch_dir.exists() and arch_dir.is_symlink():
            if default_root_link.is_symlink():
                logger.debug("Removing old link.")
                os.unlink(default_root_link)
            shutil.copy(str(arch_dir), str(default_root_link), follow_symlinks=False)

    def _backup_bundle_file(self):
        """Copy the bundle file used for the build into the case directory."""
        source = f"{self.bundle_dir}/source/bundle.yml"
        try:
            shutil.copyfile(source, f"{self.case_dir}/bundle.yml")
            logger.info("Backed up bundle from {}", source)
        except FileNotFoundError:
            logger.info("Unable to find {}, no bundle backup made", source)

    def _remove_local_install(self):
        """Remove a previous local install, whether a symlink or a directory."""
        if os.path.islink(self.local_install_dir):
            logger.debug("Removing old link.")
            os.unlink(self.local_install_dir)
        elif os.path.exists(self.local_install_dir):
            shutil.rmtree(self.local_install_dir)

    def run_build(self):
        """Build and install the bundle, then update the install symlinks."""
        tactusmakedirs(self.exp_bindir)
        tactusmakedirs(self.exp_builddir)
        self._backup_bundle_file()

        if self.skip_build:
            logger.info("Skipping build, found {}/bin/MASTERODB", self.exp_bindir)
        else:
            self._remove_local_install()

            logger.info("Building bundle sources at {}", self.exp_builddir)
            nthreads = os.environ.get("OMP_NUM_THREADS") or os.cpu_count() or 1
            BatchJob(os.environ).run(
                f"cd {self.bundle_dir};  {self.ecbundle_bin} build "
                + f"--arch {self.arch} {self.ninja_arg} {self.forecast_only_flag}"
                + f" {self.rebuild_args} {self.prec_arg} -j{nthreads} "
                + f"--install-dir={self.install_dir} --install "
                + f"--build-dir={self.exp_builddir}"
            )
            logger.info("Installed bundle at {}", self.install_dir)

        if self.install:
            self.make_install_arch_symlink()

            _replace_symlink(self.latest_install, self.install_dir_latest)
            logger.info(
                "Symlinked installation from {} to {}",
                self.latest_install,
                self.install_dir_latest,
            )

            tactusmakedirs(Path(self.local_install_dir).parent)
            _replace_symlink(self.install_dir, self.local_install_dir)
            logger.info(
                "Symlinked installation from {} to {}",
                self.install_dir,
                self.local_install_dir,
            )


class IALClone(_CloneStep, Task):
    """Clone IAL and check out the requested version."""

    def __init__(self, config):
        """Construct object.

        Args:
            config (ParsedConfig): Configuration
        """
        Task.__init__(self, config, __class__.__name__)
        self.setup_clone()

    def execute(self):
        """Execute task."""
        self.run_clone()


class TactusBundleCreate(_BundleCreateStep, Task):
    """Create, or update, the ecbundle source bundle."""

    def __init__(self, config):
        """Construct object.

        Args:
            config (ParsedConfig): Configuration
        """
        Task.__init__(self, config, __class__.__name__)
        self.setup_create()

    def execute(self):
        """Execute task."""
        self.run_create()


class TactusBundleBuild(_BundleBuildStep, Task):
    """Build the bundle for one precision, given by task.args.prec."""

    def __init__(self, config):
        """Construct object.

        Args:
            config (ParsedConfig): Configuration
        """
        Task.__init__(self, config, __class__.__name__)
        self.setup_build(self.config.get("task.args.prec", DEFAULT_PRECISION))

    def execute(self):
        """Execute task."""
        self.run_build()


class TactusCompile(_CloneStep, _BundleCreateStep, _BundleBuildStep, Task):
    """Clone IAL, create the bundle and build it, all in one task.

    Builds every precision from build_precisions(), or only task.args.prec
    when that is given.
    """

    def __init__(self, config):
        """Construct object.

        Args:
            config (ParsedConfig): Configuration
        """
        Task.__init__(self, config, __class__.__name__)
        precision = self.config.get("task.args.prec", None)
        self.precisions = [precision] if precision else build_precisions(self.config)

        # Clone and create only read the config, so set them up here to fail
        # early on config errors. The build is set up in execute(), after the
        # bundle tree exists.
        self.setup_clone()
        self.setup_create()

    def execute(self):
        """Execute task."""
        nsteps = 2 + len(self.precisions)
        logger.info("Step 1/{}: clone IAL", nsteps)
        self.run_clone()

        logger.info("Step 2/{}: create bundle", nsteps)
        self.run_create()

        for step, precision in enumerate(self.precisions, start=3):
            logger.info("Step {}/{}: build bundle for {}", step, nsteps, precision)
            self.setup_build(precision)
            self.run_build()
