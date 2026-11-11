"""OCPP 1.6J charge point and CSMS simulators (M2).

Both live in the same image as the Modbus devices and the poller (D10), so this package is
imported by whichever role the container is started as:

    python ocpp/csms.py     # the central system test double (a WebSocket server)
    python ocpp/cp.py       # the charge point (a WebSocket client, and a Modbus client)

`ocpp16.py` holds the frame codec, shared so the encoder and decoder cannot drift - the same
rule that makes `sunspec.py` shared by the Modbus device and the Modbus poller.
"""
