"""Smallest check that fails if normalize breaks: python3 -m ingest.test_normalize"""
from ingest.ocm_sync import normalize

POI = {
    "ID": 12345,
    "StatusTypeID": 50,
    "DateLastVerified": "2026-01-01T00:00:00Z",
    "AddressInfo": {"Title": "Test Hub", "AddressLine1": "NH48", "Town": "Pune",
                    "Latitude": 18.52, "Longitude": 73.85},
    "OperatorInfo": {"Title": "Tata Power"},
    "Connections": [
        {"ConnectionType": {"Title": "CCS (Type 2)"}, "CurrentTypeID": 30,
         "PowerKW": 60, "Quantity": 2, "Reference": "IN*TP*001"},
        {"ConnectionType": {"Title": "Type 2 (Socket Only)"}, "CurrentTypeID": 20},
    ],
}

charger, stalls = normalize(POI)
assert charger["name"] == "Test Hub"
assert charger["operator"] == "Tata Power"
assert charger["address"] == "NH48, Pune"
assert charger["status"] == "operational"
assert (charger["lat"], charger["lon"]) == (18.52, 73.85)
assert stalls[0] == {"connector_type": "CCS (Type 2)", "current_type": "DC",
                     "power_kw": 60, "kw_is_assumed": False,
                     "evse_id": "IN*TP*001", "quantity": 2}
# missing kW -> assumed, planner-safe AC default
assert stalls[1]["kw_is_assumed"] and stalls[1]["power_kw"] == 7
# DC inferred from connector title when CurrentTypeID absent
c2, s2 = normalize({"ID": 1, "AddressInfo": {"Latitude": 1, "Longitude": 1},
                    "Connections": [{"ConnectionType": {"Title": "CHAdeMO"}}]})
assert s2[0]["current_type"] == "DC" and s2[0]["power_kw"] == 25 and s2[0]["kw_is_assumed"]
assert c2["status"] == "unknown"
print("normalize: all checks pass")
