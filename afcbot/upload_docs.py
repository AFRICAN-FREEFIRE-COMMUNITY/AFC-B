"""
AFC Bot — Document Upload Tool
================================
Run this script to add new documents (txt or PDF) to the bot's knowledge base.
The bot will automatically pick up the new content the next time it replies.

Usage:
    python upload_docs.py path/to/your/document.pdf
    python upload_docs.py path/to/your/document.txt
    python upload_docs.py                              ← lists current docs
"""

import os
import sys
import shutil

# Keep emoji prints from crashing a non-UTF-8 console (e.g. Windows cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Anchor to the script's folder (same as bot.py) — a cwd-relative "knowledge"
# would silently drop docs into <cwd>/knowledge where the bot never reads them.
KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")


def list_docs():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    files = os.listdir(KNOWLEDGE_DIR)
    if not files:
        print("📂  No documents uploaded yet.")
    else:
        print(f"📂  Documents in knowledge base ({len(files)} files):")
        for f in files:
            size = os.path.getsize(os.path.join(KNOWLEDGE_DIR, f))
            print(f"   • {f}  ({size:,} bytes)")


def upload_txt(filepath: str):
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    filename = os.path.basename(filepath)
    dest = os.path.join(KNOWLEDGE_DIR, filename)
    shutil.copy2(filepath, dest)
    print(f"✅  Uploaded: {filename} → {dest}")


def upload_pdf(filepath: str):
    try:
        import pdfplumber
    except ImportError:
        print("❌  pdfplumber not installed. Run: pip install pdfplumber")
        sys.exit(1)

    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    filename = os.path.splitext(os.path.basename(filepath))[0] + ".txt"
    dest = os.path.join(KNOWLEDGE_DIR, filename)

    print(f"📄  Extracting text from {os.path.basename(filepath)}...")
    with pdfplumber.open(filepath) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    text = "\n\n".join(pages)

    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"✅  Extracted and saved: {filename} → {dest}")
    print(f"   Total characters extracted: {len(text):,}")


def remove_doc(filename: str):
    target = os.path.join(KNOWLEDGE_DIR, filename)
    if os.path.exists(target):
        os.remove(target)
        print(f"🗑️   Removed: {filename}")
    else:
        print(f"❌  File not found: {filename}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        list_docs()
        print("\nUsage:")
        print("  python upload_docs.py <file.txt or file.pdf>   ← add a document")
        print("  python upload_docs.py --remove <filename>      ← remove a document")
        sys.exit(0)

    if sys.argv[1] == "--remove":
        if len(sys.argv) < 3:
            print("❌  Provide the filename to remove. E.g.: python upload_docs.py --remove myfile.txt")
            sys.exit(1)
        remove_doc(sys.argv[2])
        sys.exit(0)

    filepath = sys.argv[1]

    if not os.path.exists(filepath):
        print(f"❌  File not found: {filepath}")
        sys.exit(1)

    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".txt":
        upload_txt(filepath)
    elif ext == ".pdf":
        upload_pdf(filepath)
    else:
        print(f"❌  Unsupported file type: {ext}. Only .txt and .pdf are supported.")
        sys.exit(1)

    print("\n🔄  The bot will use this document automatically on its next reply.")
    list_docs()
