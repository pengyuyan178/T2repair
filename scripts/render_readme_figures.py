import argparse
import hashlib
import json
from pathlib import Path

import fitz


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(root, width):
    rows = json.loads((root / "figures/manifest.json").read_text(encoding="utf-8"))
    output = root / "assets/paper"
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for row in rows:
        source = root / row["file"]
        if sha256(source) != row["sha256"]:
            raise ValueError("Update the PDF manifest before rendering: " + row["file"])
        with fitz.open(source) as document:
            if len(document) != 1:
                raise ValueError("Expected a single-page figure: " + row["file"])
            page = document[0]
            scale = width / page.rect.width
            pixels = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            target = output / (source.stem + ".png")
            pixels.save(target)
            records.append({"pdf": row["file"], "pdf_sha256": row["sha256"],
                            "preview": target.relative_to(root).as_posix(), "preview_sha256": sha256(target),
                            "width": pixels.width, "height": pixels.height})
    manifest = {"purpose": "README display previews; the linked PDFs are the original paper figures.",
                "renderer": "PyMuPDF " + fitz.VersionBind, "width_requested": width, "figures": records}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"rendered": len(records), "width": width, "preview_bytes": sum((root / r["preview"]).stat().st_size for r in records)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render the paper PDFs as lossless README display previews.")
    parser.add_argument("--width", type=int, default=2400)
    args = parser.parse_args()
    if args.width < 1:
        parser.error("--width must be positive")
    render(Path(__file__).resolve().parents[1], args.width)
