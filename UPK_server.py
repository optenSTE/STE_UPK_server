# -*- coding: utf-8 -*-

import asyncio
import websockets
import json
import time
import random
import logging
import sqlite3
import hashlib
import hyperion_python3
import datetime
import OptenFiberOpticDevices

master_connection = None  # первое соединение управляет генерацией данных

instrument_description = dict()
instrument_description_hash = 0

conn = None
cur = None

# буфер для хранения неусредненных измерений
measurements_short_buffer = dict()
measurements_short_buffer['is_locked'] = False

async def connection_handler(connection, path):
    global instrument_description, instrument_description_hash, master_connection

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

            # have no instrument from now
            # instrument_description.clear()

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

        instrument_description = json_msg
        instrument_description_hash = 100


def get_one_block(h1, instrument_info):
    """ Функция получает один блок усредненных измерений со всех измерителей, описанных в si255_instrument_info """

    si255_result = list()

    sn = h1.get_serial_number()

    return si255_result

def generate_one_block(h1=None, instrument_info=None):
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


def return_error(e):
    """ функция принимает все ошибки программы, передает их на сервер"""
    print(e)
    return None


def instrument_init(instrument_description):
    """ функция инициализирует x55 """

    # получаем адрес x55
    instrument_ip = instrument_description['IP_address']
    if not isinstance(instrument_ip, str):
        instrument_ip = instrument_ip[0]

    # соединяемся с x55
    h1 = None
    while not h1:
        try:
            h1 = hyperion_python3.Hyperion(instrument_ip)
        except hyperion_python3.HyperionError as e:
            return_error(e)
            return None

    while not h1.is_ready():
        pass

    # ToDo настраиваем параметры PeakDetection
    peak_detection_settings = instrument_description['DetectionSettings']
    print(peak_detection_settings)

    # instrument time settings
    h1.set_ntp_enabled(False)
    local_UTC_time = datetime.datetime.utcnow()
    h1.set_instrument_utc_date_time(local_UTC_time.year, local_UTC_time.month, local_UTC_time.day, local_UTC_time.hour, local_UTC_time.minute, local_UTC_time.second)

    # включаем все каналы, на которых есть решетки
    active_channels = set()
    for device in instrument_description['devices']:
        active_channels.add(int(device['x55_channel']))
    try:
        h1.set_active_full_spectrum_channel_numbers(active_channels)
    except hyperion_python3.HyperionError as e:
        return_error(e)
        return None

    # запускаем!
    h1.enable_spectrum_streaming()
    h1.enable_peak_streaming()

    return h1


async def get_data_from_x55_coroutine():
    """ получение данных с x55 и сохранение их в буфер """
    global measurements_short_buffer, cur, con

    # если нет информации об инструменте, то не можем получать данные
    while not instrument_description:
        await asyncio.sleep(0.1)

    index_of_reflection = 1.4682
    speed_of_light = 299792458.0

    # вытаскиваем информацию об устройствах
    devices = list()
    for device_description in instrument_description['devices']:

        # ToDo перенести это в класс ODTiT
        try:
            device = OptenFiberOpticDevices.ODTiT(device_description['x55_channel'])
            device.id = device_description['ID']
            device.name = device_description['Name']
            device.channel = device_description['x55_channel']
            device.ctes = device_description['CTES']
            device.e = device_description['E']
            device.size = (device_description['Asize'], device_description['Bsize'])
            device.t_min = device_description['Tmin']
            device.t_max = device_description['Tmax']
            device.f_min = device_description['Fmin']
            device.f_max = device_description['Fmax']
            device.f_reserve = device_description['Freserve']
            device.span_rope_diameter = device_description['SpanRopeDiametr']
            device.span_len = device_description['SpanRopeLen']
            device.span_rope_density = device_description['SpanRopeDensity']
            device.span_rope_EJ = device_description['SpanRopeEJ']
            device.bend_sens = device_description['Bending_sensivity']
            device.time_of_flight = int(-2E9 * device_description['Distance'] * index_of_reflection / speed_of_light)

            device.sensors[0].id = device_description['Sensor4100']['ID']
            device.sensors[0].type = device_description['Sensor4100']['type']
            device.sensors[0].name = device_description['Sensor4100']['name']
            device.sensors[0].wl0 = device_description['Sensor4100']['WL0']
            device.sensors[0].t0 = device_description['Sensor4100']['T0']
            device.sensors[0].p_max = device_description['Sensor4100']['Pmax']
            device.sensors[0].p_min = device_description['Sensor4100']['Pmin']
            device.sensors[0].st = device_description['Sensor4100']['ST']

            device.sensors[1].id = device_description['Sensor3110_1']['ID']
            device.sensors[1].type = device_description['Sensor3110_1']['type']
            device.sensors[1].name = device_description['Sensor3110_1']['name']
            device.sensors[1].wl0 = device_description['Sensor3110_1']['WL0']
            device.sensors[1].t0 = device_description['Sensor3110_1']['T0']
            device.sensors[1].p_max = device_description['Sensor3110_1']['Pmax']
            device.sensors[1].p_min = device_description['Sensor3110_1']['Pmin']
            device.sensors[1].fg = device_description['Sensor3110_1']['FG']
            device.sensors[1].ctet = device_description['Sensor3110_1']['CTET']

            device.sensors[2].id = device_description['Sensor3110_2']['ID']
            device.sensors[2].type = device_description['Sensor3110_2']['type']
            device.sensors[2].name = device_description['Sensor3110_2']['name']
            device.sensors[2].wl0 = device_description['Sensor3110_2']['WL0']
            device.sensors[2].t0 = device_description['Sensor3110_2']['T0']
            device.sensors[2].p_max = device_description['Sensor3110_2']['Pmax']
            device.sensors[2].p_min = device_description['Sensor3110_2']['Pmin']
            device.sensors[2].fg = device_description['Sensor3110_2']['FG']
            device.sensors[2].ctet = device_description['Sensor3110_2']['CTET']

        except KeyError as e:
            return_error('JSON error - key ' + str(e) + ' did not find')

        devices.append(device)

    # все каналы, на которых есть решетки
    active_channels = set()
    for device in devices:
        active_channels.add(int(device.channel))

    # инициализация x55 - пока не соединимся
    h1 = instrument_init(instrument_description)
    while not h1:
        await asyncio.sleep(hyperion_python3.DEFAULT_TIMEOUT / 1000)
        h1 = instrument_init(instrument_description)

    wavelength_start = h1.get_wavelength_start()
    wavelength_delta = h1.get_wavelength_delta()
    wavelength_finish = wavelength_start + h1.get_wavelength_number_of_points() * h1.get_wavelength_delta()

    while True:
        await asyncio.sleep(0.01)

        all_peaks = h1.get_peaks()
        spectrum = h1.get_spectrum()
        measurement_time = h1.peaksHeader.timeStampInt + h1.peaksHeader.timeStampFrac * 1E-9

        for device in devices:
            # переводим пики в пикометры, а также компенсируем все пики по расстоянию до текущего устройтва
            wls_pm = list(map(lambda wl: h1.shift_wavelength_by_offset(wl, device.time_of_flight)*1000, all_peaks.get_channel(device.channel)))

            # среди всех пиков ищем 3 подходящих для теукущего измерителя
            wls = device.find_yours_wls(wls_pm, device.channel)

            # если все три пика измерителя нашлись, то вычисляем тяжения и пр. Нет - вставляем пустышки
            if wls:
                device_output = device.get_tension_fav_ex(wls[1], wls[2], wls[0])
            else:
                device_output = device.get_tension_fav_ex(0, 0, 0, True)

            device_output.setdefault('Time', measurement_time)

            # ждем освобождения буфера
            while measurements_short_buffer['is_locked']:
                await asyncio.sleep(0.1)

            # блокируем буфер для записи в него
            measurements_short_buffer['is_locked'] = True
            try:
                measurements_short_buffer.setdefault(measurement_time, []).append(device_output)

            finally:
                # разблокируем буфер
                measurements_short_buffer['is_locked'] = False


async def save_measurements_to_db():
    global measurements_short_buffer, instrument_description

    while True:
        await asyncio.sleep(0.1)

        # ждем появления данных в буфере
        while len(measurements_short_buffer.items()) < 2:
            await asyncio.sleep(1)

        # ждем освобождения буфера
        while measurements_short_buffer['is_locked']:
            await asyncio.sleep(0.1)

        # блокируем буфер (чтобы надежно прочитать его)
        measurements_short_buffer['is_locked'] = True
        try:
            # тут храним усредненные измерения
            averaged_measurements = dict()
            
            # по всем записям в measurements_short_buffer
            for (cur_output_time, devices_output) in measurements_short_buffer.items():
                if cur_output_time == 'is_locked':
                    continue
                    
                # время усредненного блока, в которое попадает это измерение
                averaged_block_time = cur_output_time - cur_output_time % (1/instrument_description['SampleRate'])

                # создаем запись с таким временем или добавляем в существующую
                cur_mean_block = averaged_measurements.setdefault(averaged_block_time, len(devices_output)*9*[0.0])

                """
                Порядок вывода измерений
                    00  unix_timestamp
                    
                device0
                    0	num_of_measurements
                    1	t
                    2	t_std
                    3	f_av
                    4	f_av_std
                    5	f_blend
                    6	f_blend_std
                    7	ice
                    8	ice_std
                    
                device1
                    9	num_of_measurements
                    10	t
                    11	t_std
                    12	f_av
                    13	f_av_std
                    14	f_blend
                    15	f_blend_std
                    16	ice
                    17	ice_std

                device2
                    18	num_of_measurements
                    19	t
                    ...
                """
                measurements_output_order = {'T_degC': 1, 'Fav_N': 3, 'Fblend_N': 5, 'Ice_mm': 7}
                measurements_output_size = len(measurements_output_order) + 1

                # по всем измерениям текущего измерителя
                for device_num, cur_output in enumerate(devices_output):

                    # пустые измерения пропускаем
                    if not cur_output['T_degC']:
                        continue

                    # усредняем поля из списка
                    i = cur_mean_block[0 + device_num * measurements_output_size]
                    for name, index in measurements_output_order.items():
                        cur_mean_block[index + device_num * measurements_output_size] = (cur_mean_block[index + device_num * measurements_output_size]*i + cur_output[name]) \
                                                                                        / (i+1)
                    cur_mean_block[0 + device_num * measurements_output_size] += 1

            # измерения учтены, их можно удалять из measurements_short_buffer
            for cur_output_time in list(measurements_short_buffer.keys()):
                if cur_output_time == 'is_locked':
                    continue
                del measurements_short_buffer[cur_output_time]

            # выводим усредненные измерения в БД
            key_to_be_deleted = list(averaged_measurements.keys())[:-1]
            for averaged_block_time in sorted(key_to_be_deleted):
                one_measurement = [averaged_block_time] + averaged_measurements[averaged_block_time]

                # сохранение текущего блока измерений в базу данных
                try:
                    # Выполняем SQL-запрос
                    request = '''INSERT INTO measurements (equipment_description_id, measurement) VALUES (%d,  "%s")''' % \
                              (instrument_description_hash, str(one_measurement).strip('[]'))
                    cur.executescript(request)
                except sqlite3.DatabaseError as e:
                    print(u"Ошибка сохранения данных в БД:", e)
                else:
                    # Save (commit) the changes
                    con.commit()

                # эти записи уже не нужны
                del averaged_measurements[averaged_block_time]

        except Exception as e:
            return_error(e)
        finally:
            # разблокируем буфер
            measurements_short_buffer['is_locked'] = False

            # спим
            await asyncio.sleep(int(1.0 / instrument_description['SampleRate']))


async def data_generation_coroutine():
    """ asyncio-Функция для наполнения БД измерений """

    global cur, con

    while True:
        # если нет информации об инструменте, то не можем генерить данные
        while not instrument_description:
           await asyncio.sleep(0.1)

        # инициализация x55 - пока не соединимся
        h1 = None
        while not h1:
            h1 = instrument_init(instrument_description)
            await asyncio.sleep(hyperion_python3.DEFAULT_TIMEOUT/1000)

        # получение текущего блока измерений
        one_measurement = get_one_block(h1, instrument_description)

        # сохранение текущего блока измерений в базу данных
        try:
            # Выполняем SQL-запрос
            request = '''INSERT INTO measurements (equipment_description_id, measurement) VALUES (%d,  "%s")''' % (instrument_description_hash, str(one_measurement).strip('[]'))
            cur.executescript(request)
        except sqlite3.DatabaseError as e:
            print(u"Ошибка сохранения данных в БД:", e)
        else:
            # Save (commit) the changes
            con.commit()

        # получение частоты выдачи данных
        try:
            sample_rate = 1 / instrument_description['SampleRate']
        except (KeyError, TypeError):
            sample_rate = 1

        await asyncio.sleep(1 / sample_rate)


async def data_send_coroutine():
    """ asyncio-функция для отправки данных всем существующим подключениям """

    while True:
        await asyncio.sleep(0.01)

        # ждем пока будет кому отправлять
        if not master_connection:
            continue

        measurement_id, measurement = None, None
        try:
            for measurement_id, measurement in cur.execute('SELECT id, measurement FROM measurements ORDER BY id DESC LIMIT 1'):
                pass
        except sqlite3.DatabaseError:
            await asyncio.sleep(0.1)
            continue

        if not (isinstance(measurement_id, int) and isinstance(measurement, str)):
            await asyncio.sleep(0.1)
            continue

        measurement = measurement.split(', ')
        for i in range(len(measurement)):
            measurement[i] = float(measurement[i])
            if i < 2:
                measurement[i] = int(measurement[i])
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

            # удаление текущего блока измерений
            cur.execute('DELETE FROM measurements WHERE id = %d' % measurement_id)


def run_server(loop=None):
    loop = asyncio.get_event_loop()

    address, port = '192.168.1.216', 7681
    loop.run_until_complete(websockets.serve(connection_handler, address, port))
    logging.info('Server {} has been started'.format((address, port)))

    # creation or open database for measurements results
    conn = sqlite3.connect('example.db')

    # DEBUG starting coroutine for data generation
    # asyncio.async(data_generation_coroutine())

    # функция получает измерения от x55 c исходной частотой и складывает их во временный буффер
    asyncio.async(get_data_from_x55_coroutine())

    # функция усредняет данные из временного буфера и записывает их в БД
    asyncio.async(save_measurements_to_db())

    # coroutine for sending data to a websocket-client
    asyncio.async(data_send_coroutine())

    loop.run_forever()


if __name__ == "__main__":

    try:
        con = sqlite3.connect('test.db')
        cur = con.cursor()
        cur.execute('SELECT SQLITE_VERSION()')
        data = cur.fetchone()
        print("SQLite version: %s" % data)

        # создание таблицы
        try:
            # Выполняем SQL-запрос
            cur.executescript('''CREATE TABLE IF NOT EXISTS measurements 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, equipment_description_id integer,  measurement text)''')
        except sqlite3.DatabaseError as err:
            print(u"Ошибка:", err)
        else:
            print(u"Запрос на создание таблицы успешно выполнен")
            # Save (commit) the changes
            con.commit()

    except sqlite3.Error as e:
        print("SQLite3 error %s:" % e.args[0])
        exit(1)

    logging.basicConfig(format=u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s]  %(message)s', level=logging.DEBUG)  # , filename=u'UPK_server.log')
    logging.info(u'Start file')
    run_server()
