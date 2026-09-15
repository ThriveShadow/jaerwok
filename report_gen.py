import json
import subprocess
import matplotlib.pyplot as plt
from fpdf import FPDF
from pathlib import Path
from typing import Optional

LOGO_SVG = Path(__file__).resolve().parent / "static" / "img" / "jaerwok.svg"
LOGO_PNG_CACHE = Path(__file__).resolve().parent / "static" / "img" / "jaerwok_logo_cache.png"

REPORT_TITLE = "JAERWOK PLANTATION MAPPING SYSTEM - Report"
REPORT_SUBTITLE = (
    "Made by Ridwan & Jason for BINUS Thesis - AGRICULTURAL CROP MAPPING AND "
    "ANOMALY DETECTION SYSTEM USING UAV (UNMANNED AERIAL VEHICLE) WITH INTEGRATED "
    "IMAGE STITCHING AND MACHINE LEARNING"
)


def _get_logo_png() -> Optional[Path]:
    """Rasterize the SVG logo to PNG once and cache it (FPDF can't embed SVG)."""
    if LOGO_PNG_CACHE.exists():
        return LOGO_PNG_CACHE
    if not LOGO_SVG.exists():
        return None
    try:
        import cairosvg
        cairosvg.svg2png(url=str(LOGO_SVG), write_to=str(LOGO_PNG_CACHE),
                          output_width=400, output_height=400)
        return LOGO_PNG_CACHE
    except Exception:
        return None


class ReportPDF(FPDF):
    """FPDF subclass that stamps the header (logo + title + subtitle) and a
    'Page X/Y' footer on every page automatically."""

    def header(self):
        logo = _get_logo_png()
        text_x = 10
        if logo:
            self.image(str(logo), x=10, y=8, w=16)
            text_x = 30
        content_w = 200 - text_x

        self.set_xy(text_x, 8)
        self.set_font("Arial", "B", 13)
        self.multi_cell(content_w, 6, REPORT_TITLE, align="L")
        self.set_x(text_x)
        self.set_font("Arial", "", 7)
        self.multi_cell(content_w, 3.5, REPORT_SUBTITLE, align="L")

        self.ln(2)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")


def _read_odm_stats(project_dir: Path, fallback_image_count: Optional[int] = None) -> dict:
    stats_path = project_dir / "odm_report" / "stats.json"
    if not stats_path.exists():
        return {}
    try:
        with open(stats_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    out = {}
    recon = raw.get("reconstruction_statistics", raw)
    out["images_used"] = (
        recon.get("reconstructed_images_count")
        or recon.get("reconstructed_shots_count")
        or raw.get("images_count")
        or raw.get("reconstructed_images_count")
        or fallback_image_count
    )
    out["points"] = recon.get("reconstructed_points_count") or raw.get("point_cloud_points")
    out["gsd_cm"] = raw.get("odm_processing_statistics", {}).get("average_gsd") or raw.get("gsd")
    out["area_sqm"] = raw.get("processing_statistics", {}).get("area")
    out["processing_time_s"] = raw.get("odm_processing_statistics", {}).get("total_time") or raw.get("processing_time")

    stats_times = raw.get("processing_statistics", {}).get("steps_times", {})
    feat_stats = raw.get("features_statistics", {}).get("detected_features", {})
    out["feature_extraction_time"] = stats_times.get("Feature Extraction", "N/A")
    out["detected_features_mean"] = feat_stats.get("mean", "N/A")
    out["total_time_human"] = raw.get("odm_processing_statistics", {}).get("total_time_human", "N/A")
    return out


def _make_preview_png(ortho_path: Path, out_png: Path) -> bool:
    if not ortho_path.exists():
        return False
    try:
        # Added -a_nodata 0 to force black to transparent, removed -b flags
        subprocess.run(["gdal_translate", "-of", "PNG", "-a_nodata", "0", "-outsize", "12%", "12%", str(ortho_path), str(out_png)], check=True, capture_output=True)
        return out_png.exists()
    except Exception:
        pass
    try:
        from PIL import Image
        import numpy as np
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(ortho_path) as im:
            im = im.convert("RGBA")
            arr = np.array(im)
            
            # Mask black pixels to transparent in PIL fallback
            r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]
            mask = (r == 0) & (g == 0) & (b == 0)
            arr[mask, 3] = 0
            
            im = Image.fromarray(arr)
            im.thumbnail((2000, 2000))
            im.save(out_png, "PNG")
        return True
    except Exception:
        return False

def _plot_usage(usage_file: Path, out_png: Path):
    if not usage_file.exists():
        return False

    with open(usage_file) as f:
        data = json.load(f)

    if not data.get("time"):
        return False

    fig, ax = plt.subplots(figsize=(12, 7))

    ax.plot(data["time"], data["cpu"], label="CPU %", color="red", linewidth=1.5)
    ax.plot(data["time"], data["ram"], label="RAM %", color="blue", linewidth=1.5)
    ax.set_ylim(0, 125)

    if "markers" in data:
        markers = sorted(data["markers"], key=lambda m: m["time"])
        GROUP_THRESHOLD = 3.0
        groups = []
        current_group = []
        for m in markers:
            if not current_group:
                current_group = [m]
            elif m["time"] - current_group[-1]["time"] < GROUP_THRESHOLD:
                current_group.append(m)
            else:
                groups.append(current_group)
                current_group = [m]
        if current_group:
            groups.append(current_group)

        for group in groups:
            group_time = group[0]["time"]
            ax.axvline(x=group_time, color="gray", linestyle="--", alpha=0.5, zorder=1)

            if len(group) == 1:
                m = group[0]
                label = f"{m['label']} ({int(m['time'])}s)"
                ax.text(group_time, 75, label, rotation=90, ha="center", va="center",
                        fontsize=9, color="black",
                        bbox=dict(boxstyle="round,pad=0.25", facecolor="white", alpha=0.85, edgecolor="none"),
                        zorder=5)
            else:
                lines = [f"• {m['label']} ({int(m['time'])}s)" for m in group]
                label = "\n".join(lines)
                ax.text(group_time, 75, label, rotation=90, ha="center", va="center",
                        fontsize=9, color="black", linespacing=1.3,
                        bbox=dict(boxstyle="round,pad=0.35", facecolor="white", alpha=0.9, edgecolor="none"),
                        zorder=5)

    ax.set_xlabel("Time (seconds)")
    ax.set_ylabel("Usage %")
    ax.set_title("System Resource Usage (CPU & RAM)")
    ax.legend(loc="upper right")

    plt.tight_layout()
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close()

    return out_png.exists()


def _image_dims_mm(path: Path, max_w: float, max_h: float):
    """Return (w, h) in mm for an image scaled to fit within max_w x max_h,
    preserving aspect ratio."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            px_w, px_h = im.size
    except Exception:
        return max_w, max_w * 0.75

    aspect = px_h / px_w
    w = max_w
    h = w * aspect
    if h > max_h:
        h = max_h
        w = h / aspect
    return w, h


# ---------------------------------------------------------------------------
# Step-level summary: cheap, no PDF. Called right after step 2 (reconstruction)
# finishes so the step-2 result panel (preview + stats) has something to show.
# ---------------------------------------------------------------------------
def build_summary(project_dir: Path) -> dict:
    project_dir = Path(project_dir)
    report_dir = project_dir / "report"
    report_dir.mkdir(exist_ok=True)

    ortho_path = project_dir / "odm_orthophoto" / "odm_orthophoto.tif"
    preview_png = report_dir / "orthomosaic_preview.png"

    preview_ok = _make_preview_png(ortho_path, preview_png)

    n_input_images = len(list((project_dir / "images").glob("*")))
    stats = _read_odm_stats(project_dir, fallback_image_count=n_input_images)

    ngrdi_meta_path = project_dir / "ngrdi" / "ngrdi_report.json"
    yolo_meta_path = project_dir / "yolo" / "yolo_report.json"

    data = {
        "input_images": n_input_images,
        "images_used": stats.get("images_used"),
        "reconstructed_points": stats.get("points"),
        "average_gsd_cm": stats.get("gsd_cm"),
        "area_sqm": stats.get("area_sqm"),
        "processing_time_s": stats.get("processing_time_s"),
        "orthophoto_exists": ortho_path.exists(),
        "orthophoto_size_bytes": ortho_path.stat().st_size if ortho_path.exists() else None,
        "preview_available": preview_ok,
        "ngrdi_available": ngrdi_meta_path.exists(),
        "yolo_available": yolo_meta_path.exists(),
    }

    with open(report_dir / "report.json", "w") as f:
        json.dump(data, f, indent=2)

    return data


# ---------------------------------------------------------------------------
# Full PDF report: heavy, on-demand. Called from the new Step 5 ("Generate
# Report" button). Pulls together ODM + NGRDI + YOLO results, whichever of
# them exist so far.
# ---------------------------------------------------------------------------
def _generate_pdf(odm_stats: dict, preview_png: Path, graph_png: Path,
                   dsm_png: Optional[Path], matchgraph_png: Optional[Path],
                   ngrdi_data: Optional[dict], ngrdi_preview_png: Optional[Path],
                   yolo_data: Optional[dict], yolo_annotated_png: Optional[Path],
                   out_pdf: Path):
    pdf = ReportPDF()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    if preview_png.exists():
        max_h = pdf.h - pdf.get_y() - pdf.b_margin
        w, h = _image_dims_mm(preview_png, max_w=190, max_h=max_h)
        x = 10 + (190 - w) / 2
        pdf.image(str(preview_png), x=x, w=w, h=h)

    if dsm_png and dsm_png.exists():
        pdf.add_page()
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 10, "Digital Surface Model (DSM)", ln=1, align="C")
        max_h = pdf.h - pdf.get_y() - pdf.b_margin
        w, h = _image_dims_mm(dsm_png, max_w=190, max_h=max_h)
        x = 10 + (190 - w) / 2
        pdf.image(str(dsm_png), x=x, w=w, h=h)

    if matchgraph_png and matchgraph_png.exists():
        pdf.add_page()
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 10, "Feature Match Graph", ln=1, align="C")
        max_h = pdf.h - pdf.get_y() - pdf.b_margin
        w, h = _image_dims_mm(matchgraph_png, max_w=190, max_h=max_h)
        x = 10 + (190 - w) / 2
        pdf.image(str(matchgraph_png), x=x, w=w, h=h)

    # ---- ODM / reconstruction stats -------------------------------------
    pdf.add_page()
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(0, 8, "Processing Statistics", ln=1)
    pdf.set_font("Arial", '', 11)
    pdf.cell(0, 6, f"Images Used: {odm_stats.get('images_used', 'N/A')}", ln=1)
    pdf.cell(0, 6, f"Reconstructed Points: {odm_stats.get('points', 'N/A')}", ln=1)
    pdf.cell(0, 6, f"Mean Detected Features: {odm_stats.get('detected_features_mean', 'N/A')}", ln=1)
    pdf.cell(0, 6, f"Average GSD: {odm_stats.get('gsd_cm', 'N/A')} cm/px", ln=1)
    pdf.cell(0, 6, f"Area Covered: {odm_stats.get('area_sqm', 'N/A')} sqm", ln=1)
    pdf.cell(0, 6, f"Feature Extraction Time: {odm_stats.get('feature_extraction_time', 'N/A')} s", ln=1)
    pdf.cell(0, 6, f"Total Processing Time: {odm_stats.get('total_time_human', 'N/A')} ({odm_stats.get('processing_time_s', 'N/A')} s)", ln=1)

    if graph_png.exists():
        pdf.add_page()
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 10, "System Resource Usage (CPU & RAM)", ln=1, align="C")
        max_h = pdf.h - pdf.get_y() - pdf.b_margin
        w, h = _image_dims_mm(graph_png, max_w=190, max_h=max_h)
        x = 10 + (190 - w) / 2
        pdf.image(str(graph_png), x=x, w=w, h=h)

    # ---- NGRDI section -----------------------------------------------
    if ngrdi_data:
        pdf.add_page()
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 10, "NGRDI Vegetation Index Analysis", ln=1)
        pdf.set_font("Arial", '', 11)
        pdf.cell(0, 6, f"Processing Time: {ngrdi_data.get('processing_time_s', 'N/A')} s", ln=1)
        pdf.cell(0, 6, f"Colormap Range (vmin / vmax): {ngrdi_data.get('vmin')} / {ngrdi_data.get('vmax')}", ln=1)
        pdf.ln(4)

        col_w = 90
        caption_h = 6
        y_caption = pdf.get_y()
        y_image = y_caption + caption_h
        max_h = pdf.h - y_image - pdf.b_margin

        if preview_png.exists():
            pdf.set_xy(10, y_caption)
            pdf.set_font("Arial", 'I', 8)
            pdf.cell(col_w, caption_h, "Orthomosaic (RGB)", align="C")
            w, h = _image_dims_mm(preview_png, max_w=col_w, max_h=max_h)
            pdf.image(str(preview_png), x=10 + (col_w - w) / 2, y=y_image, w=w, h=h)

        if ngrdi_preview_png and ngrdi_preview_png.exists():
            pdf.set_xy(105, y_caption)
            pdf.set_font("Arial", 'I', 8)
            pdf.cell(col_w, caption_h, "NGRDI (Red-Yellow-Green Index)", align="C")
            w, h = _image_dims_mm(ngrdi_preview_png, max_w=col_w, max_h=max_h)
            pdf.image(str(ngrdi_preview_png), x=105 + (col_w - w) / 2, y=y_image, w=w, h=h)

    # ---- YOLOv8 detection section --------------------------------------
    if yolo_data:
        pdf.add_page()
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(0, 10, "YOLOv8 Detection", ln=1)
        pdf.set_font("Arial", '', 11)
        pdf.cell(0, 6, f"Model: {yolo_data.get('model_file', 'N/A')}", ln=1)
        pdf.cell(0, 6, f"Tiles Processed: {yolo_data.get('total_tiles', 'N/A')} (tile size {yolo_data.get('tile_size', 'N/A')}px, overlap {yolo_data.get('overlap', 'N/A')}px)", ln=1)
        pdf.cell(0, 6, f"Confidence / IOU Threshold: {yolo_data.get('conf', 'N/A')} / {yolo_data.get('iou', 'N/A')}", ln=1)
        pdf.cell(0, 6, f"Total Detections: {yolo_data.get('total_detections', 'N/A')}", ln=1)
        pdf.cell(0, 6, f"Processing Time: {yolo_data.get('processing_time_s', 'N/A')} s", ln=1)

        counts = yolo_data.get("counts_by_class") or {}
        if counts:
            pdf.ln(2)
            pdf.set_font("Arial", 'B', 10)
            pdf.cell(0, 6, "Detections by class:", ln=1)
            pdf.set_font("Arial", '', 10)
            for name, count in counts.items():
                pdf.cell(0, 5, f"  - {name}: {count}", ln=1)

        if yolo_annotated_png and yolo_annotated_png.exists():
            pdf.ln(4)
            max_h = pdf.h - pdf.get_y() - pdf.b_margin
            w, h = _image_dims_mm(yolo_annotated_png, max_w=190, max_h=max_h)
            x = 10 + (190 - w) / 2
            pdf.image(str(yolo_annotated_png), x=x, w=w, h=h)

    pdf.output(str(out_pdf))


def build_pdf_report(project_dir: Path) -> dict:
    """Generates the full PDF (and a combined full_report.json) from
    whichever of ODM / NGRDI / YOLO results currently exist on disk. Safe
    to call at any point after step 2 - later steps just add their section
    if/when they've been run."""
    project_dir = Path(project_dir)
    report_dir = project_dir / "report"
    report_dir.mkdir(exist_ok=True)

    ortho_path = project_dir / "odm_orthophoto" / "odm_orthophoto.tif"
    preview_png = report_dir / "orthomosaic_preview.png"
    graph_png = report_dir / "usage_graph.png"
    pdf_report = report_dir / "report.pdf"

    dsm_png = project_dir / "opensfm" / "stats" / "dsm.png"
    matchgraph_png = project_dir / "opensfm" / "stats" / "matchgraph.png"

    if not preview_png.exists():
        _make_preview_png(ortho_path, preview_png)
    _plot_usage(report_dir / "usage.json", graph_png)

    n_input_images = len(list((project_dir / "images").glob("*")))
    odm_stats = _read_odm_stats(project_dir, fallback_image_count=n_input_images)

    ngrdi_meta_path = project_dir / "ngrdi" / "ngrdi_report.json"
    ngrdi_data = None
    ngrdi_preview_png = None
    if ngrdi_meta_path.exists():
        with open(ngrdi_meta_path) as f:
            ngrdi_data = json.load(f)
        candidate = project_dir / "ngrdi" / "ngrdi_preview.png"
        if candidate.exists():
            ngrdi_preview_png = candidate

    yolo_meta_path = project_dir / "yolo" / "yolo_report.json"
    yolo_data = None
    yolo_annotated_png = None
    if yolo_meta_path.exists():
        with open(yolo_meta_path) as f:
            yolo_data = json.load(f)
        candidate = project_dir / "yolo" / yolo_data.get("annotated_image", "")
        if candidate.exists():
            yolo_annotated_png = candidate

    _generate_pdf(odm_stats, preview_png, graph_png, dsm_png, matchgraph_png,
                  ngrdi_data, ngrdi_preview_png, yolo_data, yolo_annotated_png,
                  pdf_report)

    full_data = {
        "input_images": n_input_images,
        "odm": odm_stats,
        "ngrdi": ngrdi_data,
        "yolo": yolo_data,
        "orthophoto_exists": ortho_path.exists(),
        "orthophoto_size_bytes": ortho_path.stat().st_size if ortho_path.exists() else None,
    }
    with open(report_dir / "full_report.json", "w") as f:
        json.dump(full_data, f, indent=2)

    return full_data