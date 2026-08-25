"""Canari — surface OI analysis using MASTERODB (configuration 701).
"""
import json
import os
import shutil

from ..config_parser import ConfigPaths
from ..datetime_utils import as_datetime
from ..initial_conditions import InitialConditions
from ..logs import logger
from ..namelist import NamelistGenerator
from ..os_utils import tactusmakedirs
from .base import Task
from .batch import BatchJob


def _patch_nampre(fort4_path):
    """Inject IPRSA predictor entries into the NAMPRE group in fort.4.

    CANARI reads NAMPRE to populate IPRSA (desired predictor VARNO codes per
    analysis type) via CANAMI → CAPREDI.  An empty NAMPRE leaves all IPRSA
    at NMDI, so CAPREDI returns NBPREA=0 for every analysis, CANEVA finds
    0 predictors and returns INBPR=0 everywhere → no OI increment.

    VARNO codes (varno_module.F90, CY50t2):  39=T2M, 58=RH2M, 11=TS(SST)
    Column indices in IPRSA(NVNUMAX,JPANAL) (canami.F90):
      NAT2=5, NAH2=6, NAST=9

    We write the full IPRSA array in Fortran column-major sequential order using
    N*value repetition syntax rather than element subscript assignments
    (IPRSA(1,5)=39).  This avoids a silent failure observed with Intel Fortran
    where 2-D subscript assignments in namelist input are accepted (POSNAMEF
    finds the group, READ executes without error) but IPRSA is not updated.
    The sequential format uses a different parser path and is unambiguous.
    """
    # IPRSA(NVNUMAX, JPANAL) layout (canami.F90 / nampre.nam.h / varno_module.F90):
    #   NVNUMAX=284, JPANAL=11, NMDI=2**31-1=2147483647
    # Fortran column-major: element (row,col) → 0-indexed flat = (col-1)*NVNUMAX + (row-1)
    #   IPRSA(1,NAT2=5) = 39  → index (5-1)*284 = 1136
    #   IPRSA(1,NAH2=6) = 58  → index (6-1)*284 = 1420
    #   IPRSA(1,NAST=9) = 11  → index (9-1)*284 = 2272
    NMDI = 2147483647
    NVNUMAX = 284
    JPANAL = 11
    iprsa = [NMDI] * (NVNUMAX * JPANAL)
    iprsa[(5 - 1) * NVNUMAX] = 39
    iprsa[(6 - 1) * NVNUMAX] = 58
    iprsa[(9 - 1) * NVNUMAX] = 11

    # Compact consecutive equal values with Fortran's N*value repetition syntax.
    def _nml_list(vals):
        parts = []
        i = 0
        while i < len(vals):
            v = vals[i]
            n = 1
            while i + n < len(vals) and vals[i + n] == v:
                n += 1
            parts.append(f"{n}*{v}" if n > 1 else str(v))
            i += n
        return ", ".join(parts)

    new_block = "&NAMPRE\n IPRSA = " + _nml_list(iprsa) + "\n/\n"

    with open(fort4_path, "r") as f:
        content = f.read()
    patched = content.replace("&NAMPRE\n/\n", new_block, 1)
    if patched == content:
        logger.warning("Canari: NAMPRE group not found in fort.4 — predictors not injected")
    with open(fort4_path, "w") as f:
        f.write(patched)



class Canari(Task):
    """Run CANARI surface analysis (OI, MASTERODB conf 701).
    """

    def __init__(self, config):
        """Construct Canari task.

        Args:
            config (tactus.ParsedConfig): Experiment configuration.
        """
        Task.__init__(self, config, __class__.__name__)
        self.basetime = as_datetime(config["general.times.basetime"])
        self.domain = config["domain.name"]
        self.cnmexp = config["general.cnmexp"]
        self.member_str = config["general.member_str"]
        self.da_scratch = self.platform.substitute(config["da.scratch"])
        # climate files (Const.Clim.MM) live in system.climdir
        self.clim_dir = self.platform.get_system_value("climdir")
        self.nbpool = config.get("da.nbpool", 12)
        self.nlgen = NamelistGenerator(config, "canari")
        self.nlgen_surfex = NamelistGenerator(config, "surfex")
        logger.debug("Constructed Canari task")

    def execute(self):
        """Run CANARI surface OI analysis."""
        yyyy = self.basetime.strftime("%Y")
        mm = self.basetime.strftime("%m")
        dd = self.basetime.strftime("%d")
        rr = self.basetime.strftime("%H")
        ymdrr = f"{yyyy}{mm}{dd}{rr}"

        # month neighbour for climate interpolation
        day = int(dd)
        m2 = (int(mm) - 1) if day <= 15 else (int(mm) + 1)
        if m2 == 0:
            m2 = 12
        if m2 == 13:
            m2 = 1
        mm2 = f"{m2:02d}"

        odb_archive_dir = os.path.join(
            self.da_scratch, yyyy, mm, dd, rr, "odbmerge_surface"
        )

        # --- binary ---
        masterodb_bin = self.get_binary("MASTERODB")
        if not os.path.lexists("MASTERODB"):
            os.symlink(masterodb_bin, "MASTERODB")

        # --- namelist ---
        self.nlgen.load("canari")

        nml = self.nlgen.assemble_namelist("canari")
        self.nlgen.write_namelist(nml, "fort.4")
        # NAMPRE: {} in the YAML produces an empty group; inject IPRSA predictor
        # definitions so that CANAMI/CAPREDI sets NBPREA > 0 for each analysis.
        _patch_nampre("fort.4")

        # --- static input files ---
        input_definition = ConfigPaths.path_from_subpath(
            self.platform.get_system_value("assimilation_input_definition")
        )
        with open(input_definition, "r", encoding="utf-8") as f:
            input_data = json.load(f)
        self.fmanager.input_data_iterator(input_data)

        # --- climate files (Const.Clim.MM → ICMSHANALCLIM/ICMSHANALCLI2) ---
        # Names must match CNMEXP set in canari_namelists.yml (currently ANAL).
        for clim_month, clim_link in [
            (mm, "ICMSHANALCLIM"),
            (mm2, "ICMSHANALCLI2"),
        ]:
            clim_src = os.path.join(self.clim_dir, f"Const.Clim.{clim_month}")
            if os.path.isfile(clim_src):
                shutil.copy2(clim_src, clim_link)
            else:
                logger.warning("Canari: climate file not found: {}", clim_src)

        # --- Const.Clim.sfx (mid-month PGD sfx for Surfex, named Const.ClimMM15.sfx) ---
        pgd_sfx_src = os.path.join(self.clim_dir, f"Const.Clim{mm}15.sfx")
        if os.path.isfile(pgd_sfx_src):
            shutil.copy2(pgd_sfx_src, "Const.Clim.sfx")
        else:
            logger.warning("Canari: PGD sfx file not found: {}", pgd_sfx_src)

        # --- first guess (atmosphere FA + surface sfx) ---
        # Delegate to the same logic used by Initialization/FirstGuess so
        # that cold-start, restart, and cycling modes are all handled correctly.
        fg_atm, fg_sfx = InitialConditions(self.config).find_initial_files()
        for link in ["ICMSHCYCLINIT", "ICMGGCYCLINIT", "ELSCFCYCLALBC000", "ELSCFANALALBC000", "ICMSHANALINIT"]:
            if not os.path.lexists(link):
                os.symlink(fg_atm, link)
        logger.info("Canari: atmospheric first guess {}", fg_atm)

        if not os.path.lexists("ICMSHCYCLINIT.sfx"):
            os.symlink(fg_sfx, "ICMSHCYCLINIT.sfx")
        if not os.path.lexists("ICMSHANALINIT.sfx"):
            shutil.copy2(fg_sfx, "ICMSHANALINIT.sfx")
        # ICMSHANAL+0000.sfx is both the surface first guess and the analysis
        # output: MASTERODB reads it, runs CANARI_SFX, and writes the analysis
        # back in place.  It must be a real writable copy — a symlink pointing
        # to the archived first guess would be modified in the archive.
        if not os.path.lexists("ICMSHANAL+0000.sfx"):
            shutil.copy2(fg_sfx, "ICMSHANAL+0000.sfx")
        logger.info("Canari: soil first guess {}", fg_sfx)

        # --- ODB ---
        if not os.path.isdir(odb_archive_dir):
            raise FileNotFoundError(
                f"Canari: merged ECMA ODB archive not found at {odb_archive_dir}"
            )
        for entry in sorted(os.listdir(odb_archive_dir)):
            src = os.path.join(odb_archive_dir, entry)
            if os.path.isdir(src) and not os.path.exists(entry):
                shutil.copytree(src, entry, symlinks=True)
        if not os.path.isdir("ECMA"):
            raise RuntimeError("Canari: ECMA directory missing after ODB copy.")
        # IOASSIGN symlinks required by MASTERODB
        if not os.path.lexists("IOASSIGN"):
            os.symlink(os.path.join("ECMA", "ECMA.IOASSIGN"), "IOASSIGN")
        if not os.path.lexists("IOASSIGN.ECMA"):
            os.symlink(os.path.join("ECMA", "ECMA.IOASSIGN"), "IOASSIGN.ECMA")

        # --- ODB environment ---
        rte = dict(os.environ)
        rte.update(
            {
                "TO_ODB_ECMWF": "0",
                "TO_ODB_SWAPOUT": "0",
                "ODB_DEBUG": "0",
                "ODB_CTX_DEBUG": "0",
                "ODB_REPRODUCIBLE_SEQNO": "4",
                "ODB_STATIC_LINKING": "1",
                "ODB_IO_METHOD": "1",
                "ODB_IO_FILESIZE": "128",
                "ODB_IO_GRPSIZE": str(self.nbpool),
                "EC_PROFILE_HEAP": "0",
                "TRACEBK": "0",
                "ODB_ANALYSIS_DATE": f"{yyyy}{mm}{dd}",
                "ODB_ANALYSIS_TIME": f"{rr}0000",
                "TIME_INIT_YYYYMMDD": f"{yyyy}{mm}{dd}",
                "TIME_INIT_HHMMSS": f"{rr}0000",
                "ODB_FEBINPATH": os.path.dirname(masterodb_bin),
                "ODB_CMA": "ECMA",
                "ODB_SRCPATH_ECMA": os.path.join(self.wdir, "ECMA"),
                "ODB_DATAPATH_ECMA": os.path.join(self.wdir, "ECMA"),
                "ODB_MERGEODB_DIRECT": "0",
                "BASETIME": ymdrr,
                "CNMEXPB": "CYCL",
                "F_RECLUNIT": "BYTE",
                "F_UFMTENDIAN": "big:10,33,50,54,81",
                # Poolmask creation via gather4poolmask_counts crashes (SIGSEGV) when
                # cmake ODB BATOR produces ghost pools with index.body.len=NMDI;
                # CANARI analysis does not require a poolmask to function.
                "ODB_ECMA_CREATE_POOLMASK": "0",
                "ODB_ECMA_POOLMASK_FILE": os.path.join(
                    self.wdir, "ECMA", "ECMA.poolmask"
                ),
                "IOASSIGN": "IOASSIGN",
            }
        )

        # fort.61 is read by oi_cavegi.F90 (unit 61) for vegetation polynomial coefficients.
        # POLYNOMES_ISBA from the Harmonie-IAL const area provides the standard coefficients.
        polynomes_src = (
            "/lus/h2resw01/hpcperm/fag/release/Harmonie-IAL/const/sa_const/POLYNOMES_ISBA"
        )
        if not os.path.exists("fort.61"):
            os.symlink(polynomes_src, "fort.61")

        # EXSEG1.nam is read by MASTERODB's SURFEX OI (unit 39) when LAEICS_SX=.T.
        # Generated via the same nlgen_surfex/assemble_surfex.yml plumbing as Forecast's
        # EXSEG1.nam ("canari" target = forecast's full scheme config + the NAM_NACVEG/
        # NAM_ASSIM overrides CANARI's OI needs), so CANARI's SURFEX runs with the same
        # LECOSG/NAM_IO_OFFLINE/ISBA-patch configuration as the forecast model that
        # produced its PGD/PREP input, instead of falling back to SURFEX's own defaults.
        self.nlgen_surfex.load("canari")
        surfex_settings = self.nlgen_surfex.assemble_namelist("canari")
        self.nlgen_surfex.write_namelist(surfex_settings, "EXSEG1.nam")

        # libphyex_dp.so bakes in mpi_serial stubs as T (strong) symbols, which
        # override the W (weak) mpi_initialized_ from libmpi_mpifh.so process-wide.
        # The serial stub always returns ldflag=.FALSE., so DrHook's startup check
        # "is MPI initialized?" fires even after MPL_INIT. Bypass the assertion;
        # CANARI surface OI computation is unaffected (it does not need real MPI).
        rte["DR_HOOK_ASSERT_MPI_INITIALIZED"] = "0"

        # RESOLVED (2026-08-25): CANARI's oi_control_ SIGSEGV (TBB allocator
        # internals, oi_control.F90:562/573) was multi-rank specific — see the
        # full writeup in this experiment's config.toml under
        # [submission.task_exceptions.Canari]. NPROC is pinned to 1 there and
        # picked up below via self.config.get(...), which re-reads that TOML
        # fresh on every run. [submission.task_exceptions.Canari.ENV] also
        # documents OMPI_MCA_pml=ob1 (needed for a separate MPI_Finalize/UCX
        # crash, still present even at NPROC=1) for the record, but .ENV
        # entries are only baked into the generated job script at suite
        # regeneration time ("tactus case -c ..."), unlike self.config reads
        # — confirmed the hard way (job ran with NPROC=1 correctly but no
        # OMPI_MCA_pml in its environment, and crashed again). Set explicitly
        # here too so it actually takes effect without needing a live-suite
        # regeneration.
        rte["OMPI_MCA_pml"] = "ob1"

        # Kept from that investigation as cheap, still-useful safety nets:
        rte["OMP_NUM_THREADS"] = "1"  # harmless; not itself the fix
        rte["UCX_HANDLE_ERRORS"] = "bt"  # gets a backtrace out of UCX's signal handler, if it ever fires again

        # Diagnostic only (10-50x slowdown from Valgrind's instrumentation);
        # leave VALGRIND_CANARI unset for normal runs.
        _use_valgrind = os.environ.get("VALGRIND_CANARI", "0") == "1"

        # --- run MASTERODB (CANARI conf 701) ---
        # cmake MASTERODB: libphyex_dp.so bakes in mpi_serial T symbols that override
        # real OpenMPI W symbols process-wide → MPL_NUMPROC=1 always. Force srun -n
        # {nproc} so the SLURM task count matches MPL, regardless of SLURM --ntasks
        # allocation (nproc is 1 — see the resolution note above).
        nproc = self.config.get("submission.task_exceptions.Canari.NPROC", 1)
        output_file = "ICMSHANAL+0000"
        # `ulimit -c unlimited` before exec so a core file is produced on any
        # future SIGSEGV/SIGABRT — gdb on the core gives the exact faulting
        # frame regardless of which runtime's signal handler (or none)
        # intercepts it. This is exactly what pinned down the TBB allocator
        # fault during the investigation above.
        if _use_valgrind:
            inner = (
                "valgrind --tool=memcheck --track-origins=yes --error-limit=no "
                "--log-file=valgrind.rank%p.log ./MASTERODB"
            )
        else:
            inner = "./MASTERODB"
        BatchJob(rte, wrapper=f"srun -n {nproc}").run(
            f"bash -c 'ulimit -c unlimited; exec {inner}'"
        )

        if not os.path.isfile(output_file) or os.path.getsize(output_file) == 0:
            raise RuntimeError(f"Canari: {output_file} not produced or empty.")

        # --- archive output ---
        out_dir = os.path.join(self.da_scratch, yyyy, mm, dd, rr, "canari")
        tactusmakedirs(out_dir)
        shutil.copy2(output_file, os.path.join(out_dir, output_file))
        if os.path.isfile("ICMSHANAL+0000.sfx"):
            shutil.copy2(
                "ICMSHANAL+0000.sfx", os.path.join(out_dir, "ICMSHANAL+0000.sfx")
            )
        logger.info("Canari: analysis archived to {}", out_dir)
        self.archive_logs(["NODE.001_01", "fort.4"])
