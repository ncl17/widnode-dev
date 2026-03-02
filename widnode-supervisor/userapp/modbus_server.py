# modbus_server.py
import asyncio
import logging
from dataclasses import dataclass

# --- Imports de datastore (compat v4/v3.10+ vs v3.x/v2.x) ---
DeviceContext = None
ServerContext = None
DataBlockClass = None
SEQUENTIAL_SIGNATURE = True  # True -> ctor(address, values); False -> ctor(values)

try:
    # v4 / v3.10+ API
    from pymodbus.datastore import ModbusDeviceContext as DeviceContext
    from pymodbus.datastore import ModbusServerContext as ServerContext
    try:
        # v4 recomienda Sparse
        from pymodbus.datastore import ModbusSparseDataBlock as DataBlockClass
        SEQUENTIAL_SIGNATURE = False
    except Exception:
        # fallback si existe Sequential
        from pymodbus.datastore import ModbusSequentialDataBlock as DataBlockClass
        SEQUENTIAL_SIGNATURE = True
except Exception:
    # v3.0–v3.9 / v2.x
    from pymodbus.datastore import ModbusSlaveContext as DeviceContext
    from pymodbus.datastore import ModbusServerContext as ServerContext
    try:
        from pymodbus.datastore import ModbusSequentialDataBlock as DataBlockClass
        SEQUENTIAL_SIGNATURE = True
    except Exception:
        from pymodbus.datastore import ModbusSparseDataBlock as DataBlockClass
        SEQUENTIAL_SIGNATURE = False

# --- Imports de servidor (compat async vs sync) ---
ASYNC_AVAILABLE = False
SYNC_AVAILABLE = False
StartAsyncTcpServer = None
StartTcpServer = None
try:
    # v4 / v3.x
    from pymodbus.server import StartAsyncTcpServer as StartAsyncTcpServer
    ASYNC_AVAILABLE = True
except Exception:
    try:
        # v3.x antiguo
        from pymodbus.server.async_io import StartAsyncTcpServer as StartAsyncTcpServer
        ASYNC_AVAILABLE = True
    except Exception:
        try:
            # v2.x síncrono
            from pymodbus.server.sync import StartTcpServer as StartTcpServer
            SYNC_AVAILABLE = True
        except Exception:
            pass

# FC03/FC16 usan holding registers -> func_code 3
HOLDING = 3

@dataclass
class Map:
    base: int = 0
    stride: int = 16
    max_index: int = 256  # cantidad máxima de índices

class GatewayModbusServer:
    """
    Server Modbus TCP con HR por índice:
      0: ts_lo (U16)
      1: ts_hi (U16)
      2: rmsx (U16 ×1000)
      3: rmsy (U16 ×1000)
      4: rmsz (U16 ×1000)
      5: temp (I16×1000 empaquetado en U16)
      6: rmsx_vel (U16 ×1000)
      7: rmsy_vel (U16 ×1000)
      8: rmsz_vel (U16 ×1000)
      9..15: reserva (0)
    """
    def __init__(self, bind_host: str, port: int, unit_id: int, mapping: Map):
        self._bind = bind_host
        self._port = port
        self._unit = unit_id
        self._map = mapping
        self._lock = asyncio.Lock()
        self._task = None
        self._thread = None

        size = mapping.base + mapping.stride * (mapping.max_index + 1)

        # Crear bloque HR según clase disponible
        if SEQUENTIAL_SIGNATURE:
            hr = DataBlockClass(0, [0] * size)
        else:
            hr = DataBlockClass([0] * size)

        # Contexto por "device" (antes 'slave')
        device_ctx = DeviceContext(hr=hr)

        # v4/v3.10+: ServerContext(devices={id: ctx}, single=False)
        # v3.x/v2.x  : ServerContext(slaves={id: ctx}, single=False)  -> mismo nombre en la clase, cambia parámetro interno
        # La clase expone el acceso como context[unit_id]
        # self._ctx = ServerContext(devices={unit_id: device_ctx}, single=False)
        try:
            # v4 / v3.10+ usan 'devices'
            self._ctx = ServerContext(devices={unit_id: device_ctx}, single=False)
        except TypeError:
            # v3.0–v3.9 / v2.x usan 'slaves'
            self._ctx = ServerContext(slaves={unit_id: device_ctx}, single=False)

    async def start(self):
        logging.info(f"[MODBUS_SERVER] escuchando en {self._bind}:{self._port}")
        if ASYNC_AVAILABLE:
            self._task = asyncio.create_task(
                StartAsyncTcpServer(context=self._ctx, address=(self._bind, self._port))
            )
        elif SYNC_AVAILABLE:
            import threading
            def run_sync():
                StartTcpServer(context=self._ctx, address=(self._bind, self._port))
            self._thread = threading.Thread(target=run_sync, daemon=True)
            self._thread.start()
            logging.warning("[MODBUS_SERVER] Modo compat: servidor síncrono en hilo (pymodbus 2.x)")
        else:
            raise RuntimeError("pymodbus demasiado antiguo: no hay server async ni sync disponible")

    async def close(self):
        if self._task:
            self._task.cancel()
            self._task = None
        # en v2.x no hay stop limpio del sync server: se cae al terminar el proceso

    def _addr(self, idx: int) -> int:
        return self._map.base + idx * self._map.stride

    @staticmethod
    def _q1000_u16(x: float) -> int:
        if x != x or x is None:
            return 0
        v = int(round(x * 1000.0))
        if v < 0: v = 0
        if v > 65535: v = 65535
        return v

    @staticmethod
    def _q1000_i16_to_u16(x: float) -> int:
        if x != x or x is None:
            return 0
        v = int(round(x * 1000.0))
        if v < -32768: v = -32768
        if v > 32767: v = 32767
        return v & 0xFFFF  # two's complement en 16 bits

    async def update_block(self, idx: int, ts_in: int, vals: dict) -> bool:
        """Escribe bloque si ts_in > ts_stored. Devuelve True si actualizó."""
        addr = self._addr(idx)
        ts_lo = ts_in & 0xFFFF
        ts_hi = (ts_in >> 16) & 0xFFFF

        with_vals = [
            self._q1000_u16(vals.get("rmsx", 0.0)),
            self._q1000_u16(vals.get("rmsy", 0.0)),
            self._q1000_u16(vals.get("rmsz", 0.0)),
            self._q1000_i16_to_u16(vals.get("temp", 0.0)),
            self._q1000_u16(vals.get("rmsx_vel", 0.0)),
            self._q1000_u16(vals.get("rmsy_vel", 0.0)),
            self._q1000_u16(vals.get("rmsz_vel", 0.0)),
        ]

        async with self._lock:
            cur = self._ctx[self._unit].getValues(HOLDING, addr, count=2)
            ts_cur = (cur[0] & 0xFFFF) | ((cur[1] & 0xFFFF) << 16)
            if ts_in <= ts_cur:
                return False

            # 1) Métricas
            self._ctx[self._unit].setValues(HOLDING, addr + 2, with_vals)
            # 2) Reserva a cero
            self._ctx[self._unit].setValues(HOLDING, addr + 12, [0] * (self._map.stride - 12))
            # 3) Commit: timestamp lo/hi
            self._ctx[self._unit].setValues(HOLDING, addr, [ts_lo, ts_hi])
            return True

    async def update_alarm_block(self, idx: int, ts_in: int, vals: list[int]) -> bool:
            """Escribe bloque si ts_in > ts_stored. Devuelve True si actualizó."""
            addr = self._addr(idx)
            ts_lo = ts_in & 0xFFFF
            ts_hi = (ts_in >> 16) & 0xFFFF

            async with self._lock:

                self._ctx[self._unit].setValues(HOLDING, addr + 11, vals)
                # 3) Commit: timestamp lo/hi
                self._ctx[self._unit].setValues(HOLDING, addr+9, [ts_lo, ts_hi])
                return True
