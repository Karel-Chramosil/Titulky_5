def print_devices(cap) -> int:
    if cap.error or not cap.devices:
        print(cap.error or "Žádné loopback zařízení.")
        return 1
    for device in cap.devices:
        mark = " *" if device.index == cap.default_index else ""
        print(f"[{device.index}] {device.name}  {device.rate} Hz, {device.channels} kanálů{mark}")
    return 0
