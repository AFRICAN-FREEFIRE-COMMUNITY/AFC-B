"""
afc_ocr.training
================================================================================
Off-box (GPU) training package for the AFC self-hosted OCR student model.

Nothing in here trains on the box that runs Django. The CPU box only ASSEMBLES the
dataset (afc_ocr.services.dataset) and runs the EVAL GATE (afc_ocr.services.eval_gate).
The actual PaddleOCR fine-tune runs on a GPU (Colab / Kaggle / a spot g4dn) per the
step-by-step in finetune_ppocrv5.md, driven by finetune.py.

IMPORTANT: importing this package (or finetune.py) must NOT require paddle to be
installed. The heavy paddle import lives INSIDE the train function, guarded, so that the
eval-gate path and `import afc_ocr.training.finetune` stay dependency-light on the CPU
box. See finetune.py for the guard.
"""
