import json
import subprocess
import time
from pathlib import Path
from typing import Iterator, Tuple

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


class NGRDIError(RuntimeError):
    pass


class NGRDIProcessor:
    """
    Computes NGRDI (Normalized Green-Red Difference Index) from the RGB
    orthophoto in two block-wise passes so large rasters never load fully
    into memory:
      1. Pure NGRDI (float32, single band).
      2. Red-Yellow-Green colorized NGRDI (uint8, 3 band) for interpretation.
    """

    def __init__(self, project_dir: Path, vmin: float = -0.05, vmax: float = 0.2):
        self.project_dir = Path(project_dir)
        self.vmin = vmin
        self.vmax = vmax

    @property
    def ortho_path(self) -> Path:
        return self.project_dir / "odm_orthophoto" / "odm_orthophoto.tif"

    @property
    def out_dir(self) -> Path:
        return self.project_dir / "ngrdi"

    def run(self) -> Iterator[Tuple[int, str]]:
        if not self.ortho_path.exists():
            raise NGRDIError("No orthomosaic found - run reconstruction first.")

        self.out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.out_dir / "ngrdi_raw.tif"
        colored_path = self.out_dir / "ngrdi_colored.tif"
        preview_path = self.out_dir / "ngrdi_preview.png"

        t0 = time.time()
        yield 5, "Starting NGRDI computation"

        # ---- Pass 1: pure NGRDI (float32) --------------------------------
        yield 10, "Computing raw NGRDI raster (Green - Red) / (Green + Red)"
        with rasterio.open(self.ortho_path) as src:
            profile = src.profile.copy()
            profile.update(dtype=rasterio.float32, count=1, compress="lzw")

            blocks = list(src.block_windows(1))
            n_blocks = max(1, len(blocks))
            with rasterio.open(raw_path, "w", **profile) as dst:
                np.seterr(divide="ignore", invalid="ignore")
                for i, (_, window) in enumerate(blocks):
                    green = src.read(2, window=window).astype("float32")
                    red = src.read(1, window=window).astype("float32")
                    ngrdi = (green - red) / (green + red)
                    ngrdi = np.nan_to_num(ngrdi)
                    dst.write(ngrdi.astype(rasterio.float32), 1, window=window)

                    pct = 10 + int(35 * (i + 1) / n_blocks)
                    if i % 20 == 0 or i == n_blocks - 1:
                        yield pct, f"Raw NGRDI: block {i + 1}/{n_blocks}"

        yield 45, "Raw NGRDI raster complete"

        # ---- Pass 2: Red-Yellow-Green colorized version --------------------
        yield 50, "Colorizing NGRDI with Red-Yellow-Green index"
        cmap = plt.get_cmap("RdYlGn")
        chunk = 1024
        with rasterio.open(raw_path) as src:
            width, height = src.width, src.height
            profile = src.profile.copy()
            profile.update(dtype=rasterio.uint8, count=3, compress="lzw")

            xs = list(range(0, width, chunk))
            ys = list(range(0, height, chunk))
            total_chunks = max(1, len(xs) * len(ys))
            done = 0

            with rasterio.open(colored_path, "w", **profile) as dst:
                for y in ys:
                    for x in xs:
                        w = min(chunk, width - x)
                        h = min(chunk, height - y)
                        window = Window(x, y, w, h)

                        ngrdi = src.read(1, window=window)
                        ngrdi_norm = np.clip((ngrdi - self.vmin) / (self.vmax - self.vmin), 0, 1)
                        colored = cmap(ngrdi_norm)

                        r = (colored[:, :, 0] * 255).astype(np.uint8)
                        g = (colored[:, :, 1] * 255).astype(np.uint8)
                        b = (colored[:, :, 2] * 255).astype(np.uint8)

                        dst.write(r, 1, window=window)
                        dst.write(g, 2, window=window)
                        dst.write(b, 3, window=window)

                        done += 1
                        pct = 50 + int(40 * done / total_chunks)
                        if done % 10 == 0 or done == total_chunks:
                            yield pct, f"Colorizing NGRDI: chunk {done}/{total_chunks}"

        yield 92, "Colorized NGRDI raster complete"

        # ---- Light preview PNG (web UI + PDF report) -----------------------
        yield 95, "Rendering preview PNG"
        self._make_preview(colored_path, preview_path)

        elapsed = time.time() - t0
        self._write_meta(raw_path, colored_path, preview_path, elapsed)
        yield 100, f"NGRDI analysis complete ({elapsed:.1f}s)"

    def _make_preview(self, colored_path: Path, out_png: Path, max_dim: int = 1600):
        try:
            subprocess.run(
                ["gdal_translate", "-of", "PNG", "-outsize", "15%", "15%",
                 str(colored_path), str(out_png)],
                check=True, capture_output=True,
            )
            if out_png.exists():
                return
        except Exception:
            pass
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(colored_path) as im:
                im = im.convert("RGB")
                im.thumbnail((max_dim, max_dim))
                im.save(out_png)
        except Exception:
            pass

    def _write_meta(self, raw_path: Path, colored_path: Path, preview_path: Path, elapsed: float):
        meta = {
            "vmin": self.vmin,
            "vmax": self.vmax,
            "processing_time_s": round(elapsed, 2),
            "raw_geotiff": raw_path.name,
            "colored_geotiff": colored_path.name,
            "preview_available": preview_path.exists(),
            "raw_size_bytes": raw_path.stat().st_size if raw_path.exists() else None,
            "colored_size_bytes": colored_path.stat().st_size if colored_path.exists() else None,
        }
        with open(self.out_dir / "ngrdi_report.json", "w") as f:
            json.dump(meta, f, indent=2)