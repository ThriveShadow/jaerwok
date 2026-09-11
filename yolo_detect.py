"""
YOLOv8 detection over a (potentially huge) orthophoto GeoTIFF.

Strategy:
  1. Tile the GeoTIFF into overlapping windows (default 1024x1024, 160px
     overlap) using rasterio windowed reads - only one tile's worth of
     pixels is ever in memory at a time, regardless of source image size.
  2. Run YOLOv8 inference per tile, forced to CPU.
  3. Map each tile's local detections into full-resolution global pixel
     coordinates, then run per-class NMS globally to remove duplicate
     detections caused by the tile overlap.
  4. Render a single large annotated JPEG. To avoid loading the full-
     resolution array (which can be gigabytes on a large orthophoto),
     this is read back via a GDAL-decimated (downsampled) read capped at
     DEFAULT_MAX_OUTPUT_DIM px on the longest side, with detection boxes
     scaled into that same coordinate space before drawing.
"""
import json
import time
from pathlib import Path
from typing import Iterator, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import rasterio
from rasterio.windows import Window
from rasterio.enums import Resampling


class YoloDetectError(Exception):
    pass


DEFAULT_TILE_SIZE = 1024
DEFAULT_OVERLAP = 160
DEFAULT_CONF = 0.25
DEFAULT_IOU = 0.35
# Longest side of the final annotated JPEG. Keeps memory bounded when
# reading the orthophoto back for drawing, regardless of source size.
DEFAULT_MAX_OUTPUT_DIM = 6000

_PALETTE = [
    (230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48),
    (145, 30, 180), (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212),
    (0, 128, 128), (220, 190, 255), (170, 110, 40), (255, 250, 200), (128, 0, 0),
]


def _color_for_class(cls_id: int):
    return _PALETTE[cls_id % len(_PALETTE)]


class YoloDetector:
    def __init__(self, project_dir: Path, model_path: Path, conf: float = DEFAULT_CONF,
                 iou: float = DEFAULT_IOU, tile_size: int = DEFAULT_TILE_SIZE,
                 overlap: int = DEFAULT_OVERLAP, max_output_dim: int = DEFAULT_MAX_OUTPUT_DIM):
        self.project_dir = Path(project_dir)
        self.model_path = Path(model_path)
        self.conf = conf
        self.iou = iou
        self.tile_size = tile_size
        self.overlap = overlap
        self.max_output_dim = max_output_dim

    def run(self) -> Iterator[Tuple[int, str]]:
        ortho_path = self.project_dir / "odm_orthophoto" / "odm_orthophoto.tif"
        if not ortho_path.exists():
            raise YoloDetectError("Run orthomosaic reconstruction first - no orthophoto found.")
        if not self.model_path.exists():
            raise YoloDetectError(f"Model file not found: {self.model_path.name}")

        start_time = time.time()
        out_dir = self.project_dir / "yolo"
        out_dir.mkdir(exist_ok=True)

        yield 1, "Loading YOLOv8 model (CPU)..."
        from ultralytics import YOLO  # deferred - heavy import, only needed here
        model = YOLO(str(self.model_path))
        class_names = model.names

        yield 3, "Reading orthophoto metadata..."
        with rasterio.open(ortho_path) as src:
            width, height = src.width, src.height
            band_count = src.count

        step = self.tile_size - self.overlap
        if step <= 0:
            raise YoloDetectError("overlap must be smaller than tile_size")

        xs = list(range(0, width, step))
        ys = list(range(0, height, step))
        total_tiles = len(xs) * len(ys)

        yield 4, (f"Tiling {width}x{height} orthophoto into {total_tiles} tiles "
                  f"({self.tile_size}px, {self.overlap}px overlap)")

        all_boxes = []  # [x1, y1, x2, y2, conf, cls_id] in full-res pixel coords
        tiles_done = 0

        with rasterio.open(ortho_path) as src:
            band_indexes = [1, 2, 3] if band_count >= 3 else [1, 1, 1]

            for ty in ys:
                for tx in xs:
                    w = min(self.tile_size, width - tx)
                    h = min(self.tile_size, height - ty)
                    window = Window(tx, ty, w, h)

                    tile_arr = src.read(band_indexes, window=window)  # (3, h, w)
                    tile_arr = np.moveaxis(tile_arr, 0, -1)  # (h, w, 3)

                    # Skip essentially-empty tiles (edges of the orthophoto's
                    # non-rectangular footprint) - no point running inference
                    # on pure nodata.
                    if tile_arr.max() == 0:
                        tiles_done += 1
                        continue

                    if tile_arr.dtype != np.uint8:
                        tile_arr = np.clip(tile_arr, 0, 255).astype(np.uint8)

                    tile_img = Image.fromarray(tile_arr, mode="RGB")

                    results = model.predict(
                        source=tile_img, conf=self.conf, iou=self.iou,
                        device="cpu", verbose=False,
                    )[0]

                    if results.boxes is not None and len(results.boxes) > 0:
                        boxes = results.boxes.xyxy.cpu().numpy()
                        confs = results.boxes.conf.cpu().numpy()
                        classes = results.boxes.cls.cpu().numpy().astype(int)
                        for box, conf_, cls_id in zip(boxes, confs, classes):
                            all_boxes.append([
                                float(box[0] + tx), float(box[1] + ty),
                                float(box[2] + tx), float(box[3] + ty),
                                float(conf_), int(cls_id),
                            ])

                    tiles_done += 1
                    if tiles_done % 5 == 0 or tiles_done == total_tiles:
                        pct = 5 + int((tiles_done / total_tiles) * 70)  # ramps 5 -> 75
                        yield pct, f"Inference on tile {tiles_done}/{total_tiles}"

        yield 76, f"Raw detections: {len(all_boxes)}. De-duplicating overlap regions (NMS)..."

        final_detections = self._global_nms(all_boxes)

        yield 80, f"{len(final_detections)} detections after de-duplication"

        yield 82, "Rendering annotated preview (downsampled for large orthophotos)..."
        annotated_path = out_dir / "yolo_detections.jpg"
        out_w, out_h = self._render_annotated(
            ortho_path, final_detections, class_names, annotated_path, width, height,
        )

        yield 95, "Writing detection metadata..."
        counts_by_class = {}
        for det in final_detections:
            name = class_names.get(int(det[5]), str(int(det[5])))
            counts_by_class[name] = counts_by_class.get(name, 0) + 1

        meta = {
            "model_file": self.model_path.name,
            "conf": self.conf,
            "iou": self.iou,
            "tile_size": self.tile_size,
            "overlap": self.overlap,
            "total_tiles": total_tiles,
            "total_detections": len(final_detections),
            "counts_by_class": counts_by_class,
            "orthophoto_width": width,
            "orthophoto_height": height,
            "annotated_width": out_w,
            "annotated_height": out_h,
            "annotated_image": annotated_path.name,
            "processing_time_s": round(time.time() - start_time, 1),
        }
        with open(out_dir / "yolo_report.json", "w") as f:
            json.dump(meta, f, indent=2)

        with open(out_dir / "detections.json", "w") as f:
            json.dump(
                [{"x1": d[0], "y1": d[1], "x2": d[2], "y2": d[3], "conf": d[4],
                  "class_id": int(d[5]), "class_name": class_names.get(int(d[5]), str(int(d[5])))}
                 for d in final_detections],
                f, indent=2,
            )

        yield 99, "YOLOv8 detection complete"

    def _global_nms(self, boxes):
        if not boxes:
            return []
        try:
            import cv2
        except ImportError:
            raise YoloDetectError(
                "opencv-python-headless is required for NMS de-duplication "
                "(pip install opencv-python-headless)"
            )

        boxes_np = np.array(boxes)
        final = []
        for cls_id in np.unique(boxes_np[:, 5]):
            cls_mask = boxes_np[:, 5] == cls_id
            cls_boxes = boxes_np[cls_mask]

            xywh, scores = [], []
            for b in cls_boxes:
                w = b[2] - b[0]
                h = b[3] - b[1]
                xywh.append([int(b[0]), int(b[1]), int(w), int(h)])
                scores.append(float(b[4]))

            indices = cv2.dnn.NMSBoxes(xywh, scores, score_threshold=self.conf, nms_threshold=self.iou)
            if len(indices) > 0:
                for idx in np.array(indices).flatten():
                    final.append(cls_boxes[idx])
        return final

    def _render_annotated(self, ortho_path, detections, class_names, out_path, full_w, full_h):
        scale = min(1.0, self.max_output_dim / max(full_w, full_h))
        out_w = max(1, int(full_w * scale))
        out_h = max(1, int(full_h * scale))

        with rasterio.open(ortho_path) as src:
            band_count = src.count
            band_indexes = [1, 2, 3] if band_count >= 3 else [1, 1, 1]
            arr = src.read(
                band_indexes,
                out_shape=(3, out_h, out_w),
                resampling=Resampling.bilinear,
            )
        arr = np.moveaxis(arr, 0, -1)
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(12, int(14 * (out_w / 2000))))
        except Exception:
            font = ImageFont.load_default()

        for det in detections:
            x1, y1, x2, y2, conf_, cls_id = det
            cls_id = int(cls_id)
            sx1, sy1, sx2, sy2 = x1 * scale, y1 * scale, x2 * scale, y2 * scale
            color = _color_for_class(cls_id)
            name = class_names.get(cls_id, str(cls_id))
            label = f"{name} {conf_:.2f}"

            width_px = max(2, int(3 * (out_w / 2000)))
            draw.rectangle([sx1, sy1, sx2, sy2], outline=color, width=width_px)

            text_bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]
            draw.rectangle([sx1, max(0, sy1 - th - 6), sx1 + tw + 6, sy1], fill=color)
            draw.text((sx1 + 3, max(0, sy1 - th - 5)), label, fill=(0, 0, 0), font=font)

        img.save(out_path, "JPEG", quality=90)
        return out_w, out_h