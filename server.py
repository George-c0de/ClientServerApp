import asyncio
import asyncpg
import json
import sys
from asyncio import StreamReader, StreamWriter
from typing import Any
from commands import Command
from settings import settings
from asyncpg.pool import Pool

class VirtualMachine:
    """Класс, представляющий виртуальную машину (клиента)"""
    def __init__(self, vm_id: str, ram: int, cpu: int, disks: list[tuple[str, int]], writer: StreamWriter) -> None:
        self.vm_id: str = vm_id
        self.ram: int = ram
        self.cpu: int = cpu
        self.disks: list[tuple[str, int]] = disks
        self.authorized: bool = False
        self.writer: StreamWriter = writer

    def to_dict(self) -> dict[str, Any]:
        return {
            "vm_id": self.vm_id,
            "ram": self.ram,
            "cpu": self.cpu,
            "disks": self.disks,
            "authorized": self.authorized
        }

class ServerApp:
    def __init__(self) -> None:
        self.settings = settings
        # Словарь для хранения активных подключений ВМ (in-memory кеш для уменьшения запросов к БД)
        self.connected_vms: dict[str, VirtualMachine] = {}
        self._db_pool: Pool | None = None

    @property
    def db_pool(self) -> Pool:
        if self._db_pool is None:
            raise ValueError("db pool not initialized")
        return self._db_pool

    async def init_db(self) -> None:
        """Инициализация подключения к базе данных и создание таблиц, если их нет."""
        self._db_pool = await asyncpg.create_pool(
            user=self.settings.db_user,
            password=self.settings.db_password,
            database=self.settings.db_name,
            host=self.settings.db_host,
            port=self.settings.db_port
        )
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS virtual_machines (
                    vm_id TEXT PRIMARY KEY,
                    ram INTEGER,
                    cpu INTEGER,
                    authorized BOOLEAN,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS disks (
                    disk_id TEXT PRIMARY KEY,
                    capacity INTEGER,
                    vm_id TEXT REFERENCES virtual_machines(vm_id)
                );
            ''')

    async def add_vm_to_db(self, vm: VirtualMachine) -> None:
        """
        Добавляет или обновляет запись о виртуальной машине в базе данных,
        а также информацию о её жестких дисках.
        """
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO virtual_machines(vm_id, ram, cpu, authorized)
                VALUES($1, $2, $3, $4)
                ON CONFLICT (vm_id) DO UPDATE 
                SET ram = $2, cpu = $3, authorized = $4, last_seen = CURRENT_TIMESTAMP;
            ''', vm.vm_id, vm.ram, vm.cpu, vm.authorized)
            for disk in vm.disks:
                disk_id, capacity = disk
                await conn.execute('''
                    INSERT INTO disks(disk_id, capacity, vm_id)
                    VALUES($1, $2, $3)
                    ON CONFLICT (disk_id) DO UPDATE SET capacity = $2, vm_id = $3;
                ''', disk_id, capacity, vm.vm_id)

    async def list_connected_vms(self) -> list[dict[str, Any]]:
        """Возвращает список подключённых ВМ (in-memory)."""
        return [vm.to_dict() for vm in self.connected_vms.values()]

    async def list_authorized_vms(self) -> list[dict[str, Any]]:
        """Возвращает список авторизованных ВМ из базы данных."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch('SELECT * FROM virtual_machines WHERE authorized = true')
            return [dict(row) for row in rows]

    async def list_all_vms(self) -> list[dict[str, Any]]:
        """Возвращает список всех ВМ, когда-либо подключавшихся к серверу."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch('SELECT * FROM virtual_machines')
            return [dict(row) for row in rows]

    async def list_all_disks(self) -> list[dict[str, Any]]:
        """Возвращает список всех жестких дисков с информацией о привязке к ВМ."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT d.*, v.vm_id as vm_associated 
                FROM disks d LEFT JOIN virtual_machines v ON d.vm_id = v.vm_id
            ''')
            return [dict(row) for row in rows]

    async def handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        addr = writer.get_extra_info('peername')
        print(f"[INFO] Новое подключение: {addr}")

        current_vm: VirtualMachine | None = None
        try:
            while True:
                data: bytes = await reader.readline()
                if not data:
                    break
                message: str = data.decode().strip()
                if not message:
                    continue
                print(f"[DEBUG] От {addr}: {message}")
                parts: list[str] = message.split()
                try:
                    command: Command = Command(parts[0].upper())
                except ValueError:
                    writer.write("Неизвестная команда\n".encode())
                    await writer.drain()
                    continue

                if command == Command.AUTH:
                    # Формат: AUTH <vm_id> <password>
                    if len(parts) < 3:
                        writer.write("Ошибка: недостаточно аргументов для AUTH\n".encode())
                        await writer.drain()
                        continue
                    vm_id: str = parts[1]
                    password: str = parts[2]
                    if password != self.settings.auth_password:
                        writer.write("Ошибка: неверный пароль\n".encode())
                        await writer.drain()
                        continue
                    # Если клиент уже авторизован, сообщаем об этом.
                    if current_vm is not None:
                        writer.write("Вы уже авторизованы\n".encode())
                        await writer.drain()
                        continue
                    current_vm = VirtualMachine(vm_id, ram=0, cpu=0, disks=[], writer=writer)
                    current_vm.authorized = True
                    self.connected_vms[vm_id] = current_vm
                    await self.add_vm_to_db(current_vm)
                    writer.write(f"Успешная аутентификация для VM {vm_id}\n".encode())
                    await writer.drain()

                elif command == Command.ADD_VM:
                    # Команда ADD_VM разрешена только авторизованному клиенту и только для своей ВМ
                    if current_vm is None:
                        writer.write("Ошибка: не выполнена аутентификация. Сначала выполните AUTH\n".encode())
                        await writer.drain()
                        continue
                    if parts[1] != current_vm.vm_id:
                        writer.write("Ошибка: доступ запрещён. Вы можете управлять только своей виртуальной машиной.\n".encode())
                        await writer.drain()
                        continue
                    if len(parts) < 4:
                        writer.write("Ошибка: недостаточно аргументов для ADD_VM\n".encode())
                        await writer.drain()
                        continue
                    try:
                        ram: int = int(parts[2])
                        cpu: int = int(parts[3])
                    except ValueError:
                        writer.write("Ошибка: неверный формат числовых параметров\n".encode())
                        await writer.drain()
                        continue
                    disks: list[tuple[str, int]] = []
                    for disk_str in parts[4:]:
                        if ':' not in disk_str:
                            continue
                        disk_id, cap_str = disk_str.split(':', 1)
                        try:
                            capacity: int = int(cap_str)
                        except ValueError:
                            continue
                        disks.append((disk_id, capacity))
                    # Обновляем данные текущей ВМ
                    current_vm.ram = ram
                    current_vm.cpu = cpu
                    current_vm.disks = disks
                    await self.add_vm_to_db(current_vm)
                    writer.write(f"Данные для VM {current_vm.vm_id} добавлены\n".encode())
                    await writer.drain()

                elif command == Command.UPDATE_VM:
                    # Обновление данных возможно только для авторизованного клиента
                    if current_vm is None:
                        writer.write("Ошибка: не выполнена аутентификация\n".encode())
                        await writer.drain()
                        continue
                    if len(parts) < 3:
                        writer.write("Ошибка: недостаточно аргументов для UPDATE_VM\n".encode())
                        await writer.drain()
                        continue
                    try:
                        current_vm.ram = int(parts[1])
                        current_vm.cpu = int(parts[2])
                    except ValueError:
                        writer.write("Ошибка: неверный формат числовых параметров\n".encode())
                        await writer.drain()
                        continue
                    disks = []
                    for disk_str in parts[3:]:
                        if ':' not in disk_str:
                            continue
                        disk_id, cap_str = disk_str.split(':', 1)
                        try:
                            capacity = int(cap_str)
                        except ValueError:
                            continue
                        disks.append((disk_id, capacity))
                    if disks:
                        current_vm.disks = disks
                    await self.add_vm_to_db(current_vm)
                    writer.write(f"VM {current_vm.vm_id} обновлена\n".encode())
                    await writer.drain()

                elif command == Command.LIST_CONNECTED:
                    vms: list[dict[str, Any]] = await self.list_connected_vms()
                    writer.write((json.dumps(vms, ensure_ascii=False) + "\n").encode())
                    await writer.drain()

                elif command == Command.LIST_AUTHORIZED:
                    vms = await self.list_authorized_vms()
                    writer.write((json.dumps(vms, ensure_ascii=False) + "\n").encode())
                    await writer.drain()

                elif command == Command.LIST_ALL:
                    vms = await self.list_all_vms()
                    writer.write((json.dumps(vms, ensure_ascii=False) + "\n").encode())
                    await writer.drain()

                elif command == Command.LIST_DISKS:
                    disks = await self.list_all_disks()
                    writer.write((json.dumps(disks, ensure_ascii=False) + "\n").encode())
                    await writer.drain()

                elif command == Command.LOGOUT:
                    if current_vm:
                        current_vm.authorized = False
                        await self.add_vm_to_db(current_vm)
                        writer.write(f"VM {current_vm.vm_id} вышла\n".encode())
                        await writer.drain()
                        self.connected_vms.pop(current_vm.vm_id, None)
                        current_vm = None
                    else:
                        writer.write("Ошибка: не выполнена аутентификация\n".encode())
                        await writer.drain()

                else:
                    writer.write("Неизвестная команда\n".encode())
                    await writer.drain()

        except Exception as e:
            print(f"[ERROR] Ошибка при обработке клиента {addr}: {e}")
        finally:
            if current_vm:
                self.connected_vms.pop(current_vm.vm_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
            print(f"[INFO] Отключение клиента {addr}")

    async def internal_command_handler(self) -> None:
        """
        Обработка внутренних команд, вводимых через консоль сервера (например, для вывода списков).
        Для завершения работы сервера введите команду EXIT.
        """
        while True:
            cmd: str = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not cmd:
                continue
            cmd = cmd.strip()
            if cmd.upper() == "EXIT":
                print("Завершение работы сервера...")
                for vm in self.connected_vms.values():
                    vm.writer.write("Сервер закрывается\n".encode())
                    await vm.writer.drain()
                    vm.writer.close()
                    try:
                        await vm.writer.wait_closed()
                    except ConnectionResetError:
                        pass
                asyncio.get_event_loop().stop()
                break
            elif cmd.upper() == Command.LIST_CONNECTED.value:
                vms = await self.list_connected_vms()
                print("Подключенные ВМ:")
                print(json.dumps(vms, ensure_ascii=False, indent=2))
            elif cmd.upper() == Command.LIST_AUTHORIZED.value:
                vms = await self.list_authorized_vms()
                print("Авторизованные ВМ:")
                print(json.dumps(vms, ensure_ascii=False, indent=2))
            elif cmd.upper() == Command.LIST_ALL.value:
                vms = await self.list_all_vms()
                print("Все ВМ:")
                print(json.dumps(vms, ensure_ascii=False, indent=2))
            elif cmd.upper() == Command.LIST_DISKS.value:
                disks = await self.list_all_disks()
                print("Список дисков:")
                print(json.dumps(disks, ensure_ascii=False, indent=2))
            else:
                print("Неизвестная внутренняя команда")

    async def run(self) -> None:
        await self.init_db()
        server = await asyncio.start_server(self.handle_client, self.settings.server_host, self.settings.server_port)
        addr = server.sockets[0].getsockname()
        print(f"[INFO] Сервер запущен на {addr}")
        # Запускаем обработчик внутренних команд
        asyncio.create_task(self.internal_command_handler())
        async with server:
            await server.serve_forever()

if __name__ == '__main__':
    try:
        app = ServerApp()
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("[INFO] Сервер остановлен")