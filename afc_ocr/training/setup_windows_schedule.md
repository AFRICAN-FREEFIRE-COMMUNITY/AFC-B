# Hands-off weekly OCR training on your Windows PC (RTX 3060)

This is the operator setup for the automated local-GPU training cycle
(`afc_ocr.training.train_cycle`). Once configured it runs **unattended, weekly**, on
your PC: it asks the AFC server whether a retrain is due, pulls the new training data,
fine-tunes the OCR student model **locally on your RTX 3060 (offline)**, runs the eval
gate, and pushes the improved model back. Nobody needs to be at the keyboard.

The pieces:

| File | What it is |
|---|---|
| `train_cycle.py` | The orchestrator. Runnable as `python -m afc_ocr.training.train_cycle`. |
| `finetune.py` | The off-box fine-tune driver it calls (the GPU step). Needs paddle. |
| `afc_ocr_train_cycle.xml` | The Windows Task Scheduler task definition (weekly Sun 03:00). |
| `register_schedule.ps1` | Registers that task for you (fills machine paths). |

What the loop does each run, step by step:

1. `GET /events/ocr/retrain-status/` , if not due (and you did not pass `--force`), it logs and exits cleanly.
2. `GET /events/ocr/dataset-export/?splits=train,eval&include_synthetic=true` , downloads + unzips the dataset bundle.
3. Fine-tunes the student on your GPU via `finetune.run_finetune(...)` (this is the part that needs paddle).
4. The eval gate (inside the trainer) decides `ship` / `reject`. A reject is a normal, successful no-op (nothing worse than production ever ships).
5. If `ship`, it `POST`s the new bundle to `/events/ocr/upload-model/?promote=true`.
6. It writes a rotating log under the work dir and prints a one-line summary.

---

## 1. Configure the two environment variables

The loop reads its server config from **USER environment variables** so your admin token
is never written into a file or a command line a process list could leak.

| Variable | Value |
|---|---|
| `AFC_API_BASE` | The AFC API base URL, e.g. `https://api.africanfreefirecommunity.com` (local: `http://localhost:8000`). No trailing `/events`. |
| `AFC_OCR_TOKEN` | A long-lived **admin** Bearer token (a `SessionToken` for an account with admin / `event_admin` / `head_admin` rights, since the endpoints are admin-gated). |
| `AFC_OCR_WORKDIR` | (optional) Where datasets / bundles / logs are staged. Default: `C:\Users\<you>\.afc_ocr_train`. |

Set them persistently (PowerShell, current user , no admin needed):

```powershell
setx AFC_API_BASE  "https://api.africanfreefirecommunity.com"
setx AFC_OCR_TOKEN "<paste-the-admin-bearer-token-here>"
```

`setx` writes the USER environment permanently. **Open a new terminal** afterward so the
values are visible (the current shell keeps the old environment). Verify:

```powershell
$env:AFC_API_BASE
# (do not echo the token into shared logs)
```

The token is the same kind the frontend stores in the `auth_token` cookie. Generate one
for a dedicated admin "trainer" account so you can rotate it without disturbing humans.
The endpoints validate it exactly like `afc_ocr.views._auth` does:
`Authorization: Bearer <token>`.

---

## 2. Install paddlepaddle-gpu + paddleocr + paddle2onnx for the RTX 3060

The fine-tune (step 3) runs on the GPU and needs PaddlePaddle's **CUDA** build. The
RTX 3060 is an Ampere card; your NVIDIA driver **581.95** is recent enough to support the
current CUDA 12.x runtime that the CUDA 12.6 Paddle wheels target (the CUDA runtime ships
inside the wheel , you do **not** need a separate CUDA Toolkit install, only a recent
driver, which you have).

Install into a SEPARATE training venv (`.venv-train`), NOT the serving venv (`.venv`).
Paddle pins an older numpy that would break onnxruntime + opencv in the serving env, so we
keep the GPU training stack isolated. Create it once, then install Paddle into it:

```powershell
# from the backend dir. Create the separate training venv (one time):
.\.venv\Scripts\python.exe -m venv .venv-train

# 1. paddlepaddle-gpu, CUDA 12.6 build, from the official Paddle package index:
.\.venv-train\Scripts\python.exe -m pip install paddlepaddle-gpu==3.0.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

# 2. PaddleOCR (the recognizer training code) + paddle2onnx (the ONNX exporter) + requests
#    (train_cycle.py talks to the AFC server over HTTP):
.\.venv-train\Scripts\python.exe -m pip install paddleocr paddle2onnx requests
```

Notes:
- `-i https://www.paddlepaddle.org.cn/packages/stable/cu126/` is the Paddle stable index
  for the **CUDA 12.6** wheels. If a newer point release than `3.0.0` is published, drop
  the `==3.0.0` pin to take the latest CUDA 12.6 build, or change `cu126` to the matching
  CUDA tag the Paddle install page lists for your chosen version (e.g. `cu118` for the
  CUDA 11.8 line). Confirm the exact current wheel on
  `https://www.paddlepaddle.org.cn/en/install/quick` , the index URL pattern above is the
  stable interface, the version tag is the one thing to verify against the live page.
- These are large GPU wheels (hundreds of MB). Install once; the weekly task reuses them.

### Verify paddle sees the GPU

```powershell
.\.venv-train\Scripts\python.exe -c "import paddle; print(paddle.device.is_compiled_with_cuda(), paddle.device.cuda.device_count())"
```

Expected on a working setup: `True 1`. (`True` = the wheel is CUDA-compiled; `1` = paddle
sees your one RTX 3060.) If you get `False`, you installed the CPU wheel , uninstall and
reinstall the `-gpu` wheel from the `cu126` index above. If you get `True 0`, paddle is
CUDA-compiled but cannot see the card , check `nvidia-smi` and the driver.

The orchestrator wraps the same probe in a friendly form:

```powershell
.\.venv-train\Scripts\python.exe -m afc_ocr.training.train_cycle --check-gpu
```

It prints a clear "ready to train" line when paddle sees CUDA, or an actionable
"paddle not installed / install paddlepaddle-gpu ..." message when it does not , and it
**never crashes** if paddle is absent.

---

## 3. Test the cycle before scheduling it

Run the full flow but **skip the actual GPU train** with `--dry-run` (and `--force` so it
proceeds even when the server says "not due"):

```powershell
.\.venv-train\Scripts\python.exe -m afc_ocr.training.train_cycle --force --dry-run
```

This exercises: `retrain-status` , `dataset-export` (download + unzip) , **(skips the
paddle fine-tune)** , stops. It does **not** touch the GPU or paddle, so it is safe to run
before paddle is even installed (as long as `AFC_API_BASE` + `AFC_OCR_TOKEN` are set and
the API is reachable). You should see a `SUMMARY` line ending in `outcome=dry-run`.

Useful flags:

| Flag | Effect |
|---|---|
| `--force` | Train even if `retrain-status` says not due. |
| `--dry-run` | Run status , export , **skip** the paddle train , stop. |
| `--no-promote` | Upload the bundle but do **not** ask the server to promote it. |
| `--check-gpu` | Just report whether paddle sees CUDA, then exit. |
| `--api-base` / `--token` / `--workdir` | Override the env vars for a one-off run. |

Exit codes (the Task Scheduler "last run result" reflects these): `0` clean (trained+
shipped, or cleanly did nothing , not due / gate-reject / empty dataset), `2` config,
`3` network, `4` no data, `5` train (incl. missing paddle), `6` upload.

Logs land at `%AFC_OCR_WORKDIR%\logs\train_cycle.log` (rotating, ~10 MB ceiling). Each
run stages its dataset + bundle under `%AFC_OCR_WORKDIR%\runs\run_<timestamp>\`.

---

## 4. Register the weekly scheduled task

```powershell
cd <repo>\backend\afc_ocr\training
.\register_schedule.ps1
```

The script resolves your venv python + the backend dir, fills the placeholders in
`afc_ocr_train_cycle.xml`, and registers the task under `\AFC\afc_ocr_train_cycle`. Pass
`-PythonExe` / `-WorkingDir` if your paths differ from the defaults.

If your machine policy blocks running unsigned scripts, allow it for this one process:

```powershell
powershell -ExecutionPolicy Bypass -File .\register_schedule.ps1
```

### What the schedule does (cadence + missed-start behaviour)

| Setting | Value | Why |
|---|---|---|
| Trigger | **Weekly, every Sunday 03:00** local | Off-peak; once a week is plenty for this loop. |
| Run after a missed start | **Yes** (`StartWhenAvailable=true`) | If the PC was **off** at 03:00 Sunday, it runs the **next time the PC is on**. |
| Network required | **Yes** (`RunOnlyIfNetworkAvailable=true`) | It must reach the AFC API to pull data + push the model. |
| Wake the PC to run | **No** (`WakeToRun=false`) | It does **not** wake the machine from sleep; it waits until the PC is awake (paired with run-after-missed-start so the run still happens on next wake/boot). |
| If already running | Ignore the new start | One training run at a time. |
| Time limit | 6 hours, then stopped | Safety cap on a stuck run. |

Verify it registered:

```powershell
schtasks /Query /TN "\AFC\afc_ocr_train_cycle" /V /FO LIST
```

Run it once by hand (it will report "not due" unless new gold data has accrued):

```powershell
Start-ScheduledTask -TaskName "afc_ocr_train_cycle" -TaskPath "\AFC\"
```

Remove it later:

```powershell
Unregister-ScheduledTask -TaskName "afc_ocr_train_cycle" -TaskPath "\AFC\" -Confirm:$false
```

---

## 5. What "good" looks like after it is wired

- `--check-gpu` prints `paddle sees CUDA: 1 device(s); GPU0 = NVIDIA GeForce RTX 3060. Ready to train.`
- The weekly task's "Last Run Result" in Task Scheduler is `0x0` on a clean run.
- After a run that shipped, `train_cycle.log` ends with `outcome=shipped ... promoted=True`.
- After a run where the model did not improve, it ends with `outcome=gate-reject` (exit 0)
  , that is correct: the gate refused to ship a non-improvement, which is the whole point.

The model only ever changes in production when the server-side gate **also** agrees on
upload (`promote=true`), so a bad local run can never degrade live OCR.
