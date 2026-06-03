"""OopsVar task — OOPS-based 3D-Var (screening + minimization via OOVAR).
"""
import datetime
import glob
import os
import shutil
import stat
import subprocess

from ..datetime_utils import as_datetime
from ..logs import logger
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
        self.oops_namelist_dir = self.platform.substitute(
            config.get("da.oops.namelist_dir", "")
        )
        self.varbc_dir = self.platform.substitute(config.get("da.varbc_dir", ""))
        self.varbc_init_dir = self.platform.substitute(config.get("da.varbc_init_dir", ""))
        self.analysis_dir = self.platform.substitute(config.get("da.analysis_dir", ""))
        self.da_bgcycle = config.get("da.bgcycle", "")
        self.rttov_coef_dir = self.platform.substitute(
            config.get("da.rttov_coef_dir", config.get("da.const_dir", ""))
        )
        self.nbpool = config.get("da.oops.nbpool", 128)

        # Minimization parameters
        self.niter = config.get("da.oops.niter", 66)
        self.nsimu = config.get("da.oops.nsimu", 69)
        self.rednmc = config.get("da.oops.rednmc", 0.5)

        # Geometry / parallel I/O
        self.nproma = config.get("da.oops.nproma", -32)
        self.nprgpns = config.get("da.oops.nprgpns", 128)
        self.nprgpew = config.get("da.oops.nprgpew", 1)
        self.nprtrv = config.get("da.oops.nprtrv", 1)
        self.nprtrw = config.get("da.oops.nprtrw", 128)
        self.nstrin = config.get("da.oops.nstrin", 2)
        self.nstrout = config.get("da.oops.nstrout", 2)

        # JK transform / covariance parameters (Fortran .TRUE./.FALSE. strings)
        def _f90bool(val):
            return ".TRUE." if val else ".FALSE."

        self.lejk = _f90bool(config.get("da.oops.lejk", False))
        self.lsprt = _f90bool(config.get("da.oops.lsprt", False))
        self.qlgp = _f90bool(config.get("da.oops.qlgp", True))
        self.qlsp = _f90bool(config.get("da.oops.qlsp", False))
        self.nsmaxjk = config.get("da.oops.nsmaxjk", 215)
        self.alphakt = config.get("da.oops.alphakt", 0.70)
        self.alphakvor = config.get("da.oops.alphakvor", 0.80)
        self.alphakdiv = config.get("da.oops.alphakdiv", 0.10)
        self.alphakq = config.get("da.oops.alphakq", 0.04)
        self.alphakp = config.get("da.oops.alphakp", 0.0)
        self.presinfjk = config.get("da.oops.presinfjk", 100500.0)
        self.presupjk = config.get("da.oops.presupjk", 98000.0)
        self.ntruncjk = config.get("da.oops.ntruncjk", 8)

        if not self.oops_namelist_dir:
            raise RuntimeError(
                "OopsVar: da.oops.namelist_dir must be configured when da.do_upper_air = true."
            )
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
        oovar_bin = self.get_binary("OOVAR")
        bindir = os.path.dirname(oovar_bin)
        os.symlink(oovar_bin, "OOVAR")

        # ioassign tools — must be executable in the working directory
        for tool in ("ioassign", "create_ioassign"):
            src = os.path.join(bindir, tool)
            if os.path.isfile(src):
                shutil.copy2(src, tool)
                os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC | stat.S_IXGRP)

        # --- ECMA ODB ---
        if not os.path.isdir(odb_dir):
            raise FileNotFoundError(
                f"OopsVar: merged ECMA ODB not found at {odb_dir}"
            )
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
            self.da_scratch, yyyy, mm, dd, rr, "blendsur", "ICMSHANAL+0000_updated_surface"
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
        fg_local = os.path.abspath("ICMSHOOPS+0000")
        for link_name in (
            "ICMSHMINIIMIN", "ICMSHMINIINIT", "ICMRFMINI0000",
            "ICMSHOOPSINIT", "ICMSHOOPSIMIN",
            "ICMSHSCREINIT",
        ):
            if not os.path.lexists(link_name):
                os.symlink(fg_local, link_name)

        # --- VarBC ---
        dateprev = (self.basetime - datetime.timedelta(days=1)).strftime("%Y/%m/%d")
        varbc_src = os.path.join(self.varbc_dir, dateprev, f"VARBC.cycle_{rr}")
        varbc_init_src = os.path.join(self.varbc_init_dir, f"VARBC.cycle_{rr}") if self.varbc_init_dir else ""
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

        # --- constant links ---
        const_globs = [
            "ATLAS_*", "ATLAS*", "errgrib*", "ECOZC", "MCICA", "RAD*",
            "amv_*", "bcor_noaa.dat", "bcor_meto.dat",
            "sigmab.dat", "scat*", "rmtberr*", "correl.dat", "rszcoef_fmt",
            "iasichannels*",
        ]
        for pattern in const_globs:
            for src in glob.glob(os.path.join(self.da_const_dir, pattern)):
                link = os.path.basename(src)
                if not os.path.lexists(link):
                    os.symlink(src, link)

        # RTTOV coefficient and cloud-detection files from the rttov subfolder.
        rttov_dir = os.path.join(self.da_const_dir, "rttov")
        if os.path.isdir(rttov_dir):
            for src in glob.glob(os.path.join(rttov_dir, "*")):
                link = os.path.basename(src)
                if not os.path.lexists(link):
                    os.symlink(src, link)
        else:
            for pattern in ("sccldcoef*", "rtcoef_*"):
                for src in glob.glob(os.path.join(self.da_const_dir, pattern)):
                    link = os.path.basename(src)
                    if not os.path.lexists(link):
                        os.symlink(src, link)

        # RTTOV looks for "amsub" coefficient files (sensor code 4), but MetOp and
        # NOAA-19 ship MHS (the AMSU-B successor); their coef files are named "mhs".
        # Create amsub aliases so RTTOV finds them under both names.
        for mhs_link in glob.glob("rtcoef_*_mhs.*"):
            amsub_link = mhs_link.replace("_mhs.", "_amsub.")
            if not os.path.lexists(amsub_link):
                os.symlink(os.path.realpath(mhs_link), amsub_link)

        # UW IR emissivity atlas (required for IASI): link all files from ir_atlas subdir.
        ir_atlas_dir = os.path.join(self.da_const_dir, "ir_atlas")
        if os.path.isdir(ir_atlas_dir):
            for src in glob.glob(os.path.join(ir_atlas_dir, "*")):
                link = os.path.basename(src)
                if not os.path.lexists(link):
                    os.symlink(src, link)

        # Optional LHN files (skip silently if absent)
        for lhn_file in ("MAP_INCA_*.txt", "LHN_DUMMY.fa"):
            for src in glob.glob(os.path.join(self.da_const_dir, lhn_file)):
                link = os.path.basename(src)
                if not os.path.lexists(link):
                    os.symlink(src, link)

        # --- OOPS namelists ---
        self._link_oops_namelists(nproc, yyyy, mm, dd, rr)

        # --- ODB environment ---
        rte = dict(os.environ)
        rte.update(
            {
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
                # ODB
                "BASETIME": ymdrr,  # hretr_conv.F90:380 expects YYYYMMDDHH, not ISO 8601
                "ODB_ANALYSIS_DATE": f"{yyyy}{mm}{dd}",
                "ODB_ANALYSIS_TIME": f"{rr}0000",
                "ODB_STATIC_LINKING": "1",
                "TO_ODB_ECMWF": "0",
                "ODB_TRACE_FILE": "List_odb",
                "TO_ODB_DEBUG": "0",
                "ODB_TRACE_PROC": "0",
                "ODB_IO_METHOD": "4",
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
            }
        )

        # --- run OOVAR ---
        BatchJob(rte, wrapper=self.platform.substitute(self.wrapper)).run(
            "./OOVAR oops.json"
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

    def _link_oops_namelists(self, nproc, yyyy, mm, dd, rr):
        """Generate / link all OOPS namelist files from da.oops.namelist_dir."""
        nd = self.oops_namelist_dir

        # Substitution dict for the main minimization namelist template.
        # "NBPROC" (no braces) handles the namelist_oops_leftovers style where
        # all parallelism settings are written as bare NBPROC.
        subst = {
            "{NBPROC}":    str(nproc),
            "NBPROC":      str(nproc),
            "{NPRTRV}":    str(self.nprtrv),
            "{NPRTRW}":    str(self.nprtrw),
            "{NSTRIN}":    str(self.nstrin),
            "{NSTROUT}":   str(self.nstrout),
            "{NPROMA}":    str(self.nproma),
            "{NPRGPEW}":   str(self.nprgpew),
            "{NPRGPNS}":   str(self.nprgpns),
            "{niter}":     str(self.niter),
            "{nsimu}":     str(self.nsimu),
            "{rednmc}":    str(self.rednmc),
            "{LEJK}":      self.lejk,
            "{LSPRT}":     self.lsprt,
            "{qlsp}":      self.qlsp,
            "{qlgp}":      self.qlgp,
            "{NSMAXJK}":   str(self.nsmaxjk),
            "{ALPHAKT}":   str(self.alphakt),
            "{ALPHAKVOR}": str(self.alphakvor),
            "{ALPHAKDIV}": str(self.alphakdiv),
            "{ALPHAKQ}":   str(self.alphakq),
            "{ALPHAKP}":   str(self.alphakp),
            "{PRESINFJK}": str(self.presinfjk),
            "{PRESUPJK}":  str(self.presupjk),
            "{NTRUNCJK}":  str(self.ntruncjk),
        }

        # fort.4 from namelist_oops_leftovers template
        self._fill_template(
            os.path.join(nd, "namelist_oops_leftovers"), "fort.4", subst
        )

        # Geometry namelists — NPROMA may be hardcoded in the file; substitution
        # is a no-op in that case but harmless.
        geom_subst = {"{nproma}": str(self.nproma)}
        self._fill_template(
            os.path.join(nd, "naml_standard_geometry"),
            "naml_standard_geometry",
            geom_subst,
        )
        # geometry_tENS is only needed for 3D-EnVar; skip with a warning if absent
        tENS_src = os.path.join(nd, "naml_geometry_tENS")
        if os.path.isfile(tENS_src):
            self._fill_template(tENS_src, "naml_standard_geometry_tENS", geom_subst)
        else:
            logger.warning("OopsVar: namelist_geometry_tENS not found, skipping (3D-Var only)")

        # OOPS JSON — written as oops.json; date placeholders substituted if present
        json_subst = {
            "{yyyy}": yyyy,
            "{mm}":   mm,
            "{dd}":   dd,
            "{hh}":   rr,
            "{{now.iso8601()}}": f"{yyyy}-{mm}-{dd}T{rr}:00:00Z",
        }
        self._fill_template(
            os.path.join(nd, "3dvar.json"), "oops.json", json_subst
        )

        # Namelists linked as-is (no substitution)
        link_map = {
            "naml_bmatrix":           "naml_bmatrix",
            "naml_observations_tlad": "naml_observations_tlad",
            "naml_observations":      "naml_observations",
            "naml_traj_model":        "naml_traj_model",
            "naml_linear_model":      "naml_linear_model",
            "naml_nonlinear_model":   "naml_nonlinear_model",
            "naml_oops_write_spec":   "naml_oops_write_spec",
            "naml_write_analysis":    "naml_write_analysis",
            "namelist_gom_setup_hres": "namelist_gom_setup_hres",
            "namelist_gom_setup":      "namelist_gom_setup",
        }
        for src_name, link_name in link_map.items():
            src = os.path.join(nd, src_name)
            if os.path.isfile(src):
                if not os.path.lexists(link_name):
                    os.symlink(src, link_name)
                    os.chmod(link_name, os.stat(link_name).st_mode | stat.S_IEXEC)
            else:
                logger.warning("OopsVar: OOPS namelist not found: {}", src)

        # yomlocs.F90 opens namelist_gom_setup_<N> (indexed by obs type slot)
        if os.path.lexists("namelist_gom_setup") and not os.path.lexists("namelist_gom_setup_0"):
            os.symlink("namelist_gom_setup", "namelist_gom_setup_0")

        # iasichannels — copy so OOVAR can read it
        iasi = os.path.join(nd, "iasichannels")
        if os.path.isfile(iasi):
            shutil.copy2(iasi, "iasichannels")

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
            path, result.stdout.strip(),
        )

    @staticmethod
    def _fill_template(src_path, dst_path, substitutions):
        """Read *src_path*, apply *substitutions* dict, write to *dst_path*.

        Raises FileNotFoundError if *src_path* does not exist.
        """
        if not os.path.isfile(src_path):
            raise FileNotFoundError(
                f"OopsVar: OOPS template not found: {src_path}"
            )
        text = open(src_path).read()
        for placeholder, value in substitutions.items():
            text = text.replace(placeholder, value)
        with open(dst_path, "w") as fh:
            fh.write(text)
        logger.debug("OopsVar: wrote {} from template {}", dst_path, src_path)
