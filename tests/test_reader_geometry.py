"""Exercise the browser's local anchor calculations without a DOM or model calls."""

import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture(scope="module")
def geometry():
    node = shutil.which("node")
    assert node is not None, "Node is required to execute browser geometry regressions"
    source = Path(__file__).resolve().parents[1] / "sourceloom/static/reader_geometry.js"
    module_url = "data:text/javascript;base64," + base64.b64encode(source.read_bytes()).decode("ascii")
    script = """
        import { readFileSync } from 'node:fs';
        const request = JSON.parse(readFileSync(0, 'utf8'));
        const geometry = await import(request.module);
        const before = JSON.stringify(request.args);
        const result = geometry[request.function](...request.args);
        process.stdout.write(JSON.stringify({result, unchanged: before === JSON.stringify(request.args)}));
    """

    def call(function, *args):
        completed = subprocess.run(
            [node, "--input-type=module", "-e", script],
            input=json.dumps({"module": module_url, "function": function, "args": args}),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        response = json.loads(completed.stdout)
        assert response["unchanged"], "Geometry must not mutate the caller's anchors or rows"
        return response["result"]

    return call


def anchor(left, right, *, page=1, block="paragraph-a", precision="region"):
    return dict(left=left, right=right, page=page, blockId=block, precision=precision)


@pytest.mark.parametrize("side,point,expected", [("left", 25, 250), ("right", 250, 25)])
def test_same_page_interpolation_works_in_both_directions(geometry, side, point, expected):
    result = geometry("mapPoint", [anchor(0, 200), anchor(100, 400)], point, side)

    assert result["position"] == pytest.approx(expected)
    assert result["precision"] == "region"
    assert result["blockId"] == "paragraph-a"


def test_editor_line_anchors_interpolate_with_page_zero(geometry):
    anchors = [anchor(0, 40, page=0, precision="line"), anchor(80, 200, page=0, precision="line")]

    result = geometry("mapPoint", anchors, 20, "left")

    assert result["position"] == 80
    assert result["precision"] == "line"


def test_unsorted_anchors_are_sorted_without_modifying_the_input(geometry):
    result = geometry("mapPoint", [anchor(100, 400), anchor(0, 200)], 50, "left")

    assert result["position"] == 300


@pytest.mark.parametrize("point,expected", [(-10, 200), (110, 400)])
def test_outside_local_anchor_range_uses_nearest_content_anchor(geometry, point, expected):
    result = geometry("mapPoint", [anchor(0, 200), anchor(100, 400)], point, "left")

    assert result["position"] == expected
    assert result["precision"] == "region"


@pytest.mark.parametrize("point,expected", [(25, 500), (50, 500), (75, 100)])
def test_reordered_range_uses_nearest_anchor_instead_of_interpolating(geometry, point, expected):
    result = geometry("mapPoint", [anchor(0, 500), anchor(100, 100)], point, "left")

    assert result["position"] == expected
    assert result["precision"] == "region"


def test_distant_source_pages_do_not_authorize_interpolation(geometry):
    result = geometry("mapPoint", [anchor(0, 100, page=1), anchor(100, 500, page=5)], 25, "left")

    assert result["position"] == 100


def test_separate_explicit_regions_do_not_authorize_gap_interpolation(geometry):
    anchors = [
        anchor(0, 100, block="region-a"),
        anchor(100, 200, block="region-a"),
        anchor(200, 500, block="region-b"),
        anchor(300, 600, block="region-b"),
    ]

    result = geometry("mapPoint", anchors, 140, "left")

    assert result["position"] == 200
    assert result["precision"] == "region"
    assert result["blockId"] == "region-a"


@pytest.mark.parametrize("follower,expected", [(90, 100), (850, 900), (1000, 900)])
def test_repeated_source_position_chooses_occurrence_nearest_follower(geometry, follower, expected):
    anchors = [anchor(50, 900, block="copy-b"), anchor(50, 100, block="copy-a")]

    result = geometry("mapPoint", anchors, 50, "left", follower)

    assert result["position"] == expected


def test_repeated_right_position_chooses_source_nearest_follower(geometry):
    result = geometry("mapPoint", [anchor(100, 50), anchor(900, 50)], 50, "right", 850)

    assert result["position"] == 900


@pytest.mark.parametrize("follower,expected", [(50, 50), (1050, 1050)])
def test_repeated_resource_range_interpolates_inside_nearest_occurrence(geometry, follower, expected):
    anchors = [
        anchor(0, 0, block="copy-a"),
        anchor(100, 100, block="copy-a"),
        anchor(0, 1000, block="copy-b"),
        anchor(100, 1100, block="copy-b"),
    ]

    result = geometry("mapPoint", anchors, 50, "left", follower)

    assert result["position"] == expected
    assert result["precision"] == "region"


@pytest.mark.parametrize("side", ["left", "right"])
def test_empty_anchors_have_no_global_percentage_fallback(geometry, side):
    assert geometry("mapPoint", [], 750, side, 125) is None


def test_invalid_coordinates_do_not_become_anchors(geometry):
    anchors = [dict(left=None, right=100), dict(left=100, right="200"), dict(left=100)]

    assert geometry("mapPoint", anchors, 50, "left") is None


def test_invalid_anchors_do_not_hide_a_valid_local_anchor(geometry):
    result = geometry("mapPoint", [dict(left=None, right=100), anchor(20, 300)], 50, "left")

    assert result["position"] == 300


def test_mixed_precision_interpolation_reports_page_precision(geometry):
    anchors = [anchor(0, 0, precision="region"), anchor(100, 200, precision="page")]

    result = geometry("mapPoint", anchors, 50, "left")

    assert result["position"] == 100
    assert result["precision"] == "page"


def test_locate_empty_rows_returns_none(geometry):
    assert geometry("locate", [], 10) is None


@pytest.mark.parametrize("point,expected", [(-1, "first"), (0, "first"), (99.5, "first"),
                                            (100, "second"), (199.5, "second"),
                                            (200, "third"), (500, "third")])
def test_locate_binary_search_respects_row_boundaries(geometry, point, expected):
    rows = [dict(top=0, id="first"), dict(top=100, id="second"), dict(top=200, id="third")]

    assert geometry("locate", rows, point)["id"] == expected


def test_locate_equal_tops_chooses_last_row_at_boundary(geometry):
    rows = [dict(top=0, id="first"), dict(top=100, id="second"), dict(top=100, id="third")]

    assert geometry("locate", rows, 100)["id"] == "third"
