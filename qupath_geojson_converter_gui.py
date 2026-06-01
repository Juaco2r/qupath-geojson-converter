"""
QuPath GeoJSON Converter GUI

Simple Tkinter application to convert external GeoJSON classification fields
into QuPath-compatible annotation and object properties.

Main conversion target:
properties = {
    "objectType": "annotation" or "detection",
    "classification": {
        "name": "Class name",
        "color": [R, G, B]
    }
}

Efficiency notes:
- Conversion runs in a background thread, so the GUI stays responsive.
- Progress/status updates are sent to the GUI through a thread-safe queue.
- Output is written as compact JSON by default, which is faster and produces
  smaller files than pretty-printed JSON.
- Optional geometry tools are imported only when a merge-like mode is selected.

For packaging:
- Build separately on each OS with PyInstaller.
- Basic conversion uses only the Python standard library.
- Build dependencies should be included per selected feature set.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# Improve scaling on Windows, especially after packaging.
try:  # pragma: no cover - platform-specific convenience
    import ctypes

    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


SUPPORTED_EXTENSIONS = {".geojson", ".json"}
DEFAULT_SUFFIX = "_qupath"
DEFAULT_COLOR = [255, 0, 0]
PROGRESS_UPDATE_EVERY_N_FEATURES = 250
COMPACT_JSON_OUTPUT = True

APP_VERSION = "1.0.0"
ZENODO_DOI = "10.5281/zenodo.20496387"
ZENODO_URL = "https://doi.org/10.5281/zenodo.20496387"
GITHUB_URL = "https://github.com/Juaco2r/qupath-geojson-converter"
AUTHOR_NAME = "Jose Rodriguez"
AUTHOR_ORCID = "0000-0003-4373-5480"
APP_ICON_FILENAME = "assets/icon/qupath_geojson_converter.ico"

MODE_MERGE = "merge"
MODE_STANDARD = "standard"
MODE_OBJECTS = "objects"
MODE_CLASS_ANNOTATIONS_OBJECTS = "class_annotations_objects"
MODE_SPLIT = "split"

ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class ConversionOptions:
    keep_extra_properties: bool = True
    split_multipolygons: bool = False
    merge_same_class: bool = False
    create_detection_objects: bool = False
    create_objects_inside_annotations: bool = False
    overwrite: bool = False


# -----------------------------------------------------------------------------
# Conversion helpers
# -----------------------------------------------------------------------------


def check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Conversion cancelled by user.")


def hex_to_rgb(hex_color: Any) -> Optional[List[int]]:
    """Convert '#FF0100' or 'FF0100' to [255, 1, 0]."""
    if hex_color is None:
        return None

    text = str(hex_color).strip()
    if not text:
        return None

    text = text.replace("#", "")

    # Support short CSS hex, e.g. #F00 -> #FF0000.
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)

    if len(text) != 6:
        return None

    try:
        return [
            int(text[0:2], 16),
            int(text[2:4], 16),
            int(text[4:6], 16),
        ]
    except ValueError:
        return None


def normalize_rgb(color: Any) -> Optional[List[int]]:
    """Return a QuPath-friendly [R, G, B] list when possible."""
    if color is None:
        return None

    if isinstance(color, str):
        return hex_to_rgb(color)

    if isinstance(color, (list, tuple)) and len(color) >= 3:
        try:
            rgb = [
                int(round(float(color[0]))),
                int(round(float(color[1]))),
                int(round(float(color[2]))),
            ]
            return [max(0, min(255, value)) for value in rgb]
        except Exception:
            return None

    return None


def get_class_name(properties: Dict[str, Any]) -> str:
    """Get class name from common external or QuPath-like property formats."""
    classification = properties.get("classification")
    if isinstance(classification, dict):
        name = classification.get("name")
        if name:
            return str(name)

    for key in ("class_name", "name", "label", "category"):
        value = properties.get(key)
        if value:
            return str(value)

    return "Annotation"


def get_class_color(properties: Dict[str, Any]) -> List[int]:
    """Get color as [R, G, B] from common property formats."""
    classification = properties.get("classification")
    if isinstance(classification, dict):
        rgb = normalize_rgb(classification.get("color"))
        if rgb is not None:
            return rgb

    for key in ("class_color_hex", "color", "fill", "stroke"):
        rgb = normalize_rgb(properties.get(key))
        if rgb is not None:
            return rgb

    return list(DEFAULT_COLOR)


def convert_feature_to_qupath(
    feature: Dict[str, Any],
    *,
    keep_extra_properties: bool = True,
    object_type: str = "annotation",
) -> Dict[str, Any]:
    """Convert one GeoJSON Feature to QuPath-compatible properties."""
    properties = feature.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}

    class_name = get_class_name(properties)
    class_color = get_class_color(properties)

    object_type = "detection" if object_type == "detection" else "annotation"

    new_properties: Dict[str, Any] = {
        "objectType": object_type,
        "classification": {
            "name": class_name,
            "color": class_color,
        },
    }

    # Give detections a readable name too. QuPath mainly uses the classification
    # for counts, but this helps when inspecting individual imported objects.
    if object_type == "detection":
        new_properties["name"] = class_name
        new_properties["measurements"] = {}

    if keep_extra_properties:
        excluded = {
            "classification",
            "class_name",
            "class_color_hex",
            "color",
            "fill",
            "stroke",
            "objectType",
        }
        for key, value in properties.items():
            if key not in excluded:
                new_properties[key] = value

    new_feature: Dict[str, Any] = {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": new_properties,
    }

    if "id" in feature:
        new_feature["id"] = feature["id"]

    return new_feature


def split_multipolygon_feature(feature: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Split a MultiPolygon feature into multiple Polygon features.

    This avoids deepcopying the full feature repeatedly, which helps when files
    contain many large geometries.
    """
    geometry = feature.get("geometry") or {}
    if not isinstance(geometry, dict) or geometry.get("type") != "MultiPolygon":
        return [feature]

    coordinates = geometry.get("coordinates") or []
    output_features: List[Dict[str, Any]] = []

    base_properties = feature.get("properties") or {}
    base_id = feature.get("id")

    for index, polygon_coordinates in enumerate(coordinates, start=1):
        new_feature: Dict[str, Any] = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": polygon_coordinates,
            },
            "properties": base_properties,
        }
        if base_id is not None:
            new_feature["id"] = f"{base_id}_{index}"
        output_features.append(new_feature)

    return output_features


def safe_filename_part(text: str) -> str:
    """Create a simple safe text part for generated IDs."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return text.strip("_") or "annotation"


# -----------------------------------------------------------------------------
# Optional Shapely-based merge helpers
# -----------------------------------------------------------------------------


def import_shapely_tools() -> Dict[str, Any]:
    """Import Shapely lazily so the basic converter stays dependency-free."""
    try:
        from shapely.geometry import MultiPolygon, Polygon, mapping, shape
        from shapely.ops import unary_union

        try:
            from shapely.validation import make_valid
        except Exception:
            make_valid = None

        return {
            "MultiPolygon": MultiPolygon,
            "Polygon": Polygon,
            "mapping": mapping,
            "shape": shape,
            "unary_union": unary_union,
            "make_valid": make_valid,
        }
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("The merge option needs Shapely installed: pip install shapely") from exc


def extract_polygonal_parts(geom: Any, tools: Dict[str, Any]) -> List[Any]:
    """Extract Polygon objects from Polygon, MultiPolygon, or GeometryCollection."""
    Polygon = tools["Polygon"]
    MultiPolygon = tools["MultiPolygon"]

    if geom is None or getattr(geom, "is_empty", False):
        return []

    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty and geom.area > 0 else []

    if isinstance(geom, MultiPolygon):
        return [part for part in geom.geoms if not part.is_empty and part.area > 0]

    if hasattr(geom, "geoms"):
        parts: List[Any] = []
        for subgeom in geom.geoms:
            parts.extend(extract_polygonal_parts(subgeom, tools))
        return parts

    return []


def repair_geometry(geom: Any, tools: Dict[str, Any]) -> Any:
    """Repair invalid geometry using make_valid when available, otherwise buffer(0)."""
    if geom is None or getattr(geom, "is_empty", False):
        return geom

    if getattr(geom, "is_valid", True):
        return geom

    make_valid = tools.get("make_valid")
    if make_valid is not None:
        try:
            repaired = make_valid(geom)
            if repaired is not None and not repaired.is_empty:
                geom = repaired
        except Exception:
            pass

    if not getattr(geom, "is_valid", True):
        try:
            repaired = geom.buffer(0)
            if repaired is not None and not repaired.is_empty:
                geom = repaired
        except Exception:
            pass

    return geom


def common_extra_properties(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only extra properties with the same value across all merged features."""
    if not features:
        return {}

    excluded = {"classification", "objectType", "merged_feature_count", "contained_object_count", "measurements"}
    first_props = features[0].get("properties") or {}
    common: Dict[str, Any] = {}

    for key, first_value in first_props.items():
        if key in excluded:
            continue
        same_for_all = True
        for feature in features[1:]:
            props = feature.get("properties") or {}
            if props.get(key) != first_value:
                same_for_all = False
                break
        if same_for_all:
            common[key] = first_value

    return common


def merge_features_by_class(
    features: List[Dict[str, Any]],
    *,
    keep_extra_properties: bool = True,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Merge all features with the same classification.name into one annotation."""
    tools = import_shapely_tools()
    shape = tools["shape"]
    mapping = tools["mapping"]
    unary_union = tools["unary_union"]
    MultiPolygon = tools["MultiPolygon"]

    errors: List[str] = []
    grouped: Dict[str, Dict[str, Any]] = {}

    total = max(len(features), 1)
    for index, feature in enumerate(features, start=1):
        check_cancel(cancel_event)
        if progress_callback and (index == 1 or index % PROGRESS_UPDATE_EVERY_N_FEATURES == 0 or index == total):
            progress_callback("Preparing merge", min(0.78, 0.55 + 0.20 * (index / total)))

        properties = feature.get("properties") or {}
        classification = properties.get("classification") or {}
        class_name = str(classification.get("name") or "Annotation")
        class_color = normalize_rgb(classification.get("color")) or list(DEFAULT_COLOR)

        group = grouped.setdefault(
            class_name,
            {
                "class_name": class_name,
                "class_color": class_color,
                "features": [],
                "parts": [],
            },
        )
        group["features"].append(feature)

        geometry = feature.get("geometry")
        if not geometry:
            errors.append(f"Feature {index} in class '{class_name}' has no geometry and was skipped during merge.")
            continue

        try:
            geom = shape(geometry)
            geom = repair_geometry(geom, tools)
            parts = extract_polygonal_parts(geom, tools)
            if not parts:
                errors.append(
                    f"Feature {index} in class '{class_name}' did not contain valid polygonal geometry after repair."
                )
                continue
            group["parts"].extend(parts)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Feature {index} in class '{class_name}' could not be converted: {exc}")

    merged_features: List[Dict[str, Any]] = []
    class_items = list(grouped.items())
    total_classes = max(len(class_items), 1)

    for class_index, (class_name, group) in enumerate(class_items, start=1):
        check_cancel(cancel_event)
        if progress_callback:
            progress_callback(
                f"Merging class {class_index}/{total_classes}: {class_name}",
                min(0.90, 0.78 + 0.10 * ((class_index - 1) / total_classes)),
            )

        parts = group["parts"]
        if not parts:
            errors.append(f"Class '{class_name}' could not be merged because it had no valid polygonal parts.")
            continue

        try:
            merged_geom = unary_union(parts)
            merged_geom = repair_geometry(merged_geom, tools)
            merged_parts = extract_polygonal_parts(merged_geom, tools)

            if not merged_parts:
                errors.append(f"Class '{class_name}' resulted in empty geometry after union.")
                continue

            if len(merged_parts) == 1:
                output_geom = merged_parts[0]
            else:
                output_geom = MultiPolygon(merged_parts)

            properties: Dict[str, Any] = {
                "objectType": "annotation",
                "classification": {
                    "name": class_name,
                    "color": group["class_color"],
                },
                "merged_feature_count": len(group["features"]),
            }

            if keep_extra_properties:
                properties.update(common_extra_properties(group["features"]))

            merged_features.append(
                {
                    "type": "Feature",
                    "id": f"merged_{safe_filename_part(class_name)}",
                    "geometry": mapping(output_geom),
                    "properties": properties,
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Class '{class_name}' could not be merged: {exc}")

    return merged_features, errors



def create_parent_annotations_for_detections(
    detection_features: List[Dict[str, Any]],
    *,
    keep_extra_properties: bool = True,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Create one parent annotation per class while keeping all individual detections.

    Output structure:
    - one annotation per class, named and classified with the class name
    - all original regions are also written as individual detection objects

    In QuPath, annotations are spatial regions rather than pure logical folders.
    The output therefore gives QuPath an annotation region for each class and
    individual objects with the same classification. A custom measurement is also
    written to the annotation so the imported class count remains visible even if
    QuPath's automatic spatial counts include other overlapping objects.
    """
    tools = import_shapely_tools()
    shape = tools["shape"]
    mapping = tools["mapping"]
    unary_union = tools["unary_union"]
    MultiPolygon = tools["MultiPolygon"]

    errors: List[str] = []
    grouped: Dict[str, Dict[str, Any]] = {}

    total = max(len(detection_features), 1)
    for index, feature in enumerate(detection_features, start=1):
        check_cancel(cancel_event)
        if progress_callback and (
            index == 1 or index % PROGRESS_UPDATE_EVERY_N_FEATURES == 0 or index == total
        ):
            progress_callback(
                "Preparing parent annotations",
                min(0.78, 0.55 + 0.20 * (index / total)),
            )

        properties = feature.get("properties") or {}
        classification = properties.get("classification") or {}
        class_name = str(classification.get("name") or "Annotation")
        class_color = normalize_rgb(classification.get("color")) or list(DEFAULT_COLOR)

        group = grouped.setdefault(
            class_name,
            {
                "class_name": class_name,
                "class_color": class_color,
                "features": [],
                "parts": [],
            },
        )
        group["features"].append(feature)

        geometry = feature.get("geometry")
        if not geometry:
            errors.append(
                f"Object {index} in class '{class_name}' has no geometry and was skipped while creating the parent annotation."
            )
            continue

        try:
            geom = shape(geometry)
            geom = repair_geometry(geom, tools)
            parts = extract_polygonal_parts(geom, tools)
            if not parts:
                errors.append(
                    f"Object {index} in class '{class_name}' did not contain valid polygonal geometry."
                )
                continue
            group["parts"].extend(parts)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Object {index} in class '{class_name}' could not be processed: {exc}")

    parent_annotations: List[Dict[str, Any]] = []
    class_items = list(grouped.items())
    total_classes = max(len(class_items), 1)

    for class_index, (class_name, group) in enumerate(class_items, start=1):
        check_cancel(cancel_event)
        if progress_callback:
            progress_callback(
                f"Creating parent annotation {class_index}/{total_classes}: {class_name}",
                min(0.90, 0.78 + 0.10 * ((class_index - 1) / total_classes)),
            )

        parts = group["parts"]
        if not parts:
            errors.append(f"Class '{class_name}' could not get a parent annotation because it had no valid geometry.")
            continue

        try:
            parent_geom = unary_union(parts)
            parent_geom = repair_geometry(parent_geom, tools)
            parent_parts = extract_polygonal_parts(parent_geom, tools)

            if not parent_parts:
                errors.append(f"Class '{class_name}' resulted in an empty parent annotation.")
                continue

            if len(parent_parts) == 1:
                output_geom = parent_parts[0]
            else:
                output_geom = MultiPolygon(parent_parts)

            object_count = len(group["features"])
            properties: Dict[str, Any] = {
                "objectType": "annotation",
                "name": class_name,
                "classification": {
                    "name": class_name,
                    "color": group["class_color"],
                },
                "contained_object_count": object_count,
                "measurements": {
                    "Imported object count": object_count,
                    f"Imported Num {class_name}": object_count,
                },
            }

            if keep_extra_properties:
                properties.update(common_extra_properties(group["features"]))
                # Preserve the parent annotation name even when common properties
                # include a different or empty name.
                properties["name"] = class_name

            parent_annotations.append(
                {
                    "type": "Feature",
                    "id": f"parent_{safe_filename_part(class_name)}",
                    "geometry": mapping(output_geom),
                    "properties": properties,
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Class '{class_name}' could not get a parent annotation: {exc}")

    return parent_annotations, errors


# -----------------------------------------------------------------------------
# File conversion
# -----------------------------------------------------------------------------


def convert_geojson_file(
    input_path: Path,
    output_path: Path,
    *,
    options: ConversionOptions,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """Convert one GeoJSON file. Returns non-fatal warning/error messages."""
    errors: List[str] = []

    check_cancel(cancel_event)
    if progress_callback:
        progress_callback("Reading file", 0.02)

    with input_path.open("r", encoding="utf-8") as file:
        geojson = json.load(file)

    check_cancel(cancel_event)

    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        raise ValueError("Input is not a valid GeoJSON FeatureCollection.")

    input_features = geojson.get("features")
    if not isinstance(input_features, list):
        raise ValueError("GeoJSON FeatureCollection does not contain a valid 'features' list.")

    converted_features: List[Dict[str, Any]] = []
    total_features = max(len(input_features), 1)

    for feature_index, feature in enumerate(input_features, start=1):
        check_cancel(cancel_event)

        if progress_callback and (
            feature_index == 1
            or feature_index % PROGRESS_UPDATE_EVERY_N_FEATURES == 0
            or feature_index == total_features
        ):
            progress_callback("Converting features", min(0.55, 0.05 + 0.50 * (feature_index / total_features)))

        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            errors.append(f"Feature {feature_index} is not a valid GeoJSON Feature and was skipped.")
            continue

        make_objects = options.create_detection_objects or options.create_objects_inside_annotations
        converted = convert_feature_to_qupath(
            feature,
            keep_extra_properties=options.keep_extra_properties,
            object_type="detection" if make_objects and not options.merge_same_class else "annotation",
        )

        if options.merge_same_class:
            converted_features.append(converted)
        elif options.split_multipolygons:
            converted_features.extend(split_multipolygon_feature(converted))
        else:
            converted_features.append(converted)

    check_cancel(cancel_event)

    if options.merge_same_class:
        converted_features, merge_errors = merge_features_by_class(
            converted_features,
            keep_extra_properties=options.keep_extra_properties,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        errors.extend(merge_errors)

    elif options.create_objects_inside_annotations:
        parent_annotations, parent_errors = create_parent_annotations_for_detections(
            converted_features,
            keep_extra_properties=options.keep_extra_properties,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        errors.extend(parent_errors)
        # Put annotations first so QuPath can build the hierarchy around the objects.
        converted_features = parent_annotations + converted_features

    check_cancel(cancel_event)
    if progress_callback:
        progress_callback("Writing output", 0.92)

    output_geojson = {
        "type": "FeatureCollection",
        "features": converted_features,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file first, then atomically replace/rename.
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        if COMPACT_JSON_OUTPUT:
            json.dump(output_geojson, file, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(output_geojson, file, ensure_ascii=False, indent=2)

    os.replace(temp_path, output_path)

    check_cancel(cancel_event)
    if progress_callback:
        progress_callback("Finished file", 1.0)

    return errors


def create_output_path(input_path: Path, output_folder: Optional[Path], overwrite: bool) -> Path:
    """Create output path using the default suffix and avoiding collisions unless overwrite is selected."""
    target_folder = output_folder if output_folder is not None else input_path.parent
    candidate = target_folder / f"{input_path.stem}{DEFAULT_SUFFIX}.geojson"

    if overwrite or not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = target_folder / f"{input_path.stem}{DEFAULT_SUFFIX}_{index}.geojson"
        if not candidate.exists():
            return candidate
        index += 1


def collect_geojson_files_from_folder(folder: Path, include_subfolders: bool) -> List[Path]:
    """Collect GeoJSON/JSON files from a folder."""
    if include_subfolders:
        iterator = folder.rglob("*")
    else:
        iterator = folder.iterdir()

    files = [path for path in iterator if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS]
    return sorted(files, key=lambda p: str(p).lower())



def resource_path(relative_path: str) -> Path:
    """
    Return the correct resource path when running from source or from a
    PyInstaller one-file/one-folder executable.
    """
    try:
        base_path = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    except Exception:
        base_path = Path(__file__).resolve().parent
    return base_path / relative_path


def set_tk_window_icon(root: tk.Tk) -> None:
    """
    Set the application window icon.

    On Windows, iconbitmap works well with .ico files and also controls the
    taskbar/window icon when the app is packaged. Other platforms may ignore
    .ico files depending on the Tk build, so failures are safely ignored.
    """
    icon_file = resource_path(APP_ICON_FILENAME)
    if not icon_file.exists():
        return

    try:
        root.iconbitmap(default=str(icon_file))
    except Exception:
        pass

# -----------------------------------------------------------------------------
# GUI
# -----------------------------------------------------------------------------


class QuPathGeoJSONConverterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("QuPath GeoJSON Converter")
        set_tk_window_icon(self.root)
        self.root.geometry("980x820")
        self.root.minsize(880, 690)

        self.selected_files: List[Path] = []
        self.worker_thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.message_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()

        self.same_folder_var = tk.BooleanVar(value=True)
        self.include_subfolders_var = tk.BooleanVar(value=False)
        self.keep_extra_var = tk.BooleanVar(value=True)
        self.mode_var = tk.StringVar(value=MODE_MERGE)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.custom_output_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self._update_output_controls()
        self._update_running_state(False)
        self._refresh_option_controls()
        self.root.after(100, self._process_worker_messages)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        title = ttk.Label(header, text="QuPath GeoJSON Converter", font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            header,
            text="Convert GeoJSON classification fields into QuPath-compatible annotations and objects.",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(4, 0))

        header_buttons = ttk.Frame(header)
        header_buttons.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        self.about_button = ttk.Button(header_buttons, text="About", command=self.show_about)
        self.about_button.grid(row=0, column=0, sticky="e", padx=(0, 6))

        self.help_button = ttk.Button(header_buttons, text="Help", command=self.show_help)
        self.help_button.grid(row=0, column=1, sticky="e")

        controls = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure(6, weight=1)

        self.add_files_button = ttk.Button(controls, text="Add GeoJSON files", command=self.add_files)
        self.add_files_button.grid(row=0, column=0, padx=(0, 8), pady=4)

        self.add_folder_button = ttk.Button(controls, text="Add folder", command=self.add_folder)
        self.add_folder_button.grid(row=0, column=1, padx=(0, 8), pady=4)

        self.remove_button = ttk.Button(controls, text="Remove selected", command=self.remove_selected)
        self.remove_button.grid(row=0, column=2, padx=(0, 8), pady=4)

        self.clear_button = ttk.Button(controls, text="Clear list", command=self.clear_list)
        self.clear_button.grid(row=0, column=3, padx=(0, 8), pady=4)

        self.include_subfolders_check = ttk.Checkbutton(
            controls,
            text="Include subfolders",
            variable=self.include_subfolders_var,
        )
        self.include_subfolders_check.grid(row=0, column=4, padx=(8, 0), pady=4, sticky="w")

        middle = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        middle.grid(row=2, column=0, sticky="nsew")
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)

        files_frame = ttk.LabelFrame(middle, text="Selected files", padding=8)
        files_frame.grid(row=0, column=0, sticky="nsew")
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(0, weight=1)

        self.file_listbox = tk.Listbox(files_frame, selectmode=tk.EXTENDED, activestyle="dotbox")
        self.file_listbox.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(files_frame, orient="vertical", command=self.file_listbox.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.file_listbox.configure(yscrollcommand=y_scroll.set)

        x_scroll = ttk.Scrollbar(files_frame, orient="horizontal", command=self.file_listbox.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.file_listbox.configure(xscrollcommand=x_scroll.set)

        bottom = ttk.Frame(self.root, padding=(12, 4, 12, 12))
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        output_frame = ttk.LabelFrame(bottom, text="Output", padding=8)
        output_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        output_frame.columnconfigure(1, weight=1)

        self.same_folder_check = ttk.Checkbutton(
            output_frame,
            text="Save each converted file in the same folder as its input file",
            variable=self.same_folder_var,
            command=self._update_output_controls,
        )
        self.same_folder_check.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Label(output_frame, text="Custom output folder:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.output_entry = ttk.Entry(output_frame, textvariable=self.custom_output_var)
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.output_browse_button = ttk.Button(output_frame, text="Browse", command=self.choose_output_folder)
        self.output_browse_button.grid(row=1, column=2, sticky="e")

        options_frame = ttk.LabelFrame(bottom, text="Options", padding=8)
        options_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        options_frame.columnconfigure(0, weight=1)
        options_frame.columnconfigure(1, weight=1)

        self.keep_extra_check = ttk.Checkbutton(
            options_frame,
            text="Keep extra properties",
            variable=self.keep_extra_var,
        )
        self.keep_extra_check.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.overwrite_check = ttk.Checkbutton(
            options_frame,
            text="Overwrite existing converted files",
            variable=self.overwrite_var,
        )
        self.overwrite_check.grid(row=0, column=1, sticky="w", pady=(0, 4))

        ttk.Separator(options_frame, orient="horizontal").grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(2, 6)
        )

        ttk.Label(options_frame, text="Conversion mode (select one):").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )

        self.merge_check = ttk.Radiobutton(
            options_frame,
            text="Merge into one annotation per class",
            variable=self.mode_var,
            value=MODE_MERGE,
        )
        self.merge_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)

        self.standard_check = ttk.Radiobutton(
            options_frame,
            text="Standard QuPath annotations (no merge/split)",
            variable=self.mode_var,
            value=MODE_STANDARD,
        )
        self.standard_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=2)

        self.detection_check = ttk.Radiobutton(
            options_frame,
            text="Create individual QuPath objects for counts",
            variable=self.mode_var,
            value=MODE_OBJECTS,
        )
        self.detection_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=2)

        self.objects_inside_check = ttk.Radiobutton(
            options_frame,
            text="Create class annotations + individual objects",
            variable=self.mode_var,
            value=MODE_CLASS_ANNOTATIONS_OBJECTS,
        )
        self.objects_inside_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=2)

        self.split_check = ttk.Radiobutton(
            options_frame,
            text="Split MultiPolygon into individual Polygon features",
            variable=self.mode_var,
            value=MODE_SPLIT,
        )
        self.split_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=2)

        action_frame = ttk.Frame(bottom)
        action_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        action_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(action_frame, variable=self.progress_var, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.convert_button = ttk.Button(action_frame, text="Convert", command=self.convert_files)
        self.convert_button.grid(row=0, column=1, sticky="e")

        self.cancel_button = ttk.Button(action_frame, text="Cancel", command=self.cancel_conversion)
        self.cancel_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

        status = ttk.Label(bottom, textvariable=self.status_var)
        status.grid(row=3, column=0, sticky="w")

        self.error_frame = ttk.LabelFrame(bottom, text="Error log", padding=8)
        self.error_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.error_frame.columnconfigure(0, weight=1)
        self.error_text = tk.Text(self.error_frame, height=5, wrap="word")
        self.error_text.grid(row=0, column=0, sticky="ew")
        self.error_text.configure(state="disabled")
        self.error_frame.grid_remove()

    def _update_output_controls(self) -> None:
        is_running = self._is_worker_running()
        state = "disabled" if self.same_folder_var.get() or is_running else "normal"
        self.output_entry.configure(state=state)
        self.output_browse_button.configure(state=state)

    def _mode_checks(self) -> List[ttk.Radiobutton]:
        return [
            self.merge_check,
            self.standard_check,
            self.detection_check,
            self.objects_inside_check,
            self.split_check,
        ]

    def _refresh_option_controls(self) -> None:
        """Keep all conversion modes selectable unless a conversion is running."""
        state = "disabled" if self._is_worker_running() else "normal"
        for check in self._mode_checks():
            check.configure(state=state)

    def show_about(self) -> None:
        about_text = (
            f"QuPath GeoJSON Converter v{APP_VERSION}\n\n"
            "A lightweight graphical tool for converting GeoJSON annotation files "
            "into QuPath-compatible annotation and detection formats.\n\n"
            f"Author: {AUTHOR_NAME}\n"
            f"ORCID: {AUTHOR_ORCID}\n"
            "License: MIT\n\n"
            f"GitHub: {GITHUB_URL}\n"
            f"Zenodo DOI: {ZENODO_DOI}\n"
            f"DOI URL: {ZENODO_URL}\n\n"
            "Suggested citation:\n"
            f"{AUTHOR_NAME}. QuPath GeoJSON Converter. Version {APP_VERSION}. "
            f"Zenodo. https://doi.org/{ZENODO_DOI}\n\n"
            "This is an independent utility and is not affiliated with or endorsed by the QuPath project."
        )
        messagebox.showinfo("About", about_text)

    def show_help(self) -> None:
        help_text = (
            "QuPath GeoJSON Converter\n"
            f"Version {APP_VERSION} | DOI: {ZENODO_DOI}\n\n"
            "Input and output\n"
            "• Add one or more GeoJSON/JSON files, or add a full folder.\n"
            "• By default, converted files are saved next to each input file with the '_qupath' suffix.\n"
            "• Choose a custom output folder only if you want all converted files saved together.\n\n"
            "General options\n"
            "• Keep extra properties: keeps non-classification metadata when possible.\n"
            "• Overwrite existing converted files: replaces the existing output file instead of creating _2, _3, etc.\n\n"
            "Conversion modes\n"
            "• Merge into one annotation per class: default mode. Creates one QuPath annotation for each class. "
            "Best for tissue regions or area-level labels.\n"
            "• Standard QuPath annotations: converts each feature to a QuPath annotation without merging, splitting, or creating objects. "
            "Best when you want the safest one-to-one annotation conversion.\n"
            "• Create individual QuPath objects for counts: writes each feature as a detection object. "
            "Best when you want QuPath class counts. These objects do not appear in the Annotation list.\n"
            "• Create class annotations + individual objects: creates one annotation per class and also keeps individual objects. "
            "Useful when you want a class region plus object counts. Imported count measurements are added to the parent annotation.\n"
            "• Split MultiPolygon into individual Polygon features: keeps annotations but separates MultiPolygon geometries. "
            "Use this only if QuPath has trouble importing MultiPolygon objects.\n\n"
            "Only one conversion mode can be active at a time. Click another mode to switch directly.\n"
        )
        messagebox.showinfo("Help", help_text)

    def add_files(self) -> None:
        file_paths = filedialog.askopenfilenames(
            title="Select GeoJSON files",
            filetypes=(
                ("GeoJSON files", "*.geojson"),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ),
        )
        self._add_paths([Path(path) for path in file_paths])

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select folder containing GeoJSON files")
        if not folder:
            return
        try:
            files = collect_geojson_files_from_folder(Path(folder), self.include_subfolders_var.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Folder error", str(exc))
            return
        self._add_paths(files)

    def _add_paths(self, paths: Iterable[Path]) -> None:
        existing: set[Path] = set()
        for path in self.selected_files:
            try:
                existing.add(path.resolve())
            except Exception:
                existing.add(path.absolute())

        added = 0
        for path in paths:
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path.absolute()
            if resolved in existing:
                continue
            self.selected_files.append(path)
            existing.add(resolved)
            added += 1

        self._refresh_file_list()
        self.status_var.set(f"Added {added} file(s). Total selected: {len(self.selected_files)}.")

    def _refresh_file_list(self) -> None:
        self.file_listbox.delete(0, tk.END)
        for path in self.selected_files:
            self.file_listbox.insert(tk.END, str(path))

    def remove_selected(self) -> None:
        selected_indices = list(self.file_listbox.curselection())
        if not selected_indices:
            return

        selected_set = set(selected_indices)
        self.selected_files = [path for idx, path in enumerate(self.selected_files) if idx not in selected_set]
        self._refresh_file_list()
        self.status_var.set(f"Removed {len(selected_indices)} file(s). Total selected: {len(self.selected_files)}.")

    def clear_list(self) -> None:
        self.selected_files.clear()
        self._refresh_file_list()
        self.progress_var.set(0)
        self.status_var.set("File list cleared.")
        self._clear_errors()

    def choose_output_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.custom_output_var.set(folder)

    def _clear_errors(self) -> None:
        self.error_text.configure(state="normal")
        self.error_text.delete("1.0", tk.END)
        self.error_text.configure(state="disabled")
        self.error_frame.grid_remove()

    def _append_error(self, text: str) -> None:
        self.error_frame.grid()
        self.error_text.configure(state="normal")
        self.error_text.insert(tk.END, text.rstrip() + "\n")
        self.error_text.see(tk.END)
        self.error_text.configure(state="disabled")

    def _is_worker_running(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def _update_running_state(self, running: bool) -> None:
        normal_or_disabled = "disabled" if running else "normal"
        self.add_files_button.configure(state=normal_or_disabled)
        self.add_folder_button.configure(state=normal_or_disabled)
        self.remove_button.configure(state=normal_or_disabled)
        self.clear_button.configure(state=normal_or_disabled)
        self.include_subfolders_check.configure(state=normal_or_disabled)
        self.same_folder_check.configure(state=normal_or_disabled)
        self.keep_extra_check.configure(state=normal_or_disabled)
        self.split_check.configure(state=normal_or_disabled)
        self.merge_check.configure(state=normal_or_disabled)
        self.standard_check.configure(state=normal_or_disabled)
        self.detection_check.configure(state=normal_or_disabled)
        self.objects_inside_check.configure(state=normal_or_disabled)
        self.overwrite_check.configure(state=normal_or_disabled)
        self.convert_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self._update_output_controls()
        if not running:
            self._refresh_option_controls()

    def convert_files(self) -> None:
        if self._is_worker_running():
            return

        if not self.selected_files:
            messagebox.showwarning("No files selected", "Please add at least one GeoJSON file.")
            return

        custom_output_folder: Optional[Path] = None
        if not self.same_folder_var.get():
            output_text = self.custom_output_var.get().strip()
            if not output_text:
                messagebox.showwarning(
                    "Output folder missing",
                    "Please select a custom output folder or use the same input folder option.",
                )
                return
            custom_output_folder = Path(output_text)

        selected_mode = self.mode_var.get() or MODE_MERGE

        options = ConversionOptions(
            keep_extra_properties=self.keep_extra_var.get(),
            split_multipolygons=selected_mode == MODE_SPLIT,
            merge_same_class=selected_mode == MODE_MERGE,
            create_detection_objects=selected_mode == MODE_OBJECTS,
            create_objects_inside_annotations=selected_mode == MODE_CLASS_ANNOTATIONS_OBJECTS,
            overwrite=self.overwrite_var.get(),
        )

        files_to_convert = list(self.selected_files)

        self._clear_errors()
        self.progress_var.set(0)
        self.status_var.set("Starting conversion...")
        self.cancel_event.clear()
        self._update_running_state(True)

        self.worker_thread = threading.Thread(
            target=self._conversion_worker,
            args=(files_to_convert, custom_output_folder, options),
            daemon=True,
        )
        self.worker_thread.start()

    def cancel_conversion(self) -> None:
        if self._is_worker_running():
            self.cancel_event.set()
            self.status_var.set("Cancelling... finishing current safe step.")
            self.cancel_button.configure(state="disabled")

    def _conversion_worker(
        self,
        files_to_convert: List[Path],
        custom_output_folder: Optional[Path],
        options: ConversionOptions,
    ) -> None:
        total_files = len(files_to_convert)
        success_count = 0
        error_count = 0
        cancelled = False

        def send_progress(text: str, percent: float) -> None:
            self.message_queue.put(("progress", (text, percent)))

        try:
            for file_index, input_path in enumerate(files_to_convert, start=1):
                check_cancel(self.cancel_event)

                file_prefix = f"{file_index}/{total_files}: {input_path.name}"
                send_progress(f"Converting {file_prefix}", ((file_index - 1) / total_files) * 100)

                def local_progress(stage: str, local_fraction: float) -> None:
                    local_fraction = max(0.0, min(1.0, local_fraction))
                    overall = ((file_index - 1) + local_fraction) / total_files * 100
                    send_progress(f"{stage} — {file_prefix}", overall)

                try:
                    output_path = create_output_path(
                        input_path,
                        custom_output_folder,
                        overwrite=options.overwrite,
                    )

                    non_fatal_errors = convert_geojson_file(
                        input_path,
                        output_path,
                        options=options,
                        progress_callback=local_progress,
                        cancel_event=self.cancel_event,
                    )

                    success_count += 1
                    if non_fatal_errors:
                        error_count += len(non_fatal_errors)
                        self.message_queue.put(
                            ("error", f"{input_path.name}: converted with warnings/errors -> {output_path.name}")
                        )
                        for item in non_fatal_errors:
                            self.message_queue.put(("error", f"  - {item}"))

                except InterruptedError:
                    cancelled = True
                    raise
                except Exception as exc:  # noqa: BLE001
                    error_count += 1
                    self.message_queue.put(("error", f"{input_path.name}: FAILED"))
                    self.message_queue.put(("error", f"  - {exc}"))
                    traceback.print_exc()

            self.message_queue.put(
                (
                    "done",
                    {
                        "success_count": success_count,
                        "total_files": total_files,
                        "error_count": error_count,
                        "cancelled": False,
                    },
                )
            )
        except InterruptedError:
            cancelled = True
            self.message_queue.put(
                (
                    "done",
                    {
                        "success_count": success_count,
                        "total_files": total_files,
                        "error_count": error_count,
                        "cancelled": cancelled,
                    },
                )
            )

    def _process_worker_messages(self) -> None:
        try:
            while True:
                kind, payload = self.message_queue.get_nowait()

                if kind == "progress":
                    text, percent = payload
                    self.status_var.set(text)
                    self.progress_var.set(percent)
                elif kind == "error":
                    self._append_error(str(payload))
                elif kind == "done":
                    self._handle_done(payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._process_worker_messages)

    def _handle_done(self, payload: Dict[str, Any]) -> None:
        success_count = int(payload.get("success_count", 0))
        total_files = int(payload.get("total_files", 0))
        error_count = int(payload.get("error_count", 0))
        cancelled = bool(payload.get("cancelled", False))

        self.progress_var.set(100 if not cancelled else self.progress_var.get())
        self._update_running_state(False)

        if cancelled:
            self.status_var.set(f"Cancelled. Converted {success_count}/{total_files} file(s).")
            messagebox.showinfo("Conversion cancelled", f"Cancelled. Converted {success_count}/{total_files} file(s).")
            return

        if error_count == 0:
            self.status_var.set(f"Done. Converted {success_count}/{total_files} file(s) without errors.")
            messagebox.showinfo("Conversion complete", f"Converted {success_count}/{total_files} file(s) without errors.")
        else:
            self.status_var.set(
                f"Done. Converted {success_count}/{total_files} file(s), with {error_count} warning/error item(s)."
            )
            messagebox.showwarning(
                "Conversion complete with errors",
                "Some files were converted with warnings/errors. Check the error log at the bottom.",
            )


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if sys.platform.startswith("win"):
            style.theme_use("vista")
    except Exception:
        pass

    QuPathGeoJSONConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
