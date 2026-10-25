"""Synthetic MF4 recordings with the channel names used by the built-in libraries."""

from __future__ import annotations

import datetime as _dt
import zlib
from pathlib import Path

import numpy as np


def aeb_recording(path, *, speed_kph=40.0, aeb_at=(5.0,), decel=-8.0, duration=20.0, rate=100.0,
                  speed_unit="m/s", with_gap=True, response_delay=0.15, start=None):
    """Ego drives at constant speed; at each `aeb_at` time a PB/FB request brakes it to a stop."""
    from asammdf import MDF, Signal

    t = np.arange(0.0, duration, 1.0 / rate)
    target = np.zeros_like(t)
    accel = np.zeros_like(t)
    v = np.full_like(t, speed_kph / 3.6)
    stop_times = []
    for t0 in aeb_at:
        v0 = speed_kph / 3.6
        t_brake = t0 + response_delay
        t_stop = t_brake + v0 / -decel
        stop_times.append(t_stop)
        req = (t >= t0) & (t < t_stop)
        target[req] = -6.0
        target[(t >= t0 + 0.4) & (t < t_stop)] = -15.0
        braking = (t >= t_brake) & (t < t_stop)
        accel[braking] = decel
        v[braking] = v0 + decel * (t[braking] - t_brake)
        v[(t >= t_stop) & (t < t_stop + 2.0)] = 0.0
    gap = np.clip(60.0 - np.cumsum(v) / rate, 0, None) if with_gap else None
    speed = v * 3.6 if speed_unit == "km/h" else v
    m = MDF(version="4.10")
    m.append([Signal(speed, t, name="speedESC", unit=speed_unit),
              Signal(np.zeros_like(t), t, name="throttle_value", unit="%")],
             acq_name="vehicleIOActuationFeedback")
    m.append([Signal(accel, t[:], name="acceleration[0]", unit="m/s2"),
              Signal(np.zeros_like(t), t, name="acceleration[1]", unit="m/s2")], acq_name="imuFrame")
    m.append([Signal(target, t, name="payload/long_ctrl_accel_request", unit="m/s2")],
             acq_name="vehicleIOASILCommandE2EWrapper")
    if gap is not None:
        tg = t[::5]
        m.append([Signal(gap[::5], tg, name="aeb_state_info/aeb_full_state_info/longitudinal_clearance", unit="m")],
                 acq_name="aebStateInfo")
    # a start of its own (from the name) per file: separate drives, unless a test sets `start`
    m.header.start_time = start or (_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
                                    + _dt.timedelta(hours=zlib.crc32(Path(path).name.encode()) % 10000))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    m.save(str(path), overwrite=True)
    m.close()
    return {"stop_times": stop_times, "starts": list(aeb_at)}
