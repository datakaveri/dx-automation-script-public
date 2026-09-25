import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import rasterio
from rasterio.warp import transform_bounds

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("stac_ingestion")


def load_config(config_path: str) -> Dict[str, Any]:
    """Load and validate system configuration file."""
    path = Path(config_path)
    if not path.is_file():
        logger.error(f"Configuration file not found: {config_path}")
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_tif_files(folder_path: str) -> List[str]:
    """Recursively discover GeoTIFF files within a directory."""
    folder = Path(folder_path)
    if not folder.exists():
        logger.error(f"Input directory does not exist: {folder_path}")
        return []
    
    tif_files = [
        str(p) for p in folder.rglob("*") 
        if p.suffix.lower() in [".tif", ".tiff"] and p.is_file()
    ]
    return tif_files


def extract_metadata(tif_path: str) -> Dict[str, Any]:
    """Extract spatial extent, CRS, resolution, and raster metadata from a GeoTIFF."""
    with rasterio.open(tif_path) as src:
        bounds = src.bounds
        crs = src.crs
        width = src.width
        height = src.height
        transform = src.transform

        if crs and crs.to_epsg() != 4326:
            bbox_4326 = transform_bounds(crs, "EPSG:4326", *bounds)
        else:
            bbox_4326 = bounds

        minx, maxx = sorted([bbox_4326[0], bbox_4326[2]])
        miny, maxy = sorted([bbox_4326[1], bbox_4326[3]])

        geom = {
            "type": "Polygon",
            "coordinates": [[
                [minx, miny],
                [maxx, miny],
                [maxx, maxy],
                [minx, maxy],
                [minx, miny]
            ]]
        }

        return {
            "bbox": [minx, miny, maxx, maxy],
            "geometry": geom,
            "width": width,
            "height": height,
            "crs": crs.to_string() if crs else "EPSG:4326",
            "transform": list(transform),
            "bands": src.count,
            "dtypes": list(src.dtypes),
            "nodata": src.nodata,
            "bounds_raw": (bounds.left, bounds.bottom, bounds.right, bounds.top)
        }


def create_feature(tif_path: str, metadata: Dict[str, Any], collection_id: str, href_prefix: str) -> Dict[str, Any]:
    """Build a STAC Item (Feature) representation for a given raster asset."""
    file_path = Path(tif_path)
    file_id = file_path.stem
    asset_id = str(uuid.uuid4())

    modified_time = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    asset_href = f"{href_prefix}/{file_path.name}"

    res_x = round(metadata["transform"][0], 3)
    res_y = round(abs(metadata["transform"][4]), 3)

    return {
        "type": "Feature",
        "id": file_id,
        "bbox": metadata["bbox"],
        "geometry": metadata["geometry"],
        "assets": {
            asset_id: {
                "title": f"LAI 2015 {file_id}",
                "description": "High-resolution GeoTIFF data asset.",
                "href": asset_href,
                "type": "image/tiff",
                "s3BucketId": "default",
                "roles": ["data"],
                "size": file_path.stat().st_size
            }
        },
        "properties": {
            "datetime": modified_time,
            "driver": "GTiff",
            "description": "LAI 2015 Image Metadata",
            "number_of_bands": metadata["bands"],
            "raster_width": metadata["width"],
            "raster_height": metadata["height"],
            "projection": metadata["crs"],
            "spatial_resolution_x": f"{res_x}m",
            "spatial_resolution_y": f"{res_y}m",
            "geo_transform_origin_x": metadata["transform"][2],
            "geo_transform_origin_y": metadata["transform"][5],
            "bounds_min_x": min(metadata["bounds_raw"][0], metadata["bounds_raw"][2]),
            "bounds_max_x": max(metadata["bounds_raw"][0], metadata["bounds_raw"][2]),
            "bounds_min_y": min(metadata["bounds_raw"][1], metadata["bounds_raw"][3]),
            "bounds_max_y": max(metadata["bounds_raw"][1], metadata["bounds_raw"][3])
        },
        "collection": collection_id
    }


def create_collection(
    collection_id: str, 
    bbox: List[float], 
    start_datetime: str, 
    end_datetime: str, 
    title: str, 
    description: str
) -> Dict[str, Any]:
    """Generate the root STAC Collection specification object."""
    return {
        "type": "Collection",
        "id": collection_id,
        "title": title,
        "description": description,
        "license": "proprietary",
        "extent": {
            "spatial": {
                "bbox": [bbox],
                "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
            },
            "temporal": {
                "interval": [[start_datetime, end_datetime]]
            }
        }
    }


def push_collection_to_api(collection: Dict[str, Any], api_url: str, auth_token: str, timeout: int = 60) -> bool:
    """HTTP POST the root STAC Collection entity to the remote STAC endpoint."""
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(api_url, json=collection, headers=headers, timeout=timeout)
        if response.status_code in [200, 201]:
            logger.info(f"Collection '{collection['id']}' posted successfully (Status: {response.status_code}).")
            return True
        else:
            logger.error(f"Failed to post collection. Status: {response.status_code}, Body: {response.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"HTTP request exception during collection posting: {str(e)}")
        return False


def push_items_batch_to_api(
    features: List[Dict[str, Any]], 
    collection_id: str, 
    api_base_url: str, 
    auth_token: str, 
    timeout: int = 60
) -> bool:
    """HTTP POST a FeatureCollection payload containing all STAC Items to the remote endpoint."""
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    items_url = f"{api_base_url.rstrip('/')}/collections/{collection_id}/items"
    feature_collection = {
        "type": "FeatureCollection",
        "features": features
    }

    try:
        logger.info(f"Posting batch payload of {len(features)} item(s) to: {items_url}")
        response = requests.post(items_url, json=feature_collection, headers=headers, timeout=timeout)
        if response.status_code in [200, 201]:
            logger.info(f"Batch item payload posted successfully (Status: {response.status_code}).")
            return True
        else:
            logger.error(f"Failed to post item batch. Status: {response.status_code}, Body: {response.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"HTTP request exception during batch item posting: {str(e)}")
        return False


def save_to_local_files(collection: Dict[str, Any], features: List[Dict[str, Any]], output_folder: str) -> Tuple[str, str]:
    """Persist STAC Collection and FeatureCollection local JSON representations to disk."""
    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    collection_path = out_dir / "collection.json"
    with open(collection_path, "w", encoding="utf-8") as f:
        json.dump(collection, f, indent=4)
    logger.info(f"Saved collection metadata locally to {collection_path}")

    features_path = out_dir / "features.json"
    feature_collection = {
        "type": "FeatureCollection",
        "features": features
    }
    with open(features_path, "w", encoding="utf-8") as f:
        json.dump(feature_collection, f, indent=4)
    logger.info(f"Saved {len(features)} feature metadata item(s) locally to {features_path}")

    return str(collection_path), str(features_path)


def run_pipeline(config_path: str) -> None:
    """Main execution control loop."""
    config = load_config(config_path)

    input_folder = config["paths"]["input_dir"]
    output_folder = config["paths"]["output_dir"]
    collection_id = config["stac"]["collection_id"]
    href_prefix = config["stac"].get("href_prefix_override") or collection_id
    collection_title = config["stac"].get("title", "STAC Collection")
    collection_desc = config["stac"].get("description", "STAC Collection Description")

    api_cfg = config.get("api", {})
    push_enabled = api_cfg.get("push_to_api", False)
    timeout = api_cfg.get("timeout_seconds", 60)

    tif_files = find_tif_files(input_folder)
    if not tif_files:
        logger.warning(f"No GeoTIFF files found in target folder: {input_folder}")
        return

    logger.info(f"Found {len(tif_files)} GeoTIFF file(s). Beginning extraction process...")

    features = []
    all_bounds = []
    all_dates = []

    for tif in tif_files:
        try:
            metadata = extract_metadata(tif)
            feature = create_feature(tif, metadata, collection_id, href_prefix)
            features.append(feature)
            all_bounds.append(metadata["bbox"])
            all_dates.append(feature["properties"]["datetime"])
            logger.info(f"Processed file: {Path(tif).name}")
        except Exception as e:
            logger.error(f"Error processing file {Path(tif).name}: {str(e)}")
            continue

    if not features:
        logger.error("Processing aborted: No valid STAC features generated.")
        return

    # Calculate global temporal and spatial bounds
    all_minx = min(b[0] for b in all_bounds)
    all_miny = min(b[1] for b in all_bounds)
    all_maxx = max(b[2] for b in all_bounds)
    all_maxy = max(b[3] for b in all_bounds)

    temporal_start = min(all_dates)
    temporal_end = max(all_dates)

    collection = create_collection(
        collection_id=collection_id,
        bbox=[all_minx, all_miny, all_maxx, all_maxy],
        start_datetime=temporal_start,
        end_datetime=temporal_end,
        title=collection_title,
        description=collection_desc
    )

    save_to_local_files(collection, features, output_folder)

    if push_enabled:
        api_url = api_cfg.get("api_url")
        api_base = api_cfg.get("api_base")
        auth_token = api_cfg.get("auth_token")

        if not auth_token:
            logger.error("Authentication token missing in configuration. Skipping remote upload.")
            return

        logger.info("Executing API ingestion pipeline...")
        col_success = push_collection_to_api(collection, api_url, auth_token, timeout=timeout)
        if col_success:
            push_items_batch_to_api(features, collection_id, api_base, auth_token, timeout=timeout)
    else:
        logger.info("API execution skipped (push_to_api flag is disabled).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest GeoTIFFs into STAC specification format.")
    parser.add_argument(
        "-c", "--config", 
        default="config.json", 
        help="Path to JSON configuration file (default: config.json)"
    )
    args = parser.parse_args()

    run_pipeline(args.config)