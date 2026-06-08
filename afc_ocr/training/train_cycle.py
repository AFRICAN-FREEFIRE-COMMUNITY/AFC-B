"""
afc_ocr/training/train_cycle.py
================================================================================
THE HANDS-OFF WEEKLY LOOP. This is the automated local-GPU training cycle for the
AFC self-hosted OCR student model. It is meant to run UNATTENDED, on the operator's
Windows PC (an NVIDIA RTX 3060, 12 GB), on a schedule (Windows Task Scheduler, weekly,
see setup_windows_schedule.md). Nobody is at the keyboard when it fires.

WHAT IT DOES, IN ONE LINE
    Ask the AFC server "is a retrain due?" -> if yes, pull the new training data ->
    fine-tune the student locally on the GPU (offline) -> let the eval gate decide if
    the new model is genuinely better -> push the improved bundle back to the server.

WHERE THIS SITS IN THE SYSTEM (end to end)
    This is the CLIENT side of the OCR learning loop. It talks to three admin-gated
    server endpoints (all under <AFC_API_BASE>/events/ocr/, Bearer-token auth):

        1. GET  /events/ocr/retrain-status/
               -> {due, new_gold_since_last, last_dataset_version, reason}
           "Has enough new admin-confirmed (gold) data accumulated to bother retraining?"
           The server owns that policy; this client just obeys it (unless --force).

        2. GET  /events/ocr/dataset-export/?splits=train,eval&include_synthetic=true
               -> a ZIP (rec_gt.txt + crops/ + rec_keys.txt + manifest.json)
           The exact same dataset shape afc_ocr.services.dataset.assemble_rec_dataset
           produces, just delivered over HTTP instead of a hand-copied tarball. The ZIP
           is unpacked into this run's work dir so the trainer can read it locally.

        3. POST /events/ocr/upload-model/  (multipart file=<bundle.zip>, ?promote=true)
               -> {version, stored, promoted, gate_passed}
           Ships the produced student_v<N>/ bundle (zipped) back. The server stores it
           and, when promote=true AND its own gate re-check agrees, flips the live
           pointer (afc_ocr.services.model_registry.promote) so the next OCR request
           uses the new model. No Django restart needed (see model_registry).

    The local heavy lifting (the actual fine-tune) is delegated to
    afc_ocr.training.finetune.run_finetune(), which is the OFF-BOX driver documented in
    afc_ocr/training/finetune_ppocrv5.md. That function is the ONLY place paddle is
    imported, and it is guarded: importing this module, and running --check-gpu /
    --dry-run, must work on a box WITHOUT paddle (the CPU box, CI). The gate that decides
    whether anything ships is afc_ocr.services.eval_gate, run inside run_finetune; this
    orchestrator only reads the produced bundle's eval_report.json["ship"] to decide
    whether to upload + promote.

WHY EVERYTHING IS DEFENSIVE
    This runs with nobody watching. A network blip, an empty dataset, a gate rejection,
    or a missing paddle install must all end in a clean, logged, non-zero-but-not-crashed
    exit, never a raw stack trace. Every external step is wrapped; the only thing that
    leaves this process is an exit code and a log line.

USAGE (manual smoke test on the GPU box):
    python -m afc_ocr.training.train_cycle --check-gpu          # does paddle see CUDA?
    python -m afc_ocr.training.train_cycle --force --dry-run    # full flow, skip paddle
    python -m afc_ocr.training.train_cycle                      # the real weekly run

CONFIG (env first, CLI overrides; see setup_windows_schedule.md):
    AFC_API_BASE   e.g. https://api.africanfreefirecommunity.com  (no trailing /events)
    AFC_OCR_TOKEN  a long-lived admin Bearer token (the scheduled task injects it)
    AFC_OCR_WORKDIR  where to stage datasets / bundles / logs (default: ~/.afc_ocr_train)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import logging.handlers
import os
import sys
import zipfile
from datetime import datetime, timezone

# requests is a light, always-present dep on the CPU box (used by other AFC tooling).
# It is imported at module top level on purpose: it is cheap and the whole client needs
# it. paddle is the ONLY heavy import and it stays inside run_finetune / _gpu_report.
import requests

# The off-box fine-tune driver. Importing it is paddle-free by design (the guard lives
# inside run_finetune), so this top-level import is safe on the CPU box.
from afc_ocr.training import finetune

logger = logging.getLogger("afc_ocr.train_cycle")

# ──────────────────────────────────────────────────────────────────────────────
# Defaults / conventions. Centralised so the schedule docs and the code agree on
# names, paths, and the server contract in ONE place.
# ──────────────────────────────────────────────────────────────────────────────

# Env var names the scheduled task / operator sets (see setup_windows_schedule.md).
ENV_API_BASE = "AFC_API_BASE"
ENV_TOKEN = "AFC_OCR_TOKEN"
ENV_WORKDIR = "AFC_OCR_WORKDIR"

# Default work dir under the user's home (Windows: C:\Users\<you>\.afc_ocr_train).
# Everything this loop stages (datasets, bundles, logs) lives here, never in the repo.
DEFAULT_WORKDIR = os.path.join(os.path.expanduser("~"), ".afc_ocr_train")

# Server endpoint paths, relative to <AFC_API_BASE>. The OCR urls mount under events/
# (afc/urls.py: path("events/", include('afc_ocr.urls'))), and the retrain endpoints
# live under events/ocr/ per the server contract.
EP_RETRAIN_STATUS = "/events/ocr/retrain-status/"
EP_DATASET_EXPORT = "/events/ocr/dataset-export/"
EP_UPLOAD_MODEL = "/events/ocr/upload-model/"

# Network timeouts (seconds). A small connect timeout fails fast when the API is down
# (the common unattended case); a generous read timeout tolerates a large dataset ZIP.
CONNECT_TIMEOUT = 10
READ_TIMEOUT_STATUS = 30          # tiny JSON
READ_TIMEOUT_EXPORT = 600         # the dataset ZIP can be large (crops/)
READ_TIMEOUT_UPLOAD = 600         # the bundle upload

# Process exit codes — meaningful so the Task Scheduler "last run result" is auditable.
EXIT_OK = 0                       # ran cleanly (trained+shipped, OR cleanly did nothing)
EXIT_CONFIG = 2                   # bad / missing configuration (no token, no API base)
EXIT_NETWORK = 3                  # a server call failed (down, 4xx/5xx, timeout)
EXIT_NO_DATA = 4                  # export returned an empty dataset -> nothing to train
EXIT_TRAIN = 5                    # the fine-tune itself failed (incl. missing paddle)
EXIT_GATE_REJECT = 0             # gate said "do not ship" -> that is a SUCCESSFUL no-op
EXIT_UPLOAD = 6                   # producing/uploading the bundle failed


# ──────────────────────────────────────────────────────────────────────────────
# Logging — a rotating file under the work dir + a console echo. Set up once, at the
# start of a run, so an unattended run leaves a durable, bounded trail on disk.
# ──────────────────────────────────────────────────────────────────────────────

def _setup_logging(workdir: str, verbose: bool = True) -> str:
    """
    Configure the module logger to write to <workdir>/logs/train_cycle.log (rotating,
    5 files x 2 MB) AND echo to the console. Returns the log file path.

    Rotating so an unattended weekly job can run for months without filling the disk:
    each file caps at 2 MB and we keep 5 generations (~10 MB ceiling). The console echo
    is what a manual `--dry-run` smoke test reads; the file is what you read after an
    unattended run to see what happened last Sunday at 03:00.
    """
    logs_dir = os.path.join(workdir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "train_cycle.log")

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Idempotent: clear any handlers from a previous in-process call (e.g. tests) so we
    # do not double-log when train_cycle is invoked more than once in one interpreter.
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_h = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    logger.addHandler(file_h)

    console_h = logging.StreamHandler(stream=sys.stdout)
    console_h.setFormatter(fmt)
    logger.addHandler(console_h)

    # Do not propagate to the root logger (avoids double lines if the host app configured
    # logging too — e.g. when this is invoked from within a Django process).
    logger.propagate = False
    return log_path


# ──────────────────────────────────────────────────────────────────────────────
# Config resolution — env first, CLI overrides. Surfaced loudly so a misconfigured
# scheduled task fails with a clear message instead of a confusing 401 later.
# ──────────────────────────────────────────────────────────────────────────────

class Config:
    """Resolved run configuration. Built by resolve_config(); see its docstring."""

    def __init__(self, api_base: str, token: str, workdir: str,
                 force: bool, dry_run: bool, promote: bool):
        self.api_base = api_base
        self.token = token
        self.workdir = workdir
        self.force = force
        self.dry_run = dry_run
        self.promote = promote

    def auth_headers(self) -> dict:
        """The Bearer header every server call carries (matches afc_ocr.views._auth:
        `Authorization: Bearer <token>`)."""
        return {"Authorization": f"Bearer {self.token}"}

    def url(self, path: str) -> str:
        """Join api_base + an endpoint path, tolerating a trailing slash on api_base."""
        return self.api_base.rstrip("/") + path


def resolve_config(args) -> Config:
    """
    Build a Config from env vars, with CLI flags overriding. Token/api-base come from the
    environment (the scheduled task injects them) so a long-lived admin token is never
    written into a CLI string a process list could leak.

    Raises ValueError with an actionable message if a required value is missing (the
    caller converts that into EXIT_CONFIG so the scheduler records a clear failure).
    """
    api_base = args.api_base or os.environ.get(ENV_API_BASE)
    token = args.token or os.environ.get(ENV_TOKEN)
    workdir = args.workdir or os.environ.get(ENV_WORKDIR) or DEFAULT_WORKDIR

    # --check-gpu and --dry-run still want a workdir for logs, but a real run needs the
    # server config. We validate server config in the run flow, not here, so --check-gpu
    # can run with no token at all.
    return Config(
        api_base=api_base or "",
        token=token or "",
        workdir=workdir,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
        promote=not bool(args.no_promote),
    )


def _require_server_config(cfg: Config) -> None:
    """Hard-check that a REAL run (status/export/upload) has the server config it needs.
    Raises ValueError (-> EXIT_CONFIG) with the exact env var to set."""
    missing = []
    if not cfg.api_base:
        missing.append(f"{ENV_API_BASE} (e.g. https://api.africanfreefirecommunity.com)")
    if not cfg.token:
        missing.append(f"{ENV_TOKEN} (a long-lived admin Bearer token)")
    if missing:
        raise ValueError(
            "Missing required configuration: " + "; ".join(missing) +
            ". Set them as environment variables (see setup_windows_schedule.md) or pass "
            "--api-base / --token."
        )


# ──────────────────────────────────────────────────────────────────────────────
# GPU sanity (--check-gpu) — guards the paddle import so a box without paddle gets a
# clear message, not an ImportError traceback.
# ──────────────────────────────────────────────────────────────────────────────

def _gpu_report() -> dict:
    """
    Probe whether PaddlePaddle is installed and whether it sees the CUDA GPU. This is the
    ONE place (besides finetune.run_finetune) that touches paddle, and the import is
    guarded so the absence of paddle is reported as data, not raised as an error.

    Returns a dict:
        {
          "paddle_installed": bool,
          "cuda_compiled":    bool | None,   # paddle built with CUDA support
          "device_count":     int  | None,   # how many CUDA devices paddle sees
          "device_name":      str  | None,   # name of GPU 0 (e.g. "NVIDIA GeForce RTX 3060")
          "message":          str,           # human-readable summary
        }
    On a CPU box (no paddle) this returns paddle_installed=False with an actionable
    "install paddlepaddle-gpu ..." message and never raises.
    """
    report = {
        "paddle_installed": False,
        "cuda_compiled": None,
        "device_count": None,
        "device_name": None,
        "message": "",
    }
    try:
        import paddle  # noqa: F401  (heavy GPU dep; only present on the training box)
    except Exception as exc:
        # Expected on the CPU box / CI. Tell the operator exactly how to fix it.
        report["message"] = (
            "paddle not installed. The fine-tune runs on the local GPU and needs "
            "paddlepaddle-gpu. Install it on this PC (RTX 3060, CUDA build) per "
            "setup_windows_schedule.md, e.g.: "
            "pip install paddlepaddle-gpu==3.0.0 -i "
            "https://www.paddlepaddle.org.cn/packages/stable/cu126/  then  "
            "pip install paddleocr paddle2onnx. "
            f"(import error: {exc})"
        )
        return report

    report["paddle_installed"] = True
    try:
        report["cuda_compiled"] = bool(paddle.device.is_compiled_with_cuda())
        report["device_count"] = int(paddle.device.cuda.device_count())
        if report["device_count"] and report["device_count"] > 0:
            # device.cuda.get_device_name(0) -> e.g. "NVIDIA GeForce RTX 3060".
            try:
                report["device_name"] = paddle.device.cuda.get_device_name(0)
            except Exception:
                # Older/newer paddle may name this differently; the count is enough to
                # confirm CUDA works, so a missing name is not fatal to the check.
                report["device_name"] = None
        if report["cuda_compiled"] and report["device_count"]:
            report["message"] = (
                f"paddle sees CUDA: {report['device_count']} device(s); "
                f"GPU0 = {report['device_name'] or 'unknown'}. Ready to train."
            )
        elif report["cuda_compiled"]:
            report["message"] = (
                "paddle is CUDA-compiled but sees 0 devices. Check the NVIDIA driver / "
                "that the RTX 3060 is visible (nvidia-smi)."
            )
        else:
            report["message"] = (
                "paddle is installed but NOT compiled with CUDA (CPU-only build). "
                "Reinstall the GPU wheel: pip uninstall paddlepaddle ; then install "
                "paddlepaddle-gpu per setup_windows_schedule.md."
            )
    except Exception as exc:
        report["message"] = f"paddle is installed but the CUDA probe failed: {exc}"
    return report


def check_gpu() -> int:
    """`--check-gpu` entry: print the GPU report and return an exit code. Returns EXIT_OK
    when paddle sees a CUDA device, EXIT_TRAIN otherwise (so a scheduled pre-flight check
    can flag a broken GPU setup), but NEVER raises."""
    rep = _gpu_report()
    logger.info("GPU check: %s", rep["message"])
    logger.info("GPU check details: %s", json.dumps(rep, default=str))
    ready = bool(rep["paddle_installed"] and rep["cuda_compiled"] and (rep["device_count"] or 0) > 0)
    return EXIT_OK if ready else EXIT_TRAIN


# ──────────────────────────────────────────────────────────────────────────────
# Step (a): GET retrain-status
# ──────────────────────────────────────────────────────────────────────────────

def fetch_retrain_status(cfg: Config) -> dict:
    """
    GET /events/ocr/retrain-status/ -> {due, new_gold_since_last, last_dataset_version,
    reason}. The server decides whether enough new gold data has accrued to retrain; this
    client just reports + obeys that decision (unless --force overrides it).

    Raises requests.RequestException on any transport/HTTP error (the run flow catches it
    and exits EXIT_NETWORK cleanly). Returns the parsed JSON dict on success.
    """
    url = cfg.url(EP_RETRAIN_STATUS)
    logger.info("Step (a): GET retrain-status -> %s", url)
    resp = requests.get(
        url, headers=cfg.auth_headers(),
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_STATUS),
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "retrain-status: due=%s new_gold_since_last=%s last_dataset_version=%s reason=%r",
        data.get("due"), data.get("new_gold_since_last"),
        data.get("last_dataset_version"), data.get("reason"),
    )
    return data


# ──────────────────────────────────────────────────────────────────────────────
# Step (b): GET dataset-export -> save + unzip
# ──────────────────────────────────────────────────────────────────────────────

def fetch_and_unzip_dataset(cfg: Config, run_dir: str) -> dict:
    """
    GET /events/ocr/dataset-export/?splits=train,eval&include_synthetic=true, stream the
    returned ZIP to <run_dir>/dataset.zip, then unzip it into <run_dir>/dataset/.

    The ZIP carries the exact assemble_rec_dataset shape (rec_gt.txt + crops/ +
    rec_keys.txt + manifest.json). After unzip we expect a train shard and an eval shard;
    the server may lay them out as dataset/dataset_train/ + dataset/dataset_eval/ (mirror
    of finetune_ppocrv5.md step 1) or flat. We resolve both layouts and return the paths.

    Returns:
        {
          "zip_path": str,
          "root": str,                 # the unzip root
          "dataset_dir": str | None,   # train shard (rec_gt.txt + rec_keys.txt + crops/)
          "eval_dir": str | None,      # eval shard
          "manifest": dict | None,     # parsed manifest.json if present
          "train_lines": int,          # how many rec_gt.txt lines in the train shard
        }

    Raises requests.RequestException on transport/HTTP error. A successful response with
    an EMPTY train shard is NOT raised here; the run flow inspects train_lines and exits
    EXIT_NO_DATA cleanly (nothing to train on is a normal, logged outcome).
    """
    params = {"splits": "train,eval", "include_synthetic": "true"}
    url = cfg.url(EP_DATASET_EXPORT)
    logger.info("Step (b): GET dataset-export -> %s ?%s", url, params)

    zip_path = os.path.join(run_dir, "dataset.zip")
    # Stream so a large crops/ payload never has to fit in memory all at once.
    with requests.get(
        url, headers=cfg.auth_headers(), params=params, stream=True,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_EXPORT),
    ) as resp:
        resp.raise_for_status()
        with open(zip_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB chunks
                if chunk:
                    fh.write(chunk)
    size = os.path.getsize(zip_path)
    logger.info("dataset-export: saved %s (%.1f MB)", zip_path, size / (1024 * 1024))

    # Unzip into run_dir/dataset/. zipfile guards against absolute paths but we still
    # validate each member stays inside the target (defence-in-depth vs zip traversal).
    root = os.path.join(run_dir, "dataset")
    os.makedirs(root, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            dest = os.path.normpath(os.path.join(root, member))
            if not dest.startswith(os.path.abspath(root) + os.sep) and dest != os.path.abspath(root):
                # A member that would escape the extraction root -> skip it, log it.
                logger.warning("dataset-export: skipping unsafe zip member %r", member)
                continue
        zf.extractall(root)
    logger.info("dataset-export: unzipped into %s", root)

    info = _resolve_dataset_layout(root)
    logger.info(
        "dataset layout: train=%s eval=%s train_lines=%d manifest=%s",
        info["dataset_dir"], info["eval_dir"], info["train_lines"],
        "present" if info["manifest"] else "absent",
    )
    info["zip_path"] = zip_path
    info["root"] = root
    return info


def _resolve_dataset_layout(root: str) -> dict:
    """
    Find the train + eval shard directories inside an unzipped export, tolerating both the
    nested layout from finetune_ppocrv5.md (dataset_train/ + dataset_eval/) and a flat one
    (rec_gt.txt directly under root, single shard). Also count the train rec_gt.txt lines
    so the caller can detect an empty dataset, and parse manifest.json if present.

    A shard dir is one that contains rec_gt.txt + rec_keys.txt. We prefer a dir whose name
    contains 'train' for the train shard and 'eval' for the eval shard; if neither nested
    dir exists but root itself is a shard, we treat root as the train shard and reuse it as
    the eval shard (a degenerate but safe fallback the gate will still run against).
    """
    def _is_shard(d: str) -> bool:
        return (os.path.isfile(os.path.join(d, finetune.REC_GT_FILENAME))
                and os.path.isfile(os.path.join(d, finetune.CHAR_DICT_FILENAME)))

    def _count_lines(d: str) -> int:
        gt = os.path.join(d, finetune.REC_GT_FILENAME)
        if not os.path.isfile(gt):
            return 0
        n = 0
        with open(gt, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n += 1
        return n

    dataset_dir = None
    eval_dir = None

    # Walk one level (and root itself) looking for shard dirs.
    candidates = [root]
    try:
        candidates += [os.path.join(root, name) for name in sorted(os.listdir(root))
                       if os.path.isdir(os.path.join(root, name))]
    except OSError:
        pass

    for d in candidates:
        if not _is_shard(d):
            continue
        base = os.path.basename(d).lower()
        if "eval" in base and eval_dir is None:
            eval_dir = d
        elif "train" in base and dataset_dir is None:
            dataset_dir = d
        elif dataset_dir is None:
            # An unlabeled shard (e.g. flat root): use it as the train shard.
            dataset_dir = d

    # Degenerate fallback: a single flat shard -> train, and reuse it as eval so the gate
    # still has something to score (the server normally ships a real eval shard).
    if dataset_dir is not None and eval_dir is None:
        eval_dir = dataset_dir

    # manifest.json may sit at root or inside a shard.
    manifest = None
    for cand in [os.path.join(root, "manifest.json")] + (
        [os.path.join(dataset_dir, "manifest.json")] if dataset_dir else []
    ):
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
                break
            except Exception as exc:
                logger.warning("dataset-export: manifest.json present but unreadable: %s", exc)

    return {
        "dataset_dir": dataset_dir,
        "eval_dir": eval_dir,
        "manifest": manifest,
        "train_lines": _count_lines(dataset_dir) if dataset_dir else 0,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Step (c)+(d): fine-tune on the local GPU + read the gate decision
# ──────────────────────────────────────────────────────────────────────────────

def _next_version(dataset_info: dict, run_dir: str) -> int:
    """
    Decide the integer student version N for this run -> bundle student_v<N>/.

    Best effort, deterministic-ish: if the export manifest carries a numeric hint use it;
    otherwise stamp the version from the UTC date (YYYYMMDD) so successive weekly runs get
    monotonically increasing, human-legible versions without needing server coordination.
    The server is the real source of truth for the canonical version (it returns one from
    upload-model); this is only the local working tag for the produced bundle dir.
    """
    manifest = dataset_info.get("manifest") or {}
    for key in ("next_version", "version", "student_version"):
        val = manifest.get(key)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    # Fall back to a date stamp, e.g. 20260608 -> student_v20260608.
    return int(datetime.now(timezone.utc).strftime("%Y%m%d"))


def run_local_finetune(cfg: Config, dataset_info: dict, run_dir: str) -> dict:
    """
    Step (c): fine-tune on the local GPU by delegating to finetune.run_finetune(), and
    step (d): read the gate decision the trainer baked into the bundle's eval_report.json.

    run_finetune is the ONLY paddle-touching call. We:
      - skip it entirely on --dry-run (return a synthetic "skipped" result),
      - catch a missing-paddle / paddle-init failure and re-raise it as a clear, actionable
        message (run_finetune already raises a RuntimeError telling the user to install
        paddlepaddle-gpu + paddleocr; we surface that to the operator and the run flow
        turns it into EXIT_TRAIN).

    Returns a dict:
        {
          "bundle_dir": str | None,   # the produced student_v<N>/ (None on dry-run)
          "ship": bool,               # gate decision (eval_report.json["ship"])
          "version": int,             # the student version N used
          "skipped": bool,            # True when --dry-run skipped the real train
          "reasons": list,            # gate reasons (for the summary log)
        }
    """
    version = _next_version(dataset_info, run_dir)

    if cfg.dry_run:
        logger.info(
            "Step (c): --dry-run -> SKIPPING the paddle fine-tune (would train "
            "student_v%d from %s, eval %s). No GPU touched.",
            version, dataset_info.get("dataset_dir"), dataset_info.get("eval_dir"),
        )
        return {
            "bundle_dir": None,
            "ship": False,           # nothing was produced -> nothing to ship
            "version": version,
            "skipped": True,
            "reasons": ["[SKIP] --dry-run: paddle fine-tune not executed"],
        }

    dataset_dir = dataset_info.get("dataset_dir")
    eval_dir = dataset_info.get("eval_dir")
    out_dir = os.path.join(run_dir, "models")
    os.makedirs(out_dir, exist_ok=True)

    # The pretrained PP-OCRv5_mobile_rec base. The operator places it under the work dir
    # (see setup_windows_schedule.md); env override for flexibility.
    pretrained = os.environ.get(
        "AFC_OCR_PRETRAINED",
        os.path.join(cfg.workdir, "pretrain_models", "PP-OCRv5_mobile_rec_pretrained.pdparams"),
    )

    logger.info(
        "Step (c): fine-tuning student_v%d on the local GPU (train=%s eval=%s pretrained=%s)",
        version, dataset_dir, eval_dir, pretrained,
    )
    try:
        # run_finetune does steps 1-5 (train -> export ONNX -> eval gate -> write_bundle)
        # and returns {"bundle_dir","ship","metrics","reasons"}. paddle is imported INSIDE
        # it, guarded; on a box without paddle it raises a RuntimeError with install help.
        result = finetune.run_finetune(
            dataset_dir=dataset_dir,
            eval_dir=eval_dir,
            pretrained=pretrained,
            out_dir=out_dir,
            version=version,
            current_metrics=None,        # server holds the live model's metrics; gate vs {}
            must_pass_results=None,
        )
    except RuntimeError as exc:
        # run_finetune raises this when paddle is missing (or the skeleton is unfilled).
        # Re-raise as the same actionable message; the run flow maps it to EXIT_TRAIN.
        logger.error("Step (c): fine-tune could not run: %s", exc)
        raise

    # Step (d): the gate already ran inside run_finetune; trust its bundle decision, but
    # ALSO re-read eval_report.json from disk as the source of truth (defensive: the
    # bundle on disk is what we would upload, so its recorded ship flag is authoritative).
    bundle_dir = result.get("bundle_dir")
    ship = bool(result.get("ship"))
    reasons = list(result.get("reasons") or [])
    if bundle_dir:
        ship, reasons = _read_ship_from_bundle(bundle_dir, fallback_ship=ship, fallback_reasons=reasons)

    logger.info("Step (d): gate decision for student_v%d -> ship=%s", version, ship)
    return {
        "bundle_dir": bundle_dir,
        "ship": ship,
        "version": version,
        "skipped": False,
        "reasons": reasons,
    }


def _read_ship_from_bundle(bundle_dir: str, fallback_ship: bool, fallback_reasons: list):
    """
    Read <bundle_dir>/eval_report.json and return (ship, reasons). The bundle on disk is
    what we upload, so its recorded ship flag is the authoritative gate decision (matches
    finetune.write_bundle, which writes {"ship": ..., "gate_reasons": [...]}). Falls back
    to the in-memory values if the file is missing/unreadable (never raises)."""
    report_path = os.path.join(bundle_dir, finetune.BUNDLE_EVAL_REPORT)
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        return bool(report.get("ship", fallback_ship)), list(report.get("gate_reasons", fallback_reasons))
    except Exception as exc:
        logger.warning("could not read %s (%s); using in-memory gate result", report_path, exc)
        return fallback_ship, fallback_reasons


# ──────────────────────────────────────────────────────────────────────────────
# Step (e): zip the bundle + POST upload-model
# ──────────────────────────────────────────────────────────────────────────────

def _zip_bundle(bundle_dir: str, run_dir: str) -> str:
    """
    Zip a student_v<N>/ bundle directory into <run_dir>/<name>.zip for upload. The ZIP
    contains the bundle dir as the top-level folder (so the server unpacks it back to
    student_v<N>/ verbatim: rec.onnx + rec_keys.txt + VERSION + model_card.json +
    eval_report.json). Returns the zip path.
    """
    name = os.path.basename(bundle_dir.rstrip(os.sep))
    zip_path = os.path.join(run_dir, f"{name}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, files in os.walk(bundle_dir):
            for fname in files:
                abs_path = os.path.join(dirpath, fname)
                # arcname keeps the bundle folder as the top-level entry in the zip.
                arc = os.path.join(name, os.path.relpath(abs_path, bundle_dir))
                zf.write(abs_path, arc)
    logger.info("Zipped bundle %s -> %s (%.1f KB)",
                bundle_dir, zip_path, os.path.getsize(zip_path) / 1024)
    return zip_path


def upload_bundle(cfg: Config, bundle_dir: str, run_dir: str) -> dict:
    """
    Step (e): zip the produced bundle and POST it to /events/ocr/upload-model/ (multipart
    file=<bundle.zip>, ?promote=true unless --no-promote). The server stores it and, when
    promote=true AND its own gate re-check agrees, flips the live pointer
    (afc_ocr.services.model_registry.promote).

    Returns the parsed server JSON {version, stored, promoted, gate_passed}. Raises
    requests.RequestException on transport/HTTP error (run flow -> EXIT_UPLOAD).
    """
    zip_path = _zip_bundle(bundle_dir, run_dir)
    url = cfg.url(EP_UPLOAD_MODEL)
    params = {"promote": "true" if cfg.promote else "false"}
    logger.info("Step (e): POST upload-model -> %s ?%s (file=%s)",
                url, params, os.path.basename(zip_path))

    with open(zip_path, "rb") as fh:
        files = {"file": (os.path.basename(zip_path), fh, "application/zip")}
        resp = requests.post(
            url, headers=cfg.auth_headers(), params=params, files=files,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_UPLOAD),
        )
    resp.raise_for_status()
    data = resp.json()
    logger.info(
        "upload-model: version=%s stored=%s promoted=%s gate_passed=%s",
        data.get("version"), data.get("stored"),
        data.get("promoted"), data.get("gate_passed"),
    )
    return data


# ──────────────────────────────────────────────────────────────────────────────
# The orchestrator — wires (a)..(f) together with defensive handling at each step.
# ──────────────────────────────────────────────────────────────────────────────

def run_cycle(cfg: Config) -> int:
    """
    Run one full training cycle. Returns a process exit code (EXIT_*). NEVER raises: every
    external step is wrapped so an unattended run ends in a clean, logged exit instead of
    a stack trace. The flow is exactly (a)..(f) from the module docstring.

    A new timestamped run dir under <workdir>/runs/ isolates this run's dataset + bundle
    so concurrent or successive runs never stomp each other.
    """
    started = datetime.now(timezone.utc)
    run_dir = os.path.join(cfg.workdir, "runs", started.strftime("run_%Y%m%dT%H%M%SZ"))
    os.makedirs(run_dir, exist_ok=True)
    logger.info("=== train_cycle START === run_dir=%s dry_run=%s force=%s promote=%s",
                run_dir, cfg.dry_run, cfg.force, cfg.promote)

    # Validate server config up front so a misconfigured task fails fast + clearly.
    try:
        _require_server_config(cfg)
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="config-error")
        return EXIT_CONFIG

    # ── (a) retrain-status ─────────────────────────────────────────────────────
    try:
        status = fetch_retrain_status(cfg)
    except requests.RequestException as exc:
        logger.error("retrain-status failed (network/HTTP): %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="network-error")
        return EXIT_NETWORK
    except ValueError as exc:  # bad JSON body
        logger.error("retrain-status returned an unparseable body: %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="network-error")
        return EXIT_NETWORK

    due = bool(status.get("due"))
    if not due and not cfg.force:
        logger.info("Not due (reason=%r) and --force not set -> nothing to do. Clean exit.",
                    status.get("reason"))
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="not-due")
        return EXIT_OK
    if not due and cfg.force:
        logger.info("Not due, but --force set -> proceeding with the cycle anyway.")

    # ── (b) dataset-export ─────────────────────────────────────────────────────
    try:
        dataset_info = fetch_and_unzip_dataset(cfg, run_dir)
    except requests.RequestException as exc:
        logger.error("dataset-export failed (network/HTTP): %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="network-error")
        return EXIT_NETWORK
    except (zipfile.BadZipFile, OSError) as exc:
        logger.error("dataset-export downloaded but could not be unpacked: %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="bad-dataset")
        return EXIT_NO_DATA

    if dataset_info["train_lines"] <= 0:
        logger.warning("Exported dataset has 0 training lines -> nothing to train on. "
                       "Clean exit (this is a normal no-op, not an error).")
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="empty-dataset")
        return EXIT_NO_DATA

    # ── (c)+(d) fine-tune + gate ───────────────────────────────────────────────
    try:
        train_result = run_local_finetune(cfg, dataset_info, run_dir)
    except RuntimeError as exc:
        # Missing paddle, or the off-box skeleton not yet filled. Clear, actionable, no
        # traceback to the scheduler — just a logged failure + EXIT_TRAIN.
        logger.error("Fine-tune step failed: %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="train-failed")
        return EXIT_TRAIN
    except Exception as exc:  # defensive: never let a training crash escape unlogged
        logger.exception("Unexpected error during fine-tune: %s", exc)
        _summary(started, trained=None, gate=None, uploaded=False, promoted=False,
                 outcome="train-failed")
        return EXIT_TRAIN

    version = train_result["version"]

    # --dry-run stops here: the flow proved status -> export -> (skip train) without paddle.
    if train_result["skipped"]:
        logger.info("--dry-run complete: status -> export -> (skipped train) flowed "
                    "cleanly. No bundle produced, nothing uploaded.")
        _summary(started, trained=version, gate="skipped", uploaded=False, promoted=False,
                 outcome="dry-run")
        return EXIT_OK

    # The gate (run inside run_finetune) says whether the new model is a real, regression-
    # free win. A reject is a SUCCESSFUL outcome: we correctly declined to ship a non-win.
    if not train_result["ship"]:
        logger.info("Gate REJECTED student_v%d (no regression-free improvement). "
                    "Not uploading. Reasons: %s", version, " | ".join(train_result["reasons"]))
        _summary(started, trained=version, gate="reject", uploaded=False, promoted=False,
                 outcome="gate-reject")
        return EXIT_GATE_REJECT

    # ── (e) upload + promote ───────────────────────────────────────────────────
    try:
        upload = upload_bundle(cfg, train_result["bundle_dir"], run_dir)
    except requests.RequestException as exc:
        logger.error("upload-model failed (network/HTTP): %s", exc)
        _summary(started, trained=version, gate="ship", uploaded=False, promoted=False,
                 outcome="upload-error")
        return EXIT_UPLOAD
    except (OSError, ValueError) as exc:
        logger.error("upload-model: could not zip/send the bundle or parse the reply: %s", exc)
        _summary(started, trained=version, gate="ship", uploaded=False, promoted=False,
                 outcome="upload-error")
        return EXIT_UPLOAD

    promoted = bool(upload.get("promoted"))
    _summary(started, trained=version, gate="ship",
             uploaded=bool(upload.get("stored")), promoted=promoted,
             outcome="shipped", server_version=upload.get("version"))
    return EXIT_OK


def _summary(started, trained, gate, uploaded, promoted, outcome, server_version=None):
    """
    Emit the single, clear END-OF-RUN summary line the operator (or the scheduler log)
    reads to know what happened, plus the elapsed time. One line, every code path, so an
    unattended run always closes with a definitive verdict.
        trained: the student version N (or None if no train happened)
        gate:    "ship" | "reject" | "skipped" | None
        outcome: a short tag (not-due, empty-dataset, gate-reject, shipped, ...)
    """
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info(
        "=== train_cycle SUMMARY === outcome=%s trained=%s gate=%s uploaded=%s "
        "promoted=%s server_version=%s elapsed=%.1fs",
        outcome,
        f"student_v{trained}" if trained is not None else "none",
        gate if gate is not None else "n/a",
        uploaded, promoted,
        server_version if server_version is not None else "n/a",
        elapsed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI for `python -m afc_ocr.training.train_cycle`. Defined separately so it can be
    imported in tests without running anything."""
    p = argparse.ArgumentParser(
        prog="afc_ocr.training.train_cycle",
        description=(
            "Automated local-GPU training cycle for the AFC self-hosted OCR student "
            "model. Pulls new training data from the AFC server, fine-tunes locally on "
            "the GPU, and pushes the improved model back. See setup_windows_schedule.md."
        ),
    )
    p.add_argument("--api-base", default=None,
                   help=f"AFC API base URL (overrides ${ENV_API_BASE}), "
                        "e.g. https://api.africanfreefirecommunity.com")
    p.add_argument("--token", default=None,
                   help=f"Admin Bearer token (overrides ${ENV_TOKEN}). Prefer the env var "
                        "so the token is not visible in the process list.")
    p.add_argument("--workdir", default=None,
                   help=f"Work dir for datasets/bundles/logs (overrides ${ENV_WORKDIR}, "
                        f"default {DEFAULT_WORKDIR}).")
    p.add_argument("--force", action="store_true",
                   help="Train even if retrain-status says not due.")
    p.add_argument("--dry-run", action="store_true",
                   help="Run status -> export -> (SKIP the paddle fine-tune) -> stop. "
                        "Proves the loop without touching the GPU or paddle.")
    p.add_argument("--no-promote", action="store_true",
                   help="Upload the bundle but do NOT ask the server to promote it "
                        "(upload with ?promote=false).")
    p.add_argument("--check-gpu", action="store_true",
                   help="Report whether paddle sees CUDA + the device name, then exit. "
                        "Guards the paddle import (clean message if paddle is absent).")
    return p


def main(argv=None) -> int:
    """
    Entry point. Resolves config, sets up logging, dispatches to --check-gpu or the full
    cycle, and returns a process exit code. Wraps the very top so even a setup failure
    exits cleanly with EXIT_CONFIG rather than a traceback.
    """
    args = _build_arg_parser().parse_args(argv)
    cfg = resolve_config(args)

    # Logging needs a workdir; create it early so even --check-gpu leaves a log trail.
    try:
        log_path = _setup_logging(cfg.workdir)
        logger.info("train_cycle invoked (log -> %s)", log_path)
    except OSError as exc:
        # Could not create the work dir / log file -> fall back to basic console logging
        # so the operator still sees WHY it could not start.
        logging.basicConfig(level=logging.INFO)
        logger.error("Could not set up logging in workdir %r: %s", cfg.workdir, exc)
        return EXIT_CONFIG

    # --check-gpu is a standalone pre-flight: no server config needed.
    if args.check_gpu:
        return check_gpu()

    return run_cycle(cfg)


if __name__ == "__main__":
    # Return the cycle's exit code to the OS so Task Scheduler records the real result.
    sys.exit(main())
