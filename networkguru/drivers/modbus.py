"""A small async Modbus TCP client, standard library only: read holding registers (function 3)."""

from __future__ import annotations

import asyncio
import struct

EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "device failure",
              6: "device busy", 10: "gateway path unavailable", 11: "no answer from the device behind the gateway"}


class ModbusError(Exception):
    pass


class ModbusClient:
    """One TCP connection to one device; `async with ModbusClient(host) as m:`."""

    def __init__(self, host: str, port: int = 502, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._r = self._w = None
        self._tid = 0

    async def __aenter__(self):
        try:
            self._r, self._w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise ModbusError(f"no Modbus TCP answer from {self.host}:{self.port} (timeout)") from None
        except ConnectionRefusedError:
            raise ModbusError(f"Modbus TCP refused on {self.host}:{self.port} - is it switched on in the device?") from None
        except OSError as e:
            raise ModbusError(f"cannot reach {self.host}:{self.port} ({e.strerror or e})") from None
        return self

    async def __aexit__(self, *exc):
        if self._w:
            self._w.close()
            try:
                await asyncio.wait_for(self._w.wait_closed(), 1.0)
            except (TimeoutError, asyncio.TimeoutError, OSError):
                pass

    async def read_holding(self, unit: int, address: int, count: int) -> list[int]:
        """`count` 16-bit registers from `address` (as the device's register list numbers them)."""
        self._tid = (self._tid + 1) & 0xFFFF
        req = struct.pack(">HHHBBHH", self._tid, 0, 6, unit, 3, address, count)
        self._w.write(req)
        try:
            await self._w.drain()
            head = await asyncio.wait_for(self._r.readexactly(7), self.timeout)
            tid, proto, length, _unit = struct.unpack(">HHHB", head)
            body = await asyncio.wait_for(self._r.readexactly(length - 1), self.timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise ModbusError("the Modbus answer did not come (timeout)") from None
        except (asyncio.IncompleteReadError, OSError):
            raise ModbusError("the Modbus connection closed") from None
        if tid != self._tid or proto != 0:
            raise ModbusError("unexpected Modbus answer")
        if body[0] == 0x83:
            code = body[1] if len(body) > 1 else 0
            raise ModbusError(f"Modbus error at unit {unit} register {address}: {EXCEPTIONS.get(code, code)}")
        if body[0] != 3 or len(body) < 2 or body[1] != 2 * count or len(body) < 2 + 2 * count:
            raise ModbusError("bad Modbus answer")
        return list(struct.unpack(f">{count}H", body[2:2 + 2 * count]))


def signed(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v
