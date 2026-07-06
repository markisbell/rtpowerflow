"""Range constraints on the API request models: raw API calls must not be able
to inject physically nonsensical values (negative PV kWp, negative charger
power, out-of-range penetrations) into the profile generators."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from netzsim.api import (
    BatteryRequest,
    EstimationConfigModel,
    EvRequest,
    LoadgenPolicy,
    PvRequest,
)


def test_loadgen_policy_rejects_nonsense():
    with pytest.raises(ValidationError):
        LoadgenPolicy(pv_kwp=-5.0)
    with pytest.raises(ValidationError):
        LoadgenPolicy(ev_charger_kw=-11.0)
    with pytest.raises(ValidationError):
        LoadgenPolicy(ev_penetration=1.5)
    with pytest.raises(ValidationError):
        LoadgenPolicy(power_factor=0.0)
    with pytest.raises(ValidationError):
        LoadgenPolicy(scale=-1.0)
    # the documented defaults stay valid
    assert LoadgenPolicy().pv_kwp == 5.0


def test_der_requests_rejects_nonsense():
    with pytest.raises(ValidationError):
        PvRequest(bus=0, kwp=0.0)
    with pytest.raises(ValidationError):
        EvRequest(bus=0, kw=-3.7)
    with pytest.raises(ValidationError):
        EvRequest(bus=0, dur_min=500)          # window is 1-4 h
    with pytest.raises(ValidationError):
        EvRequest(bus=0, start_min=1440)
    with pytest.raises(ValidationError):
        BatteryRequest(bus=0, capacity_kwh=-10)
    with pytest.raises(ValidationError):
        BatteryRequest(bus=0, soc0=1.5)
    assert EvRequest(bus=0).dur_min == 120


def test_estimation_config_rejects_nonsense():
    with pytest.raises(ValidationError):
        EstimationConfigModel(load_basis="magic")
    with pytest.raises(ValidationError):
        EstimationConfigModel(slp_annual_kwh=100)          # below any household
    with pytest.raises(ValidationError):
        EstimationConfigModel(pseudo_std_pct=0)
    # documented DSO-practice defaults
    cfg = EstimationConfigModel()
    assert cfg.pv_pseudo is False and cfg.ev_pseudo is False
    assert cfg.load_basis == "profile" and cfg.slp_annual_kwh == 4000.0
