import asyncio
import websockets
import json
import time
import random
import logging


measurements_results = list()  # список для хранения результатов измерений перед их отправкой на сервер
master_connection = None  # самое первое соединение - оно управляет генерацией данных

instrument = dict()


async def connection_handler(connection, path):
    global instrument, master_connection

    logging.info('New connection {} - path {}'.format(connection.remote_address[:2], path))

    if not master_connection:
        master_connection = connection
    else:
        # master connection has already made, refuse this connection
        return

    while True:
        try:
            msg = await connection.recv()
        except websockets.exceptions.ConnectionClosed:
            # соединение закрыто, то нужно прекращать всю работу
            logging.info('connection closed', connection.remote_address[:2])

            # очищаем список соединений
            master_connection = None

            # clean all measurements results
            measurements_results.clear()

            # have no instrument from now
            instrument.clear()

            break

        logging.info("Received a message:\n %r" % msg)

        # convert incoming message to JSON dict
        json_msg = dict()
        try:
            json_msg = json.loads(msg)
        except json.JSONDecodeError:
            logging.info('wrong JSON message has been refused')
            json_msg.clear()
            continue

        instrument = json_msg


def generate_one_block(instrument_info=None):
    """ Функция генерирует один блок усредненных измерений со всех измерителей, описанных в si255_instrument_info """

    si255_result = list()

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

    si255_result.append(unix_timestamp)

    devices_info = None
    try:
        devices_info = instrument_info['devices']
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

    while True:
        # если нет информации об инструменте, то не можем генерить данные
        while not instrument:
            await asyncio.sleep(0.1)

        # сохранение текущего блока измерений
        measurements_results.append(generate_one_block(instrument))

        # получение частоты выдачи данных
        try:
            sample_rate = 1 / instrument['SampleRate']
        except (KeyError, TypeError):
            sample_rate = 1

        await asyncio.sleep(1 / sample_rate)


async def data_send_coroutine():
    """ asyncio-функция для отправки данных всем существующим подключениям """

    while True:
        for measurement in measurements_results:

            # message preparing
            try:
                msg = json.dumps(measurement)
            except json.JSONDecodeError:
                continue

            if master_connection:
                logging.info('send message {} for connection {}'.format(msg, master_connection.remote_address[:2]))

                # is client still alive?
                try:
                    await master_connection.ping()
                except websockets.exceptions.ConnectionClosed:
                    continue

                # send data block
                try:
                    await master_connection.send(msg)
                except websockets.exceptions.ConnectionClosed:
                    continue

                measurements_results.remove(measurement)
        await asyncio.sleep(0.1)


def run_server(loop=None):
    loop = asyncio.get_event_loop()

    address, port = '', 7682
    loop.run_until_complete(websockets.serve(connection_handler, address, port))
    logging.info('Server {} has been started'.format((address, port)))

    # starting coroutine for data generation
    asyncio.async(data_generation_coroutine())

    # starting coroutine for sending data to a websocket-client
    asyncio.async(data_send_coroutine())

    loop.run_forever()


if __name__ == "__main__":
    logging.basicConfig(format=u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s]  %(message)s',
                        level=logging.DEBUG) # , filename=u'UPK_server.log')
    logging.info(u'Start file')
    run_server()
