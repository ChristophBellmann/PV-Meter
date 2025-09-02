#!/usr/bin/env python3
import asyncio
from bleak import BleakScanner, BleakClient

# 🔧 Name deines BLE-Geräts
DEVICE_NAME = "DL24M_BLE"

async def scan_for_device(device_name):
    print("🔍 Suche nach BLE-Geräten...")
    devices = await BleakScanner.discover(timeout=5.0)
    for device in devices:
        if device.name == device_name:
            print(f"✅ Gerät gefunden: {device.name} @ {device.address}")
            return device.address
    print("❌ Gerät nicht gefunden.")
    return None

async def list_gatt_services(address):
    print(f"\n🔌 Verbinde mit {address}...")
    async with BleakClient(address) as client:
        print("📋 GATT-Services und -Characteristics:")
        for service in client.services:
            print(f"🔹 Service: {service.uuid}")
            for char in service.characteristics:
                props = ', '.join(char.properties)
                print(f"   └─ Char: {char.uuid}  [{props}]")

async def main():
    address = await scan_for_device(DEVICE_NAME)
    if address:
        await list_gatt_services(address)

if __name__ == "__main__":
    asyncio.run(main())
