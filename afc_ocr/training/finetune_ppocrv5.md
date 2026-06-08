# Fine-tuning the AFC OCR student (PP-OCRv5_mobile_rec), off-box on a GPU

This is the step-by-step the operator runs to produce a new student recognizer for the
AFC self-hosted OCR. The fine-tune runs **off-box on a GPU** (Colab, Kaggle, or a spot
`g4dn`), never on the Django box (no GPU there). The CPU box only does two things:
assemble the dataset (`afc_ocr.services.dataset`) and run the eval gate
(`afc_ocr.services.eval_gate`).

**Everything produced here stays local / gitignored.** The dataset (`media/ocr_training/`),
the trained weights, and the student bundle are never committed and never pushed. The
off-box box reads from a **read-only prod-DB clone** (see the project prod-DB-clone memory
note) or from an exported tarball; it never writes to prod.

> Accuracy note: PaddleOCR's exact config filenames and a couple of the v3.x export flags
> drift between point releases. The config keys, the `tools/train.py` /
> `tools/export_model.py` invocations, and the standalone `paddle2onnx` command below are
> the documented, stable interface; where a value depends on your installed version (most
> notably the inference model filename, `inference.json` in newer v3.x vs
> `inference.pdmodel` / `inference.pdiparams` in older builds) it is called out inline.
> Verify those one or two spots against the version you actually `pip install`.

Sources for the commands below: PaddleOCR text-recognition module docs
(`paddleocr.ai/main/en/version3.x/module_usage/text_recognition.html`), the PP-OCRv5
fine-tuning write-up at `timc.me/blog/finetune-paddleocr-text-recognition.html`, and the
PaddleOCR `deploy/paddle2onnx` readme.

---

## 0. Pieces and where they live

| Thing | Produced by | Path |
|---|---|---|
| Train dataset (`rec_gt.txt` + `crops/` + `rec_keys.txt`) | `assemble_rec_dataset(splits=('train',))` | `media/ocr_training/dataset_train/` |
| Frozen gold eval set | `assemble_rec_dataset(splits=('eval',))` | `media/ocr_training/dataset_eval/` |
| Frozen manifest (audit trail) | `freeze_manifest(version, pairs)` | `media/ocr_training/manifests/manifest_<v>.jsonl` |
| Trained student bundle | this procedure (`finetune.py` + paddle) | `media/models/student_v<N>/` |
| The driver | `afc_ocr/training/finetune.py` | (this repo) |

The bundle prod loads is exactly `{rec.onnx, rec_keys.txt, VERSION}` plus
`model_card.json` + `eval_report.json` for provenance. `local_ocr.LocalOCREngine` reads
`<model_dir>/rec.onnx`, `<model_dir>/rec_keys.txt`, `<model_dir>/VERSION`.

---

## 1. On the CPU box: assemble + freeze the dataset, then export a tarball

Run inside the backend venv (`backend/.venv/Scripts/python.exe`). Use the read-only prod
clone as the DB so you train on real captured data (never write to prod).

```python
# python manage.py shell
import os
from afc_ocr.services import dataset

MEDIA = "media/ocr_training"

# Train shard: gold + silver + synthetic, train split only (the assembler enforces that
# silver/synthetic can only land in train).
train = dataset.assemble_rec_dataset(
    out_dir=os.path.join(MEDIA, "dataset_train"),
    splits=("train",),
    include_synthetic=True,
)

# Frozen gold EVAL shard: eval split only -> gold (admin_review) only, by construction.
ev = dataset.assemble_rec_dataset(
    out_dir=os.path.join(MEDIA, "dataset_eval"),
    splits=("eval",),
    include_synthetic=False,
)

# Freeze the immutable manifest for the train shard so this model is traceable to its data.
from afc_ocr.models import OCRTrainingPair
pairs = OCRTrainingPair.objects.filter(pair_id__in=train["pair_ids"])
dataset.freeze_manifest(train["dataset_version"], pairs)

print("train:", train["counts"], "version:", train["dataset_version"])
print("eval :", ev["counts"])
```

Both `dataset_train/` and `dataset_eval/` now contain `rec_gt.txt`, `rec_keys.txt`
(the **custom AFC char dictionary**), and a `crops/` folder. Tar them up for the GPU box:

```bash
tar czf afc_ocr_dataset.tgz -C media/ocr_training dataset_train dataset_eval
```

`rec_keys.txt` is the single source of truth for the alphabet. The **same file** must be
passed to training (`Global.character_dict_path`) and shipped in the bundle
(`rec.onnx` is built against it). Never regenerate the dict separately on the GPU box.

---

## 2. On the GPU box: environment

```bash
# CUDA wheel matched to the box; verify the exact wheel name for your CUDA version.
pip install paddlepaddle-gpu paddleocr paddle2onnx
git clone https://github.com/PaddlePaddle/PaddleOCR.git
cd PaddleOCR
# unpack the dataset tarball next to the repo
tar xzf /path/to/afc_ocr_dataset.tgz -C ./
```

Download the pretrained PP-OCRv5_mobile_rec weights (the base we fine-tune from). Get the
`PP-OCRv5_mobile_rec` pretrained `.pdparams` from the PaddleOCR model list / Hugging Face
(`PaddlePaddle/PP-OCRv5_mobile_rec`) and place it at
`./pretrain_models/PP-OCRv5_mobile_rec_pretrained.pdparams`.

---

## 3. Patch the recognition config (the exact knobs)

Copy the stock mobile-rec config as the base and edit these keys. The PP-OCRv5 configs live
under `configs/rec/PP-OCRv5/` (e.g. `PP-OCRv5_mobile_rec.yml`; the docs reference
`PP-OCRv5_server_rec.yml` for the server variant). Edit the YAML so:

```yaml
Global:
  use_gpu: true
  epoch_num: 100                       # tune to your data size; start 50-200
  pretrained_model: ./pretrain_models/PP-OCRv5_mobile_rec_pretrained.pdparams
  # THE CUSTOM AFC DICTIONARY produced by assemble_rec_dataset. Single source of truth.
  character_dict_path: ./dataset_train/rec_keys.txt
  # Free Fire player names contain spaces -> the model must be able to emit a space.
  # use_space_char=True makes PaddleOCR treat space as an emittable token (handled via
  # this flag, NOT as a line in rec_keys.txt — which is why the assembler strips the
  # literal space from the dict it writes).
  use_space_char: true
  save_model_dir: ./output/afc_student/

Train:
  dataset:
    data_dir: ./dataset_train/          # rec_gt.txt paths are relative to here (crops/...)
    label_file_list:
      - ./dataset_train/rec_gt.txt

Eval:
  dataset:
    data_dir: ./dataset_eval/
    label_file_list:
      - ./dataset_eval/rec_gt.txt
```

Key names, verbatim, that matter and are easy to get wrong:
- `Global.character_dict_path` — point at the **custom** `rec_keys.txt`, not the stock
  `ppocrv5_dict.txt`. If this is wrong the ONNX vocab will not match what AFC reads.
- `Global.use_space_char: true` — required because names have spaces.
- `Global.pretrained_model` — the PP-OCRv5_mobile_rec base, so we fine-tune rather than
  train from scratch.
- `Train.dataset.label_file_list` / `Eval.dataset.label_file_list` — YAML **lists**; the
  paths are relative to the corresponding `data_dir`.

These can also be passed as `-o Key=Value` overrides on the train command instead of
editing the YAML (see below), which is what `finetune.py`'s skeleton documents.

---

## 4. Train

```bash
python3 tools/train.py -c configs/rec/PP-OCRv5/PP-OCRv5_mobile_rec.yml \
  -o Global.pretrained_model=./pretrain_models/PP-OCRv5_mobile_rec_pretrained.pdparams \
     Global.character_dict_path=./dataset_train/rec_keys.txt \
     Global.use_space_char=True \
     Global.epoch_num=100 \
     Train.dataset.data_dir=./dataset_train/ \
     Train.dataset.label_file_list=["./dataset_train/rec_gt.txt"] \
     Eval.dataset.data_dir=./dataset_eval/ \
     Eval.dataset.label_file_list=["./dataset_eval/rec_gt.txt"]
```

The best checkpoint lands at `./output/afc_student/best_accuracy.pdparams` (PaddleOCR
keeps the best-on-eval checkpoint). Watch the eval accuracy logged each epoch; stop when
it plateaus.

---

## 5. Export: checkpoint -> inference model -> ONNX

**5a. checkpoint -> static inference model:**

```bash
python3 tools/export_model.py -c configs/rec/PP-OCRv5/PP-OCRv5_mobile_rec.yml \
  -o Global.pretrained_model=./output/afc_student/best_accuracy.pdparams \
     Global.character_dict_path=./dataset_train/rec_keys.txt \
     Global.use_space_char=True \
     Global.save_inference_dir=./output/afc_student_infer/
```

This writes the inference model into `./output/afc_student_infer/`. **Version caveat:**
newer PaddleOCR v3.x emits `inference.json` + `inference.pdiparams`; older builds emit
`inference.pdmodel` + `inference.pdiparams`. Check which files actually appear and use
their names in 5b.

**5b. inference model -> ONNX (paddle2onnx):**

```bash
paddle2onnx \
  --model_dir ./output/afc_student_infer/ \
  --model_filename inference.pdmodel \
  --params_filename inference.pdiparams \
  --save_file ./output/afc_student_infer/rec.onnx \
  --opset_version 11 \
  --enable_onnx_checker True
```

Notes:
- If your export produced `inference.json` (newer v3.x), pass
  `--model_filename inference.json` instead. Some recent PaddleOCR builds wrap this as
  `paddlex --paddle2onnx ...`; the standalone `paddle2onnx` above is the stable path. Use
  whichever your installed version provides — confirm with `paddle2onnx --help`.
- `--opset_version 11` is the safe default for onnxruntime; paddle2onnx >= 1.2.3 supports
  dynamic shapes by default (the old `--input_shape_dict` is deprecated), so the exported
  `rec.onnx` accepts variable-width crops, which is what `rapidocr_onnxruntime` feeds it.

You now have `rec.onnx` built against the custom AFC `rec_keys.txt`.

---

## 6. Eval gate (the safety spine) and bundle assembly

Run the gate with `afc_ocr.services.eval_gate` over the **frozen gold eval slice**. This
is the same gate the CPU box uses; nothing ships that is not a clear, regression-free win.

Sketch (the cell->image regrouping for §5.4 uses the eval manifest; `finetune.py` wires
this in steps 4-5 of `run_finetune`):

```python
from afc_ocr.services import eval_gate
from afc_ocr.training import finetune

# Run the new rec.onnx over the eval crops -> per-image predictions, aligned to gold.
predictions, gold = _predict_eval("./output/afc_student_infer/rec.onnx", "./dataset_eval/")

new_metrics = eval_gate.compute_metrics(predictions, gold)

# current_metrics: run the SAME eval over the CURRENTLY shipped student (or {} for the
# very first student -> any positive primary metric clears the EPS gate from zero).
ship, reasons = eval_gate.regression_gate(
    new_metrics, current_metrics, must_pass_results
)

# Emit the versioned bundle regardless of ship (so a HOLD is still inspectable), but only
# FLIP the pointer when ship is True (step 7).
bundle = finetune.write_bundle(
    out_dir="./media/models",
    version=N,
    onnx_path="./output/afc_student_infer/rec.onnx",
    keys_path="./dataset_train/rec_keys.txt",
    metrics=new_metrics,
    gate_reasons=reasons,
    ship=ship,
    dataset_version="<train dataset_version from step 1>",
)
print("ship:", ship)
for r in reasons:
    print(r)
```

The gate ships ONLY when **per-image exact-JSON accuracy rises by >= 0.005** AND no
secondary metric (name exact-match, name CER, kill exact-match, row-alignment) regresses
past 0.005 AND the must-pass slice has zero new failures. See the heavy comments in
`eval_gate.regression_gate`.

`write_bundle` produces:

```
media/models/student_v<N>/
├── rec.onnx           # the fine-tuned recognizer
├── rec_keys.txt       # the custom AFC dictionary it was built against
├── VERSION            # "student_v<N>"  (local_ocr reads this as model_version)
├── model_card.json    # base model, dataset_version, char count, ship decision, reasons
└── eval_report.json   # full compute_metrics() + gate reasons
```

---

## 7. Promote into production (drop + flip the pointer)

Bring the bundle back to the backend box (scp / download) and place it at:

```
backend/media/models/student_v<N>/
```

Then point prod at it. `local_ocr.LocalOCREngine(model_dir=...)` loads
`<model_dir>/rec.onnx + rec_keys.txt + VERSION`; the engine is selected via the model
directory (`settings.OCR_LOCAL_MODEL_PATH`, per `local_ocr.py`'s P2 note). To promote:

1. Copy the verified bundle to `backend/media/models/student_v<N>/`.
2. Flip the "current" pointer to `student_v<N>` (set `OCR_LOCAL_MODEL_PATH` to that
   directory, or update whatever symlink/setting `local_ocr` reads as current — confirm
   the exact mechanism in `local_ocr.py` / settings before flipping).
3. Restart the workers so the lazy `get_engine()` singleton rebuilds against the new
   bundle.
4. Smoke-test: re-run a couple of known screenshots through the upload path and confirm
   the read matches the eval expectation.

**Only ship a bundle whose `eval_report.json` has `"ship": true`.** A bundle with
`ship: false` is kept for inspection but must never be flipped to current. Nothing ships
worse than what is already in production.

---

## 8. Reproducibility / audit trail

- The `manifest_<version>.jsonl` frozen in step 1 records exactly which `pair_id` /
  `image_sha256` / `final_json_hash` went into the train shard. Keep it next to the
  bundle's `model_card.json` (which records the same `dataset_version`) so any shipped
  student is fully traceable to its training data.
- The split (train/eval/holdout) is decided once, deterministically, from each
  screenshot's content hash at capture time (`afc_ocr.models._assign_split`). The
  assembler only filters by it, so a screenshot can never leak from train into the eval
  set across runs.
