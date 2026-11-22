"""
build_zip.py
Bundles all modular project files into `Aurora-KoA-Modular.zip` with the exact directory tree.
"""

import os
import zipfile


def build_archive(output_filename: str = "Aurora-KoA-Modular.zip") -> None:
    packed = 0
    with zipfile.ZipFile(output_filename, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_dir, _, files in os.walk("."):
            if any(skip in root_dir for skip in (".git", "__pycache__", ".venv", "venv", "media_vault")):
                continue
            for fname in sorted(files):
                if fname.endswith((".db", ".db-wal", ".db-shm", ".zip")):
                    continue
                full_path = os.path.join(root_dir, fname)
                rel_path = os.path.relpath(full_path, ".").replace("\\", "/")
                arcname = f"Aurora-KoA-main/{rel_path}"
                zf.write(full_path, arcname=arcname)
                print(f" + Packed {arcname}")
                packed += 1
    print(f"\nCreated {output_filename} ({packed} files) successfully!")


if __name__ == "__main__":
    build_archive()
