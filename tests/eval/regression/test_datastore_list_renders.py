"""``vmware-storage datastore list`` must render every datastore it is given.

Real-hardware finding, 2026-08-30 (VCF 9.1): the command failed 100% of the
time. The usage column was built as ``f"[{usage_style}]{pct}%[/]"`` with
``usage_style = "red" if pct > 85 else ""`` — so on any datastore below the
threshold Rich was handed ``[]12.5%[/]``, an empty open tag followed by a close
tag with nothing to close, and raised ``MarkupError``. The healthy case was the
broken one, which is why nobody hit it while building the feature: a lab
datastore over 85% full renders fine.

Two values in that row are markup-parsed but are not ours to write. The style is
one. The other is the datastore *name*, which comes back from vCenter: a
datastore called ``[SSD] prod`` is legal, ordinary, and gets its ``[SSD]`` eaten
as a style tag — or raises, depending on what follows. Values are data, so they
go in as ``Text`` and the styling arrives through the style argument, where it
cannot be confused with content.

The control here matters as much as the crash: a table of ordinary datastores
must still render, and one over 85% must still come out red. A fix that renders
everything by dropping the colour is a different defect.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from vmware_storage.cli import app

runner = CliRunner()


def _ds(name: str, usage_pct: float, **over):
    row = {
        "name": name,
        "type": "VMFS",
        "total_gb": 1024.0,
        "free_gb": 512.0,
        "usage_pct": usage_pct,
        "vm_count": 3,
    }
    row.update(over)
    return row


def _run(items):
    with (
        patch("vmware_storage.cli._get_connection", return_value=MagicMock()),
        patch(
            "vmware_storage.ops.inventory.list_datastores",
            return_value={"items": items},
        ),
    ):
        return runner.invoke(app, ["datastore", "list"], catch_exceptions=False)


def test_a_datastore_below_the_threshold_renders() -> None:
    """The defect: an empty style produced ``[]12.5%[/]``."""
    result = _run([_ds("datastore1", 12.5)])
    assert result.exit_code == 0, result.output
    assert "datastore1" in result.output
    assert "12.5%" in result.output


def test_an_ordinary_mixed_table_renders() -> None:
    """Control: normal values must keep working."""
    result = _run(
        [_ds("ds-a", 12.5), _ds("ds-b", 91.0), _ds("ds-c", 85.0), _ds("ds-d", 0.0)]
    )
    assert result.exit_code == 0, result.output
    for name in ("ds-a", "ds-b", "ds-c", "ds-d"):
        assert name in result.output


def test_a_datastore_over_the_threshold_is_still_styled_red() -> None:
    """Control: rendering everything by dropping the colour is a different bug."""
    from rich.console import Console
    from rich.text import Text

    from vmware_storage.cli import _usage_cell

    hot = _usage_cell(91.0)
    cool = _usage_cell(12.5)
    assert isinstance(hot, Text) and isinstance(cool, Text)
    assert hot.style == "red"
    assert cool.style != "red"

    # ...and the style survives to the terminal, not just the object.
    console = Console(force_terminal=True, width=40)
    with console.capture() as cap:
        console.print(hot)
    assert "\x1b[31m" in cap.get()


@pytest.mark.parametrize(
    "name", ["[SSD] prod", "vsanDatastore [2]", "ds[/]", "[bold]nope"]
)
def test_a_datastore_named_with_square_brackets_renders(name: str) -> None:
    """vCenter's names are data. Rich must not read them as style tags."""
    result = _run([_ds(name, 12.5)])
    assert result.exit_code == 0, result.output


def test_a_missing_usage_value_does_not_crash_the_whole_listing() -> None:
    """One odd row must not take the other datastores down with it.

    Four of the eight hosts on the reporting estate were ``notResponding``, so a
    row arriving without the number the comparison needs is the ordinary case
    there, not a hypothetical.
    """
    result = _run([_ds("ds-a", 12.5), _ds("ds-odd", None), _ds("ds-b", 91.0)])
    assert result.exit_code == 0, result.output
    assert "ds-a" in result.output
    assert "ds-b" in result.output
    assert "ds-odd" in result.output
