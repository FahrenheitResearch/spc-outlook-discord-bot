import json
import argparse
import dataclasses
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402
from shapely.geometry import Point, Polygon  # noqa: E402


DAY1_HTML = """
<html>
<head><title>Storm Prediction Center Jun 13, 2026 1300 UTC Day 1 Convective Outlook</title></head>
<body>
<script>
function show_tab(nam) {
  document.getElementById("main").src = "day1" + nam + ".png";
}
</script>
<td OnClick="show_tab('otlk_1300')"><a>Categorical</a></td>
<td OnClick="show_tab('probotlk_1300_torn')"><a>Tornado</a></td>
<td OnClick="show_tab('probotlk_1300_wind')"><a>Wind</a></td>
<td OnClick="show_tab('probotlk_1300_hail')"><a>Hail</a></td>
Updated:&nbsp;Sat Jun 13 12:53:59 UTC 2026
<a href="archive/2026/KWNSPTSDY1_202606131300.txt">WUUS01 PTSDY1</a>
</body>
</html>
"""


DAY48_HTML = """
<html>
<head><title>Storm Prediction Center Jun 13, 2026 Day 4-8 Severe Weather Outlook</title></head>
<body>
<script>
function show_tab(nam) {
  document.getElementById("main").src = "day" + nam + "prob.gif";
}
</script>
<td><a href="#" onClick="show_tab('48')">D4-8</a></td>
<td><a href="#" onClick="show_tab('4')">D4</a></td>
<td><a href="#" onClick="show_tab('5')">D5</a></td>
<td><a href="#" onClick="show_tab('6')">D6</a></td>
<td><a href="#" onClick="show_tab('7')">D7</a></td>
<td><a href="#" onClick="show_tab('8')">D8</a></td>
Updated:&nbsp;Sat Jun 13 07:56:03 UTC 2026
<a href="/products/exper/day4-8/archive/2026/KWNSPTSD48_20260613.txt">WUUS48 PTSD48</a>
</body>
</html>
"""


PTS_DAY1_TEXT = """
WUUS01 KWNS 131630
PTSDY1

DAY 1 CONVECTIVE OUTLOOK AREAL OUTLINE
NWS STORM PREDICTION CENTER NORMAN OK
1130 AM CDT SAT JUN 13 2026

VALID TIME 131630Z - 141200Z

PROBABILISTIC OUTLOOK POINTS DAY 1

... TORNADO ...

0.02 34210021 35009940 34309880 34210021
&&

... WIND ...

0.15 36009600 37009500 38009600 36009600
CIG1 37009700 38009600 37009600 37009700
&&

... HAIL ...

0.05 31509820 32009750 31209720 31509820
&&

CATEGORICAL OUTLOOK POINTS DAY 1

... CATEGORICAL ...

TSTM 30000080 31000120 31509950 30000080
MRGL 34210021 35009940 34309880 34210021
SLGT 36009600 37009500 38009600 36009600
&&
"""


PTS_DAY48_TEXT = """
WUUS48 KWNS 130753
PTSD48

DAY 4-8 CONVECTIVE OUTLOOK AREAL OUTLINE
NWS STORM PREDICTION CENTER NORMAN OK
0253 AM CDT SAT JUN 13 2026

VALID TIME 161200Z - 211200Z

SEVERE WEATHER OUTLOOK POINTS DAY 4

... ANY SEVERE ...

&&

SEVERE WEATHER OUTLOOK POINTS DAY 5

... ANY SEVERE ...

0.15 37409605 37799664 38279696 37409605
&&

SEVERE WEATHER OUTLOOK POINTS DAY 6

... ANY SEVERE ...

0.30 39508546 41138466 41298383 39508546
&&
"""


GEOJSON_LAYER = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "LABEL": "TSTM",
                    "LABEL2": "General Thunderstorms Risk",
                    "fill": "#C1E9C1",
                    "stroke": "#55BB55",
                    "ISSUE": "202606140447",
                    "VALID": "202606151200",
                    "EXPIRE": "202606161200",
                    "ISSUE_ISO": "2026-06-14T04:47:00+00:00",
                    "VALID_ISO": "2026-06-15T12:00:00+00:00",
                    "EXPIRE_ISO": "2026-06-16T12:00:00+00:00",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-84.0, 30.0], [-83.0, 30.0], [-83.0, 31.0], [-84.0, 30.0]]],
                },
            }
        ],
    }
)


class ParserTests(unittest.TestCase):
    def test_day1_image_urls_follow_current_issue_time(self) -> None:
        spec = bot.BUNDLES[0]
        images = bot.parse_image_urls(DAY1_HTML, spec)

        self.assertEqual(
            images,
            [
                ("categorical", "https://www.spc.noaa.gov/products/outlook/day1otlk_1300.png"),
                ("tornado", "https://www.spc.noaa.gov/products/outlook/day1probotlk_1300_torn.png"),
                ("wind", "https://www.spc.noaa.gov/products/outlook/day1probotlk_1300_wind.png"),
                ("hail", "https://www.spc.noaa.gov/products/outlook/day1probotlk_1300_hail.png"),
            ],
        )

    def test_day48_image_urls_include_combined_and_individual_days(self) -> None:
        spec = bot.BUNDLES[3]
        images = bot.parse_image_urls(DAY48_HTML, spec)

        self.assertEqual([label for label, _ in images], ["day4-8", "day4", "day5", "day6", "day7", "day8"])
        self.assertEqual(images[0][1], "https://www.spc.noaa.gov/products/exper/day4-8/day48prob.gif")
        self.assertEqual(images[-1][1], "https://www.spc.noaa.gov/products/exper/day4-8/day8prob.gif")

    def test_product_id_and_updated_are_extracted(self) -> None:
        spec = bot.BUNDLES[0]
        title = bot.extract_title(DAY1_HTML, spec.name)
        updated = bot.extract_updated(DAY1_HTML)
        product_id = bot.extract_product_id(DAY1_HTML, spec, title, updated)

        self.assertIn("1300 UTC Day 1", title)
        self.assertEqual(updated, "Sat Jun 13 12:53:59 UTC 2026")
        self.assertEqual(product_id, "PTSDY1202606131300")

    def test_multipart_has_payload_and_files(self) -> None:
        image = bot.MapImage(
            label="categorical",
            url="https://www.spc.noaa.gov/products/outlook/day1otlk_1300.png",
            filename="day1_categorical.png",
            content_type="image/png",
            sha256="abc",
            data=b"not-real-image",
        )

        body, content_type = bot.multipart_body({"username": "Fast Severe Outlook Bot"}, (image,))

        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="payload_json"', body)
        self.assertIn(b'name="files[0]"; filename="day1_categorical.png"', body)
        self.assertIn(json.dumps({"username": "Fast Severe Outlook Bot"}).encode("utf-8"), body)

    def test_pts_coord_parser_handles_longitudes_west_of_100(self) -> None:
        self.assertEqual(bot.parse_pts_coord("37009500"), (-95.0, 37.0))
        self.assertEqual(bot.parse_pts_coord("34210021"), (-100.21, 34.21))
        self.assertEqual(bot.parse_pts_coord("30000080"), (-100.8, 30.0))

    def test_pts_text_parser_extracts_maps_and_metadata(self) -> None:
        product = bot.parse_pts_text(PTS_DAY1_TEXT, bot.BUNDLES[0])

        self.assertEqual(product.product_id, "PTSDY1:131630Z")
        self.assertEqual(product.issued, "1130 AM CDT SAT JUN 13 2026")
        self.assertEqual(product.valid, "131630Z - 141200Z")
        self.assertEqual(set(product.maps), {"categorical", "tornado", "wind", "hail"})
        self.assertIn("MRGL", product.maps["categorical"])
        self.assertIn("0.02", product.maps["tornado"])
        self.assertIn("0.15", product.maps["wind"])
        self.assertIn("CIG1", product.maps["wind"])
        self.assertEqual(product.maps["categorical"]["MRGL"][0][0], (-100.21, 34.21))

    def test_open_pts_contours_close_to_right_side_without_chord(self) -> None:
        points = [
            (-112.45, 31.26),
            (-104.01, 38.92),
            (-96.61, 33.99),
            (-83.67, 33.65),
            (-74.59, 36.38),
        ]

        repaired = bot.close_open_pts_contour(points)
        closure = bot.boundary_path(points[-1], points[0], bot.MAP_EXTENT, clockwise=True)

        self.assertIsNotNone(repaired)
        self.assertIn((-66.0, 24.0), closure)
        self.assertTrue(repaired.contains(Point((-100.0, 30.5))))
        self.assertTrue(repaired.contains(Point((-82.46, 27.95))))
        self.assertFalse(repaired.contains(Point((-100.0, 45.0))))

    def test_current_style_tstm_open_contour_does_not_flood_northern_conus(self) -> None:
        points = [
            (-113.26, 31.79), (-113.74, 32.70), (-114.11, 33.45), (-114.66, 35.26),
            (-114.91, 35.97), (-115.33, 36.39), (-115.59, 36.58), (-115.91, 36.64),
            (-116.52, 36.53), (-117.04, 36.38), (-117.51, 36.27), (-117.90, 36.18),
            (-118.36, 36.21), (-118.95, 36.41), (-119.97, 37.21), (-120.75, 38.09),
            (-121.00, 38.47), (-120.84, 39.24), (-120.64, 39.59), (-120.45, 39.62),
            (-119.82, 39.57), (-119.33, 39.31), (-118.76, 39.30), (-118.04, 39.36),
            (-117.39, 39.64), (-116.61, 40.01), (-116.07, 40.22), (-115.14, 40.46),
            (-114.52, 40.49), (-113.81, 40.49), (-113.23, 40.33), (-112.23, 40.28),
            (-111.07, 40.42), (-110.45, 40.54), (-109.34, 40.58), (-108.39, 40.53),
            (-106.94, 40.56), (-104.93, 40.71), (-103.89, 40.67), (-102.66, 40.34),
            (-102.12, 39.92), (-101.71, 39.28), (-101.67, 38.86), (-101.75, 38.57),
            (-102.01, 36.69), (-101.98, 36.44), (-101.95, 36.14), (-101.75, 35.93),
            (-100.85, 35.35), (-99.68, 35.04), (-98.00, 35.03), (-94.93, 34.84),
            (-92.73, 34.69), (-91.89, 34.77), (-91.15, 35.32), (-90.14, 36.42),
            (-87.97, 39.72), (-85.98, 41.76), (-83.92, 43.29), (-81.35, 44.22),
        ]

        repaired = bot.repaired_open_pts_geometry(points)

        self.assertIsNotNone(repaired)
        self.assertTrue(repaired.contains(Point((-104.99, 39.74))))  # Denver
        self.assertTrue(repaired.contains(Point((-96.80, 32.78))))  # Dallas
        self.assertTrue(repaired.contains(Point((-119.81, 39.53))))  # Reno
        self.assertFalse(repaired.contains(Point((-100.78, 46.81))))  # Bismarck
        self.assertFalse(repaired.contains(Point((-122.33, 47.61))))  # Seattle
        self.assertFalse(repaired.contains(Point((-87.63, 41.88))))  # Chicago

    def test_current_style_slgt_open_contour_does_not_pick_conus_complement(self) -> None:
        points = [
            (-70.62, 46.17), (-69.80, 45.45), (-69.71, 45.12), (-69.93, 44.66),
            (-71.53, 43.36), (-72.31, 42.21), (-73.11, 40.01),
        ]

        repaired = bot.repaired_open_pts_geometry(points)

        self.assertIsNotNone(repaired)
        self.assertLess(repaired.area, 40.0)
        self.assertFalse(repaired.contains(Point((-100.78, 46.81))))  # Bismarck
        self.assertFalse(repaired.contains(Point((-122.33, 47.61))))  # Seattle
        self.assertFalse(repaired.contains(Point((-104.99, 39.74))))  # Denver
        self.assertFalse(repaired.contains(Point((-96.80, 32.78))))  # Dallas

    def test_current_style_mrgl_open_contour_does_not_pick_conus_complement(self) -> None:
        points = [
            (-67.10, 45.73), (-69.49, 44.20), (-70.77, 43.22), (-71.68, 40.67),
        ]

        repaired = bot.repaired_open_pts_geometry(points)

        self.assertIsNotNone(repaired)
        self.assertLess(repaired.area, 20.0)
        self.assertFalse(repaired.contains(Point((-100.78, 46.81))))  # Bismarck
        self.assertFalse(repaired.contains(Point((-122.33, 47.61))))  # Seattle
        self.assertFalse(repaired.contains(Point((-104.99, 39.74))))  # Denver
        self.assertFalse(repaired.contains(Point((-96.80, 32.78))))  # Dallas

    def test_mature_pts_polygonizer_groups_split_open_contours(self) -> None:
        segments = (
            (
                (-70.62, 46.17), (-69.80, 45.45), (-69.71, 45.12), (-69.93, 44.66),
                (-71.53, 43.36), (-72.31, 42.21), (-73.11, 40.01),
            ),
            (
                (-75.05, 36.21), (-75.95, 35.79), (-78.26, 34.43), (-78.83, 34.23),
                (-79.95, 34.53), (-83.59, 33.80), (-84.55, 33.71), (-87.07, 34.20),
                (-87.75, 34.60), (-87.73, 34.90), (-87.38, 35.30), (-86.07, 35.64),
                (-83.86, 35.85), (-81.24, 36.55), (-80.36, 37.35), (-80.12, 38.28),
                (-80.70, 38.71), (-81.62, 39.00), (-83.56, 38.42), (-84.37, 38.58),
                (-84.94, 38.94), (-84.89, 39.38), (-82.64, 41.16), (-81.47, 42.50),
            ),
        )

        geometry = bot.pts_sequences_to_geometry(segments)

        self.assertIsNotNone(geometry)
        self.assertLess(geometry.area, 150.0)
        self.assertFalse(geometry.contains(Point((-100.78, 46.81))))  # Bismarck
        self.assertFalse(geometry.contains(Point((-122.33, 47.61))))  # Seattle
        self.assertFalse(geometry.contains(Point((-104.99, 39.74))))  # Denver
        self.assertFalse(geometry.contains(Point((-96.80, 32.78))))  # Dallas
        self.assertTrue(geometry.contains(Point((-80.84, 35.23))))  # Charlotte

    def test_mature_pts_polygonizer_uses_marine_boundary_for_general_thunder(self) -> None:
        segments = (
            (
                (-113.26, 31.79), (-113.74, 32.70), (-114.11, 33.45), (-114.66, 35.26),
                (-114.91, 35.97), (-115.33, 36.39), (-115.59, 36.58), (-115.91, 36.64),
                (-116.52, 36.53), (-117.04, 36.38), (-117.51, 36.27), (-117.90, 36.18),
                (-118.36, 36.21), (-118.95, 36.41), (-119.97, 37.21), (-120.75, 38.09),
                (-121.00, 38.47), (-120.84, 39.24), (-120.64, 39.59), (-120.45, 39.62),
                (-119.82, 39.57), (-119.33, 39.31), (-118.76, 39.30), (-118.04, 39.36),
                (-117.39, 39.64), (-116.61, 40.01), (-116.07, 40.22), (-115.14, 40.46),
                (-114.52, 40.49), (-113.81, 40.49), (-113.23, 40.33), (-112.23, 40.28),
                (-111.07, 40.42), (-110.45, 40.54), (-109.34, 40.58), (-108.39, 40.53),
                (-106.94, 40.56), (-104.93, 40.71), (-103.89, 40.67), (-102.66, 40.34),
                (-102.12, 39.92), (-101.71, 39.28), (-101.67, 38.86), (-101.75, 38.57),
                (-102.01, 36.69), (-101.98, 36.44), (-101.95, 36.14), (-101.75, 35.93),
                (-100.85, 35.35), (-99.68, 35.04), (-98.00, 35.03), (-94.93, 34.84),
                (-92.73, 34.69), (-91.89, 34.77), (-91.15, 35.32), (-90.14, 36.42),
                (-87.97, 39.72), (-85.98, 41.76), (-83.92, 43.29), (-81.35, 44.22),
            ),
        )

        geometry = bot.pts_sequences_to_geometry(segments)

        self.assertIsNotNone(geometry)
        self.assertFalse(geometry.contains(Point((-122.33, 47.61))))  # Seattle
        self.assertFalse(geometry.contains(Point((-100.78, 46.81))))  # Bismarck
        self.assertTrue(geometry.contains(Point((-104.99, 39.74))))  # Denver
        self.assertTrue(geometry.contains(Point((-96.80, 32.78))))  # Dallas

    def test_non_overlapping_outlook_fills_remove_higher_risk_from_lower_fill(self) -> None:
        raw_geometries = {
            "TSTM": Polygon(((0, 0), (4, 0), (4, 4), (0, 4))),
            "SLGT": Polygon(((1, 1), (3, 1), (3, 3), (1, 3))),
        }

        visible = bot.non_overlapping_outlook_fills(raw_geometries, bot.RISK_ORDER)

        self.assertIn("TSTM", visible)
        self.assertIn("SLGT", visible)
        self.assertAlmostEqual(visible["TSTM"].area, 12.0)
        self.assertAlmostEqual(visible["SLGT"].area, 4.0)
        self.assertFalse(visible["TSTM"].contains(Point((2, 2))))
        self.assertTrue(visible["SLGT"].contains(Point((2, 2))))

    def test_day48_pts_preserves_probability_labels(self) -> None:
        product = bot.parse_pts_text(PTS_DAY48_TEXT, bot.BUNDLES[3])

        self.assertIn("0.15", product.maps["day5"])
        self.assertIn("0.30", product.maps["day6"])
        self.assertIn("0.15", product.maps["day4-8"])
        self.assertIn("0.30", product.maps["day4-8"])
        self.assertIn("DAY48_OUTLOOK", bot.risk_labels_from_product(product))

    def test_direct_live_geojson_fetch_uses_bowecho_style_layer_urls(self) -> None:
        spec = bot.BUNDLES[1]
        seen_urls: list[str] = []
        original_fetch_text = bot.fetch_text
        try:
            def fake_fetch_text(url: str, timeout: int = 20) -> str:
                seen_urls.append(url)
                return GEOJSON_LAYER

            bot.fetch_text = fake_fetch_text
            product = bot.fetch_direct_geojson_product_for_spec(spec)
        finally:
            bot.fetch_text = original_fetch_text

        self.assertEqual(
            seen_urls,
            [
                "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.lyr.geojson",
                "https://www.spc.noaa.gov/products/outlook/day2otlk_torn.lyr.geojson",
                "https://www.spc.noaa.gov/products/outlook/day2otlk_wind.lyr.geojson",
                "https://www.spc.noaa.gov/products/outlook/day2otlk_hail.lyr.geojson",
            ],
        )
        self.assertEqual(product.product_id, "geojson:day2:202606140447")
        self.assertEqual(product.updated, "2026-06-14 0447Z")
        self.assertIn("TSTM", product.maps["categorical"])

    def test_geojson_product_id_is_stable_between_direct_and_zip_sources(self) -> None:
        spec = bot.BUNDLES[0]
        maps = {"categorical": {"MRGL": ["geom"]}}
        properties = {
            "ISSUE": "202606141254",
            "ISSUE_ISO": "2026-06-14T12:54:00Z",
            "VALID_ISO": "2026-06-14T13:00:00Z",
            "EXPIRE_ISO": "2026-06-15T12:00:00Z",
        }

        direct_product = bot.geojson_product_from_maps(spec, maps, properties, "day1otlk_direct", "directhash")
        zip_product = bot.geojson_product_from_maps(spec, maps, properties, "day1otlk_20260614_1300", "ziphash")

        self.assertEqual(direct_product.product_id, "geojson:day1:202606141254")
        self.assertEqual(zip_product.product_id, direct_product.product_id)

    def test_geojson_first_uses_raw_pts_when_raw_is_newer(self) -> None:
        spec = bot.BUNDLES[1]
        geojson_product = bot.PtsProduct(
            spec=spec,
            product_id="geojson:day2otlk_20260613_1730:20260614120000Z",
            title=spec.name,
            issued="2026-06-13 1737Z",
            valid="2026-06-14 1200Z - 2026-06-15 1200Z",
            updated="2026-06-13 1737Z",
            source="geojson",
            maps={},
        )
        pts_product = bot.PtsProduct(
            spec=spec,
            product_id="PTSDY2:151200Z",
            title=spec.name,
            issued="1147 PM CDT SAT JUN 13 2026",
            valid="151200Z - 161200Z",
            updated="1147 PM CDT SAT JUN 13 2026",
            source="pts",
            maps={},
        )
        original_geojson = bot.fetch_geojson_product_for_spec
        original_pts = bot.pts_product_from_text_or_feed
        try:
            bot.fetch_geojson_product_for_spec = lambda _spec: geojson_product
            bot.pts_product_from_text_or_feed = lambda _spec, _pts_text=None: pts_product

            chosen = bot.choose_custom_product(spec, None, "geojson-first")
        finally:
            bot.fetch_geojson_product_for_spec = original_geojson
            bot.pts_product_from_text_or_feed = original_pts

        self.assertIs(chosen, pts_product)

    def test_geojson_only_uses_pts_for_day48(self) -> None:
        spec = bot.BUNDLES[3]
        pts_product = bot.PtsProduct(
            spec=spec,
            product_id="PTSD48:1200Z",
            title=spec.name,
            issued="1200 PM CDT SUN JUN 14 2026",
            valid="171200Z - 221200Z",
            updated="1200 PM CDT SUN JUN 14 2026",
            source="pts",
            maps={},
        )
        original_geojson = bot.fetch_geojson_product_for_spec
        original_pts = bot.pts_product_from_text_or_feed
        try:
            bot.fetch_geojson_product_for_spec = lambda _spec: self.fail("Day 4-8 should not use direct GeoJSON")
            bot.pts_product_from_text_or_feed = lambda _spec, _pts_text=None: pts_product

            chosen = bot.choose_custom_product(spec, None, "geojson-only")
        finally:
            bot.fetch_geojson_product_for_spec = original_geojson
            bot.pts_product_from_text_or_feed = original_pts

        self.assertIs(chosen, pts_product)

    def test_risk_filter_supports_enh_plus_and_day48_override(self) -> None:
        product = bot.parse_pts_text(PTS_DAY1_TEXT, bot.BUNDLES[0])
        day1_snapshot = bot.BundleSnapshot(
            spec=bot.BUNDLES[0],
            title="test",
            updated="now",
            product_id="test",
            page_url=bot.BUNDLES[0].page_url,
            images=(),
            risk_labels=bot.risk_labels_from_product(product),
        )
        day48_product = bot.parse_pts_text(PTS_DAY48_TEXT, bot.BUNDLES[3])
        day48_snapshot = bot.BundleSnapshot(
            spec=bot.BUNDLES[3],
            title="test",
            updated="now",
            product_id="test",
            page_url=bot.BUNDLES[3].page_url,
            images=(),
            risk_labels=bot.risk_labels_from_product(day48_product),
        )

        self.assertFalse(
            bot.snapshot_passes_risk_filter(day1_snapshot, min_risk_level="enh", always_post_day48=True)[0]
        )
        self.assertTrue(
            bot.snapshot_passes_risk_filter(day48_snapshot, min_risk_level="enh", always_post_day48=True)[0]
        )

    def test_preview_post_key_ignores_render_hash_changes(self) -> None:
        runner = bot.OutlookBot.__new__(bot.OutlookBot)
        runner.args = argparse.Namespace(min_risk_level="any", always_post_day48=False)
        image_a = bot.MapImage("categorical", "a", "a.png", "image/png", "aaa", b"a")
        image_b = bot.MapImage("categorical", "b", "b.png", "image/png", "bbb", b"b")
        first = bot.BundleSnapshot(
            spec=bot.BUNDLES[0],
            title="Day 1",
            updated="2026-06-14 1254Z",
            product_id="preview:geojson:day1:202606141254",
            page_url=bot.BUNDLES[0].page_url,
            images=(image_a,),
        )
        second = dataclasses.replace(first, images=(image_b,))
        official_second = dataclasses.replace(second, product_id="official:day1:202606141254")
        official_first = dataclasses.replace(first, product_id="official:day1:202606141254")

        self.assertNotEqual(first.post_key, second.post_key)
        self.assertEqual(runner.configured_post_key(first), runner.configured_post_key(second))
        self.assertNotEqual(runner.configured_post_key(official_first), runner.configured_post_key(official_second))

    def test_state_keeps_recent_post_keys_for_cdn_flip_flops(self) -> None:
        snapshot = bot.BundleSnapshot(
            spec=bot.BUNDLES[0],
            title="Day 1",
            updated="2026-06-14 1254Z",
            product_id="preview:geojson:day1:202606141254",
            page_url=bot.BUNDLES[0].page_url,
            images=(),
        )
        state: dict[str, object] = {"posted": {}}

        bot.mark_posted(state, snapshot, mode="posted", reason="first", state_key="day1:preview", post_key="old-key")
        bot.mark_posted(state, snapshot, mode="posted", reason="second", state_key="day1:preview", post_key="new-key")

        self.assertTrue(bot.bundle_is_posted(state, snapshot, state_key="day1:preview", post_key="old-key"))
        self.assertTrue(bot.bundle_is_posted(state, snapshot, state_key="day1:preview", post_key="new-key"))
        self.assertFalse(bot.bundle_is_posted(state, snapshot, state_key="day1:preview", post_key="other-key"))

    def test_fetch_json_retries_incomplete_spc_response(self) -> None:
        responses = iter(["{\"type\": \"Feature", "{\"type\": \"FeatureCollection\", \"features\": []}"])
        original_fetch_text = bot.fetch_text
        original_sleep = bot.time.sleep
        try:
            bot.fetch_text = lambda _url, timeout=20: next(responses)
            bot.time.sleep = lambda _seconds: None

            text, parsed = bot.fetch_json_with_retries("https://example.test/layer.geojson", attempts=2)
        finally:
            bot.fetch_text = original_fetch_text
            bot.time.sleep = original_sleep

        self.assertIn("FeatureCollection", text)
        self.assertEqual(parsed["type"], "FeatureCollection")

    def test_link_content_adds_official_product_url(self) -> None:
        snapshot = bot.BundleSnapshot(
            spec=bot.BUNDLES[0],
            title="Day 1",
            updated="2026-06-14 1631Z",
            product_id="preview:PTSDY1:141630Z",
            page_url="https://www.spc.noaa.gov/products/outlook/day1otlk.html",
            images=(),
        )

        payload = bot.discord_payload(snapshot, content_mode="link", include_username=False)

        self.assertIn("Updated: 2026-06-14 1631Z", payload["content"])
        self.assertIn("Official SPC discussion/product:", payload["content"])
        self.assertIn("<https://www.spc.noaa.gov/products/outlook/day1otlk.html>", payload["content"])


if __name__ == "__main__":
    unittest.main()
