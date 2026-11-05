"""A standalone profile's gateway ticks cron for its own profile only.

``gateway.standalone: true`` makes a gateway serve only its launch profile, but the cron tick set
was every profile under the root. Such a gateway then ran startup recovery on, and could fire due
jobs of, the default profile and every sibling — profiles another gateway (or none) owns.
"""

from __future__ import annotations

from pathlib import Path

import gateway.run as gw_run
import hermes_cli.gateway_multiplex_mode as mm
from hermes_cli import profiles


def _setup(monkeypatch, tmp_path, standalone):
    homes = [("default", tmp_path), ("a", tmp_path / "profiles" / "a"), ("evals", tmp_path / "profiles" / "evals")]
    monkeypatch.setattr(gw_run, "_multiplex_profile_homes", lambda config: list(homes))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "evals")
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(mm, "_standalone_launcher", lambda: standalone)
    return homes


def test_standalone_gateway_ticks_only_its_own_profile(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, standalone=True)
    assert gw_run._cron_tick_profile_homes(object()) == [("evals", tmp_path / "profiles" / "evals")]


def test_multiplexing_gateway_still_ticks_every_profile(monkeypatch, tmp_path):
    homes = _setup(monkeypatch, tmp_path, standalone=False)
    assert gw_run._cron_tick_profile_homes(object()) == homes


def test_standalone_probe_failure_keeps_the_full_set(monkeypatch, tmp_path):
    homes = _setup(monkeypatch, tmp_path, standalone=False)

    def boom():
        raise RuntimeError("no profile store")
    monkeypatch.setattr(mm, "_standalone_launcher", boom)
    assert gw_run._cron_tick_profile_homes(object()) == homes
