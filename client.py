import asyncio
from random import randint

from commands import Command
from settings import settings

async def auto_tcp_client() -> None:
    reader, writer = await asyncio.open_connection(settings.server_host, settings.server_port)
    rand_int_client = randint(1, 50)

    command_templates = [
        f"{Command.ADD_VM} vm{rand_int_client} 2048 2 disk1_{rand_int_client}:100 disk2:200",
        f"{Command.UPDATE_VM} 4096 4 disk1_{rand_int_client}:150 disk3:300",
        f"{Command.LIST_DISKS}",
        f"{Command.LOGOUT}"
    ]
    if randint(0, 1) == 1:
        command_templates.insert(
            0,
            f"{Command.AUTH} vm{rand_int_client} {settings.auth_password}"
        )

    for cmd in command_templates:
        # Подставляем значение i в шаблон
        print("Отправка команды:", cmd)
        writer.write((cmd + "\n").encode())
        await writer.drain()
        # Читаем ответ от сервера
        data = await reader.readline()
        if not data:
            print("Нет ответа от сервера.")
            break
        print("Ответ сервера:", data.decode().strip())
        await asyncio.sleep(1)

    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError:
        pass

async def main() -> None:
    await auto_tcp_client()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Клиент остановлен")