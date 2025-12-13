#!/usr/bin/env python3
"""
Download and extract NZ coastline data from Natural Earth.
Creates a GeoJSON file suitable for the test_client.py land avoidance feature.
"""

import json
import urllib.request
import zipfile
import io
import os
import sys

# Natural Earth 10m coastline (high resolution)
NATURAL_EARTH_URL = "https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip"

# Bounding box for NZ region (covers North and South Island with some margin)
NZ_BOUNDS = {
    "min_lat": -47.5,
    "max_lat": -34.0,
    "min_lon": 166.0,
    "max_lon": 179.0
}

# Tighter bounds for Auckland region (for smaller file)
AUCKLAND_BOUNDS = {
    "min_lat": -37.2,
    "max_lat": -36.4,
    "min_lon": 174.4,
    "max_lon": 175.4
}


def download_natural_earth():
    """Download Natural Earth land shapefile."""
    print(f"Downloading Natural Earth data from {NATURAL_EARTH_URL}...")
    req = urllib.request.Request(NATURAL_EARTH_URL, headers={'User-Agent': 'WindsurferTracker/1.0'})

    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read()

    print(f"Downloaded {len(data) / 1024 / 1024:.1f} MB")
    return data


def extract_geojson_from_shapefile(zip_data, bounds):
    """Extract GeoJSON from shapefile zip, filtering to bounds.

    Note: This requires the shapefile to be in GeoJSON format or we need
    a shapefile parser. Natural Earth also provides GeoJSON directly.
    """
    # Try to get GeoJSON version instead
    geojson_url = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_land.geojson"

    print(f"Downloading GeoJSON from {geojson_url}...")
    req = urllib.request.Request(geojson_url, headers={'User-Agent': 'WindsurferTracker/1.0'})

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            geojson = json.loads(response.read().decode('utf-8'))
        print("Downloaded GeoJSON successfully")
        return geojson
    except Exception as e:
        print(f"Failed to download GeoJSON: {e}")
        return None


def filter_to_bounds(geojson, bounds):
    """Filter GeoJSON features to those within bounds."""
    filtered_features = []

    for feature in geojson.get('features', []):
        geom = feature.get('geometry', {})
        geom_type = geom.get('type', '')

        if geom_type == 'Polygon':
            if polygon_intersects_bounds(geom['coordinates'][0], bounds):
                # Clip polygon to bounds
                clipped = clip_polygon_to_bounds(geom['coordinates'][0], bounds)
                if clipped and len(clipped) >= 3:
                    filtered_features.append({
                        'type': 'Feature',
                        'geometry': {
                            'type': 'Polygon',
                            'coordinates': [clipped]
                        }
                    })

        elif geom_type == 'MultiPolygon':
            for poly_coords in geom['coordinates']:
                if polygon_intersects_bounds(poly_coords[0], bounds):
                    clipped = clip_polygon_to_bounds(poly_coords[0], bounds)
                    if clipped and len(clipped) >= 3:
                        filtered_features.append({
                            'type': 'Feature',
                            'geometry': {
                                'type': 'Polygon',
                                'coordinates': [clipped]
                            }
                        })

    return {
        'type': 'FeatureCollection',
        'features': filtered_features
    }


def polygon_intersects_bounds(coords, bounds):
    """Check if any point in polygon is within or near bounds."""
    margin = 0.5  # Add margin for edge cases
    for lon, lat in coords:
        if (bounds['min_lat'] - margin <= lat <= bounds['max_lat'] + margin and
            bounds['min_lon'] - margin <= lon <= bounds['max_lon'] + margin):
            return True
    return False


def clip_polygon_to_bounds(coords, bounds):
    """Simple clipping - keep points within expanded bounds."""
    margin = 0.1
    clipped = []
    for lon, lat in coords:
        if (bounds['min_lat'] - margin <= lat <= bounds['max_lat'] + margin and
            bounds['min_lon'] - margin <= lon <= bounds['max_lon'] + margin):
            clipped.append([lon, lat])
    return clipped


def simplify_polygon(coords, tolerance=0.001):
    """Simple Douglas-Peucker-like simplification."""
    if len(coords) <= 10:
        return coords

    # Keep every Nth point based on tolerance
    step = max(1, int(len(coords) * tolerance * 10))
    simplified = coords[::step]

    # Ensure closed polygon
    if simplified[0] != simplified[-1]:
        simplified.append(simplified[0])

    return simplified


def convert_to_simple_format(geojson, bounds):
    """Convert to our simple land_polygons format."""
    polygons = []

    for feature in geojson.get('features', []):
        geom = feature.get('geometry', {})
        if geom.get('type') == 'Polygon':
            coords = geom['coordinates'][0]
            # Convert from [lon, lat] to [lat, lon] and simplify
            polygon = [[c[1], c[0]] for c in coords]
            if len(polygon) >= 3:
                # Simplify large polygons
                if len(polygon) > 100:
                    polygon = simplify_polygon(polygon, 0.0005)
                polygons.append(polygon)

    return {
        'bounds': bounds,
        'land_polygons': polygons
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Download NZ coastline data")
    parser.add_argument("--output", "-o", default="coastline_nz.json",
                        help="Output file path")
    parser.add_argument("--region", choices=["nz", "auckland"], default="auckland",
                        help="Region to extract (nz=all NZ, auckland=Auckland area only)")
    args = parser.parse_args()

    bounds = AUCKLAND_BOUNDS if args.region == "auckland" else NZ_BOUNDS

    print(f"Extracting {args.region} region: {bounds}")

    # Download and parse GeoJSON
    geojson = extract_geojson_from_shapefile(None, bounds)
    if not geojson:
        print("Failed to download coastline data")
        sys.exit(1)

    # Filter to bounds
    print("Filtering to region bounds...")
    filtered = filter_to_bounds(geojson, bounds)
    print(f"Found {len(filtered['features'])} polygons in region")

    # Convert to simple format
    print("Converting to simple format...")
    simple = convert_to_simple_format(filtered, bounds)
    print(f"Created {len(simple['land_polygons'])} land polygons")

    # Calculate total points
    total_points = sum(len(p) for p in simple['land_polygons'])
    print(f"Total points: {total_points}")

    # Save
    with open(args.output, 'w') as f:
        json.dump(simple, f)

    file_size = os.path.getsize(args.output) / 1024
    print(f"Saved to {args.output} ({file_size:.1f} KB)")


if __name__ == "__main__":
    main()
