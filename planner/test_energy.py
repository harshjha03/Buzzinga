"""Offline check of the energy model (no API calls): python3 -m planner.test_energy"""
from planner.energy import climb_wh, speed_factor, temp_factor, trip_energy_wh

# flat 100 km at 90 km/h == base consumption exactly
steps = [(100_000, 4000)]  # 100 km in 4000 s = 90 km/h
flat = trip_energy_wh(135, 1550, steps, [100.0, 100.0], temp_c=25)
assert abs(flat - 13_500) < 1, flat

assert speed_factor(90) == 1.0
assert speed_factor(120) > 1.2          # aero penalty
assert speed_factor(25) == 0.85         # city floor
assert temp_factor(38) > temp_factor(25) == 1.0

# 500 m climb costs ~2.3 kWh extra for 1550 kg; descent gives most back
up = climb_wh(1550, [0, 500])
down = climb_wh(1550, [500, 0])
assert 2200 < up < 2500, up
assert -0.6 * up * 0.9 - 100 < down < 0, down

# moderate descent: regen credit applies but stays above the floor
downhill = trip_energy_wh(135, 1550, steps, [2000.0, 0.0], temp_c=25)
assert 8000 < downhill < 8600, downhill

# absurd descent (5 km drop): clamped to 30% of flat consumption
plunge = trip_energy_wh(135, 1550, steps, [5000.0, 0.0], temp_c=25)
assert plunge == 0.3 * 13_500, plunge

print("energy model: all checks pass")
