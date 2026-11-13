"""
build_zip.py
Run `python build_zip.py` locally to bundle all 10 modular files into
`Aurora-KoA-Modular.zip` (You can also download this .zip directly from the
Database Vault page inside the running Streamlit web app).
"""

import os
import zipfile

FILES_TO_PACK = [
    "bot.py",
    "profiles.py",
    "mode_gaming.py",
    "mode_realtor.py",
    "database.py",
    "ai_engine.py",
    "discord_worker.py",
    "web_ui.py",
    "requirements.txt",
    "secrets_template.toml"
]


def build_archive(output_filename: str = "Aurora-KoA-Modular.zip") -> None:
    with zipfile.ZipFile(output_filename, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in FILES_TO_PACK:
            if os.path.exists(fname):
                zf.write(fname, arcname=f"Aurora-KoA-main/{fname}")
                print(f" + Packed {fname}")
    print(f"\nCreated {output_filename} successfully!")


if __name__ == "__main__":
    build_archive()