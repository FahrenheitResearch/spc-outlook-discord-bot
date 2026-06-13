import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import spc_outlook_bot as bot  # noqa: E402


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

        body, content_type = bot.multipart_body({"username": "SPC Outlook Bot"}, (image,))

        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="payload_json"', body)
        self.assertIn(b'name="files[0]"; filename="day1_categorical.png"', body)
        self.assertIn(json.dumps({"username": "SPC Outlook Bot"}).encode("utf-8"), body)

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


if __name__ == "__main__":
    unittest.main()
