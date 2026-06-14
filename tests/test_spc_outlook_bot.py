import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402
from shapely.geometry import Point  # noqa: E402


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
            def fake_fetch_text(url: str) -> str:
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


if __name__ == "__main__":
    unittest.main()
