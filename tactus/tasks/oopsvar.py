"""OopsVar task — OOPS-based 3D-Var (screening + minimization via OOVAR)."""

import datetime
import glob
import json
import os
import shutil
import stat
import subprocess

import yaml

from ..config_parser import ConfigPaths
from ..datetime_utils import as_datetime
from ..logs import logger
from ..namelist import NamelistGenerator
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob


class OopsVar(Task):
    """Run OOPS-based 3D-Var (screening + minimization in one OOVAR call).

    Reads the merged ECMA ODB from the OdbMerge scratch, the blended
    first-guess from BlendSur, and produces the 3D-Var analysis
    (``ICMSHOOPS+0000``) archived to ``da.analysis_dir``.
    """

    def __init__(self, config):
        """Construct OopsVar task.

        Args:
            config (tactus.ParsedConfig): Experiment configuration.
        """
        Task.__init__(self, config, __class__.__name__)
        self.basetime = as_datetime(config["general.times.basetime"])
        self.da_scratch = self.platform.substitute(config["da.scratch"])
        self.da_const_dir = self.platform.substitute(config["da.const_dir"])
        self.domain = config["domain.name"]
        self.varbc_dir = self.platform.substitute(config.get("da.varbc_dir", ""))
        self.varbc_init_dir = self.platform.substitute(
            config.get("da.varbc_init_dir", "")
        )
        self.analysis_dir = self.platform.substitute(config.get("da.analysis_dir", ""))
        self.da_bgcycle = config.get("da.bgcycle", "")
        self.rttov_coef_dir = self.platform.substitute(
            config.get("da.rttov_coef_dir", config.get("da.const_dir", ""))
        )
        self.nbpool = config.get("da.oops.nbpool", 128)
        self.oovar_input_def = config.get("da.oovar_input_definition", "")
        self.nlgen = NamelistGenerator(config, "oovar")
        logger.debug("Constructed OopsVar task")

    # ------------------------------------------------------------------

    def execute(self):
        """Run OOVAR 3D-Var analysis and archive the result."""
        yyyy = self.basetime.strftime("%Y")
        mm = self.basetime.strftime("%m")
        dd = self.basetime.strftime("%d")
        rr = self.basetime.strftime("%H")
        ymdrr = f"{yyyy}{mm}{dd}{rr}"

        nproc = int(os.environ.get("NPROC", "128"))

        odb_dir = os.path.join(
            self.da_scratch, yyyy, mm, dd, rr, "odbmerge_3dvar", "ECMA"
        )
        # --- binary ---
        oovar_bin = self.get_binary("ifs4dvar.DP")
        bindir = os.path.dirname(oovar_bin)
        os.symlink(oovar_bin, "ifs4dvar.DP")

        # ioassign tools — must be executable in the working directory
        for tool in ("ioassign", "create_ioassign"):
            src = os.path.join(bindir, tool)
            if os.path.isfile(src):
                shutil.copy2(src, tool)
                os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC | stat.S_IXGRP)

        # --- ECMA ODB ---
        if not os.path.isdir(odb_dir):
            raise FileNotFoundError(f"OopsVar: merged ECMA ODB not found at {odb_dir}")
        shutil.copytree(odb_dir, "ECMA", symlinks=True)
        # The merged IOASSIGN references subbases as ../ECMA.{obstype}/ relative to
        # ECMA/, so they must exist alongside ECMA in the work dir.
        odbmerge_dir = os.path.dirname(odb_dir)
        for entry in os.listdir(odbmerge_dir):
            if entry.startswith("ECMA.") and os.path.isdir(
                os.path.join(odbmerge_dir, entry)
            ):
                if not os.path.lexists(entry):
                    os.symlink(os.path.join(odbmerge_dir, entry), entry)

        # --- CCMA skeleton via create_ioassign (serial) ---
        os.makedirs("CCMA", exist_ok=True)
        rte_setup = dict(os.environ)
        rte_setup["PATH"] = f"{os.getcwd()}:{rte_setup.get('PATH', '')}"
        BatchJob(rte_setup, wrapper="").run(f"./create_ioassign -lCCMA -n{nproc}")
        # Build a combined IOASSIGN with ECMA entries (from merged OdbMerge output)
        # followed by the CCMA entries (from create_ioassign). OOVAR reads both.
        if os.path.isfile("IOASSIGN") and os.path.isfile("ECMA/IOASSIGN"):
            with open("ECMA/IOASSIGN", "a") as ecma_io, open("IOASSIGN") as ccma_io:
                ecma_io.write(ccma_io.read())
        if os.path.isfile("ECMA/IOASSIGN"):
            if not os.path.isfile("CCMA/IOASSIGN"):
                shutil.copy2("ECMA/IOASSIGN", "CCMA/IOASSIGN")
            # Always overwrite — create_ioassign wrote CCMA-only; we need ECMA+CCMA.
            shutil.copy2("ECMA/IOASSIGN", "IOASSIGN")

        # --- first guess: blended surface analysis from BlendSur ---
        blendsur_file = os.path.join(
            self.da_scratch,
            yyyy,
            mm,
            dd,
            rr,
            "blendsur",
            "ICMSHANAL+0000_updated_surface",
        )
        if not os.path.isfile(blendsur_file):
            raise FileNotFoundError(
                f"OopsVar: BlendSur output not found at {blendsur_file}"
            )
        logger.info("OopsVar: first guess from BlendSur {}", blendsur_file)
        shutil.copy2(blendsur_file, "ICMSHOOPS+0000")
        # Convert spectral .PHYS articles to gridpoint so OOVAR/RDFA2GP can read
        # them (cold-start coupling files from ARPEGE FULLPOS use spectral storage
        # which RDFA2GP requests as gridpoint, causing FACILO DESACCORD CSP./PDG.)
        # self._sp2gp_first_guess("ICMSHOOPS+0000")
        fg_local = "ICMSHOOPS+0000"
        for link_name in (
            "ICMSHMINIIMIN",
            "ICMSHMINIINIT",
            "ICMRFMINI0000",
            "ICMSHOOPSINIT",
            "ICMSHOOPSIMIN",
            "ICMSHSCREINIT",
        ):
            if not os.path.lexists(link_name):
                os.symlink(fg_local, link_name)

        # --- VarBC ---
        dateprev = (self.basetime - datetime.timedelta(days=1)).strftime("%Y/%m/%d")
        varbc_src = os.path.join(self.varbc_dir, dateprev, f"VARBC.cycle_{rr}")
        varbc_init_src = (
            os.path.join(self.varbc_init_dir, f"VARBC.cycle_{rr}")
            if self.varbc_init_dir
            else ""
        )
        if os.path.isfile(varbc_src):
            shutil.copy2(varbc_src, "VARBC.cycle")
        elif varbc_init_src and os.path.isfile(varbc_init_src):
            shutil.copy2(varbc_init_src, "VARBC.cycle")
            logger.info("OopsVar: VARBC initialised from {}", varbc_init_src)
        elif ymdrr != self.da_bgcycle:
            raise FileNotFoundError(
                f"OopsVar: VARBC file not found at {varbc_src}"
                + (f" or init path {varbc_init_src}" if varbc_init_src else "")
                + ". Cold-start (bgcycle) is the only allowed run without VARBC."
            )

        # --- B-matrix (3 files for OOPS: .bal, .cv, .cvt) ---
        bmat_dir = os.path.join(self.da_const_dir, self.domain)
        for bmat in ("stabal96.bal", "stabal96.cv", "stabal96.cvt"):
            src = os.path.join(bmat_dir, bmat)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.basename(bmat))
            else:
                logger.warning("OopsVar: B-matrix file not found: {}", src)

        # --- constant file links ---
        self._link_const_files()

        # --- OOPS namelists and JSON config ---
        self._link_oops_namelists(yyyy, mm, dd, rr)

        # --- ODB environment ---
        rte = dict(os.environ)
        rte.update({
            "ECKIT_MPI_FORCE": "parallel",
            "EC_MEMINFO": "0",
            "EC_LINUX_TRBK": "1",
            "EC_MPI_ATEXIT": "0",
            "EC_PROFILE_HEAP": "0",
            "OOPS_LOGFILE": "oops.log",
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "1"),
            "F_UFMTENDIAN": "big",
            "F_RECLUNIT": "BYTE",
            "MKL_DYNAMIC": "FALSE",
            "DR_HOOK": "0",
            "DR_HOOK_SILENT": "1",
            "DR_HOOK_IGNORE_SIGNALS": "-1",
            "DR_HOOK_ASSERT_MPI_INITIALIZED": "0",
            # ODB
            "BASETIME": ymdrr,  # hretr_conv.F90:380 expects YYYYMMDDHH, not ISO 8601
            "ODB_ANALYSIS_DATE": f"{yyyy}{mm}{dd}",
            "ODB_ANALYSIS_TIME": f"{rr}0000",
            "ODB_STATIC_LINKING": "1",
            "TO_ODB_ECMWF": "0",
            "ODB_TRACE_FILE": "List_odb",
            "TO_ODB_DEBUG": "0",
            "ODB_TRACE_PROC": "0",
            "ODB_IO_METHOD": "1",
            "ODB_IO_FILESIZE": "128",
            "ODB_IO_GRPSIZE": str(self.nbpool),
            "BATOR_NBSLOT": str(self.config.get("da.bator_nbslot", 1)),
            "ODB_CCMA_CREATE_DIRECT": "1",
            "BATOR_WINDOW_LEN": str(self.config.get("da.bator_window_len", 180)),
            "BATOR_WINDOW_SHIFT": str(self.config.get("da.bator_window_shift", -90)),
            "BATOR_SLOT_LEN": str(self.config.get("da.bator_slot_len", 0)),
            "BATOR_CENTER_LEN": "0",
            "ODB_SRCPATH_RSTBIAS": os.path.join(self.wdir, "ECMA"),
            "ODB_SRCPATH_ECMA": os.path.join(self.wdir, "ECMA"),
            "ODB_DATAPATH_ECMA": os.path.join(self.wdir, "ECMA"),
            "ODB_SRCPATH_CCMA": os.path.join(self.wdir, "CCMA"),
            "ODB_DATAPATH_CCMA": os.path.join(self.wdir, "CCMA"),
            "ODB_CCMA_CREATE_POOLMASK": "1",
            "ODB_CCMA_POOLMASK_FILE": os.path.join(self.wdir, "CCMA", "CCMA.poolmask"),
            "ODB_CMA": "CCMA",
            "IOASSIGN": os.path.join(self.wdir, "IOASSIGN"),
            "RTTOV_COEFDIR": self.rttov_coef_dir,
            "PATH": f"{os.getcwd()}:{rte.get('PATH', '')}",
            "OOPS_TRACE": "1",
        })

        # --- run OOVAR ---
        with open("oops.log", "a") as oops_log:
            BatchJob(rte, wrapper=self.platform.substitute(self.wrapper)).run(
                "./ifs4dvar.DP oops.json", logfile=oops_log
            )

        # --- verify and archive ---
        analysis = "ICMSHOOPS+0000"
        if not os.path.isfile(analysis) or os.path.getsize(analysis) == 0:
            raise RuntimeError("OopsVar: analysis file not produced or empty.")

        if self.analysis_dir:
            out_dir = os.path.join(self.analysis_dir, yyyy, mm, dd)
            tactusmakedirs(out_dir)
            dst = os.path.join(out_dir, f"analysis.{ymdrr}")
            shutil.copy2(analysis, dst)
            logger.info("OopsVar: analysis archived to {}", dst)

        # Archive updated VarBC to varbc_dir for the next cycle to read
        if self.varbc_dir and os.path.isfile("VARBC.cycle"):
            varbc_out = os.path.join(self.varbc_dir, yyyy, mm, dd)
            tactusmakedirs(varbc_out)
            shutil.copy2("VARBC.cycle", os.path.join(varbc_out, f"VARBC.cycle_{rr}"))

        self.archive_logs(["oops.log", "NODE.001_01"])

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _link_oops_namelists(self, yyyy, mm, dd, rr):
        """Generate all OOPS namelist files and oops.json from YAML sources."""
        namelist_map = {
            "oovar_fort4": "fort.4",
            "oovar_obs": "naml_observations",
            "oovar_obs_tlad": "naml_observations_tlad",
            "oovar_bmatrix": "naml_bmatrix",
            "oovar_geometry": "naml_standard_geometry",
            "oovar_traj": "naml_traj_model",
            "oovar_linmod": "naml_linear_model",
            "oovar_nonlinmod": "naml_nonlinear_model",
            "oovar_write_spec": "naml_oops_write_spec",
            "oovar_write_analysis": "naml_write_analysis",
            "oovar_gom_setup": "namelist_gom_setup",
            "oovar_gom_setup_hres": "namelist_gom_setup_hres",
        }
        for target, output_file in namelist_map.items():
            self.nlgen.generate_namelist(target, output_file)

        # yomlocs.F90 opens namelist_gom_setup_<N> (indexed by obs type slot)
        if os.path.isfile("namelist_gom_setup") and not os.path.lexists(
            "namelist_gom_setup_0"
        ):
            os.symlink("namelist_gom_setup", "namelist_gom_setup_0")

        self._render_oops_json(yyyy, mm, dd, rr)

    def _render_oops_json(self, yyyy, mm, dd, rr):
        """Render oovar_config.yml to oops.json with the analysis time substituted."""
        config_yml = self.nlgen.nlfile.parent / "oovar_config.yml"
        with open(config_yml) as f:
            text = f.read()
        analysis_time = f"{yyyy}-{mm}-{dd}T{rr}:00:00Z"
        text = text.replace("{analysis_time}", analysis_time)
        oops_config = yaml.safe_load(text)
        with open("oops.json", "w") as f:
            json.dump(oops_config, f, indent=2)
        logger.debug("OopsVar: wrote oops.json for {}", analysis_time)

    def _link_const_files(self):
        """Symlink constant input files into the working directory."""
        if not self.oovar_input_def:
            logger.warning(
                "OopsVar: da.oovar_input_definition not set; skipping constant file links"
            )
            return

        def_path = ConfigPaths.path_from_subpath(self.oovar_input_def)
        with open(def_path) as f:
            entries = json.load(f)

        base_map = {
            "const_dir": self.da_const_dir,
            "rttov_coef_dir": self.rttov_coef_dir,
            "ir_atlas_dir": os.path.join(self.da_const_dir, "ir_atlas"),
        }

        for entry in entries:
            if entry.get("amsub_alias"):
                # RTTOV uses "amsub" sensor code but MetOp/NOAA-19 ship MHS coef files;
                # create amsub aliases so RTTOV finds them under both names.
                for mhs_link in glob.glob("rtcoef_*_mhs.*"):
                    amsub_link = mhs_link.replace("_mhs.", "_amsub.")
                    if not os.path.lexists(amsub_link):
                        os.symlink(os.path.realpath(mhs_link), amsub_link)
                continue

            base_dir = base_map.get(entry["base"], "")
            if not os.path.isdir(base_dir):
                if not entry.get("optional", True):
                    raise FileNotFoundError(
                        f"OopsVar: base directory not found: {base_dir!r}"
                    )
                continue

            dest = entry.get("destination")
            for src in glob.glob(os.path.join(base_dir, entry["pattern"])):
                link = dest if dest else os.path.basename(src)
                if not os.path.lexists(link):
                    os.symlink(src, link)

    @staticmethod
    def _sp2gp_first_guess(path: str) -> None:
        """Convert 3D spectral FA articles to gridpoint in *path*, in-place.

        Cold-start coupling files from ARPEGE FULLPOS store 3D model fields
        (u/v/T/ps) as LAM spectral coefficients (LSUSPECA_GP=T). OOVAR's
        RDFA2GP requests them as gridpoint (LDCOSP=F) → FACILO DESACCORD
        CSP./PDG. (KREP=-92).

        SPECSURF.* fields are deliberately excluded: the model geometry setup
        reads SPECSURFGEOPOTEN with LDCOSP=T (spectral), so converting it to
        gridpoint would trigger an inverse DESACCORD.
        """
        python = "/home/acrd/public/venvs/epygram/2.0.3/bin/python3"
        if not os.path.isfile(python):
            logger.warning(
                "OopsVar: epygram python not found at {}, "
                "skipping SP→GP conversion (warm-start may still work)",
                python,
            )
            return
        # Inline epygram script — mirrors fa_sp2gp.fa_sp2gp(in_place=True)
        # but skips SPECSURF fields that must remain spectral.
        script = f"""
import epygram
epygram.init_env()
r = epygram.formats.resource({path!r}, openmode='a', fmt='FA')
converted = 0
for fn in r.listfields():
    field = r.readfield(fn)
    if not isinstance(field, epygram.fields.H2DField) or not field.spectral:
        continue
    if fn.startswith('SPECSURF'):
        continue
    compression = {{'KNBPDG': r.fieldscompression[fn]['KNBCSP']}}
    if compression.get('KNBPDG') == 0:
        compression['KNGRIB'] = 0
    field.sp2gp()
    r.writefield(field, compression)
    converted += 1
r.close()
print(converted)
"""
        result = subprocess.run(
            [python, "-c", script],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"OopsVar: SP→GP conversion failed for {path}:\n{result.stderr}"
            )
        logger.info(
            "OopsVar: SP→GP conversion completed for {} ({} fields)",
            path,
            result.stdout.strip(),
        )
