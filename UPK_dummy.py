import asyncio
import websockets
import json
import generate_JSON
import time
import random

measurements_results = list()  # список для хранения результатов измерений перед их отправкой на сервер
connections = list()  # список всех соединений

instrument = generate_JSON.si255_instrument


async def connection_handler(connection, path):
    global instrument
    print('New connection', connection.remote_address[:2])
    while True:
        try:
            msg = await connection.recv()
        except websockets.exceptions.ConnectionClosed:
            # удаляем текущее соединение из списка соединений
            print('connection closed', connection.remote_address[:2])
            connections.remove(connection)
            break
        print("Received JSON:\n %r" % msg)
        instrument = json.loads(msg)

        # добавляем соединение в список
        connections.append(connection)


def generate_one_block(instrument=None):
    """ Функция генерирует один блок усредненных измерений со всех измерителей, описанных в si255_instrument """

    # ************************************************************************************
    # Результаты измерений одного измерителя
    # ************************************************************************************
    # все значения float
    unix_timestamp = int(time.time())  # Время начала блока, сек с 01.01.1970
    num_of_measurements = 10  # Количество сырых измерений, попавших в блок
    t = 23.89  # Температура, degC
    f_av = 463.65  # Тяжение, daN
    f_blend = 34.02  # Изгиб, daN
    ice = 0.032  # Стенка эквивалентного гололеда, мм

    # вносим в данные случайную ошибку
    t_std = t / 10.0
    t = random.uniform(t - t_std / 3.0, t + t_std / 3.0)

    f_av_std = f_av / 10.0
    f_av = random.uniform(f_av - f_av_std / 3.0, f_av + f_av_std / 3.0)

    f_blend_std = f_blend / 10.0
    f_blend = random.uniform(f_blend - f_blend_std / 3.0, f_blend + f_blend_std / 3.0)

    ice_std = ice / 10.0
    ice = random.uniform(ice - ice_std / 3.0, ice + ice_std / 3.0)

    # ************************************************************************************
    # Результаты всех измерителей:
    #   Сначала идет UNIX-время измерения (время начала блока, в котором усреднялись сырые данные),
    #   затем указано количество сырых измерений в блоке,
    #   далее перечислены пара значение, СКО для каждой выходной величины с первого измерителя,
    #   после этого идут значения со всех последующих измерителей.
    # Порядок следования величин: температура [degC], тяжение [daN], изгиб [daN], стенка эквивалентного гололеда [mm].
    # ************************************************************************************

    si255_result = list()
    si255_result.append(unix_timestamp)

    devices_info = None
    try:
        devices_info = instrument['devices']
    except (KeyError, TypeError):
        pass

    for _ in range(len(devices_info)):
        si255_result.append(num_of_measurements)
        si255_result.append(t)
        si255_result.append(t_std)

        si255_result.append(f_av)
        si255_result.append(f_av_std)

        si255_result.append(f_blend)
        si255_result.append(f_blend_std)

        si255_result.append(ice)
        si255_result.append(ice_std)

    return si255_result


async def data_generation_coroutine():
    """ asyncio-Функция для наполнения списка измерений """
    # ждем первого подключения и с него берем параметры для генерации данных
    while not connections:
        await asyncio.sleep(1)

    print('Есть первое подключение, начинаем генерить данные')

    try:
        sample_rate = 1 / instrument['SampleRate']
    except (KeyError, TypeError):
        sample_rate = 1

    while True:
        measurements_results.append(generate_one_block(instrument))
        print('new data is ready', measurements_results[-1][0])

        await asyncio.sleep(1 / sample_rate)


async def data_send_coroutine():
    """ asyncio-функция для отправки данных всем существующим подключениям """

    while not measurements_results:
        await asyncio.sleep(1)
        pass
    print('Отправка данных всем подключениям')

    while True:
        for measurement in measurements_results:
            for connection in connections:
                print('send result {} to connection {}'.format(measurements_results[-1][0], connection.remote_address[:2]))
                message_to_send = json.dumps(measurement)
                await connection.send(message_to_send)
            measurements_results.remove(measurement)
        await asyncio.sleep(0.1)


loop = asyncio.get_event_loop()

start_server = websockets.serve(connection_handler, '192.168.1.216', 7681)
print('Server started')
loop.run_until_complete(start_server)
asyncio.async(data_generation_coroutine())
asyncio.async(data_send_coroutine())

loop.run_forever()
